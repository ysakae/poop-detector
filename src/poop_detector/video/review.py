"""保存した動画の自動判定。動画から並べた画像を判定器（VideoJudge）に渡し、結果を検証して保存する。

判定器は差し替えられる。既定の実装は Codex CLI（codex_cli.CodexCliJudge）。
"""

import fcntl
import logging
import math
import sqlite3
import subprocess
import tempfile
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self, cast

import cv2
import numpy as np

from ..detection.roi import AnyROI, MaskedROI, full_mask, outline
from ..errors import AlreadyRunningError
from ..features.tray_tracking import TrayTracker
from ..frames import Frame, Mask
from ..records.events import EXCRETION_LOCATIONS, EXCRETION_TYPES, utcnow
from .clip_reviews import Assessment
from .clip_storage import REVIEW_LOCK
from .clips import ACTIVITIES, CLIP_LABELS, ClipRow, ClipStore

logger = logging.getLogger(__name__)
EVIDENCE = {"feces": "new_stool", "urine": "new_wet_patch", "both": "both_visible"}
SCHEMA: dict[str, Any] = {
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
TRAY_CONTEXT = """犬のトイレの連続画像です。左上から行方向に時刻順で並びます。
各画像は登録済みのトイレ領域です。"""
EVIDENCE_PROMPT = """動画中に新たに発生した排泄だけを判定してください。
排便は前にはなかった便が現れたこと、排尿は尿の流れか新しい濡れ跡が発生したことが
画像で確認できる場合に限ります。以前からある跡、影、反射、シートのずれは除外してください。
赤外線の白黒映像では、色の代わりに、犬が離れた後にも同じ場所に残る新しい明暗や形の変化を
前後の画像で比較してください。便と濡れ跡が別々に確認できる場合は both にしてください。
通過、食事、匂い嗅ぎ、着座だけなら none、排泄らしい姿勢だけで跡や尿が見えない、
遮蔽やフレーム欠落で前後を比較できない、種類が分からない場合は uncertain にしてください。
排泄の場合は start_seconds に開始時刻の推定値または null を、
end_seconds に直後の根拠が見える画像の秒数を入れてください。他の場合は両方 null にしてください。
reason に時刻と画像で確認した根拠を日本語の短文で記載してください。
添付画像だけを見て、ツールを使わず指定のJSONを返してください。"""


OUTSIDE_SCHEMA = {
    **SCHEMA,
    "properties": {
        **SCHEMA["properties"],
        "location": {"type": ["string", "null"], "enum": [*EXCRETION_LOCATIONS, None]},
    },
    "required": [*SCHEMA["required"], "location"],
}
OUTSIDE_CONTEXT = """犬とトイレ周辺の連続画像です。左上から行方向に時刻順で並びます。
緑の枠はトレーの境界です。枠の外側がトレー外です。黒いマスク部分は監視範囲外です。
確認できた排泄物や尿の流れの位置を location に記入してください。緑の枠内は tray、
枠外は outside です。犬の体の位置で判断しないでください。排泄なし・判別不能の
場合は null とし、境界上で位置が分からない場合は uncertain を返してください。"""


def parse_assessment(
    output: object, duration: float, *, include_location: bool = False
) -> Assessment:
    """判定器の出力を検証する。根拠・時刻・位置が足りなければ ValueError。"""
    if not isinstance(output, dict):
        raise ValueError("自動判定の形式が不正です")
    label, activity = output.get("label"), output.get("activity")
    start, end, reason = (output.get(key) for key in ("start_seconds", "end_seconds", "reason"))
    if (
        label not in CLIP_LABELS
        or activity not in ACTIVITIES
        or output.get("evidence") != EVIDENCE.get(label, "none")
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        raise ValueError("自動判定に必要な根拠がありません")
    for offset in (start, end):
        if offset is not None and (type(offset) not in (int, float) or not 0 <= offset <= duration):
            raise ValueError("自動判定の時刻が動画範囲外です")
    location = (
        output.get("location") if include_location else ("tray" if label in EVIDENCE else None)
    )
    if include_location and (
        "location" not in output
        or (label in EVIDENCE and location not in EXCRETION_LOCATIONS)
        or (label not in EVIDENCE and location is not None)
    ):
        raise ValueError("自動判定の位置が不正です")
    if label in EVIDENCE:
        if end is None or end <= 0 or (start is not None and start > end):
            raise ValueError("自動判定の時刻が不正です")
    else:
        start = end = None
    return Assessment(label, activity, start, end, reason.strip()[:1000], location)


class TrayNotVisibleError(ValueError):
    """動画の中でトレーの位置を確かめられるフレームが足りない。"""


def save_sheet(destination: Path, tiles: Sequence[Frame], timestamps: Sequence[float]) -> None:
    """画像に秒数を書き入れ、4列のコンタクトシートとして保存する。"""
    labeled: list[Frame] = []
    for tile, timestamp in zip(tiles, timestamps, strict=True):
        framed = cv2.copyMakeBorder(tile, 26, 0, 0, 0, cv2.BORDER_CONSTANT)
        cv2.putText(framed, f"{timestamp:.2f}s", (6, 19), 0, 0.55, (255, 255, 255), 1)
        labeled.append(framed)
    columns = min(len(labeled), 4)
    labeled.extend(np.zeros_like(labeled[0]) for _ in range(-len(labeled) % columns))
    sheet = np.vstack(
        [np.hstack(labeled[index : index + columns]) for index in range(0, len(labeled), columns)]
    )
    if not cv2.imwrite(str(destination), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88]):
        raise OSError("自動判定用の画像を保存できません")


def save_difference_sheet(
    destination: Path, tiles: Sequence[Frame], timestamps: Sequence[float], mask: Mask
) -> None:
    """最初の3コマの中央値との差を強調した比較画像を保存する。"""
    # 網目の細かな模様をぼかし、ROI外との境界のにじみを除く。
    sigma = max(1, min(mask.shape) / 70)
    margin = math.ceil(2 * sigma)
    kernel = np.ones((2 * margin + 1, 2 * margin + 1), np.uint8)
    interior = cv2.erode(mask, kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
    if not np.any(interior):
        return
    smoothed = [
        cv2.GaussianBlur(cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY).astype(np.float32), (0, 0), sigma)
        for tile in tiles
    ]
    reference = np.median(smoothed[:3], axis=0)
    differences: list[Frame] = []
    for gray in smoothed:
        delta = gray - reference
        delta -= np.median(delta[interior])
        tile = np.clip(128 + 8 * delta, 0, 255).astype(np.uint8)
        tile[~interior] = 0
        differences.append(cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR))
    save_sheet(destination, differences, timestamps)


