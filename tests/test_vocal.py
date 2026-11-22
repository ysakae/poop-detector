"""カメラの音声から鳴き声・吠え声の時間を記録する（#275、実験的）を検証する。"""

import io
import subprocess
from datetime import datetime, timedelta

import numpy as np
import pytest

from poop_detector.config import Config, ConfigError, check_monitor_features
from poop_detector.experimental.vocal import (
    RATE,
    WINDOW_BYTES,
    WINDOW_SECONDS,
    AudioReader,
    VocalDetector,
    VocalRecorder,
    measure,
    probe_audio,
)
from poop_detector.notifications import ToiletNotifier
from poop_detector.records.absences import AwayMode
from poop_detector.records.database import events_database
from poop_detector.records.vocal_episodes import VocalEpisodes

START = datetime(2026, 11, 23, 9, 0).astimezone()
SAMPLES = int(RATE * WINDOW_SECONDS)


def tone(frequency, amplitude):
    t = np.arange(SAMPLES) / RATE
    return (np.sin(2 * np.pi * frequency * t) * amplitude * 32767).astype(np.int16)


def noise(amplitude, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.normal(0, amplitude * 32767, SAMPLES)).clip(-32768, 32767).astype(np.int16)


QUIET = noise(0.002)
BARK = tone(800, 0.5)
HUM = tone(60, 0.5)  # 冷蔵庫や換気扇のような低い音


# --- 測る ---


def test_measure_reports_loudness_and_voice_band():
    quiet_db, _ = measure(QUIET)
    bark_db, bark_band = measure(BARK)
    _, hum_band = measure(HUM)
    assert bark_db > quiet_db + 30
    assert bark_band > 0.9
    assert hum_band < 0.1
    assert measure(np.zeros(SAMPLES, np.int16))[0] <= -90


# --- 区間 ---


def feed(detector, windows, start=START):
    finished = []
    now = start
    for samples in windows:
        finished += detector.update(*measure(samples), now)
        now += timedelta(seconds=WINDOW_SECONDS)
    return finished, now


def test_barking_after_quiet_becomes_one_episode():
    detector = VocalDetector(threshold_db=15)
    windows = [QUIET] * 20 + [BARK, BARK, QUIET, BARK] + [QUIET] * 10
    finished, _ = feed(detector, windows)
    [episode] = finished
    assert episode.started_at == START + timedelta(seconds=20 * WINDOW_SECONDS)
    assert episode.loud_seconds == pytest.approx(3 * WINDOW_SECONDS)
    assert episode.peak_db > -20


def test_low_hum_and_single_clicks_are_not_episodes():
    detector = VocalDetector(threshold_db=15)
    finished, _ = feed(detector, [QUIET] * 20 + [HUM] * 10 + [QUIET] * 10)
    assert finished == []
    finished, _ = feed(VocalDetector(threshold_db=15), [QUIET] * 20 + [BARK] + [QUIET] * 10)
    assert finished == []


def test_detector_waits_for_the_background_level_before_judging():
    finished, _ = feed(VocalDetector(threshold_db=15), [BARK] * 4 + [QUIET] * 10)
    assert finished == []


def test_flush_closes_an_open_episode():
    detector = VocalDetector(threshold_db=15)
    _, now = feed(detector, [QUIET] * 20 + [BARK] * 4)
    [episode] = detector.flush(now)
    assert episode.loud_seconds == pytest.approx(4 * WINDOW_SECONDS)
    assert detector.flush(now) == []


# --- 記録と通知 ---


class Notifier:
    def __init__(self):
        self.sent = []

    def send_vocal(self, loud_seconds, minutes):
        self.sent.append((loud_seconds, minutes))
        return True


def episode(start, loud):
    from poop_detector.experimental.vocal import Episode

    return Episode(start, start + timedelta(seconds=loud * 1.5), loud, -10.0)


def test_recorder_saves_episodes_and_notifies_once_while_away(tmp_path):
    database = events_database(tmp_path)
    episodes, away = VocalEpisodes(database), AwayMode(database)
    notifier = Notifier()
    recorder = VocalRecorder(episodes, away, notifier, Config(audio_notify_seconds=60))
    recorder.record(episode(START, 40))
    assert notifier.sent == []
    away.set_away(True)
    recorder.record(episode(START + timedelta(minutes=2), 30))
    assert notifier.sent == [(70, 10)]
    recorder.record(episode(START + timedelta(minutes=3), 30))
    assert len(notifier.sent) == 1
    assert len(episodes.list()) == 3


def test_recorder_does_not_notify_at_home(tmp_path):
    database = events_database(tmp_path)
    notifier = Notifier()
    recorder = VocalRecorder(
        VocalEpisodes(database), AwayMode(database), notifier, Config(audio_notify_seconds=10)
    )
    recorder.record(episode(START, 120))
    assert notifier.sent == []


