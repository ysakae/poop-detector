"""GUI のサーバー。uvicorn で ASGI アプリ（asgi.py）を動かし、起動と停止を受け持つ。

安全対策とルートへの振り分けは asgi.py にある。待ち受けは既定で 127.0.0.1 だけで、
外から開くときは設定の gui.public_url と、HTTPS を終端するリバースプロキシを使う。
要求の URL（登録用 URL の値や画像の ID を含む）はアクセスログに出さない。
"""

import base64
import errno
import hashlib
import hmac
import ipaddress
import logging
import secrets
import socket
import threading
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self

import uvicorn

from ..capture.live_preview import LivePreview
from ..errors import UserError
from ..records.database import events_database
from ..records.events import Store
from ..video.clip_storage import DEFAULT_DISK_MIB
from ..video.clips import ClipStore
from ..web_address import LOCAL_ONLY, Site, WebOptions, proxy_list
from .accounts import Accounts
from .asgi import create_app
from .pages import page_title
from .passkeys import PasskeyAuth
from .rate_limit import RateLimits
from .sessions import SessionPolicy, SessionStore
from .snapshots import Snapshots

logger = logging.getLogger(__name__)
DEFAULT_LIMITS = RateLimits()

# 停止の要求から、処理中の応答（動画の配信など）を待つ上限の秒数。
SHUTDOWN_GRACE_SECONDS = 5


@dataclass(frozen=True)
class GuiApp:
    """ルートが使う依存。起動ごとのトークンもここに持つ。"""

    root: Path
    store: Store
    clips: ClipStore
    preview: LivePreview
    # セッションごとの CSRF トークンを導く鍵（起動ごとに作る）。
    csrf_secret: bytes
    sessions: SessionStore
    accounts: Accounts
    passkeys: PasskeyAuth
    # 受け付ける Host と Origin、パスキーの RP（実際に待ち受けたポートから決まる）。
    site: Site
    title: str
    disk_mb: float
    # 管理 › 設定で書き換える設定ファイル。None なら設定の画面は表示だけ（テストなど）。
    config_path: Path | None = None
    # テーマを選んでいない人とログイン前の画面のテーマ（設定の gui.theme）。
    default_theme: str = "professional"
    # 範囲の登録で画面に見せた映像の一時置き場（保存にはその映像を使う）。
    snapshots: Snapshots = field(default_factory=Snapshots)

    def csrf_for(self, session_id: str) -> str:
        """セッションの CSRF トークン。セッションの ID と鍵の HMAC で、ID そのものは出さない。"""
        digest = hmac.new(self.csrf_secret, session_id.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class GuiServer:
    """待ち受けのソケットと uvicorn のサーバー。

    ソケットは作った時点で待ち受けを始めるので、serve_forever の前に届いた接続も待たせておける。
    """

    def __init__(
        self, app: GuiApp, listener: socket.socket, web: WebOptions, limits: RateLimits
    ) -> None:
        self.app = app
        self.socket = listener
        host, port = listener.getsockname()[:2]
        self.server_address: tuple[str, int] = (host, port)
        self.server_port: int = port
        self.site = app.site
        proxies = proxy_list(web.trusted_proxies)
        config = uvicorn.Config(
            create_app(app, self.site, limits),
            # 信頼するプロキシからの X-Forwarded-For・X-Forwarded-Proto だけを反映する。
            proxy_headers=bool(proxies),
            forwarded_allow_ips=proxies or None,
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


def listen(host: str, port: int) -> socket.socket:
    """host の port で待ち受けるソケット。使用中なら利用者向けの文言で止める。"""
    family = socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
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
    web: WebOptions = LOCAL_ONLY,
    limits: RateLimits = DEFAULT_LIMITS,
    config_path: str | Path | None = None,
) -> GuiServer:
    """GUI のサーバーを作る。待ち受けのアドレスと外部の URL は web、ポートは port を使う。

    起動ごとに、CSRF トークンを導く鍵と、セッションを始めるための起動トークンを作る。
    limits は要求の回数の上限（テストで小さくする）。config_path は管理 › 設定で書き換える
    設定ファイル（起動時の作業ディレクトリから絶対パスにする）。
    """
    listener = listen(web.host, port)
    database = events_database(root)
    accounts = Accounts(database)
    if not ipaddress.ip_address(web.host).is_loopback and not web.public_url:
        logger.warning(
            "GUI を %s で待ち受けます。外から開く場合は gui.public_url に https の URL を設定し、"
            "HTTPS を終端するリバースプロキシを通してください",
            web.host,
        )
    app = GuiApp(
        root=Path(root),
        store=Store(root),
        clips=ClipStore(root),
        preview=LivePreview(root),
        csrf_secret=secrets.token_bytes(32),
        sessions=SessionStore(
            database, SessionPolicy(web.session_idle_hours, web.session_max_days)
        ),
        accounts=accounts,
        passkeys=PasskeyAuth(accounts),
        site=Site(listener.getsockname()[1], web.public_url),
        title=page_title(pet_name),
        disk_mb=disk_mb,
        config_path=Path(config_path).resolve() if config_path is not None else None,
        default_theme=web.theme,
    )
    return GuiServer(app, listener, web, limits)


def announce(base: str, *, opened: bool, public_url: str | None, has_users: bool) -> None:
    """起動の案内を出す。URL に秘密は含まないので、ログやファイルに残ってもよい。

    opened はブラウザーで開いたか。外部の URL があればそちらを、なければ base を開いている。
    """
    mark = "（ブラウザーで開きました）" if opened else ""
    print(f"GUI: {base}{'' if public_url else mark}", flush=True)  # noqa: T201
    if public_url:
        print(f"外から開くとき: {public_url}{mark}", flush=True)  # noqa: T201
    if not has_users:
        print(  # noqa: T201
            "まだユーザーがいません。このマシンで `poop-detector users invite 名前 --role admin` "
            "を実行し、表示された URL から管理者のパスキーを登録してください。",
            flush=True,
        )


def open_gui_url(server: GuiServer, open_browser: bool) -> None:
    """GUI の URL を案内し、必要ならブラウザーで開く。

    パスキーは登録したドメインでだけ使えるので、外部の URL（gui.public_url）があればそちらを
    開く。なければ、IP アドレスでは使えないパスキーのために localhost で開く。
    """
    base = f"http://localhost:{server.server_port}/"
    public_url = server.site.public_base
    opened = open_browser and webbrowser.open(public_url or base)
    announce(
        base,
        opened=opened,
        public_url=public_url,
        has_users=bool(server.app.accounts.users.list()),
    )


@contextmanager
def background_gui(
    root: str | Path,
    port: int,
    open_browser: bool,
    *,
    disk_mb: float,
    pet_name: str,
    web: WebOptions = LOCAL_ONLY,
    config_path: str | Path | None = None,
) -> Iterator[None]:
    """監視中は別スレッドで GUI を提供し、監視が終われば待ち受けを閉じる。"""
    with make_server(
        root, port, disk_mb=disk_mb, pet_name=pet_name, web=web, config_path=config_path
    ) as server:
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
    web: WebOptions = LOCAL_ONLY,
    config_path: str | Path | None = None,
) -> None:
    with make_server(
        root, port, disk_mb=disk_mb, pet_name=pet_name, web=web, config_path=config_path
    ) as server:
        open_gui_url(server, open_browser)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("GUIを終了しました")  # noqa: T201
