"""監視中の判定の状況（回数の上限、後回し、未判定の動画、判定器の失敗）の集計を確認する。"""

from datetime import UTC, datetime, timedelta

from poop_detector.config import Config
from poop_detector.features.outages import OutageLog
from poop_detector.records.database import events_database
from poop_detector.video.clip_reviews import Assessment
from poop_detector.video.clips import ClipStore
from poop_detector.video.review_status import review_status

NOW = datetime(2026, 11, 15, 3, 0, tzinfo=UTC)


def clip(store, source, minutes_ago, *, error=None, judged=False):
    clip_id = store.request()
    # dog_visit は録画の途中で付く種類なので、保存要求のあとで書き換える。
    store.update(clip_id, status="ready", source=source, path=f"clips/{clip_id}.mp4", duration=30)
    when = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    with store.store.connect() as db:
        db.execute("UPDATE clips SET requested_at=? WHERE id=?", (when, clip_id))
    if error:
        store.assessments.save(clip_id, error=error)
    if judged:
        store.assessments.save(clip_id, Assessment("none", "other", None, None, "変化なし"))
    return clip_id


def status(tmp_path, config=None):
    store = ClipStore(tmp_path)
    outages = OutageLog(events_database(tmp_path))
    return review_status(store, outages, config or Config(), NOW)


def test_quiet_monitor_judges_everything(tmp_path):
    found = status(tmp_path)
    assert (found.used, found.max_per_hour, found.postponed) == (0, 20, ())
    assert not found.capped
    assert found.waiting == (("検知・滞在の通知", 0), ("犬の出入り", 0), ("トレー外の滞在", 0))
    assert (found.failed, found.judge_failures, found.outage_since) == (0, 0, None)
    assert found.fallback == ()


def test_calls_in_the_last_hour_decide_which_clips_are_postponed(tmp_path):
    store = ClipStore(tmp_path)
    for minutes in (*range(1, 12), 61, 90):
        store.judge_runs.record(
            "codex", None, "review", failed=False, at=NOW - timedelta(minutes=minutes)
        )
    found = status(tmp_path)
    # 61分より前の呼び出しは数えない。上限20回の半分を超えたので、トレー外の滞在を後回しにする。
    assert (found.used, found.postponed, found.capped) == (11, ("トレー外の滞在",), False)

    for minutes in range(1, 5):
        store.judge_runs.record(
            "claude", None, "benchmark", failed=True, at=NOW - timedelta(minutes=minutes)
        )
    assert status(tmp_path).postponed == ("犬の出入り", "トレー外の滞在")
    assert status(tmp_path, Config(review_max_per_hour=15)).capped
    unlimited = status(tmp_path, Config(review_max_per_hour=0))
    assert (unlimited.used, unlimited.postponed, unlimited.capped) == (15, (), False)


def test_waiting_and_failed_clips_are_counted_for_the_last_day(tmp_path):
    store = ClipStore(tmp_path)
    clip(store, "detected", 5)
    clip(store, "early_alert", 10)
    clip(store, "dog_visit", 20)
    clip(store, "outside_stay", 30)
    clip(store, "outside_stay", 40, judged=True)
    clip(store, "dog_exit", 50, error="自動判定に失敗しました")
    clip(store, "dog_enter", 60 * 25)
    labeled = clip(store, "outside_stay", 15)
    with store.store.connect() as db:
        db.execute("UPDATE clips SET label='none' WHERE id=?", (labeled,))
    found = status(tmp_path)
    assert found.waiting == (("検知・滞在の通知", 2), ("犬の出入り", 1), ("トレー外の滞在", 1))
    assert found.failed == 1


def test_judge_failures_and_outage_come_from_the_ledgers(tmp_path):
    store = ClipStore(tmp_path)
    runs = store.judge_runs
    runs.record("codex", None, "review", failed=True, at=NOW - timedelta(minutes=50))
    runs.record("codex", None, "review", failed=False, at=NOW - timedelta(minutes=40))
    runs.record("codex", None, "review", failed=True, at=NOW - timedelta(minutes=30))
    # 比較（benchmark）の呼び出しは、監視の判定の失敗に数えない。
    runs.record("claude", None, "benchmark", failed=False, at=NOW - timedelta(minutes=25))
    runs.record("codex", None, "review", failed=True, at=NOW - timedelta(minutes=20))
    assert status(tmp_path).judge_failures == 2

    since = (NOW - timedelta(minutes=30)).isoformat()
    OutageLog(events_database(tmp_path)).begin("judge", since)
    found = status(tmp_path, Config(review_provider="codex", review_fallback="claude,gemini"))
    assert found.outage_since == since
    assert found.fallback == ("Claude Code CLI", "Antigravity CLI")
