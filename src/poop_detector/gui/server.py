"""GUI のサーバー。uvicorn で ASGI アプリ（asgi.py）を動かし、起動と停止を受け持つ。

安全対策とルートへの振り分けは asgi.py にある。待ち受けは 127.0.0.1 だけで、
要求の URL（トークンや画像の ID を含む）はアクセスログに出さない。
"""

import errno
import secrets
import socket
import sys
import threading
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

import uvicorn

from ..capture.live_preview import LivePreview
from ..errors import UserError
from ..records.events import Store
from ..video.clip_storage import DEFAULT_DISK_MIB
from ..video.clips import ClipStore
from .access import launch_url
from .asgi import create_app
from .pages import page_title

# 停止の要求から、処理中の応答（動画の配信など）を待つ上限の秒数。
SHUTDOWN_GRACE_SECONDS = 5


@dataclass(frozen=True)
class GuiApp:
    """ルートが使う依存。起動ごとのトークンもここに持つ。"""

    root: Path
    store: Store
    clips: ClipStore
    preview: LivePreview
    # 保存操作の CSRF 対策に使い、画面のフォームに埋め込む。
    csrf_token: str
    # 閲覧を含むすべての要求の認証に使い、Cookie で受け取る。
    access_token: str
    title: str
    disk_mb: float


class GuiServer:
    """待ち受けのソケットと uvicorn のサーバー。

    ソケットは作った時点で待ち受けを始めるので、serve_forever の前に届いた接続も待たせておける。
    """

    def __init__(self, app: GuiApp, listener: socket.socket) -> None:
        self.app = app
        self.socket = listener
        host, port = listener.getsockname()[:2]
        self.server_address: tuple[str, int] = (host, port)
        self.server_port: int = port
        config = uvicorn.Config(
            create_app(app, port),
            log_config=None,
            log_level="warning",
            access_log=False,
            lifespan="off",
            server_header=False,
            timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
        )
        self._server = uvicorn.Server(config)
        self._started = False
        self._stopped = threading.Event()

    @property
    def access_token(self) -> str:
        return self.app.access_token

    def serve_forever(self, poll_interval: float | None = None) -> None:
        """停止を求められるまで要求を処理する。poll_interval は以前の版との互換のための引数。"""
        del poll_interval
        self._started = True
        try:
            self._server.run(sockets=[self.socket])
        finally:
            self._stopped.set()

    def shutdown(self) -> None:
        """serve_forever を止め、終わるまで待つ。まだ始まっていなければ、始まってすぐに止まる。"""
        self._server.should_exit = True
        if self._started:
            self._stopped.wait()

    def server_close(self) -> None:
        self.socket.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.server_close()


def listen(port: int) -> socket.socket:
    """127.0.0.1 の port で待ち受けるソケット。使用中なら利用者向けの文言で止める。"""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
    except OSError as error:
        listener.close()
        if error.errno == errno.EADDRINUSE:
            raise UserError(
                f"GUIのポート {port} は使用中です。gui --port または run --gui-port で変更してください"
            ) from None
        raise
    return listener


def make_server(
    root: str | Path,
    port: int = 8765,
    *,
    disk_mb: float = DEFAULT_DISK_MIB,
    pet_name: str = "",
) -> GuiServer:
    """127.0.0.1 だけで待ち受ける GUI のサーバーを作る。

    起動ごとに、保存操作の CSRF トークンと、閲覧を含むすべての要求に使うアクセストークンを作る。
    """
    listener = listen(port)
    app = GuiApp(
        root=Path(root),
        store=Store(root),
        clips=ClipStore(root),
        preview=LivePreview(root),
        csrf_token=secrets.token_urlsafe(32),
        access_token=secrets.token_urlsafe(32),
        title=page_title(pet_name),
        disk_mb=disk_mb,
    )
    return GuiServer(app, listener)


def announce(base: str, url: str, *, opened: bool, interactive: bool) -> None:
    """起動の案内を出す。トークン付きの URL は端末に直接出すときだけ表示し、ログに残さない。"""
    if interactive:
        print(f"GUI: {url}", flush=True)  # noqa: T201
        print("この URL は起動中だけ有効なパスワードと同じです。他人に見せないでください。")  # noqa: T201
    elif opened:
        print(f"GUIをブラウザーで開きました: {base}", flush=True)  # noqa: T201
    else:
        print(  # noqa: T201
            "トークン付きの URL は端末にだけ表示します。出力をファイルなどへ送らずに起動するか、"
            "--no-browser を外してブラウザーで開いてください。",
            flush=True,
        )


def open_gui_url(server: GuiServer, open_browser: bool) -> None:
    base = f"http://127.0.0.1:{server.server_port}/"
    url = launch_url(base, server.access_token)
    opened = open_browser and webbrowser.open(url)
    announce(base, url, opened=opened, interactive=sys.stdout.isatty())


@contextmanager
def background_gui(
    root: str | Path,
    port: int,
    open_browser: bool,
    *,
    disk_mb: float,
    pet_name: str,
) -> Iterator[None]:
    """監視中は別スレッドで GUI を提供し、監視が終われば待ち受けを閉じる。"""
    with make_server(root, port, disk_mb=disk_mb, pet_name=pet_name) as server:
        thread = threading.Thread(target=server.serve_forever, name="poop-detector-gui")
        thread.start()
        try:
            open_gui_url(server, open_browser)
            yield
        finally:
            server.shutdown()
            thread.join()


def serve(
    root: str | Path,
    port: int = 8765,
    open_browser: bool = True,
    *,
    disk_mb: float = DEFAULT_DISK_MIB,
    pet_name: str = "",
) -> None:
    with make_server(root, port, disk_mb=disk_mb, pet_name=pet_name) as server:
        open_gui_url(server, open_browser)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("GUIを終了しました")  # noqa: T201
