"""画面から設定ファイルを書き換える処理（#88）を検証する。"""

import pytest

from poop_detector.config import Config, ConfigError
from poop_detector.settings_file import (
    READ_ONLY,
    SettingsConflictError,
    changes,
    digest,
    parse_form,
    restore,
    write,
)

EXAMPLE = """\
# 監視の設定（コメントは残る）
data_dir: data
pet_name: ""

camera:
  # 1秒あたりの判定回数
  fps: 3.0
  timeout_ms: 5000

detection:
  threshold: 30   # 濃度の閾値
"""


def form(config=None, **values):
    """Config の値を、画面のフォームと同じ文字列の辞書にする（真偽値はチェックが入ったものだけ）。"""
    config = config or Config()
    fields = {}
    for name, value in vars(config).items():
        if isinstance(value, bool):
            if value:
                fields[name] = "true"
        else:
            fields[name] = str(value)
    for name, value in values.items():
        if value is None:
            fields.pop(name, None)
        else:
            fields[name] = value
    return fields


def test_form_values_become_typed_settings():
    current = Config()
    config = parse_form(
        form(threshold="25", fps="2.5", pet_name=" ポチ ", notifications=None), current
    )
    assert config.threshold == 25
    assert config.fps == 2.5
    assert config.pet_name == "ポチ"
    assert config.notifications is False


def test_invalid_values_name_the_setting_and_its_range():
    with pytest.raises(ConfigError, match=r"detection\.threshold は1〜255 の整数"):
        parse_form(form(threshold="300"), Config())
    with pytest.raises(ConfigError, match=r"camera\.fps"):
        parse_form(form(fps="速い"), Config())
    with pytest.raises(ConfigError, match=r"camera\.fps"):
        parse_form(form(fps="nan"), Config())
    with pytest.raises(ConfigError, match=r"review\.provider"):
        parse_form(form(review_provider="other"), Config())


def test_feature_combinations_are_checked():
    with pytest.raises(ConfigError, match="video_review"):
        parse_form(form(detect_outside_enabled="true"), Config())


def test_read_only_settings_keep_the_current_value():
    current = Config(gui_public_url="https://toilet.example.com")
    config = parse_form(
        form(current, data_dir="elsewhere", gui_public_url="", gui_port="80"), current
    )
    assert config.data_dir == "data"
    assert config.gui_public_url == "https://toilet.example.com"
    assert config.gui_port == current.gui_port
    assert {
        "data_dir",
        "gui_host",
        "gui_port",
        "gui_public_url",
        "gui_trusted_proxies",
    } <= READ_ONLY


def test_changes_list_setting_names_and_values():
    new = Config(threshold=25, clip_enabled=False)
    assert [(c.name, c.before, c.after) for c in changes(Config(), new)] == [
        ("detection.threshold", 30, 25),
        ("clips.enabled", True, False),
    ]


def test_write_keeps_comments_and_untouched_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE)
    old = Config.load(path)
    write(path, Config(threshold=25, clip_enabled=False), base=digest(path))
    text = path.read_text()
    assert "# 監視の設定（コメントは残る）" in text
    assert "# 1秒あたりの判定回数" in text
    assert "threshold: 25" in text
    assert "# 濃度の閾値" in text
    assert "clips:\n  enabled: false" in text
    assert Config.load(path) == Config(threshold=25, clip_enabled=False)
    # 直前のファイルを1つ残す。
    assert (tmp_path / "config.yaml.previous").read_text() == EXAMPLE
    assert old.threshold == 30


def test_write_creates_a_missing_file(tmp_path):
    path = tmp_path / "config" / "config.yaml"
    write(path, Config(pet_name="ポチ"), base=digest(path))
    assert Config.load(path).pet_name == "ポチ"
    assert not (tmp_path / "config" / "config.yaml.previous").exists()


def test_write_updates_old_style_names_in_place(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("clip_enabled: true\nfeatures:\n  codex: false\n")
    write(path, Config(clip_enabled=False, video_review_enabled=True), base=digest(path))
    text = path.read_text()
    assert "clip_enabled: false" in text
    assert "codex: true" in text
    assert "clips:" not in text
    config = Config.load(path)
    assert (config.clip_enabled, config.video_review_enabled) == (False, True)


def test_write_refuses_when_the_file_changed_meanwhile(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE)
    base = digest(path)
    path.write_text(EXAMPLE.replace("threshold: 30", "threshold: 40"))
    with pytest.raises(SettingsConflictError):
        write(path, Config(threshold=25), base=base)


def test_restore_swaps_with_the_previous_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE)
    write(path, Config(threshold=25), base=digest(path))
    restore(path, base=digest(path))
    assert Config.load(path).threshold == 30
    # 戻したこと自体も戻せる。
    restore(path, base=digest(path))
    assert Config.load(path).threshold == 25


def test_restore_without_a_previous_file_is_refused(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE)
    with pytest.raises(ConfigError, match="直前の設定"):
        restore(path, base=digest(path))
