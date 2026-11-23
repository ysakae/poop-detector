"""留守中のダイジェスト（#273）の、留守の区間・静止画・タイムラプス・年表を検証する。"""

import os
import shutil
import time
from datetime import datetime, timedelta

import numpy as np
import pytest

from poop_detector.config import Config, ConfigError
from poop_detector.features.digest import (
    KEEP_DAYS,
    DigestSnapshots,
    SnapshotExtension,
    timelapse,
)
from poop_detector.records.absences import AwayMode
from poop_detector.records.away_periods import AwayPeriods
from poop_detector.records.database import events_database

FRAME = np.full((120, 160, 3), 128, np.uint8)


def periods(tmp_path):
    database = events_database(tmp_path)
    return AwayPeriods(database), AwayMode(database)


# --- 留守の区間 ---


def test_away_toggle_opens_and_closes_one_period(tmp_path):
    away_periods, away = periods(tmp_path)
    assert away_periods.current() is None
    away.set_away(True, "家族")
    away.set_away(True, "家族")  # 押し直しても区間は1つ
    current = away_periods.current()
    assert current is not None
    assert current.ended_at is None
    away.set_away(False, "家族")
    away.set_away(False, "家族")
    assert away_periods.current() is None
    [period] = away_periods.list()
    assert period.id == current.id
    assert period.ended_at is not None
    assert away_periods.get(period.id) == period
    assert away_periods.get("missing") is None


def test_latest_finished_period(tmp_path):
    away_periods, away = periods(tmp_path)
    assert away_periods.latest_finished() is None
    away.set_away(True)
    assert away_periods.latest_finished() is None
    away.set_away(False)
    assert away_periods.latest_finished() is not None


# --- 静止画 ---


def test_snapshots_are_saved_in_order_and_old_folders_removed(tmp_path):
    snapshots = DigestSnapshots(tmp_path)
    start = datetime(2026, 11, 22, 9, 0).astimezone()
    for minute in (2, 0, 1):
        snapshots.save("a" * 32, FRAME, start + timedelta(minutes=minute))
    frames = snapshots.frames("a" * 32)
    assert len(frames) == 3
    assert frames == sorted(frames)
    old = snapshots.folder("b" * 32)
    snapshots.save("b" * 32, FRAME, start)
    stale = time.time() - (KEEP_DAYS + 1) * 86400
    os.utime(old, (stale, stale))
    assert snapshots.cleanup() == 1
    assert not old.exists()
    assert snapshots.frames("a" * 32)
    with pytest.raises(KeyError):
        snapshots.folder("../x")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 22, 9, 0).astimezone()

    def __call__(self):
        return self.now


def test_extension_saves_only_while_away_at_the_interval(tmp_path):
    away_periods, away = periods(tmp_path)
    snapshots = DigestSnapshots(tmp_path)
    clock = Clock()
    ext = SnapshotExtension(away_periods, snapshots, 60, clock=clock)
    ext.on_camera_frame(FRAME, False, 0.0)
    away.set_away(True)
    identifier = away_periods.current().id
    for tick in (60.0, 90.0, 120.0, 180.0):
        ext.on_camera_frame(FRAME, False, tick)
        clock.now += timedelta(seconds=30)
    ext.on_camera_frame(None, True, 300.0)
    assert len(snapshots.frames(identifier)) == 3
    away.set_away(False)
    ext.on_camera_frame(FRAME, False, 400.0)
    assert len(snapshots.frames(identifier)) == 3


# --- タイムラプス ---


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg がない")
def test_timelapse_is_built_once_and_rebuilt_when_frames_grow(tmp_path):
    snapshots = DigestSnapshots(tmp_path)
    start = datetime(2026, 11, 22, 9, 0).astimezone()
    for minute in range(5):
        snapshots.save("a" * 32, FRAME, start + timedelta(minutes=minute))
    path = timelapse(snapshots, "a" * 32)
    assert path is not None
    assert path.stat().st_size > 0
    built = path.stat().st_mtime_ns
    assert timelapse(snapshots, "a" * 32).stat().st_mtime_ns == built
    time.sleep(0.01)
    snapshots.save("a" * 32, FRAME, start + timedelta(minutes=9))
    assert timelapse(snapshots, "a" * 32).stat().st_mtime_ns != built


def test_timelapse_without_frames_is_none(tmp_path):
    assert timelapse(DigestSnapshots(tmp_path), "c" * 32) is None


# --- 設定 ---


def test_digest_settings():
    assert Config().digest_enabled is True
    assert Config().digest_snapshot_seconds == 60
    with pytest.raises(ConfigError, match=r"digest\.snapshot_seconds"):
        Config(digest_snapshot_seconds=1)


