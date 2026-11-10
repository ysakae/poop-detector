"""テスト全体で共有する準備処理。"""

import os
import time

import pytest


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
