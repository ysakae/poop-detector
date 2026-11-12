"""管理 › 書き出しとバックアップ（CSV・印刷用の集計・バックアップ。#90）を検証する。"""

import csv
import io
import json
import re
import zipfile
from urllib.parse import urlencode

from support import exchange, login

from poop_detector.records.backup import MANIFEST

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
PAGE = "/manage/export"


def page(server, cookie, path=PAGE):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def post(server, cookie, path, fields):
    token = re.search(r'name="csrf" value="([^"]+)"', page(server, cookie))[1]
    body = urlencode({**fields, "csrf": token})
    return exchange(server, "POST", path, body, {**FORM, **cookie}, auth=False)


def audit_actions(server):
    return [entry.action for entry in server.app.accounts.audit.recent()]


def test_page_is_in_the_management_menu_and_admin_only(gui):
    server, _, _ = gui
    html = page(server, login(server), "/manage")
    assert f'href="{PAGE}"' in html
    assert "書き出しとバックアップ" in html
    member = login(server, role="member")
    for path in (PAGE, f"{PAGE}/summary"):
        assert exchange(server, "GET", path, headers=member, auth=False)[0] == 403


def test_csv_download_of_the_chosen_period_is_audited(gui):
    server, store, _ = gui
    store.add_excretion("feces", "2026-11-01T08:00:00+09:00", note="<b>メモ</b>")
    store.add_excretion("urine", "2026-12-01T08:00:00+09:00")
    cookie = login(server)
    status, headers, body = post(
        server, cookie, f"{PAGE}/records.csv", {"from": "2026-11-01", "to": "2026-11-30"}
    )
    assert status == 200
    assert headers["Content-Type"].startswith("text/csv")
    assert "attachment" in headers["Content-Disposition"]
    assert "poop-records-20261101-20261130.csv" in headers["Content-Disposition"]
    table = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    assert len(table) == 2
    assert "<b>メモ</b>" in table[1]
    assert "export.csv" in audit_actions(server)


def test_csv_with_a_bad_period_shows_the_reason(gui):
    server, _, _ = gui
    status, _, body = post(
        server, login(server), f"{PAGE}/records.csv", {"from": "2026-11-30", "to": "2026-11-01"}
    )
    assert status == 400
    assert "期間の終わりは始まり以降にしてください" in body.decode()


def test_print_summary_shows_counts_and_hours(gui):
    server, store, _ = gui
    store.add_excretion("both", "2026-11-02T21:15:00+09:00")
    store.add_excretion("feces", "2026-11-03T07:00:00+09:00")
    cookie = login(server)
    html = page(server, cookie, f"{PAGE}/summary?from=2026-11-01&to=2026-11-07")
    assert "2026/11/01〜2026/11/07" in html
    assert re.search(r"排便</th><td>2", html)
    assert re.search(r"排尿</th><td>1", html)
    assert "21時" in html
    assert "export.summary" in audit_actions(server)


def test_print_summary_rejects_a_bad_period(gui):
    server, _, _ = gui
    status, _, body = exchange(
        server, "GET", f"{PAGE}/summary?from=bad", headers=login(server), auth=False
    )
    assert status == 400
    assert "YYYY-MM-DD" in body.decode()


def test_backup_download_with_and_without_clips(gui):
    server, _, _ = gui
    root = server.app.root
    (root / "roi.json").write_text("{}")
    (root / "clips").mkdir(exist_ok=True)
    (root / "clips" / "one.mp4").write_bytes(b"mp4")
    cookie = login(server)

    status, headers, body = post(server, cookie, f"{PAGE}/backup", {})
    assert status == 200
    assert headers["Content-Type"] == "application/zip"
    assert re.search(
        r'filename="poop-detector-backup-\d{8}-\d{6}\.zip"', headers["Content-Disposition"]
    )
    assert int(headers["Content-Length"]) == len(body)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read(MANIFEST))
    assert {"events.sqlite3", "roi.json"} <= names
    assert "clips/one.mp4" not in names
    assert manifest["include_clips"] is False

    status, _, body = post(server, cookie, f"{PAGE}/backup", {"clips": "1"})
    assert status == 200
    assert "clips/one.mp4" in zipfile.ZipFile(io.BytesIO(body)).namelist()
    assert audit_actions(server).count("backup.create") == 2
    # 一時ファイルを残さない
    assert not list(root.glob(".backup-*"))


def test_page_explains_that_restore_runs_on_the_monitoring_machine(gui):
    server, _, _ = gui
    html = page(server, login(server))
    assert "poop-detector restore" in html
    assert 'name="clips"' in html
