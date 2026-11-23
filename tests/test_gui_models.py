"""管理 › 判定器とモデル（状態の表示、判定器・モデルの選び直し、比較の結果、犬検出モデル。#109）を検証する。"""

import json
import re
import threading
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

import pytest
from support import exchange, login, ready_clip

from poop_detector.config import Config
from poop_detector.video.clip_reviews import Assessment
from poop_detector.video.plan_usage import QuotaGroup, QuotaWindow
from poop_detector.video.token_usage import TokenUsage

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
PAGE = "/manage/models"
PLAN = "/manage/models/plan-usage"
CATALOG = "/manage/models/catalog"
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
    # 管理のメニューはまとまりの名前を出し、この画面はそのまとまりの入口になる。
    assert "判定と録画" in html
    assert "判定器とモデル" in page(server, login(server))
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
    assert '<option value=":custom" selected>' in html
    assert 'name="custom_model" value="model-b"' in html


def test_model_menu_offers_cli_default(models_gui):
    server, _ = models_gui
    html = page(server, login(server))
    menu = re.search(r'<select name="model">(.*?)</select>', html, re.DOTALL)
    assert menu is not None
    assert '<option value="" selected>判定器の既定</option>' in menu[1]


def test_model_catalog_reads_only_the_selected_cli_without_saving(models_gui, monkeypatch):
    server, path = models_gui
    before = path.read_text()
    asked = []

    def read(provider):
        asked.append(provider)
        if provider == "claude":
            raise ValueError("private CLI output")
        return ("from-cli",)

    monkeypatch.setattr("poop_detector.gui.models_routes.list_models", read, raising=False)
    admin = login(server)
    html = page(server, admin)
    assert asked == []
    csrf = re.search(r'name="csrf" value="([^"]+)"', html)[1]
    body = urlencode({"provider": "gemini", "csrf": csrf})
    status, _, data = exchange(server, "POST", CATALOG, body, {**FORM, **admin}, auth=False)
    assert status == 200
    assert json.loads(data) == {"models": ["from-cli"]}
    status, _, _ = exchange(
        server, "POST", CATALOG, "provider=gemini", {**FORM, **admin}, auth=False
    )
    assert status == 403
    member = login(server, role="member")
    status, _, _ = exchange(server, "POST", CATALOG, body, {**FORM, **member}, auth=False)
    assert status == 403
    body = urlencode({"provider": "claude", "csrf": csrf})
    status, _, data = exchange(server, "POST", CATALOG, body, {**FORM, **admin}, auth=False)
    assert status == 503
    assert b"private CLI output" not in data
    assert asked == ["gemini", "claude"]
    assert path.read_text() == before


def test_model_menu_saves_a_preset_custom_model_and_cli_default(models_gui):
    server, path = models_gui
    admin = login(server)
    for choice, expected in (
        ({"model": "sonnet", "custom_model": "unused"}, "sonnet"),
        ({"model": "opus[1m]"}, "opus[1m]"),
        ({"model": ":custom", "custom_model": "model-b"}, "model-b"),
        ({"model": "", "custom_model": "unused"}, ""),
    ):
        status, _, _ = post(server, admin, {"provider": "claude", **choice})
        assert status == 303
        config = Config.load(path)
        assert (config.review_provider, config.review_model) == ("claude", expected)


def test_model_menu_retains_invalid_custom_input_for_correction(models_gui):
    server, _ = models_gui
    status, _, html = post(
        server,
        login(server),
        {"provider": "claude", "model": ":custom", "custom_model": "bad model!"},
    )
    assert status == 400
    assert "英数字で始まるモデル名" in html
    assert '<option value=":custom" selected>' in html
    assert 'name="custom_model" value="bad model!"' in html