def write_sheet(
    path: Path,
    destination: Path,
    roi: AnyROI,
    duration: float,
    *,
    tray_tracker: TrayTracker | None = None,
    outside_roi: AnyROI | None = None,
    difference_path: Path | None = None,
) -> list[float]:
    """動画から最大24コマを取り出してシートにし、各コマの秒数を返す。"""
    capture = cv2.VideoCapture(str(path))
    tiles: list[Frame] = []
    timestamps: list[float] = []
    mask: Mask | None = None
    count = 0
    compare = difference_path is not None and tray_tracker is not None and outside_roi is None
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
            if tray_tracker is not None:
                if outside_roi is None:
                    aligned = tray_tracker.align(frame)
                else:
                    aligned = tray_tracker.align(
                        frame, source_mask=full_mask(outside_roi, frame.shape)
                    )
                if aligned is None:
                    continue
                frame = aligned
            if outside_roi is None:
                crop = roi.crop(frame)
            else:
                annotated = frame.copy()
                cv2.polylines(annotated, [outline(roi, frame.shape)], True, (0, 255, 0), 2)
                crop = outside_roi.crop(annotated)
            scale = min(480 / crop.shape[1], 300 / crop.shape[0])
            height, width = crop.shape[:2]
            tile = cv2.resize(crop, (max(1, round(width * scale)), max(1, round(height * scale))))
            if compare and mask is None:
                mask = np.full(tile.shape[:2], 255, np.uint8)
                if isinstance(roi, MaskedROI):
                    mask = cv2.resize(
                        roi.mask(frame.shape), tile.shape[1::-1], interpolation=cv2.INTER_NEAREST
                    )
            tiles.append(tile)
            timestamps.append(round(index / fps, 2))
    finally:
        capture.release()
    if tray_tracker is not None and len(tiles) < max(2, math.ceil(count * 2 / 3)):
        raise TrayNotVisibleError("位置を確認できるフレームが不足しています")
    save_sheet(destination, tiles, timestamps)
    if compare and len(tiles) >= 4 and difference_path is not None and mask is not None:
        save_difference_sheet(difference_path, tiles, timestamps, mask)
    return timestamps


