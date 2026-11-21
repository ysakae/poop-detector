"""ごはんの量の目安に使う、犬種ごとの体格と成犬の体重の表。"""

from dataclasses import dataclass
from typing import Literal

SizeClass = Literal["toy", "small", "medium", "large", "giant"]

SIZE_CLASSES: tuple[SizeClass, ...] = ("toy", "small", "medium", "large", "giant")
SIZE_NAMES: dict[SizeClass, str] = {
    "toy": "超小型",
    "small": "小型",
    "medium": "中型",
    "large": "大型",
    "giant": "超大型",
}
# 成犬とみなす月齢の目安（体格ごと）。
ADULT_MONTHS: dict[SizeClass, int] = {
    "toy": 10,
    "small": 10,
    "medium": 12,
    "large": 15,
    "giant": 18,
}


@dataclass(frozen=True)
class Breed:
    """犬種名と、体格・成犬の体重（グラム）の一般的な中央値。"""

    name: str
    size_class: SizeClass
    adult_grams: int


def size_from_adult(grams: int) -> SizeClass:
    """成犬の体重から体格を決める。"""
    if grams < 4000:
        return "toy"
    if grams < 10000:
        return "small"
    if grams < 25000:
        return "medium"
    if grams < 45000:
        return "large"
    return "giant"


def _breed(name: str, grams: int) -> Breed:
    return Breed(name, size_from_adult(grams), grams)


BREEDS: tuple[Breed, ...] = (
    _breed("トイ・プードル", 3500),
    _breed("チワワ", 2500),
    _breed("ミニチュア・ダックスフンド", 5000),
    _breed("ポメラニアン", 2500),
    _breed("ヨークシャー・テリア", 3000),
    _breed("マルチーズ", 3500),
    _breed("パピヨン", 4000),
    _breed("シー・ズー", 6500),
    _breed("ミニチュア・シュナウザー", 7000),
    _breed("パグ", 8000),
    _breed("柴", 9000),
    _breed("ジャック・ラッセル・テリア", 6500),
    _breed("キャバリア・キング・チャールズ・スパニエル", 7500),
    _breed("ウェルシュ・コーギー・ペンブローク", 12000),
    _breed("フレンチ・ブルドッグ", 12000),
    _breed("ビーグル", 12000),
    _breed("ボーダー・コリー", 18000),
    _breed("ラブラドール・レトリーバー", 30000),
    _breed("ゴールデン・レトリーバー", 30000),
    _breed("ジャーマン・シェパード", 32000),
    _breed("秋田", 35000),
    _breed("グレート・ピレニーズ", 50000),
)


def find_breed(name: str) -> Breed | None:
    """前後の空白を除いて、名前が完全に一致する犬種を返す。"""
    wanted = name.strip()
    return next((breed for breed in BREEDS if breed.name == wanted), None)
