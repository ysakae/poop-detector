"""保存済みの動画の後処理（Codex による判定と、旋回候補の分析）。通知はしない。"""

import argparse
import sys

import cv2

from ..scene import scene_signature
from .context import (
    CommandContext,
    CommandError,
    check_limit,
    load_outside_roi,
    load_scene,
    load_tray_tracker,
)


def require_codex_login() -> None:
    from ..codex_review import require_codex

    try:
        require_codex()
    except ValueError as error:
        # インストールとログインの案内だけで、外部の出力は含まない。
        raise CommandError(str(error)) from None


def codex_review(args: argparse.Namespace, context: CommandContext) -> int:
    from ..clips import ClipStore
    from ..codex_review import Review, inspect_clip

    outside_roi = load_outside_roi(context) if args.detect_outside else None
    require_codex_login()
    check_limit(args.limit)
    roi, baseline = load_scene(context)
    tray_tracker = load_tray_tracker(context, baseline, roi, args.track_tray)
    review = Review(
        ClipStore(context.root),
        lambda path, row: inspect_clip(
            path, row, roi, tray_tracker=tray_tracker, outside_roi=outside_roi
        ),
        scene_signature=scene_signature(baseline, roi),
    )
    try:
        completed, failed = review.run(args.limit, clip_id=args.clip)
    except ValueError as error:
        # 別の判定が実行中であることなど、このモジュールの文言だけを表示する。
        raise CommandError(str(error)) from None
    print(f"Codex判定候補 {completed}件、失敗 {failed}件")
    return 2 if failed else 0


def circling_review(args: argparse.Namespace, context: CommandContext) -> int:
    """保存済みの動画に犬の検出をかけ、トレー付近の旋回候補を記録する。"""
    from ..circling import inspect_circling
    from ..clips import ClipStore
    from ..dog import DogDetector

    check_limit(args.limit)
    roi, baseline = load_scene(context)
    store = ClipStore(context.root)
    rows = store.circling_targets(scene_signature(baseline, roi), args.limit, args.clip)
    if not rows:
        print("現在の画角で分析できる保存済み動画がありません")
        return 2 if args.clip else 0
    tracker = load_tray_tracker(context, baseline, roi, args.track_tray)
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
            if not store.save_circling_review(clip_id, candidates, sampled, located):
                raise OSError("動画の状態が変わりました")
        except (OSError, ValueError, cv2.error):
            failed += 1
            print(f"{clip_id} 旋回分析に失敗しました", file=sys.stderr)
            continue
        offsets = "、".join(f"{start:.1f}〜{end:.1f}秒" for start, end in candidates)
        print(
            f"{clip_id} 旋回候補 {offsets or 'なし'}（位置取得 {located}/{sampled}コマ）",
            flush=True,
        )
    return 2 if failed else 0
