"""サブコマンドの振り分けと、複数のコマンドで使う準備処理を検証する。"""

import argparse

import pytest

from poop_detector.commands import context as commands_context
from poop_detector.commands.context import (
    CommandContext,
    CommandError,
    build_notifier,
    camera_source,
    check_limit,
    load_outside_roi,
)
from poop_detector.commands.records import require_timezones
from poop_detector.config import Config
from poop_detector.main import HANDLERS, parser
from poop_detector.notifications import MacNotifier, NtfyNotifier
from poop_detector.roi import ROI

SETTINGS = {
    "POOP_RTSP_URL": "",
    "POOP_NTFY_TOPIC": "",
    "POOP_NTFY_EARLY_TOPIC": "",
    "POOP_NTFY_SERVER": "",
}


def test_every_subcommand_has_a_handler():
    commands = next(
        action for action in parser()._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert set(commands.choices) == set(HANDLERS)
    for name, sub in commands.choices.items():
        assert sub.get_default("handler") is HANDLERS[name]


def test_enabling_dog_detection_returns_a_new_config(tmp_path):
    config = Config(data_dir=str(tmp_path))
    original = CommandContext(config)
    enabled = original.with_dog_detection()
    assert enabled.config.dog_enabled is True
    assert original.config.dog_enabled is False
    assert config.dog_enabled is False
    assert enabled.root == original.root == tmp_path


def test_context_paths_follow_data_dir(tmp_path):
    context = CommandContext(Config(data_dir=str(tmp_path)))
    assert context.roi_path == tmp_path / "roi.json"
    assert context.outside_roi_path == tmp_path / "outside-roi.json"
    assert context.baseline_path == tmp_path / "baseline" / "empty.png"
    assert context.tray_reference_path == tmp_path / "baseline" / "tray-reference.png"
    assert context.dog_model_dir == tmp_path / "models" / "dog-yolox"


def test_outside_roi_must_be_registered(tmp_path):
    context = CommandContext(Config(data_dir=str(tmp_path)))
    with pytest.raises(CommandError, match="setup-roi --outside"):
        load_outside_roi(context)
    ROI(0, 0, 1, 1).save(context.outside_roi_path)
    assert load_outside_roi(context) == ROI(0, 0, 1, 1)


@pytest.mark.parametrize("limit", [0, 51])
def test_limit_must_be_between_1_and_50(limit):
    with pytest.raises(CommandError, match="1〜50件"):
        check_limit(limit)


def test_camera_source_requires_rtsp_url():
    with pytest.raises(CommandError, match="POOP_RTSP_URL"):
        camera_source(SETTINGS)
    assert camera_source({**SETTINGS, "POOP_RTSP_URL": "rtsp://camera.invalid/"}) == (
        "rtsp://camera.invalid/"
    )


def test_notifier_reads_topics_only_for_commands_that_send(monkeypatch):
    monkeypatch.setattr(commands_context.platform, "system", lambda: "Darwin")
    settings = {**SETTINGS, "POOP_NTFY_TOPIC": "family"}
    notifier, uses_ntfy = build_notifier(Config(), settings, sends=True)
    assert isinstance(notifier, NtfyNotifier)
    assert uses_ntfy
    notifier, uses_ntfy = build_notifier(Config(), settings, sends=False)
    assert isinstance(notifier, MacNotifier)
    assert not uses_ntfy


def test_notifier_errors_are_command_errors():
    with pytest.raises(CommandError, match="POOP_NTFY_TOPIC"):
        build_notifier(Config(), {**SETTINGS, "POOP_NTFY_EARLY_TOPIC": "watch"}, sends=True)
    with pytest.raises(CommandError, match="トピック名"):
        build_notifier(Config(), {**SETTINGS, "POOP_NTFY_TOPIC": "bad topic"}, sends=True)


def test_mac_notifier_is_disabled_off_macos(monkeypatch, caplog):
    monkeypatch.setattr(commands_context.platform, "system", lambda: "Linux")
    notifier, _ = build_notifier(Config(), SETTINGS, sends=True)
    assert isinstance(notifier, MacNotifier)
    assert not notifier.enabled
    assert "POOP_NTFY_TOPIC" in caplog.text


def test_record_times_need_a_timezone():
    require_timezones(argparse.Namespace(start="2026-11-01T09:00:00+09:00", end=None))
    with pytest.raises(ValueError, match="タイムゾーン"):
        require_timezones(argparse.Namespace(start="2026-11-01T09:00:00", end=None))
    # 時刻の引数がないコマンドでも呼べる。
    require_timezones(argparse.Namespace())