def test_judge_counts_by_period_come_from_the_runs(models_gui):
    server, _ = models_gui
    runs = server.app.clips.judge_runs
    runs.record("codex", None, "review", failed=False)
    runs.record("codex", None, "review", failed=True)
    runs.record("claude", "model-b", "benchmark", failed=False, at=datetime(2020, 1, 1, tzinfo=UTC))
    html = page(server, login(server))
    for period in ("今日", "7日", "30日", "今月", "全期間"):
        assert f'<th class="count">{period}</th>' in html
    assert re.search(
        r"<td>Codex CLI</td><td>（既定）</td><td>監視・保存済み動画</td>"
        r'(<td class="count">2</td>){5}<td class="count">1</td>',
        html,
    )
    assert re.search(
        r'<td>判定器の比較</td>(<td class="count">0</td>){4}<td class="count">1</td>'
        r'<td class="count">0</td>',
        html,
    )
    # トークン数を出さなかった呼び出しだけなら、1回あたりの数は出さない。
    assert "<th>1回あたりのトークン（30日）</th>" in html
    assert "入力" not in html.split('id="usage-title"')[1].split("</table>")[0]

    runs.record("codex", None, "review", failed=False, usage=TokenUsage(17000, 4000, 200))
    runs.record("codex", None, "review", failed=False, usage=TokenUsage(19001, 0, 401))
    html = page(server, login(server))
    assert "<td>入力 18,000（キャッシュ 2,000）／出力 300</td>" in html


def test_plan_usage_is_asked_only_when_requested(models_gui):
    server, _ = models_gui
    asked = []

    def codex():
        asked.append("codex")
        resets = datetime.now(UTC) + timedelta(hours=2, minutes=10, seconds=30)
        return (QuotaGroup("Codex", QuotaWindow(30, resets), QuotaWindow(93, None)),)

    def claude():
        raise ValueError("CLI の出力 secret-output")

    server.app.plan_usage.readers = {"codex": codex, "claude": claude}
    cookie = login(server)
    html = page(server, cookie)
    assert "プランの使用量" in html
    assert "まだ問い合わせていません" in html
    assert asked == []

    member = login(server, role="member")
    status, _, _ = exchange(server, "POST", PLAN, "csrf=x", {**FORM, **member}, auth=False)
    assert status == 403
    token = re.search(r'name="csrf" value="([^"]+)"', html)[1]
    body = urlencode({"csrf": token})
    status, headers, _ = exchange(server, "POST", PLAN, body, {**FORM, **cookie}, auth=False)
    assert status == 303
    assert headers["Location"] == PAGE
    assert asked == ["codex"]
    html = page(server, cookie)
    # 制限ごとに、消費の割合を横棒のグラフで示す（数値と戻るまでの時間も添える）。
    assert 'role="meter" aria-label="Codex CLI Codex 5時間" aria-valuenow="30"' in html
    assert 'class="quota-fill quota-normal" style="width:30%"' in html
    assert 'class="quota-fill quota-high" style="width:93%"' in html
    assert "30% 使用" in html
    assert "93% 使用" in html
    assert "あと2時間10分で戻る" in html
    assert "Claude Code CLI" in html
    assert "不明（CLI のログインと版を確認してください）" in html
    assert "secret-output" not in html


def test_choosing_a_judge_writes_the_settings_and_the_audit_log(models_gui, monkeypatch):
    server, path = models_gui
    monkeypatch.setattr("poop_detector.video.gemini_api.GeminiApiJudge.check", lambda self: None)
    admin = login(server, "管理者")
    status, headers, _ = post(
        server, admin, {"provider": "gemini-api", "model": "gemini-3.8-flash"}
    )
    assert (status, headers["Location"]) == (303, f"{PAGE}?saved=1")
    config = Config.load(path)
    assert (config.review_provider, config.review_model) == ("gemini-api", "gemini-3.8-flash")
    assert "# 手で書いたコメント" in path.read_text()
    entry = server.app.accounts.audit.recent()[0]
    assert (entry.actor, entry.action) == ("管理者", "judge.select")
    assert "review.provider: codex → gemini-api" in entry.detail
    html = page(server, admin, f"{PAGE}?saved=1")
    assert '<option value="gemini-api" selected>' in html
    assert "使える状態です" in html
    assert "保存しました" in html
    assert "次の動画の判定から新しい判定器を使います" in html


def test_invalid_choices_and_conflicts_are_refused(models_gui):
    server, path = models_gui
    admin = login(server)
    before = path.read_text()
    status, _, html = post(server, admin, {"provider": "other", "model": ""})
    assert status == 400
    assert "codex、claude、gemini、gemini-api のいずれか" in html
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


