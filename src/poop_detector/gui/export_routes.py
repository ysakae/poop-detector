"""管理 › 書き出しとバックアップ。確定記録の CSV、期間の集計の印刷用ページ、バックアップの作成。

家の中の画像や記録を含むので、すべて管理者だけの操作にし、監査ログに残す。
復元はデータの保存先を入れ替える操作なので、画面からは行わず、監視するマシンで
`poop-detector restore` を実行する（このマシンで実行できることを信頼の根拠にする）。
ルートの表への登録はこのモジュールを読み込んだときに行う（asgi.py が読み込む）。
"""

import tempfile
from collections.abc import Iterator
from datetime import date
from typing import IO, TYPE_CHECKING, Any

from ..errors import InvalidInputError
from ..records.backup import create_backup
from ..records.export import parse_period, records_csv, summarize_period, weights_csv
from ..records.weights import weights
from ..timezones import local_now, local_today, timezone_label
from .pages import Query
from .routes import Form, audit, first, get, post
from .templating import render_template
from .weight_routes import trend

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

PAGE = "/manage/export"
CHUNK = 1 << 16


def _day(value: date) -> str:
    return value.strftime("%Y/%m/%d")


def _period_text(first_day: date, last_day: date) -> str:
    return f"{_day(first_day)}〜{_day(last_day)}"


def _render(
    handler: "Handler",
    *,
    status: int = 200,
    start: str = "",
    end: str = "",
    error: str | None = None,
) -> None:
    default_first, default_last = parse_period("", "", today=local_today())
    page = render_template(
        "export.html",
        title=handler.app.title,
        token=handler.csrf,
        page=PAGE,
        start=start or default_first.isoformat(),
        end=end or default_last.isoformat(),
        timezone=timezone_label(),
        error=error,
    )
    handler.respond(status, page)


@get(PAGE, role="admin")
def export_page(handler: "Handler", _query: Query) -> None:
    _render(handler)


@post(f"{PAGE}/records.csv", role="admin")
@post(f"{PAGE}/weights.csv", role="admin")
def export_csv(handler: "Handler", form: Form) -> None:
    start = form.fields.get("from", "")
    end = form.fields.get("to", "")
    try:
        first_day, last_day = parse_period(start, end, today=local_today())
    except InvalidInputError as error:
        _render(handler, status=400, start=start, end=end, error=str(error))
        return
    if handler.path.split("?", 1)[0].endswith("/weights.csv"):
        data = weights_csv(weights(handler.app.store.database, first_day, last_day))
        kind = "weights"
    else:
        data = records_csv(handler.app.store.records(), first_day, last_day)
        kind = "records"
    name = f"poop-{kind}-{first_day:%Y%m%d}-{last_day:%Y%m%d}.csv"
    audit(handler, "export.csv", _period_text(first_day, last_day))
    handler.respond(
        200,
        data,
        "text/csv; charset=utf-8",
        {"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _summary(first_day: date, last_day: date, records: list[Any]) -> dict[str, Any]:
    summary = summarize_period(records, first_day, last_day)
    return {
        "period": _period_text(first_day, last_day),
        "days": summary.days,
        "total": summary.total,
        "feces": summary.feces,
        "urine": summary.urine,
        "unspecified": summary.unspecified,
        "recorded_days": summary.recorded_days,
        "per_day": f"{summary.per_day:.1f}",
        "locations": sorted(summary.locations.items()),
        "hourly": [
            {
                "hour": f"{row.hour}時",
                "feces": row.feces,
                "urine": row.urine,
                "unspecified": row.unspecified,
            }
            for row in summary.hourly
        ],
        "daily": [
            {
                "day": _day(row.day),
                "total": row.total,
                "feces": row.feces,
                "urine": row.urine,
                "unspecified": row.unspecified,
            }
            for row in summary.daily
        ],
    }


@get(f"{PAGE}/summary", role="admin")
def summary_page(handler: "Handler", query: Query) -> None:
    """動物病院に見せる、期間の回数と時間帯の集計（ブラウザーの印刷で紙や PDF にする）。"""
    start, end = first(query, "from"), first(query, "to")
    try:
        first_day, last_day = parse_period(start, end, today=local_today())
    except InvalidInputError as error:
        _render(handler, status=400, start=start, end=end, error=str(error))
        return
    values = _summary(first_day, last_day, list(handler.app.store.records()))
    measurements = weights(handler.app.store.database, first_day, last_day)
    audit(handler, "export.summary", values["period"])
    page = render_template(
        "export_summary.html",
        title=handler.app.title,
        summary=values,
        measurements=measurements,
        points=trend(measurements),
        bounds=(measurements[0].day, measurements[-1].day) if measurements else (),
        printed_at=local_now().strftime("%Y/%m/%d %H:%M"),
        timezone=timezone_label(),
    )
    handler.respond(200, page)


def _chunks(file: IO[bytes]) -> Iterator[bytes]:
    """一時ファイルを少しずつ返し、読み終えるか接続が切れたら閉じる（名前のない一時ファイル）。"""
    try:
        file.seek(0)
        while block := file.read(CHUNK):
            yield block
    finally:
        file.close()


@post(f"{PAGE}/backup", role="admin")
def export_backup(handler: "Handler", form: Form) -> None:
    include_clips = form.fields.get("clips") == "1"
    root = handler.app.root
    # 名前のない一時ファイル（作ってすぐ消える）に書き、送り終えたら閉じる。
    file = tempfile.TemporaryFile(dir=root, prefix=".backup-")  # noqa: SIM115
    try:
        summary = create_backup(root, file, include_clips=include_clips)
        size = file.tell()
    except BaseException:
        file.close()
        raise
    detail = f"{summary.files}件のファイル（動画{summary.clips}本）"
    audit(handler, "backup.create", detail)
    name = f"poop-detector-backup-{local_now():%Y%m%d-%H%M%S}.zip"
    handler.stream(
        200,
        _chunks(file),
        "application/zip",
        {"Content-Disposition": f'attachment; filename="{name}"', "Content-Length": str(size)},
    )
