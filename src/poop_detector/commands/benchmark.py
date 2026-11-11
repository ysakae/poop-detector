"""ラベル付き動画で判定器を比較し、運用の記録と別に結果を出力する。"""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Config
from ..detection.roi import ROI
from ..detection.scene import load_baseline, scene_signature
from ..features.tray_tracking import TrayTracker
from ..video.benchmark import evaluate, read_cases
from ..video.clips import ClipStore
from ..video.review import VideoJudge
from .context import CommandContext, CommandError, load_outside_roi, load_scene, load_tray_tracker
from .review import ready_judge


def parse_judges(values: list[str]) -> dict[str, VideoJudge]:
    configurations = {}
    for value in values:
        provider, separator, model = value.partition(":")
        if not separator or not model or value in configurations:
            raise CommandError("--judge は異なる 判定器:モデル名 を指定してください")
        configurations[value] = Config(review_provider=provider, review_model=model)
    if len(configurations) < 2:
        raise CommandError("比較する --judge を2つ以上指定してください")
    return {name: ready_judge(config) for name, config in configurations.items()}


def format_score(score: float | None) -> str:
    return "―" if score is None else f"{score:.1%}"


def print_summary(summaries: dict[str, Any]) -> None:
    print("判定器:モデル\t件数\t排便 適合率/再現率\t排尿 適合率/再現率\t判定不能\tエラー")
    for name, summary in summaries.items():
        scores = [
            f"{format_score(summary['types'][label]['precision'])}/"
            f"{format_score(summary['types'][label]['recall'])}"
            for label in ("feces", "urine")
        ]
        print(name, summary["cases"], *scores, summary["uncertain"], summary["errors"], sep="\t")


def video_benchmark(args: argparse.Namespace, context: CommandContext) -> int:
    if not args.send_images:
        raise CommandError("選んだ判定器に評価画像を送る場合は --send-images を指定してください")
    destination = Path(args.output)
    if destination.exists():
        raise CommandError("出力先のファイルが既にあります")
    dataset = Path(args.dataset)
    raw_manifest = dataset.read_bytes()
    config = context.config
    track_tray = args.track_tray or config.track_tray_enabled
    detect_outside = args.detect_outside or config.detect_outside_enabled
    if args.scene:
        scene = Path(args.scene)
        roi = ROI.load(scene / "roi.json")
        baseline = load_baseline(scene / "baseline" / "empty.png", roi)
        tracker = (
            TrayTracker.from_baseline(scene / "baseline" / "tray-reference.png", baseline, roi)
            if track_tray
            else None
        )
        outside_roi = ROI.load(scene / "outside-roi.json") if detect_outside else None
    else:
        roi, baseline = load_scene(context)
        tracker = load_tray_tracker(context, baseline, roi, track_tray)
        outside_roi = load_outside_roi(context) if detect_outside else None
    signature = scene_signature(baseline, roi)
    cases, excluded = read_cases(json.loads(raw_manifest), ClipStore(context.root), signature)
    judges = parse_judges(args.judge)
    with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        rows, summaries = evaluate(cases, judges, roi, tracker, outside_roi)
        report = {
            "version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "dataset": dataset.name,
            "dataset_sha256": hashlib.sha256(raw_manifest).hexdigest(),
            "scene_signature": signature,
            "track_tray": track_tray,
            "detect_outside": detect_outside,
            "excluded_without_confirmed_label": excluded,
            "cases": rows,
            "summaries": summaries,
        }
        json.dump(report, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print_summary(summaries)
    print(f"正解が未確定のため除外した録画 {len(excluded)}件")
    return 0
