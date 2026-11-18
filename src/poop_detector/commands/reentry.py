"""再侵入の記録・片付け・評価・配置別集計を操作する CLI。"""

import argparse
import json
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime, tzinfo
from typing import Any

from ..errors import InvalidInputError
from ..features.reentry import ReentryStore
from ..records.events import EXCRETION_TYPES, Store
from .context import CommandContext
from .registration import CommandGroup


def add_parser(commands: CommandGroup) -> None:
    command = commands.add("reentry", run, help="排泄後の再侵入と片付けの記録")
    actions = command.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="候補、片付け状態、再侵入の詳細")
    actions.add_parser("report", help="配置ごとの再侵入と排便の集計")
    clean = actions.add_parser("collect", help="片付け完了を記録")
    clean.add_argument("event_id")
    event = actions.add_parser("event-feedback", help="排泄候補の正誤、便の状態、メモ")
    event.add_argument("event_id")
    event.add_argument("--label", choices=(*EXCRETION_TYPES, "false", "unknown"))
    event.add_argument("--location", choices=("tray", "outside"))
    event.add_argument("--stool", choices=("normal", "soft", "diarrhea", "other", "unknown"))
    event.add_argument("--note")
    visit = actions.add_parser("visit-feedback", help="再侵入の正誤、踏んだかどうか、メモ")
    visit.add_argument("visit_id", type=int)
    visit.add_argument("--correct", choices=("yes", "no", "unknown"))
    visit.add_argument("--stepped", choices=("yes", "no", "unknown"))
    visit.add_argument("--note")


def _binary(value: str | None) -> int | None:
    return {"yes": 1, "no": 0}.get(value or "")


def run(args: argparse.Namespace, context: CommandContext) -> int:
    store = ReentryStore(context.root)
    if args.action == "report":
        print(json.dumps(report(store), ensure_ascii=False, indent=2))
        return 0
    if args.action == "collect":
        if not store.collect(args.event_id):
            raise InvalidInputError("未回収の記録が見つかりません")
        print("片付け完了を記録しました")
        return 0
    if args.action == "list":
        print(json.dumps(list_records(store), ensure_ascii=False, indent=2))
        return 0
    if args.note is not None and len(args.note) > 2000:
        raise InvalidInputError("メモは2000文字以内にしてください")
    with closing(store.db.connect()) as db, db:
        if args.action == "event-feedback":
            if (
                db.execute(
                    "SELECT 1 FROM reentry_waste WHERE event_id=?", (args.event_id,)
                ).fetchone()
                is None
            ):
                raise InvalidInputError("排泄候補が見つかりません")
            if args.label:
                Store._classify(db, args.event_id, args.label, location=args.location)
            elif args.location:
                raise InvalidInputError("--location は --label と一緒に指定してください")
            db.execute(
                """UPDATE reentry_waste SET stool=COALESCE(?,stool), note=COALESCE(?,note)
                   WHERE event_id=?""",
                (args.stool, args.note, args.event_id),
            )
        elif not db.execute(
            """UPDATE reentry_visits
               SET correct=CASE WHEN ? THEN ? ELSE correct END,
                   stepped=CASE WHEN ? THEN ? ELSE stepped END, note=COALESCE(?,note)
               WHERE id=?""",
            (
                args.correct is not None,
                _binary(args.correct),
                args.stepped is not None,
                _binary(args.stepped),
                args.note,
                args.visit_id,
            ),
        ).rowcount:
            raise InvalidInputError("再侵入の記録が見つかりません")
    print("フィードバックを記録しました")
    return 0


def _visit_rows(store: ReentryStore) -> dict[str, list[dict[str, Any]]]:
    visits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with closing(store.db.connect()) as db:
        for row in db.execute("SELECT * FROM reentry_visits ORDER BY started_at, id"):
            visits[row["event_id"]].append(dict(row))
    return visits


