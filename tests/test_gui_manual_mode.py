"""カメラを使わないときも同じデータにトイレを記録できる。"""

import pytest
from support import request, submit

from poop_detector.gui.server import make_server


@pytest.mark.parametrize("submitted_action", ["manual", "missed"])
def test_manual_home_hides_live_view_and_links_to_manual_entry(gui, submitted_action):
    server, store, _ = gui
    with make_server(store.root, 0, manual_only=True) as manual:
        from threading import Thread

        thread = Thread(target=manual.serve_forever, daemon=True)
        thread.start()
        try:
            status, page = request(manual, "GET", "/")
            assert status == 200
            html = page.decode()
            assert "手動記録モード" in html
            assert "現在のカメラ映像" not in html
            assert 'href="/records/new"' in html
            status, page = request(manual, "GET", "/records/new")
            assert status == 200
            html = page.decode()
            assert "トイレを記録" in html
            assert "通知は来ましたか？" not in html
            assert (
                'href="/review"'
                not in html.split('<form method="post"', 1)[0].split("<h2>トイレを記録</h2>", 1)[1]
            )
            assert '<input type="hidden" name="action" value="manual">' in html
            assert (
                submit(
                    manual,
                    "/save",
                    {"action": submitted_action, "type": "urine", "start": "2024-01-01T08:00"},
                )[0]
                == 303
            )
            assert [row["excretion_type"] for row in store.records()] == ["urine"]
            assert store.metrics()["missed"] == 0
        finally:
            manual.shutdown()
            thread.join()
    assert "現在のカメラ映像" in request(server, "GET", "/")[1].decode()


def test_camera_record_still_offers_missed_option(gui):
    server, _, _ = gui
    status, page = request(server, "GET", "/records/new")
    assert status == 200
    assert "通知は来ましたか？" in page.decode()
    assert 'value="missed"' in page.decode()