def inspect_clip(
    path: Path,
    row: ClipRow,
    roi: AnyROI,
    judge: "VideoJudge | None" = None,
    *,
    tray_tracker: TrayTracker | None = None,
    outside_roi: AnyROI | None = None,
) -> Assessment:
    """動画をシートにして判定器に判定させ、結果を検証して返す。判定器の既定は Codex CLI。"""
    if judge is None:
        judge = default_judge()
    outside_stay = row.get("source") == "outside_stay"
    if outside_stay and outside_roi is None:
        raise ValueError("トレー外の動画には --detect-outside が必要です")
    with tempfile.TemporaryDirectory(prefix="poop-review-") as temporary:
        folder = Path(temporary)
        difference_path = folder / "differences.jpg"
        try:
            timestamps = write_sheet(
                path,
                folder / "frames.jpg",
                roi,
                row["duration"] or 0,
                tray_tracker=tray_tracker,
                outside_roi=outside_roi,
                difference_path=difference_path,
            )
        except TrayNotVisibleError:
            return Assessment(
                "uncertain", "other", None, None, "トレーの位置を確認できないため判定を保留しました"
            )
        context = TRAY_CONTEXT if outside_roi is None else OUTSIDE_CONTEXT
        prompt = context + "\n" + EVIDENCE_PROMPT
        images = ["frames.jpg"]
        if difference_path.is_file():
            images.append(difference_path.name)
            prompt += (
                "\n2枚目は同じ時刻・配置の比較画像です。最初の3コマの中央値と比べ、"
                "網目や画面全体の明るさの変化を抑えて、暗くなった箇所を強調しています。"
                "犬が離れたあとも同じ位置に残る小さな暗い跡を探し、元画像の前後と照合してください。"
                "比較画像だけで排尿とせず、犬の体、動く影、反射、位置ずれを除外してください。"
                "比較の基準に犬が映っている場合は、その場所の変化を元画像で確認してください。"
            )
        if outside_stay:
            prompt += "\n今回はトレー外の排泄だけを判定し、トレー内の排泄は対象外としてください。"
        if tray_tracker is not None or outside_roi is not None:
            prompt += "\nend_seconds には画像に表示された秒数をそのまま記入してください。"
        schema = SCHEMA if outside_roi is None else OUTSIDE_SCHEMA
        output = judge.judge(folder, prompt, images, schema)
        assessment = parse_assessment(
            output,
            row["duration"] or 0,
            include_location=outside_roi is not None,
        )
        if (
            (tray_tracker is not None or outside_roi is not None)
            and assessment.end_seconds is not None
            and round(assessment.end_seconds, 2) not in timestamps
        ):
            return Assessment(
                "uncertain",
                "other",
                None,
                None,
                "トレーの位置を確認できた画像に判定の根拠がありません",
            )
        if outside_stay and assessment.location == "tray":
            return Assessment("none", "other", None, None, "トレー外での排泄は確認できません")
        return assessment


