"""動画の自動判定の保存・通知・CLI 連携と、判定器の差し替えを確認する。"""

import json
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from poop_detector.detection.roi import ROI
from poop_detector.main import parser
from poop_detector.notifications import NtfyNotifier
from poop_detector.video.clip_labels import ClipLabel
from poop_detector.video.clips import ClipStore
from poop_detector.video.codex_cli import CodexCliJudge
from poop_detector.video.judge_source import FixedJudge, JudgeChoice, ReloadingJudge
from poop_detector.video.review import (
    RESUME_WINDOW,
    Assessment,
    Review,
    inspect_clip,
    parse_assessment,
    retry_delay,
)

FECES = Assessment("feces", "excreting", 10, 15, "離れた後に新しい便が見えます")
FEEDING = Assessment("none", "feeding", None, None, "餌皿で食べています")


def ready(store, when, **values):
    clip_id = store.request()
    path = store.root / "clips" / f"{clip_id}.mp4"
    path.touch()
    store.update(
        clip_id,
        status="ready",
        path=f"clips/{path.name}",
        duration=40,
        start_at=when.isoformat(),
        scene_signature="scene",
        **values,
    )
    with store.store.connect() as db:
        db.execute("UPDATE clips SET requested_at=? WHERE id=?", (when.isoformat(), clip_id))
    return clip_id


