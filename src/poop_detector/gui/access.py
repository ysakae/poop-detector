"""ラベル GUI の閲覧に使う、起動ごとのアクセストークンと Cookie の扱い。

起動時に作るトークンを URL の `token` で一度だけ受け取り、HttpOnly・SameSite=Strict の
Cookie に移す。以後は画面・画像・動画・スクリプトを含むすべての要求で Cookie を検査する。
"""

import hmac
from urllib.parse import parse_qsl, urlencode

COOKIE_NAME = "poop_detector_session"
# URL の問い合わせ文字列で使う名前で、トークンの値ではない。
TOKEN_PARAM = "token"  # noqa: S105


def session_cookie(token: str) -> str:
    """Set-Cookie に入れる値。ブラウザーを閉じると消えるセッション Cookie にする。"""
    return f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict"


def has_session(cookie_header: str | None, token: str) -> bool:
    """Cookie ヘッダーに有効なセッションが含まれるか。

    同じホストの別ポートが残した古い値と並ぶことがあるため、同名の値をすべて比べる。
    """
    found = False
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            # 短絡させず、一致の位置で処理時間が変わらないようにする
            found |= hmac.compare_digest(value.encode(), token.encode())
    return found


def token_in_query(query: str) -> str | None:
    """URL のクエリーに含まれるトークン（なければ None）。"""
    values = [
        value for key, value in parse_qsl(query, keep_blank_values=True) if key == TOKEN_PARAM
    ]
    return values[0] if values else None


def token_matches(candidate: str, token: str) -> bool:
    return hmac.compare_digest(candidate.encode(), token.encode())


def redirect_without_token(path: str, query: str) -> str:
    """トークンを取り除いた同じ画面への移動先。外部へ出る形のパスは "/" に置き換える。"""
    if not path.startswith("/") or path.startswith(("//", "/\\")):
        return "/"
    rest = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != TOKEN_PARAM]
    return path + ("?" + urlencode(rest) if rest else "")


def launch_url(base_url: str, token: str) -> str:
    """ブラウザーで最初に開く、トークン付きの URL。"""
    return f"{base_url}?{urlencode({TOKEN_PARAM: token})}"
