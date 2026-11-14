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
from ..video import benchmark_sets
from ..video.benchmark import Case, Scene, dataset_cases, evaluate, read_cases
from ..video.benchmark_results import REPORT_VERSION, default_output
from ..video.benchmark_sets import Dataset
from ..video.cli_versions import cli_version
from ..video.clips import ClipStore
from ..video.judge_runs import JudgeRunLog, RecordingJudge
from ..video.review import VideoJudge
from .context import CommandContext, CommandError, load_outside_roi, load_scene, load_tray_tracker
from .datasets import check_dataset, print_problems, resolve
from .datasets import load_scene as load_scene_folder
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


def record_runs(judges: dict[str, VideoJudge], log: JudgeRunLog) -> dict[str, VideoJudge]:
    """比較で判定器を呼んだ回数を、運用の判定と分けて数える（判定の台帳は変えない）。"""
    recorded: dict[str, VideoJudge] = {}
    for name, judge in judges.items():
        provider, _, model = name.partition(":")
        recorded[name] = RecordingJudge(judge, log, provider, model, "benchmark")
    return recorded


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


def _legacy_cases(
    args: argparse.Namespace, context: CommandContext, manifest: object
) -> tuple[list[Case], list[str], dict[str, Scene]]:
    """手書きの評価セット。1つの画角（現在か --scene）で撮った録画だけを評価する。"""
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
    cases, excluded = read_cases(manifest, ClipStore(context.root), signature)
    return cases, excluded, {signature: Scene(roi, tracker, outside_roi)}


def _dataset_cases(
    args: argparse.Namespace, context: CommandContext, dataset: Dataset
) -> tuple[list[Case], dict[str, Scene]]:
    """固定した評価セット。録画・ラベル・画角を確かめてから、画角ごとの登録情報を読む。"""
    if dataset.version is None:
        raise CommandError("下書きの評価セットは benchmark-dataset freeze で固定してください")
    if args.scene:
        raise CommandError("固定した評価セットでは、画角の場所を評価セットから読みます")
    store = ClipStore(context.root)
    problems = check_dataset(dataset, store, context.root)
    if problems.blocking:
        print_problems(problems)
        raise CommandError("評価セットに問題があるため比較しません。" + "、".join(problems.lines()))
    config = context.config
    track_tray = args.track_tray or config.track_tray_enabled
    detect_outside = args.detect_outside or config.detect_outside_enabled
    folders = resolve(dataset, context.root)
    used = {entry.scene_signature for entry in dataset.included}
    scenes = {
        signature: load_scene_folder(
            folders[signature], context.root, track_tray=track_tray, detect_outside=detect_outside
        )
        for signature in sorted(str(value) for value in used)
    }
    return dataset_cases(dataset, store), scenes


def judge_details(names: list[str]) -> dict[str, dict[str, str | None]]:
    """結果に残す、指定した判定器・モデルと CLI の版。"""
    details: dict[str, dict[str, str | None]] = {}
    for name in names:
        provider, _, model = name.partition(":")
        details[name] = {"provider": provider, "model": model, "cli_version": cli_version(provider)}
    return details


def video_benchmark(args: argparse.Namespace, context: CommandContext) -> int:
    if not args.send_images:
        raise CommandError("選んだ判定器に評価画像を送る場合は --send-images を指定してください")
    created = datetime.now(UTC)
    # 既定の保存先なら、管理 › 判定器とモデルの画面で結果を見られる。
    destination = Path(args.output) if args.output else default_output(context.root, created)
    if destination.exists():
        raise CommandError("出力先のファイルが既にあります")
    dataset_path = Path(args.dataset)
    raw_manifest = dataset_path.read_bytes()
    manifest = json.loads(raw_manifest)
    config = context.config
    dataset = benchmark_sets.parse(manifest) if benchmark_sets.is_dataset(manifest) else None
    if dataset is None:
        cases, excluded, scenes = _legacy_cases(args, context, manifest)
    else:
        (cases, scenes), excluded = _dataset_cases(args, context, dataset), []
    judges = record_runs(parse_judges(args.judge), ClipStore(context.root).judge_runs)
    details = judge_details(list(judges))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        rows, summaries = evaluate(cases, judges, scenes)
        report = {
            "version": REPORT_VERSION,
            "created_at": created.isoformat(),
            "dataset": dataset_path.name,
            "dataset_sha256": hashlib.sha256(raw_manifest).hexdigest(),
            "dataset_info": None
            if dataset is None
            else {
                "name": dataset.name,
                "version": dataset.version,
                "content_sha256": dataset.content_sha256,
            },
            "scene_signatures": sorted(scenes),
            "track_tray": args.track_tray or config.track_tray_enabled,
            "detect_outside": args.detect_outside or config.detect_outside_enabled,
            "judges": details,
            "excluded_without_confirmed_label": excluded,
            "cases": rows,
            "summaries": summaries,
        }
        json.dump(report, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print_summary(summaries)
    if excluded:
        print(f"正解が未確定のため除外した録画 {len(excluded)}件")
    return 0
