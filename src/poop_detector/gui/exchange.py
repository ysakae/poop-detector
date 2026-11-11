"""1つの要求の内容と、ルートが返す応答をまとめる。

ルートの関数は Exchange を受け取り、respond・redirect・stream のどれかで応答を決める。
安全のためのヘッダーは、ルートではなく asgi.SecurityHeaders がすべての応答に付ける。
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from starlette.datastructures import Headers
from starlette.responses import Response, StreamingResponse

if TYPE_CHECKING:
    from .server import GuiApp

HTML = "text/html; charset=utf-8"


@dataclass
class Exchange:
    """ルートに渡す要求と、ルートが決めた応答。

    path は問い合わせ文字列を含む、要求の行のままのパス（パーセント符号化を解かない）。
    """

    app: "GuiApp"
    path: str
    headers: Headers
    # 信頼するプロキシの背後では、X-Forwarded-Proto を反映した値（http か https）。
    scheme: str = "http"
    # この要求のセッションの CSRF トークン。画面のフォームに埋め込む。
    csrf: str = ""
    response: Response | None = field(default=None, init=False)

    def respond(
        self,
        status: int,
        data: str | bytes,
        mime: str = HTML,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        body = data.encode() if isinstance(data, str) else data
        self.response = Response(body, status, dict(headers or {}), media_type=mime)

    def redirect(self, location: str, headers: Mapping[str, str] | None = None) -> None:
        """303 で同じサーバーの別の画面へ移す。"""
        self.response = Response(b"", 303, {"Location": location, **(headers or {})})

    def stream(
        self, status: int, chunks: Iterator[bytes], mime: str, headers: Mapping[str, str]
    ) -> None:
        """大きなファイル（動画）を少しずつ返す。Content-Length は headers で渡す。"""
        self.response = StreamingResponse(chunks, status, dict(headers), media_type=mime)
