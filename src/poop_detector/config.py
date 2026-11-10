"""YAMLの動作設定と、.envで管理する接続情報。"""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import dotenv_values

from .clip_storage import DEFAULT_DISK_MIB

CONNECTION_KEYS = ("POOP_RTSP_URL", "POOP_NTFY_TOPIC", "POOP_NTFY_EARLY_TOPIC")


def load_connection_settings():
    path = Path(".env")
    values = dotenv_values(path, interpolate=False, encoding="utf-8-sig") if path.is_file() else {}
    return {key: os.environ.get(key, values.get(key) or "") for key in CONNECTION_KEYS}


@dataclass
class Config:
    fps: float = 3.0
    timeout_ms: int = 5000
    retry_seconds: float = 2.0
    verify_seconds: float = 3.0
    clear_seconds: float = 3.0
    threshold: int = 30
    color_threshold: int = 20
    min_area: int = 40
    occupied_ratio: float = 0.18
    notifications: bool = True
    data_dir: str = "data"
    clip_enabled: bool = True
    clip_before_seconds: float = 30
    clip_after_seconds: float = 30
    clip_fps: float = 5
    clip_max_width: int = 1280
    clip_memory_mb: int = 128
    clip_disk_mb: int = DEFAULT_DISK_MIB
    dog_enabled: bool = False
    dog_fps: float = 1
    dog_confidence: float = 0.4
    dog_exit_seconds: float = 2
    dog_clip_cooldown_seconds: float = 60

    def __post_init__(self):
        for name in ("fps", "timeout_ms", "verify_seconds", "clear_seconds", "min_area"):
            if not 0 < getattr(self, name) < float("inf"):
                raise ValueError(f"{name}は正の有限値にしてください")
        if not 0 <= self.retry_seconds < float("inf"):
            raise ValueError("再接続待機時間が不正です")
        if not 0 < self.occupied_ratio <= 1 or not 0 < self.threshold < 256:
            raise ValueError("画像差分設定が不正です")
        if not 0 <= self.color_threshold < 256:
            raise ValueError("color_thresholdは0〜255です（0で色差検知を無効化）")
        if not isinstance(self.notifications, bool):
            raise ValueError("notificationsはtrueまたはfalseです")
        if not isinstance(self.clip_enabled, bool):
            raise ValueError("clip_enabledはtrueまたはfalseです")
        if not isinstance(self.dog_enabled, bool):
            raise ValueError("dog_enabledはtrueまたはfalseです")
        if not (
            0.2 <= self.dog_fps <= 10
            and 0 < self.dog_confidence < 1
            and 2 / self.dog_fps <= self.dog_exit_seconds <= 60
            and 0 <= self.dog_clip_cooldown_seconds < float("inf")
        ):
            raise ValueError("犬の検出設定を確認してください")
        if not (
            0 <= self.clip_before_seconds <= 300
            and 0 <= self.clip_after_seconds <= 300
            and 1 <= self.clip_fps <= 30
            and 160 <= self.clip_max_width <= 3840
            and 8 <= self.clip_memory_mb <= 4096
            and self.clip_disk_mb >= 8
        ):
            raise ValueError("動画設定の範囲を確認してください")

    @classmethod
    def load(cls, path):
        path = Path(path)
        return cls(**(yaml.safe_load(path.read_text()) or {})) if path.exists() else cls()
