"""家族のお世話を記録して、別の家族から依頼日の記録を確認する。"""

from urllib.parse import urlencode

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
