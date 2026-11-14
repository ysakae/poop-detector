"""画面の共通の枠（メニュー、ログイン中の人、監視の状態）の値。

画面の配置は docs/screens.md の表に合わせる。メニューは見せ方だけで、管理者だけの画面を
断るのは asgi.dispatch（ルートの表の役割）が行う。
"""

from collections.abc import Callable
from dataclasses import dataclass

# 全員が使う場所。(URL, 名前)。URL の下の画面（/clips?id=… など）も同じ場所として扱う。
MEMBER_SECTIONS: tuple[tuple[str, str], ...] = (
    ("/", "ホーム"),
    ("/review", "確認待ち"),
    ("/records", "記録"),
    ("/clips", "動画"),
    ("/activity", "行動マップ"),
)
MANAGEMENT = ("/manage", "管理")
# 管理の中の画面。ここに並ぶ URL はすべて管理者だけのルートにする（test_gui_roles.py が確かめる）。
MANAGEMENT_PAGES: tuple[tuple[str, str], ...] = (
    ("/manage", "稼働状況とトレー"),
    ("/manage/areas", "範囲の登録"),
    ("/manage/settings", "設定"),
    ("/manage/checks", "確認と診断"),
    ("/manage/models", "判定器とモデル"),
    ("/manage/storage", "動画の保存"),
    ("/manage/export", "書き出しとバックアップ"),
    ("/manage/users", "ユーザー"),
    ("/manage/invite", "招待"),
    ("/manage/audit", "監査ログ"),
)
# 監視の状態（LivePreview.monitor_status の値）の表示。(文言, 見た目の区分)。
MONITOR_STATES: dict[str, tuple[str, str]] = {
    "manual": ("手動記録モード", "off"),
    "connected": ("稼働中", "ok"),
    "searching": ("カメラを再接続中", "warn"),
}
MONITOR_UNKNOWN = ("停止中または未確認", "off")


@dataclass(frozen=True)
class Viewer:
    """画面を見ている人。asgi.dispatch がログインした要求ごとに作る。

    monitor（監視の状態）と pending（確認待ちの件数）は、画面を描くときだけ呼ぶ関数
    （画像などの要求では読まない）。
    """

    name: str
    admin: bool
    path: str
    monitor: Callable[[], str]
    pending: Callable[[], int]


@dataclass(frozen=True)
class MenuItem:
    """メニューの1項目。current はいま開いている場所、badge は名前の横に出す件数。"""

    href: str
    text: str
    current: bool
    badge: int | None = None


@dataclass(frozen=True)
class Frame:
    """共通の枠に出す値。"""

    person: str
    monitor: str
    monitor_state: str
    menu: tuple[MenuItem, ...]
    management: tuple[MenuItem, ...]


def _within(path: str, section: str) -> bool:
    if section == "/":
        return path == "/"
    return path == section or path.startswith(section + "/")


def main_menu(path: str, admin: bool, pending: int = 0) -> tuple[MenuItem, ...]:
    """pending が1件以上なら、確認待ちに件数を出す。"""
    sections = MEMBER_SECTIONS + ((MANAGEMENT,) if admin else ())
    return tuple(
        MenuItem(
            href, text, _within(path, href), pending if href == "/review" and pending else None
        )
        for href, text in sections
    )


def management_menu(path: str, admin: bool) -> tuple[MenuItem, ...]:
    """管理の中の画面を開いているときだけ、管理のメニューを返す。"""
    if not (admin and _within(path, MANAGEMENT[0])):
        return ()
    return tuple(MenuItem(href, text, path == href) for href, text in MANAGEMENT_PAGES)


def frame_for(viewer: Viewer | None) -> Frame | None:
    """ログインしていない画面（ログイン・パスキーの登録）では None（枠のメニューを出さない）。"""
    if viewer is None:
        return None
    monitor, state = MONITOR_STATES.get(viewer.monitor(), MONITOR_UNKNOWN)
    return Frame(
        person=viewer.name,
        monitor=monitor,
        monitor_state=state,
        menu=main_menu(viewer.path, viewer.admin, viewer.pending()),
        management=management_menu(viewer.path, viewer.admin),
    )
