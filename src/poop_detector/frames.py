"""モジュール間で受け渡す画像とマスクの型。"""

from typing import Any, TypeAlias

from numpy.typing import NDArray

# OpenCV の BGR 画像（高さ×幅×3）や、グレースケール画像（高さ×幅）。通常は uint8。
# OpenCV の型スタブは dtype を整数と浮動小数の和で返すため、dtype までは絞らない。
Frame: TypeAlias = NDArray[Any]
# 監視する画素を 255（または True）で表す2次元のマスク。
Mask: TypeAlias = NDArray[Any]
# 画像の形状。frame.shape をそのまま渡す。
Shape: TypeAlias = tuple[int, ...]
