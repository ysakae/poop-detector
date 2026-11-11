"""動画のラベルと、動画から排泄の記録を確定する処理。"""

import math
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from ..records.events import EXCRETION_LOCATIONS, EXCRETION_TYPES, EventRow, validate_location
from .clips import ACTIVITIES, CLIP_LABELS, ClipRow, ClipStore


@dataclass(frozen=True)
class ClipLabel:
    """人が動画に付けるラベル。start・end は動画内の秒数。"""

    label: str
    start: float | None = None
    end: float | None = None
    activity: str = "other"
    note: str = ""
    # None なら動画の既存の値を保ち、空文字なら消す。
    location: str | None = None


class ClipLabels:
    """clips のラベルの列と clip_confirmations テーブル（動画と確定記録のひも付け）。"""

    def __init__(self, clips: ClipStore) -> None:
        self.clips = clips
        self.store = clips.store
        self.connect = clips.connect

    def confirmation(self, clip_id: str) -> str | None:
        """動画から確定した記録の ID。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT event_id FROM clip_confirmations WHERE clip_id=?", (clip_id,)
            ).fetchone()
        return row["event_id"] if row else None

    # 以下では list メソッドと名前が重なるため、組み込みの list は builtins から参照する。
    def record_candidates(self, clip: ClipRow) -> list[EventRow]:
        """動画の時間帯の前後30秒に重なる、確定記録と検知イベント。"""
        start = datetime.fromisoformat(clip["start_at"] or clip["requested_at"])
        margin = timedelta(seconds=30)
        end = start + timedelta(seconds=clip["duration"] or 0)
        return [
            record
            for record in self.store.list()
            if record["id"] == clip["event_id"]
            or (
                (record["feedback"] == 1 or record["kind"] == "SUSPECTED")
                and datetime.fromisoformat(record["actual_start"] or record["timestamp"])
                <= end + margin
                and datetime.fromisoformat(
                    record["actual_end"] or record["actual_start"] or record["timestamp"]
                )
                >= start - margin
            )
        ]

    def record_videos(self) -> dict[str, list[str]]:
        """記録の ID ごとの、ひも付いた動画の ID。"""
        links: dict[str, list[str]] = {}
        with self.connect() as db:
            for row in db.execute(
                """SELECT c.id, COALESCE(k.event_id, c.event_id) AS record_id
                FROM clips c LEFT JOIN clip_confirmations k ON k.clip_id=c.id
                WHERE c.status='ready' AND c.path IS NOT NULL ORDER BY c.requested_at"""
            ):
                if row["record_id"]:
                    links.setdefault(row["record_id"], []).append(row["id"])
        return links

    def save(self, clip_id: str, clip_label: ClipLabel, *, record_id: str | None = None) -> None:
        """動画にラベルを付ける。record_id を渡すと排泄の記録としても確定する（"new" で新規）。"""
        label, start, end = clip_label.label, clip_label.start, clip_label.end
        activity, note, location = clip_label.activity, clip_label.note, clip_label.location
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
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
            if len(note) > 2000:
                raise ValueError("メモは2000文字以内です")
            validate_location(location)
            location = row["location"] if location is None else location or None
            if label in ("none", "uncertain"):
                start = end = location = None
            if record_id is not None:
                resolved = replace(clip_label, start=start, end=end, location=location)
                location = self._confirm(db, row, record_id, resolved)
            db.execute(
                """UPDATE clips SET label=?,actual_start_offset=?,actual_end_offset=?,
                activity=?,note=?,location=? WHERE id=?""",
                (label, start, end, activity, note, location, clip_id),
            )

    def _confirm(
        self, db: sqlite3.Connection, clip: sqlite3.Row, record_id: str, clip_label: ClipLabel
    ) -> str | None:
        """動画のラベルを排泄の記録として確定し、記録の場所を返す。record_id が "new" なら新規。"""
        if clip_label.label not in ("feces", "urine", "both"):
            raise ValueError("確定記録には排便・排尿・両方のいずれかを選んでください")
        record_id, linked = self._linked_record(db, clip, record_id)
        if clip_label.location == "outside" and record_id == clip["event_id"]:
            raise ValueError("トレー外の排泄は、トレー内の検知とは別の記録を選んでください")
        times = self._absolute_times(clip, clip_label)
        if record_id == "new":
            record_id = self._confirm_new(db, clip, clip_label, times)
            location = clip_label.location
        else:
            location = self._confirm_existing(db, record_id, clip_label, times)
        if not linked:
            db.execute("INSERT INTO clip_confirmations VALUES (?,?)", (clip["id"], record_id))
        return location

    @staticmethod
    def _linked_record(
        db: sqlite3.Connection, clip: sqlite3.Row, record_id: str
    ) -> tuple[str, bool]:
        """すでにひも付いた記録があれば、その ID を使う。(記録の ID, ひも付け済みか) を返す。"""
        link = db.execute(
            "SELECT event_id FROM clip_confirmations WHERE clip_id=?", (clip["id"],)
        ).fetchone()
        if link is None:
            return record_id, False
        if record_id not in ("new", link["event_id"]):
            raise ValueError("この動画はすでに別の記録にひも付いています")
        return link["event_id"], True

    @staticmethod
    def _absolute_times(
        clip: sqlite3.Row, clip_label: ClipLabel
    ) -> tuple[str, str | None, str | None]:
        """動画内の秒数を、(録画の開始, 排泄の開始, 排泄の完了) の UTC の時刻にする。"""
        start, end = clip_label.start, clip_label.end
        if not clip["start_at"] and (start is not None or end is not None):
            raise ValueError("録画の開始日時が分からないため、動画内の時刻を変換できません")
        captured = datetime.fromisoformat(clip["start_at"] or clip["requested_at"])
        start_at = (captured + timedelta(seconds=start)).isoformat() if start is not None else None
        end_at = (captured + timedelta(seconds=end)).isoformat() if end is not None else None
        return captured.isoformat(), start_at, end_at

    def _confirm_new(
        self,
        db: sqlite3.Connection,
        clip: sqlite3.Row,
        clip_label: ClipLabel,
        times: tuple[str, str | None, str | None],
    ) -> str:
        """動画から新しい確定記録を作る。トレー内の検知がある動画は、その検知を使わせる。"""
        if clip["event_id"] and clip_label.location != "outside":
            raise ValueError("対応する検知記録を保存先に選んでください")
        captured, start_at, end_at = times
        return self.store._insert_excretion(
            db,
            clip_label.label,
            "VIDEO",
            start_at or end_at or captured,
            start_at,
            end_at,
            clip_label.note,
            location=clip_label.location,
        )

    def _confirm_existing(
        self,
        db: sqlite3.Connection,
        record_id: str,
        clip_label: ClipLabel,
        times: tuple[str, str | None, str | None],
    ) -> str | None:
        """既存の記録に動画のラベルを合わせる。場所や種類が食い違えば ValueError。"""
        record = db.execute(
            """SELECT e.*, l.excretion_type, l.note FROM events e
            LEFT JOIN excretion_labels l ON l.event_id=e.id WHERE e.id=?""",
            (record_id,),
        ).fetchone()
        if record is None:
            raise ValueError("保存先の記録を選んでください")
        known_location = record["location"]
        if record["kind"] == "SUSPECTED" and known_location is None:
            known_location = "tray"
        location = clip_label.location
        if location and known_location and location != known_location:
            raise ValueError(f"保存先の場所は{EXCRETION_LOCATIONS[known_location]}です")
        location = location or known_location
        if record["feedback"] == 1:
            existing = record["excretion_type"] or "feces"
            if existing not in (clip_label.label, "both", "unspecified"):
                raise ValueError(f"確定記録の種類は{EXCRETION_TYPES[existing]}です")
            if location and record["location"] is None:
                db.execute("UPDATE events SET location=? WHERE id=?", (location, record_id))
            return location
        _, start_at, end_at = times
        self.store._classify(
            db,
            record_id,
            clip_label.label,
            start_at or record["actual_start"],
            end_at or record["actual_end"],
            clip_label.note or record["note"] or "",
            location=location,
        )
        return location
