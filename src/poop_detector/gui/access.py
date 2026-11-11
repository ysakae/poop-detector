"""GUI のセッションの Cookie の扱い。

パスキーでログインするとサーバー側のセッション（sessions.SessionStore）を始め、その ID を
HttpOnly・SameSite=Strict の Cookie に入れる。以後は画面・画像・動画・スクリプトを含む
すべての要求で、Cookie のセッションを確かめる。
"""

COOKIE_NAME = "poop_detector_session"


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
