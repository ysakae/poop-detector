"""鳴き声らしさを YAMNet（AudioSet の521クラス）で確かめる（実験的、#295）。

鳴き声の記録（experimental.vocal）の音量と帯域の判断で見つけた区間だけに、YAMNet をかけて
犬に関わるクラスの確からしさを求める。モデルは Google の MediaPipe が配る yamnet.tflite を、
クラスの一覧は tensorflow/models の固定のコミットのものを、SHA-256 を確かめて取得する。
実行には任意の依存の ai-edge-litert（LiteRT）が要る（uv sync --extra audio-model）。
"""

import csv
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import numpy as np
from numpy.typing import NDArray

# Google の MediaPipe の音声分類のモデル（float32 の版1）。
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/"
CLASS_MAP_REVISION = "dfffd623b6be8d1d9744b8e261fbac370d17c46d"
CLASS_MAP_URL = (
    f"https://raw.githubusercontent.com/tensorflow/models/{CLASS_MAP_REVISION}"
    "/research/audioset/yamnet/"
)
MODEL_FILES = {
    "yamnet.tflite": "4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf",
    "yamnet_class_map.csv": "cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2",
}
MAX_DOWNLOAD = 20_000_000
# 犬に関わるクラス（Dog, Bark, Yip, Howl, Bow-wow, Growling, Whimper (dog)）。
DOG_CLASSES = (69, 70, 71, 72, 73, 74, 75)
# YAMNet の入力の長さ（16kHz で0.975秒）と、窓をずらす幅。
WINDOW = 15600
HOP = WINDOW // 2


def _url(name: str) -> str:
    return (MODEL_URL if name.endswith(".tflite") else CLASS_MAP_URL) + name


def setup_vocal_model(directory: str | Path, *, opener: Callable[..., Any] = urlopen) -> Path:
    """固定の場所からモデルとクラスの一覧を取得し、SHA-256 を確かめて保存する。

    既存のファイルは確かめるだけで、取り直さない。
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, expected in MODEL_FILES.items():
        path = directory / name
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(
                    "既存の鳴き声のモデルのハッシュが一致しません。ファイルは保持しました"
                )
            continue
        # 取得先は上の固定の https の URL だけ。
        with opener(_url(name), timeout=30) as response:
            data = response.read(MAX_DOWNLOAD + 1)
        if len(data) > MAX_DOWNLOAD or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("取得した鳴き声のモデルのハッシュが一致しません")
        with path.open("xb") as output:
            output.write(data)
    return directory


class YamnetClassifier:
    """区間の音から、犬に関わるクラスの確からしさの最大値と、いちばん近いクラスの名前を求める。"""

    def __init__(self, directory: str | Path, *, interpreter: Any = None) -> None:
        directory = Path(directory)
        if interpreter is None:
            try:
                from ai_edge_litert.interpreter import Interpreter
            except ImportError:
                raise ValueError(
                    "鳴き声のモデルには ai-edge-litert が要ります: uv sync --extra audio-model"
                ) from None
            interpreter = Interpreter(model_path=str(directory / "yamnet.tflite"))
        interpreter.allocate_tensors()
        self.interpreter = interpreter
        self.input = interpreter.get_input_details()[0]
        self.output = interpreter.get_output_details()[0]
        with (directory / "yamnet_class_map.csv").open(encoding="utf-8") as file:
            self.names = [row["display_name"] for row in csv.DictReader(file)]

    def classify(self, samples: NDArray[np.int16]) -> tuple[float, str]:
        """(犬らしさ, いちばん近いクラスの名前)。犬らしさは窓ごとの最大値。"""
        wave: NDArray[np.float32] = (samples / 32768).astype(np.float32)
        if len(wave) < WINDOW:
            wave = np.pad(wave, (0, WINDOW - len(wave))).astype(np.float32)
        starts = range(0, len(wave) - WINDOW + 1, HOP)
        totals = np.zeros(len(self.names), dtype=np.float64)
        best = 0.0
        for start in starts:
            scores = self._scores(wave[start : start + WINDOW])
            best = max(best, float(scores[list(DOG_CLASSES)].max()))
            totals[: len(scores)] += scores
        top = int(np.argmax(totals))
        return best, self.names[top] if top < len(self.names) else str(top)

    def _scores(self, window: NDArray[np.float32]) -> NDArray[np.float32]:
        self.interpreter.set_tensor(self.input["index"], window.reshape(self.input["shape"]))
        self.interpreter.invoke()
        scores: NDArray[np.float32] = np.asarray(
            self.interpreter.get_tensor(self.output["index"]), dtype=np.float32
        ).reshape(-1)
        return scores
