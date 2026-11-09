"""OpenCV DNNで実行する犬の存在検出。姿勢や排泄は判定しない。"""

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

import cv2
import numpy as np

from .roi import full_mask, outline

REVISION = "48b10fa505aeb51fbd127fc60a5c660040e823b7"
MODEL_FILES = {
    "object_detection_yolox_2022nov.onnx": "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
    "LICENSE": "0ec3668d3274bcf29e8a29e9576d5a2cd96fc78d3c5bec4387355a796e5d9088",
}
logger = logging.getLogger(__name__)


def setup_model(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, expected in MODEL_FILES.items():
        path = directory / name
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("既存モデルのハッシュが一致しません。ファイルは保持しました")
            continue
        host = (
            "media.githubusercontent.com/media"
            if name.endswith(".onnx")
            else "raw.githubusercontent.com"
        )
        url = f"https://{host}/opencv/opencv_zoo/{REVISION}/models/object_detection_yolox/{name}"
        with urlopen(url, timeout=30) as response:
            data = response.read(60_000_001)
        if len(data) > 60_000_000 or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("取得したモデルのハッシュが一致しません")
        with path.open("xb") as output:
            output.write(data)
    return directory


class DogDetector:
    def __init__(self, directory, confidence=0.4, net=None):
        self.confidence = confidence
        if net is None:
            directory = Path(directory)
            for name, expected in MODEL_FILES.items():
                path = directory / name
                if not path.exists():
                    raise FileNotFoundError("setup-dog-modelを先に実行してください")
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError("モデルのハッシュが一致しません")
            net = cv2.dnn.readNetFromONNX(str(directory / "object_detection_yolox_2022nov.onnx"))
            net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self.net = net
        grids, strides = [], []
        for stride in (8, 16, 32):
            x, y = np.meshgrid(np.arange(640 // stride), np.arange(640 // stride))
            grid = np.stack([x, y], axis=-1).reshape(-1, 2)
            grids.append(grid)
            strides.append(np.full((len(grid), 1), stride))
        self.grids = np.concatenate(grids).astype(np.float32)
        self.strides = np.concatenate(strides).astype(np.float32)

    def predict(self, frame):
        height, width = frame.shape[:2]
        scale = min(640 / width, 640 / height)
        resized = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))))
        padded = np.full((640, 640, 3), 114, dtype=np.uint8)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        self.net.setInput(cv2.dnn.blobFromImage(padded, swapRB=True))
        output = self.net.forward().reshape(-1, 85)
        if len(output) != len(self.grids):
            raise ValueError("モデルの出力形状が一致しません")
        scores = output[:, 4] * output[:, 5 + 16]
        selected = np.where(
            (scores >= self.confidence)
            & (np.argmax(output[:, 5:], axis=1) == 16)
            & np.isfinite(output).all(axis=1)
        )[0]
        if len(selected) == 0:
            return []
        center = (output[selected, :2] + self.grids[selected]) * self.strides[selected] / scale
        sizes = np.exp(np.clip(output[selected, 2:4], -20, 20)) * self.strides[selected] / scale
        boxes = np.concatenate([center - sizes / 2, sizes], axis=1)
        keep = cv2.dnn.NMSBoxes(boxes.tolist(), scores[selected].tolist(), self.confidence, 0.5)
        results = []
        for index in np.asarray(keep, dtype=int).reshape(-1):
            x, y, w, h = boxes[index]
            x1, x2 = np.clip(np.rint([x, x + w]), 0, width - 1).astype(int)
            y1, y2 = np.clip(np.rint([y, y + h]), 0, height - 1).astype(int)
            if x2 > x1 and y2 > y1:
                results.append((int(x1), int(y1), int(x2), int(y2)))
        return results


def overlaps_roi(box, roi, shape):
    height, width = shape[:2]
    mask = full_mask(roi, shape)
    x1, y1, x2, y2 = box
    return bool(np.any(mask[max(0, y1) : min(height, y2 + 1), max(0, x1) : min(width, x2 + 1)]))


@dataclass
class DogState:
    present: bool
    on_roi: bool
    transition: str | None
    boxes: list
    inference_ms: float
    error: bool = False


class DogObserver:
    def __init__(self, detector, fps=1, exit_seconds=2):
        self.detector, self.fps, self.exit_seconds = detector, fps, exit_seconds
        self.reset()

    def reset(self):
        self.last_inference = self.last_seen = self.last_roi = float("-inf")
        self.previous_roi = False
        self.boxes = []
        self.error = False
        self.inference_ms = 0.0

    def update(self, frame, roi, now=None):
        now = time.monotonic() if now is None else now
        if now - self.last_inference >= 1 / self.fps:
            self.last_inference = now
            start = time.perf_counter()
            try:
                self.boxes = self.detector.predict(frame)
                self.error = False
                if self.boxes:
                    self.last_seen = now
                if any(overlaps_roi(box, roi, frame.shape) for box in self.boxes):
                    self.last_roi = now
            except Exception:
                self.error = True
                logger.error("犬の推論に失敗しました。排泄判定を保留して再試行します")
            self.inference_ms = (time.perf_counter() - start) * 1000
        present = now - self.last_seen < self.exit_seconds
        occupied = now - self.last_roi < self.exit_seconds
        transition = None
        if not self.error and occupied != self.previous_roi:
            transition = "dog_enter" if occupied else "dog_exit"
            self.previous_roi = occupied
        return DogState(
            present, occupied, transition, self.boxes.copy(), self.inference_ms, self.error
        )


class OutsideWatch:
    """トレー外で立ち止まった場面を、滞在ごとに一度録画する。"""

    def __init__(self, tray, scope, exit_seconds):
        self.tray = tray
        self.scope = scope
        self.exit_seconds = exit_seconds
        self.reset()

    def reset(self):
        self.anchor = None
        self.last_seen = float("-inf")
        self.recorded = False

    def update(self, boxes, shape, now):
        tray = outline(self.tray, shape)
        scope = outline(self.scope, shape)
        candidates = []
        for x1, y1, x2, y2 in boxes:
            center = ((x1 + x2) / 2, (y1 + y2) / 2)
            if (
                cv2.pointPolygonTest(tray, center, False) < 0
                and cv2.pointPolygonTest(scope, center, False) >= 0
            ):
                candidates.append(((x2 - x1) * (y2 - y1), center, min(x2 - x1, y2 - y1)))
        if not candidates:
            if now - self.last_seen >= self.exit_seconds:
                self.reset()
            return False

        if now - self.last_seen > self.exit_seconds and self.anchor is not None:
            self.reset()
        self.last_seen = now
        _, center, size = max(candidates)
        if self.anchor is None or np.linalg.norm(np.subtract(self.anchor, center)) > max(
            5, size * 0.4
        ):
            self.anchor = center
            self.started_at = now
            self.recorded = False
        return not self.recorded and now - self.started_at >= 2
