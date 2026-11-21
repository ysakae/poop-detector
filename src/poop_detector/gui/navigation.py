"""画面の共通の枠（メニュー、ログイン中の人、監視の状態）の値。

画面の配置は docs/screens.md の表に合わせる。メニューは見せ方だけで、管理者だけの画面を
断るのは asgi.dispatch（ルートの表の役割）が行う。
"""

from collections.abc import Callable
from dataclasses import dataclass

# 全員が使う場所。(URL, 名前, アイコン)。URL の下の画面（/clips?id=… など）も同じ場所として扱う。
MEMBER_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("/", "ホーム", "home"),
    ("/review", "確認待ち", "review"),
    ("/records", "記録", "records"),
    ("/care", "お世話", "food"),
    ("/weight", "体重", "weight"),
    ("/advice", "ごはんの相談", "chat"),
    ("/clips", "動画", "video"),
    ("/activity", "行動マップ", "map"),
    ("/sleep", "睡眠", "sleep"),
)
MANAGEMENT = ("/manage", "管理", "manage")
# スマートフォンの幅で画面の下に並べる場所。ほかの場所と PERSONAL は「その他」にまとめる。
# 毎日使う場所を並べる。動画は、確認待ちからラベルを付けるので「その他」に置く。
TAB_SECTIONS = frozenset({"/", "/review", "/records", "/care"})
PERSONAL: tuple[tuple[str, str, str], ...] = (
    ("/account", "自分の設定", "user"),
    ("/logout", "ログアウト", "logout"),
)
# 管理の中の画面を、役割ごとの5つのまとまりに分ける。(まとまりの名前, ((URL, 画面の名前), ...))。
# 管理のメニューにはまとまりだけを並べ、まとまりを開くとその中の画面をタブで切り替える。
# まとまりの入口は、最初の画面（よく使う画面を先に置く）。
MANAGEMENT_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("稼働状況", (("/manage", "稼働状況"),)),
    (
        "カメラと範囲",
        (
            ("/manage/areas", "範囲の登録"),
            ("/manage/checks", "確認と診断"),
            ("/manage/setup", "初回セットアップ"),
        ),
    ),
    ("判定と録画", (("/manage/models", "判定器とモデル"), ("/manage/storage", "動画の保存"))),
    ("設定とデータ", (("/manage/settings", "設定"), ("/manage/export", "書き出しとバックアップ"))),
    (
        "家族",
        (("/manage/users", "ユーザー"), ("/manage/invite", "招待"), ("/manage/audit", "監査ログ")),
    ),
)
# 管理の中の画面。ここに並ぶ URL はすべて管理者だけのルートにする（test_gui_roles.py が確かめる）。
MANAGEMENT_PAGES: tuple[tuple[str, str], ...] = tuple(
    page for _, pages in MANAGEMENT_GROUPS for page in pages
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
    """メニューの1項目。current はいま開いている場所、badge は名前の横に出す件数。

    icon は gui/icons.py の名前で、下のタブに出す（上のメニューは文字だけ）。
    """

    href: str
    text: str
    current: bool
    badge: int | None = None
    icon: str = ""


@dataclass(frozen=True)
class Frame:
    """共通の枠に出す値。"""

    person: str
    monitor: str
    monitor_state: str
    menu: tuple[MenuItem, ...]
    management: tuple[MenuItem, ...]
    # 開いているまとまりの中の画面のタブ。画面が1つだけのまとまりでは空。
    management_tabs: tuple[MenuItem, ...]
    # スマートフォンの幅の下のタブと、「その他」の中身。more_current は、いまの場所がその中にあるか。
    tabs: tuple[MenuItem, ...]
    more: tuple[MenuItem, ...]
    more_current: bool


def _within(path: str, section: str) -> bool:
    if section == "/":
        return path == "/"
    return path == section or path.startswith(section + "/")


def main_menu(path: str, admin: bool, pending: int = 0) -> tuple[MenuItem, ...]:
    """pending が1件以上なら、確認待ちに件数を出す。"""
    sections = MEMBER_SECTIONS + ((MANAGEMENT,) if admin else ())
    return tuple(
        MenuItem(
            href,
            text,
            _within(path, href),
            pending if href == "/review" and pending else None,
            icon,
        )
        for href, text, icon in sections
    )


def _on_page(path: str, href: str) -> bool:
    """path が href の画面か、その下の画面（/manage/export/summary など）か。"""
    if href == MANAGEMENT[0]:
        return path == href
    return _within(path, href)


def _current_group(path: str) -> tuple[tuple[str, str], ...]:
    for _, pages in MANAGEMENT_GROUPS:
        if any(_on_page(path, href) for href, _ in pages):
            return pages
    return ()


def management_menu(path: str, admin: bool) -> tuple[MenuItem, ...]:
    """管理の中の画面を開いているときだけ、管理のまとまりのメニューを返す。"""
    if not (admin and _within(path, MANAGEMENT[0])):
        return ()
    current = _current_group(path)
    return tuple(MenuItem(pages[0][0], name, pages == current) for name, pages in MANAGEMENT_GROUPS)


def management_tabs(path: str, admin: bool) -> tuple[MenuItem, ...]:
    """開いているまとまりに画面が2つ以上あるとき、その中の画面のタブを返す。"""
    pages = _current_group(path) if admin else ()
    if len(pages) < 2:
        return ()
    return tuple(MenuItem(href, text, _on_page(path, href)) for href, text in pages)


def frame_for(viewer: Viewer | None) -> Frame | None:
    """ログインしていない画面（ログイン・パスキーの登録）では None（枠のメニューを出さない）。"""
    if viewer is None:
        return None
    monitor, state = MONITOR_STATES.get(viewer.monitor(), MONITOR_UNKNOWN)
    menu = main_menu(viewer.path, viewer.admin, viewer.pending())
    more = tuple(item for item in menu if item.href not in TAB_SECTIONS) + tuple(
        MenuItem(href, text, _within(viewer.path, href), None, icon)
        for href, text, icon in PERSONAL
    )
    return Frame(
        person=viewer.name,
        monitor=monitor,
        monitor_state=state,
        menu=menu,
        management=management_menu(viewer.path, viewer.admin),
        management_tabs=management_tabs(viewer.path, viewer.admin),
        tabs=tuple(item for item in menu if item.href in TAB_SECTIONS),
        more=more,
        more_current=any(item.current for item in more),
    )
