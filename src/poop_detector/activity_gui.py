"""登録した床面に滞在時間と通過回数を重ねる。"""

import sqlite3
from datetime import date
from functools import partial
from html import escape

from .activity_map import GRID, ActivityLayout, ActivityStore
from .timezones import local_today, timezone_label

PERIODS = ("1", "7", "30")


def heatmap(layout, cells, mode):
    metric = 3 if mode == "pass" else 2
    total = sum(row[metric] for row in cells)
    color = "#f29527" if mode == "pass" else "#0086a0"
    unit = "回" if mode == "pass" else "秒"
    content = (
        f'<svg viewBox="0 0 {GRID} {GRID}" role="img" aria-label="サークルの行動ヒートマップ" '
        'style="display:block;width:min(100%,560px);height:auto;background:#263c46;border-radius:8px">'
    )
    outline = layout.map_outline()
    if outline is not None:
        points = " ".join(f"{round(x * GRID, 5):g},{round(y * GRID, 5):g}" for x, y in outline)
        content += (
            f'<defs><clipPath id="activity-floor"><polygon points="{points}"/></clipPath></defs>'
            '<g clip-path="url(#activity-floor)">'
        )
    content += (
        f'<image href="/activity/background?id={layout.id}" width="{GRID}" height="{GRID}" '
        'preserveAspectRatio="none" opacity=".6"/>'
    )
    for row in cells:
        amount = row[metric]
        if amount <= 0:
            continue
        x, y = row[:2]
        percent = 100 * amount / total
        opacity = min(0.9, 0.2 + 0.7 * percent / 10)
        content += (
            f'<rect x="{x}" y="{y}" width="1" height="1" fill="{color}" opacity="{opacity:.3f}">'
            f"<title>{amount:.0f}{unit}（全体の{percent:.1f}％）</title></rect>"
        )
    if outline is not None:
        content += "</g>"
    return content + "</svg>"


def render_activity(root, query, *, title=None):
    from .label_gui import document

    page = partial(document, title=title)

    content = '<h2>サークルの行動マップ</h2><p><a href="/">ライブ映像・記録へ戻る</a></p>'
    try:
        layout = ActivityLayout.load(root)
    except (OSError, ValueError, TypeError, KeyError):
        return page(
            content
            + "<p>行動マップを使うには、先に <code>uv run poop-detector setup-activity-map</code>"
            " で床面を登録し、監視に <code>--activity-map</code> を付けて起動してください。</p>"
        )
    try:
        last_day = date.fromisoformat(query.get("date", [""])[0])
    except ValueError:
        last_day = local_today()
    days = query.get("days", ["7"])[0]
    if days not in PERIODS:
        days = "7"
    first_day = date.fromordinal(max(1, last_day.toordinal() - int(days) + 1))
    mode = "pass" if query.get("mode") == ["pass"] else "stay"
    periods = "".join(
        f'<option value="{value}"{" selected" if value == days else ""}>{value}日間</option>'
        for value in PERIODS
    )
    stay = " selected" if mode == "stay" else ""
    passing = " selected" if mode == "pass" else ""
    content += (
        '<form action="/activity" method="get" class="toolbar">'
        f'<label>表示<select name="mode"><option value="stay"{stay}>滞在時間</option>'
        f'<option value="pass"{passing}>通過回数</option></select></label>'
        f'<label>集計終了日 <input type="date" name="date" value="{last_day}" required></label>'
        f'<label>期間<select name="days">{periods}</select></label><button>表示</button></form>'
        f"<p>{first_day}〜{last_day}（{escape(timezone_label())}）</p>"
    )
    try:
        cells, sampled, located = ActivityStore(root).summary(layout.id, first_day, last_day)
    except sqlite3.Error:
        return page(content + "<p>集計を読み込めませんでした。画面を更新してください。</p>")
    if not sampled:
        content += "<p>この期間の集計データはまだありません。</p>"
    else:
        content += (
            f"<p>観測 {sampled / 60:.1f}分 ／ 位置を確認できた時間 {located / 60:.1f}分"
            f" ／ 通過 {sum(row[3] for row in cells)}回</p>"
        )
    content += heatmap(layout, cells, mode)
    return page(
        content
        + '<p class="note">背景は床面の登録時の映像です。色が濃いほどその区画の割合が高く、10％以上で最も濃くなります。'
        "滞在時間は検出枠の中心があった時間です。通過回数は別の区画に移った回数で、"
        "途中で位置を確認できなかった区間はつなぎません。</p>"
    )
