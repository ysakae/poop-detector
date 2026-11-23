"""外出（や夜）の区間の出来事を台帳から集め、時刻の順の年表にする。

画面の表示（アイコンやリンク）は持たず、種類（kind）と台帳の ID（ref）だけを返す。
GUI のまとめの画面と、判定器による要約の作業（features.digest_summary）が使う。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeAlias

from ..records.absences import Absences
from ..records.events import EXCRETION_TYPES, Store
from ..records.floor_objects import FloorObjects
from ..records.vocal_episodes import VocalEpisodes
from ..video.clips import ClipStore
from .sleep_store import SleepStore

EntryKind: TypeAlias = Literal["record", "detection", "clip", "absence", "vocal", "floor"]


@dataclass(frozen=True)
class TimelineEntry:
    """年表の1行。ref は種類ごとの台帳の ID（記録・動画・床の候補）。"""

    at: datetime
    kind: EntryKind
    text: str
    ref: str = ""


@dataclass(frozen=True)
class DigestContent:
    """区間の出来事。asleep_minutes は睡眠の記録があるときだけ。"""

    start: datetime
    end: datetime
    entries: list[TimelineEntry]
    asleep_minutes: int | None


def _within(stamp: str | None, start: datetime, end: datetime) -> datetime | None:
    if not stamp:
        return None
    at = datetime.fromisoformat(stamp)
    return at if start <= at < end else None


def _events(store: Store, start: datetime, end: datetime) -> list[TimelineEntry]:
    entries = []
    for row in store.list():
        at = _within(row["actual_start"] or row["timestamp"], start, end)
        if at is None:
            continue
        if row["feedback"] == 1 and row["excretion_type"] in EXCRETION_TYPES:
            text = f"{EXCRETION_TYPES[row['excretion_type']]}の記録"
            entries.append(TimelineEntry(at, "record", text, row["id"]))
        elif row["kind"] == "SUSPECTED":
            entries.append(TimelineEntry(at, "detection", "トイレの変化を検知", row["id"]))
    return entries


def _clips(
    clips: ClipStore, start: datetime, end: datetime, labels: Mapping[str, str]
) -> list[TimelineEntry]:
    entries = []
    for row in clips.list():
        at = _within(row["requested_at"], start, end)
        if at is None or row["status"] != "ready":
            continue
        text = f"動画（{labels.get(row['source'], row['source'])}）"
        entries.append(TimelineEntry(at, "clip", text, row["id"]))
    return entries


def _others(store: Store, root: Path, start: datetime, end: datetime) -> list[TimelineEntry]:
    entries = []
    for absence in Absences(store.database).list(200):
        if start <= absence.started_at < end:
            length = (
                f"約{round(absence.minutes)}分" if absence.minutes is not None else "続いている"
            )
            entries.append(
                TimelineEntry(absence.started_at, "absence", f"サークルにいない（{length}）")
            )
    for vocal in VocalEpisodes(store.database).between(start, end):
        text = f"鳴き声（{round(vocal.loud_seconds)}秒）"
        entries.append(TimelineEntry(vocal.started_at, "vocal", text))
    for found in FloorObjects(store.database, root).list(200):
        at = _within(found.detected_at, start, end)
        if at is not None:
            entries.append(TimelineEntry(at, "floor", "床に見慣れない物", found.id))
    return entries


def _asleep(store: Store, start: datetime, end: datetime) -> int | None:
    records = SleepStore(store.database).minutes(start, end)
    observed = [record for record in records if record.state != "unknown"]
    if not observed:
        return None
    return sum(record.state == "asleep" for record in observed)


def collect(
    store: Store,
    clips: ClipStore,
    root: Path,
    start: datetime,
    end: datetime,
    clip_labels: Mapping[str, str],
) -> DigestContent:
    """start から end までの出来事を、時刻の順に並べる。"""
    entries = (
        _events(store, start, end)
        + _clips(clips, start, end, clip_labels)
        + _others(store, root, start, end)
    )
    entries.sort(key=lambda entry: entry.at)
    return DigestContent(start, end, entries, _asleep(store, start, end))
