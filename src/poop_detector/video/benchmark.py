"""人がラベルを付けた動画を使い、判定器ごとの検出精度を比較する。"""

import hashlib
import json
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2

from ..detection.roi import AnyROI
from ..errors import InvalidInputError
from ..features.tray_tracking import TrayTracker
from .benchmark_sets import Dataset
from .clip_reviews import Assessment
from .clips import ClipRow, ClipStore
from .review import PreparedClip, VideoJudge, prepare_clip

LABELS = ("feces", "urine", "both", "none")
PREDICTIONS = (*LABELS, "uncertain", "error")
REVIEW_ERRORS = (OSError, ValueError, TypeError, subprocess.SubprocessError, cv2.error)


@dataclass(frozen=True)
class Case:
    """人が確認した評価対象の動画1本と、その分類用のタグ。

    scene は撮影時の画角の署名で、evaluate に渡す scenes の鍵。split は評価セットの
    調整用（tuning）か最終評価用（final）で、手書きの評価セットでは空。
    """

    row: ClipRow
    path: Path
    label: str
    tags: tuple[str, ...]
    scene: str = ""
    split: str = ""


@dataclass(frozen=True)
class Scene:
    """1つの画角の登録情報。録画はそれぞれ撮影時の登録情報で切り出す。"""

    roi: AnyROI
    tray_tracker: TrayTracker | None = None
    outside_roi: AnyROI | None = None


def read_cases(manifest: object, store: ClipStore, signature: str) -> tuple[list[Case], list[str]]:
    """評価前に動画と正解を確認し、同じイベントが二重に入るのを防ぐ。"""
    entries = manifest.get("clips") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or not entries:
        raise InvalidInputError("評価セットの clips に動画を指定してください")
    cases = []
    excluded = []
    ids: set[str] = set()
    events: set[str] = set()
    for entry in entries:
        clip_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(clip_id, str) or not clip_id or clip_id in ids:
            raise InvalidInputError("評価セットに不正または重複した動画IDがあります")
        ids.add(clip_id)
        row = store.get(clip_id)
        if row is None:
            raise InvalidInputError("評価セットに台帳に存在しない動画があります")
        label = row["label"]
        if label not in LABELS:
            excluded.append(clip_id)
            continue
        path = store.video_path(row)
        if path is None or row["scene_signature"] != signature:
            raise InvalidInputError("保存済みで現在の基準と一致する動画だけを指定してください")
        event = entry.get("event", row["event_id"] or clip_id)
        linked_event = row["event_id"]
        tags = entry.get("tags", [])
        if (
            not isinstance(event, str)
            or not event
            or event in events
            or (linked_event is not None and linked_event in events)
            or not isinstance(tags, list)
            or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
        ):
            raise InvalidInputError(
                "評価セットのイベントの重複、またはタグの形式を確認してください"
            )
        events.add(event)
        if linked_event is not None:
            events.add(linked_event)
        case_tags = {*tags}
        if row["interrupted"]:
            case_tags.add("一部欠落")
        if label is not None:
            cases.append(Case(row, path, label, tuple(sorted(case_tags)), scene=signature))
    if not cases:
        raise InvalidInputError("評価できる確定ラベル付き動画がありません")
    return cases, excluded


def dataset_cases(dataset: Dataset, store: ClipStore) -> list[Case]:
    """固定した評価セットの録画を、撮影時の画角と分割を付けて読む。

    録画・ラベル・画角は benchmark_sets.inspect で先に確かめておく。
    """
    cases = []
    for entry in dataset.included:
        row = store.get(entry.id)
        path = store.video_path(row)
        if row is None or path is None or entry.scene_signature is None:
            raise InvalidInputError("評価セットの録画を確認してください")
        tags = set(entry.tags)
        if row["interrupted"]:
            tags.add("一部欠落")
        cases.append(
            Case(
                row,
                path,
                entry.label,
                tuple(sorted(tags)),
                scene=entry.scene_signature,
                split=entry.split,
            )
        )
    if not cases:
        raise InvalidInputError("評価できる確定ラベル付き動画がありません")
    return cases


