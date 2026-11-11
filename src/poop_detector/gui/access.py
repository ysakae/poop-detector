"""GUI の起動トークンと、セッションの Cookie の扱い。

起動時に作るトークンを URL の `token` で一度だけ受け取り、サーバー側のセッション
（sessions.SessionStore）を始めて、その ID を HttpOnly・SameSite=Strict の Cookie に入れる。
以後は画面・画像・動画・スクリプトを含むすべての要求で、Cookie のセッションを確かめる。
"""

import hmac
from urllib.parse import parse_qsl, urlencode

COOKIE_NAME = "poop_detector_session"
# URL の問い合わせ文字列で使う名前で、トークンの値ではない。
TOKEN_PARAM = "token"  # noqa: S105


def session_cookie(session_id: str, *, secure: bool = False, max_age: int | None = None) -> str:
    """Set-Cookie に入れる値。

    secure は HTTPS で届いた要求のとき。HTTP では送られなくなるので、同じマシンからの
    HTTP の接続では付けない。max_age を省くと、ブラウザーを閉じると消える Cookie になる。
    """
    cookie = f"{COOKIE_NAME}={session_id}; Path=/; HttpOnly; SameSite=Strict"
    if max_age is not None:
        cookie += f"; Max-Age={max_age}"
    return f"{cookie}; Secure" if secure else cookie


def expired_cookie(*, secure: bool = False) -> str:
    """ログアウトで、ブラウザーのセッションの Cookie を消す Set-Cookie の値。"""
    return session_cookie("", secure=secure, max_age=0)


def session_ids(cookie_header: str | None) -> list[str]:
    """Cookie ヘッダーに含まれるセッションの ID。

    同じホストの別ポートが残した古い値と並ぶことがあるため、同名の値をすべて返す。
    """
    found = []
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and value:
            found.append(value)
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
