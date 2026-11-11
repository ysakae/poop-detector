"""動画の自動判定の保存・通知・CLI 連携と、判定器の差し替えを確認する。"""

import json
from datetime import UTC, datetime, timedelta

import cv2
import numpy as np
import pytest

from poop_detector.clip_labels import ClipLabel
from poop_detector.clips import ClipStore
from poop_detector.codex_cli import CodexCliJudge
from poop_detector.main import parser
from poop_detector.roi import ROI
from poop_detector.video_review import Assessment, Review, inspect_clip, parse_assessment

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
    review = Review(store, lambda path, row: FECES, lambda label: notified.append(label) or True)
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


def test_backfill_never_notifies_and_live_skips_feeding(tmp_path):
    store = ClipStore(tmp_path)
    when = datetime.now(UTC)
    old = ready(store, when - timedelta(minutes=10))
    live = ready(store, when)
    review = Review(
        store,
        lambda path, row: FECES if row["id"] == old else FEEDING,
        lambda label: pytest.fail("通知対象外です"),
    )
    review.run(limit=10, since=when.isoformat())
    assert store.assessments.get(old) is None
    assert store.assessments.get(live)["activity"] == "feeding"
    review.run(limit=10)
    assert store.assessments.get(old)["label"] == "feces"
    assert store.get(old)["label"] is None


def test_failed_review_is_visible_and_can_be_retried(tmp_path):
    store = ClipStore(tmp_path)
    clip_id = ready(store, datetime.now(UTC))

    def fail(path, row):
        raise OSError("認証情報を含むかもしれない出力")

    assert Review(store, fail).run(limit=1) == (0, 1)
    assert "認証情報" not in store.assessments.get(clip_id)["error"]
    assert Review(store, lambda path, row: FECES).run(limit=1, clip_id=clip_id) == (1, 0)
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


def test_image_and_schema_reach_codex_cli(tmp_path, monkeypatch):
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
        assert json.loads((folder / "schema.json").read_text())["additionalProperties"] is False
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

    assert inspect_clip(path, row, ROI(0.2, 0.2, 0.5, 0.5), judge=CodexCliJudge(run)) == FEEDING


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
    review = Review(store, lambda path, row: FECES, scene_signature="scene")
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
        lambda path, row: FECES if row["id"] == first else both,
        lambda label: notices.append(label) or True,
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