def summarize(pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    counts = Counter(pairs)
    types = {}
    for label in ("feces", "urine"):
        positive = (label, "both")
        true_positive = sum(
            counts[actual, predicted] for actual in positive for predicted in positive
        )
        predicted_total = sum(
            count for (_, predicted), count in counts.items() if predicted in positive
        )
        actual_total = sum(count for (actual, _), count in counts.items() if actual in positive)
        types[label] = {
            "true_positive": true_positive,
            "false_positive": predicted_total - true_positive,
            "false_negative": actual_total - true_positive,
            "precision": true_positive / predicted_total if predicted_total else None,
            "recall": true_positive / actual_total if actual_total else None,
        }
    return {
        "cases": len(pairs),
        "uncertain": sum(predicted == "uncertain" for _, predicted in pairs),
        "errors": sum(predicted == "error" for _, predicted in pairs),
        "types": types,
        "confusion": {
            actual: {predicted: counts[actual, predicted] for predicted in PREDICTIONS}
            for actual in LABELS
        },
    }


def input_metadata(prepared: PreparedClip, extracted_at: str) -> dict[str, Any]:
    prompt = json.dumps([prepared.prompt, prepared.schema], sort_keys=True, ensure_ascii=False)
    return {
        "extracted_at": extracted_at,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "sample_times_seconds": prepared.timestamps,
        "images": {
            name: hashlib.sha256((prepared.folder / name).read_bytes()).hexdigest()
            for name in prepared.images
        },
    }


def evaluate_case(case: Case, judges: Mapping[str, VideoJudge], scene: Scene) -> dict[str, Any]:
    """1本の録画から画像を一度だけ切り出し、同じ画像と指示をすべての判定器に渡す。"""
    results: dict[str, dict[str, Any]] = {}
    metadata = None
    outside = scene.outside_roi if case.row["source"] in ("outside_stay", "manual") else None
    try:
        with prepare_clip(
            case.path, case.row, scene.roi, tray_tracker=scene.tray_tracker, outside_roi=outside
        ) as prepared:
            if isinstance(prepared, Assessment):
                results = {name: asdict(prepared) for name in judges}
            else:
                metadata = input_metadata(prepared, datetime.now(UTC).isoformat())
                for name, judge in judges.items():
                    try:
                        results[name] = asdict(prepared.inspect(judge))
                    except REVIEW_ERRORS:
                        results[name] = {"label": "error", "error": "判定器の実行に失敗"}
    except REVIEW_ERRORS:
        results = {
            name: {"label": "error", "error": "評価用の動画を準備できません"} for name in judges
        }
    return {
        "id": case.row["id"],
        "label": case.label,
        "split": case.split,
        "tags": case.tags,
        "scene_signature": case.scene,
        "inputs": metadata,
        "results": results,
    }


def evaluate(
    cases: Sequence[Case], judges: Mapping[str, VideoJudge], scenes: Mapping[str, Scene]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """録画ごとに判定し、判定器ごとに全体・条件別・分割別の集計を返す。"""
    rows = [evaluate_case(case, judges, scenes[case.scene]) for case in cases]
    tags = sorted({tag for case in cases for tag in case.tags})
    splits = sorted({case.split for case in cases if case.split})
    summaries = {}
    for name in judges:
        pairs = [(row["label"], row["results"][name]["label"]) for row in rows]
        paired = list(zip(cases, pairs, strict=True))
        summaries[name] = {
            **summarize(pairs),
            "by_tag": {
                tag: summarize([pair for case, pair in paired if tag in case.tags]) for tag in tags
            },
            "by_split": {
                split: summarize([pair for case, pair in paired if case.split == split])
                for split in splits
            },
        }
    return rows, summaries
