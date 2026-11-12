"""評価セット（判定器の比較に使う、人が確認した録画の一覧）を作り、確かめ、版を固定する。

作成（build）は台帳のラベル付き動画から下書きを作り、同じ出来事の録画を1本にまとめる。
人が下書きの tags と exclude を直したあと、固定（freeze）で版の番号と内容のハッシュを付ける。
固定した版は video-benchmark が読み、録画・ラベル・画角の登録情報が当時のままか確かめる。
評価セットは data/ の下に置き、映像そのものは入れない（録画の ID とラベルと条件だけ）。
"""

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from ..errors import InvalidInputError
from ..records.events import Store
from ..timezones import to_local
from .clips import ClipRow, ClipStore

FORMAT = 2
DATASETS_DIR = Path("benchmarks") / "datasets"
LABELS = ("feces", "urine", "both", "none")
SPLITS = ("tuning", "final")
SPLIT_NAMES = {"tuning": "調整用", "final": "最終評価用"}
# この秒数以内に重なる録画は、同じ出来事を撮った重複とみなす。
MERGE_SECONDS = 30
# 自動で付ける条件のタグ。
INTERRUPTED_TAG = "一部欠落"
OUTSIDE_TAG = "トレー外"
# 読む評価セットのファイルの大きさの上限。
FILE_LIMIT_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class Entry:
    """評価セットの録画1本。label は人が付けた正解、split は調整用か最終評価用か。"""

    id: str
    label: str
    event: str
    tags: tuple[str, ...]
    split: str
    date: str
    scene_signature: str | None
    source: str
    activity: str
    exclude: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "event": self.event,
            "tags": list(self.tags),
            "split": self.split,
            "date": self.date,
            "scene_signature": self.scene_signature,
            "source": self.source,
            "activity": self.activity,
            "exclude": self.exclude,
        }


@dataclass(frozen=True)
class Missed:
    """人が記録した見逃し。録画がないものは、録画に対する判定精度とは別に数える。"""

    total: int = 0
    with_recording: int = 0

    @property
    def without_recording(self) -> int:
        return self.total - self.with_recording


