"""CLI の各サブコマンドの分岐を、偽のカメラと一時ディレクトリで検証する。"""

import json
from types import SimpleNamespace

import pytest
import yaml
from support import spot_scene

from poop_detector.commands import context, setup
from poop_detector.config import CONNECTION_KEYS
from poop_detector.events import Store
from poop_detector.main import main
from poop_detector.roi import ROI, PolygonROI
from poop_detector.scene import load_baseline, save_baseline

SECRET_URL = "rtsp://private:secret@camera.invalid/live"


class StillCamera:
    """main の Camera の代わりに、同じフレームを返し続ける。"""

    generation = 1
    healthy = True
    timeout_ms = 100

    def __init__(self, frame):
        self.frame = frame

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return None if self.frame is None else self.frame.copy()


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """一時ディレクトリを作業場所にし、接続情報を消した状態で CLI を呼べるようにする。

    run(*args) は --config を付けて main を呼び、root はデータの保存先。
    """
    monkeypatch.chdir(tmp_path)
    for key in CONNECTION_KEYS:
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "data"
    (tmp_path / "config.yaml").write_text(f"data_dir: {root}\n")

    def run(*args):
        return main(["--config", "config.yaml", *args])

    return SimpleNamespace(run=run, root=root, path=tmp_path, monkeypatch=monkeypatch)


def use_camera(env, frame):
    env.monkeypatch.setenv("POOP_RTSP_URL", SECRET_URL)
    env.monkeypatch.setattr(context, "Camera", lambda *args: StillCamera(frame))
    env.monkeypatch.setattr(setup, "has_display", lambda: True)


def register_scene(env, roi=None):
    """ROI と基準画像を登録済みの状態にして、基準画像を返す。"""
    roi = roi or ROI(0, 0, 1, 1)
    baseline, _ = spot_scene()
    env.root.mkdir(parents=True, exist_ok=True)
    roi.save(env.root / "roi.json")
    save_baseline(env.root / "baseline" / "empty.png", roi.crop(baseline), roi)
    return baseline


# --- 入口と共通の扱い ---


def test_cli_help_and_missing_url(cli_env, capsys):
    assert cli_env.run("check-camera") == 2
    assert "RTSP" in capsys.readouterr().err
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0


def test_baseline_and_roi_cli_do_not_overwrite(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {tmp_path}\n")
    (tmp_path / "roi.json").write_text("{}")
    monkeypatch.setenv("POOP_RTSP_URL", SECRET_URL)
    monkeypatch.setattr(setup, "has_display", lambda: True)
    assert main(["--config", str(config), "setup-roi"]) == 2
    assert "secret" not in capsys.readouterr().err
    assert (tmp_path / "roi.json").read_text() == "{}"


def test_baseline_rejects_changed_roi(tmp_path):
    baseline, _ = spot_scene()
    roi = ROI(0, 0, 0.5, 0.5)
    path = tmp_path / "empty.png"
    save_baseline(path, baseline, roi)
    assert load_baseline(path, roi).shape == baseline.shape
    with pytest.raises(ValueError, match="ROIが変わっています"):
        load_baseline(path, ROI(0.5, 0.5, 0.5, 0.5))


def test_errors_never_print_the_camera_url(cli_env, capsys):
    use_camera(cli_env, None)
    assert cli_env.run("check-camera") == 2
    err = capsys.readouterr().err
    assert "secret" not in err
    assert "処理できませんでした" in err


# --- 範囲と基準画像の登録 ---


def test_check_camera_reports_frame_size(cli_env, capsys):
    baseline, _ = spot_scene()
    use_camera(cli_env, baseline)
    assert cli_env.run("check-camera") == 0
    assert "200x200" in capsys.readouterr().out


def test_setup_roi_and_capture_baseline_require_replace_to_overwrite(cli_env):
    baseline, _ = spot_scene()
    use_camera(cli_env, baseline)
    cli_env.monkeypatch.setattr(setup.cv2, "selectROI", lambda *args: (20, 20, 100, 100))
    cli_env.monkeypatch.setattr(setup.cv2, "destroyAllWindows", lambda: None)
    cli_env.root.mkdir()

    assert cli_env.run("setup-roi") == 0
    roi = ROI.load(cli_env.root / "roi.json")
    assert roi.pixels(200, 200) == (20, 20, 100, 100)
    assert cli_env.run("setup-roi") == 2

    assert cli_env.run("capture-baseline") == 0
    saved = load_baseline(cli_env.root / "baseline" / "empty.png", roi)
    assert saved.shape == (100, 100, 3)
    assert cli_env.run("capture-baseline") == 2
    assert cli_env.run("capture-baseline", "--replace") == 0


@pytest.mark.parametrize(
    ("option", "filename"), [("--polygon", "roi.json"), ("--outside", "outside-roi.json")]
)
def test_setup_roi_polygon_options_save_to_their_files(cli_env, option, filename):
    baseline, _ = spot_scene()
    use_camera(cli_env, baseline)
    polygon = PolygonROI([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]])
    cli_env.monkeypatch.setattr(setup, "select_polygon", lambda *args: polygon)
    cli_env.root.mkdir()
    assert cli_env.run("setup-roi", option) == 0
    assert ROI.load(cli_env.root / filename) == polygon


