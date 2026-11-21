"""GUI の応答の圧縮の回帰テスト。

リバースプロキシ（Tailscale Serve など）が圧縮しなくても、モバイル回線での転送量を減らす。
文字の応答だけを圧縮し、ライブ映像・画像・動画は圧縮せずにそのまま流す。
"""

import gzip
from contextlib import closing
from http.client import HTTPConnection

from support import exchange, session_cookie

from poop_detector.gui.asgi import UNCOMPRESSED
from poop_detector.gui.assets import asset_url

GZIP = {"Accept-Encoding": "gzip"}


def test_pages_are_compressed_for_browsers_that_accept_gzip(gui):
    server, _, _ = gui
    status, headers, body = exchange(server, "GET", "/", headers=GZIP)

    assert status == 200
    assert headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in headers["Vary"]
    assert b"<!doctype html>" in gzip.decompress(body)
    # 安全のためのヘッダーは圧縮しても付く。
    assert headers["Cache-Control"] == "no-store"


def test_stylesheet_and_scripts_are_compressed(gui):
    server, _, _ = gui
    for name in ("style.css", "live.js"):
        status, headers, body = exchange(server, "GET", asset_url(name), headers=GZIP)
        assert status == 200
        assert headers["Content-Encoding"] == "gzip"
        assert gzip.decompress(body)


def test_plain_response_without_accept_encoding(gui):
    server, _, _ = gui
    status, headers, body = exchange(server, "GET", "/")

    assert status == 200
    assert "Content-Encoding" not in headers
    assert b"<!doctype html>" in body


def test_images_are_sent_as_they_are(gui):
    server, _, event = gui
    status, headers, body = exchange(server, "GET", f"/image?id={event}", headers=GZIP)

    assert status == 200
    assert "Content-Encoding" not in headers
    assert body.startswith(b"\xff\xd8")


def test_live_stream_and_videos_are_not_compressed(gui):
    """ライブ映像は少しずつ流すので、圧縮でまとめて遅らせない。"""
    assert "multipart/*" in UNCOMPRESSED
    assert "video/*" in UNCOMPRESSED
    server, _, _ = gui
    with closing(HTTPConnection(*server.server_address, timeout=2)) as conn:
        conn.request("GET", "/live/stream", headers={**session_cookie(server), **GZIP})
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Encoding") is None
