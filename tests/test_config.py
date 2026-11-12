"""設定ファイルの読み込み（機能ごとのセクションと古い平らな書き方）と検証を確かめる。"""

import dataclasses
import logging

import pytest
import yaml

from poop_detector.config import SETTINGS, Config, ConfigError


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_missing_file_uses_defaults(tmp_path):
    assert Config.load(tmp_path / "none.yaml") == Config()


def test_sections_are_read_into_settings(tmp_path):
    path = write(
        tmp_path,
        """
data_dir: saved
pet_name: ポチ
camera: {fps: 5}
detection: {verify_seconds: 4, threshold: 25}
notifications: {enabled: false}
clips: {enabled: false, disk_mb: 2048}
dog: {enabled: true, fps: 2}
features: {codex: true, early_alert: true, track_tray: true}
""",
    )
    config = Config.load(path)
    assert (config.data_dir, config.pet_name, config.fps) == ("saved", "ポチ", 5)
    assert (config.verify_seconds, config.threshold) == (4, 25)
    assert config.notifications is False
    assert (config.clip_enabled, config.clip_disk_mb) == (False, 2048)
    assert (config.dog_enabled, config.dog_fps) == (True, 2)
    assert (config.video_review_enabled, config.early_alert_enabled, config.track_tray_enabled) == (
        True,
        True,
        True,
    )
    assert config.activity_map_enabled is False


def test_flat_keys_still_work_with_a_warning(tmp_path, caplog):
    """0.1.0 の開発中の書き方（平らな項目）も読み、新しい書き方を案内する。"""
    path = write(tmp_path, "fps: 4\nclip_enabled: false\nnotifications: false\ndog_fps: 2\n")
    with caplog.at_level(logging.WARNING, logger="poop_detector.config"):
        config = Config.load(path)
    assert (config.fps, config.clip_enabled, config.notifications, config.dog_fps) == (
        4,
        False,
        False,
        2,
    )
    assert "clip_enabled → clips.enabled" in caplog.text
    assert "fps → camera.fps" in caplog.text


def test_example_config_has_every_setting_and_loads_without_warnings(caplog):
    with caplog.at_level(logging.WARNING, logger="poop_detector.config"):
        config = Config.load("config/config.example.yaml")
    assert caplog.text == ""
    with open("config/config.example.yaml") as file:
        data = yaml.safe_load(file)
    for setting in SETTINGS:
        section = data.get(setting.section, {}) if setting.section else data
        assert setting.key in section, setting.name
    assert config == Config()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("fps: 0\n", r"camera.fps は0 より大きい数"),
        ("camera: {fps: 0}\n", r"camera.fps は0 より大きい数.*値: 0"),
        ("clips: {before_seconds: 400}\n", r"clips.before_seconds は0〜300 の数"),
        ("clips: {max_width: 100}\n", r"clips.max_width は160〜3840 の整数"),
        ("dog: {confidence: 1}\n", r"dog.confidence は0 より大きく 1 未満"),
        ("dog: {fps: 0.5, exit_seconds: 2}\n", r"dog.exit_seconds は 2 / dog.fps（4秒）以上"),
        ("features: {video_review: yes-please}\n", r"features.video_review はtrue または false"),
        ("camera: {fps: '3'}\n", r"camera.fps は"),
        ("detection: {threshold: 2.5}\n", r"detection.threshold は1〜255 の整数"),
        ("clips: {color: red}\n", r"clips.color という設定項目はありません"),
        ("unknown: 1\n", r"unknown という設定項目はありません"),
        ("fps: 3\ncamera: {fps: 4}\n", r"camera.fps が二重に設定されています"),
        ("- 1\n- 2\n", r"「項目: 値」の形"),
        ("camera: {fps: [\n", r"YAML として読めません"),
    ],
)
def test_errors_name_the_setting_and_its_allowed_range(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        Config.load(write(tmp_path, text))


def test_config_is_immutable_and_replace_validates():
    config = Config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.fps = 10  # type: ignore[misc]
    assert dataclasses.replace(config, dog_enabled=True).dog_enabled is True
    with pytest.raises(ConfigError, match=r"camera\.fps"):
        dataclasses.replace(config, fps=-1)


def test_sections_round_trip(tmp_path):
    config = Config(fps=5, clip_enabled=False, video_review_enabled=False, pet_name=" ポチ ")
    assert config.pet_name == "ポチ"
    path = write(tmp_path, yaml.safe_dump(config.to_sections(), allow_unicode=True))
    assert Config.load(path) == config


def test_enabled_features_lists_what_is_on():
    config = Config(dog_enabled=True, early_alert_enabled=True, notifications=False)
    assert config.enabled_features() == ["動画保存", "犬の検出", "見守り通知"]


def test_watchdog_settings(tmp_path):
    """監視の見張り（#91）は既定で有効。知らせるまでの時間を分で変えられる。"""
    assert (Config().watchdog_enabled, Config().watchdog_minutes) == (True, 5)
    path = write(tmp_path, "watchdog: {enabled: false, minutes: 15}\n")
    config = Config.load(path)
    assert (config.watchdog_enabled, config.watchdog_minutes) == (False, 15)
    with pytest.raises(ConfigError, match=r"watchdog\.minutes"):
        Config.load(write(tmp_path, "watchdog: {minutes: 0}\n"))
