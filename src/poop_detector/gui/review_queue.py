"""確認待ち（まだラベルのない検知イベントと、排泄に関わる動画）を1つの列にまとめる。

動画のある検知は、動画で確かめる1件にまとめる。どちらかにラベルが付けば、その件は終わり。
動画で「排泄なし」とすると、ひも付く検知も誤検知になる（video.clip_labels.ClipLabels.save）。
確認待ちから外した動画（video.review_dismissals）と、その動画にまとめた検知は並べない。
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode

if TYPE_CHECKING:
    from ..records.events import Store
    from ..video.clips import ClipStore

# 排泄に関わる動画の発生元。犬の出入りや滞在の動画は、自動判定が排泄の候補としたものだけを入れる。
REVIEW_SOURCES = frozenset({"detected", "manual"})
EXCRETION_LABELS = frozenset({"feces", "urine", "both"})
NEXT_PAGE = "/review/next"
UNKNOWN_SCENE = "unknown"


@dataclass(frozen=True)
class ReviewItem:
    """確認待ちの1件。at は UTC の ISO 8601。event_id は動画にまとめた検知。

    review_label・location は自動判定の候補（動画だけ）、image は検知の画像があるか（検知だけ）。
    scene は撮影時の画角の署名（動画だけ。記録のない動画は None）。
    """

    kind: Literal["event", "clip"]
    id: str
    at: str
    source: str
    review_label: str | None = None
    event_id: str | None = None
    location: str | None = None
    image: bool = False
    review_failed: bool = False
    scene: str | None = None

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"

    @property
    def url(self) -> str:
        """ラベルを付ける画面。from=review で、保存後に次の件へ進む。"""
        if self.kind == "clip":
            return "/clips?" + urlencode({"id": self.id, "from": "review"})
        return "/records/events?" + urlencode({"id": self.id, "from": "review"})


def _optional(row: Mapping[str, Any], key: str) -> Any:
    """行にあれば、その列の値（自動判定を付けずに読んだ行には、判定の列がない）。"""
    return row[key] if key in row.keys() else None  # noqa: SIM118 sqlite3.Row は in で列名を調べられない


def _unlabeled_clip(clip: Mapping[str, Any]) -> bool:
    return clip["status"] == "ready" and clip["label"] is None


def _dismissed(clip: Mapping[str, Any]) -> bool:
    return bool(_optional(clip, "review_dismissed"))


def _waiting_clip(clip: Mapping[str, Any]) -> bool:
    return _unlabeled_clip(clip) and not _dismissed(clip)


def pending_items(
    events: Iterable[Mapping[str, Any]], clips: Iterable[Mapping[str, Any]]
) -> list[ReviewItem]:
    """確認待ちを新しい順に返す。events は Store.list、clips は ClipStore.list の行。"""
    events = list(events)
    clips = list(clips)
    unlabeled = {
        row["id"] for row in events if row["kind"] == "SUSPECTED" and row["feedback"] is None
    }
    known = {row["id"] for row in events}
    # 動画のどれかにラベルが付いた検知は、終わったものとして扱う。
    # 確認待ちから外した動画の検知も、画像で確かめる件として戻さない。
    finished = {clip["event_id"] for clip in clips if clip["label"] is not None or _dismissed(clip)}
    items: list[ReviewItem] = []
    represented: set[str] = set()
    for clip in sorted(clips, key=lambda row: row["requested_at"]):
        if not _waiting_clip(clip):
            continue
        event_id = clip["event_id"]
        if event_id in known:
            if event_id not in unlabeled or event_id in finished or event_id in represented:
                continue
            represented.add(event_id)
        elif not (clip["source"] in REVIEW_SOURCES or clip["review_label"] in EXCRETION_LABELS):
            continue
        items.append(
            ReviewItem(
                "clip",
                clip["id"],
                clip["requested_at"],
                clip["source"],
                clip["review_label"],
                event_id if event_id in known else None,
                _optional(clip, "review_location"),
                review_failed=bool(_optional(clip, "review_error")),
                scene=_optional(clip, "scene_signature"),
            )
        )
    items += [
        ReviewItem(
            "event", row["id"], row["timestamp"], "event", image=bool(_optional(row, "image"))
        )
        for row in events
        if row["id"] in unlabeled and row["id"] not in represented | finished
    ]
    return sorted(items, key=lambda item: item.at, reverse=True)


def next_item(
    items: Sequence[ReviewItem], after: str | None, at: str | None = None
) -> ReviewItem | None:
    """after の次の件。最後の後は None。

    after が列にない（保存して列から消えた）ときは、その時刻 at より古い件へ進む。
    at も分からなければ先頭から始める。
    """
    keys = [item.key for item in items]
    if after in keys:
        index = keys.index(after) + 1
        return items[index] if index < len(items) else None
    if at is not None:
        return next((item for item in items if item.at < at), None)
    return items[0] if items else None


def item_time(store: "Store", clips: "ClipStore", key: str) -> str | None:
    """key（event:ID・clip:ID）の件の時刻。見つからなければ None。"""
    kind, _, item_id = key.partition(":")
    if kind == "clip":
        clip = clips.get(item_id)
        return clip["requested_at"] if clip else None
    if kind == "event":
        return next((row["timestamp"] for row in store.list() if row["id"] == item_id), None)
    return None


def review_items(store: "Store", clips: "ClipStore") -> list[ReviewItem]:
    return pending_items(store.list(), clips.list(include_assessments=True))


def review_bar(items: Sequence[ReviewItem], key: str) -> dict[str, Any]:
    """確認待ちから開いた画面の上に出す、残りの件数と「飛ばす」。"""
    return {"remaining": len(items), "skip_url": f"{NEXT_PAGE}?after={key}"}


@dataclass(frozen=True)
class SceneGroup:
    """確認待ちの動画を撮影時の画角でまとめた1組。

    clip_ids は確認待ちの動画、dismissed は確認待ちから外したラベルのない動画の数。
    first・last は確認待ちの動画（残っていなければ外した動画）の最も古い・新しい時刻（UTC）、
    thumbnail はそのうち最も新しい動画。
    current は、いちばん新しい変換済みの動画と同じ画角か。
    """

    scene: str | None
    clip_ids: tuple[str, ...]
    dismissed: int
    first: str | None
    last: str | None
    thumbnail: str | None
    current: bool

    @property
    def waiting(self) -> int:
        return len(self.clip_ids)


def scene_groups(
    items: Sequence[ReviewItem], clips: Iterable[Mapping[str, Any]]
) -> list[SceneGroup]:
    """確認待ちの動画と、外した動画を画角ごとに数える。新しい画角から順に並べる。

    items は pending_items の結果、clips は ClipStore.list(include_assessments=True) の行。
    """
    ready = [clip for clip in clips if clip["status"] == "ready"]
    newest = max(ready, key=lambda clip: clip["requested_at"], default=None)
    current = newest["scene_signature"] if newest else None
    waiting: dict[str | None, list[ReviewItem]] = {}
    for item in items:
        if item.kind == "clip":
            waiting.setdefault(item.scene, []).append(item)
    dismissed: dict[str | None, list[tuple[str, str]]] = {}
    for clip in ready:
        if _unlabeled_clip(clip) and _dismissed(clip):
            entry = (clip["requested_at"], clip["id"])
            dismissed.setdefault(clip["scene_signature"], []).append(entry)
    groups = []
    for scene in waiting.keys() | dismissed.keys():
        rows = sorted(waiting.get(scene, []), key=lambda item: item.at)
        # 確認待ちが残っていなければ、外した動画で期間とサムネイルを示す。
        shown = [(item.at, item.id) for item in rows] or sorted(dismissed[scene])
        latest = max([shown[-1][0], *(at for at, _ in dismissed.get(scene, []))])
        groups.append(
            (
                latest,
                SceneGroup(
                    scene,
                    tuple(item.id for item in rows),
                    len(dismissed.get(scene, [])),
                    shown[0][0],
                    shown[-1][0],
                    shown[-1][1],
                    newest is not None and scene == current,
                ),
            )
        )
    return [group for _, group in sorted(groups, key=lambda pair: pair[0], reverse=True)]


def find_scene(groups: Sequence[SceneGroup], key: str) -> SceneGroup | None:
    """画面で渡す画角の値（署名、記録のない画角は UNKNOWN_SCENE）に合う組。"""
    return next((group for group in groups if scene_key(group.scene) == key), None)


def scene_key(scene: str | None) -> str:
    """画面で渡す画角の値。署名の記録がない画角は UNKNOWN_SCENE。"""
    return scene or UNKNOWN_SCENE
