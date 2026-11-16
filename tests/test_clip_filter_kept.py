"""動画のラベルや保護を保存したあとも、一覧の絞り込み（?show=）を保つことを検証する。"""

import re
from urllib.parse import urlencode

from support import exchange, login

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def page(server, path, cookie):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def post(server, cookie, path, fields, page_path):
    html = page(server, page_path, cookie)
    token = re.search(r'name="csrf" value="([^"]+)"', html)[1]
    body = urlencode({**fields, "csrf": token})
    return exchange(server, "POST", path, body, {**FORM, **cookie}, auth=False)


def ready_clip(server, **values):
    clips = server.app.clips
    clip_id = clips.request(**values)
    clips.update(clip_id, status="ready", path=f"clips/{clip_id}.mp4", duration=30)
    return clip_id


def test_forms_carry_the_current_filter(gui):
    server, _, _ = gui
    clip_id = ready_clip(server, source="dog_enter")
    html = page(server, f"/clips?id={clip_id}&show=unlabeled", login(server, role="member"))
    # 1回押す・くわしく入力・保護・評価の候補の各フォームが、絞り込みを持つ。
    assert html.count('<input type="hidden" name="show" value="unlabeled">') >= 3


def test_all_filter_adds_nothing(gui):
    server, _, _ = gui
    clip_id = ready_clip(server, source="dog_enter")
    html = page(server, f"/clips?id={clip_id}", login(server, role="member"))
    assert 'name="show"' not in html


def test_saving_keeps_the_filter(gui):
    server, _, _ = gui
    clip_id = ready_clip(server, source="dog_enter")
    cookie = login(server, role="member")
    path = f"/clips?id={clip_id}&show=unlabeled"
    fields = {"id": clip_id, "label": "none", "activity": "other", "show": "unlabeled"}
    _, headers, _ = post(server, cookie, "/clips/label", fields, path)
    assert headers["Location"] == f"/clips?id={clip_id}&show=unlabeled&saved=1"
    fields = {"id": clip_id, "protected": "1", "show": "labeled"}
    _, headers, _ = post(server, cookie, "/clips/protect", fields, path)
    assert headers["Location"] == f"/clips?id={clip_id}&show=labeled&saved=1"


def test_unknown_filter_is_dropped(gui):
    server, _, _ = gui
    clip_id = ready_clip(server, source="dog_enter")
    cookie = login(server, role="member")
    fields = {"id": clip_id, "label": "none", "activity": "other", "show": "evil&x=1"}
    _, headers, _ = post(server, cookie, "/clips/label", fields, f"/clips?id={clip_id}")
    assert headers["Location"] == f"/clips?id={clip_id}&saved=1"


def test_review_flow_still_moves_to_the_next(gui):
    server, _, _ = gui
    clip_id = ready_clip(server, source="manual")
    cookie = login(server, role="member")
    fields = {"id": clip_id, "label": "none", "activity": "other", "from": "review"}
    fields["show"] = "unlabeled"
    _, headers, _ = post(server, cookie, "/clips/label", fields, f"/clips?id={clip_id}")
    assert headers["Location"] == f"/review/next?after=clip:{clip_id}"
