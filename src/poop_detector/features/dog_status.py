"""犬検出モデルの状態（取得済みか、ハッシュの確認）と、check-dog の直近の結果。

モデルの取得（setup-dog-model）と処理負荷の確認（check-dog）は CLI に残し、GUI はここで
状態と保存済みの結果を読むだけにする。check-dog の結果には画像の場所や検出枠を入れない。
"""

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .dog import MODEL_FILES

CHECK_FILE = "check-dog.json"
CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class DogModelStatus:
    """モデルのファイルの状態。verified はすべてそろい、ハッシュも一致したとき。"""

    missing: tuple[str, ...]
    mismatched: tuple[str, ...]

    @property
    def present(self) -> bool:
        return not self.missing

    @property
    def verified(self) -> bool:
        return not self.missing and not self.mismatched

    @property
    def absent(self) -> bool:
        """どのファイルもない（まだ取得していない）。"""
        return len(self.missing) == len(MODEL_FILES)


@dataclass(frozen=True)
class CheckResult:
    """check-dog の直近の結果。"""

    checked_at: str
    repeat: int
    dogs: int
    mean_inference_ms: float
    cpu_percent: float
    rss_mib: float


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def model_status(directory: str | Path) -> DogModelStatus:
    directory = Path(directory)
    missing = []
    mismatched = []
    for name, expected in MODEL_FILES.items():
        path = directory / name
        if not path.is_file():
            missing.append(name)
        elif _sha256(path) != expected:
            mismatched.append(name)
    return DogModelStatus(tuple(missing), tuple(mismatched))


def save_check(directory: str | Path, measured: dict[str, Any], *, repeat: int, at: str) -> None:
    """check-dog の計測を保存する。検出枠は数だけにし、画像の場所は受け取らない。"""
    result = CheckResult(
        checked_at=at,
        repeat=repeat,
        dogs=len(measured["dog_boxes"]),
        mean_inference_ms=float(measured["mean_inference_ms"]),
        cpu_percent=float(measured["cpu_percent_inference_loop"]),
        rss_mib=float(measured["rss_mib"]),
    )
    path = Path(directory) / CHECK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{CHECK_FILE}.tmp")
    with pending.open("w", encoding="utf-8") as stream:
        json.dump(asdict(result), stream, ensure_ascii=False)
    os.replace(pending, path)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError
    return float(value)


def _whole(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    return value


def load_check(directory: str | Path) -> CheckResult | None:
    """保存済みの結果。ないか、形が違えば None。"""
    try:
        data = json.loads((Path(directory) / CHECK_FILE).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data["checked_at"], str):
            return None
        return CheckResult(
            checked_at=data["checked_at"],
            repeat=_whole(data["repeat"]),
            dogs=_whole(data["dogs"]),
            mean_inference_ms=_number(data["mean_inference_ms"]),
            cpu_percent=_number(data["cpu_percent"]),
            rss_mib=_number(data["rss_mib"]),
        )
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        return None
