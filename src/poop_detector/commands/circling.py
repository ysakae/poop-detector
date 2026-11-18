"""旋回の規則と軌跡の決定木を、人が確かめた保存動画で比べる（circling-compare）。"""

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2

from ..detection.scene import scene_signature
from .context import CommandContext, CommandError, load_tray_tracker

logger = logging.getLogger(__name__)

MAX_LIMIT = 500


def _local_day(row: dict[str, Any]) -> str:
    stamp = row.get("start_at") or row["requested_at"]
    return datetime.fromisoformat(stamp).astimezone().date().isoformat()


def _confidence(args: argparse.Namespace, context: CommandContext) -> float:
    """この比較で使う犬検出のしきい値。監視の設定は変えない。"""
    if args.dog_confidence is None:
        return context.config.dog_confidence
    return float(args.dog_confidence)


def _measure(args: argparse.Namespace, context: CommandContext) -> list[Any]:
    """いまの画角でラベルのある動画に犬検出をかけ、比べる事例を作る。"""
    from ..experimental.circling import find_circling, track_dog
    from ..experimental.circling_compare import EXCRETION_LABELS, Case, case_label
    from ..experimental.trajectory import trajectory_features
    from ..features.dog import DogDetector
    from ..video.clips import ClipStore

    roi, baseline = context.scene.load()
    store = ClipStore(context.root)
    labels = sorted(EXCRETION_LABELS | {"none"})
    rows = store.circling.labeled(scene_signature(baseline, roi), labels, args.limit)
    tracker = load_tray_tracker(
        context.scene, baseline, roi, args.track_tray or context.config.track_tray_enabled
    )
    model = DogDetector(context.dog_model_dir, _confidence(args, context))
    cases = []
    for row in rows:
        excretion = case_label(row["label"])
        path = store.video_path(row)
        if excretion is None or path is None:
            continue
        try:
            track = track_dog(path, model.predict, roi, tray_tracker=tracker)
        except (OSError, ValueError, cv2.error):
            logger.warning("%s 犬の位置を取り出せませんでした", row["id"])
            continue
        cases.append(
            Case(
                row["id"],
                _local_day(dict(row)),
                excretion,
                trajectory_features(track.samples),
                len(find_circling(track.samples)),
            )
        )
    return cases


def _load(path: Path) -> list[Any]:
    from ..experimental.circling_compare import Case

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [Case.from_data(item) for item in data["cases"]]
    except (OSError, ValueError, KeyError, TypeError):
        raise CommandError(f"{path.name} を特徴量のファイルとして読めません") from None


def _summary(name: str, result: dict[str, Any]) -> str:
    if "skipped" in result:
        return f"{name}: 比較できません（{result['skipped']}）"

    def rate(value: float | None) -> str:
        return "—" if value is None else f"{value:.2f}"

    return (
        f"{name}: 適合率 {rate(result['precision'])}、再現率 {rate(result['recall'])}"
        f"（TP {result['tp']}・FP {result['fp']}・FN {result['fn']}・TN {result['tn']}）"
    )


def circling_compare(args: argparse.Namespace, context: CommandContext) -> int:
    """比べた結果を表示し、--output があれば事例の特徴量と一緒に JSON で保存する。"""
    from ..experimental.circling_compare import evaluate

    if not 1 <= args.limit <= MAX_LIMIT:
        raise CommandError(f"--limitは1〜{MAX_LIMIT}件で指定してください")
    if args.features and args.dog_confidence is not None:
        raise CommandError("--features の特徴量は検出済みのため、--dog-confidence と併用できません")
    cases = _load(Path(args.features)) if args.features else _measure(args, context)
    if not cases:
        print("現在の画角で、人が排泄の有無を確かめた動画がありません")
        return 0
    result = evaluate(cases)
    if args.output:
        report: dict[str, Any] = {"cases": [case.to_data() for case in cases], "result": result}
        if not args.features:
            report["dog_confidence"] = _confidence(args, context)
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    note = "（排泄ありが少なく参考値）" if result["small_sample"] else ""
    print(f"動画 {result['cases']}本（排泄あり {result['positives']}本、{result['days']}日）{note}")
    print(_summary("規則（旋回候補あり）", result["rule"]))
    print(_summary("決定木（軌跡の特徴量）", result["classifier"]))
    return 0
