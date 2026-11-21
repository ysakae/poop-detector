"""同梱の静的ファイル（スクリプト・スタイル・画像）の URL と、ブラウザーに預ける期間を決める。

画面は `asset("menu.js")` で内容のハッシュを付けた URL を出し、その URL の応答は1年間
預けてもらう（中身を変えると URL が変わるので、古いものは使われない）。版のない URL や
古い版は、ETag で変わっていないかを確かめてから使ってもらう。いずれも本人の端末だけに
預ける（private）ので、プロキシなどの共有のキャッシュには残らない。
"""

from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .exchange import Exchange

STATIC = Path(__file__).with_name("static")
# スタイルはデザインの変数とまとめてテンプレートの隣に置いている。
STYLE = Path(__file__).with_name("templates") / "style.css"
IMMUTABLE = "private, max-age=31536000, immutable"
REVALIDATE = "private, no-cache"
# 動画のサムネイルと検知の画像。ID ごとに中身は変わらないが、消した録画の画像を
# 端末に長く残さないよう1日にとどめる。
IMAGE_CACHE = "private, max-age=86400"
# 保存した録画の動画。画像と同じく、ID ごとに中身は変わらないので1日預けてもらう。
VIDEO_CACHE = IMAGE_CACHE


def source(name: str) -> Path:
    """配信するファイルの場所。name はルートの表に登録した名前だけを受け取る。"""
    return STYLE if name == "style.css" else STATIC / name


@lru_cache(maxsize=64)
def _digest(path: Path, modified_ns: int) -> str:
    # 更新時刻を鍵に含め、開発中にファイルを書き換えたら版も変わるようにする。
    del modified_ns
    return sha256(path.read_bytes()).hexdigest()[:16]


def version(name: str) -> str:
    """ファイルの内容のハッシュ（URL の v と ETag に使う）。"""
    path = source(name)
    return _digest(path, path.stat().st_mtime_ns)


def asset_url(name: str) -> str:
    """内容のハッシュを付けた URL。テンプレートからは asset(...) で使う。"""
    return f"/assets/{name}?v={version(name)}"


def serve_asset(handler: "Exchange", name: str, mime: str, requested: str | None) -> None:
    """静的ファイルを返す。いまの版を指す URL なら長く預け、ほかは確かめてから使ってもらう。"""
    current = version(name)
    tag = f'"{current}"'
    headers = {"Cache-Control": IMMUTABLE if requested == current else REVALIDATE, "ETag": tag}
    if handler.headers.get("if-none-match") == tag:
        handler.respond(304, b"", mime, headers)
        return
    handler.respond(200, source(name).read_bytes(), mime, headers)
