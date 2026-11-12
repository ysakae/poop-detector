"""管理 › 判定器とモデル（状態の表示、判定器・モデルの選び直し、比較の結果、犬検出モデル。#109）を検証する。"""

import json
import re
import threading
from urllib.parse import urlencode

import pytest
from support import exchange, login, ready_clip

from poop_detector.config import Config
from poop_detector.video.clip_reviews import Assessment

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
PAGE = "/manage/models"
SECRET_KEY = "sk-ant-secret-key-123"


@pytest.fixture
def models_gui(tmp_path, monkeypatch):
    """作業ディレクトリに .env と設定ファイルを置いて起動した GUI。判定器の確認は偽物にする。"""
    from poop_detector.gui.server import make_server

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"ANTHROPIC_API_KEY={SECRET_KEY}\n")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"# 手で書いたコメント\ndata_dir: {tmp_path / 'data'}\n")
    checked = []

    def check(self):
        checked.append(self.name)
        raise ValueError("codex loginでCodex CLIにログインしてください")

    monkeypatch.setattr("poop_detector.video.codex_cli.CodexCliJudge.check", check)

    def check_cli(self):
        checked.append(self.name)

    monkeypatch.setattr("poop_detector.video.providers.ClaudeCodeJudge.check", check_cli)
    monkeypatch.setattr("poop_detector.video.providers.AntigravityJudge.check", check_cli)
    server = make_server(tmp_path / "data", 0, config_path=config_path)
    server.checked = checked
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    yield server, config_path
    server.shutdown()
    server.server_close()
    thread.join()


def page(server, cookie, path=PAGE):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def post(server, cookie, fields):
    html = page(server, cookie)
    token = re.search(r'name="csrf" value="([^"]+)"', html)[1]
    base = re.search(r'name="base" value="([^"]*)"', html)[1]
    body = urlencode({"base": base, **fields, "csrf": token})
    status, headers, data = exchange(server, "POST", PAGE, body, {**FORM, **cookie}, auth=False)
    return status, headers, data.decode()


def test_management_menu_links_the_page_for_admins_only(models_gui):
    server, _ = models_gui
    html = page(server, login(server), "/manage")
    assert f'href="{PAGE}"' in html
    assert "判定器とモデル" in html
    member = login(server, role="member")
    status, _, _ = exchange(server, "GET", PAGE, headers=member, auth=False)
    assert status == 403
    status, _, body = exchange(server, "GET", "/", headers=member, auth=False)
    assert f'href="{PAGE}"' not in body.decode()


def test_page_shows_the_current_judge_and_its_readiness(models_gui):
    server, _ = models_gui
    html = page(server, login(server))
    assert "<h2>判定器とモデル</h2>" in html
    assert "Codex CLI" in html
    assert "codex loginでCodex CLIにログインしてください" in html
    assert server.checked == ["Codex CLI"]
    assert re.search(r'<option value="codex" selected>', html)
    # 判定がまだないことと、比較の結果・犬検出モデルの案内を出す。
    assert "まだ判定していません" in html
    assert "video-benchmark" in html
    assert "setup-dog-model" in html


def test_cli_judge_shows_readiness_without_secrets(models_gui):
    server, path = models_gui
    path.write_text(path.read_text() + "review:\n  provider: claude\n  model: model-b\n")
    html = page(server, login(server))
    assert "Claude Code CLI" in html
    assert "Antigravity CLI" in html
    assert server.checked == ["Claude Code CLI"]
    assert "使える状態です" in html
    assert SECRET_KEY not in html
    assert 'name="model" value="model-b"' in html


def test_judge_counts_come_from_the_assessments(models_gui):
    server, _ = models_gui
    clips = server.app.clips
    ok, failed = ready_clip(clips), ready_clip(clips)
    clips.assessments.save(ok, Assessment(label="none"), provider="codex")
    clips.assessments.save(failed, error="判定器の実行に失敗", provider="codex")
    html = page(server, login(server))
    assert re.search(r"<td>Codex CLI</td><td>（既定）</td><td>2</td><td>1</td>", html)
    assert "失敗" in html


