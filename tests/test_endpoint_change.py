"""判定器を呼ぶ前のふるい分けに使う、録画の前後の差分の指標を確かめる（判定は変えない）。"""

import json
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import pytest

from poop_detector.detection.roi import ROI
from poop_detector.video.clips import ClipStore
from poop_detector.video.codex_cli import CodexCliJudge
from poop_detector.video.review import Assessment, Review, endpoint_change, inspect_clip

NONE = {
    "label": "none",
    "activity": "other",
    "start_seconds": None,
    "end_seconds": None,
    "evidence": "none",
    "reason": "変化なし",
}


def tiles_with(spots, count=8, size=(60, 96)):
    """各コマに (中心, 半径) の暗い跡を描いた人工のコマを作る。"""
    tiles = []
    for index in range(count):
        tile = np.full((*size, 3), 160, np.uint8)
        for center, radius, frames in spots:
            if index in frames:
                cv2.circle(tile, center, radius, (90, 90, 90), -1)
        tiles.append(tile)
    return tiles


def test_static_scene_has_no_change():
    mask = np.full((60, 96), 255, np.uint8)
    assert endpoint_change(tiles_with([]), mask) == 0.0


def test_mark_left_after_the_dog_counts_as_change():
    mask = np.full((60, 96), 255, np.uint8)
    change = endpoint_change(tiles_with([((48, 30), 10, range(5, 8))]), mask)
    assert change is not None
    assert change > 0.059


def test_passing_shadow_in_only_one_of_the_last_frames_is_ignored():
    # 最後の3コマで最も変化の小さいコマを採るので、1コマだけの影は数えない。
    mask = np.full((60, 96), 255, np.uint8)
    assert endpoint_change(tiles_with([((48, 30), 10, [6])]), mask) == 0.0


def test_too_few_frames_have_no_value():
    mask = np.full((60, 96), 255, np.uint8)
    assert endpoint_change(tiles_with([], count=5), mask) is None


def write_video(path: Path, mark_from: int | None, frames: int = 40) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5, (320, 240))
    assert writer.isOpened()
    for index in range(frames):
        frame = np.full((240, 320, 3), 160, np.uint8)
        if mark_from is not None and index >= mark_from:
            cv2.circle(frame, (160, 120), 50, (80, 80, 80), -1)
        writer.write(frame)
    writer.release()
    return path


def judge_none(_args, **kwargs):
    (kwargs["cwd"] / "result.json").write_text(json.dumps(NONE))


@pytest.mark.parametrize(("mark_from", "changed"), [(None, False), (20, True)])
def test_inspected_clip_carries_the_change_without_altering_the_verdict(
    tmp_path, mark_from, changed
):
    path = write_video(tmp_path / "clip.mp4", mark_from)
    # 録画の長さから9コマを取り出す（最初と最後の3コマが重ならない）。
    result = inspect_clip(path, {"duration": 20}, ROI(0, 0, 1, 1), judge=CodexCliJudge(judge_none))
    # 判定の比較には指標を含めない。
    assert result == Assessment("none", "other", None, None, "変化なし")
    assert result.endpoint_change is not None
    assert (result.endpoint_change > 0.059) is changed


def test_review_stores_the_change_with_the_assessment(tmp_path):
    store = ClipStore(tmp_path)
    clip_id = store.request()
    (store.root / "clips" / f"{clip_id}.mp4").touch()
    store.update(
        clip_id,
        status="ready",
        path=f"clips/{clip_id}.mp4",
        duration=40,
        start_at=datetime.now(UTC).isoformat(),
    )
    measured = Assessment("none", "other", None, None, "変化なし", endpoint_change=0.031)
    assert Review(store, lambda path, row, judge: measured).run(limit=1) == (1, 0)
    assert store.assessments.get(clip_id)["endpoint_change"] == pytest.approx(0.031)
