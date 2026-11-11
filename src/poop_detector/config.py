"""YAMLの動作設定と、.envで管理する接続情報。"""

import logging
import os
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import dotenv_values

from .errors import UserError
from .video.clip_storage import DEFAULT_DISK_MIB

CONNECTION_KEYS = ("POOP_RTSP_URL", "POOP_NTFY_TOPIC", "POOP_NTFY_EARLY_TOPIC", "POOP_NTFY_SERVER")
PET_NAME_MAX_LENGTH = 32
logger = logging.getLogger(__name__)


def load_connection_settings() -> dict[str, str]:
    return load_secret_settings(CONNECTION_KEYS)


def load_secret_settings(keys: tuple[str, ...]) -> dict[str, str]:
    path = Path(".env")
    values = dotenv_values(path, interpolate=False, encoding="utf-8-sig") if path.is_file() else {}
    return {key: os.environ.get(key, values.get(key) or "") for key in keys}


def validated_pet_name(value: Any) -> str:
    """通知文と画面に埋め込む名前を検証し、前後の空白を除いて返す。"""
    if not isinstance(value, str):
        raise ValueError("pet_nameは文字列で指定してください")
    name = value.strip()
    if len(name) > PET_NAME_MAX_LENGTH:
        raise ValueError(f"pet_nameは{PET_NAME_MAX_LENGTH}文字以内にしてください")
    # 改行などの制御文字は通知や画面の表示を崩すため受け付けない。
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise ValueError("pet_nameに改行などの制御文字は使えません")
    return name


def validate_timezone(value: Any) -> None:
    """空ならマシンの設定を使う。値があれば IANA のタイムゾーン名か確かめる。"""
    if not isinstance(value, str):
        raise ValueError("timezoneはAsia/TokyoなどのIANAタイムゾーン名で指定してください")
    if not value:
        return
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezoneはAsia/TokyoなどのIANAタイムゾーン名で指定してください") from None


def apply_timezone(name: str) -> None:
    """プロセスのローカル時刻を指定のタイムゾーンにする。空なら変更しない。"""
    if not name:
        return
    validate_timezone(name)
    os.environ["TZ"] = name
    time.tzset()
    # zoneinfo と OS のタイムゾーン情報が食い違うと、tzset は黙って UTC になる。
    # 反映後の時差が zoneinfo の値と一致するか確かめ、違えば起動を止める。
    now = datetime.now(UTC)
    if now.astimezone().utcoffset() != now.astimezone(ZoneInfo(name)).utcoffset():
        raise ConfigError(f"timezone {name} をこのマシンの時刻設定に反映できませんでした")


class ConfigError(UserError, ValueError):
    """設定ファイルの誤り。項目名と許容範囲を伝える文言で、そのまま表示してよい。"""


@dataclass(frozen=True)
class Setting:
    """設定の1項目。YAML の section.key と、Config の属性の対応と検証。"""

    attr: str
    section: str  # 空ならファイルの最上位に書く
    key: str
    kind: type
    valid: Callable[[Any], bool]
    rule: str

    @property
    def name(self) -> str:
        return f"{self.section}.{self.key}" if self.section else self.key


def _number(
    low: float, high: float = float("inf"), *, low_open: bool = False, high_open: bool = False
) -> Callable[[Any], bool]:
    def valid(value: Any) -> bool:
        above = low < value if low_open else low <= value
        below = value < high if high_open else value <= high
        return bool(above and below and value < float("inf"))

    return valid


def _always(_value: Any) -> bool:
    return True


