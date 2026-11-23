"""保存済みの動画の後処理（自動判定と、旋回候補の分析）。通知はしない。"""

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import cv2

from ..config import Config
from ..detection.scene import scene_signature
from .context import (
    CommandContext,
    CommandError,
    check_limit,
    load_outside_roi,
    load_tray_tracker,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..detection.roi import AnyROI
    from ..features.tray_tracking import TrayTracker
    from ..video.clips import ClipRow
    from ..video.review import VideoJudge


def ready_judge(config: Config) -> "VideoJudge":
    """設定で選んだ判定器を用意し、使える状態か確かめる。"""
    from ..video.providers import build_judge

    try:
        judge = build_judge(config.review_provider, config.review_model)
        judge.check()
    except ValueError as error:
        # インストールとログインの案内だけで、外部の出力は含まない。
        raise CommandError(str(error)) from None
    return judge


def video_review(args: argparse.Namespace, context: CommandContext) -> int:
    """保存済みの未ラベルの動画を自動判定する（通知はしない）。"""
    from ..video.clips import ClipStore
    from ..video.judge_source import FixedJudge, JudgeChoice
    from ..video.review import Review, inspect_clip

    detect_outside = args.detect_outside or context.config.detect_outside_enabled
    outside_roi = load_outside_roi(context) if detect_outside else None
    judge = ready_judge(context.config)
    check_limit(args.limit)
    roi, baseline = context.scene.load()
    track_tray = args.track_tray or context.config.track_tray_enabled
    tray_tracker = load_tray_tracker(context.scene, baseline, roi, track_tray)
    review = Review(
        ClipStore(context.root),
        lambda path, row, chosen: inspect_clip(
            path, row, roi, chosen, tray_tracker=tray_tracker, outside_roi=outside_roi
        ),
        scene_signature=scene_signature(baseline, roi),
        judges=FixedJudge(
            JudgeChoice(judge, context.config.review_provider, context.config.review_model or None)
        ),
    )
    try:
        completed, failed = review.run(args.limit, clip_id=args.clip)
    except ValueError as error:
        # 別の判定が実行中であることなど、このモジュールの文言だけを表示する。
        raise CommandError(str(error)) from None
    print(f"自動判定の候補 {completed}件、失敗 {failed}件")
    return 2 if failed else 0


def circling_review(args: argparse.Namespace, context: CommandContext) -> int:
    """保存済みの動画に犬の検出をかけ、トレー付近の旋回候補を記録する。"""
    from ..experimental.circling import inspect_circling
    from ..features.dog import DogDetector
    from ..video.clips import ClipStore

    check_limit(args.limit)
    roi, baseline = context.scene.load()
    store = ClipStore(context.root)
    rows = store.circling.targets(scene_signature(baseline, roi), args.limit, args.clip)
    if not rows:
        print("現在の画角で分析できる保存済み動画がありません")
        return 2 if args.clip else 0
    track_tray = args.track_tray or context.config.track_tray_enabled
    tracker = load_tray_tracker(context.scene, baseline, roi, track_tray)
    model = DogDetector(context.dog_model_dir, context.config.dog_confidence)
    failed = 0
    for row in rows:
        clip_id = row["id"]
        try:
            path = store.video_path(row)
            if path is None:
                raise OSError("動画がありません")
            candidates, sampled, located = inspect_circling(
                path, model.predict, roi, tray_tracker=tracker
            )
            if not store.circling.save(clip_id, candidates, sampled, located):
                raise OSError("動画の状態が変わりました")
        except (OSError, ValueError, cv2.error):
            failed += 1
            logger.warning("%s 旋回分析に失敗しました", clip_id)
            continue
        offsets = "、".join(f"{start:.1f}〜{end:.1f}秒" for start, end in candidates)
        print(
            f"{clip_id} 旋回候補 {offsets or 'なし'}（位置取得 {located}/{sampled}コマ）",
            flush=True,
        )
    return 2 if failed else 0


def video_screening(args: argparse.Namespace, context: CommandContext) -> int:
    """保存済みの動画に犬検出をかけ、判定の前のふるい分けの指標を記録する（判定はしない）。

    画角はデータの保存先と tray-scenes/ の下の過去の登録から、動画の署名で選ぶ。
    """
    from ..experimental.screening import ClipScreener
    from ..video.clips import ClipStore
    from .datasets import load_scene, scene_folders

    if args.evaluate_endpoint is not None:
        evaluate_endpoint(Path(args.evaluate_endpoint), context)
        return 0

    check_limit(args.limit)
    folders = scene_folders(context.root, [])
    store = ClipStore(context.root)
    rows = store.screenings.targets(folders, args.limit, args.clip)
    if not rows:
        print("登録済みの画角で指標を求められる保存済み動画がありません")
        return 2 if args.clip else 0
    track_tray = args.track_tray or context.config.track_tray_enabled
    scenes: dict[str, tuple[AnyROI, TrayTracker | None]] = {}

    def scene_of(row: "ClipRow") -> tuple["AnyROI", "TrayTracker | None"]:
        signature = str(row["scene_signature"])
        if signature not in scenes:
            folder = context.root / folders[signature]
            scene = load_scene(folder, context.root, track_tray=track_tray, detect_outside=False)
            scenes[signature] = (scene.roi, scene.tray_tracker)
        return scenes[signature]

    screener = ClipScreener(
        store.screenings, scene_of, context.dog_model_dir, context.config.dog_confidence
    )
    failed = 0
    for row in rows:
        clip_id = row["id"]
        try:
            path = store.video_path(row)
            if path is None:
                raise OSError("動画がありません")
            result = screener(path, row, again=True)
        except (OSError, ValueError, cv2.error):
            failed += 1
            logger.warning("%s ふるい分けの指標を求められませんでした", clip_id)
            continue
        if result is not None:
            print(
                f"{clip_id} トレー上 {result.tray_frames}コマ・確信度の低いコマ "
                f"{result.weak_tray_frames}／滞在 {result.tray_seconds:.1f}秒"
                f"（低い確信度を含めて {result.weak_tray_seconds:.1f}秒）",
                flush=True,
            )
    return 2 if failed else 0


def evaluate_endpoint(path: Path, context: CommandContext) -> None:
    """台帳の前後差分の値を使い、犬の出入りを判定から省いた場合の見逃しを数える。"""
    from ..video import benchmark_sets
    from ..video.clips import ClipStore

    dataset = benchmark_sets.read(path)
    if dataset.version is None:
        raise CommandError("benchmark-dataset freeze で評価セットを固定してください")

    assessments = ClipStore(context.root).assessments
    measurements: dict[str, list[tuple[str, float | None]]] = {
        split: [] for split in benchmark_sets.SPLITS
    }
    for entry in dataset.included:
        if entry.source in ("dog_enter", "dog_exit", "dog_visit"):
            assessment = assessments.get(entry.id)
            change = assessment["endpoint_change"] if assessment is not None else None
            measurements[entry.split].append((entry.label, change))

    print("評価区分\tしきい値\t対象動画\t未計測\t省略候補\t見逃す排便\t見逃す排尿")
    for split, cases in measurements.items():
        unknown = sum(change is None for _, change in cases)
        for threshold in (0.0, 0.005, 0.01, 0.02, 0.05):
            skipped = [
                label for label, change in cases if change is not None and change <= threshold
            ]
            print(
                benchmark_sets.SPLIT_NAMES[split],
                f"{threshold * 100:g}%",
                len(cases),
                unknown,
                len(skipped),
                sum(label in ("feces", "both") for label in skipped),
                sum(label in ("urine", "both") for label in skipped),
                sep="\t",
            )
