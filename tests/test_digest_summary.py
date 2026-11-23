"""留守中のまとめの、判定器による短い文章の要約（#292）を検証する。"""

from datetime import datetime, timedelta

import numpy as np
import pytest

from poop_detector.config import Config
from poop_detector.features.digest import DigestSnapshots
from poop_detector.features.digest_summary import (
    HOURLY_LIMIT,
    MAX_IMAGES,
    SUMMARY_SCHEMA,
    DigestSummaryWorker,
    parse_summary,
    summary_prompt,
)
from poop_detector.features.digest_timeline import TimelineEntry
from poop_detector.records.absences import AwayMode
from poop_detector.records.away_periods import AwayPeriods
from poop_detector.records.digest_summaries import DigestSummaries
from poop_detector.records.events import Store
from poop_detector.video.clips import ClipStore
from poop_detector.video.judge_runs import JudgeRunLog

FRAME = np.full((120, 160, 3), 128, np.uint8)


class Judge:
    name = "偽の判定器"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def check(self):
        pass

    def judge(self, folder, prompt, images, schema):
        for image in images:
            assert (folder / image).is_file()
        self.calls.append((prompt, list(images), schema))
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


def answer(text="おおむね落ち着いて過ごしました。"):
    return {"summary": text, "points": ["14時ごろ鳴いていた"]}


def setup(tmp_path, outputs, *, images=False, clock=None):
    store = Store(tmp_path)
    database = store.database
    judge = Judge(outputs)
    worker = DigestSummaryWorker(
        store,
        ClipStore(tmp_path),
        tmp_path,
        DigestSummaries(database),
        AwayPeriods(database),
        JudgeRunLog(database),
        lambda: (judge, "codex", None),
        tmp_path / "digest-work",
        images=images,
        pet_name="ポチ",
        clock=clock or (lambda: datetime.now().astimezone()),
    )
    return worker, store, judge


def finished_period(store, *, snapshots=0):
    away = AwayMode(store.database)
    away.set_away(True)
    period = AwayPeriods(store.database).current()
    now = datetime.now().astimezone()
    store.add_excretion("feces", now.isoformat())
    frames = DigestSnapshots(store.root)
    for minute in range(snapshots):
        frames.save(period.id, FRAME, now + timedelta(minutes=minute))
    away.set_away(False)
    return period


# --- 作業 ---


def test_worker_summarizes_a_finished_period_with_text_only(tmp_path):
    worker, store, judge = setup(tmp_path, [answer()])
    period = finished_period(store, snapshots=6)
    assert worker.poll_once()
    summary = DigestSummaries(store.database).get(period.id)
    assert summary.status == "done"
    assert summary.summary == "おおむね落ち着いて過ごしました。"
    assert summary.points == ["14時ごろ鳴いていた"]
    prompt, images, schema = judge.calls[0]
    assert images == []
    assert "排便の記録" in prompt
    assert schema == SUMMARY_SCHEMA
    assert not worker.poll_once()


def test_worker_sends_a_few_snapshots_only_when_allowed(tmp_path):
    worker, store, judge = setup(tmp_path, [answer()], images=True)
    finished_period(store, snapshots=20)
    worker.poll_once()
    _, images, _ = judge.calls[0]
    assert len(images) == MAX_IMAGES


def test_worker_ignores_ongoing_and_old_periods(tmp_path):
    later = lambda: datetime.now().astimezone() + timedelta(days=3)  # noqa: E731
    worker, store, judge = setup(tmp_path, [answer()], clock=later)
    finished_period(store)
    AwayMode(store.database).set_away(True)  # 次の外出は続いている
    assert not worker.poll_once()
    assert judge.calls == []


def test_retry_request_and_failure(tmp_path):
    worker, store, _ = setup(tmp_path, [OSError("CLI がない"), {"summary": 1}, answer("2回目")])
    period = finished_period(store)
    summaries = DigestSummaries(store.database)
    worker.poll_once()
    assert summaries.get(period.id).status == "failed"
    assert not worker.poll_once()  # 失敗した要約は、頼まれるまで作り直さない
    summaries.request(period.id)
    worker.poll_once()
    assert summaries.get(period.id).failure == "invalid_output"
    summaries.request(period.id)
    worker.poll_once()
    assert summaries.get(period.id).summary == "2回目"


