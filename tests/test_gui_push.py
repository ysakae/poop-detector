"""PWA（マニフェストと Service Worker）と、GUI での Web Push の購読（#89）を検証する。"""

import base64
import json
import re
from urllib.parse import urlencode

from support import exchange, login

from poop_detector.gui.asgi import SECURITY_HEADERS

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
ENDPOINT = "https://fcm.googleapis.com/fcm/send/device-1"
P256DH = base64.urlsafe_b64encode(b"\x04" + b"k" * 64).rstrip(b"=").decode()
AUTH = base64.urlsafe_b64encode(b"a" * 16).rstrip(b"=").decode()


def page(server, cookie, path="/account"):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def post(server, cookie, path, fields):
    token = re.search(r'name="csrf" value="([^"]+)"', page(server, cookie))[1]
    body = urlencode({**fields, "csrf": token})
    return exchange(server, "POST", path, body, {**FORM, **cookie}, auth=False)


def test_manifest_and_icons_are_public(gui):
    server, _, _ = gui
    status, headers, body = exchange(server, "GET", "/manifest.webmanifest", auth=False)
    assert status == 200
    assert headers["Content-Type"].startswith("application/manifest+json")
    manifest = json.loads(body)
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        status, headers, data = exchange(server, "GET", icon["src"], auth=False)
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_service_worker_is_served_from_the_root(gui):
    server, _, _ = gui
    status, headers, body = exchange(server, "GET", "/sw.js", auth=False)
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")
    script = body.decode()
    assert 'addEventListener("push"' in script
    assert 'addEventListener("notificationclick"' in script


def test_pages_link_the_manifest_and_csp_allows_it():
    from poop_detector.gui.templating import render_template

    html = render_template(
        "message.html",
        title="t",
        heading="h",
        message="m",
        back_url="/",
        back_text="b",
        wrap_link=False,
    )
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    policy = SECURITY_HEADERS["Content-Security-Policy"]
    assert "manifest-src 'self'" in policy
    assert "worker-src 'self'" in policy


def test_account_page_offers_web_push(gui):
    server, _, _ = gui
    html = page(server, login(server, role="member"))
    assert "通知（Web Push）" in html
    key = server.app.push_keys.public_key()
    assert f'data-public-key="{key}"' in html
    assert 'src="/assets/push.js"' in html
    assert 'name="alerts" value="1" checked' in html
    assert re.search(r'name="early" value="1">', html)


def test_device_subscribes_and_unsubscribes(gui):
    server, _, _ = gui
    cookie = login(server, "家族B", role="member")
    user = server.app.accounts.users.by_name("家族B")
    fields = {"endpoint": ENDPOINT, "p256dh": P256DH, "auth": AUTH}
    status, _, body = post(server, cookie, "/account/push/subscribe", fields)
    assert (status, json.loads(body)) == (200, {"ok": True})
    assert [s.endpoint for s in server.app.push_subscriptions.for_user(user.id)] == [ENDPOINT]
    # ほかの人は、この端末の購読を消せない。
    other = login(server, role="member")
    post(server, other, "/account/push/unsubscribe", {"endpoint": ENDPOINT})
    assert len(server.app.push_subscriptions.for_user(user.id)) == 1
    status, _, body = post(server, cookie, "/account/push/unsubscribe", {"endpoint": ENDPOINT})
    assert (status, json.loads(body)) == (200, {"ok": True})
    assert server.app.push_subscriptions.for_user(user.id) == []


def test_subscriptions_to_unknown_hosts_are_refused(gui):
    server, _, _ = gui
    cookie = login(server, role="member")
    fields = {"endpoint": "https://192.168.1.10/push", "p256dh": P256DH, "auth": AUTH}
    status, _, body = post(server, cookie, "/account/push/subscribe", fields)
    assert status == 400
    assert json.loads(body)["ok"] is False


def test_preferences_are_saved_per_user(gui):
    server, _, _ = gui
    cookie = login(server, "家族B", role="member")
    status, headers, _ = post(server, cookie, "/account/push/preferences", {"early": "1"})
    assert (status, headers["Location"]) == (303, "/account?push_saved=1")
    user = server.app.accounts.users.by_name("家族B")
    assert (user.push_alerts, user.push_early) == (False, True)
    assert "通知の受け取り方を保存しました" in page(server, cookie, "/account?push_saved=1")


def test_test_push_goes_only_to_my_devices(gui, monkeypatch):
    server, _, _ = gui
    sent = []

    def send_to(self, subscription, payload, vapid):
        sent.append((subscription.endpoint, json.loads(payload)))
        return True

    monkeypatch.setattr("poop_detector.web_push.WebPushNotifier.send_to", send_to)
    cookie = login(server, "家族B", role="member")
    other = login(server, "家族C", role="member")
    users = server.app.accounts.users
    subscriptions = server.app.push_subscriptions
    subscriptions.add(users.by_name("家族B").id, ENDPOINT, P256DH, AUTH)
    subscriptions.add(users.by_name("家族C").id, ENDPOINT + "-c", P256DH, AUTH)
    status, _, body = post(server, cookie, "/account/push/test", {})
    assert status == 200
    assert "テストの通知を送りました" in body.decode()
    assert [endpoint for endpoint, _ in sent] == [ENDPOINT]
    assert sent[0][1]["url"] == "/account"
    status, _, body = post(server, login(server, role="member"), "/account/push/test", {})
    assert status == 400
    assert "この端末で通知を受け取る" in body.decode()
    assert other


def test_only_admins_choose_watchdog_alerts(gui):
    """監視の異常の通知は管理者だけが選べる。一般ユーザーの保存では変わらない（#91）。"""
    server, _, _ = gui
    member = login(server, "家族B", role="member")
    assert 'name="watchdog"' not in page(server, member)
    post(server, member, "/account/push/preferences", {"alerts": "1", "watchdog": "1"})
    users = server.app.accounts.users
    assert users.by_name("家族B").push_watchdog is True

    admin = login(server, "管理者B", role="admin")
    assert 'name="watchdog" value="1" checked' in page(server, admin)
    post(server, admin, "/account/push/preferences", {"alerts": "1"})
    assert users.by_name("管理者B").push_watchdog is False
    post(server, admin, "/account/push/preferences", {"watchdog": "1"})
    user = users.by_name("管理者B")
    assert (user.push_alerts, user.push_watchdog) == (False, True)
