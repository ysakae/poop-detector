"""監視側の動画収集。フレームを上限付きで保持し、保存要求の前後を MP4 に変換する。"""

import fcntl
import itertools
import logging
import os
import queue
import selectors
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Self, TypeAlias

import cv2

from ..errors import AlreadyRunningError
from ..frames import Frame
from ..records.events import utcnow
from .clip_storage import DEFAULT_DISK_MIB, MIB, free_space, over_limit, recording_usage
from .clips import ClipStore, RequestSource
from .thumbnails import ensure_thumbnail

logger = logging.getLogger(__name__)
# 録画中に届いた別の保存要求は、新しい動画を作らずにその録画を延ばして1本にまとめる
# （犬の入場・トレー外の滞在・見守り通知が数秒おきに重なり、同じ場面の動画が何本もできるため）。
# 最初の要求からこの秒数を過ぎた要求はまとめず、動画1本の長さとメモリーの使用量を抑える
# （前後の秒数を足して最長4分ほど。判定のシートは長さに応じて枚数を増やす）。
MAX_EXTENSION_SECONDS = 180
# 録画の終わりからこの秒数は動画を確定させずに待ち、その間に届いた要求も同じ動画にまとめる。
# 犬が出入りを繰り返すと、録画の終わった直後に次の要求が届き、同じ滞在が別の動画になるため。
# 要求が来なければ、待っていた間のコマは捨てて元の終わりで確定する。
# 新しい動画は要求の前（clips.before_seconds、既定30秒）から録るので、別に録った動画が
# 前の動画の終わりから30秒以内に始まる要求までまとめるには、30秒＋30秒待つ必要がある
# （30秒では、終わりの41秒後ほどに来る犬の出入りの要求を取りこぼしていた）。
MERGE_GRACE_SECONDS = 60
# 動画をまとめたときに残すきっかけの優先度（小さいほど優先）。自動判定の順番
# （clip_reviews.PRIORITY_ORDER）と同じで、トレーの中を見る判定をトレー外の判定より優先する。
SOURCE_RANK: dict[str, int] = {
    "dog_enter": 1,
    "dog_exit": 1,
    "dog_visit": 1,
    "outside_stay": 2,
}
# (取得時刻 monotonic, 取得時刻 UTC の ISO 8601, JPEG のバイト列)
Sample: TypeAlias = tuple[float, str, bytes]
# 録画の MP4 への変換を打ち切る秒数。
ENCODE_TIMEOUT = 30
Encoder: TypeAlias = Callable[[Sequence[Sample], Path, float], float]


def encode_frame(frame: Frame, max_width: int) -> bytes | None:
    """録画用に幅を制限し、MP4 に使える偶数寸法の JPEG を返す。"""
    height, width = frame.shape[:2]
    scale = min(1, max_width / width)
    size = (max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2))
    resized = cv2.resize(frame, size) if size != (width, height) else frame
    ok, encoded = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return encoded.tobytes() if ok else None


def encode_mp4(
    samples: Sequence[Sample], path: Path, fps: float, *, timeout: float = ENCODE_TIMEOUT
) -> float:
    """JPEG のサンプルを一定の FPS の MP4 にし、動画の秒数を返す。

    timeout 秒で FFmpeg を打ち切る（録画は既定の ENCODE_TIMEOUT、長いタイムラプスは長くする）。
    """
    if not samples:
        raise ValueError("保存するフレームがありません")
    # カメラの揺れる取得間隔を一定FPSへ揃える。欠落区間は同じフレームで補間する。
    frames: list[bytes] = []
    index = 0
    count = max(1, round((samples[-1][0] - samples[0][0]) * fps) + 1)
    for step in range(count):
        target = samples[0][0] + step / fps
        while index + 1 < len(samples) and samples[index + 1][0] <= target + 1e-6:
            index += 1
        frames.append(samples[index][2])
    encode_jpegs(frames, path, fps, timeout=timeout)
    return count / fps


def encode_jpegs(frames: Iterable[bytes], path: Path, fps: float, *, timeout: float) -> None:
    """JPEG を順にパイプへ渡す。書き込みで詰まった時間も FFmpeg の制限時間に含める。"""
    executable = shutil.which("ffmpeg")
    if not executable:
        raise OSError("FFmpegが見つかりません")
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "image2pipe",
        "-framerate",
        str(fps),
        "-vcodec",
        "mjpeg",
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(path),
    ]
    deadline = time.monotonic() + timeout
    # shutil.which で見つけた FFmpeg を、固定の引数と保存先のパスだけで起動する。
    with subprocess.Popen(  # noqa: S603
        command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ) as process:
        try:
            if process.stdin is None:
                raise OSError("FFmpeg の入力パイプを開けません")
            try:
                with process.stdin as pipe, selectors.DefaultSelector() as selector:
                    fd = pipe.fileno()
                    os.set_blocking(fd, False)
                    selector.register(fd, selectors.EVENT_WRITE)
                    for jpeg in frames:
                        pending = memoryview(jpeg)
                        while pending:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or not selector.select(remaining):
                                raise subprocess.TimeoutExpired(command, timeout)
                            pending = pending[os.write(fd, pending) :]
            except BrokenPipeError:
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
                if code:
                    raise subprocess.CalledProcessError(code, command) from None
                raise
            code = process.wait(timeout=max(0, deadline - time.monotonic()))
            if code:
                raise subprocess.CalledProcessError(code, command)
        finally:
            if process.poll() is None:
                process.kill()