SETTINGS: tuple[Setting, ...] = (
    Setting("data_dir", "", "data_dir", str, bool, "データの保存先のディレクトリ"),
    Setting(
        "timezone",
        "",
        "timezone",
        str,
        _always,
        "Asia/Tokyo などの IANA タイムゾーン名（空ならマシンの設定）",
    ),
    Setting(
        "pet_name",
        "",
        "pet_name",
        str,
        _always,
        f"{PET_NAME_MAX_LENGTH}文字以内の犬の名前（空なら名前を出さない）",
    ),
    Setting(
        "fps",
        "camera",
        "fps",
        float,
        _number(0, low_open=True),
        "0 より大きい数（1秒あたりの判定回数）",
    ),
    Setting(
        "timeout_ms",
        "camera",
        "timeout_ms",
        int,
        _number(0, low_open=True),
        "0 より大きい整数（ミリ秒）",
    ),
    Setting("retry_seconds", "camera", "retry_seconds", float, _number(0), "0 以上の数（秒）"),
    Setting(
        "verify_seconds",
        "detection",
        "verify_seconds",
        float,
        _number(0, low_open=True),
        "0 より大きい数（秒）",
    ),
    Setting(
        "clear_seconds",
        "detection",
        "clear_seconds",
        float,
        _number(0, low_open=True),
        "0 より大きい数（秒）",
    ),
    Setting("threshold", "detection", "threshold", int, _number(1, 255), "1〜255 の整数"),
    Setting(
        "color_threshold",
        "detection",
        "color_threshold",
        int,
        _number(0, 255),
        "0〜255 の整数（0 で色差の検知を無効化）",
    ),
    Setting(
        "min_area",
        "detection",
        "min_area",
        int,
        _number(0, low_open=True),
        "0 より大きい整数（画素）",
    ),
    Setting(
        "occupied_ratio",
        "detection",
        "occupied_ratio",
        float,
        _number(0, 1, low_open=True),
        "0 より大きく 1 以下の数",
    ),
    Setting("notifications", "notifications", "enabled", bool, _always, "true または false"),
    Setting("clip_enabled", "clips", "enabled", bool, _always, "true または false"),
    Setting(
        "clip_before_seconds",
        "clips",
        "before_seconds",
        float,
        _number(0, 300),
        "0〜300 の数（秒）",
    ),
    Setting(
        "clip_after_seconds", "clips", "after_seconds", float, _number(0, 300), "0〜300 の数（秒）"
    ),
    Setting("clip_fps", "clips", "fps", float, _number(1, 30), "1〜30 の数"),
    Setting(
        "clip_max_width", "clips", "max_width", int, _number(160, 3840), "160〜3840 の整数（画素）"
    ),
    Setting("clip_memory_mb", "clips", "memory_mb", float, _number(8, 4096), "8〜4096 の数（MiB）"),
    Setting("clip_disk_mb", "clips", "disk_mb", float, _number(8), "8 以上の数（MiB）"),
    Setting("dog_enabled", "dog", "enabled", bool, _always, "true または false"),
    Setting("dog_fps", "dog", "fps", float, _number(0.2, 10), "0.2〜10 の数"),
    Setting(
        "dog_confidence",
        "dog",
        "confidence",
        float,
        _number(0, 1, low_open=True, high_open=True),
        "0 より大きく 1 未満の数",
    ),
    Setting(
        "dog_exit_seconds",
        "dog",
        "exit_seconds",
        float,
        _number(0, 60, low_open=True),
        "60 以下で、2 / dog.fps 以上の数（秒）",
    ),
    Setting(
        "dog_clip_cooldown_seconds",
        "dog",
        "clip_cooldown_seconds",
        float,
        _number(0),
        "0 以上の数（秒）",
    ),
    Setting(
        "early_alert_stay_seconds",
        "early_alert",
        "stay_seconds",
        float,
        _number(1, 600),
        "1〜600 の数（秒。同じ場所にこの時間とどまったら見守り通知を送る）",
    ),
    Setting(
        "early_alert_cooldown_seconds",
        "early_alert",
        "cooldown_seconds",
        float,
        _number(0, 3600),
        "0〜3600 の数（秒。次の見守り通知までの最短の間隔）",
    ),
    Setting(
        "review_provider",
        "review",
        "provider",
        str,
        lambda value: value in ("codex", "claude", "gemini"),
        "codex、claude、gemini のいずれか",
    ),
    Setting(
        "review_model",
        "review",
        "model",
        str,
        lambda value: value == "" or bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value)),
        "英数字で始まるモデル名（空なら Codex の既定）",
    ),
    Setting(
        "video_review_enabled",
        "features",
        "video_review",
        bool,
        _always,
        "true または false（run --video-review と同じ）",
    ),
    Setting(
        "early_alert_enabled",
        "features",
        "early_alert",
        bool,
        _always,
        "true または false（run --early-alert と同じ）",
    ),
    Setting(
        "track_tray_enabled",
        "features",
        "track_tray",
        bool,
        _always,
        "true または false（--track-tray と同じ）",
    ),
    Setting(
        "activity_map_enabled",
        "features",
        "activity_map",
        bool,
        _always,
        "true または false（run --activity-map と同じ）",
    ),
    Setting(
        "detect_outside_enabled",
        "features",
        "detect_outside",
        bool,
        _always,
        "true または false（--detect-outside と同じ）",
    ),
)
SETTINGS_BY_ATTR = {setting.attr: setting for setting in SETTINGS}
SETTINGS_BY_NAME = {(setting.section, setting.key): setting for setting in SETTINGS}
SECTIONS = tuple(dict.fromkeys(setting.section for setting in SETTINGS if setting.section))
# 以前の名前。読み込めるようにし、新しい名前を警告で案内する。
RENAMED = {("features", "codex"): ("features", "video_review")}


