"""確定記録の期間指定と、関連する記録への移動を確認する。"""

import re
from datetime import date, datetime, timedelta
from html import unescape
from urllib.parse import urlencode

import pytest
from support import csrf_token, exchange, request

from poop_detector.gui import pages
from poop_detector.records.absences import Absences
from poop_detector.records.events import Store
from poop_detector.records.vocal_episodes import VocalEpisodes


@pytest.fixture
def records_gui(gui, monkeypatch):
    monkeypatch.setattr(pages, "local_today", lambda: date(2026, 11, 23))
    return gui


def get_html(server, path):
    status, body = request(server, "GET", path)
    assert status == 200, path
    return body.decode()


def test_records_default_to_seven_local_days(records_gui, monkeypatch):
    server, store, _ = records_gui
    store.add_excretion("feces", "2026-11-16T14:59:59+00:00", note="期間の直前")
    store.add_excretion("feces", "2026-11-16T15:00:00+00:00", note="期間の初日")
    store.add_excretion("urine", "2026-11-23T14:59:59+00:00", note="期間の最終日")
    store.add_excretion("urine", "2026-11-23T15:00:00+00:00", note="期間の直後")

    def fail_on_full_history(_store):
        pytest.fail("記録ページは全期間の確定記録を取得しない")

    monkeypatch.setattr(Store, "records", fail_on_full_history)
    html = get_html(server, "/records")
    assert "2026-11-17〜2026-11-23" in html
    assert html.index("期間の最終日") < html.index("期間の初日")
    assert "期間の直前" not in html
    assert "期間の直後" not in html
    assert "次の7日間</a>" not in html


def test_records_period_can_change_without_changing_statistics(records_gui):
    server, store, _ = records_gui
    store.add_excretion("feces", "2026-11-16T10:00:00+09:00", note="前の期間の排便")
    html = get_html(server, "/records?date=2026-11-23&days=7")
    assert "前の期間の排便" not in html
    previous = unescape(re.search(r'href="([^"]+)">前の7日間</a>', html)[1])
    html = get_html(server, previous.split("#")[0])
    assert "前の期間の排便" in html
    assert "2026-11-10〜2026-11-16" in html
    assert "2026-11-17〜2026-11-23（7日間・記録あり0日）" in html
    assert '<input type="hidden" name="record_end" value="2026-11-16">' in html
    next_week = unescape(re.search(r'href="([^"]+)">次の7日間</a>', html)[1])
    assert "record_end=2026-11-23" in next_week
    assert "今日に戻る</a>" in html


@pytest.mark.parametrize("value", ["invalid", "9999-12-31", "0001-01-01"])
def test_records_invalid_date_uses_default_period(records_gui, value):
    server, _, _ = records_gui
    html = get_html(server, f"/records?record_end={value}&date={value}")
    assert "2026-11-17〜2026-11-23" in html
    assert "この期間の確定記録はありません" in html


@pytest.mark.parametrize(
    ("start", "message"),
    [("2026-11-16T23:10", "記録しました"), ("2026-11-24T00:10", "保存しました")],
)
def test_saving_a_manual_record_outside_the_current_week_opens_the_record(
    records_gui, start, message
):
    server, _, _ = records_gui
    fields = {
        "action": "manual",
        "type": "feces",
        "start": start,
        "note": "排便の確認",
        "csrf": csrf_token(server),
    }
    status, headers, _ = exchange(
        server,
        "POST",
        "/save",
        urlencode(fields),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 303
    html = get_html(server, headers["Location"])
    assert "排便の確認" in html
    assert message in html


@pytest.mark.parametrize(
    ("path", "label"),
    [
        ("/trends", "いつもとの違い"),
        ("/presence", "不在の記録"),
        ("/vocal", "鳴き声の記録"),
        ("/night", "夜間のまとめ"),
    ],
)
def test_record_shortcuts_restore_the_selected_period(records_gui, path, label):
    server, _, _ = records_gui
    html = get_html(server, "/records?date=2026-11-18&days=90&record_end=2026-11-16")
    shortcuts = re.search(r'<nav aria-label="関連する記録".*?</nav>', html, re.S)[0]
    assert html.index(shortcuts) < html.index("1日のうんち・おしっこ回数")
    link = re.search(rf'<a[^>]*href="({path}\?[^"]+)"[^>]*>(.*?)</a>', shortcuts, re.S)
    assert label in link[2]
    assert '<svg class="icon"' in link[2]
    detail = get_html(server, unescape(link[1]))
    back = unescape(re.search(r'href="([^"]+)">記録に戻る</a>', detail)[1])
    assert back == "/records?date=2026-11-18&days=90&record_end=2026-11-16"
    assert "2026-11-10〜2026-11-16" in get_html(server, back)
    if path == "/night":
        night = unescape(re.search(r'href="(/night\?day=[^"]+)"', detail)[1])
        assert back.replace("&", "&amp;") in get_html(server, night)


def test_returning_to_default_records_after_midnight_keeps_the_opened_week(
    records_gui, monkeypatch
):
    server, _, _ = records_gui
    html = get_html(server, "/records")
    link = unescape(re.search(r'href="(/night[^"]*)"', html)[1])
    monkeypatch.setattr(pages, "local_today", lambda: date(2026, 11, 24))
    detail = get_html(server, link)
    back = unescape(re.search(r'href="([^"]+)">記録に戻る</a>', detail)[1])
    assert "2026-11-17〜2026-11-23" in get_html(server, back)


@pytest.mark.parametrize("action", ["/presence/label", "/vocal/label", "/away"])
def test_record_period_survives_actions_on_related_pages(records_gui, action):
    server, store, _ = records_gui
    at = datetime.fromisoformat("2026-11-16T10:00:00+09:00")
    if action == "/vocal/label":
        episodes = VocalEpisodes(store.database)
        episode = episodes.add(at, at + timedelta(seconds=4), 3.0, -12.0)
        clip = episodes.clip_path(server.app.root, episode)
        clip.parent.mkdir(parents=True)
        clip.touch()
        path = "/vocal"
        fields = {"id": str(episode), "label": "dog"}
    else:
        path = "/presence"
        absence = Absences(store.database).start(at, away=False)
        fields = {"id": absence, "label": "walk"}
        if action == "/away":
            fields = {"away": "1", "back": path}

    detail_path = f"{path}?record_end=2026-11-16"
    detail = get_html(server, detail_path)
    form = re.search(rf'action="{action}".*?</form>', detail, re.S)[0]
    assert '<input type="hidden" name="record_end" value="2026-11-16">' in form
    status, headers, _ = exchange(
        server,
        "POST",
        action,
        urlencode({**fields, "record_end": "2026-11-16", "csrf": csrf_token(server, detail_path)}),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 303
    saved = get_html(server, headers["Location"].split("#")[0])
    assert 'href="/records?record_end=2026-11-16">記録に戻る</a>' in saved
