"""空のトイレの基準画像と、基準画像と ROI の組の識別子。"""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2

from ..errors import ReferenceMismatchError, SetupRequiredError
from ..frames import Frame
from .roi import ROI, AnyROI


@dataclass(frozen=True)
class SceneFiles:
    """同じトレー登録に属する監視範囲、差分の基準画像、追従用の基準画像。"""

    root: Path

    @property
    def roi_path(self) -> Path:
        return self.root / "roi.json"

    @property
    def baseline_path(self) -> Path:
        return self.root / "baseline" / "empty.png"

    @property
    def tray_reference_path(self) -> Path:
        return self.root / "baseline" / "tray-reference.png"

    def load(self) -> tuple[AnyROI, Frame]:
        if not self.roi_path.is_file():
            raise SetupRequiredError("監視範囲がありません。先に setup-roi で登録してください")
        roi = ROI.load(self.roi_path)
        return roi, load_baseline(self.baseline_path, roi)


def save_baseline(path: str | Path, frame: Frame, roi: AnyROI) -> None:
    """空のトイレの基準画像と、そのときの ROI を並べて保存する。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), frame):
        raise OSError("基準画像を保存できません")
    path.with_suffix(".json").write_text(json.dumps(asdict(roi)))


def load_baseline(path: str | Path, roi: AnyROI) -> Frame:
    """基準画像を読む。未登録なら SetupRequiredError、保存時と ROI が違えば ReferenceMismatchError。"""
    path = Path(path)
    if not path.is_file() or not path.with_suffix(".json").is_file():
        raise SetupRequiredError("基準画像がありません。先に capture-baseline で登録してください")
    if json.loads(path.with_suffix(".json").read_text()) != asdict(roi):
        raise ReferenceMismatchError("ROIが変わっています。基準画像を再登録してください")
    frame = cv2.imread(str(path))
    if frame is None:
        raise ReferenceMismatchError(
            "基準画像を読み込めません。capture-baseline --replace で登録し直してください"
        )
    result: Frame = frame
    return result


def scene_signature(baseline: Frame, roi: AnyROI) -> str:
    """基準画像と ROI の組の識別子。どちらかが変われば通知済み状態を引き継がない。"""
    return hashlib.sha256(
        baseline.tobytes() + json.dumps(asdict(roi), sort_keys=True).encode()
    ).hexdigest()