def test_datasets_show_counts_by_split_condition_and_problems(models_gui):
    from poop_detector.video import benchmark_sets
    from poop_detector.video.clip_labels import ClipLabel

    server, _ = models_gui
    clips = server.app.clips
    feces, none = ready_clip(clips), ready_clip(clips)
    clips.update(none, start_at="2026-11-10T06:10:00+00:00")
    for clip_id in (feces, none):
        (clips.root / "clips" / f"{clip_id}.mp4").write_bytes(b"x")
        clips.update(clip_id, scene_signature="scene-a")
    clips.labels.save(feces, ClipLabel("feces"))
    clips.labels.save(none, ClipLabel("none", activity="sniffing"))
    clips.store.add_excretion("urine", "2026-11-01T01:00:00+00:00", missed=True)
    dataset = benchmark_sets.build(
        clips,
        name="night<set>",
        created_at="2026-11-12T00:00:00+00:00",
        scenes={},
        final_since=date(2026, 11, 10),
    )
    folder = benchmark_sets.datasets_dir(server.app.root)
    benchmark_sets.write_new(folder / "a.json", dataset)
    (folder / "broken.json").write_text("{")
    html = page(server, login(server))
    assert "night&lt;set&gt;" in html
    assert "下書き" in html
    assert re.search(r"<td>調整用</td><td>1</td><td>0</td><td>0</td><td>0</td>", html)
    assert re.search(r"<td>最終評価用</td><td>0</td><td>0</td><td>0</td><td>1</td>", html)
    assert "録画なし 1件" in html
    assert "当時の ROI・基準画像が見つからない画角: 1件" in html
    assert "読めない評価セットのファイルが 1 件" in html


def test_benchmark_shows_each_clip_answer_and_judge_versions(models_gui):
    server, _ = models_gui
    clips = server.app.clips
    clip_id = ready_clip(clips)
    folder = server.app.root / "benchmarks"
    folder.mkdir(parents=True)
    scores = {
        "true_positive": 1,
        "false_positive": 0,
        "false_negative": 0,
        "precision": 1.0,
        "recall": 1.0,
    }
    summary = {"cases": 2, "uncertain": 0, "errors": 1, "types": {"feces": scores, "urine": scores}}
    report = {
        "version": 2,
        "created_at": "2026-11-10T01:00:00+00:00",
        "dataset": "eval-v1.json",
        "dataset_info": {"name": "eval", "version": 1, "content_sha256": "0" * 64},
        "judges": {
            "codex:model-a": {"provider": "codex", "model": "model-a", "cli_version": "codex 1.2"},
            "claude:model-b": {"provider": "claude", "model": "model-b", "cli_version": None},
        },
        "cases": [
            {
                "id": clip_id,
                "label": "feces",
                "split": "final",
                "tags": ["夜間"],
                "results": {
                    "codex:model-a": {"label": "feces"},
                    "claude:model-b": {"label": "error"},
                },
            },
            {
                "id": "not-in-ledger",
                "label": "none",
                "results": {
                    "codex:model-a": {"label": "none"},
                    "claude:model-b": {"label": "none"},
                },
            },
        ],
        "summaries": {"codex:model-a": summary, "claude:model-b": summary},
    }
    (folder / "r.json").write_text(json.dumps(report))
    html = page(server, login(server))
    assert "版1" in html
    assert "codex 1.2" in html
    assert f'href="/clips?id={clip_id}"' in html
    assert "not-in-ledger" not in html
    assert "最終評価用" in html
    assert re.search(r'<td class="answer-miss">エラー</td>', html)


def test_review_status_shows_the_cap_postponed_clips_and_judge_failures(models_gui):
    server, config_path = models_gui
    config_path.write_text(
        config_path.read_text() + "review: {max_per_hour: 4, fallback: claude}\n"
    )
    html = page(server, login(server))
    assert '<h3 id="review-status-title">監視中の判定の状況</h3>' in html
    assert "0回（上限 4回）" in html
    assert "すべての動画を判定します" in html
    assert "Claude Code CLI" in html

    runs = server.app.clips.judge_runs
    for _ in range(3):
        runs.record("codex", None, "review", failed=True)
    server.app.outages.begin("judge", datetime(2026, 11, 14, 22, 30, tzinfo=UTC).isoformat())
    html = page(server, login(server))
    assert "3回（上限 4回）" in html
    assert "犬の出入り、トレー外の滞在を後回しにしています" in html
    assert "2026/11/15 07:30:00 から失敗が続いています" in html

    runs.record("codex", None, "review", failed=False)
    html = page(server, login(server))
    assert "上限に達したため、枠が空くまで判定を待っています" in html


