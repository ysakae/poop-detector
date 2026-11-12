"""画面から設定ファイル（config.yaml）を書き換える。

値は config.SETTINGS の表と Config の検証をそのまま使い、CLI と同じ規則で確かめる。
書き換えるのは変えた項目の値だけで、コメントと並び順は ruamel.yaml の往復の読み書きで残す。
保存は同じディレクトリの一時ファイルに書いてから置き換え、直前のファイルを .previous に1つ残す。
外からの公開に関わる項目と data_dir は READ_ONLY で、画面からは変えない。
"""

import hashlib
import io
import math
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .config import (
    RENAMED,
    SETTINGS,
    Config,
    ConfigError,
    Setting,
    check_monitor_features,
    settings_from_yaml,
)

# 誤ると外から開けなくなる・データの場所が変わる項目。画面では値を出すだけにする。
READ_ONLY = frozenset({"data_dir", "gui_host", "gui_port", "gui_public_url", "gui_trusted_proxies"})
PREVIOUS_SUFFIX = ".previous"


class SettingsConflictError(ConfigError):
    """画面を開いてから保存するまでの間に、設定ファイルがほかで書き換えられた。"""


@dataclass(frozen=True)
class Change:
    """1項目の変更。name は設定ファイルでの名前（detection.threshold など）。"""

    name: str
    before: Any
    after: Any


def digest(path: Path) -> str:
    """設定ファイルの内容の SHA-256。ファイルがなければ空文字。画面を開いた時点の版として使う。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return ""


def _parse_value(setting: Setting, raw: str | None) -> Any:
    if setting.kind is bool:
        return raw == "true"
    text = (raw or "").strip()
    if setting.kind is str:
        return text
    try:
        value: int | float = int(text) if setting.kind is int else float(text)
    except ValueError:
        raise ConfigError(f"{setting.name} は{setting.rule}で指定してください") from None
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{setting.name} は{setting.rule}で指定してください")
    return value


def parse_form(fields: Mapping[str, str], current: Config) -> Config:
    """画面のフォームの値（属性名ごとの文字列）を、検証済みの設定にする。

    真偽値はチェックが入った項目だけが "true" で届く。READ_ONLY の項目はいまの値を使う。
    """
    values = {
        setting.attr: (
            getattr(current, setting.attr)
            if setting.attr in READ_ONLY
            else _parse_value(setting, fields.get(setting.attr))
        )
        for setting in SETTINGS
    }
    config = Config(**values)
    check_monitor_features(config)
    return config


def changes(before: Config, after: Config) -> list[Change]:
    return [
        Change(setting.name, getattr(before, setting.attr), getattr(after, setting.attr))
        for setting in SETTINGS
        if getattr(before, setting.attr) != getattr(after, setting.attr)
    ]


def _round_trip() -> YAML:
    """コメントと並び順を残す読み書き。既定の typ="rt" は任意のオブジェクトを作らない。"""
    document = YAML(typ="rt")
    document.indent(mapping=2, sequence=4, offset=2)
    document.preserve_quotes = True
    return document


def _place(data: CommentedMap, setting: Setting) -> tuple[CommentedMap, str]:
    """項目を書く場所。古い書き方（平らな名前・以前の名前）で書いてあれば、その場所を使う。"""
    if not setting.section:
        return data, setting.key
    if setting.attr in data:
        return data, setting.attr
    section = data.get(setting.section)
    if isinstance(section, CommentedMap):
        for (old_section, old_key), new in RENAMED.items():
            renamed_here = new == (setting.section, setting.key) and old_section == setting.section
            if renamed_here and old_key in section:
                return section, old_key
        return section, setting.key
    section = CommentedMap()
    data[setting.section] = section
    return section, setting.key


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _check_base(path: Path, base: str) -> None:
    if digest(path) != base:
        raise SettingsConflictError(
            "設定ファイルがほかで書き換えられました。画面を開き直してから保存してください"
        )


def write(path: Path, config: Config, *, base: str) -> list[Change]:
    """config と違う項目だけを設定ファイルに書く。base は画面を開いたときの digest。"""
    _check_base(path, base)
    document = _round_trip()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    data = document.load(original) if original.strip() else None
    if data is None:
        data = CommentedMap()
    if not isinstance(data, CommentedMap):
        raise ConfigError("設定ファイルは「項目: 値」の形で書いてください")
    before = Config.load(path)
    updates = changes(before, config)
    by_name = {setting.name: setting for setting in SETTINGS}
    for change in updates:
        mapping, key = _place(data, by_name[change.name])
        mapping[key] = change.after
    stream = io.StringIO()
    document.dump(data, stream)
    if original:
        _write_atomic(path.with_name(path.name + PREVIOUS_SUFFIX), original)
    _write_atomic(path, stream.getvalue())
    if Config.load(path) != config:
        # 書いた結果が意図と違えば、直前のファイルに戻してから止める。
        _write_atomic(path, original)
        raise ConfigError("設定ファイルを書き換えられませんでした。直前の内容に戻しました")
    return updates


def has_previous(path: Path) -> bool:
    return path.with_name(path.name + PREVIOUS_SUFFIX).is_file()


def restore(path: Path, *, base: str) -> None:
    """直前の設定ファイルに戻す。いまのファイルは .previous に入れ替え、戻したことも戻せる。"""
    _check_base(path, base)
    previous = path.with_name(path.name + PREVIOUS_SUFFIX)
    if not previous.is_file():
        raise ConfigError("戻せる直前の設定がありません")
    old = previous.read_text(encoding="utf-8")
    Config(**_load_values(old))
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    _write_atomic(path, old)
    _write_atomic(previous, current)


def _load_values(text: str) -> dict[str, Any]:
    """戻す前に、直前のファイルが今の版で読めるか確かめる。"""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        raise ConfigError("直前の設定ファイルを YAML として読めません") from None
    return settings_from_yaml(data)
