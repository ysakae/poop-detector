"""家族のお世話を記録して、別の家族から依頼日の記録を確認する。"""

from urllib.parse import urlencode

import pytest
from support import login, request


def test_family_shares_care_entries(gui):
    server, _, _ = gui
    first = login(server, name="母", role="member")
    second = login(server, name="父", role="member")
    status, page = request(server, "GET", "/care", headers=first, auth=False)
    assert status == 200
    import re

    csrf = re.search(rb'name="csrf" value="([^"]+)"', page)[1].decode()
    body = urlencode({"csrf": csrf, "kind": "feeding", "at": "2024-03-01T08:00", "note": "30 g"})
    status, _ = request(
        server,
        "POST",
        "/care",
        body=body,
        headers={**first, "Content-Type": "application/x-www-form-urlencoded"},
        auth=False,
    )
    assert status == 303
    status, page = request(server, "GET", "/care?day=2024-03-01", headers=second, auth=False)
    assert status == 200
    assert "給餌" in page.decode()
    assert "30 g" in page.decode()
    assert "母" in page.decode()


def test_feeding_defaults_and_food_history(gui):
    from support import submit

    server, _, _ = gui
    for at, food, grams in (
        ("2024-03-01T08:00", "フードA", "20"),
        ("2024-03-01T12:00", "フードB", "25.5"),
        ("2024-03-01T18:00", "フードA", "30"),
    ):
        assert (
            submit(
                server,
                "/care",
                {"kind": "feeding", "at": at, "food": food, "grams": grams},
                page="/care",
            )[0]
            == 303
        )
    assert (
        submit(
            server,
            "/care",
            {"kind": "sheet", "at": "2024-03-01T20:00", "food": "混入", "grams": "99"},
            page="/care",
        )[0]
        == 303
    )
    status, page = request(server, "GET", "/care?day=2024-03-01")
    assert status == 200
    html = page.decode()
    assert 'name="food" value="フードA"' in html
    assert 'name="grams" value="30"' in html
    assert html.count('<option value="フードA">') == 1
    assert '<option value="フードB">' in html
    assert "25.5 g" in html
    assert "混入" not in html
    other = login(server, role="member")
    assert b'name="grams" value="30"' in request(server, "GET", "/care", headers=other)[1]


def test_edit_feeding_uses_saved_values_and_keeps_invalid_input(gui):
    import re

    from support import submit

    server, _, _ = gui
    fields = {"kind": "feeding", "at": "2024-03-01T08:00", "food": "旧フード", "grams": "18.5"}
    assert submit(server, "/care", fields, page="/care")[0] == 303
    day_url = "/care?day=2024-03-01"
    identifier = re.search(rb"amp;id=([a-f0-9]+)", request(server, "GET", day_url)[1])[1].decode()
    assert (
        submit(
            server,
            "/care",
            {**fields, "at": "2024-03-02T08:00", "food": "新フード", "grams": "25"},
            page="/care",
        )[0]
        == 303
    )
    edit_url = f"{day_url}&id={identifier}"
    assert b'name="grams" value="18.5"' in request(server, "GET", edit_url)[1]
    status, page = submit(
        server,
        "/care",
        {**fields, "id": identifier, "food": "入力中", "grams": "-1", "note": "メモ"},
        page=edit_url,
    )
    assert status == 400
    assert 'name="food" value="入力中"' in page.decode()
    assert 'name="note" maxlength="500" value="メモ"' in page.decode()
    assert (
        submit(server, "/care", {**fields, "id": identifier, "grams": "19"}, page=edit_url)[0]
        == 303
    )
    assert "19 g" in request(server, "GET", day_url)[1].decode()