def test_daily_totals(tmp_path):
    episodes = VocalEpisodes(events_database(tmp_path))
    episodes.add(START, START + timedelta(seconds=30), 12.5, -12.0)
    episodes.add(START + timedelta(hours=1), START + timedelta(hours=1, seconds=5), 2.0, -20.0)
    totals = episodes.daily_totals(START.date(), START.date())
    assert totals[START.date()] == (2, pytest.approx(14.5))
    assert len(episodes.between(START, START + timedelta(minutes=30))) == 1


# --- 音声の読み取り ---


class Process:
    def __init__(self, data):
        self.stdout = io.BytesIO(data)
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_reader_passes_whole_windows_and_never_logs_the_url(caplog):
    windows = []
    data = (QUIET.tobytes() + BARK.tobytes()) + b"\x00" * 10
    processes = []

    def spawn(command, **_kwargs):
        assert "rtsp://user:secret@cam/stream" in command
        process = Process(data)
        processes.append(process)
        return process

    reader = AudioReader("rtsp://user:secret@cam/stream", windows.append, spawn=spawn)
    reader.read_once()
    assert len(windows) == 2
    assert len(windows[0]) == SAMPLES
    assert processes[0].killed
    assert "secret" not in caplog.text
    assert WINDOW_BYTES == SAMPLES * 2


def test_probe_audio_reads_the_codec_and_handles_failures():
    def runner(command, **_kwargs):
        assert "-select_streams" in command
        return subprocess.CompletedProcess(command, 0, stdout="aac\n", stderr="")

    assert probe_audio("rtsp://cam", runner=runner) == "aac"

    def silent(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    assert probe_audio("rtsp://cam", runner=silent) is None

    def broken(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 10)

    assert probe_audio("rtsp://cam", runner=broken) is None


# --- 文面と設定 ---


class Messages(ToiletNotifier):
    def __init__(self):
        super().__init__(True, "ポチ")
        self.messages = []

    def _deliver(self, message, *, early_alert, path=""):
        self.messages.append((message, path))
        return True


def test_vocal_message():
    notifier = Messages()
    assert notifier.send_vocal(75, 10)
    message, path = notifier.messages[0]
    assert "ポチ" in message
    assert "10分" in message
    assert "約1分" in message
    assert path == "/vocal"


def test_audio_settings():
    config = Config()
    assert config.audio_enabled is False
    assert (config.audio_threshold_db, config.audio_notify_seconds) == (15, 60)
    with pytest.raises(ConfigError, match=r"audio\.threshold_db"):
        Config(audio_threshold_db=1)
    # 音声は犬の検出を使わない。
    assert not check_monitor_features(Config(audio_enabled=True)).dog_enabled


# --- 監視の間の読み取り ---


def test_audio_watch_records_barking_from_a_fake_ffmpeg(tmp_path, monkeypatch):
    import time as clock

    from poop_detector.experimental import vocal

    data = b"".join(w.tobytes() for w in [QUIET] * 20 + [BARK] * 4 + [QUIET] * 10)
    monkeypatch.setattr(vocal, "_ffmpeg_command", lambda source: [source])
    monkeypatch.setattr(vocal, "RETRY_SECONDS", 60)
    database = events_database(tmp_path)
    episodes = VocalEpisodes(database)
    recorder = VocalRecorder(episodes, AwayMode(database), Notifier(), Config())
    spawned = []

    class Reader(vocal.AudioReader):
        def __init__(self, source, on_window, **kwargs):
            super().__init__(source, on_window, spawn=lambda *a, **k: Process(data))
            spawned.append(source)

    monkeypatch.setattr(vocal, "AudioReader", Reader)
    with vocal.audio_watch("rtsp://cam", recorder, 15):
        for _ in range(50):
            if episodes.list():
                break
            clock.sleep(0.02)
    assert spawned == ["rtsp://cam"]
    assert len(episodes.list()) == 1


def test_reader_stop_kills_a_running_ffmpeg():
    from poop_detector.experimental.vocal import AudioReader

    reader = AudioReader("rtsp://cam", lambda window: None)
    reader.stop()  # 動いていなければ何もしない
    process = Process(b"")
    reader.process = process
    reader.stop()
    assert process.killed


# --- 画面とコマンド ---


def test_vocal_page_and_digest_timeline(gui):
    from support import exchange, login

    from poop_detector.records.away_periods import AwayPeriods

    server, store, _ = gui
    database = store.database
    cookie = login(server, role="member")
    status, _, body = exchange(server, "GET", "/vocal", headers=cookie, auth=False)
    assert status == 200
    assert "鳴き声の記録はまだありません" in body.decode()
    AwayMode(database).set_away(True)
    period = AwayPeriods(database).current()
    now = datetime.now().astimezone()
    VocalEpisodes(database).add(now, now + timedelta(seconds=90), 75, -12.0)
    _, _, body = exchange(server, "GET", "/vocal", headers=cookie, auth=False)
    html = body.decode()
    assert "1分15秒" in html
    assert "最大 -12 dBFS" in html
    _, _, body = exchange(server, "GET", f"/digest?id={period.id}", headers=cookie, auth=False)
    assert "鳴き声（75秒）" in body.decode()
    _, _, body = exchange(server, "GET", "/records", headers=cookie, auth=False)
    assert 'href="/vocal"' in body.decode()
