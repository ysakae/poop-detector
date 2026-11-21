"""ホーム画面に追加したときのアイコンが、GUI のテーマの色で配られることを確かめる。"""

import json
import re
from pathlib import Path

import cv2
import numpy as np
from support import exchange, request

from poop_detector.gui.assets import asset_url

STYLE = Path(__file__).resolve().parents[1] / "src/poop_detector/gui/templates/style.css"


def light(name: str) -> tuple[int, int, int]:
    """style.css の変数のライトの色（RGB）。"""
    value = re.search(rf"--{name}:light-dark\(#([0-9a-f]{{6}})", STYLE.read_text())[1]
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def decode(body: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def rgb(image: np.ndarray, y: int, x: int) -> tuple[int, int, int]:
    blue, green, red = (int(v) for v in image[y, x])
    return red, green, blue


def close(actual: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    return all(abs(a - e) <= 2 for a, e in zip(actual, expected, strict=True))


def test_pages_point_ios_to_the_apple_touch_icon(gui):
    server, _, _ = gui
    page = request(server, "GET", "/")[1].decode()
    assert f'<link rel="apple-touch-icon" href="{asset_url("apple-touch-icon.png")}">' in page


def test_icons_are_served_without_a_session_in_the_theme_colors(gui):
    server, _, _ = gui
    sizes = {
        "/assets/apple-touch-icon.png": 180,
        "/apple-touch-icon.png": 180,
        "/assets/icon-192.png": 192,
        "/assets/icon-512.png": 512,
    }
    for path, size in sizes.items():
        status, headers, body = exchange(server, "GET", path, auth=False)
        assert status == 200, path
        assert headers["Content-Type"] == "image/png", path
        image = decode(body)
        assert image.shape == (size, size, 3), path
        # 角まで主色で塗り（iOS が角を丸める）、中央の肉球の大きな指球は背景色。
        assert close(rgb(image, 0, 0), light("primary")), path
        assert close(rgb(image, round(size * 0.66), size // 2), light("bg")), path


def test_manifest_uses_the_theme_background(gui):
    server, _, _ = gui
    status, body = request(server, "GET", "/manifest.webmanifest")
    assert status == 200
    manifest = json.loads(body)
    assert manifest["background_color"] == "#{:02x}{:02x}{:02x}".format(*light("bg"))
    assert manifest["theme_color"] == "#{:02x}{:02x}{:02x}".format(*light("bg"))
