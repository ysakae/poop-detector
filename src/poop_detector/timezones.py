"""表示と日別集計に使うローカル時刻。

タイムゾーンはプロセスのローカル時刻に従う。設定の timezone は起動時に
config.apply_timezone で TZ 環境変数として反映する。
"""

from datetime import UTC, datetime


def to_local(value):
    """タイムゾーン付きの ISO 8601 文字列をローカル時刻の datetime にする。"""
    return datetime.fromisoformat(value).astimezone()


def local_now():
    return datetime.now(UTC).astimezone()


def local_today():
    return local_now().date()


def timezone_label():
    """画面に表示するタイムゾーンの略称（JST、EST など）。"""
    return local_now().tzname()


def parse_local(value):
    """フォームの日時をタイムゾーン付きにする。時差がなければローカル時刻とみなす。"""
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        # 時差のない datetime の astimezone はローカル時刻として解釈する。
        stamp = stamp.astimezone()
    return stamp
