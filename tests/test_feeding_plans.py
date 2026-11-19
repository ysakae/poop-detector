"""家族で共有する給餌の予定と、未記録のときだけ送る通知を検証する。"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from support import request, submit

from poop_detector.gui.feeding_notifications import notify_due
from poop_detector.records import feeding_plans
from poop_detector.records.care import CareEntry, entries, save_entry
from poop_detector.timezones import local_now, parse_local
from poop_detector.web_push import WebPushNotifier


def due_clocks(database, now):
    return [reminder.clock for reminder in feeding_plans.claim_due(database, now, "端末")]


def test_concurrent_feeding_counts_once_and_extra_feeding_stays_separate(gui):
    _, store, _ = gui
    db = store.database
    now = parse_local("2025-05-02T12:00")
    plan = feeding_plans.save_plan(db, "12:00", now=now - timedelta(days=1))

    def record(actor):
        return save_entry(db, CareEntry("", now, "feeding", actor, "", "", 250), plan_id=plan)

    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = workers.map(record, ["家族A", "家族B"])
    assert first == second
    save_entry(db, CareEntry("", now, "feeding", "家族B", "", "", 50))
    assert sum(entry.amount_dg for entry in entries(db, now.date())) == 300
    assert due_clocks(db, now) == []


def test_deleted_feeding_reopens_the_plan_and_can_be_replaced(gui):
    server, store, _ = gui
    db = store.database
    now = parse_local("2025-05-02T12:00")
    plan = feeding_plans.save_plan(db, "12:00", now=now - timedelta(days=1))
    entry = CareEntry("", now, "feeding", "家族", "", "", 250)
    first = save_entry(db, entry, plan_id=plan)
    assert due_clocks(db, now) == []
    assert submit(server, "/care/delete", {"id": first}, page="/care")[0] == 303
    assert not feeding_plans.plans_for_day(db, now.date())[0].recorded
    assert due_clocks(db, now) == ["12:00"]
    replacement = save_entry(db, entry, plan_id=plan)
    assert replacement != first
    assert [row.id for row in entries(db, now.date())] == [replacement]
    assert feeding_plans.plans_for_day(db, now.date())[0].recorded


def test_quick_feeding_keeps_the_date_when_midnight_passes_during_lock(gui, monkeypatch):
    from contextlib import contextmanager

    from poop_detector.records.care import quick_entry

    _, store, _ = gui
    database = store.database
    before_midnight = parse_local("2025-05-02T23:59:59")
    plan = feeding_plans.save_plan(database, "23:59", now=before_midnight - timedelta(days=1))
    clock = before_midnight
    connect = database.connect

    def trace(sql):
        nonlocal clock
        if sql == "BEGIN IMMEDIATE":
            clock = before_midnight + timedelta(seconds=2)

    @contextmanager
    def tracked_connection():
        with connect() as db:
            db.set_trace_callback(trace)
            yield db

    monkeypatch.setattr(database, "connect", tracked_connection)
    monkeypatch.setattr("poop_detector.records.care.local_now", lambda: clock)
    assert quick_entry(database, "feeding", "家族", plan_id=plan)
    assert feeding_plans.plans_for_day(database, before_midnight.date())[0].recorded
    assert not feeding_plans.plans_for_day(database, clock.date())[0].recorded


def test_reminder_is_claimed_once_and_old_times_are_skipped(gui):
    _, store, _ = gui
    db = store.database
    now = parse_local("2025-05-02T12:00")
    yesterday = now - timedelta(days=1)
    noon = feeding_plans.save_plan(db, "12:00", now=yesterday)
    stopped = feeding_plans.save_plan(db, "11:58", now=yesterday)
    feeding_plans.stop_plan(db, stopped)
    changed = feeding_plans.save_plan(db, "11:59", now=yesterday)
    feeding_plans.save_plan(db, "12:01", plan_id=changed, now=now)

    assert due_clocks(db, now) == ["12:00"]
    assert due_clocks(db, now) == []
    assert due_clocks(db, now + timedelta(minutes=1)) == ["12:01"]
    tomorrow = now + timedelta(days=1)
    assert feeding_plans.plans_for_day(db, tomorrow.date())[0].id == noon
    assert due_clocks(db, tomorrow) == ["12:00"]


def test_feeding_plan_is_available_on_care_page(gui):
    server, store, _ = gui
    assert submit(server, "/care/plans", {"clock": "12:00"}, page="/care")[0] == 303
    html = request(server, "GET", "/care")[1].decode()
    assert "ごはんの予定" in html
    assert "12:00" in html
    assert "未記録" in html
    now = local_now()
    plan = feeding_plans.plans_for_day(store.database, now.date())[0]
    fields = {
        "at": now.strftime("%Y-%m-%dT%H:%M"),
        "kind": "feeding",
        "grams": "25",
        "plan_id": plan.id,
    }
    assert submit(server, "/care", fields, page="/care")[0] == 303
    assert "記録済み" in request(server, "GET", "/care")[1].decode()


def test_feeding_push_recipients_require_opt_in(gui, monkeypatch):
    server, _, _ = gui
    user = server.app.accounts.users.create("受取人", "member")
    transport = server.app.push_subscriptions
    monkeypatch.setattr("poop_detector.web_push.valid_key", lambda *_: True)
    transport.add(user, "https://fcm.googleapis.com/fcm/send/test", "key", "auth")
    assert transport.recipients(feeding=True) == []
    server.app.accounts.users.set_push(user, alerts=False, early=False, feeding=True)
    assert len(transport.recipients(feeding=True)) == 1
    assert transport.recipients() == []


def test_planned_quick_record_from_home_and_stop(gui):
    server, store, _ = gui
    day = local_now().date()
    plan = feeding_plans.save_plan(store.database, "08:00")
    assert "08:00 のごはんを記録" in request(server, "GET", "/")[1].decode()
    fields = {"plan_id": plan, "plan_day": day.isoformat()}
    assert submit(server, "/care/quick", fields, page="/")[0] == 303
    assert submit(server, "/care/quick", fields, page="/")[0] == 303
    assert len(entries(store.database, day)) == 1
    assert "記録済み" in request(server, "GET", "/care")[1].decode()

    edited = {"plan_id": plan, "clock": "08:30"}
    assert submit(server, "/care/plans", edited, page="/care")[0] == 303
    assert feeding_plans.plans_for_day(store.database, day)[0].clock == "08:30"
    assert submit(server, "/care/plans", {"plan_id": plan, "stop": "1"}, page="/care")[0] == 303
    assert feeding_plans.plans_for_day(store.database, day) == []
    assert len(entries(store.database, day)) == 1


def test_due_notification_uses_web_push_only_once_for_opted_in_family(gui, monkeypatch):
    server, store, _ = gui
    now = parse_local("2025-05-02T12:00")
    db = store.database
    feeding_plans.save_plan(db, "12:00", now=now - timedelta(days=1))
    opted_in = server.app.accounts.users.create("通知あり", "member")
    opted_out = server.app.accounts.users.create("通知なし", "member")
    notifier = WebPushNotifier(
        server.app.push_subscriptions, server.app.push_keys, subject="https://example.com"
    )
    monkeypatch.setattr("poop_detector.web_push.valid_key", lambda *_: True)
    for user in (opted_in, opted_out):
        notifier.subscriptions.add(
            user, f"https://fcm.googleapis.com/fcm/send/{user}", "key", "auth"
        )
    server.app.accounts.users.set_push(opted_in, alerts=False, early=False, feeding=True)
    sent = []

    def deliver(subscription, payload, _vapid):
        sent.append((subscription.user_id, json.loads(payload)))
        return "sent"

    monkeypatch.setattr(notifier, "send_to_result", deliver)
    notify_due(db, notifier, now)
    notify_due(db, notifier, now + timedelta(minutes=1))
    assert sent == [
        (
            opted_in,
            {
                "title": "ごはんの時間",
                "body": "12:00 のごはんはまだ記録されていません。",
                "url": "/care?day=2025-05-02",
            },
        )
    ]


def test_stale_home_and_conflicting_plan_times_show_errors(gui):
    server, store, _ = gui
    database = store.database
    plan = feeding_plans.save_plan(database, "08:00")
    assert submit(server, "/care/plans", {"clock": "08:00"}, page="/care")[0] == 400
    assert submit(server, "/care/plans", {"clock": "24:00"}, page="/care")[0] == 400
    yesterday = (local_now() - timedelta(days=1)).date().isoformat()
    fields = {"plan_id": plan, "plan_day": yesterday}
    status, body = submit(server, "/care/quick", fields, page="/")
    assert status == 400
    assert "ホームを開き直して" in body.decode()
    assert not entries(database, local_now().date())


def test_recording_a_past_plan_opens_the_selected_day(gui):
    server, store, _ = gui
    plan = feeding_plans.save_plan(store.database, "08:00")
    yesterday = (local_now() - timedelta(days=1)).date().isoformat()
    status, body = request(server, "GET", f"/care?day={yesterday}&plan={plan}")
    assert status == 200
    assert f'name="at" value="{yesterday}T' in body.decode()
    assert f'value="{plan}" selected' in body.decode()


def test_opening_stale_plan_link_keeps_already_recorded_plan_selected(gui):
    server, store, _ = gui
    db = store.database
    now = local_now()
    plan = feeding_plans.save_plan(db, "08:00")
    link = f"/care?day={now.date()}&plan={plan}"
    save_entry(db, CareEntry("", now, "feeding", "家族A", "", "", 250), plan_id=plan)

    status, body = request(server, "GET", link)
    assert status == 200
    assert f'value="{plan}" selected>08:00 の予定（記録済み）' in body.decode()
    fields = {
        "at": now.strftime("%Y-%m-%dT%H:%M"),
        "kind": "feeding",
        "grams": "25",
        "plan_id": plan,
    }
    assert submit(server, "/care", fields, page="/care")[0] == 303
    assert len(entries(db, now.date())) == 1

    feeding_plans.stop_plan(db, plan)
    status, body = request(server, "GET", link)
    assert status == 400
    assert "このごはんの予定は終了しました" in body.decode()


def test_explicit_push_retry_only_retries_the_unsent_device(gui, monkeypatch):
    server, store, _ = gui
    now = parse_local("2025-05-02T12:00")
    db = store.database
    feeding_plans.save_plan(db, "12:00", now=now - timedelta(days=1))
    user = server.app.accounts.users.create("通知を受け取る家族", "member")
    server.app.accounts.users.set_push(user, alerts=False, early=False, feeding=True)
    notifier = WebPushNotifier(
        server.app.push_subscriptions, server.app.push_keys, subject="https://example.com"
    )
    monkeypatch.setattr("poop_detector.web_push.valid_key", lambda *_: True)
    first = "https://fcm.googleapis.com/fcm/send/first"
    second = "https://fcm.googleapis.com/fcm/send/second"
    for endpoint in (first, second):
        notifier.subscriptions.add(user, endpoint, "key", "auth")
    sent = []

    def deliver(subscription, _payload, _vapid):
        endpoint = subscription.endpoint
        retry = endpoint == second and endpoint not in sent
        sent.append(endpoint)
        return "retry" if retry else "sent"

    monkeypatch.setattr(notifier, "send_to_result", deliver)
    notify_due(db, notifier, now)
    notify_due(db, notifier, now + timedelta(minutes=1))
    notify_due(db, notifier, now + timedelta(minutes=2))
    assert sent.count(first) == 1
    assert sent.count(second) == 2


def test_reminder_remains_due_after_midnight_within_the_grace_period(gui):
    _, store, _ = gui
    db = store.database
    now = parse_local("2025-05-03T00:03")
    feeding_plans.save_plan(db, "23:59", now=now - timedelta(days=1))
    assert due_clocks(db, now) == ["23:59"]
    assert due_clocks(db, now + timedelta(minutes=1)) == []