def test_setup_needs_a_display(cli_env, capsys):
    use_camera(cli_env, spot_scene()[0])
    cli_env.monkeypatch.setattr(setup, "has_display", lambda: False)
    assert cli_env.run("setup-roi") == 2
    assert cli_env.run("setup-activity-map") == 2
    assert "画面が必要" in capsys.readouterr().err


def test_setup_activity_map_requires_replace_to_overwrite(cli_env, capsys):
    from poop_detector.activity_map import ActivityLayout

    baseline, _ = spot_scene()
    use_camera(cli_env, baseline)
    floor = PolygonROI([[0.1, 0.1], [0.9, 0.2], [0.8, 0.9], [0.2, 0.8]])
    cli_env.monkeypatch.setattr(setup, "select_polygon", lambda *args: floor)
    cli_env.root.mkdir()
    assert cli_env.run("setup-activity-map") == 0
    first = ActivityLayout.load(cli_env.root).id
    assert cli_env.run("setup-activity-map") == 2
    assert "--replace" in capsys.readouterr().err
    assert cli_env.run("setup-activity-map", "--replace") == 0
    assert ActivityLayout.load(cli_env.root).id != first


def test_capture_baseline_with_tray_tracking_rejects_a_frame_without_tray(cli_env, capsys):
    baseline, _ = spot_scene()
    use_camera(cli_env, baseline)
    register_scene(cli_env)
    assert cli_env.run("capture-baseline", "--replace", "--track-tray") == 2
    assert capsys.readouterr().err.strip()
    assert not (cli_env.root / "baseline" / "tray-reference.png").exists()


# --- 記録とラベル ---


def test_record_commands_store_and_list_excretions(cli_env, capsys):
    store = Store(cli_env.root)
    event = store.record(spot_scene()[1], 0.3, "2026-11-01T00:00:00+00:00", 1)
    store.set_monitor_state("SUSPECTED", event)

    assert cli_env.run("feedback", event, "true", "--type", "urine", "--note", "朝") == 0
    assert cli_env.run("feedback", event, "true", "--start", "2026-11-01T09:00:00+09:00") == 0
    row = next(r for r in Store(cli_env.root).list() if r["id"] == event)
    assert row["excretion_type"] == "urine"
    assert row["note"] == "朝"

    assert cli_env.run("acknowledge", event) == 0
    assert Store(cli_env.root).monitor_state()["state"] == "ACKNOWLEDGED"

    assert cli_env.run("missed", "--start", "2026-11-01T10:00:00+09:00", "--type", "feces") == 0
    assert cli_env.run("record", "--type", "feces", "--start", "2026-11-01T11:00:00+09:00") == 0
    capsys.readouterr()

    assert cli_env.run("records") == 0
    assert len(json.loads(capsys.readouterr().out)) >= 2
    assert cli_env.run("events") == 0
    assert event in {row["id"] for row in json.loads(capsys.readouterr().out)}
    assert cli_env.run("metrics") == 0
    assert json.loads(capsys.readouterr().out)["missed"] == 1


def test_record_commands_require_timezone_in_times(cli_env, capsys):
    assert cli_env.run("record", "--type", "feces", "--start", "2026-11-01T11:00:00") == 2
    assert "処理できませんでした" in capsys.readouterr().err
    assert Store(cli_env.root).records() == []


