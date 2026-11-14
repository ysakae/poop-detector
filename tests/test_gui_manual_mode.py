"""カメラを使わないときも同じデータに排泄を記録できる。"""

from support import request, submit

from poop_detector.gui.server import make_server


def test_manual_home_hides_live_view_and_links_to_manual_entry(gui):
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
            assert 'href="/records/events?manual=1#manual-record"' in html
            status, page = request(manual, "GET", "/records/events?manual=1")
            assert status == 200
            assert b'<details id="manual-record" open>' in page
            assert (
                submit(
                    manual,
                    "/save",
                    {"action": "manual", "type": "urine", "start": "2024-01-01T08:00"},
                )[0]
                == 303
            )
            assert any(row["excretion_type"] == "urine" for row in store.records())
        finally:
            manual.shutdown()
            thread.join()
    assert "現在のカメラ映像" in request(server, "GET", "/")[1].decode()
