"""判定器の比較に使う評価の候補と条件のタグ（動画の画面で人が選ぶ）。

benchmark_candidates テーブルに、録画ごとの選択（評価に入れる・除く）と条件のタグを持つ。
benchmark-dataset create がこれを読み、評価セットの下書きの exclude と tags に反映する。
選べるのは、保存済みで人が排泄の種類か排泄なしのラベルを付けた録画だけ。
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..records.events import utcnow

if TYPE_CHECKING:
    from .clips import ClipStore

STATES = {"include": "評価に入れる", "exclude": "評価から除く"}
# 動画の画面にチェックボックスで並べる条件のタグ。ほかのタグは自由に書ける。
CONDITION_TAGS = (
    "昼間",
    "夜間（赤外線）",
    "メッシュ付きトレー",
    "遮蔽",
    "既存の跡と新しい排泄",
)
TAG_LENGTH = 30
TAG_LIMIT = 10
# 1つの入力欄に複数のタグを書くときの区切り。
SEPARATORS = re.compile(r"[,、，]")
# 評価の正解に使えるラベル（判断できない・未ラベルは選べない）。
EVALUATED_LABELS = ("feces", "urine", "both", "none")


@dataclass(frozen=True)
class Candidate:
    """録画1本の選択。state が None なら、評価セットの既定（ラベルのある録画をすべて使う）。"""

    state: str | None
    tags: tuple[str, ...] = ()


def parse_tags(values: Iterable[str]) -> tuple[str, ...]:
    """入力欄の値を条件のタグにする。区切りで分け、前後の空白を除き、重複を除く。"""
    tags: list[str] = []
    for value in values:
        for part in SEPARATORS.split(value):
            tag = part.strip()
            if not tag or tag in tags:
                continue
            if any(ord(char) < 32 for char in tag):
                raise ValueError("条件のタグに使えない文字が含まれています")
            if len(tag) > TAG_LENGTH:
                raise ValueError(f"条件のタグは1つ{TAG_LENGTH}文字以内にしてください")
            tags.append(tag)
    if len(tags) > TAG_LIMIT:
        raise ValueError(f"条件のタグは{TAG_LIMIT}個までです")
    return tuple(tags)


def _candidate(state: str | None, tags: str) -> Candidate:
    return Candidate(state, tuple(json.loads(tags)))


class BenchmarkCandidates:
    """benchmark_candidates テーブル。"""

    def __init__(self, clips: "ClipStore") -> None:
        self.connect = clips.connect

    def get(self, clip_id: str) -> Candidate | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT state, tags FROM benchmark_candidates WHERE clip_id=?", (clip_id,)
            ).fetchone()
        return None if row is None else _candidate(row["state"], row["tags"])

    def all(self) -> dict[str, Candidate]:
        with self.connect() as db:
            return {
                row["clip_id"]: _candidate(row["state"], row["tags"])
                for row in db.execute("SELECT clip_id, state, tags FROM benchmark_candidates")
            }

    def save(self, clip_id: str, state: str | None, tags: Iterable[str]) -> None:
        """録画の選択とタグを保存する。選択もタグもなければ行を消し、既定に戻す。"""
        if state is not None and state not in STATES:
            raise ValueError("評価の候補の選択を確認してください")
        normalized = parse_tags(tags)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status, label FROM clips WHERE id=?", (clip_id,)).fetchone()
            if row is None or row["status"] != "ready" or row["label"] not in EVALUATED_LABELS:
                raise ValueError(
                    "評価の候補には、保存済みで排泄の種類か排泄なしの"
                    "ラベルを付けた動画を選んでください"
                )
            if state is None and not normalized:
                db.execute("DELETE FROM benchmark_candidates WHERE clip_id=?", (clip_id,))
                return
            db.execute(
                """INSERT INTO benchmark_candidates (clip_id, state, tags, updated_at)
                VALUES (?,?,?,?) ON CONFLICT(clip_id) DO UPDATE SET
                state=excluded.state, tags=excluded.tags, updated_at=excluded.updated_at""",
                (clip_id, state, json.dumps(list(normalized), ensure_ascii=False), utcnow()),
            )
