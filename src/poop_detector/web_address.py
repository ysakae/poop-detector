"""GUI の待ち受けと、外から開くときの URL・信頼するプロキシの検証と、許す Host と Origin。

インターネット経由で使うときは、HTTPS を終端するリバースプロキシやトンネルの背後で動かす。
外部の URL はパスキーの RP ID と Cookie の基準になるので、HTTPS のドメイン名に限る。
"""

import ipaddress
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Self
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .config import Config

LOCAL_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# リバースプロキシを同じマシンで動かす構成を既定にする。
DEFAULT_TRUSTED_PROXIES = "127.0.0.1"
# 開発用に、同じマシンのブラウザーからだけ HTTP の外部 URL を許す名前。
DEVELOPMENT_HOSTS = ("localhost",)


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def valid_listen_host(value: str) -> bool:
    """待ち受けのアドレス。名前の解決に頼らないよう、IP アドレスだけを受け付ける。"""
    return _is_ip(value)


def valid_proxies(value: str) -> bool:
    """カンマ区切りの IP アドレスかネットワーク。空なら転送のヘッダーを信頼しない。

    すべてを信頼する "*" は、偽の X-Forwarded-Proto で HTTPS を装えるので受け付けない。
    """
    if not value.strip():
        return True
    try:
        for part in value.split(","):
            ipaddress.ip_network(part.strip(), strict=False)
    except ValueError:
        return False
    return True


def proxy_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _split_public_url(value: str) -> tuple[str, str, int | None] | None:
    """外部の URL を (scheme, 小文字のホスト名, 明示したポート) に分ける。使えなければ None。"""
    try:
        url = urlsplit(value.strip())
        port = url.port
    except ValueError:
        return None
    host = (url.hostname or "").lower()
    if url.scheme not in ("https", "http") or not host or _is_ip(host):
        return None
    if url.username is not None or url.password is not None:
        return None
    if url.path not in ("", "/") or url.query or url.fragment:
        return None
    if url.scheme == "http" and host not in DEVELOPMENT_HOSTS:
        return None
    return url.scheme, host, port


def valid_public_url(value: str) -> bool:
    """空（外から開かない）か、HTTPS のドメイン名（開発用に http://localhost も可）。"""
    return not value or _split_public_url(value) is not None


@dataclass(frozen=True)
class WebOptions:
    """GUI の待ち受けと外部の URL。設定ファイルの gui セクションから作る。"""

    host: str = LOCAL_HOST
    port: int = DEFAULT_PORT
    public_url: str = ""
    trusted_proxies: str = DEFAULT_TRUSTED_PROXIES
    session_idle_hours: float = 168
    session_max_days: float = 30
    # テーマを選んでいない人とログイン前の画面に使うデザインテーマ。
    theme: str = "professional"

    @classmethod
    def from_config(cls, config: "Config", *, port: int | None = None) -> Self:
        """設定から作る。port はコマンドの引数（--port など）で上書きするときに渡す。"""
        return cls(
            host=config.gui_host,
            port=config.gui_port if port is None else port,
            public_url=config.gui_public_url,
            trusted_proxies=config.gui_trusted_proxies,
            session_idle_hours=config.gui_session_idle_hours,
            session_max_days=config.gui_session_max_days,
            theme=config.gui_theme,
        )


# 既定の待ち受け（同じマシンだけ）。関数の引数の既定値に使う。
LOCAL_ONLY = WebOptions()


@dataclass(frozen=True)
class Site:
    """実際に待ち受けたポートと外部の URL から決まる、受け付ける Host と Origin。"""

    port: int
    public_url: str = ""

    @cached_property
    def _public(self) -> tuple[str, str, int | None] | None:
        return _split_public_url(self.public_url) if self.public_url else None

    @cached_property
    def hosts(self) -> frozenset[str]:
        """Host ヘッダーとして受け付ける値（小文字）。"""
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if self._public is not None:
            scheme, host, port = self._public
            default = 443 if scheme == "https" else 80
            if port is None or port == default:
                hosts |= {host, f"{host}:{default}"}
            else:
                hosts.add(f"{host}:{port}")
        return frozenset(hosts)

    @cached_property
    def origins(self) -> frozenset[str]:
        """POST の Origin ヘッダーとして受け付ける値。"""
        origins = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
        if self.public_base is not None:
            origins.add(self.public_base.rstrip("/"))
        return frozenset(origins)

    @cached_property
    def public_base(self) -> str | None:
        """外から開くときの URL（末尾は /）。設定していなければ None。"""
        if self._public is None:
            return None
        scheme, host, port = self._public
        default = 443 if scheme == "https" else 80
        netloc = host if port is None or port == default else f"{host}:{port}"
        return f"{scheme}://{netloc}/"
