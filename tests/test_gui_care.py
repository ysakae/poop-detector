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