def _check(setting: Setting, value: Any) -> None:
    """型と範囲を確かめ、違えば項目名と許容範囲を伝える。"""
    if setting.kind is bool:
        ok = isinstance(value, bool)
    elif setting.kind is str:
        ok = isinstance(value, str)
    elif setting.kind is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
    if not ok or not setting.valid(value):
        raise ConfigError(f"{setting.name} は{setting.rule}で指定してください（値: {value!r}）")


@dataclass(frozen=True)
class Config:
    """config.yaml の動作設定。作成時に検証し、作った後は変えない（変えるときは replace）。"""

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
    clip_memory_mb: float = 128
    clip_disk_mb: float = DEFAULT_DISK_MIB
    dog_enabled: bool = False
    dog_fps: float = 1
    dog_confidence: float = 0.4
    dog_exit_seconds: float = 2
    dog_clip_cooldown_seconds: float = 60
    pet_name: str = ""
    timezone: str = ""
    early_alert_stay_seconds: float = 4
    early_alert_cooldown_seconds: float = 30
    review_provider: str = "codex"
    review_model: str = ""
    video_review_enabled: bool = False
    early_alert_enabled: bool = False
    track_tray_enabled: bool = False
    activity_map_enabled: bool = False
    detect_outside_enabled: bool = False

    def __post_init__(self) -> None:
        for setting in SETTINGS:
            _check(setting, getattr(self, setting.attr))
        try:
            # 前後の空白を除いた名前を持つ。不変なので object.__setattr__ で一度だけ設定する。
            object.__setattr__(self, "pet_name", validated_pet_name(self.pet_name))
            validate_timezone(self.timezone)
        except ValueError as error:
            raise ConfigError(str(error)) from None
        # 犬の検出の間隔より短い時間では、退場を判断できない。
        if self.dog_exit_seconds < 2 / self.dog_fps:
            raise ConfigError(
                f"dog.exit_seconds は 2 / dog.fps（{2 / self.dog_fps:g}秒）以上にしてください"
            )

    @classmethod
    def load(cls, path: str | Path) -> Self:
        """設定ファイルを読む。ファイルがなければ既定値。"""
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            raise ConfigError(f"{path} を YAML として読めません") from None
        return cls(**settings_from_yaml(data))

    def to_sections(self) -> dict[str, Any]:
        """設定ファイルと同じセクションの形の辞書（show-config で表示する）。"""
        result: dict[str, Any] = {}
        for setting in SETTINGS:
            value = getattr(self, setting.attr)
            if setting.section:
                result.setdefault(setting.section, {})[setting.key] = value
            else:
                result[setting.key] = value
        return result

    def enabled_features(self) -> list[str]:
        """有効な機能の名前（起動時のログに出す）。"""
        names = {
            "clip_enabled": "動画保存",
            "dog_enabled": "犬の検出",
            "video_review_enabled": "動画の自動判定",
            "early_alert_enabled": "見守り通知",
            "track_tray_enabled": "トレー追従",
            "activity_map_enabled": "行動マップ",
            "detect_outside_enabled": "トレー外検知",
            "notifications": "通知",
        }
        return [text for attr, text in names.items() if getattr(self, attr)]


