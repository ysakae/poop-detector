"""ホーム画面に追加したときのアイコン（PWA・iPhone・iPad）を、GUI のテーマの色で作り直す。

肉球の形は gui/static/paw.svg をもとに、小さく表示しても指がつぶれないよう指のあいだを空けた。
色は style.css のライトの配色（主色の地に背景色の肉球）。
iOS は角を自動で丸めるので、角まで塗った正方形にする。Android の maskable でも欠けないよう、
肉球は中央の 62% に収める。色や形を変えたら、このスクリプトを実行し直してください。
    uv run python scripts/make-app-icons.py
"""

from pathlib import Path

import cv2
import numpy as np

STATIC = Path(__file__).resolve().parents[1] / "src/poop_detector/gui/static"
# style.css の --primary と --bg（ライト）。
BACKGROUND = "#bf4a2a"
PAW = "#faf7f2"
# 肉球の楕円（48 の枠の中の中心 x・y と半径 x・y）。
PAW_ELLIPSES = (
    (24, 31, 10, 7.5),
    (11, 19.5, 4, 5.5),
    (19.5, 12, 4, 5.5),
    (28.5, 12, 4, 5.5),
    (37, 19.5, 4, 5.5),
)
PAW_BOX = (7.0, 6.5, 41.0, 38.5)  # 肉球が占める範囲（左・上・右・下）
PAW_SHARE = 0.62
SUPERSAMPLE = 4
# ファイル名と一辺の画素数。apple-touch-icon は iOS が推奨する 180。
ICONS = {"apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512}


def bgr(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    red, green, blue = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return blue, green, red


def draw(size: int) -> np.ndarray:
    """size 画素の正方形のアイコン。大きく描いてから縮め、縁をなめらかにする。"""
    big = size * SUPERSAMPLE
    image = np.full((big, big, 3), bgr(BACKGROUND), np.uint8)
    left, top, right, bottom = PAW_BOX
    scale = big * PAW_SHARE / max(right - left, bottom - top)
    offset_x = big / 2 - (left + right) / 2 * scale
    offset_y = big / 2 - (top + bottom) / 2 * scale
    for cx, cy, rx, ry in PAW_ELLIPSES:
        center = (round(offset_x + cx * scale), round(offset_y + cy * scale))
        axes = (round(rx * scale), round(ry * scale))
        cv2.ellipse(image, center, axes, 0, 0, 360, bgr(PAW), -1, cv2.LINE_AA)
    return cv2.resize(image, (size, size), interpolation=cv2.INTER_AREA)


def main() -> None:
    for name, size in ICONS.items():
        path = STATIC / name
        if not cv2.imwrite(str(path), draw(size)):
            raise OSError(f"{path} を書き込めませんでした")
        print(f"{path.name}（{size}×{size}）を作りました")


if __name__ == "__main__":
    main()