def test_label_gui_passes_settings_to_the_server(cli_env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "poop_detector.gui.server.serve", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    (cli_env.path / "config.yaml").write_text(
        f"data_dir: {cli_env.root}\npet_name: ポチ\nclip_disk_mb: 64\n"
    )
    assert cli_env.run("label-gui", "--port", "0", "--no-browser") == 0
    assert calls == [((cli_env.root, 0, False), {"disk_mb": 64, "pet_name": "ポチ"})]


# --- 通知 ---


def test_early_topic_requires_main_topic(cli_env, monkeypatch, capsys):
    monkeypatch.setenv("POOP_NTFY_EARLY_TOPIC", "watch")
    assert cli_env.run("test-notification") == 2
    assert "POOP_NTFY_TOPIC" in capsys.readouterr().err


def test_unsafe_ntfy_server_is_rejected(cli_env, monkeypatch, capsys):
    monkeypatch.setenv("POOP_NTFY_TOPIC", "family")
    monkeypatch.setenv("POOP_NTFY_SERVER", "ftp://ntfy.invalid")
    assert cli_env.run("test-notification") == 2
    assert capsys.readouterr().err.strip()


def test_early_alert_test_needs_early_topic(cli_env, monkeypatch, capsys):
    monkeypatch.setenv("POOP_NTFY_TOPIC", "family")
    assert cli_env.run("test-notification", "--early-alert") == 2
    assert "POOP_NTFY_EARLY_TOPIC" in capsys.readouterr().err


@pytest.mark.parametrize(("accepted", "code"), [(True, 0), (False, 2)])
def test_mac_test_notification_reports_result(cli_env, monkeypatch, capsys, accepted, code):
    created = []

    class Notifier:
        def __init__(self, enabled, pet_name=""):
            created.append(enabled)

        def send_test(self, early_alert=False):
            return accepted

    monkeypatch.setattr(context.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(context, "MacNotifier", Notifier)
    assert cli_env.run("test-notification") == code
    assert created == [True]
    out, err = capsys.readouterr()
    assert ("通知センター" in out) is accepted
    assert bool(err.strip()) is not accepted


# --- 監視の起動条件 ---


def test_detect_outside_requires_codex_and_registered_area(cli_env, monkeypatch, capsys):
    assert cli_env.run("run", "--detect-outside") == 2
    assert "--codex" in capsys.readouterr().err
    monkeypatch.setattr("poop_detector.codex_review.require_codex", lambda: None)
    assert cli_env.run("run", "--codex", "--detect-outside") == 2
    assert "setup-roi --outside" in capsys.readouterr().err


def test_codex_requires_clips_and_login(cli_env, monkeypatch, capsys):
    (cli_env.path / "config.yaml").write_text(f"data_dir: {cli_env.root}\nclip_enabled: false\n")
    assert cli_env.run("run", "--codex") == 2
    assert "動画保存" in capsys.readouterr().err

    def missing():
        raise ValueError("Codex CLIをインストールしてください")

    monkeypatch.setattr("poop_detector.codex_review.require_codex", missing)
    assert cli_env.run("codex-review") == 2
    assert "Codex CLI" in capsys.readouterr().err


def test_codex_review_checks_limit_and_reports_counts(cli_env, monkeypatch, capsys):
    monkeypatch.setattr("poop_detector.codex_review.require_codex", lambda: None)
    assert cli_env.run("codex-review", "--limit", "0") == 2
    assert "--limit" in capsys.readouterr().err
    register_scene(cli_env)
    assert cli_env.run("codex-review") == 0
    assert "Codex判定候補 0件、失敗 0件" in capsys.readouterr().out


def test_run_requires_registered_scene(cli_env, capsys):
    use_camera(cli_env, spot_scene()[0])
    assert cli_env.run("run", "--max-frames", "1") == 2
    assert "secret" not in capsys.readouterr().err


def test_run_with_activity_map_requires_floor(cli_env, capsys):
    use_camera(cli_env, spot_scene()[0])
    register_scene(cli_env)
    assert cli_env.run("run", "--activity-map", "--max-frames", "1") == 2
    assert "setup-activity-map" in capsys.readouterr().err


# --- 犬検出モデル ---


def test_setup_dog_model_reports_saved_directory(cli_env, monkeypatch, capsys):
    monkeypatch.setattr("poop_detector.dog.setup_model", lambda directory: directory)
    assert cli_env.run("setup-dog-model") == 0
    assert "dog-yolox" in capsys.readouterr().out


def test_check_dog_validates_input_and_prints_measurements(cli_env, monkeypatch, capsys):
    import cv2

    class Detector:
        def __init__(self, directory, confidence):
            pass

        def predict(self, frame):
            return [[1, 2, 3, 4]]

    monkeypatch.setattr("poop_detector.dog.DogDetector", Detector)
    image = cli_env.path / "dog.png"
    cv2.imwrite(str(image), spot_scene()[1])
    assert cli_env.run("check-dog", "--image", str(image), "--repeat", "0") == 2
    assert cli_env.run("check-dog", "--image", str(cli_env.path / "none.png")) == 2
    capsys.readouterr()
    assert cli_env.run("check-dog", "--image", str(image), "--repeat", "2") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dog_boxes"] == [[1, 2, 3, 4]]
    assert result["mean_inference_ms"] >= 0


# --- 設定ファイルの機能（features）とフラグ ---


def write_config(env, text):
    (env.path / "config.yaml").write_text(f"data_dir: {env.root}\n{text}")


def test_features_in_config_work_without_flags(cli_env, capsys):
    """普段の起動は run だけで、機能は設定ファイルで有効にできる。"""
    write_config(cli_env, "features: {detect_outside: true}\n")
    assert cli_env.run("run") == 2
    err = capsys.readouterr().err
    assert "features.detect_outside" in err
    assert "features.codex / --codex" in err

    write_config(cli_env, "features: {activity_map: true}\n")
    use_camera(cli_env, spot_scene()[0])
    register_scene(cli_env)
    assert cli_env.run("run", "--max-frames", "1") == 2
    assert "setup-activity-map" in capsys.readouterr().err


def test_codex_in_config_needs_clip_recording(cli_env, capsys):
    write_config(cli_env, "features: {codex: true}\nclips: {enabled: false}\n")
    assert cli_env.run("run") == 2
    assert "clips.enabled" in capsys.readouterr().err


def test_flags_still_enable_features_on_top_of_the_config(cli_env, monkeypatch, capsys):
    """既存の起動コマンド（フラグ）は、設定で無効でもそのまま使える。"""
    write_config(cli_env, "features: {codex: false}\n")
    monkeypatch.setattr("poop_detector.codex_review.require_codex", lambda: None)
    assert cli_env.run("run", "--codex", "--detect-outside") == 2
    assert "setup-roi --outside" in capsys.readouterr().err


def test_run_logs_enabled_features(cli_env, caplog):
    write_config(cli_env, "features: {early_alert: true}\nclips: {enabled: false}\n")
    use_camera(cli_env, spot_scene()[0])
    register_scene(cli_env)
    with caplog.at_level("INFO", logger="poop_detector.commands.run"):
        assert cli_env.run("run", "--max-frames", "1") == 0
    assert "有効な機能: 見守り通知、通知" in caplog.text


def test_track_tray_in_config_applies_to_capture_baseline(cli_env, capsys):
    write_config(cli_env, "features: {track_tray: true}\n")
    use_camera(cli_env, spot_scene()[0])
    register_scene(cli_env)
    # トレーの縁がない画像なので、--track-tray を付けたときと同じく登録を断る。
    assert cli_env.run("capture-baseline", "--replace") == 2
    assert capsys.readouterr().err.strip()


def test_config_errors_are_shown_as_written(cli_env, capsys):
    write_config(cli_env, "clips: {before_seconds: 999}\n")
    assert cli_env.run("events") == 2
    err = capsys.readouterr().err
    assert "clips.before_seconds は0〜300 の数" in err
    assert "処理できませんでした" not in err


def test_show_config_prints_effective_settings(cli_env, capsys):
    write_config(cli_env, "features: {codex: true}\ndog: {enabled: true}\n")
    assert cli_env.run("show-config") == 0
    out = capsys.readouterr().out
    assert out.startswith("# 有効な機能: 動画保存、犬の検出、Codex 判定、通知")
    data = yaml.safe_load(out)
    assert data["features"]["codex"] is True
    assert data["camera"]["fps"] == 3.0
    assert data["data_dir"] == str(cli_env.root)