def settings_from_yaml(data: Any) -> dict[str, Any]:
    """YAML の内容を Config の属性名の辞書にする。

    セクションの書き方（clips: {enabled: true}）と、0.1.0 の開発中の平らな書き方
    （clip_enabled: true）の両方を読む。平らな書き方には、新しい書き方を警告で案内する。
    """
    if not isinstance(data, dict):
        raise ConfigError("設定ファイルは「項目: 値」の形で書いてください")
    values: dict[str, Any] = {}
    legacy: list[str] = []
    for key, value in data.items():
        if key in SECTIONS and isinstance(value, dict):
            for inner, inner_value in value.items():
                renamed = RENAMED.get((key, inner))
                if renamed is not None:
                    legacy.append(f"{key}.{inner} → {renamed[0]}.{renamed[1]}")
                setting = SETTINGS_BY_NAME.get(renamed or (key, inner))
                if setting is None:
                    raise ConfigError(f"{key}.{inner} という設定項目はありません")
                _store(values, setting, inner_value)
        elif (setting := SETTINGS_BY_NAME.get(("", key))) is not None:
            _store(values, setting, value)
        elif (setting := SETTINGS_BY_ATTR.get(key)) is not None:
            legacy.append(f"{key} → {setting.name}")
            _store(values, setting, value)
        else:
            raise ConfigError(f"{key} という設定項目はありません")
    if legacy:
        logger.warning(
            "古い書き方の設定項目があります。次のように書き換えてください: %s", "、".join(legacy)
        )
    return values


def _store(values: dict[str, Any], setting: Setting, value: Any) -> None:
    if setting.attr in values:
        raise ConfigError(f"{setting.name} が二重に設定されています")
    values[setting.attr] = value


def with_flags(
    config: Config,
    *,
    video_review: bool = False,
    early_alert: bool = False,
    track_tray: bool = False,
    activity_map: bool = False,
    detect_outside: bool = False,
    dog: bool = False,
) -> Config:
    """コマンドラインのフラグで有効にした機能を、設定に重ねる（フラグは有効にするだけ）。"""
    return replace(
        config,
        video_review_enabled=config.video_review_enabled or video_review,
        early_alert_enabled=config.early_alert_enabled or early_alert,
        track_tray_enabled=config.track_tray_enabled or track_tray,
        activity_map_enabled=config.activity_map_enabled or activity_map,
        detect_outside_enabled=config.detect_outside_enabled or detect_outside,
        dog_enabled=config.dog_enabled or dog,
    )


def check_monitor_features(config: Config) -> Config:
    """監視で使う機能の組み合わせを確かめ、必要な機能を補った設定を返す。

    足りない条件は、設定ファイルの項目名とフラグの両方を示して伝える。
    """
    if config.detect_outside_enabled and not config.video_review_enabled:
        raise ConfigError(
            "トレー外検知（features.detect_outside / --detect-outside）には、"
            "動画の自動判定（features.video_review / --video-review）が必要です"
        )
    if config.video_review_enabled and not config.clip_enabled:
        raise ConfigError(
            "動画の自動判定（features.video_review / --video-review）には、"
            "動画保存（clips.enabled）を有効にしてください"
        )
    if (config.activity_map_enabled or config.detect_outside_enabled) and not config.dog_enabled:
        logger.info("行動マップ・トレー外検知のため、犬の検出を有効にします")
        return replace(config, dog_enabled=True)
    return config
