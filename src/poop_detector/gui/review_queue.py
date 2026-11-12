"""確認待ち（まだラベルのない検知イベントと、排泄に関わる動画）を1つの列にまとめる。

動画のある検知は、動画で確かめる1件にまとめる。どちらかにラベルが付けば、その件は終わり。
動画で「排泄なし」とすると、ひも付く検知も誤検知になる（video.clip_labels.ClipLabels.save）。
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode

if TYPE_CHECKING:
    from ..records.events import Store
    from ..video.clips import ClipStore

# 排泄に関わる動画の発生元。犬の出入りの動画は、自動判定が排泄の候補としたものだけを入れる。
REVIEW_SOURCES = frozenset({"detected", "manual", "early_alert", "outside_stay"})
EXCRETION_LABELS = frozenset({"feces", "urine", "both"})
NEXT_PAGE = "/review/next"


@dataclass(frozen=True)
class ReviewItem:
    """確認待ちの1件。at は UTC の ISO 8601。event_id は動画にまとめた検知。"""

    kind: Literal["event", "clip"]
    id: str
    at: str
    source: str
    review_label: str | None = None
    event_id: str | None = None

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"

    @property
    def url(self) -> str:
        """ラベルを付ける画面。from=review で、保存後に次の件へ進む。"""
        if self.kind == "clip":
            return "/clips?" + urlencode({"id": self.id, "from": "review"})
        return "/records/events?" + urlencode(
            {"filter": "unlabeled", "id": self.id, "from": "review"}
        )


def _waiting_clip(clip: Mapping[str, Any]) -> bool:
    return clip["status"] == "ready" and clip["label"] is None


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
    finished = {clip["event_id"] for clip in clips if clip["label"] is not None}
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
            )
        )
    items += [
        ReviewItem("event", row["id"], row["timestamp"], "event")
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
