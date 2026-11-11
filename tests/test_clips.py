"""収集した動画の範囲・ラベル・実エンコードを確認する。"""

import shutil

import cv2
import numpy as np
import pytest

from poop_detector.clip_labels import ClipLabel


def test_buffer_and_manual_capture(tmp_path):
    from poop_detector.clips import ClipStore
    from poop_detector.recording import ClipCollector

    outputs = []

    def encode(samples, path, fps):
        outputs.append([s[0] for s in samples])
        path.write_bytes(b"mp4")
        return len(samples) / fps

    with ClipCollector(tmp_path, before=2, after=2, fps=1, encoder=encode) as collector:
        for tick in range(5):
            collector.feed(np.zeros((100, 100, 3), np.uint8), now=tick)
        clip = ClipStore(tmp_path).request("manual")
        collector.poll(now=4)
        collector.feed(np.zeros((100, 100, 3), np.uint8), now=5)
        collector.feed(np.zeros((100, 100, 3), np.uint8), now=6)
        collector.poll(now=6)
        collector.drain()
        row = ClipStore(tmp_path).get(clip)
        assert row["status"] == "ready"
        assert outputs == [[2, 3, 4, 5, 6]]
        assert row["trigger_offset"] == 2
        assert row["pre_available"] == 2
        assert row["post_available"] == 2


def test_disconnect_saves_partial_and_does_not_mix_generations(tmp_path):
    from poop_detector.clips import ClipStore
    from poop_detector.recording import ClipCollector

    def encode(samples, path, fps):
        path.write_bytes(b"mp4")
        return len(samples) / fps

    with ClipCollector(tmp_path, before=2, after=3, fps=1, encoder=encode) as collector:
        collector.feed(np.zeros((100, 100, 3), np.uint8), now=0, generation=1)
        clip = ClipStore(tmp_path).request("detected")
        collector.poll(now=0)
        collector.feed(None, now=1, generation=1)
        collector.drain()
        assert ClipStore(tmp_path).get(clip)["interrupted"] == 1
        collector.feed(np.zeros((100, 100, 3), np.uint8), now=2, generation=2)
        assert len(collector.buffer) == 1


def test_labels_and_video_time_bounds(tmp_path):
    from poop_detector.clips import ClipStore

    store = ClipStore(tmp_path)
    clip = store.request("manual")
    store.update(clip, status="ready", duration=10)
    store.labels.save(clip, ClipLabel("urine", 2, 5, "other", "目視確認"))
    assert store.get(clip)["label"] == "urine"
    for start, end, message in [
        (5, 2, "完了は開始以降"),
        (-1, 2, "動画内の秒数"),
        (2, 11, "動画内の秒数"),
    ]:
        with pytest.raises(ValueError, match=message):
            store.labels.save(clip, ClipLabel("feces", start, end))
    store.labels.save(clip, ClipLabel("none", activity="feeding"))
    assert store.get(clip)["activity"] == "feeding"
    assert store.get(clip)["actual_start_offset"] is None


def test_no_camera_request_is_rejected(tmp_path):
    from poop_detector.clips import ClipStore

    with pytest.raises(ValueError, match="カメラ接続を確認"):
        ClipStore(tmp_path).request("manual", require_live=True)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg未導入")
def test_real_browser_mp4_encoder(tmp_path):
    from poop_detector.recording import encode_mp4

    samples = []
    for index in range(5):
        frame = np.full((100, 160, 3), index * 30, np.uint8)
        ok, image = cv2.imencode(".jpg", frame)
        assert ok
        samples.append((index / 5, "2026-11-09T00:00:00+00:00", image.tobytes()))
    output = tmp_path / "test.mp4"
    duration = encode_mp4(samples, output, 5)
    capture = cv2.VideoCapture(str(output))
    try:
        assert capture.isOpened()
        assert capture.read()[0]
        assert duration == 1
    finally:
        capture.release()


