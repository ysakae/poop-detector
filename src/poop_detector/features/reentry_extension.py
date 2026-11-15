"""既存の監視に再侵入判定・動画の根拠・通知を接続する。"""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..config import Config, ConfigError
from ..detection.difference import IncrementalDetector
from ..detection.monitoring import DogExtension, FrameView, MonitorExtension, MonitorNotifier
from ..detection.roi import ROI, AnyROI, MaskedROI, PolygonROI, outline
from ..errors import InvalidInputError
from ..frames import Frame, Shape
from ..records.events import utcnow, validate_times
from ..video.recording import ClipCollector
from .reentry import Position, Presence, ReentryMonitor, ReentryStore, dog_position

LAYOUT_FILE = "reentry-layout.json"


@dataclass(frozen=True)
class Layout:
    """撮影方向を前提としない、正規化画像座標での配置。"""

    id: str
    installed_at: str
    description: str
    toilet: MaskedROI
    living: MaskedROI
    bed: MaskedROI | None
    danger: MaskedROI | None

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "Layout":
        key, at, description = (
            data[name] for name in ("configuration_id", "installed_at", "description")
        )
        if not all(isinstance(value, str) and value.strip() for value in (key, at, description)):
            raise InvalidInputError("配置ID、設置日時、配置の説明を入力してください")
        validate_times(at, None)
        return cls(
            key,
            at,
            description,
            MaskedROI(data["toilet_zone"]),
            MaskedROI(data["living_zone"]),
            MaskedROI(data["bed_zone"]) if data.get("bed_zone") is not None else None,
            MaskedROI(data["danger_zone"]) if data.get("danger_zone") is not None else None,
        )

    @classmethod
    def register(cls, store: ReentryStore, root: Path) -> "Layout":
        try:
            data = json.loads((root / LAYOUT_FILE).read_text())
            layout = cls.from_data(data)
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise ConfigError(f"{LAYOUT_FILE} の座標と配置情報を確認してください") from error
        polygon_data = json.dumps(data, sort_keys=True, ensure_ascii=False)
        with closing(store.db.connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO reentry_configurations VALUES (?, ?, ?, ?)",
                (layout.id, layout.installed_at, layout.description, polygon_data),
            )
            saved = db.execute(
                "SELECT layout FROM reentry_configurations WHERE id=?", (layout.id,)
            ).fetchone()
            if saved["layout"] != polygon_data:
                raise ConfigError(
                    "画角や配置を変更するときは、新しい configuration_id を指定してください"
                )
        return layout


def waste_position(mask: Frame, roi: AnyROI, shape: Shape, min_area: int) -> Position | None:
    """単独の局所差分を、切り出し前の画面座標に戻す。"""
    contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    candidates = [c for c in contours if cv2.contourArea(c) >= min_area]
    if len(candidates) != 1:
        return None
    contour = candidates[0]
    if cv2.contourArea(contour) >= mask.size * 0.1:
        return None
    moments = cv2.moments(contour)
    x, y = (moments[key] / moments["m00"] for key in ("m10", "m01"))
    height, width = shape[:2]
    if isinstance(roi, MaskedROI):
        left, top, _, _ = cv2.boundingRect(outline(roi, shape))
        x, y = x + left, y + top
    elif isinstance(roi, PolygonROI):
        h, w = mask.shape
        corners = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
        transform = cv2.getPerspectiveTransform(corners, roi.pixels(width, height))
        point = cv2.perspectiveTransform(np.array([[[x, y]]], np.float32), transform)[0, 0]
        x, y = float(point[0]), float(point[1])
    elif isinstance(roi, ROI):
        left, top, _, _ = roi.pixels(width, height)
        x, y = x + left, y + top
    return x / (width - 1), y / (height - 1)


class ReentryExtension(MonitorExtension):
    """推論を共有し、撮影欠落の間は再侵入の確定を保留する。"""

    def __init__(
        self,
        config: Config,
        dog: DogExtension,
        notifier: MonitorNotifier,
        collector: ClipCollector,
    ) -> None:
        self.config, self.dog, self.notifier, self.collector = config, dog, notifier, collector
        root = Path(config.data_dir)
        self.store = ReentryStore(root)
        self.layout = Layout.register(self.store, root)
        self.monitor = ReentryMonitor(
            self.store,
            self.layout.id,
            Presence(config.reentry_seconds, 2.5 / config.dog_fps),
            config.reentry_high_distance,
        )
        self.last_sample = float("-inf")

    def on_event(
        self, db: sqlite3.Connection, event_id: str, view: FrameView, detector: IncrementalDetector
    ) -> None:
        valid = detector.valid_region(view.crop, view.valid_mask)
        mask = detector.addition_mask(view.crop, valid)
        position = waste_position(mask, self.dog.roi, view.frame.shape, detector.min_area)
        if position is not None:
            zone = self.layout.danger or self.layout.toilet
            if cv2.pointPolygonTest(np.asarray(zone.points, dtype=np.float32), position, False) < 0:
                position = None
        self.store.register_in(db, event_id, self.layout.id, position)

    def on_interrupt(self) -> None:
        self.monitor.interrupt()
        self.last_sample = float("-inf")

    def on_frame(self, view: FrameView) -> None:
        tick = self.dog.observer.last_inference
        if tick == self.last_sample or tick == float("-inf"):
            return
        self.last_sample = tick
        state = self.dog.state
        side, point = (None, None)
        if state is not None and not state.error and len(state.boxes) == 1:
            side, point = dog_position(
                state.boxes[0],
                self.layout.toilet,
                self.layout.living,
                view.frame.shape,
                self.config.reentry_margin,
            )
        at = utcnow()
        notices = self.monitor.observe(side, point, tick, at)
        clip_id = self.collector.trigger(source="toilet_reentry") if notices else None
        # このフレームで上がったすべてのリスクは、同じ時刻の動画を共有する。
        high_visits = {visit for _, kind, visit in notices if kind == "high"}
        for event_id, kind, visit in notices:
            if clip_id:
                self.store.attach_clip(visit, clip_id, high=kind == "high")
            if kind != "reentry" or visit not in high_visits:
                self.notify(event_id, kind, at, clip_id)
        if self.config.reentry_uncollected_minutes > 0:
            cutoff = datetime.fromisoformat(at) - timedelta(
                minutes=self.config.reentry_uncollected_minutes
            )
            for row in self.store.pending(self.layout.id):
                if datetime.fromisoformat(row["timestamp"]) <= cutoff:
                    self.notify(row["id"], "uncollected", at, None)

    def notify(self, event_id: str, kind: str, at: str, clip_id: str | None) -> None:
        if not self.config.notifications or self.config.reentry_notice == "off":
            return
        if kind == "reentry" and self.config.reentry_notice == "high":
            return
        blocked = self.config.reentry_quiet_hours.split(",")
        local = datetime.fromisoformat(at).astimezone()
        if str(local.hour) in blocked:
            return
        if self.store.claim(event_id, kind):
            self.notifier.send_reentry(kind, event_id, local, clip_id)
