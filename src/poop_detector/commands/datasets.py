"""benchmark-dataset サブコマンド。評価セットを作り、確かめ、版を固定する。

画角（ROI と基準画像）は、データの保存先と tray-scenes/ の下にある過去の登録と、
--scene で渡した場所から探し、署名ごとに評価セットへ場所を記録する。
"""

import argparse
import json
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path

from ..detection.roi import ROI
from ..detection.scene import load_baseline, scene_signature
from ..errors import InvalidInputError
from ..features.tray_tracking import TrayTracker
from ..video import benchmark_sets
from ..video.benchmark import Scene
from ..video.benchmark_sets import LABELS, SPLIT_NAMES, Dataset, Problems
from ..video.clip_storage import protect
from ..video.clips import CLIP_LABELS, ClipStore
from .context import CommandContext, CommandError

SCENE_ARCHIVE = "tray-scenes"


def _signature(folder: Path) -> str | None:
    """ROI と基準画像の署名。読めなければ None。"""
    try:
        roi = ROI.load(folder / "roi.json")
        return scene_signature(load_baseline(folder / "baseline" / "empty.png", roi), roi)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _stored(folder: Path, root: Path) -> str:
    """評価セットに書く場所。データの保存先の中なら相対パスにする。"""
    folder = folder.resolve()
    root = root.resolve()
    return os.path.relpath(folder, root) if folder.is_relative_to(root) else str(folder)


def scene_folders(root: Path, extra: Iterable[str]) -> dict[str, str]:
    """署名ごとの画角の場所。--scene で渡した場所は、署名を読めなければ断る。"""
    found: dict[str, str] = {}
    candidates = [root]
    archive = root / SCENE_ARCHIVE
    if archive.is_dir():
        candidates.extend(sorted(path for path in archive.iterdir() if path.is_dir()))
    for folder in candidates:
        signature = _signature(folder)
        if signature is not None:
            found.setdefault(signature, _stored(folder, root))
    for value in extra:
        folder = Path(value)
        signature = _signature(folder)
        if signature is None:
            raise CommandError("--scene の場所に ROI と基準画像がありません")
        found[signature] = _stored(folder, root)
    return found


def resolve(dataset: Dataset, root: Path) -> dict[str, Path]:
    return {signature: root / folder for signature, folder in dataset.scenes.items()}


def actual_signatures(folders: Mapping[str, Path]) -> dict[str, str | None]:
    return {signature: _signature(folder) for signature, folder in folders.items()}


def load_scene(folder: Path, root: Path, *, track_tray: bool, detect_outside: bool) -> Scene:
    """画角の場所から、録画の切り出しに使う登録情報を読む。"""
    roi = ROI.load(folder / "roi.json")
    baseline = load_baseline(folder / "baseline" / "empty.png", roi)
    tracker = (
        TrayTracker.from_baseline(folder / "baseline" / "tray-reference.png", baseline, roi)
        if track_tray
        else None
    )
    outside_roi = None
    if detect_outside:
        # トレー外の範囲は画面の座標で、トレーの交換では変わらないので、保存先の登録も使う。
        path = folder / "outside-roi.json"
        path = path if path.is_file() else root / "outside-roi.json"
        if not path.is_file():
            raise CommandError("先に setup-roi --outside でトレーと周囲の床を囲んでください")
        outside_roi = ROI.load(path)
    return Scene(roi, tracker, outside_roi)


def _day(value: str | None, option: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CommandError(f"{option} は YYYY-MM-DD で指定してください") from None


def _read_base(value: str | None) -> object:
    if value is None:
        return None
    try:
        return json.loads(Path(value).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise CommandError("--base の評価セットを読めません") from None


def print_coverage(dataset: Dataset) -> None:
    """種類・分割・条件ごとの件数を表で出す。"""
    summary = benchmark_sets.coverage(dataset)
    names = [CLIP_LABELS[label] for label in LABELS]
    print("区分", *names, sep="\t")
    for split, counts in summary["by_split"].items():
        print(SPLIT_NAMES[split], *counts.values(), sep="\t")
    for tag, counts in summary["by_tag"].items():
        print(f"条件:{tag}", *counts.values(), sep="\t")
    for activity, count in summary["none_activities"].items():
        print(f"排泄なしの動作:{activity}\t{count}")
    missed = summary["missed"]
    print(
        f"評価する録画 {summary['cases']}件（除外 {summary['excluded']}件、"
        f"重複 {summary['duplicates']}件、判定不能 {summary['uncertain']}件、"
        f"未ラベル {summary['unlabeled']}件）"
    )
    print(
        f"見逃しの記録 {missed['total']}件（録画あり {missed['with_recording']}件、"
        f"録画なし {missed['without_recording']}件）"
    )


def print_problems(problems: Problems) -> None:
    lines = problems.lines()
    print("\n".join(lines) if lines else "問題はありません")


def check_dataset(dataset: Dataset, store: ClipStore, root: Path) -> Problems:
    return benchmark_sets.inspect(dataset, store, actual_signatures(resolve(dataset, root)))


def _create(args: argparse.Namespace, context: CommandContext) -> int:
    name = args.name
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise CommandError("--name は英数字・ハイフン・下線で指定してください")
    root = context.root
    dataset = benchmark_sets.build(
        ClipStore(root),
        name=name,
        created_at=datetime.now(UTC).isoformat(),
        scenes=scene_folders(root, args.scene or []),
        final_since=_day(args.final_since, "--final-since"),
        since=_day(args.since, "--since"),
        until=_day(args.until, "--until"),
        base=_read_base(args.base),
        selected_only=args.selected_only,
    )
    output = (
        Path(args.output)
        if args.output
        else benchmark_sets.datasets_dir(root) / f"{name}-draft.json"
    )
    benchmark_sets.write_new(output, dataset)
    print_coverage(dataset)
    print(f"下書きを保存しました: {output}")
    return 0


def _check(args: argparse.Namespace, context: CommandContext) -> int:
    dataset = benchmark_sets.read(Path(args.dataset))
    print(f"{dataset.name}（{'下書き' if dataset.version is None else f'版{dataset.version}'}）")
    print_coverage(dataset)
    print_problems(check_dataset(dataset, ClipStore(context.root), context.root))
    return 0


def _freeze(args: argparse.Namespace, context: CommandContext) -> int:
    if args.version is None:
        raise CommandError("freeze には --version を指定してください")
    draft = benchmark_sets.read(Path(args.dataset))
    frozen = benchmark_sets.freeze(draft, args.version)
    store = ClipStore(context.root)
    problems = check_dataset(frozen, store, context.root)
    if problems.blocking:
        print_problems(problems)
        raise InvalidInputError(
            "評価セットに問題があるため固定できません。" + "、".join(problems.lines())
        )
    output = (
        Path(args.output)
        if args.output
        else Path(args.dataset).with_name(f"{frozen.name}-v{frozen.version}.json")
    )
    benchmark_sets.write_new(output, frozen)
    # 固定した版の録画は、古い動画の自動整理で消えないように保護する。
    for entry in frozen.entries:
        protect(store, entry.id, True)
    print_coverage(frozen)
    print(f"版{frozen.version}を固定し、{len(frozen.entries)}本の録画を保護しました: {output}")
    return 0


def benchmark_dataset(args: argparse.Namespace, context: CommandContext) -> int:
    actions = {"create": _create, "check": _check, "freeze": _freeze}
    if args.action != "create" and not args.dataset:
        raise CommandError(f"{args.action} には評価セットのファイルを指定してください")
    return actions[args.action](args, context)
