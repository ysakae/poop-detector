"""テスト全体で共有する準備処理。"""

import os
import threading
import time

import numpy as np
import pytest

from poop_detector.events import Store


@pytest.fixture(autouse=True)
def fixed_local_timezone():
    """実行するマシンのタイムゾーンに左右されないよう、日本時間に固定する。

    テスト中に apply_timezone で TZ を変えても、次のテストへ持ち越さない。
    monkeypatch は他の fixture と共有されるため使わず、自前で戻す。
    """
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    yield
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


@pytest.fixture
def gui(tmp_path):
    """一時ディレクトリで動くラベル GUI と、イベントを1件入れた記録を用意する。

    (server, store, event_id) を返し、テストの後にサーバーを止める。
    """
    from poop_detector.gui.app import make_server

    store = Store(tmp_path)
    event = store.record(np.full((50, 50, 3), 150, np.uint8), 0.1, None, 1)
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, store, event
    server.shutdown()
    server.server_close()
    thread.join()
