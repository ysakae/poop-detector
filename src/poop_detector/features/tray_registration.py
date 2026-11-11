"""監視と設定画面の間でトレー交換を調整し、検証済みの基準をまとめて切り替える。"""

import fcntl
import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import UUID, uuid4

import cv2

from ..detection.roi import ROI, AnyROI
from ..detection.scene import save_baseline
from ..errors import InvalidInputError
from ..frames import Frame
from .tray_tracking import TrayTracker

Phase = Literal["idle", "begin", "paused", "apply", "applied", "failed", "cancel", "cancelled"]


class RegistrationStatus(TypedDict):
    """監視と操作側が共有する登録の状態。"""

    phase: Phase
    active: str | None
    request: str | None
    roi: dict[str, object] | None
    error: str | None


@dataclass(frozen=True)
class TrayScene:
    """同じ基準フレームから得た監視範囲・差分の基準・位置追従。"""

    roi: AnyROI
    baseline: Frame
    tracker: TrayTracker | None


class TrayRegistration:
    """交換の要求を受け付け、監視側がカメラのフレーム境界で反映する。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / ".tray-registration.json"
        self.candidate: tuple[str, Frame, TrayScene] | None = None

    def status(self) -> RegistrationStatus:
        try:
            return cast(RegistrationStatus, json.loads(self.path.read_text()))
        except FileNotFoundError:
            return {"phase": "idle", "active": None, "request": None, "roi": None, "error": None}

    def scene_root(self) -> Path:
        active = self.status()["active"]
        return self.root if active is None else self.root / "tray-scenes" / UUID(active).hex

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".tray-registration.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _write(self, state: RegistrationStatus) -> None:
        with tempfile.NamedTemporaryFile(mode="w", dir=self.root, delete=False) as file:
            path = Path(file.name)
            try:
                json.dump(state, file, ensure_ascii=False)
                file.flush()
                path.replace(self.path)
            finally:
                path.unlink(missing_ok=True)

    def begin(self) -> None:
        with self._locked():
            state = self.status()
            if state["phase"] not in ("idle", "applied", "cancelled"):
                raise InvalidInputError("トレーの交換作業は既に進行中です")
            self._write({**state, "phase": "begin", "error": None})

    def apply(self, roi: AnyROI | None = None) -> None:
        with self._locked():
            state = self.status()
            if state["phase"] not in ("paused", "failed"):
                raise InvalidInputError("先に交換作業を開始し、監視の一時保留を確認してください")
            self._write(
                {
                    **state,
                    "phase": "apply",
                    "request": uuid4().hex,
                    "roi": None if roi is None else asdict(roi),
                    "error": None,
                }
            )

    def cancel(self) -> None:
        with self._locked():
            state = self.status()
            if state["phase"] not in ("begin", "paused", "apply", "failed"):
                raise InvalidInputError("取り消せるトレー交換がありません")
            self._write({**state, "phase": "cancel"})

    def poll(self, frame: Frame | None, roi: AnyROI, *, track_tray: bool) -> TrayScene | None:
        """要求を処理し、別フレームで検証できたときだけ新しい組を返す。"""
        if self.status()["phase"] not in ("begin", "apply", "cancel"):
            return None
        with self._locked():
            state = self.status()
            if state["phase"] == "begin":
                self._write({**state, "phase": "paused"})
            elif state["phase"] == "cancel":
                self.candidate = None
                self._write(
                    {**state, "phase": "cancelled", "request": None, "roi": None, "error": None}
                )
            elif state["phase"] == "apply" and frame is not None:
                try:
                    return self._apply_frame(state, frame, roi, track_tray)
                except (OSError, TypeError, ValueError, cv2.error):
                    self.candidate = None
                    self._write(
                        {
                            **state,
                            "phase": "failed",
                            "error": "新しい基準を確認できません。空のトレーと検知範囲を確認してください",
                        }
                    )
        return None

    def _apply_frame(
        self, state: RegistrationStatus, frame: Frame, roi: AnyROI, track_tray: bool
    ) -> TrayScene | None:
        request = state["request"]
        if request is None:
            raise InvalidInputError("登録要求がありません")
        folder = self.root / "tray-scenes" / UUID(request).hex
        if self.candidate is None or self.candidate[0] != request:
            if state["roi"] is not None:
                roi = ROI.from_mapping(state["roi"])
            reference = frame.copy()
            tracker = TrayTracker(reference, roi) if track_tray else None
            self.candidate = (request, reference, TrayScene(roi, roi.crop(reference), tracker))
            return None

        _, reference, scene = self.candidate
        if frame.shape != reference.shape or (
            scene.tracker is not None and scene.tracker.align(frame) is None
        ):
            raise InvalidInputError("後続の映像で新しい基準を確認できません")
        scene.roi.save(folder / "roi.json")
        save_baseline(folder / "baseline" / "empty.png", scene.baseline, scene.roi)
        if not cv2.imwrite(str(folder / "baseline" / "tray-reference.png"), reference):
            raise OSError("追従用の基準画像を保存できません")
        self._write(
            {
                **state,
                "phase": "applied",
                "active": request,
                "request": None,
                "roi": None,
            }
        )
        self.candidate = None
        return scene
