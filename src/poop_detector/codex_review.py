"""録画のサンプル画像をCodexに渡し、判定候補を保存する。"""

import fcntl
import json
import logging
import math
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from .clips import ACTIVITIES, CLIP_LABELS
from .events import EXCRETION_TYPES, utcnow

logger = logging.getLogger(__name__)
EVIDENCE = {"feces": "new_stool", "urine": "new_wet_patch", "both": "both_visible"}
SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(CLIP_LABELS)},
        "activity": {"type": "string", "enum": list(ACTIVITIES)},
        "start_seconds": {"type": ["number", "null"]},
        "end_seconds": {"type": ["number", "null"]},
        "evidence": {"type": "string", "enum": [*EVIDENCE.values(), "none"]},
        "reason": {"type": "string"},
    },
    "required": ["label", "activity", "start_seconds", "end_seconds", "evidence", "reason"],
    "additionalProperties": False,
}
PROMPT = """犬のトイレの連続画像です。左上から行方向に時刻順で並びます。
各画像は登録済みのトイレ領域です。動画中に新たに発生した排泄だけを判定してください。
排便は前にはなかった便が現れたこと、排尿は尿の流れか新しい濡れ跡が発生したことが
画像で確認できる場合に限ります。以前からある跡、影、反射、シートのずれは除外してください。
通過、食事、匂い嗅ぎ、着座だけなら none、排泄らしい姿勢だけで跡や尿が見えない、
遮蔽やフレーム欠落で前後を比較できない、種類が分からない場合は uncertain にしてください。
排泄の場合は start_seconds に開始時刻の推定値または null を、
end_seconds に直後の根拠が見える画像の秒数を入れてください。他の場合は両方 null にしてください。
reason に時刻と画像で確認した根拠を日本語の短文で記載してください。
添付画像だけを見て、ツールを使わず指定のJSONを返してください。"""


@dataclass(frozen=True)
class Assessment:
    label: str
    activity: str
    start_seconds: float | None
    end_seconds: float | None
    reason: str


def parse_assessment(output, duration):
    if not isinstance(output, dict):
        raise ValueError("Codexの判定形式が不正です")
    label, activity = output.get("label"), output.get("activity")
    start, end, reason = (output.get(key) for key in ("start_seconds", "end_seconds", "reason"))
    if (
        label not in CLIP_LABELS
        or activity not in ACTIVITIES
        or output.get("evidence") != EVIDENCE.get(label, "none")
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        raise ValueError("Codexの判定に必要な根拠がありません")
    for offset in (start, end):
        if offset is not None and (type(offset) not in (int, float) or not 0 <= offset <= duration):
            raise ValueError("Codexの判定時刻が動画範囲外です")
    if label in EVIDENCE:
        if end is None or end <= 0 or (start is not None and start > end):
            raise ValueError("Codexの判定時刻が不正です")
    else:
        start = end = None
    return Assessment(label, activity, start, end, reason.strip()[:1000])


def write_sheet(path, destination, roi, duration):
    capture = cv2.VideoCapture(str(path))
    tiles = []
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if not math.isfinite(fps) or fps <= 0 or frame_count < 2:
            raise ValueError("動画からフレームを取得できません")
        count = min(frame_count, 24, max(2, math.ceil(duration / 2.5) + 1))
        for index in np.linspace(0, frame_count - 1, count, dtype=int):
            capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
            ok, frame = capture.read()
            if not ok:
                raise ValueError("動画の一部を読み込めません")
            crop = roi.crop(frame)
            scale = min(480 / crop.shape[1], 300 / crop.shape[0])
            tile = cv2.resize(crop, tuple(max(1, round(n * scale)) for n in crop.shape[1::-1]))
            tile = cv2.copyMakeBorder(tile, 26, 0, 0, 0, cv2.BORDER_CONSTANT)
            cv2.putText(tile, f"{index / fps:.2f}s", (6, 19), 0, 0.55, (255, 255, 255), 1)
            tiles.append(tile)
    finally:
        capture.release()
    columns = min(len(tiles), 4)
    tiles.extend(np.zeros_like(tiles[0]) for _ in range(-len(tiles) % columns))
    sheet = np.vstack(
        [np.hstack(tiles[index : index + columns]) for index in range(0, len(tiles), columns)]
    )
    if not cv2.imwrite(str(destination), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88]):
        raise OSError("Codex判定用画像を保存できません")