def test_live_review_preserves_human_labels_and_deduplicates(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    ids = [ready(store, when + timedelta(seconds=offset)) for offset in (0, 2, 20, 40)]
    store.labels.save(ids[0], ClipLabel("none", activity="feeding"))
    notified = []
    review = Review(
        store,
        lambda path, row, judge: FECES,
        lambda label, **kwargs: notified.append(label) or True,
    )
    review.run(limit=10, since=when.isoformat())
    review.run(limit=10, since=when.isoformat())
    assert store.get(ids[0])["label"] == "none"
    assert store.assessments.get(ids[0]) is None
    assert all(store.assessments.get(clip_id)["label"] == "feces" for clip_id in ids[1:])
    assert notified == ["feces", "feces", "feces"]

    duplicate = ready(store, when + timedelta(seconds=42))
    review.run(limit=10, since=when.isoformat())
    assert store.assessments.get(duplicate)["label"] == "feces"
    assert store.assessments.get(duplicate)["notified_at"] is None
    assert len(notified) == 3


def test_live_review_notices_existing_stool_once_until_clear_even_after_restart(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime(2026, 11, 19, 6, 40, tzinfo=UTC)
    first, repeated, cleared, next_stool = [
        ready(store, when + timedelta(minutes=offset)) for offset in range(4)
    ]

    def inspect(path, row, judge):
        observation = (
            {"stool_clear_seconds": 15} if row["id"] == cleared else {"stool_seen_seconds": 0}
        )
        return parse_assessment(
            {
                "label": "none",
                "activity": "other",
                "evidence": "none",
                "reason": "トレーの状態を確認しました",
                **observation,
            },
            40,
        )

    messages = []

    def opener(request, *, timeout):
        messages.append(json.loads(request.data))
        return nullcontext(SimpleNamespace(status=200))

    notifier = NtfyNotifier("family", opener=opener, public_url="https://toilet.example.com")
    for clip_id in (first, repeated, cleared, next_stool):
        review = Review(store, inspect, notifier.send_assessment)
        assert review.run(limit=1, since=when.isoformat(), clip_id=clip_id) == (1, 0)

    assert [message["click"] for message in messages] == [
        f"https://toilet.example.com/clips?id={first}&notice=1",
        f"https://toilet.example.com/clips?id={next_stool}&notice=1",
    ]
    assert "排便時刻は不明" in messages[0]["message"]
    assert "便を確認" in messages[0]["message"]
    assert when.astimezone().strftime("%m/%d %H:%M:%S") in messages[0]["message"]


def test_existing_stool_does_not_repeat_a_recent_regular_feces_notice(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    first = ready(store, when)
    second = ready(store, when + timedelta(minutes=2))

    def inspect(path, row, judge):
        if row["id"] == first:
            return FECES
        return parse_assessment(
            {
                "label": "none",
                "activity": "other",
                "evidence": "none",
                "reason": "冒頭から便が見えます",
                "stool_seen_seconds": 0,
            },
            40,
        )

    notifications = []
    review = Review(store, inspect, lambda label, **kwargs: notifications.append(label) or True)
    assert review.run(limit=2, since=when.isoformat()) == (2, 0)
    assert notifications == ["feces"]
    assert (
        store.assessments.get(second)["stool_seen_at"] == (when + timedelta(minutes=2)).isoformat()
    )


def test_late_review_of_the_same_feces_keeps_the_urine_notice(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    older = ready(store, when)
    newer = ready(store, when + timedelta(minutes=1))
    results = {
        newer: Assessment("none", "other", stool_seen_seconds=0),
        older: Assessment("both", "excreting", 10, 15, location="tray"),
    }
    notices = []
    review = Review(
        store,
        lambda path, row, judge: results[row["id"]],
        lambda label, **kwargs: notices.append((label, kwargs["clip_id"])) or True,
    )
    for clip_id in (newer, older):
        assert review.run(limit=1, since=when.isoformat(), clip_id=clip_id) == (1, 0)
    assert notices == [("feces", newer), ("urine", older)]
    assert store.assessments.get(older)["notice_label"] == "urine"


def test_failed_existing_stool_notice_can_be_sent_on_a_later_clip(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    first = ready(store, when)
    second = ready(store, when + timedelta(minutes=1))
    notices = []

    def notify(label, **kwargs):
        notices.append(kwargs["clip_id"])
        return len(notices) > 1

    review = Review(store, lambda path, row, judge: Assessment(stool_seen_seconds=0), notify)
    assert review.run(limit=2, since=when.isoformat()) == (2, 0)
    assert notices == [first, second]
    assert store.assessments.get(first)["stool_notified_at"] is None
    assert store.assessments.get(second)["stool_notified_at"]


def test_clear_at_the_same_instant_does_not_split_one_stool_observation(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    first, clear, repeated = [ready(store, when) for _ in range(3)]
    notices = []
    review = Review(
        store,
        lambda path, row, judge: (
            Assessment(stool_clear_seconds=0)
            if row["id"] == clear
            else Assessment(stool_seen_seconds=0)
        ),
        lambda label, **kwargs: notices.append(kwargs["clip_id"]) or True,
    )
    for clip in (first, clear, repeated):
        assert review.run(limit=1, since=when.isoformat(), clip_id=clip) == (1, 0)
    assert notices == [first]


def test_invalid_optional_stool_time_keeps_a_regular_urine_assessment():
    assessment = parse_assessment(
        {
            "label": "urine",
            "activity": "excreting",
            "evidence": "new_wet_patch",
            "start_seconds": 4,
            "end_seconds": 5,
            "reason": "濡れ跡が現れました",
            "stool_seen_seconds": -1,
        },
        10,
    )
    assert assessment.label == "urine"
    assert assessment.stool_seen_seconds is None


@pytest.mark.parametrize(
    ("earlier_result", "existing"),
    [(Assessment("none", stool_seen_seconds=0), True), (FECES, False)],
)
def test_late_clear_releases_the_other_stool_notice_without_rejudging(
    tmp_path, earlier_result, existing
):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    earlier, cleared, later = [
        ready(store, when + timedelta(minutes=offset)) for offset in range(3)
    ]
    notices = []
    results = {
        earlier: earlier_result,
        cleared: Assessment("none", stool_clear_seconds=10),
        later: Assessment("none", stool_seen_seconds=0),
    }
    review = Review(
        store,
        lambda path, row, judge: results[row["id"]],
        lambda label, **kwargs: (
            notices.append((kwargs["clip_id"], kwargs["existing_stool"])) or True
        ),
    )
    for clip in (later, earlier, cleared):
        assert review.run(limit=1, since=when.isoformat(), clip_id=clip) == (1, 0)
    assert notices == [(later, True), (earlier, existing)]


def test_backfill_never_notifies_and_live_skips_feeding(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    old = ready(store, when - timedelta(minutes=10))
    live = ready(store, when)
    review = Review(
        store,
        lambda path, row, judge: FECES if row["id"] == old else FEEDING,
        lambda label: pytest.fail("通知対象外です"),
    )
    review.run(limit=10, since=when.isoformat())
    assert store.assessments.get(old) is None
    assert store.assessments.get(live)["activity"] == "feeding"
    review.run(limit=10)
    assert store.assessments.get(old)["label"] == "feces"
    assert store.get(old)["label"] is None


@pytest.mark.parametrize(
    ("label", "interrupted", "location", "expected"),
    [
        ("feces", True, None, "排便の候補"),
        ("urine", True, "outside", "トレー外での排尿の候補"),
        ("both", True, None, "排便・排尿の候補"),
        ("urine", False, None, "排尿の候補"),
        ("none", True, None, None),
        ("uncertain", True, None, None),
    ],
)
def test_live_review_notifies_of_excretion_with_clip_quality(
    tmp_path, label, interrupted, location, expected
):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    clip_id = ready(store, when, interrupted=int(interrupted))
    sent = []

    def open_request(request, *, timeout):
        sent.append(json.loads(request.data))
        return nullcontext(SimpleNamespace(status=200))

    notifier = NtfyNotifier(
        "family", early_topic="watch", opener=open_request, public_url="https://toilet.example.com"
    )
    result = (
        Assessment(label, "excreting", 10, 15, "新しい排泄の跡", location)
        if expected
        else Assessment(label, "other", None, None, "新しい排泄の跡は確認できません")
    )
    review = Review(store, lambda path, row, judge: result, notifier.send_assessment)
    assert review.run(limit=10, since=when.isoformat()) == (1, 0)
    assert review.run(limit=10, since=when.isoformat()) == (0, 0)
    if expected is None:
        assert sent == []
        assert store.assessments.get(clip_id)["notified_at"] is None
    else:
        assert len(sent) == 1
        assert sent[0]["topic"] == "family"
        assert sent[0]["click"] == f"https://toilet.example.com/clips?id={clip_id}&notice=1"
        assert expected in sent[0]["message"]
        assert (when + timedelta(seconds=15)).astimezone().strftime("%m/%d %H:%M:%S") in sent[0][
            "message"
        ]
        assert ("一部欠落" in sent[0]["message"]) == interrupted
        assert store.assessments.get(clip_id)["notified_at"] is not None


def test_failed_review_is_visible_and_can_be_retried(tmp_path):
    store = ClipStore(tmp_path)
    clip_id = ready(store, datetime.now(UTC))

    def fail(path, row, judge):
        raise OSError("認証情報を含むかもしれない出力")

    assert Review(store, fail).run(limit=1) == (0, 1)
    assert "認証情報" not in store.assessments.get(clip_id)["error"]
    assert Review(store, lambda path, row, judge: FECES).run(limit=1, clip_id=clip_id) == (1, 0)
    assert store.assessments.get(clip_id)["error"] is None


def test_rejects_positive_with_invalid_time_or_missing_evidence():
    output = {
        "label": "urine",
        "activity": "excreting",
        "start_seconds": 1,
        "end_seconds": 9,
        "evidence": "new_wet_patch",
        "reason": "新しい尿跡",
    }
    assert parse_assessment(output, 10).label == "urine"
    for overrides, message in [
        ({"end_seconds": 12}, "動画範囲外"),
        ({"evidence": "none"}, "根拠がありません"),
        ({"start_seconds": True}, "動画範囲外"),
    ]:
        with pytest.raises(ValueError, match=message):
            parse_assessment(output | overrides, 10)


@pytest.mark.parametrize(("seen", "expected"), [(0, 0), (0.3, None)])
def test_image_and_schema_reach_codex_cli(tmp_path, monkeypatch, seen, expected):
    store = ClipStore(tmp_path)
    clip_id = ready(store, datetime.now(UTC))
    row = store.get(clip_id)
    path = store.root / row["path"]
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5, (96, 64))
    assert writer.isOpened()
    for _ in range(10):
        writer.write(np.full((64, 96, 3), 200, dtype=np.uint8))
    writer.release()
    row["duration"] = 2
    monkeypatch.setenv("POOP_RTSP_URL", "private")

    def run(args, **kwargs):
        folder = kwargs["cwd"]
        assert (folder / "frames.jpg").is_file()
        assert "POOP_RTSP_URL" not in kwargs["env"]
        assert args[args.index("--sandbox") + 1] == "read-only"
        schema = json.loads((folder / "schema.json").read_text())
        assert schema["additionalProperties"] is False
        assert {"stool_seen_seconds", "stool_clear_seconds"} <= set(schema["required"])
        (folder / "result.json").write_text(
            json.dumps(
                {
                    "label": "none",
                    "activity": "feeding",
                    "start_seconds": None,
                    "end_seconds": None,
                    "evidence": "none",
                    "reason": FEEDING.reason,
                    "stool_seen_seconds": seen,
                    "stool_clear_seconds": None,
                }
            )
        )

    assessment = inspect_clip(path, row, ROI(0.2, 0.2, 0.5, 0.5), judge=CodexCliJudge(run))
    assert assessment.label == "none"
    assert assessment.activity == "feeding"
    assert assessment.stool_seen_seconds == expected


def test_tracked_mesh_video_supplies_difference_without_amplifying_existing_marks(tmp_path):
    path = tmp_path / "mesh.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5, (320, 240))
    assert writer.isOpened()
    y, x = np.indices((240, 320))
    mesh = np.uint8(140 + ((x % 6 < 2) | (y % 6 < 2)) * 45)
    cv2.circle(mesh, (70, 100), 13, 115, -1)
    offset = np.full_like(mesh, 10)
    cv2.circle(offset, (210, 140), 15, 2, -1)
    for index in range(50):
        gray = mesh if index < 28 else mesh + offset
        writer.write(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))
    writer.release()

    class Tracker:
        def align(self, frame):
            return frame

    def run(args, **kwargs):
        folder = kwargs["cwd"]
        assert [args[index + 1] for index, arg in enumerate(args) if arg == "--image"] == [
            "frames.jpg",
            "differences.jpg",
        ]
        sheet = cv2.imread(str(folder / "differences.jpg"), cv2.IMREAD_GRAYSCALE)
        assert sheet is not None
        first, last_in_top_row = np.hsplit(sheet, 4)[::3]
        assert abs(int(first[201, 263]) - 128) < 10
        assert abs(int(last_in_top_row[151, 88]) - 128) < 18
        assert last_in_top_row[201, 263] < 100
        (folder / "result.json").write_text(
            json.dumps(
                {
                    "label": "none",
                    "activity": "feeding",
                    "start_seconds": None,
                    "end_seconds": None,
                    "evidence": "none",
                    "reason": FEEDING.reason,
                }
            )
        )

    assert (
        inspect_clip(
            path,
            {"duration": 10},
            ROI(0, 0, 1, 1),
            judge=CodexCliJudge(run),
            tray_tracker=Tracker(),
        )
        == FEEDING
    )


def test_cli_exposes_live_and_backfill_modes():
    assert parser().parse_args(["run", "--dog", "--codex"]).video_review
    assert parser().parse_args(["codex-review", "--limit", "3"]).limit == 3


def test_backfill_uses_matching_camera_scene(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    current = ready(store, when)
    old = ready(store, when - timedelta(minutes=1))
    store.update(old, scene_signature="old")
    review = Review(store, lambda path, row, judge: FECES, scene_signature="scene")
    assert review.run(limit=3) == (1, 0)
    assert store.assessments.get(current)["label"] == "feces"
    assert store.assessments.get(old) is None


def test_duplicate_both_is_not_a_second_notice(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    first = ready(store, when)
    duplicate = ready(store, when + timedelta(seconds=2))
    both = Assessment("both", "excreting", 10, 15, "便と濡れ跡が見えます")
    notices = []
    review = Review(
        store,
        lambda path, row, judge: FECES if row["id"] == first else both,
        lambda label, **kwargs: notices.append(label) or True,
    )
    assert review.run(limit=3, since=when.isoformat()) == (2, 0)
    assert notices == ["feces"]
    assert store.assessments.get(duplicate)["label"] == "both"


def test_single_frame_does_not_reach_codex(tmp_path):
    path = tmp_path / "one.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5, (96, 64))
    assert writer.isOpened()
    writer.write(np.zeros((64, 96, 3), dtype=np.uint8))
    writer.release()
    with pytest.raises(ValueError, match="フレームを取得できません"):
        inspect_clip(
            path,
            {"duration": 0.2},
            ROI(0, 0, 1, 1),
            judge=CodexCliJudge(
                lambda *args, **kwargs: pytest.fail("1枚しかない動画は判定できません")
            ),
        )


def write_clip(store):
    clip_id = ready(store, datetime.now(UTC))
    row = store.get(clip_id)
    path = store.root / row["path"]
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5, (96, 64))
    assert writer.isOpened()
    for _ in range(10):
        writer.write(np.full((64, 96, 3), 200, dtype=np.uint8))
    writer.release()
    row["duration"] = 2
    return path, row


def test_any_judge_can_replace_codex_cli(tmp_path):
    """VideoJudge を満たす判定器なら、Codex CLI の代わりに使える。"""

    class FixedJudge:
        name = "固定の判定器"

        def __init__(self):
            self.calls = []

        def check(self):
            pass

        def judge(self, folder, prompt, images, schema):
            self.calls.append((sorted(images), "label" in schema["properties"]))
            assert all((folder / image).is_file() for image in images)
            assert "新たに発生した排泄" in prompt
            return {
                "label": "none",
                "activity": "feeding",
                "start_seconds": None,
                "end_seconds": None,
                "evidence": "none",
                "reason": FEEDING.reason,
            }

    store = ClipStore(tmp_path)
    path, row = write_clip(store)
    judge = FixedJudge()
    assert inspect_clip(path, row, ROI(0.2, 0.2, 0.5, 0.5), judge) == FEEDING
    assert judge.calls == [(["frames.jpg"], True)]


def test_new_and_old_names_for_video_review():
    """製品名を外した新しい名前と、以前の名前（--codex、codex-review）の両方が使える。"""
    assert parser().parse_args(["run", "--video-review"]).video_review
    assert parser().parse_args(["run", "--codex"]).video_review
    assert parser().parse_args(["video-review", "--limit", "2"]).limit == 2
    old = parser().parse_args(["codex-review", "--limit", "2"])
    new = parser().parse_args(["video-review", "--limit", "2"])
    assert old.handler is new.handler


def test_old_feature_name_in_config_still_works(tmp_path, caplog):
    from poop_detector.config import Config

    path = tmp_path / "config.yaml"
    path.write_text("features: {codex: true}\n")
    with caplog.at_level("WARNING", logger="poop_detector.config"):
        assert Config.load(path).video_review_enabled is True
    assert "features.codex → features.video_review" in caplog.text


def test_review_records_the_selected_provider_and_model(tmp_path):
    store = ClipStore(tmp_path)
    clip_id = ready(store, datetime.now(UTC))
    choice = JudgeChoice(FakeJudge("claude"), "claude", "configured-model")
    review = Review(store, lambda path, row, judge: FECES, judges=FixedJudge(choice))
    assert review.run(limit=1, clip_id=clip_id) == (1, 0)
    row = store.assessments.get(clip_id)
    assert (row["provider"], row["model"]) == ("claude", "configured-model")


class FakeJudge:
    """名前だけを持つ偽の判定器。check の失敗を指定できる。"""

    def __init__(self, label, *, failure=None):
        self.name = label
        self.failure = failure

    def check(self):
        if self.failure is not None:
            raise ValueError(self.failure)

    def judge(self, folder, prompt, images, schema):
        raise AssertionError("inspect で判定するので呼ばれません")


def write_review_config(path, provider, model, stamp):
    import os

    path.write_text(f"review:\n  provider: {provider}\n  model: '{model}'\n")
    # 同じ時刻に書いても更新を見分けられるよう、更新時刻を明示する。
    os.utime(path, ns=(stamp, stamp))


def test_review_uses_the_judge_rewritten_in_the_config(tmp_path):
    config = tmp_path / "config.yaml"
    write_review_config(config, "codex", "", 1_000_000_000)
    first = FakeJudge("first")
    built = []

    def build(provider, model):
        judge = FakeJudge(f"{provider}:{model}")
        built.append(judge)
        return judge

    judges = ReloadingJudge(config, JudgeChoice(first, "codex", None), build=build)
    store = ClipStore(tmp_path)
    used = []
    review = Review(store, lambda path, row, judge: used.append(judge.name) or FECES, judges=judges)
    before = ready(store, datetime.now(UTC))
    assert review.run(limit=1, clip_id=before) == (1, 0)

    write_review_config(config, "claude", "new-model", 2_000_000_000)
    after = ready(store, datetime.now(UTC))
    assert review.run(limit=1, clip_id=after) == (1, 0)
    # 更新がなければ作り直さない。
    assert judges.current().judge is built[0]

    assert used == ["first", "claude:new-model"]
    assert len(built) == 1
    row = store.assessments.get(before)
    assert (row["provider"], row["model"]) == ("codex", None)
    row = store.assessments.get(after)
    assert (row["provider"], row["model"]) == ("claude", "new-model")


def test_review_keeps_the_previous_judge_when_the_new_one_is_unusable(tmp_path, caplog):
    config = tmp_path / "config.yaml"
    write_review_config(config, "codex", "", 1_000_000_000)
    first = FakeJudge("first")
    attempts = []

    def build(provider, model):
        attempts.append(provider)
        return FakeJudge(provider, failure="claude auth login でログインしてください")

    judges = ReloadingJudge(config, JudgeChoice(first, "codex", None), build=build)
    write_review_config(config, "claude", "", 2_000_000_000)
    with caplog.at_level("WARNING", logger="poop_detector.video.judge_source"):
        choice = judges.current()
    assert (choice.judge, choice.provider, choice.model) == (first, "codex", None)
    assert "前の判定器" in caplog.text
    # 失敗した書き換えは、次に設定ファイルが変わるまで試し直さない。
    assert judges.current().judge is first
    assert attempts == ["claude"]


def test_reloading_judge_keeps_the_judge_when_the_config_is_unreadable(tmp_path, caplog):
    config = tmp_path / "config.yaml"
    write_review_config(config, "codex", "", 1_000_000_000)
    first = FakeJudge("first")
    judges = ReloadingJudge(
        config, JudgeChoice(first, "codex", None), build=lambda *_: pytest.fail("作りません")
    )
    config.write_text("review: [\n")
    with caplog.at_level("WARNING", logger="poop_detector.video.judge_source"):
        assert judges.current().judge is first
    config.unlink()
    assert judges.current().judge is first


def test_monitor_starts_from_the_judge_checked_at_startup(tmp_path):
    from poop_detector.commands.run import judge_source
    from poop_detector.config import Config

    first = FakeJudge("first")
    config = Config(review_provider="claude", review_model="")
    choice = judge_source(str(tmp_path / "missing.yaml"), config, first).current()
    assert (choice.judge, choice.provider, choice.model) == (first, "claude", None)


class FlakyJudge:
    """down の間は呼び出しに失敗する判定器。"""

    name = "偽の判定器"

    def __init__(self):
        self.down = True
        self.calls = 0

    def check(self):
        pass

    def judge(self, folder, prompt, images, schema):
        self.calls += 1
        if self.down:
            raise subprocess.CalledProcessError(1, "judge")
        return {}


def asking(result):
    """判定器を1回呼んでから result を返す、inspect_clip の代わり。"""

    def inspect(path, row, judge):
        judge.judge(path.parent, "", [], {})
        return result

    return inspect


def flaky_review(store, judge, notify=None):
    return Review(
        store, asking(FECES), notify, judges=FixedJudge(JudgeChoice(judge, "codex", None))
    )


def test_live_review_retries_clips_once_the_judge_recovers(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    clip_id = ready(store, when)
    judge = FlakyJudge()
    notified = []
    review = flaky_review(store, judge, lambda label, **kwargs: notified.append(label) or True)

    assert review.run(limit=1, since=when.isoformat()) == (0, 1)
    assert review.run(limit=1, since=when.isoformat()) == (0, 1)
    assert review.judge_failures == 2
    assert store.assessments.get(clip_id)["error"]

    judge.down = False
    assert review.run(limit=1, since=when.isoformat()) == (1, 0)
    assert review.judge_failures == 0
    assert store.assessments.get(clip_id)["label"] == "feces"
    assert notified == ["feces"]
    assert review.run(limit=1, since=when.isoformat()) == (0, 0)


def test_live_review_does_not_retry_failures_unrelated_to_the_judge(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    ready(store, when)

    def unreadable(path, row, judge):
        raise ValueError("動画からフレームを取得できません")

    review = Review(store, unreadable)
    assert review.run(limit=1, since=when.isoformat()) == (0, 1)
    assert review.judge_failures == 0
    assert review.run(limit=1, since=when.isoformat()) == (0, 0)


def test_live_review_gives_up_on_a_clip_the_judge_keeps_failing(tmp_path, monkeypatch):
    monkeypatch.setattr("poop_detector.video.review.MAX_RETRIES", 2)
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    ready(store, when)
    judge = FlakyJudge()
    review = flaky_review(store, judge)
    for _ in range(3):
        assert review.run(limit=1, since=when.isoformat()) == (0, 1)
    assert review.run(limit=1, since=when.isoformat()) == (0, 0)
    assert judge.calls == 3


def test_live_review_judges_detections_before_outside_stays(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    outside = ready(store, when, source="outside_stay")
    visit = ready(store, when + timedelta(seconds=1), source="dog_visit")
    detected = ready(store, when + timedelta(seconds=2), source="detected")
    order = []

    def inspect(path, row, judge):
        order.append(row["id"])
        return FEEDING

    assert Review(store, inspect).run(limit=3, since=when.isoformat()) == (3, 0)
    assert order == [detected, visit, outside]


def test_retry_delay_grows_while_the_judge_keeps_failing():
    assert [retry_delay(count) for count in (0, 1, 2, 3, 4)] == [20, 20, 40, 80, 160]
    assert retry_delay(8) == retry_delay(50) == 1800


def test_worker_waits_longer_only_for_judge_failures(tmp_path):
    review = Review(ClipStore(tmp_path), lambda path, row, judge: FECES)
    worker = review.watch()
    assert worker.next_wait(1, 0) == 0
    assert worker.next_wait(0, 0) == 3
    assert worker.next_wait(0, 1) == 20
    review.judge_failures = 4
    assert worker.next_wait(0, 1) == 160


def test_worker_reports_judge_failures_to_the_watchdog(tmp_path):
    store = ClipStore(tmp_path)
    judge = FlakyJudge()
    review = flaky_review(store, judge)
    seen = []
    worker = review.watch(SimpleNamespace(observe=seen.append))
    ready(store, datetime.now(UTC))
    worker.poll_once()
    worker.poll_once()
    judge.down = False
    worker.poll_once()
    assert seen == [1, 2, 0]


def test_worker_picks_up_clips_left_unjudged_before_a_restart(tmp_path):
    # 監視を起動し直しても、前の監視が判定しきれなかった動画を取り残さない（直近1日まで）。
    store = ClipStore(tmp_path)
    now = datetime.now(UTC)
    left = ready(store, now - timedelta(hours=3))
    ready(store, now - RESUME_WINDOW - timedelta(minutes=1))
    seen = []

    def inspect(path, row, judge):
        seen.append(row["id"])
        return FEEDING

    worker = Review(store, inspect).watch()
    worker.poll_once()
    worker.poll_once()
    assert seen == [left]


def sourced(store, when, *sources):
    return [
        ready(store, when + timedelta(seconds=offset), source=source)
        for offset, source in enumerate(sources)
    ]


def judged(store):
    with store.store.connect() as db:
        return {row["clip_id"] for row in db.execute("SELECT clip_id FROM video_assessments")}


def test_hourly_cap_stops_live_review_until_the_window_moves(tmp_path, caplog):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    ids = sourced(store, when, "detected", "detected", "detected")
    clock = [when + timedelta(minutes=1)]
    judge = FlakyJudge()
    judge.down = False
    review = Review(
        store,
        asking(FEEDING),
        judges=FixedJudge(JudgeChoice(judge, "codex", None)),
        max_per_hour=2,
        now=lambda: clock[0],
    )

    assert review.run(limit=10, since=when.isoformat()) == (2, 0)
    with caplog.at_level("WARNING"):
        assert review.run(limit=10, since=when.isoformat()) == (0, 0)
        assert review.run(limit=10, since=when.isoformat()) == (0, 0)
    # 上限に達したことは、状態が変わったときに一度だけ知らせる。
    warnings = [record.message for record in caplog.records if record.levelname == "WARNING"]
    assert warnings == [
        "判定器の呼び出しが1時間あたりの上限（2回）に達しました。枠が空くまで判定を待ちます"
    ]
    assert judged(store) == set(ids[:2])
    clock[0] += timedelta(minutes=61)
    assert review.run(limit=10, since=when.isoformat()) == (1, 0)


def test_hourly_cap_keeps_room_for_detections_by_postponing_low_priority_clips(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    outside, visit, detected = sourced(store, when, "outside_stay", "dog_visit", "detected")
    clock = [when + timedelta(minutes=1)]
    judge = FlakyJudge()
    judge.down = False
    review = Review(
        store,
        asking(FEEDING),
        judges=FixedJudge(JudgeChoice(judge, "codex", None)),
        max_per_hour=20,
        now=lambda: clock[0],
    )
    # 上限の半分（10回）を使うと、トレー外の滞在を後回しにする。
    for _ in range(10):
        store.judge_runs.record("codex", None, "review", failed=False, at=clock[0])
    assert review.run(limit=10, since=when.isoformat()) == (2, 0)
    assert judged(store) == {visit, detected}
    # 4分の3（15回）を使うと、犬の出入りも後回しにして、検知と滞在の通知だけを判定する。
    for _ in range(3):
        store.judge_runs.record("codex", None, "review", failed=False, at=clock[0])
    later_visit, later_detected = sourced(
        store, when + timedelta(seconds=10), "dog_exit", "early_alert"
    )
    assert review.run(limit=10, since=when.isoformat()) == (1, 0)
    assert judged(store) == {visit, detected, later_detected}
    # 1時間が過ぎると、後回しにした動画を判定する。
    clock[0] += timedelta(minutes=61)
    assert review.run(limit=10, since=when.isoformat()) == (2, 0)
    assert judged(store) == {visit, detected, later_detected, later_visit, outside}


def test_manual_review_and_a_zero_cap_are_not_limited(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    sourced(store, when, "outside_stay", "outside_stay", "outside_stay")
    judge = FlakyJudge()
    judge.down = False
    judges = FixedJudge(JudgeChoice(judge, "codex", None))
    assert Review(store, asking(FEEDING), judges=judges, max_per_hour=1).run(limit=2) == (2, 0)
    unlimited = Review(store, asking(FEEDING), judges=judges, max_per_hour=0)
    assert unlimited.run(limit=10, since=when.isoformat()) == (1, 0)


def test_saved_mixed_assessment_defers_stool_but_notifies_urine(tmp_path):
    store = ClipStore(tmp_path)
    start = datetime.now(UTC)
    earlier = ready(store, start)
    current = ready(store, start + timedelta(seconds=2))
    result = Assessment("both", "excreting", 10, 15)
    seen = (start + timedelta(seconds=17)).isoformat()
    store.assessments.save(earlier, FECES, observed_at=seen, stool_seen_at=seen)
    assert store.assessments.claim_stool_notice(earlier)
    store.assessments.finish_stool_notice(earlier, True)
    store.assessments.save(current, result, observed_at=seen, stool_seen_at=seen)
    notices = []
    review = Review(
        store,
        lambda *args: pytest.fail("動画を再判定しました"),
        lambda label, **kwargs: notices.append((label, kwargs["clip_id"])) or True,
    )

    review._notify_assessment(store.get(current), result, start.isoformat(), seen, seen, None, True)

    assert notices == [("urine", current)]
    assert store.assessments.get(current)["stool_notified_at"] is None
