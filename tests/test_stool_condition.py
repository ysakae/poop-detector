"""動画の自動判定で、トレーの中の便の状態（下痢らしいか）を見分ける（#294）を検証する。"""

from datetime import UTC, datetime, timedelta

import pytest

from poop_detector.gui.clip_pages import clip_assessment_summary
from poop_detector.notifications import ToiletNotifier
from poop_detector.video.clip_reviews import Assessment
from poop_detector.video.clips import ClipStore
from poop_detector.video.review import (
    OUTSIDE_SCHEMA,
    STOOL_CONDITIONS,
    TRAY_SCHEMA,
    parse_assessment,
)


def output(label="feces", condition="diarrhea", **extra):
    values = {
        "label": label,
        "activity": "excreting" if label in ("feces", "urine", "both") else "other",
        "start_seconds": 2.0 if label in ("feces", "urine", "both") else None,
        "end_seconds": 5.0 if label in ("feces", "urine", "both") else None,
        "evidence": {"feces": "new_stool", "urine": "new_wet_patch", "both": "both_visible"}.get(
            label, "none"
        ),
        "reason": "形のない便が広がった",
        "stool_seen_seconds": None,
        "stool_clear_seconds": None,
        "stool_condition": condition,
    }
    values.update(extra)
    return values


# --- スキーマと解釈 ---


def test_tray_schema_asks_for_the_stool_condition_but_outside_does_not():
    assert "stool_condition" in TRAY_SCHEMA["required"]
    assert set(TRAY_SCHEMA["properties"]["stool_condition"]["enum"]) == {*STOOL_CONDITIONS, None}
    assert "stool_condition" not in OUTSIDE_SCHEMA["properties"]


def test_parse_keeps_the_condition_only_for_new_stool():
    assert parse_assessment(output(), 10).stool_condition == "diarrhea"
    assert parse_assessment(output("both", "soft"), 10).stool_condition == "soft"
    # 排便でないのに状態を返したときや、想定外の値は捨てる（判定そのものは保つ）。
    assert parse_assessment(output("urine", "diarrhea"), 10).stool_condition is None
    assert parse_assessment(output(condition="liquid"), 10).stool_condition is None
    assert parse_assessment(output(condition=None), 10).stool_condition is None


def test_older_outputs_without_the_field_still_parse():
    values = output()
    del values["stool_condition"]
    assert parse_assessment(values, 10).stool_condition is None


# --- 台帳 ---


def ready_clip(tmp_path):
    from support import ready_clip as make

    clips = ClipStore(tmp_path)
    return clips, make(clips)


def test_store_saves_and_counts_diarrhea(tmp_path):
    clips, clip_id = ready_clip(tmp_path)
    store = clips.assessments
    result = Assessment("feces", "excreting", 2.0, 5.0, "根拠", "tray", stool_condition="diarrhea")
    assert store.save(clip_id, result)
    assert store.get(clip_id)["stool_condition"] == "diarrhea"
    since = datetime.now(UTC) - timedelta(hours=1)
    assert store.count_condition("diarrhea", since) == 1
    assert store.count_condition("soft", since) == 0
    rows = clips.list(include_assessments=True)
    assert rows[0]["review_stool_condition"] == "diarrhea"


# --- 文面と表示 ---


class Messages(ToiletNotifier):
    def __init__(self):
        super().__init__(True, "ポチ")
        self.messages = []

    def _deliver(self, message, *, early_alert, path=""):
        self.messages.append(message)
        return True


def test_diarrhea_message_and_repeat():
    notifier = Messages()
    notifier.send_assessment("feces", clip_id="a", stool_condition="diarrhea")
    notifier.send_assessment("feces", clip_id="b", stool_condition="diarrhea", stool_repeat=3)
    notifier.send_assessment("feces", clip_id="c")
    first, again, plain = notifier.messages
    assert "下痢らしいうんち" in first
    assert "24時間で3回目" in again
    assert "下痢" not in plain


def test_assessment_summary_shows_the_condition():
    assert clip_assessment_summary("feces", None, False, "diarrhea") == "うんちの候補（下痢らしい）"
    assert clip_assessment_summary("feces", None, False, "normal") == "うんちの候補（ふつう）"
    assert clip_assessment_summary("feces", None, False) == "うんちの候補"


# --- 判定から通知まで ---


def test_review_passes_diarrhea_and_its_count_to_the_notifier(tmp_path, monkeypatch):
    from poop_detector.video import review as review_module

    clips, clip_id = ready_clip(tmp_path)
    sent = []

    def notify(label, **kwargs):
        sent.append((label, kwargs.get("stool_condition"), kwargs.get("stool_repeat")))
        return True

    result = Assessment("feces", "excreting", 2.0, 5.0, "根拠", "tray", stool_condition="diarrhea")
    store = clips.assessments
    store.save(clip_id, result, observed_at=datetime.now(UTC).isoformat())
    worker = review_module.Review.__new__(review_module.Review)
    worker.notify = notify
    worker.store = clips
    row = clips.get(clip_id)
    worker._notify_assessment(
        row, result, "2000-01-01T00:00:00+00:00", datetime.now(UTC).isoformat(), None, None, True
    )
    assert sent == [("feces", "diarrhea", 1)]


@pytest.mark.parametrize("condition", ["normal", None])
def test_review_does_not_pass_the_condition_unless_diarrhea(tmp_path, condition):
    from poop_detector.video import review as review_module

    clips, clip_id = ready_clip(tmp_path)
    sent = []

    def notify(label, **kwargs):
        sent.append(kwargs)
        return True

    result = Assessment("feces", "excreting", 2.0, 5.0, "根拠", "tray", stool_condition=condition)
    clips.assessments.save(clip_id, result, observed_at=datetime.now(UTC).isoformat())
    worker = review_module.Review.__new__(review_module.Review)
    worker.notify = notify
    worker.store = clips
    worker._notify_assessment(
        clips.get(clip_id),
        result,
        "2000-01-01T00:00:00+00:00",
        datetime.now(UTC).isoformat(),
        None,
        None,
        True,
    )
    assert "stool_condition" not in sent[0]


def test_review_queue_shows_the_stool_condition(gui):
    from support import exchange, login
    from support import ready_clip as make

    server, _, _ = gui
    clips = server.app.clips
    clip_id = make(clips)
    clips.assessments.save(
        clip_id,
        Assessment("feces", "excreting", 2.0, 5.0, "根拠", "tray", stool_condition="diarrhea"),
    )
    cookie = login(server, role="member")
    _, _, body = exchange(server, "GET", "/review", headers=cookie, auth=False)
    assert "うんちの候補（下痢らしい）" in body.decode()
