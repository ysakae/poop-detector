"""不具合の報告に添える診断情報（diagnose）を検証する。

報告は誰でも読めるので、認証情報・通知トピック・保存先のパス・犬の名前を含めないことを確かめる。
"""

import yaml

from poop_detector.commands import diagnose as diagnose_command
from poop_detector.config import CONNECTION_KEYS
from poop_detector.detection.roi import ROI
from poop_detector.main import main
from poop_detector.records.database import LATEST_VERSION
from poop_detector.records.events import Store

SECRET_URL = "rtsp://private:secret@camera.invalid/live"


class Judge:
    """Codex CLI を起動しない、偽の判定器。"""

    name = "偽の判定器"

    def __init__(self, problem=None):
        self.problem = problem

    def check(self):
        if self.problem:
            raise ValueError(self.problem)


def run(tmp_path, monkeypatch, capsys, config="", judge=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(diagnose_command, "build_judge", lambda provider, model: judge or Judge())
    root = tmp_path / "data"
    (tmp_path / "config.yaml").write_text(f"data_dir: {root}\n{config}")
    assert main(["--config", "config.yaml", "diagnose"]) == 0
    out = capsys.readouterr().out
    return out, yaml.safe_load(out)


def clear_connection(monkeypatch):
    for key in CONNECTION_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_diagnose_reports_versions_platform_and_features(tmp_path, monkeypatch, capsys):
    clear_connection(monkeypatch)
    _, report = run(tmp_path, monkeypatch, capsys, "features: {early_alert: true}\n")

    assert report["poop_detector"]
    assert report["python"].count(".") == 2
    assert report["os"]["system"]
    assert report["libraries"]["opencv"]
    assert "見守り通知" in report["features"]
    assert report["connection"] == {
        "rtsp_url": "未設定",
        "ntfy_topic": "未設定",
        "ntfy_early_topic": "未設定",
        "ntfy_server": "既定",
    }


def test_diagnose_never_prints_secrets_paths_or_names(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POOP_RTSP_URL", SECRET_URL)
    monkeypatch.setenv("POOP_NTFY_TOPIC", "hidden-topic-name")
    monkeypatch.setenv("POOP_NTFY_SERVER", "https://user:token@ntfy.invalid")
    out, report = run(tmp_path, monkeypatch, capsys, "pet_name: ポチ\n")

    for secret in ("secret", "camera.invalid", "hidden-topic-name", "token", "ntfy.invalid"):
        assert secret not in out
    assert str(tmp_path) not in out
    assert "ポチ" not in out
    assert report["connection"]["rtsp_url"] == "設定あり"
    assert report["connection"]["ntfy_topic"] == "設定あり"
    assert report["connection"]["ntfy_server"] == "独自"


def test_diagnose_shows_what_is_registered(tmp_path, monkeypatch, capsys):
    clear_connection(monkeypatch)
    _, before = run(tmp_path, monkeypatch, capsys)
    assert before["registered"]["roi"] is False
    assert before["registered"]["baseline"] is False

    (tmp_path / "data").mkdir()
    ROI(0, 0, 1, 1).save(tmp_path / "data" / "roi.json")
    _, after = run(tmp_path, monkeypatch, capsys)
    assert after["registered"]["roi"] is True


def test_diagnose_reads_the_database_version_without_creating_it(tmp_path, monkeypatch, capsys):
    clear_connection(monkeypatch)
    _, report = run(tmp_path, monkeypatch, capsys)
    assert report["database"] == "未作成"
    assert not (tmp_path / "data" / "events.sqlite3").exists()

    Store(tmp_path / "data")
    _, report = run(tmp_path, monkeypatch, capsys)
    assert report["database"] == f"版{LATEST_VERSION}"


def test_diagnose_reports_external_programs(tmp_path, monkeypatch, capsys):
    clear_connection(monkeypatch)
    monkeypatch.setattr(diagnose_command.shutil, "which", lambda name: None)
    _, report = run(tmp_path, monkeypatch, capsys, judge=Judge("ログインしてください"))
    assert report["programs"]["ffmpeg"] == "見つかりません"
    assert report["programs"]["video_judge"] == "偽の判定器: 使えません（ログインしてください）"

    _, report = run(tmp_path, monkeypatch, capsys)
    assert report["programs"]["video_judge"] == "偽の判定器: 使えます"


def test_diagnose_checks_configured_judge_without_disclosing_model(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(diagnose_command.shutil, "which", lambda name: None)
    (tmp_path / "config.yaml").write_text("review: {provider: gemini, model: private-model}\n")
    assert main(["--config", "config.yaml", "diagnose"]) == 0
    out = capsys.readouterr().out
    assert "private-model" not in out
    assert yaml.safe_load(out)["programs"]["video_judge"].startswith("Antigravity CLI: 使えません")