def test_memory_limit_and_disk_limit_preserve_existing_files(tmp_path):
    from poop_detector.clips import ClipStore
    from poop_detector.recording import ClipCollector

    def encode(samples, path, fps):
        path.write_bytes(b"new-video")
        return len(samples) / fps

    with ClipCollector(
        tmp_path, before=2, after=0, fps=1, disk_mb=0.000001, encoder=encode
    ) as collector:
        original = tmp_path / "clips" / "existing.mp4"
        original.write_bytes(b"keep-this-video")
        collector.feed(np.zeros((100, 100, 3), np.uint8), now=0)
        clip = ClipStore(tmp_path).request()
        collector.poll(now=0)
        collector.drain()
        assert ClipStore(tmp_path).get(clip)["status"] == "failed"
        assert original.read_bytes() == b"keep-this-video"
    other = tmp_path / "limited"
    with ClipCollector(other, memory_mb=0.000001, encoder=encode) as collector:
        collector.feed(np.zeros((100, 100, 3), np.uint8), now=0)
        assert collector._bytes() <= collector.memory_limit


def test_second_collector_is_rejected_and_stale_requests_fail(tmp_path):
    from poop_detector.clips import ClipStore
    from poop_detector.recording import ClipCollector

    store = ClipStore(tmp_path)
    stale = store.request()
    with ClipCollector(tmp_path):
        with pytest.raises(ValueError, match="別の動画監視が起動中"), ClipCollector(tmp_path):
            pass
        assert store.get(stale)["status"] == "failed"


def test_encoder_uses_disk_spool_instead_of_payload_copy(tmp_path, monkeypatch):
    import subprocess

    from poop_detector.recording import encode_mp4

    seen = []

    def run(command, **kwargs):
        assert "input" not in kwargs
        seen.append(kwargs["stdin"].read())
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert encode_mp4([(0, "", b"a"), (1, "", b"b")], tmp_path / "x.mp4", 1) == 2
    assert seen == [b"ab"]


def test_worker_continues_after_metadata_failure(tmp_path, monkeypatch):
    import sqlite3

    from poop_detector.clips import ClipStore
    from poop_detector.recording import ClipCollector

    def encode(samples, path, fps):
        path.write_bytes(b"mp4")
        return 1

    with ClipCollector(tmp_path, after=0, encoder=encode) as collector:
        original = collector.store.update
        failed = False

        def update(clip_id, **values):
            nonlocal failed
            if values.get("status") == "ready" and not failed:
                failed = True
                raise sqlite3.OperationalError("テスト用の書き込み失敗")
            return original(clip_id, **values)

        monkeypatch.setattr(collector.store, "update", update)
        collector.feed(np.zeros((100, 100, 3), np.uint8), now=0)
        first = ClipStore(tmp_path).request()
        collector.poll(now=0)
        collector.drain()
        assert ClipStore(tmp_path).get(first)["status"] == "failed"
        collector.feed(np.zeros((100, 100, 3), np.uint8), now=1)
        second = ClipStore(tmp_path).request()
        collector.poll(now=1)
        collector.drain()
        assert ClipStore(tmp_path).get(second)["status"] == "ready"


def test_camera_survives_recording_callback_failure():
    from poop_detector.camera import LatestCamera

    class Camera:
        generation = 1
        timeout_ms = 100

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return np.zeros((20, 20, 3), np.uint8)

    calls = 0

    def callback(frame, generation):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("テスト用の録画エラー")

    with LatestCamera(Camera(), callback) as camera:
        assert camera.wait_ready(1)
        assert camera.read() is not None


def test_encoding_start_metadata_failure_can_retry(tmp_path, monkeypatch):
    import sqlite3

    from poop_detector.clips import ClipStore
    from poop_detector.recording import ClipCollector

    def encode(samples, path, fps):
        path.write_bytes(b"mp4")
        return 1

    with ClipCollector(tmp_path, after=0, encoder=encode) as collector:
        original = collector.store.update
        failed = False

        def update(clip_id, **values):
            nonlocal failed
            if values.get("status") == "encoding" and not failed:
                failed = True
                raise sqlite3.OperationalError("テスト用の一時エラー")
            return original(clip_id, **values)

        monkeypatch.setattr(collector.store, "update", update)
        collector.feed(np.zeros((100, 100, 3), np.uint8), now=0)
        clip = ClipStore(tmp_path).request()
        collector.poll(now=0)
        collector.poll(now=1)
        collector.drain()
        assert ClipStore(tmp_path).get(clip)["status"] == "ready"