def test_choosing_a_judge_writes_the_settings_and_the_audit_log(models_gui):
    server, path = models_gui
    admin = login(server, "管理者")
    status, headers, _ = post(server, admin, {"provider": "gemini", "model": "gemini-x"})
    assert (status, headers["Location"]) == (303, f"{PAGE}?saved=1")
    config = Config.load(path)
    assert (config.review_provider, config.review_model) == ("gemini", "gemini-x")
    assert "# 手で書いたコメント" in path.read_text()
    entry = server.app.accounts.audit.recent()[0]
    assert (entry.actor, entry.action) == ("管理者", "judge.select")
    assert "review.provider: codex → gemini" in entry.detail
    html = page(server, admin, f"{PAGE}?saved=1")
    assert "保存しました" in html
    assert "起動し直す" in html


def test_invalid_choices_and_conflicts_are_refused(models_gui):
    server, path = models_gui
    admin = login(server)
    before = path.read_text()
    status, _, html = post(server, admin, {"provider": "other", "model": ""})
    assert status == 400
    assert "codex、claude、gemini のいずれか" in html
    status, _, html = post(server, admin, {"provider": "codex", "model": "bad model!"})
    assert status == 400
    assert "英数字で始まるモデル名" in html
    status, _, html = post(server, admin, {"provider": "claude", "model": "m", "base": "old"})
    assert status == 409
    assert "ほかで書き換えられました" in html
    assert path.read_text() == before
    status, _, html = post(server, admin, {"provider": "codex", "model": ""})
    assert status == 200
    assert "変更はありません" in html


def test_benchmark_results_are_shown_without_clip_ids(models_gui):
    server, _ = models_gui
    folder = server.app.root / "benchmarks"
    folder.mkdir(parents=True)
    scores = {
        "true_positive": 1,
        "false_positive": 1,
        "false_negative": 0,
        "precision": 0.5,
        "recall": 1.0,
    }
    report = {
        "version": 1,
        "created_at": "2026-11-10T01:00:00+00:00",
        "dataset": "night<set>.json",
        "cases": [{"id": "clip-secret-id"}],
        "summaries": {
            "codex:model-a": {
                "cases": 4,
                "uncertain": 1,
                "errors": 0,
                "types": {"feces": scores, "urine": {**scores, "precision": None}},
            }
        },
    }
    (folder / "r.json").write_text(json.dumps(report))
    html = page(server, login(server))
    assert "codex:model-a" in html
    assert "50.0%" in html
    assert "100.0%" in html
    assert "night&lt;set&gt;.json" in html
    assert "2026/11/10 10:00:00" in html
    assert "clip-secret-id" not in html


def test_dog_model_state_and_last_check_are_shown(models_gui, monkeypatch):
    from poop_detector.features import dog_status

    server, _ = models_gui
    html = page(server, login(server))
    assert "犬検出モデルはまだ取得していません" in html
    folder = server.app.root / "models" / "dog-yolox"
    folder.mkdir(parents=True)
    hashes = {}
    for name in dog_status.MODEL_FILES:
        (folder / name).write_bytes(b"x")
        hashes[name] = "0" * 64
    monkeypatch.setattr(dog_status, "MODEL_FILES", hashes)
    dog_status.save_check(
        folder,
        {
            "dog_boxes": [],
            "mean_inference_ms": 21.0,
            "cpu_percent_inference_loop": 95.5,
            "rss_mib": 410.2,
        },
        repeat=10,
        at="2026-11-11T00:00:00+00:00",
    )
    html = page(server, login(server))
    assert "ハッシュが一致しません" in html
    assert "21.0 ms" in html
    assert "95.5%" in html
    assert "2026/11/11 09:00:00" in html
