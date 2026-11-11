"""表示とログから、URL に含まれる認証情報を取り除く。"""

import logging
import re

HIDDEN = "<伏せ字>"
# カメラの URL は、認証情報のほかにも機器の場所やストリーム名を含むので丸ごと伏せる。
_STREAM_URL = re.compile(r"\b(rtsps?|rtmps?)://[^\s'\"<>]+", re.IGNORECASE)
# ほかの URL は、ユーザー名とパスワード（user:pass@）だけを伏せる。
_USERINFO = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/@'\"<>]+@", re.IGNORECASE)


def redact(text: str) -> str:
    """文字列の中の URL から認証情報を伏せた文字列を返す。"""
    text = _STREAM_URL.sub(lambda match: f"{match.group(1)}://{HIDDEN}", text)
    return _USERINFO.sub(lambda match: f"{match.group(1)}{HIDDEN}@", text)


class RedactingFormatter(logging.Formatter):
    """トレースバックを含む、整形後のログ全体を伏せ字にする。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def install_log_redaction() -> None:
    """どのハンドラーに渡るログでも、本文を伏せ字にしてから記録する。

    ハンドラーの整形より前（記録を作る時点）で置き換えるので、ほかのライブラリが
    付けたハンドラーにも効く。トレースバックは RedactingFormatter が伏せる。
    何度呼んでも、伏せ字の処理は1重にしかならない。
    """
    original = logging.getLogRecordFactory()
    if getattr(original, "redacting", False):
        return

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = original(*args, **kwargs)
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # 書式と引数が合わない記録は、出力時に logging が報告するのでそのまま渡す。
            return record
        hidden = redact(message)
        if hidden != message:
            record.msg, record.args = hidden, None
        return record

    # 関数に印を付けて二重の登録を防ぐ。関数オブジェクトの属性は型スタブにない。
    factory.redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