def merged_source(current: str, incoming: RequestSource) -> str:
    """まとめた動画のきっかけ。入場と退場は dog_visit にし、ほかは優先度の高いほうを残す。"""
    if {current, incoming} == {"dog_enter", "dog_exit"}:
        return "dog_visit"
    if SOURCE_RANK.get(incoming, 0) < SOURCE_RANK.get(current, 0):
        return incoming
    return current


class ClipCollector:
    """フレームをメモリー上限付きで保持し、保存要求の前後を MP4 に変換する。"""

    def __init__(
        self,
        root: str | Path,
        before: float = 30,
        after: float = 30,
        fps: float = 5,
        max_width: int = 1280,
        memory_mb: float = 128,
        disk_mb: float = DEFAULT_DISK_MIB,
        encoder: Encoder = encode_mp4,
        auto_cleanup: bool = True,
    ) -> None:
        self.store = ClipStore(root)
        self.before, self.after, self.fps = before, after, fps
        self.max_width = max_width
        self.memory_limit, self.disk_limit = int(memory_mb * 1024**2), int(disk_mb * MIB)
        self.encoder = encoder
        self.auto_cleanup = auto_cleanup
        self.buffer: deque[Sample] = deque()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.queue: queue.Queue[str | None] = queue.Queue(maxsize=4)
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.generation: tuple[int, tuple[int, ...]] | None = None
        self.last_sample = float("-inf")
        self.last_frame = float("-inf")
        self.last_poll = float("-inf")
        self.last_heartbeat = float("-inf")
        self.healthy: bool | None = None
        self.scene_signature: str | None = None
        self.capture_lock: IO[str] | None = None
        self.maintenance = False

    def __enter__(self) -> Self:
        lock = (self.store.root / ".capture.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise AlreadyRunningError("別の動画監視が起動中です") from None
        self.capture_lock = lock
        with self.store.connect() as db:
            # 前回の異常終了で残った処理中行を新しい録画と混ぜない。
            db.execute(
                "UPDATE clips SET status='failed',error='前回の監視が終了したため保存できませんでした' WHERE status IN ('queued','recording','encoding')"
            )
        self.worker.start()
        return self

    def set_maintenance(self, paused: bool) -> None:
        """交換作業のフレームを録画の前後に含めない。"""
        with self.lock:
            if paused and not self.maintenance:
                self._interrupt()
                self._heartbeat(False, time.monotonic())
            self.maintenance = paused

    def _bytes(self) -> int:
        # 待機バッファと複数の録画は同じ JPEG を参照するので、実体を1回ずつ数える。
        sizes = {id(jpeg): len(jpeg) for _, _, jpeg in self.buffer}
        for job in self.jobs.values():
            sizes.update((id(jpeg), len(jpeg)) for _, _, jpeg in job["samples"])
        return sum(sizes.values())

    def feed(self, frame: Frame | None, now: float | None = None, generation: int = 0) -> None:
        """カメラのフレームを受け取り、clip_fps の間隔で JPEG にして保持する。"""
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.maintenance:
                return
            if frame is None:
                if self.buffer or self.jobs:
                    self._interrupt()
                return
            identity = (generation, frame.shape)
            if self.generation is not None and self.generation != identity:
                self._interrupt()
            self.generation = identity
            self.last_frame = now
            if now - self.last_sample < 1 / self.fps - 1e-6:
                return
            self.last_sample = now
            jpeg = encode_frame(frame, self.max_width)
            if jpeg is None:
                return
            sample = (now, utcnow(), jpeg)
            self._distribute_sample(sample)
            self._enforce_memory_limit()

    def _distribute_sample(self, sample: Sample) -> None:
        now = sample[0]
        self.buffer.append(sample)
        while self.buffer and (now - self.buffer[0][0] > self.before + 1e-6):
            self.buffer.popleft()
        for job in self.jobs.values():
            if (
                job["status"] == "recording"
                and not job.get("interrupted")
                and job["trigger"] < now <= job["end"] + job.get("grace", 0) + 1e-6
            ):
                job["samples"].append(sample)

    def _enforce_memory_limit(self) -> None:
        """呼び出し側のロック内で、待機バッファ、まとめるために待っている録画、録画中の順に
        メモリーを解放する。待っている録画は終わりまで録れているので、失敗にせず確定する。
        """
        while self.buffer and self._bytes() > self.memory_limit:
            self.buffer.popleft()
        for clip_id, job in list(self.jobs.items()):
            if self._bytes() <= self.memory_limit:
                break
            samples = job["samples"]
            if job["status"] == "recording" and samples and samples[-1][0] > job["end"]:
                self._finish(clip_id)
        if self._bytes() > self.memory_limit:
            for clip_id in list(self.jobs):
                if self.jobs[clip_id]["status"] == "recording":
                    self.store.update(
                        clip_id, status="failed", error="録画メモリー上限に達しました"
                    )
                    del self.jobs[clip_id]

    def _interrupt(self) -> None:
        for clip_id, job in list(self.jobs.items()):
            if job["status"] == "recording":
                self._finish(clip_id, interrupted=True)
        self.buffer.clear()
        self.last_sample = self.last_frame = float("-inf")

    def trigger(
        self,
        event_id: str | None = None,
        source: RequestSource = "detected",
        *,
        now: float | None = None,
        tray_stay_seconds: float | None = None,
    ) -> str | None:
        """監視側から保存を要求する。同時処理数を超えたら None を返す。"""
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.maintenance:
                return None
            # 検知のイベントにひも付く動画は、検知の時刻を基準にするので別に録る。
            clip_id = None if event_id is not None else self._extend(source, now)
            if clip_id is None:
                try:
                    clip_id = self.store.request(source, event_id)
                    self.poll(now=now, force=True)
                except ValueError:
                    logger.warning("動画保存の同時処理数を超えています")
                    return None
                if event_id is None and clip_id in self.jobs:
                    # 後の要求をまとめられる動画だけ、録画の終わりから少し待って確定する。
                    # 検知と GUI からの保存はまとめないので、待たずに確定する。
                    self.jobs[clip_id]["grace"] = MERGE_GRACE_SECONDS
            if tray_stay_seconds is not None and clip_id in self.jobs:
                job = self.jobs[clip_id]
                job["tray_stay_seconds"] = max(job.get("tray_stay_seconds", 0), tray_stay_seconds)
            return clip_id

    def _extend(self, source: RequestSource, now: float) -> str | None:
        """録画中の動画を延ばして要求をまとめ、その ID を返す。まとめられなければ None。"""
        if now - self.last_frame >= 5:
            return None
        for clip_id, job in reversed(self.jobs.items()):
            if (
                job["status"] == "recording"
                and not job.get("interrupted")
                and 0 <= now - job["trigger"] <= MAX_EXTENSION_SECONDS
                and now <= job["end"] + job.get("grace", 0)
                and self._fits(job, now + self.after)
            ):
                job["end"] = max(job["end"], now + self.after)
                job["source"] = merged_source(job["source"], source)
                return clip_id
        return None

    def _fits(self, job: dict[str, Any], end: float) -> bool:
        """録画の終わりを end まで延ばしても、メモリーの上限に収まる見込みか。

        上限を超えると録画ごと失敗になるので、収まらなければ延ばさずに別の動画にする。
        """
        samples = job["samples"]
        if not samples or end <= job["end"]:
            return True
        # 確定待ちの間に受け取ったコマも保持済みなので、最後に取得したコマから先だけを見積もる。
        remaining = max(0.0, end - samples[-1][0])
        per_frame = sum(len(sample[2]) for sample in samples) / len(samples)
        return bool(self._bytes() + per_frame * self.fps * remaining <= self.memory_limit)

    def _heartbeat(self, healthy: bool, now: float) -> None:
        # GUI の録画要求は従来どおり毎秒5回拾い、稼働状態だけ書き込み頻度を落とす。
        if healthy != self.healthy or now - self.last_heartbeat >= 1:
            self.store.heartbeat(healthy)
            self.healthy = healthy
            self.last_heartbeat = now

    def poll(self, now: float | None = None, force: bool = False) -> None:
        """GUI からの保存要求を拾い、録画の終わった要求を変換に回す。"""
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.maintenance:
                return
            if not force and now - self.last_poll < 0.2:
                return
            self.last_poll = now
            self._heartbeat(now - self.last_frame < 5, now)
            for row in self.store.queued():
                clip_id = row["id"]
                if not self.buffer or now - self.last_frame >= 5:
                    self.store.update(
                        clip_id, status="failed", error="カメラ映像を取得できていません"
                    )
                    continue
                samples = [s for s in self.buffer if now - s[0] <= self.before + 1e-6]
                self.jobs[clip_id] = {
                    "samples": samples,
                    "source": row["source"],
                    "trigger": now,
                    "end": now + self.after,
                    "status": "recording",
                }
                if self._bytes() > self.memory_limit:
                    del self.jobs[clip_id]
                    self.store.update(
                        clip_id, status="failed", error="録画メモリー上限に達しました"
                    )
                else:
                    self.store.update(
                        clip_id, status="recording", scene_signature=self.scene_signature
                    )
            for clip_id, job in list(self.jobs.items()):
                if job["status"] == "recording" and (
                    job.get("interrupted") or now >= job["end"] + job.get("grace", 0)
                ):
                    self._finish(clip_id)

    def _finish(self, clip_id: str, interrupted: bool = False) -> None:
        job = self.jobs[clip_id]
        # まとめるために待っていた間のコマは、録画の終わりまでで切る。
        samples = [sample for sample in job["samples"] if sample[0] <= job["end"] + 1e-6]
        job["samples"] = samples
        if not samples:
            self.store.update(clip_id, status="failed", error="保存するフレームがありません")
            del self.jobs[clip_id]
            return
        # 終わりまで録れていれば、待っている間の途切れは中断として扱わない。
        complete = samples[-1][0] >= job["end"] - 1 / self.fps
        job["interrupted"] = bool(job.get("interrupted") or interrupted) and not complete
        gaps = any(b[0] - a[0] > max(1, 3 / self.fps) for a, b in itertools.pairwise(samples))
        try:
            self.store.update(
                clip_id,
                status="encoding",
                source=job["source"],
                start_at=samples[0][1],
                end_at=samples[-1][1],
                fps=self.fps,
                trigger_offset=max(0, job["trigger"] - samples[0][0]),
                pre_available=max(0, job["trigger"] - samples[0][0]),
                post_available=max(0, samples[-1][0] - job["trigger"]),
                tray_stay_seconds=job.get("tray_stay_seconds"),
                interrupted=int(
                    job["interrupted"] or gaps or samples[-1][0] < job["end"] - 1 / self.fps
                ),
            )
        except sqlite3.Error:
            logger.warning("動画の変換開始をDBに保存できません。次回再試行します")
            return
        job["status"] = "encoding"
        self.queue.put_nowait(clip_id)

    def _free_space(self) -> None:
        """録画の前に容量の自動整理を行う。整理に失敗しても、録画の保存は続ける。"""
        if not self.auto_cleanup:
            return
        try:
            free_space(self.store, self.disk_limit)
        except (OSError, sqlite3.Error):
            logger.warning("録画容量の自動整理に失敗しました。保存先とDBを確認してください")

    def _worker(self) -> None:
        while True:
            clip_id = self.queue.get()
            try:
                if clip_id is None:
                    return
                job = self.jobs[clip_id]
                path = self.store.root / "clips" / f"{clip_id}.mp4"
                temporary = path.with_suffix(".part.mp4")
                try:
                    self._free_space()
                    used = recording_usage(self.store.root)
                    if used >= self.disk_limit:
                        raise OSError("動画保存容量上限")
                    duration = self.encoder(job["samples"], temporary, self.fps)
                    if over_limit(used + temporary.stat().st_size, self.disk_limit, job["source"]):
                        raise OSError("動画保存容量上限")
                    temporary.replace(path)
                    self.store.update(
                        clip_id, status="ready", path=f"clips/{path.name}", duration=duration
                    )
                    self._thumbnail(clip_id)
                except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error):
                    try:
                        self.store.update(
                            clip_id,
                            status="failed",
                            error="動画保存に失敗しました。FFmpeg・保存容量・DBを確認してください",
                        )
                    except sqlite3.Error:
                        logger.error("動画の失敗状態をDBへ保存できませんでした")
                    try:
                        if temporary.exists():
                            temporary.unlink()
                    except OSError:
                        logger.error("動画の一時ファイルを片付けられませんでした")
                finally:
                    with self.lock:
                        self.jobs.pop(clip_id, None)
            finally:
                self.queue.task_done()

    def _thumbnail(self, clip_id: str) -> None:
        """一覧のサムネイルを作る。作れなくても録画は成功のままにし、開いたときに作り直す。"""
        try:
            ensure_thumbnail(self.store, self.store.get(clip_id))
        except sqlite3.Error:
            logger.warning("動画のサムネイルを作れませんでした")

    def drain(self) -> None:
        self.queue.join()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        with self.lock:
            self._interrupt()
        self.store.heartbeat(False)
        self.queue.put(None)
        self.worker.join()
        if self.capture_lock is not None:
            self.capture_lock.close()
