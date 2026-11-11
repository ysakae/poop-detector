"""イベント・排泄の記録の表示と入力、ラベル GUI の起動。カメラは使わない。"""

import argparse
import json
from datetime import datetime

from ..events import Store
from .context import CommandContext


def require_timezones(args: argparse.Namespace) -> None:
    """--start・--end の時刻にタイムゾーンがあるか確かめる。"""
    for field in ("start", "end"):
        value = getattr(args, field, None)
        if value and datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("時刻には+09:00などのタイムゾーンが必要です")


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def events(_args: argparse.Namespace, context: CommandContext) -> int:
    _print_json(Store(context.root).list())
    return 0


def metrics(_args: argparse.Namespace, context: CommandContext) -> int:
    _print_json(Store(context.root).metrics())
    return 0


def records(_args: argparse.Namespace, context: CommandContext) -> int:
    _print_json(Store(context.root).records())
    return 0


def feedback(args: argparse.Namespace, context: CommandContext) -> int:
    """検知イベントの目視の判定を保存する。省いた時刻とメモは既存の値を保つ。"""
    store = Store(context.root)
    require_timezones(args)
    values = {
        key: value
        for key, value in [("start", args.start), ("end", args.end), ("note", args.note)]
        if value is not None
    }
    existing = next(
        (row["excretion_type"] for row in store.list() if row["id"] == args.event_id),
        "unspecified",
    )
    store.classify(
        args.event_id, (args.type or existing) if args.label == "true" else "false", **values
    )
    return 0


def acknowledge(args: argparse.Namespace, context: CommandContext) -> int:
    store = Store(context.root)
    store.update(args.event_id, acknowledged=1)
    if store.monitor_state()["event_id"] == args.event_id:
        store.set_monitor_state("ACKNOWLEDGED", args.event_id)
    return 0


def missed(args: argparse.Namespace, context: CommandContext) -> int:
    store = Store(context.root)
    require_timezones(args)
    store.add_excretion(args.type, args.start, args.end, missed=True)
    return 0


def record(args: argparse.Namespace, context: CommandContext) -> int:
    store = Store(context.root)
    require_timezones(args)
    store.add_excretion(args.type, args.start, args.end, args.note)
    return 0


def label_gui(args: argparse.Namespace, context: CommandContext) -> int:
    from ..label_gui import serve

    serve(
        context.root,
        args.port,
        not args.no_browser,
        disk_mb=context.config.clip_disk_mb,
        pet_name=context.config.pet_name,
    )
    return 0
