"""カメラの音声から、犬が鳴いていた区間を見つけて記録する（実験的）。

映像とは別に FFmpeg で RTSP の音声だけを読み、16kHz・モノラルの PCM にして、
WINDOW_SECONDS ごとに音量（dBFS）と鳴き声の帯域（VOICE_BAND）の割合を測る。周りの静かな
ときの音量（背景）より threshold_db 大きく、鳴き声の帯域が多い窓を「鳴いている」とし、
GAP_SECONDS までの切れ目はつないで1つの区間にする。

室内の会話を録ることになりかねないので、音声そのものは保存せず、区間の時刻と長さ・
大きさだけを記録する。生活音（テレビ・掃除機・インターホン）とは区別できないことがある。
RTSP URL には認証情報が入るので、コマンドも FFmpeg の出力もログに出さない。
"""

import logging
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from ..config import Config
from ..records.absences import AwayMode
from ..records.vocal_episodes import VocalEpisodes
from ..timezones import local_now

logger = logging.getLogger(__name__)

RATE = 16000
WINDOW_SECONDS = 0.5
WINDOW_BYTES = int(RATE * WINDOW_SECONDS) * 2
# 鳴き声とみなす周波数の帯（Hz）。冷蔵庫や換気扇のような低い音を除く。
VOICE_BAND = (300.0, 4000.0)
VOICE_RATIO = 0.5
# この音量（dBFS）より小さい窓は、背景より大きくても鳴き声とみなさない。
MIN_DB = -50.0
# 背景の音量を覚えるまでの窓の数（起動してから5秒は判断しない）。
WARMUP_WINDOWS = 10
# 鳴いている窓の切れ目をこの秒数までつなぐ。
GAP_SECONDS = 3.0
# 鳴いていた時間がこれより短い区間は記録しない（物音1回を除く）。
MIN_LOUD_SECONDS = 1.0
# 外出中の通知で、鳴いていた時間を合計する範囲（分）と、次に知らせるまでの間（分）。
NOTICE_MINUTES = 10
NOTICE_COOLDOWN_MINUTES = 30
# FFmpeg が止まったとき、次に読み直すまでの秒数。
RETRY_SECONDS = 10


def measure(samples: NDArray[np.int16]) -> tuple[float, float]:
    """窓の音量（dBFS）と、鳴き声の帯域のエネルギーの割合を返す。"""
    values = samples.astype(np.float64) / 32768
    rms = float(np.sqrt(np.mean(values**2))) if values.size else 0.0
    loudness = 20 * np.log10(max(rms, 1e-5))
    spectrum = np.abs(np.fft.rfft(values)) ** 2
    frequencies = np.fft.rfftfreq(values.size, 1 / RATE)
    total = float(spectrum[1:].sum())
    band = (frequencies >= VOICE_BAND[0]) & (frequencies <= VOICE_BAND[1])
    ratio = float(spectrum[band].sum()) / total if total > 0 else 0.0
    return float(loudness), ratio


@dataclass(frozen=True)
class Episode:
    """鳴いていた1区間。loud_seconds は鳴いていた窓の合計、peak_db は最も大きな窓の音量。"""

    started_at: datetime
    ended_at: datetime
    loud_seconds: float
    peak_db: float


class VocalDetector:
    """窓ごとの音量と帯域から、鳴いていた区間を作る。"""

    def __init__(self, threshold_db: float) -> None:
        self.threshold_db = threshold_db
        self.background: float | None = None
        self.seen = 0
        self._open: tuple[datetime, datetime, float, float] | None = None

    def update(self, loudness: float, ratio: float, now: datetime) -> list[Episode]:
        """1窓分を受け取り、閉じた区間を返す。"""
        self.seen += 1
        background = self._learn(loudness)
        finished = self._close_if_quiet(now)
        loud = (
            self.seen > WARMUP_WINDOWS
            and loudness >= background + self.threshold_db
            and loudness >= MIN_DB
            and ratio >= VOICE_RATIO
        )
        if loud:
            end = now + timedelta(seconds=WINDOW_SECONDS)
            if self._open is None:
                self._open = (now, end, WINDOW_SECONDS, loudness)
            else:
                start, _, seconds, peak = self._open
                self._open = (start, end, seconds + WINDOW_SECONDS, max(peak, loudness))
        return finished

    def flush(self, now: datetime) -> list[Episode]:
        """続いている区間を閉じる（音声が途切れたときや終了のとき）。"""
        del now
        opened, self._open = self._open, None
        return _episode(opened)

    def _learn(self, loudness: float) -> float:
        # 背景は静かな側に早く、大きな側にゆっくり寄せる（鳴いている間に背景が上がらない）。
        if self.background is None:
            self.background = loudness
        elif loudness < self.background:
            self.background += 0.3 * (loudness - self.background)
        else:
            self.background += 0.01 * (loudness - self.background)
        return self.background

    def _close_if_quiet(self, now: datetime) -> list[Episode]:
        if self._open is None or (now - self._open[1]).total_seconds() <= GAP_SECONDS:
            return []
        return self.flush(now)


