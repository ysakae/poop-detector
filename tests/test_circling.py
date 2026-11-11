"""保存動画の旋回候補と、その確認経路を検証する。"""

import math

import cv2
import numpy as np
from support import request

from poop_detector.circling import CirclingDetector
from poop_detector.clips import ClipStore
from poop_detector.main import main
from poop_detector.roi import ROI
from poop_detector.scene import save_baseline, scene_signature


def circle_points():
    for index in range(13):
        angle = index * math.tau / 12
        yield 50 + 14 * math.cos(angle), 38 + 14 * math.sin(angle)


def test_detects_local_rotation_but_ignores_walk_and_backtracking():
    paths = (
        list(circle_points()),
        [(25 + i * 3, 38) for i in range(13)],
        [(35 + abs(i - 6) * 3, 38) for i in range(13)],
    )
    for path, expected in zip(paths, (True, False, False), strict=True):
        detector = CirclingDetector()
        hits = [detector.update(i / 3, *point, 20) for i, point in enumerate(path)]
        assert bool([hit for hit in hits if hit is not None]) == expected


def test_does_not_join_positions_across_a_tracking_gap():
    detector = CirclingDetector()
    hits = [
        detector.update(index / 3 + (3 if index >= 6 else 0), x, y, 20)
        for index, (x, y) in enumerate(circle_points())
    ]
    assert all(hit is None for hit in hits)


def test_cli_saves_rotation_with_seek_link_without_modifying_label(gui, monkeypatch):
    server, store, _ = gui
    root = store.root
    frame = np.full((76, 100, 3), 150, np.uint8)
    roi = ROI(0, 0, 1, 1)
    roi.save(root / "roi.json")
    save_baseline(root / "baseline/empty.png", frame, roi)
    clips = ClipStore(root)
    clip_id = clips.request()
    relative_path = f"clips/{clip_id}.mp4"
    writer = cv2.VideoWriter(
        str(root / relative_path), cv2.VideoWriter_fourcc(*"mp4v"), 3, (100, 76)
    )
    assert writer.isOpened()
    for _ in circle_points():
        writer.write(frame)
    writer.release()
    clips.update(
        clip_id,
        status="ready",
        path=relative_path,
        duration=13 / 3,
        scene_signature=scene_signature(frame, roi),
    )
    clips.label(clip_id, "none", activity="walking")
    config = root / "config.yaml"
    config.write_text(f"data_dir: {root}")

    class DogDetector:
        def __init__(self, *args):
            self.points = iter(circle_points())

        def predict(self, frame):
            x, y = map(round, next(self.points))
            return [(x - 10, y - 10, x + 10, y + 10)]

    monkeypatch.setattr("poop_detector.dog.DogDetector", DogDetector)
    assert main(["--config", str(config), "circling-review", "--clip", clip_id]) == 0
    row = clips.circling_review(clip_id)
    assert len(row["candidates"]) == 1
    assert clips.get(clip_id)["label"] == "none"
    _, page = request(server, "GET", f"/clips?id={clip_id}")
    assert "旋回候補".encode() in page
    assert b'data-video-seek="0.0"' in page
