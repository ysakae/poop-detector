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
    assert feeding_plans.claim_due(db, now) == []


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

    assert feeding_plans.claim_due(db, now) == ["12:00"]
    assert feeding_plans.claim_due(db, now) == []
    assert feeding_plans.claim_due(db, now + timedelta(minutes=1)) == ["12:01"]
    tomorrow = now + timedelta(days=1)
    assert feeding_plans.plans_for_day(db, tomorrow.date())[0].id == noon
    assert feeding_plans.claim_due(db, tomorrow) == ["12:00"]


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
        return True

    monkeypatch.setattr(notifier, "send_to", deliver)
    notify_due(db, notifier, now)
    notify_due(db, notifier, now + timedelta(minutes=1))
    assert sent == [
        (
            opted_in,
            {
                "title": "ごはんの時間",
                "body": "12:00 のごはんはまだ記録されていません。",
                "url": "/care",
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
