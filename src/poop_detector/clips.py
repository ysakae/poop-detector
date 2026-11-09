"""上限付き動画バッファと、別プロセスのGUIから使える収集台帳。"""

import fcntl
import logging
import math
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from uuid import uuid4

import cv2

from .events import Store, utcnow

CLIP_LABELS = {
    "feces": "排便",
    "urine": "排尿",
    "both": "両方",
    "none": "排泄なし",
    "uncertain": "判断できない",
}
ACTIVITIES = {
    "other": "その他・未分類",
    "feeding": "食事",
    "sitting": "座る",
    "lying": "伏せる・寝る",
    "sniffing": "匂いを嗅ぐ",
    "scratching": "体を掻く",
    "walking": "歩く・通過する",
    "excreting": "排泄動作",
}
logger = logging.getLogger(__name__)


class ClipStore:
    def __init__(self, root):
        self.store = Store(root)
        self.root = Path(root)
        (self.root / "clips").mkdir(parents=True, exist_ok=True)
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY, event_id TEXT, source TEXT, requested_at TEXT,
                status TEXT, path TEXT, start_at TEXT, end_at TEXT, duration REAL,
                fps REAL, trigger_offset REAL, pre_available REAL, post_available REAL,
                interrupted INTEGER DEFAULT 0, error TEXT, label TEXT,
                actual_start_offset REAL, actual_end_offset REAL, activity TEXT DEFAULT 'other',
                note TEXT DEFAULT '', scene_signature TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS capture_status (
                id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL, healthy INTEGER)""")

            db.execute("""CREATE TABLE IF NOT EXISTS codex_assessments (
                clip_id TEXT PRIMARY KEY, label TEXT, activity TEXT, start_seconds REAL,
                end_seconds REAL, reason TEXT, observed_at TEXT, error TEXT,
                assessed_at TEXT NOT NULL, notice_claimed INTEGER DEFAULT 0, notified_at TEXT)""")

    def video_path(self, row):
        if not row or row["status"] != "ready" or not row["path"]:
            return None
        path = (self.root / row["path"]).resolve()
        if path.is_relative_to((self.root / "clips").resolve()) and path.is_file():
            return path
        return None

    def assessment(self, clip_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM codex_assessments WHERE clip_id=?", (clip_id,)
            ).fetchone()
        return dict(row) if row else None

    def pending_assessments(self, limit, since=None, clip_id=None, scene_signature=None):
        conditions = ["c.status='ready'", "c.label IS NULL"]
        params = []
        if since is not None:
            conditions.extend(["c.requested_at>=?", "a.clip_id IS NULL"])
            params.append(since)
        else:
            conditions.append("(a.clip_id IS NULL OR a.error IS NOT NULL)")
        if scene_signature is not None:
            conditions.append("c.scene_signature=?")
            params.append(scene_signature)
        if clip_id is not None:
            conditions.append("c.id=?")
            params.append(clip_id)
        order = "ASC" if since is not None else "DESC"
        with self.store.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    """SELECT c.* FROM clips c
                    LEFT JOIN codex_assessments a ON a.clip_id=c.id WHERE """
                    + " AND ".join(conditions)
                    + f" ORDER BY c.requested_at {order} LIMIT ?",
                    (*params, limit),
                )
            ]

    def save_assessment(
        self,
        clip_id,
        label=None,
        activity=None,
        start_seconds=None,
        end_seconds=None,
        reason=None,
        observed_at=None,
        error=None,
    ):
        with self.store.connect() as db:
            return (
                db.execute(
                    """INSERT INTO codex_assessments
                (clip_id,label,activity,start_seconds,end_seconds,reason,observed_at,error,assessed_at)
                SELECT id,?,?,?,?,?,?,?,? FROM clips WHERE id=? AND label IS NULL AND status='ready'
                ON CONFLICT(clip_id) DO UPDATE SET label=excluded.label,activity=excluded.activity,
                start_seconds=excluded.start_seconds,end_seconds=excluded.end_seconds,
                reason=excluded.reason,observed_at=excluded.observed_at,error=excluded.error,
                assessed_at=excluded.assessed_at WHERE codex_assessments.error IS NOT NULL""",
                    (
                        label,
                        activity,
                        start_seconds,
                        end_seconds,
                        reason,
                        observed_at,
                        error,
                        utcnow(),
                        clip_id,
                    ),
                ).rowcount
                == 1
            )

    def claim_assessment_notice(self, clip_id):
        with self.store.connect() as db:
            return (
                db.execute(
                    """UPDATE codex_assessments AS candidate SET notice_claimed=1
                WHERE candidate.clip_id=? AND candidate.notice_claimed=0
                AND EXISTS (
                    SELECT 1 FROM clips c WHERE c.id=candidate.clip_id AND c.label IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM codex_assessments a JOIN clips previous ON previous.id=a.clip_id
                        WHERE a.notice_claimed=1 AND (
                            (c.event_id IS NOT NULL AND c.event_id=previous.event_id)
                            OR (c.scene_signature IS previous.scene_signature
                                AND (a.label=candidate.label OR 'both' IN (a.label,candidate.label))
                                AND ABS(julianday(a.observed_at)-julianday(candidate.observed_at))
                                    < 5.001/86400)
                        )
                    )
                )""",
                    (clip_id,),
                ).rowcount
                == 1
            )

    def finish_assessment_notice(self, clip_id, sent):
        stamp = utcnow() if sent else None
        with self.store.connect() as db:
            db.execute(
                "UPDATE codex_assessments SET notice_claimed=?,notified_at=? WHERE clip_id=?",
                (int(sent), stamp, clip_id),
            )
            if sent:
                db.execute(
                    "UPDATE events SET notified_at=? WHERE id=(SELECT event_id FROM clips WHERE id=?)",
                    (stamp, clip_id),
                )

    def live(self):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM capture_status WHERE id=1").fetchone()
        return bool(row and row["healthy"] and time.time() - row["heartbeat"] < 10)

    def heartbeat(self, healthy):
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO capture_status VALUES (1,?,?)", (time.time(), int(healthy))
            )

    def request(self, source="manual", event_id=None, require_live=False):
        if source not in ("manual", "detected", "dog_enter", "dog_exit"):
            raise ValueError("保存要求の種類が不正です")
        if require_live and not self.live():
            raise ValueError("監視を動画保存有効で起動し、カメラ接続を確認してください")
        clip_id = uuid4().hex
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT count(*) FROM clips WHERE status IN ('queued','recording','encoding')"
            ).fetchone()[0]
            if pending >= 4:
                raise ValueError("保存処理中です。完了を待ってください")
            db.execute(
                """INSERT INTO clips(id,event_id,source,requested_at,status)
                VALUES (?,?,?,?,'queued')""",
                (clip_id, event_id, source, utcnow()),
            )
        return clip_id

    def list(self, include_assessments=False):
        query = "SELECT * FROM clips ORDER BY requested_at DESC"
        if include_assessments:
            query = """SELECT c.*, a.label AS codex_label, a.error AS codex_error
                FROM clips c LEFT JOIN codex_assessments a ON a.clip_id=c.id
                ORDER BY c.requested_at DESC"""
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(query)]

    def get(self, clip_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        return dict(row) if row else None

    def update(self, clip_id, **values):
        allowed = {
            "status",
            "path",
            "start_at",
            "end_at",
            "duration",
            "fps",
            "trigger_offset",
            "pre_available",
            "post_available",
            "interrupted",
            "error",
            "scene_signature",
        }
        if not values or not values.keys() <= allowed:
            raise ValueError("更新項目が不正です")
        with self.store.connect() as db:
            db.execute(
                "UPDATE clips SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                (*values.values(), clip_id),
            )

    def label(self, clip_id, label, start=None, end=None, activity="other", note=""):
        row = self.get(clip_id)
        if (
            not row
            or row["status"] != "ready"
            or label not in CLIP_LABELS
            or activity not in ACTIVITIES
        ):
            raise ValueError("保存済み動画とラベルを確認してください")
        for value in (start, end):
            if value is not None and (
                not math.isfinite(value) or not 0 <= value <= row["duration"]
            ):
                raise ValueError("開始・完了は動画内の秒数で指定してください")
        if start is not None and end is not None and end < start:
            raise ValueError("完了は開始以降を指定してください")
        if label in ("none", "uncertain"):
            start = end = None
        if len(note) > 2000:
            raise ValueError("メモは2000文字以内です")
        with self.store.connect() as db:
            db.execute(
                """UPDATE clips SET label=?,actual_start_offset=?,actual_end_offset=?,
                activity=?,note=? WHERE id=?""",
                (label, start, end, activity, note, clip_id),
            )


def encode_mp4(samples, path, fps):
    if not samples:
        raise ValueError("保存するフレームがありません")
    # カメラの揺れる取得間隔を一定FPSへ揃える。欠落区間は同じフレームで補間する。
    frames = []
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
        subprocess.run(
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
    def __init__(
        self,
        root,
        before=30,
        after=30,
        fps=5,
        max_width=1280,
        memory_mb=128,
        disk_mb=4096,
        encoder=encode_mp4,
    ):
        self.store = ClipStore(root)
        self.before, self.after, self.fps = before, after, fps
        self.max_width = max_width
        self.memory_limit, self.disk_limit = int(memory_mb * 1024**2), int(disk_mb * 1024**2)
        self.encoder = encoder
        self.buffer = deque()
        self.jobs = {}
        self.lock = threading.RLock()
        self.queue = queue.Queue(maxsize=4)
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.generation = None
        self.last_sample = float("-inf")
        self.last_frame = float("-inf")
        self.last_poll = float("-inf")
        self.scene_signature = None

    def __enter__(self):
        self.capture_lock = (self.store.root / ".capture.lock").open("a")
        try:
            fcntl.flock(self.capture_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.capture_lock.close()
            raise ValueError("別の動画監視が起動中です") from None
        with self.store.store.connect() as db:
            # 前回の異常終了で残った処理中行を新しい録画と混ぜない。
            db.execute(
                "UPDATE clips SET status='failed',error='前回の監視が終了したため保存できませんでした' WHERE status IN ('queued','recording','encoding')"
            )
        self.worker.start()
        return self

    def _bytes(self):
        return sum(len(s[2]) for s in self.buffer) + sum(
            sum(len(s[2]) for s in job["samples"]) for job in self.jobs.values()
        )

    def feed(self, frame, now=None, generation=0):
        now = time.monotonic() if now is None else now
        with self.lock:
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
                if job["status"] == "recording" and job["trigger"] < now <= job["end"] + 1e-6:
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

    def _interrupt(self):
        for clip_id, job in list(self.jobs.items()):
            if job["status"] == "recording":
                self._finish(clip_id, interrupted=True)
        self.buffer.clear()
        self.last_sample = self.last_frame = float("-inf")

    def trigger(self, event_id=None, source="detected"):
        try:
            clip_id = self.store.request(source, event_id)
            self.poll(force=True)
            return clip_id
        except ValueError:
            logger.warning("動画保存の同時処理数を超えています")
            return None

    def poll(self, now=None, force=False):
        now = time.monotonic() if now is None else now
        with self.lock:
            if not force and now - self.last_poll < 0.2:
                return
            self.last_poll = now
            self.store.heartbeat(now - self.last_frame < 5)
            for row in self.store.list():
                if row["status"] != "queued":
                    continue
                clip_id = row["id"]
                if not self.buffer or now - self.last_frame >= 5:
                    self.store.update(
                        clip_id, status="failed", error="カメラ映像を取得できていません"
                    )
                    continue
                samples = [s for s in self.buffer if now - s[0] <= self.before + 1e-6]
                self.jobs[clip_id] = {
                    "samples": samples,
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
                if job["status"] == "recording" and now >= job["end"]:
                    self._finish(clip_id)

    def _finish(self, clip_id, interrupted=False):
        job = self.jobs[clip_id]
        samples = job["samples"]
        if not samples:
            self.store.update(clip_id, status="failed", error="保存するフレームがありません")
            del self.jobs[clip_id]
            return
        job["interrupted"] = bool(job.get("interrupted") or interrupted)
        gaps = any(
            b[0] - a[0] > max(1, 3 / self.fps) for a, b in zip(samples, samples[1:], strict=False)
        )
        try:
            self.store.update(
                clip_id,
                status="encoding",
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

    def _worker(self):
        while True:
            clip_id = self.queue.get()
            try:
                if clip_id is None:
                    return
                job = self.jobs[clip_id]
                path = self.store.root / "clips" / f"{clip_id}.mp4"
                temporary = path.with_suffix(".part.mp4")
                try:
                    used = sum(p.stat().st_size for p in path.parent.glob("*.mp4"))
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

    def drain(self):
        self.queue.join()

    def __exit__(self, *args):
        with self.lock:
            self._interrupt()
        self.store.heartbeat(False)
        self.queue.put(None)
        self.worker.join()
        self.capture_lock.close()