def test_tray_stay_report_groups_dog_clips_and_keeps_unknowns(models_gui):
    server, _ = models_gui
    clips = server.app.clips
    cases = [
        ("dog_visit", 0, "urine"),
        ("dog_enter", 4.9, "none"),
        ("dog_exit", 5, None),
        ("dog_visit", 10, "feces"),
        ("dog_visit", 20, None),
        ("dog_exit", None, "none"),
        ("early_alert", 3, "feces"),
    ]
    for source, stay, label in cases:
        clip = ready_clip(clips)
        clips.update(clip, source=source, tray_stay_seconds=stay)
        if label is not None:
            assert clips.assessments.save(clip, Assessment(label=label))
    interrupted = ready_clip(clips)
    clips.update(interrupted, source="dog_exit", tray_stay_seconds=1, interrupted=1)
    assert clips.assessments.save(interrupted, error="人工データの判定失敗")
    old = ready_clip(clips)
    clips.update(old, source="dog_exit", tray_stay_seconds=2)
    with clips.store.connect() as db:
        db.execute(
            "UPDATE clips SET requested_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(days=8)).isoformat(), old),
        )

    html = page(server, login(server))
    assert "犬の出入りとトレー滞在時間（直近7日間）" in html
    for name, total, completed, candidates in (
        ("5秒未満", 2, 2, 1),
        ("5秒以上10秒未満", 1, 0, 0),
        ("10秒以上20秒未満", 1, 1, 1),
        ("20秒以上", 1, 0, 0),
        ("計測なし", 2, 1, 0),
    ):
        row = (
            f'<th scope="row">{name}</th><td>{total}</td><td>{completed}</td><td>{candidates}</td>'
        )
        assert row in html


def test_quota_window_view_levels_and_reset_time():
    from poop_detector.gui.models_routes import quota_view

    now = datetime(2026, 11, 18, 3, 0, tzinfo=UTC)
    view = quota_view("5時間", QuotaWindow(75, now + timedelta(days=2, hours=3, minutes=5)), now)
    assert (view["percent"], view["level"]) == (75, "warn")
    assert view["resets"].startswith("あと2日3時間で戻る（")
    assert quota_view("週間", QuotaWindow(10, now + timedelta(minutes=4)), now)["resets"] == (
        "あと4分で戻る（11/18 12:04）"
    )
    assert quota_view("週間", QuotaWindow(10, now - timedelta(minutes=1)), now)["resets"] == (
        "まもなく戻ります"
    )
    assert quota_view("週間", QuotaWindow(100, None), now)["level"] == "high"
    assert quota_view("週間", QuotaWindow(69, None), now)["resets"] == ""
    assert quota_view("週間", None, now) == {"label": "週間", "percent": None}


def test_review_status_breaks_down_recent_failures_by_kind(models_gui):
    server, _ = models_gui
    html = page(server, login(server))
    assert "失敗の内訳（24時間以内）" in html
    assert "失敗はありません" in html

    runs = server.app.clips.judge_runs
    for _ in range(2):
        runs.record("gemini", None, "review", failed=True, failure="timeout")
    runs.record("gemini", None, "review", failed=True, failure="rate_limited")
    runs.record("gemini", None, "review", failed=True)
    old = datetime.now(UTC) - timedelta(days=2)
    runs.record("gemini", None, "review", failed=True, failure="exit", at=old)
    html = page(server, login(server))
    assert "タイムアウト 2回、利用制限・混雑 1回、不明 1回" in html
    assert "異常終了" not in html


def test_readiness_is_reused_for_a_short_while(models_gui):
    # CLI のログイン状態の確認は数秒かかるので、開き直すたびには確かめない。
    server, path = models_gui
    cookie = login(server)
    page(server, cookie)
    html = page(server, cookie)
    assert server.checked == ["Codex CLI"]
    assert "codex loginでCodex CLIにログインしてください" in html
    # 判定器を変えたら、その判定器をすぐ確かめる。
    path.write_text(path.read_text() + "review:\n  provider: claude\n")
    page(server, cookie)
    assert server.checked == ["Codex CLI", "Claude Code CLI"]