def _episode(opened: tuple[datetime, datetime, float, float] | None) -> list[Episode]:
    if opened is None or opened[2] < MIN_LOUD_SECONDS:
        return []
    return [Episode(*opened)]


class VocalNotifier(Protocol):
    """外出中に鳴き続けていることを知らせる通知先。"""

    def send_vocal(self, loud_seconds: float, minutes: int) -> bool: ...


class VocalRecorder:
    """区間を記録し、外出中に NOTICE_MINUTES の間で audio.notify_seconds 鳴いていたら知らせる。"""

    def __init__(
        self, episodes: VocalEpisodes, away: AwayMode, notifier: VocalNotifier, config: Config
    ) -> None:
        self.episodes, self.away, self.notifier = episodes, away, notifier
        self.notify_seconds = config.audio_notify_seconds
        self.notified_at: datetime | None = None

    def record(self, episode: Episode) -> None:
        self.episodes.add(
            episode.started_at, episode.ended_at, episode.loud_seconds, episode.peak_db
        )
        if self.away.since() is None:
            return
        now = episode.ended_at
        cooldown = timedelta(minutes=NOTICE_COOLDOWN_MINUTES)
        if self.notified_at is not None and now - self.notified_at < cooldown:
            return
        since = episode.started_at - timedelta(minutes=NOTICE_MINUTES)
        loud = sum(item.loud_seconds for item in self.episodes.between(since, now))
        if loud >= self.notify_seconds and self.notifier.send_vocal(loud, NOTICE_MINUTES):
            self.notified_at = now
            logger.info("外出中に鳴き続けていることを知らせました")


def _ffmpeg_command(source: str) -> list[str]:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise OSError("FFmpegが見つかりません")
    return [
        executable,
        "-hide_banner",
        "-loglevel",
        "quiet",
        "-rtsp_transport",
        "tcp",
        "-i",
        source,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(RATE),
        "-f",
        "s16le",
        "pipe:1",
    ]


class AudioReader:
    """FFmpeg で音声だけを読み、WINDOW_BYTES ごとの PCM を on_window に渡す。"""

    def __init__(
        self,
        source: str,
        on_window: Callable[[NDArray[np.int16]], None],
        *,
        spawn: Callable[..., Any] = subprocess.Popen,
        command: Callable[[str], list[str]] = lambda source: [source],
    ) -> None:
        self.source, self.on_window, self.spawn, self.command = source, on_window, spawn, command
        self.process: Any = None

    def read_once(self) -> None:
        """FFmpeg を1回起動し、止まるまで読む。"""
        # 固定の引数とカメラの URL だけで起動する。出力は捨て、URL をログに出さない。
        process = self.process = self.spawn(
            self.command(self.source),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            while True:
                chunk = process.stdout.read(WINDOW_BYTES)
                if len(chunk) < WINDOW_BYTES:
                    break
                self.on_window(np.frombuffer(chunk, dtype=np.int16))
        finally:
            process.kill()
            process.wait(timeout=5)
            self.process = None

    def stop(self) -> None:
        """読んでいる FFmpeg を止める（読み取りのブロックを抜けさせる）。"""
        process = self.process
        if process is not None and process.poll() is None:
            process.kill()


@contextmanager
def audio_watch(source: str, recorder: VocalRecorder, threshold_db: float) -> Iterator[None]:
    """ブロックの間、別スレッドで音声を読み続け、鳴いていた区間を記録する。"""
    stop = threading.Event()
    detector = VocalDetector(threshold_db)

    def on_window(samples: NDArray[np.int16]) -> None:
        for episode in detector.update(*measure(samples), local_now()):
            recorder.record(episode)

    reader = AudioReader(source, on_window, command=_ffmpeg_command)

    def loop() -> None:
        while not stop.is_set():
            try:
                reader.read_once()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                logger.warning("カメラの音声を読めませんでした（%s）", type(error).__name__)
            for episode in detector.flush(local_now()):
                recorder.record(episode)
            stop.wait(RETRY_SECONDS)

    thread = threading.Thread(target=loop, name="poop-detector-audio", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        reader.stop()
        thread.join(timeout=10)


def probe_audio(source: str, *, runner: Callable[..., Any] = subprocess.run) -> str | None:
    """カメラの映像に音声があれば、そのコーデック名を返す（なければ・確かめられなければ None）。"""
    executable = shutil.which("ffprobe") or "ffprobe"
    command = [
        executable,
        "-v",
        "quiet",
        "-rtsp_transport",
        "tcp",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "csv=p=0",
        source,
    ]
    try:
        # 固定の引数とカメラの URL だけで起動する。出力は codec 名だけを読む。
        result = runner(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    codec = (result.stdout or "").strip().splitlines()
    return codec[0].strip() if codec and codec[0].strip() else None
