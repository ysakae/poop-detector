"""空のトイレの基準画像と、基準画像と ROI の組の識別子。"""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import cv2

from .frames import Frame
from .roi import AnyROI


def save_baseline(path: str | Path, frame: Frame, roi: AnyROI) -> None:
    """空のトイレの基準画像と、そのときの ROI を並べて保存する。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), frame):
        raise OSError("基準画像を保存できません")
    path.with_suffix(".json").write_text(json.dumps(asdict(roi)))


def load_baseline(path: str | Path, roi: AnyROI) -> Frame:
    """基準画像を読む。保存時と ROI が違えば ValueError。"""
    path = Path(path)
    if json.loads(path.with_suffix(".json").read_text()) != asdict(roi):
        raise ValueError("ROIが変わっています。基準画像を再登録してください")
    frame = cv2.imread(str(path))
    if frame is None:
        raise ValueError("基準画像がありません")
    result: Frame = frame
    return result


def scene_signature(baseline: Frame, roi: AnyROI) -> str:
    """基準画像と ROI の組の識別子。どちらかが変われば通知済み状態を引き継がない。"""
    return hashlib.sha256(
        baseline.tobytes() + json.dumps(asdict(roi), sort_keys=True).encode()
    ).hexdigest()