def test_standard_extensions_add_snapshots_without_dog_detection(tmp_path):
    from poop_detector.detection.roi import ROI
    from poop_detector.features.monitoring import standard_extensions

    class Notifier:
        def send(self, **_kwargs):
            return False

    config = Config(data_dir=str(tmp_path), sheet_enabled=False)
    built = standard_extensions(ROI(0, 0, 1, 1), FRAME, config, Notifier(), None)
    assert any(isinstance(item, SnapshotExtension) for item in built)
    off = Config(data_dir=str(tmp_path), sheet_enabled=False, digest_enabled=False)
    assert not standard_extensions(ROI(0, 0, 1, 1), FRAME, off, Notifier(), None)


# --- 画面 ---


def test_digest_pages_show_timeline_video_and_home_link(gui):
    from support import exchange, login

    from poop_detector.records.absences import Absences

    server, store, _ = gui
    database = store.database
    away_periods, away = AwayPeriods(database), AwayMode(database)
    cookie = login(server, role="member")
    status, _, body = exchange(server, "GET", "/digest", headers=cookie, auth=False)
    assert status == 200
    assert "留守中のまとめはまだありません" in body.decode()
    away.set_away(True)
    period = away_periods.current()
    now = datetime.now().astimezone()
    store.add_excretion("feces", (now - timedelta(seconds=1)).isoformat())
    absence = Absences(database).start(now, away=True)
    Absences(database).end(absence, now + timedelta(minutes=12))
    snapshots = DigestSnapshots(server.app.root)
    for minute in range(3):
        snapshots.save(period.id, FRAME, now + timedelta(minutes=minute))
    status, _, body = exchange(server, "GET", f"/digest?id={period.id}", headers=cookie, auth=False)
    html = body.decode()
    assert status == 200
    assert "外出中" in html
    assert "サークルにいない（約12分）" in html
    assert f"/digest-video?id={period.id}" in html
    assert "静止画 3枚" in html
    away.set_away(False)
    _, _, body = exchange(server, "GET", "/", headers=cookie, auth=False)
    assert f'href="/digest?id={period.id}"' in body.decode()
    _, _, body = exchange(server, "GET", "/digest", headers=cookie, auth=False)
    assert f"/digest?id={period.id}" in body.decode()
    status, _, _ = exchange(server, "GET", "/digest?id=missing", headers=cookie, auth=False)
    assert status == 404
    status, _, _ = exchange(server, "GET", "/digest-video?id=../x", headers=cookie, auth=False)
    assert status == 404
    status, _, _ = exchange(
        server, "GET", f"/digest-video?id={'d' * 32}", headers=cookie, auth=False
    )
    assert status == 404
    if shutil.which("ffmpeg"):
        status, headers, video = exchange(
            server, "GET", f"/digest-video?id={period.id}", headers=cookie, auth=False
        )
        assert status == 200
        assert headers["content-type"] == "video/mp4"
        assert len(video) > 0


def test_digest_video_reports_encoder_failure(gui, monkeypatch):
    from support import exchange, login

    from poop_detector.gui import digest_routes

    server, _, _ = gui

    def broken(_snapshots, _identifier):
        raise OSError("FFmpeg がない")

    monkeypatch.setattr(digest_routes, "timelapse", broken)
    cookie = login(server, role="member")
    status, _, _ = exchange(
        server, "GET", f"/digest-video?id={'a' * 32}", headers=cookie, auth=False
    )
    assert status == 503


def test_timelapse_timeout_grows_with_the_number_of_frames(tmp_path, monkeypatch):
    from poop_detector.features import digest

    seen = []

    def encode(samples, path, fps, *, timeout):
        seen.append((len(samples), timeout))
        path.write_bytes(b"mp4")
        return len(samples) / fps

    monkeypatch.setattr(digest, "encode_mp4", encode)
    snapshots = DigestSnapshots(tmp_path)
    start = datetime(2026, 11, 22, 9, 0).astimezone()
    for minute in range(3):
        snapshots.save("a" * 32, FRAME, start + timedelta(minutes=minute))
    digest.timelapse(snapshots, "a" * 32)
    for minute in range(3, 1440):
        snapshots.save("a" * 32, FRAME, start + timedelta(minutes=minute))
    time.sleep(0.01)
    digest.timelapse(snapshots, "a" * 32)
    # 少ないときは録画と同じ30秒、1日分（1440枚）なら72秒まで待つ。
    assert seen == [(3, 30), (1440, pytest.approx(72))]


def test_encode_mp4_passes_the_timeout_to_ffmpeg(tmp_path, monkeypatch):
    import subprocess as subprocess_module

    from poop_detector.video import recording

    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["timeout"])
        return subprocess_module.CompletedProcess(args, 0)

    monkeypatch.setattr(recording.subprocess, "run", run)
    monkeypatch.setattr(recording.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    samples = [(0.0, "", b"jpeg"), (0.1, "", b"jpeg")]
    recording.encode_mp4(samples, tmp_path / "a.mp4", 10)
    recording.encode_mp4(samples, tmp_path / "b.mp4", 10, timeout=90)
    assert calls == [30, 90]