@pytest.mark.parametrize("kind", ["feeding", "sheet"])
def test_care_deletion_hides_the_record_but_leaves_previous_history(gui, kind):
    from datetime import date

    from support import csrf_token, exchange, submit

    from poop_detector.records.care import (
        CareEntry,
        entries,
        feeding_history,
        last_feeding,
        latest_entries,
        save_entry,
    )
    from poop_detector.timezones import parse_local

    server, store, _ = gui
    database = store.database
    day = date(2024, 3, 1)
    previous = save_entry(
        database,
        CareEntry("", parse_local("2024-03-01T08:00"), kind, "家族", "以前の記録", "前の銘柄", 200),
    )
    removed = save_entry(
        database,
        CareEntry(
            "", parse_local("2024-03-01T10:00"), kind, "家族", "削除する記録", "最新の銘柄", 300
        ),
    )
    edit = f"/care?day={day}&id={removed}"
    assert b'action="/care/delete"' in request(server, "GET", edit)[1]

    status, headers, _ = exchange(
        server,
        "POST",
        "/care/delete",
        urlencode({"id": removed, "csrf": csrf_token(server, edit)}),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert (status, headers["Location"]) == (303, f"/care?day={day}&deleted=1")
    page = request(server, "GET", headers["Location"])[1].decode()
    assert "お世話の記録を削除しました" in page
    assert "削除する記録" not in page
    assert "以前の記録" in page
    assert [entry.id for entry in entries(database, day)] == [previous]
    assert latest_entries(database)[kind].id == previous
    if kind == "feeding":
        assert feeding_history(database) == ("前の銘柄", "20", ["前の銘柄"])
        assert last_feeding(database) == ("前の銘柄", 200)
    fields = {"id": removed, "kind": kind, "at": "2024-03-01T10:00"}
    assert submit(server, "/care", fields, page=edit)[0] == 400
    assert submit(server, "/care/delete", {"id": removed}, page="/care")[0] == 303
    with database.connect() as db:
        assert db.execute("SELECT note FROM care_entries WHERE id=?", (removed,)).fetchone()[0] == (
            "削除する記録"
        )


def test_missing_care_record_cannot_be_deleted(gui):
    from support import submit

    server, _, _ = gui
    assert submit(server, "/care/delete", {"id": "missing"}, page="/care")[0] == 400


def test_quick_care_can_be_recorded_again_right_after_deletion(gui):
    from support import submit

    from poop_detector.records.care import entries, quick_entry
    from poop_detector.timezones import local_today

    server, store, _ = gui
    database = store.database
    first = quick_entry(database, "sheet", "家族")
    assert submit(server, "/care/delete", {"id": first}, page="/care")[0] == 303
    replacement = quick_entry(database, "sheet", "家族")
    assert replacement != first
    assert [entry.id for entry in entries(database, local_today())] == [replacement]


def test_quick_feeding_reads_history_after_an_in_progress_deletion(gui, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event

    from poop_detector.records.care import CareEntry, entries, quick_entry, save_entry
    from poop_detector.timezones import local_today, parse_local

    _, store, _ = gui
    database = store.database
    save_entry(
        database, CareEntry("", parse_local("2024-03-01T08:00"), "feeding", "家族", "", "旧", 200)
    )
    removed = save_entry(
        database, CareEntry("", parse_local("2024-03-01T10:00"), "feeding", "家族", "", "誤", 300)
    )
    connect = database.connect
    writing = Event()

    def trace(sql):
        if sql.startswith(("BEGIN IMMEDIATE", "INSERT INTO care_entries")):
            writing.set()

    @contextmanager
    def tracked_connection():
        with connect() as db:
            db.set_trace_callback(trace)
            yield db

    monkeypatch.setattr(database, "connect", tracked_connection)
    with ThreadPoolExecutor(max_workers=1) as executor, connect() as deletion:
        deletion.execute(
            "UPDATE care_entries SET deleted_at=? WHERE id=?",
            ("2024-03-01T11:00:00+00:00", removed),
        )
        future = executor.submit(quick_entry, database, "feeding", "家族")
        assert writing.wait(2)
        deletion.commit()
        assert future.result(timeout=2)
    (replacement,) = entries(database, local_today())
    assert (replacement.food, replacement.amount_dg) == ("旧", 200)


def test_day_view_is_one_timeline_with_totals(gui):
    """その日のお世話・体重・排泄を、1本の時系列と大きな数字で見せる（#141）。"""
    import re
    from datetime import datetime

    from poop_detector.records.care import CareEntry, save_entry
    from poop_detector.records.weights import parse_day, save_weight
    from poop_detector.timezones import to_local

    server, store, _ = gui
    day = "2024-03-01"
    for hour, kind, amount in ((8, "feeding", 300), (19, "feeding", 255), (9, "sheet", None)):
        at = to_local(datetime.fromisoformat(f"{day}T{hour:02d}:00:00+09:00").isoformat())
        save_entry(store.database, CareEntry("", at, kind, "母", "", "", amount))
    save_weight(store.database, "", parse_day(day), 1850, "")
    store.add_excretion("urine", f"{day}T10:00:00+09:00")
    html = request(server, "GET", f"/care?day={day}")[1].decode()
    stats = dict(re.findall(r'([^<>]+)</span><strong class="stat-value">([^<]+)<span', html))
    assert stats["ごはん"] == "55.5"
    assert stats["シート交換"] == "1"
    listing = html.split('<ul class="timeline day-list">', 1)[1].split("</ul>", 1)[0]
    order = [listing.index(text) for text in ("19:00", "10:00", "09:00", "08:00")]
    assert order == sorted(order)
    for text in ("給餌", "シート交換", "排尿", "体重", "1,850 g", "25.5 g", "母"):
        assert text in listing, text
    assert listing.count('<svg class="icon"') == 5


# ホームから1回押して記録する（#162）。前回と同じ給餌と、シート交換。
def quick(server, cookie, kind):
    import re
    from urllib.parse import urlencode

    from support import exchange

    html = exchange(server, "GET", "/", headers=cookie, auth=False)[2].decode()
    token = re.search(r'name="csrf" value="([^"]+)"', html)[1]
    form = {"Content-Type": "application/x-www-form-urlencoded", **cookie}
    body = urlencode({"kind": kind, "csrf": token})
    status, headers, _ = exchange(server, "POST", "/care/quick", body, form, auth=False)
    return status, headers.get("Location", "")


def home(server, cookie, path="/"):
    from support import exchange

    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def test_home_repeats_the_last_feeding_in_one_tap(gui):
    import re

    from poop_detector.records.care import CareEntry, entries, save_entry
    from poop_detector.timezones import local_now, local_today

    server, store, _ = gui
    mother = login(server, name="母", role="member")
    earlier = local_now().replace(microsecond=0)
    save_entry(store.database, CareEntry("", earlier, "feeding", "父", "", "フードA", 305))
    html = home(server, mother)
    form = re.search(r'<form method="post" action="/care/quick".*?</form>', html, re.S)[0]
    assert '<button class="secondary" name="kind" value="feeding">' in form
    assert "前回と同じごはん" in form
    assert "フードA" in form
    assert "30.5 g" in form
    status, location = quick(server, mother, "feeding")
    assert status == 303
    rows = [row for row in entries(store.database, local_today()) if row.kind == "feeding"]
    assert len(rows) == 2
    new = next(row for row in rows if row.actor == "母")
    assert (new.food, new.amount_dg) == ("フードA", 305)
    assert location == f"/?recorded={new.id}"
    done = home(server, mother, location)
    notice = re.search(r'<p class="message" role="status">.*?</p>', done, re.S)[0]
    assert "ごはん（フードA ／ 30.5 g）を記録しました" in notice
    assert f'href="/care?day={local_today().isoformat()}&amp;id={new.id}"' in notice
    # ほかの人や古い ID では、知らせを出さない。
    assert 'class="message"' not in home(server, mother, "/?recorded=unknown")


def test_home_records_a_sheet_change_in_one_tap(gui):
    from poop_detector.records.care import entries
    from poop_detector.timezones import local_today

    server, store, _ = gui
    cookie = login(server, name="母", role="member")
    assert '<button class="secondary" name="kind" value="sheet">' in home(server, cookie)
    status, location = quick(server, cookie, "sheet")
    assert status == 303
    (row,) = entries(store.database, local_today())
    assert (row.kind, row.actor) == ("sheet", "母")
    assert "シート交換を記録しました" in home(server, cookie, location)


def test_quick_feeding_without_a_previous_one_opens_the_form(gui):
    from poop_detector.records.care import entries
    from poop_detector.timezones import local_today

    server, store, _ = gui
    cookie = login(server, role="member")
    assert 'name="kind" value="feeding"' not in home(server, cookie)
    assert quick(server, cookie, "feeding") == (303, "/care#feeding")
    assert entries(store.database, local_today()) == []


def test_quick_record_ignores_a_double_tap_and_unknown_kinds(gui):
    from poop_detector.records.care import entries
    from poop_detector.timezones import local_today

    server, store, _ = gui
    cookie = login(server, name="母", role="member")
    first = quick(server, cookie, "sheet")
    assert quick(server, cookie, "sheet") == first
    assert len(entries(store.database, local_today())) == 1
    # 別の人が続けて記録したものは、別の記録として残す。
    assert quick(server, login(server, name="父", role="member"), "sheet")[0] == 303
    assert len(entries(store.database, local_today())) == 2
    assert quick(server, cookie, "weight")[0] == 400


def test_time_field_is_to_the_minute(gui):
    # お世話の日時は分まででよく、秒の入力欄を出さない。
    import re

    from support import submit

    server, _, _ = gui
    page = request(server, "GET", "/care")[1].decode()
    field = re.search(r'<input type="datetime-local" name="at"[^>]*>', page)[0]
    assert 'step="1"' not in field
    assert re.search(r'value="\d{4}-\d\d-\d\dT\d\d:\d\d"', field)
    fields = {"kind": "feeding", "at": "2024-03-01T08:05", "food": "フード", "grams": "20"}
    assert submit(server, "/care", fields, page="/care")[0] == 303
    day_url = "/care?day=2024-03-01"
    identifier = re.search(rb"amp;id=([a-f0-9]+)", request(server, "GET", day_url)[1])[1].decode()
    edit = request(server, "GET", f"{day_url}&id={identifier}")[1].decode()
    assert 'name="at" value="2024-03-01T08:05"' in edit