class VideoJudge(Protocol):
    """動画から並べた画像を見て、JSON スキーマに沿った判定を返す判定器。

    別の判定器を使うときは、このプロトコルを満たすクラスを inspect_clip に渡す。
    """

    name: str

    def check(self) -> None:
        """使える状態か確かめる。使えなければ理由を書いた ValueError。"""
        ...

    def judge(
        self, folder: Path, prompt: str, images: Sequence[str], schema: dict[str, Any]
    ) -> object:
        """folder の images（ファイル名）と指示を渡し、判定の JSON を読み込んだ値を返す。"""
        ...


def default_judge() -> VideoJudge:
    """既定の判定器（Codex CLI）。"""
    from .codex_cli import CodexCliJudge

    return CodexCliJudge()


class AssessmentNotifier(Protocol):
    """自動判定を通知する関数（NtfyNotifier.send_assessment など）。"""

    def __call__(self, label: str, *, location: str | None = None) -> bool: ...


# 動画のパスと行から判定を返す関数。inspect_clip に ROI などを束ねたもの。
Inspector = Callable[[Path, ClipRow], Assessment]


class Review:
    """変換済みの動画を自動判定し、結果を保存して必要なら通知する。"""

    def __init__(
        self,
        store: ClipStore,
        inspect: Inspector,
        notify: AssessmentNotifier | None = None,
        scene_signature: str | None = None,
    ) -> None:
        self.store = store
        self.inspect = inspect
        self.notify = notify
        self.scene_signature = scene_signature

    def run(
        self, limit: int, since: str | None = None, clip_id: str | None = None
    ) -> tuple[int, int]:
        """未判定の動画を判定し、(判定した件数, 失敗した件数) を返す。

        since を指定した監視中の実行だけが通知し、別の判定が実行中なら何もしない。
        """
        with (self.store.root / REVIEW_LOCK).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if since is not None:
                    return 0, 0
                raise AlreadyRunningError(
                    "別の自動判定が実行中です。完了後に再試行してください"
                ) from None
            return self._run(limit, since, clip_id)

    def _run(self, limit: int, since: str | None, clip_id: str | None) -> tuple[int, int]:
        completed = failures = 0
        rows = self.store.assessments.pending(limit, since, clip_id, self.scene_signature)
        for row in rows:
            try:
                path = self.store.video_path(row)
                if path is None:
                    raise OSError("動画ファイルがありません")
                result = self.inspect(path, row)
                observed_at = (
                    (
                        datetime.fromisoformat(cast(str, row["start_at"]))
                        + timedelta(seconds=cast(float, result.end_seconds))
                    ).isoformat()
                    if result.label in EVIDENCE
                    else None
                )
            except (ValueError, TypeError, OSError, subprocess.SubprocessError, cv2.error):
                self.store.assessments.save(
                    row["id"],
                    error="自動判定に失敗しました。動画・判定器のログイン・接続を確認してください",
                )
                logger.warning("自動判定に失敗しました（動画 %s）", row["id"])
                failures += 1
                continue
            stored = self.store.assessments.save(row["id"], result, observed_at=observed_at)
            if not stored:
                continue
            completed += 1
            if (
                since is not None
                and self.notify is not None
                and result.label in EXCRETION_TYPES
                and not row["interrupted"]
                and self.store.assessments.claim_notice(row["id"])
            ):
                sent = (
                    self.notify(result.label, location="outside")
                    if result.location == "outside"
                    else self.notify(result.label)
                )
                self.store.assessments.finish_notice(row["id"], sent)
        return completed, failures

    def watch(self) -> "ReviewWorker":
        return ReviewWorker(self)


class ReviewWorker:
    """監視中に新しい動画を定期的に探し、別スレッドで Review を実行する。"""

    def __init__(self, review: Review) -> None:
        self.review = review
        self.since = utcnow()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._poll, daemon=True)

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def _poll(self) -> None:
        while not self.stop.is_set():
            try:
                completed, failed = self.review.run(limit=1, since=self.since)
            except (OSError, sqlite3.Error):
                logger.warning("自動判定の台帳にアクセスできません。次回再試行します")
                completed = failed = 0
            self.stop.wait(20 if failed else (0 if completed else 3))

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop.set()
        self.thread.join(timeout=1)