def _seconds(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def list_records(store: ReentryStore) -> list[dict[str, Any]]:
    visits = _visit_rows(store)
    with closing(store.db.connect()) as db:
        rows = db.execute(
            """SELECT w.*, e.timestamp, e.actual_start, e.actual_end, e.score, e.image, e.feedback,
                      e.location, l.excretion_type
               FROM reentry_waste w JOIN events e ON e.id=w.event_id
               LEFT JOIN excretion_labels l ON l.event_id=e.id ORDER BY e.timestamp DESC"""
        ).fetchall()
    result = []
    for row in rows:
        episodes = visits[row["event_id"]]
        for number, visit in enumerate(episodes, 1):
            visit["visit_number"] = number
            visit["elapsed_seconds"] = _seconds(
                row["actual_end"] or row["actual_start"] or row["timestamp"], visit["started_at"]
            )
            visit["duration_seconds"] = (
                _seconds(visit["started_at"], visit["ended_at"]) if visit["ended_at"] else None
            )
        result.append({**dict(row), "visits": episodes})
    return result


def report(store: ReentryStore, zone: tzinfo | None = None) -> dict[str, Any]:
    tracked = list_records(store)
    now = datetime.now(UTC).isoformat()
    with closing(store.db.connect()) as db:
        layouts = db.execute("SELECT * FROM reentry_configurations").fetchall()
        reviewed = db.execute(
            """SELECT COALESCE(e.actual_start,e.timestamp) AS at, e.location
               FROM events e JOIN excretion_labels l ON l.event_id=e.id
               WHERE e.feedback=1 AND l.excretion_type IN ('feces','both')"""
        ).fetchall()
    layouts.sort(key=lambda row: datetime.fromisoformat(row["installed_at"]))
    result: dict[str, Any] = {}
    layout_ids = dict.fromkeys(row["id"] for row in layouts)
    layout_ids.update(dict.fromkeys(row["configuration_id"] for row in tracked))
    for layout_id in layout_ids:
        candidates = [row for row in tracked if row["configuration_id"] == layout_id]
        confirmed = [
            row
            for row in candidates
            if row["feedback"] == 1 and row["excretion_type"] in ("feces", "both")
        ]
        summary = _describe(confirmed, now, zone)
        summary["tracked_candidates"] = len(candidates)
        result[layout_id] = summary
    for index, layout in enumerate(layouts):
        start = datetime.fromisoformat(layout["installed_at"])
        end = (
            datetime.fromisoformat(layouts[index + 1]["installed_at"])
            if index + 1 < len(layouts)
            else datetime.fromisoformat(now)
        )
        known = Counter(
            row["location"]
            for row in reviewed
            if row["location"] in ("tray", "outside")
            and start <= datetime.fromisoformat(row["at"]) < end
        )
        locations = sum(known.values())
        result[layout["id"]].update(
            installed_at=layout["installed_at"],
            description=layout["description"],
            toilet_success={
                "count": known["tray"],
                "denominator": locations,
                "rate": known["tray"] / locations if locations else None,
            },
        )
    return result


def _describe(events: list[dict[str, Any]], now: str, zone: tzinfo | None) -> dict[str, Any]:
    daily: Counter[str] = Counter()
    hourly: Counter[int] = Counter()
    risks: Counter[str] = Counter()
    waiting: list[float] = []
    first: list[float] = []
    durations: list[float] = []
    accident_count = dangerous = 0
    followup: list[tuple[float, float | None, bool, float]] = []
    for event in events:
        at = event["actual_start"] or event["timestamp"]
        local = datetime.fromisoformat(at).astimezone(zone)
        daily[local.date().isoformat()] += 1
        hourly[local.hour] += 1
        visits = [visit for visit in event["visits"] if visit["correct"] != 0]
        risks.update(visit["risk"] for visit in visits)
        dangerous += any(visit["risk"] == "HIGH" for visit in visits)
        accident_count += sum(visit["stepped"] == 1 for visit in visits)
        waits = [max(0.0, visit["elapsed_seconds"]) for visit in visits]
        durations.extend(v["duration_seconds"] for v in visits if v["duration_seconds"] is not None)
        if waits:
            first.append(min(waits))
            waiting.extend(waits)
        began = event["actual_end"] or at
        observed_until = event["observed_until"]
        coverage = (
            _seconds(began, observed_until)
            if observed_until and _seconds(event["timestamp"], began) >= 0
            else -1.0
        )
        followup.append(
            (
                max(0.0, _seconds(event["actual_end"] or at, event["collected_at"] or now)),
                min(waits) if waits else None,
                bool(event["collected_at"]),
                coverage,
            )
        )

    rates = {}
    for minutes in (5, 15, 30):
        limit = minutes * 60
        count = sum(
            first_seen is not None and first_seen <= limit for _, first_seen, _, _ in followup
        )
        # 窓内の再侵入を確認できた場合は、欠落があっても結果を確定できる。
        denominator = sum(
            (seen is not None and seen <= limit)
            or coverage >= (min(age, limit) if collected else limit)
            for age, seen, collected, coverage in followup
        )
        rates[minutes] = {
            "count": count,
            "denominator": denominator,
            "unknown": len(events) - denominator,
            "small_sample": denominator < 20,
            "rate": count / denominator if denominator else None,
        }
    return {
        "confirmed_defecations": len(events),
        "daily_counts": dict(sorted(daily.items())),
        "hourly_counts": dict(sorted(hourly.items())),
        "reentry_rates": rates,
        "rate_note": "再侵入の有無が分かった記録で集計。観測欠落で不明の記録は分母から除外しています。",
        "first_reentry_seconds": first,
        "all_reentry_seconds": waiting,
        "stay_seconds": durations,
        "risk_counts": {risk: risks[risk] for risk in ("MEDIUM", "HIGH")},
        "high_event_rate": {
            "count": dangerous,
            "denominator": len(events),
            "rate": dangerous / len(events) if events else None,
        },
        "uncollected_seconds": [
            {"seconds": age, "still_pending": not collected} for age, _, collected, _ in followup
        ],
        "confirmed_accidents": accident_count,
        "small_sample": len(events) < 20,
    }
