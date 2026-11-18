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
from .clip_storage import DEFAULT_DISK_MIB, MIB, free_space, over_limit, recording_usage
from .clips import ClipStore, RequestSource
from .thumbnails import ensure_thumbnail

logger = logging.getLogger(__name__)
# 録画中に届いた別の保存要求は、新しい動画を作らずにその録画を延ばして1本にまとめる
# （犬の入場・トレー外の滞在・見守り通知が数秒おきに重なり、同じ場面の動画が何本もできるため）。
# 最初の要求からこの秒数を過ぎた要求はまとめず、動画1本の長さとメモリーの使用量を抑える。
MAX_EXTENSION_SECONDS = 90
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
Encoder: TypeAlias = Callable[[Sequence[Sample], Path, float], float]


def encode_frame(frame: Frame, max_width: int) -> bytes | None:
    """録画用に幅を制限し、MP4 に使える偶数寸法の JPEG を返す。"""
    height, width = frame.shape[:2]
    scale = min(1, max_width / width)
    size = (max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2))
    resized = cv2.resize(frame, size) if size != (width, height) else frame
    ok, encoded = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return encoded.tobytes() if ok else None


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
            jpeg = encode_frame(frame, self.max_width)
            if jpeg is None:
                return
            sample = (now, utcnow(), jpeg)
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
            # 検知のイベントにひも付く動画は、検知の時刻を基準にするので別に録る。
            merged = None if event_id is not None else self._extend(source, now)
            if merged is not None:
                return merged
            try:
                clip_id = self.store.request(source, event_id)
                self.poll(now=now, force=True)
                return clip_id
            except ValueError:
                logger.warning("動画保存の同時処理数を超えています")
                return None

    def _extend(self, source: RequestSource, now: float) -> str | None:
        """録画中の動画を延ばして要求をまとめ、その ID を返す。まとめられなければ None。"""
        if now - self.last_frame >= 5:
            return None
        for clip_id, job in reversed(self.jobs.items()):
            if (
                job["status"] == "recording"
                and not job.get("interrupted")
                and 0 <= now - job["trigger"] <= MAX_EXTENSION_SECONDS
                and now <= job["end"]
            ):
                job["end"] = max(job["end"], now + self.after)
                job["source"] = merged_source(job["source"], source)
                return clip_id
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
