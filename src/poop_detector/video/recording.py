"""監視側の動画収集。フレームを上限付きで保持し、保存要求の前後を MP4 に変換する。"""

import fcntl
import itertools
import logging
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Self, TypeAlias

import cv2

from ..errors import AlreadyRunningError
from ..frames import Frame
from ..records.events import utcnow
from .clip_storage import DEFAULT_DISK_MIB, MIB, recording_usage
from .clips import ClipStore, RequestSource

logger = logging.getLogger(__name__)
# 入場からこの秒数以内の退場は、入場の録画を延ばして1本にまとめる（短い出入りで動画を分けない）。
# 録画の前後の長さ（clips.before_seconds・after_seconds）で実質の長さは調整できるので、設定にはしない。
SHORT_VISIT_SECONDS = 15
# (取得時刻 monotonic, 取得時刻 UTC の ISO 8601, JPEG のバイト列)
Sample: TypeAlias = tuple[float, str, bytes]
Encoder: TypeAlias = Callable[[Sequence[Sample], Path, float], float]


def encode_mp4(samples: Sequence[Sample], path: Path, fps: float) -> float:
    """JPEG のサンプルを一定の FPS の MP4 にし、動画の秒数を返す。"""
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
    executable = shutil.which("ffmpeg")
    if not executable:
        raise OSError("FFmpegが見つかりません")
    # 連結JPEGをメモリーへ複製せず、一時ファイルからFFmpegへ渡す。
    with tempfile.TemporaryFile(dir=Path(path).parent) as spool:
        for frame in frames:
            spool.write(frame)
        spool.seek(0)
        # shutil.which で見つけた FFmpeg を、固定の引数と保存先のパスだけで起動する。
        subprocess.run(  # noqa: S603
            [
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
            ],
            stdin=spool,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
        )
    return count / fps


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
    ) -> None:
        self.store = ClipStore(root)
        self.before, self.after, self.fps = before, after, fps
        self.max_width = max_width
        self.memory_limit, self.disk_limit = int(memory_mb * 1024**2), int(disk_mb * MIB)
        self.encoder = encoder
        self.buffer: deque[Sample] = deque()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.queue: queue.Queue[str | None] = queue.Queue(maxsize=4)
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.generation: tuple[int, tuple[int, ...]] | None = None
        self.last_sample = float("-inf")
        self.last_frame = float("-inf")
        self.last_poll = float("-inf")
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
                self.store.heartbeat(False)
            self.maintenance = paused

    def _bytes(self) -> int:
        return sum(len(s[2]) for s in self.buffer) + sum(
            sum(len(s[2]) for s in job["samples"]) for job in self.jobs.values()
        )

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
            height, width = frame.shape[:2]
            scale = min(1, self.max_width / width)
            size = (max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2))
            resized = cv2.resize(frame, size) if size != (width, height) else frame
            ok, encoded = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ok:
                return
            sample = (now, utcnow(), encoded.tobytes())
            self.buffer.append(sample)
            while self.buffer and (now - self.buffer[0][0] > self.before + 1e-6):
                self.buffer.popleft()
            for job in self.jobs.values():
                if (
                    job["status"] == "recording"
                    and not job.get("interrupted")
                    and job["trigger"] < now <= job["end"] + 1e-6
                ):
                    job["samples"].append(sample)
            while self.buffer and self._bytes() > self.memory_limit:
                self.buffer.popleft()
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
    ) -> str | None:
        """監視側から保存を要求する。同時処理数を超えたら None を返す。"""
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.maintenance:
                return None
            if source == "dog_exit" and event_id is None:
                for clip_id, job in reversed(self.jobs.items()):
                    if (
                        job["status"] == "recording"
                        and not job.get("interrupted")
                        and job["source"] == "dog_enter"
                        and 0 <= now - job["trigger"] <= SHORT_VISIT_SECONDS
                        and now <= job["end"]
                        and now - self.last_frame < 5
                    ):
                        job["end"] = now + self.after
                        job["source"] = "dog_visit"
                        return clip_id
            try:
                clip_id = self.store.request(source, event_id)
                self.poll(now=now, force=True)
                return clip_id
            except ValueError:
                logger.warning("動画保存の同時処理数を超えています")
                return None

    def poll(self, now: float | None = None, force: bool = False) -> None:
        """GUI からの保存要求を拾い、録画の終わった要求を変換に回す。"""
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.maintenance:
                return
            if not force and now - self.last_poll < 0.2:
                return
            self.last_poll = now
            self.store.heartbeat(now - self.last_frame < 5)
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
                if job["status"] == "recording" and (job.get("interrupted") or now >= job["end"]):
                    self._finish(clip_id)

    def _finish(self, clip_id: str, interrupted: bool = False) -> None:
        job = self.jobs[clip_id]
        samples = job["samples"]
        if not samples:
            self.store.update(clip_id, status="failed", error="保存するフレームがありません")
            del self.jobs[clip_id]
            return
        job["interrupted"] = bool(job.get("interrupted") or interrupted)
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
                interrupted=int(
                    job["interrupted"] or gaps or samples[-1][0] < job["end"] - 1 / self.fps
                ),
            )
        except sqlite3.Error:
            logger.warning("動画の変換開始をDBに保存できません。次回再試行します")
            return
        job["status"] = "encoding"
        self.queue.put_nowait(clip_id)

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
                    used = recording_usage(self.store.root)
                    if used >= self.disk_limit:
                        raise OSError("動画保存容量上限")
                    duration = self.encoder(job["samples"], temporary, self.fps)
                    if used + temporary.stat().st_size > self.disk_limit:
                        raise OSError("動画保存容量上限")
                    temporary.replace(path)
                    self.store.update(
                        clip_id, status="ready", path=f"clips/{path.name}", duration=duration
                    )
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