def codex_environment():
    keys = ("HOME", "PATH", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL")
    return {key: os.environ[key] for key in keys if key in os.environ}


def inspect_clip(path, row, roi, runner=subprocess.run):
    with tempfile.TemporaryDirectory(prefix="poop-codex-") as temporary:
        folder = Path(temporary)
        write_sheet(path, folder / "frames.jpg", roi, row["duration"])
        (folder / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
        result = folder / "result.json"
        runner(
            [
                "codex",
                "--ask-for-approval",
                "never",
                "exec",
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--ignore-rules",
                "--disable",
                "shell_tool",
                "--disable",
                "unified_exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--output-schema",
                "schema.json",
                "--output-last-message",
                "result.json",
                PROMPT,
                "--image",
                "frames.jpg",
            ],
            cwd=folder,
            env=codex_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=120,
        )
        return parse_assessment(json.loads(result.read_text(encoding="utf-8")), row["duration"])


def require_codex():
    if not shutil.which("codex"):
        raise ValueError("Codex CLIをインストールし、codex loginでログインしてください")
    try:
        result = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True,
            check=False,
            timeout=10,
            env=codex_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Codex CLIのログイン状態を確認できません") from None
    if result.returncode != 0:
        raise ValueError("codex loginでCodex CLIにログインしてください")


class Review:
    def __init__(self, store, inspect, notify=None, scene_signature=None):
        self.store = store
        self.inspect = inspect
        self.notify = notify
        self.scene_signature = scene_signature

    def run(self, limit, since=None, clip_id=None):
        with (self.store.root / ".codex.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if since is not None:
                    return 0, 0
                raise ValueError("別のCodex判定が実行中です。完了後に再試行してください") from None
            return self._run(limit, since, clip_id)

    def _run(self, limit, since, clip_id):
        completed = failures = 0
        rows = self.store.pending_assessments(limit, since, clip_id, self.scene_signature)
        for row in rows:
            try:
                path = self.store.video_path(row)
                if path is None:
                    raise OSError("動画ファイルがありません")
                result = self.inspect(path, row)
                observed_at = (
                    (
                        datetime.fromisoformat(row["start_at"])
                        + timedelta(seconds=result.end_seconds)
                    ).isoformat()
                    if result.label in EVIDENCE
                    else None
                )
            except (ValueError, TypeError, OSError, subprocess.SubprocessError, cv2.error):
                self.store.save_assessment(
                    row["id"],
                    error="Codex判定に失敗しました。動画・ログイン・接続を確認してください",
                )
                logger.warning("Codex判定に失敗しました（動画 %s）", row["id"])
                failures += 1
                continue
            stored = self.store.save_assessment(
                row["id"], **asdict(result), observed_at=observed_at
            )
            if not stored:
                continue
            completed += 1
            if (
                since is not None
                and self.notify is not None
                and result.label in EXCRETION_TYPES
                and not row["interrupted"]
                and self.store.claim_assessment_notice(row["id"])
            ):
                sent = self.notify(result.label)
                self.store.finish_assessment_notice(row["id"], sent)
        return completed, failures

    def watch(self):
        return ReviewWorker(self)


class ReviewWorker:
    def __init__(self, review):
        self.review = review
        self.since = utcnow()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._poll, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def _poll(self):
        while not self.stop.is_set():
            try:
                completed, failed = self.review.run(limit=1, since=self.since)
            except (OSError, sqlite3.Error):
                logger.warning("Codex判定の台帳にアクセスできません。次回再試行します")
                completed = failed = 0
            self.stop.wait(20 if failed else (0 if completed else 3))

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=1)