@dataclass(frozen=True)
class Dataset:
    """評価セット。version が None なら下書き（固定前）。"""

    name: str
    created_at: str
    entries: tuple[Entry, ...]
    scenes: Mapping[str, str] = field(default_factory=dict)
    final_since: str | None = None
    version: int | None = None
    duplicates: tuple[tuple[str, str], ...] = ()
    uncertain: tuple[str, ...] = ()
    unlabeled: int = 0
    missed: Missed = Missed()

    @property
    def included(self) -> tuple[Entry, ...]:
        return tuple(entry for entry in self.entries if not entry.exclude)

    @property
    def content_sha256(self) -> str:
        """正解と条件の内容のハッシュ。固定した版が書き換えられていないかの確認に使う。"""
        content = json.dumps(
            [entry.as_json() for entry in self.included], sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(content.encode()).hexdigest()

    def as_json(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "format": FORMAT,
            "name": self.name,
            "version": self.version,
            "created_at": self.created_at,
            "final_since": self.final_since,
            "scenes": dict(self.scenes),
            "clips": [entry.as_json() for entry in self.entries],
            "duplicates": [{"id": clip, "of": kept} for clip, kept in self.duplicates],
            "uncertain": list(self.uncertain),
            "unlabeled": self.unlabeled,
            "missed": {"total": self.missed.total, "with_recording": self.missed.with_recording},
        }
        if self.version is not None:
            report["content_sha256"] = self.content_sha256
        return report


@dataclass(frozen=True)
class Problems:
    """評価の前に直すべきこと。値はどれも録画の ID か画角の署名の一覧。"""

    missing_videos: tuple[str, ...] = ()
    changed_labels: tuple[str, ...] = ()
    missing_scenes: tuple[str, ...] = ()
    duplicate_events: tuple[str, ...] = ()
    unprotected: tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        """評価や固定を止める問題があるか。保護していないだけなら止めない。"""
        return bool(
            self.missing_videos
            or self.changed_labels
            or self.missing_scenes
            or self.duplicate_events
        )

    def lines(self) -> list[str]:
        counts = (
            (self.missing_videos, "録画が見つからない"),
            (self.changed_labels, "固定後に台帳のラベルが変わった"),
            (self.missing_scenes, "当時の ROI・基準画像が見つからない画角"),
            (self.duplicate_events, "同じイベントが重複している"),
            (self.unprotected, "自動整理から保護していない"),
        )
        return [f"{name}: {len(values)}件" for values, name in counts if values]


# --- 作成 ---------------------------------------------------------------------------------


def _interval(row: ClipRow) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(row["start_at"] or row["requested_at"])
    return start, start + timedelta(seconds=row["duration"] or 0)


def _groups(rows: Sequence[ClipRow], confirmations: Mapping[str, str]) -> list[list[ClipRow]]:
    """同じイベント・同じ確定記録・時間の重なる録画を、1つの出来事にまとめる。"""
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    keys: dict[str, int] = {}
    for index, row in enumerate(rows):
        for key in (row["event_id"], confirmations.get(row["id"])):
            if key is not None:
                parent[find(index)] = find(keys.setdefault(key, index))
    order = sorted(range(len(rows)), key=lambda index: _interval(rows[index])[0])
    margin = timedelta(seconds=MERGE_SECONDS)
    latest_end, latest = None, None
    for index in order:
        start, end = _interval(rows[index])
        if latest_end is not None and latest is not None and start <= latest_end + margin:
            parent[find(index)] = find(latest)
        if latest_end is None or end > latest_end:
            latest_end, latest = end, index
    grouped: dict[int, list[ClipRow]] = {}
    for index in order:
        grouped.setdefault(find(index), []).append(rows[index])
    return list(grouped.values())


def _representative(group: Sequence[ClipRow]) -> ClipRow:
    """出来事を代表する録画。排泄を確認したもの、排泄なし、判定不能の順に、長いものを選ぶ。"""
    rank = {"feces": 0, "urine": 0, "both": 0, "none": 1}
    return min(
        group,
        key=lambda row: (rank.get(row["label"] or "", 2), -(row["duration"] or 0), row["id"]),
    )


def _auto_tags(row: ClipRow) -> set[str]:
    tags = set()
    if row["interrupted"]:
        tags.add(INTERRUPTED_TAG)
    if row["location"] == "outside":
        tags.add(OUTSIDE_TAG)
    return tags


def _carried(base: object) -> dict[str, dict[str, Any]]:
    """既存の評価セット（手書きの JSON か下書き）から、録画ごとの event・tags・exclude を読む。"""
    entries = base.get("clips") if isinstance(base, dict) else None
    if not isinstance(entries, list):
        raise InvalidInputError("引き継ぐ評価セットの clips を確認してください")
    carried = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise InvalidInputError("引き継ぐ評価セットに不正な録画があります")
        carried[entry["id"]] = {
            "event": entry.get("event"),
            "tags": _tags(entry.get("tags", [])),
            "exclude": entry.get("exclude", False) is True,
        }
    return carried


def _tags(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(tag, str) or not tag.strip() for tag in value
    ):
        raise InvalidInputError("評価セットのタグは空でない文字列の一覧にしてください")
    return tuple(tag.strip() for tag in value)


def _in_range(day: date, since: date | None, until: date | None) -> bool:
    return (since is None or day >= since) and (until is None or day <= until)


def missed_counts(
    events: Store, rows: Iterable[ClipRow], since: date | None = None, until: date | None = None
) -> Missed:
    """見逃しの記録のうち、前後30秒に録画があるものとないものを数える。"""
    intervals = [_interval(row) for row in rows]
    margin = timedelta(seconds=MERGE_SECONDS)
    total = with_recording = 0
    for record in events.list():
        if record["kind"] != "MISSED":
            continue
        start = datetime.fromisoformat(record["actual_start"] or record["timestamp"])
        end = datetime.fromisoformat(record["actual_end"] or start.isoformat())
        if not _in_range(to_local(start.isoformat()).date(), since, until):
            continue
        total += 1
        if any(begin <= end + margin and start <= finish + margin for begin, finish in intervals):
            with_recording += 1
    return Missed(total, with_recording)


def _confirmations(store: ClipStore) -> dict[str, str]:
    with store.connect() as db:
        return {
            row["clip_id"]: row["event_id"]
            for row in db.execute("SELECT clip_id, event_id FROM clip_confirmations")
        }


def build(
    store: ClipStore,
    *,
    name: str,
    created_at: str,
    scenes: Mapping[str, str],
    final_since: date | None = None,
    since: date | None = None,
    until: date | None = None,
    base: object = None,
) -> Dataset:
    """台帳の保存済み動画から下書きを作る。判定器の判定は正解に使わない。"""
    carried = {} if base is None else _carried(base)
    ready = [
        row
        for row in (cast(ClipRow, item) for item in store.list())
        if store.video_path(row) is not None
        and _in_range(to_local(_interval(row)[0].isoformat()).date(), since, until)
    ]
    labeled = [row for row in ready if row["label"] in (*LABELS, "uncertain")]
    entries: list[Entry] = []
    duplicates: list[tuple[str, str]] = []
    uncertain: list[str] = []
    for group in _groups(labeled, _confirmations(store)):
        kept = _representative(group)
        duplicates.extend((row["id"], kept["id"]) for row in group if row is not kept)
        if kept["label"] not in LABELS:
            uncertain.append(kept["id"])
            continue
        entries.append(_entry(kept, carried.get(kept["id"], {}), final_since))
    return Dataset(
        name=name,
        created_at=created_at,
        entries=tuple(entries),
        scenes=dict(scenes),
        final_since=None if final_since is None else final_since.isoformat(),
        duplicates=tuple(duplicates),
        uncertain=tuple(uncertain),
        unlabeled=len(ready) - len(labeled),
        missed=missed_counts(store.store, ready, since, until),
    )


def _entry(row: ClipRow, carried: Mapping[str, Any], final_since: date | None) -> Entry:
    day = to_local(_interval(row)[0].isoformat()).date()
    split = "final" if final_since is not None and day >= final_since else "tuning"
    event = carried.get("event") or row["event_id"] or f"clip:{row['id']}"
    tags = {*carried.get("tags", ()), *_auto_tags(row)}
    return Entry(
        id=row["id"],
        label=str(row["label"]),
        event=str(event),
        tags=tuple(sorted(tags)),
        split=split,
        date=day.isoformat(),
        scene_signature=row["scene_signature"],
        source=row["source"],
        activity=row["activity"],
        exclude=bool(carried.get("exclude", False)),
    )


# --- 読み込み・確認・固定 -------------------------------------------------------------------


def is_dataset(manifest: object) -> bool:
    """この形式（版2）の評価セットか。違えば video-benchmark の手書きの JSON として扱う。"""
    return isinstance(manifest, dict) and manifest.get("format") == FORMAT


def _string(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError
    return value


def _parse_entry(value: object) -> Entry:
    if not isinstance(value, dict):
        raise ValueError
    label, split = value["label"], value["split"]
    if label not in LABELS or split not in SPLITS or not isinstance(value["exclude"], bool):
        raise ValueError
    date.fromisoformat(str(value["date"]))
    return Entry(
        id=str(_string(value["id"])),
        label=label,
        event=str(_string(value["event"])),
        tags=_tags(value["tags"]),
        split=split,
        date=str(value["date"]),
        scene_signature=_string(value["scene_signature"], optional=True),
        source=str(_string(value["source"])),
        activity=str(_string(value["activity"])),
        exclude=value["exclude"],
    )


def _parse_counts(report: Mapping[str, Any]) -> tuple[Missed, int]:
    missed = report["missed"]
    total, with_recording, unlabeled = (
        missed["total"],
        missed["with_recording"],
        report["unlabeled"],
    )
    numbers = (total, with_recording, unlabeled)
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in numbers):
        raise ValueError
    if with_recording > total:
        raise ValueError
    return Missed(total, with_recording), unlabeled


def parse(report: object) -> Dataset:
    """評価セットの JSON を読む。形が違うか、固定した版の内容が書き換えられていれば拒否する。"""
    try:
        if not is_dataset(report) or not isinstance(report, dict):
            raise ValueError
        version = report["version"]
        if version is not None and (
            isinstance(version, bool) or not isinstance(version, int) or version < 1
        ):
            raise ValueError
        scenes = report["scenes"]
        if not isinstance(scenes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in scenes.items()
        ):
            raise ValueError
        missed, unlabeled = _parse_counts(report)
        dataset = Dataset(
            name=str(_string(report["name"])),
            created_at=str(_string(report["created_at"])),
            entries=tuple(_parse_entry(entry) for entry in report["clips"]),
            scenes=scenes,
            final_since=_string(report["final_since"], optional=True),
            version=version,
            duplicates=tuple((str(d["id"]), str(d["of"])) for d in report["duplicates"]),
            uncertain=tuple(str(clip) for clip in report["uncertain"]),
            unlabeled=unlabeled,
            missed=missed,
        )
    except (KeyError, TypeError, ValueError):
        raise InvalidInputError("評価セットの形式を確認してください") from None
    if len({entry.id for entry in dataset.entries}) != len(dataset.entries):
        raise InvalidInputError("評価セットに重複した録画があります")
    if version is not None and report.get("content_sha256") != dataset.content_sha256:
        raise InvalidInputError(
            "固定した評価セットの内容が変わっています。下書きを直して固定し直してください"
        )
    return dataset


def read(path: Path) -> Dataset:
    if path.stat().st_size > FILE_LIMIT_BYTES:
        raise InvalidInputError("評価セットのファイルが大きすぎます")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise InvalidInputError("評価セットの JSON を読めません") from None
    return parse(report)


def inspect(
    dataset: Dataset, store: ClipStore, scene_signatures: Mapping[str, str | None]
) -> Problems:
    """録画・ラベル・画角の登録情報が評価に使える状態か確かめる。

    scene_signatures は、評価セットの scenes の各署名に対し、その場所から読めた画角の署名
    （読めなければ None）。
    """
    from .clip_storage import is_protected

    missing, changed, unprotected = [], [], []
    for entry in dataset.included:
        row = store.get(entry.id)
        if row is None or store.video_path(row) is None:
            missing.append(entry.id)
            continue
        if row["label"] != entry.label:
            changed.append(entry.id)
        if not is_protected(store, entry.id):
            unprotected.append(entry.id)
    signatures = {entry.scene_signature for entry in dataset.included}
    missing_scenes = sorted(
        str(signature)
        for signature in signatures
        if signature is None or scene_signatures.get(signature) != signature
    )
    events = Counter(entry.event for entry in dataset.included)
    return Problems(
        missing_videos=tuple(missing),
        changed_labels=tuple(changed),
        missing_scenes=tuple(missing_scenes),
        duplicate_events=tuple(sorted(event for event, count in events.items() if count > 1)),
        unprotected=tuple(unprotected),
    )


def freeze(dataset: Dataset, version: int) -> Dataset:
    """除外した録画を落とし、版の番号を付ける。内容のハッシュは書き出すときに付く。"""
    if version < 1:
        raise InvalidInputError("--version は1以上で指定してください")
    if not dataset.included:
        raise InvalidInputError("評価できる録画がありません")
    return replace(dataset, entries=dataset.included, version=version)


def write_new(path: Path, dataset: Dataset) -> None:
    """新しいファイルに書く。既にあれば上書きせずに断る。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as file:
            json.dump(dataset.as_json(), file, ensure_ascii=False, indent=2)
            file.write("\n")
    except FileExistsError:
        raise InvalidInputError("出力先のファイルが既にあります") from None


# --- 件数 ---------------------------------------------------------------------------------


def coverage(dataset: Dataset) -> dict[str, Any]:
    """種類・条件ごとの件数。追加の収集が必要な場面を見るためのもの。"""
    included = dataset.included
    by_split = {
        split: {
            label: sum(e.split == split and e.label == label for e in included) for label in LABELS
        }
        for split in SPLITS
    }
    tags = sorted({tag for entry in included for tag in entry.tags})
    by_tag = {
        tag: {label: sum(tag in e.tags and e.label == label for e in included) for label in LABELS}
        for tag in tags
    }
    activities = Counter(entry.activity for entry in included if entry.label == "none")
    return {
        "cases": len(included),
        "by_split": by_split,
        "by_tag": by_tag,
        "none_activities": dict(sorted(activities.items())),
        "excluded": len(dataset.entries) - len(included),
        "duplicates": len(dataset.duplicates),
        "uncertain": len(dataset.uncertain),
        "unlabeled": dataset.unlabeled,
        "missed": {
            "total": dataset.missed.total,
            "with_recording": dataset.missed.with_recording,
            "without_recording": dataset.missed.without_recording,
        },
    }


def datasets_dir(root: str | Path) -> Path:
    return Path(root) / DATASETS_DIR


def load_datasets(root: str | Path, limit: int = 10) -> tuple[list[tuple[str, Dataset]], int]:
    """保存先の評価セット（新しい順）と、読めなかったファイルの数。"""
    folder = datasets_dir(root)
    if not folder.is_dir():
        return [], 0
    files = sorted(
        (path for path in folder.glob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:limit]
    found, unreadable = [], 0
    for path in files:
        try:
            found.append((path.name, read(path)))
        except (OSError, InvalidInputError):
            unreadable += 1
    return found, unreadable