def test_hourly_limit(tmp_path):
    worker, store, _ = setup(tmp_path, [answer()])
    log = JudgeRunLog(store.database)
    now = datetime.now().astimezone()
    for _ in range(HOURLY_LIMIT):
        log.record("codex", None, "digest", failed=False, at=now - timedelta(minutes=1))
    finished_period(store)
    assert not worker.poll_once()


# --- 指示と解釈 ---


def test_prompt_lists_entries_and_avoids_diagnosis():
    start = datetime(2026, 11, 23, 9, 0).astimezone()
    entries = [TimelineEntry(start + timedelta(hours=1), "record", "排便の記録")]
    prompt = summary_prompt("ポチ", start, start + timedelta(hours=3), entries, 50, 0)
    assert "10:00 排便の記録" in prompt
    assert "3時間" in prompt
    assert "寝ていた時間: 50分" in prompt
    assert "診断" in prompt
    assert "ポチ" in prompt


def test_parse_summary_checks_the_shape():
    assert parse_summary(answer()) == ("おおむね落ち着いて過ごしました。", ["14時ごろ鳴いていた"])
    with pytest.raises(ValueError, match="形"):
        parse_summary({"summary": 1, "points": []})
    with pytest.raises(ValueError, match="形"):
        parse_summary({"summary": "", "points": "x"})
    text, points = parse_summary({"summary": "あ" * 900, "points": ["い" * 200] * 9})
    assert len(text) <= 400
    assert len(points) <= 5
    assert all(len(point) <= 80 for point in points)


# --- 画面と設定 ---


def test_digest_page_shows_summary_and_retry(gui, monkeypatch):
    from support import exchange, login, submit

    from poop_detector.gui import digest_summary_routes

    server, store, _ = gui
    period = finished_period(store)
    cookie = login(server, role="member")
    _, _, body = exchange(server, "GET", f"/digest?id={period.id}", headers=cookie, auth=False)
    assert "判定器による要約" not in body.decode()  # 既定では出さない
    monkeypatch.setattr(digest_summary_routes, "summary_enabled", lambda app: True)
    _, _, body = exchange(server, "GET", f"/digest?id={period.id}", headers=cookie, auth=False)
    assert "要約を作る" in body.decode()
    status, _ = submit(server, "/digest/summary", {"id": period.id}, page=f"/digest?id={period.id}")
    assert status == 303
    summaries = DigestSummaries(store.database)
    assert summaries.get(period.id).status == "queued"
    _, _, body = exchange(server, "GET", f"/digest?id={period.id}", headers=cookie, auth=False)
    assert "要約を作っています" in body.decode()
    summaries.claim(datetime.now().astimezone() - timedelta(days=1))
    summaries.finish(period.id, "静かに過ごしました。", ["夕方に1回排便"], "codex", None)
    _, _, body = exchange(server, "GET", f"/digest?id={period.id}", headers=cookie, auth=False)
    html = body.decode()
    assert "静かに過ごしました。" in html
    assert "夕方に1回排便" in html
    assert "作り直す" in html
    status, _ = submit(server, "/digest/summary", {"id": "missing"}, page=f"/digest?id={period.id}")
    assert status == 404


def test_summary_settings_are_off_by_default():
    config = Config()
    assert (config.digest_summary, config.digest_summary_images) == (False, False)


def test_summary_enabled_reads_the_config_file(tmp_path):
    from types import SimpleNamespace

    from poop_detector.gui.digest_summary_routes import summary_enabled

    assert not summary_enabled(SimpleNamespace(config_path=None))
    path = tmp_path / "config.yaml"
    path.write_text("digest: {summary: true}\n")
    assert summary_enabled(SimpleNamespace(config_path=path))


def test_digest_summary_watch_runs_only_when_enabled(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from poop_detector.commands.context import CommandContext, digest_summary_watch

    judge = Judge([answer()])
    built = []

    def build_judge(provider, model=""):
        built.append(provider)
        return judge

    def watching(check, interval):
        check()
        return nullcontext()

    monkeypatch.setattr("poop_detector.video.providers.build_judge", build_judge)
    monkeypatch.setattr("poop_detector.features.watchdog.watching", watching)
    store = Store(tmp_path)
    period = finished_period(store)
    with digest_summary_watch(CommandContext(Config(data_dir=str(tmp_path)))):
        pass
    assert built == []
    on = CommandContext(Config(data_dir=str(tmp_path), digest_summary=True))
    with digest_summary_watch(on):
        pass
    assert built == ["codex"]
    assert DigestSummaries(store.database).get(period.id).status == "done"
