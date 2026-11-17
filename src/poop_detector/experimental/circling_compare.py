"""旋回の規則と、軌跡の特徴量による決定木を、人が確かめた動画で比べる（#127）。

保存済みの動画に対するオフラインの比較で、監視・通知・ラベルは変えない。決定木は
任意の依存（scikit-learn。`uv sync --extra analysis`）で動かす。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..errors import SetupRequiredError
from .trajectory import FEATURES

# 人のラベルのうち、排泄ありとして比べるもの。uncertain とラベルなしは比べない。
EXCRETION_LABELS = frozenset({"feces", "urine", "both"})
NO_EXCRETION_LABEL = "none"
# 交差検証の分割の上限と、結果を参考値にとどめる排泄ありの本数（#152 と同じ目安）。
MAX_FOLDS = 5
SMALL_SAMPLE = 30


def case_label(label: str | None) -> bool | None:
    """人のラベルを、排泄あり True・なし False・比べない None に分ける。"""
    if label in EXCRETION_LABELS:
        return True
    if label == NO_EXCRETION_LABEL:
        return False
    return None


@dataclass(frozen=True)
class Case:
    """比べる動画1本。日付は、同じ日の動画を学習と評価にまたがらせないために使う。"""

    clip_id: str
    day: str
    excretion: bool
    features: dict[str, float]
    circling: int

    def to_data(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "day": self.day,
            "excretion": self.excretion,
            "features": self.features,
            "circling": self.circling,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "Case":
        """保存した特徴量を読み直す。型が合わないものは ValueError にする。"""
        features = data.get("features")
        if (
            not isinstance(data.get("clip_id"), str)
            or not isinstance(data.get("day"), str)
            or not isinstance(data.get("excretion"), bool)
            or not isinstance(data.get("circling"), int)
            or not isinstance(features, dict)
            or set(features) != set(FEATURES)
            or not all(isinstance(value, int | float) for value in features.values())
        ):
            raise ValueError("保存した特徴量の形式が正しくありません")
        return cls(
            data["clip_id"],
            data["day"],
            data["excretion"],
            {name: float(features[name]) for name in FEATURES},
            data["circling"],
        )


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def metrics(truth: Sequence[bool], predicted: Sequence[bool]) -> dict[str, Any]:
    """排泄ありを陽性とした件数と、適合率・再現率（分母が 0 なら None）。"""
    pairs = list(zip(truth, predicted, strict=True))
    tp = sum(t and p for t, p in pairs)
    fp = sum(not t and p for t, p in pairs)
    fn = sum(t and not p for t, p in pairs)
    tn = sum(not t and not p for t, p in pairs)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": _rate(tp, tp + fp),
        "recall": _rate(tp, tp + fn),
    }


def _sklearn() -> tuple[Any, Any]:
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.model_selection import StratifiedGroupKFold
    except ImportError:
        raise SetupRequiredError(
            "決定木の比較には scikit-learn が必要です。uv sync --extra analysis を実行してください"
        ) from None
    return HistGradientBoostingClassifier, StratifiedGroupKFold


def _folds(cases: Sequence[Case]) -> int | str:
    """分割の数。比べられないときは理由を返す。"""
    days = len({case.day for case in cases})
    positives = sum(case.excretion for case in cases)
    if days < 2:
        return "評価には2日以上の動画が必要です"
    if min(positives, len(cases) - positives) < 2:
        return "排泄ありとなしの動画が2本以上ずつ必要です"
    return min(MAX_FOLDS, days, positives, len(cases) - positives)


def classify_out_of_fold(cases: Sequence[Case], folds: int) -> list[bool]:
    """日ごとに分けた交差検証で、学習に使わなかった動画だけを予測する。"""
    classifier, splitter = _sklearn()
    x = np.asarray([[case.features[name] for name in FEATURES] for case in cases])
    y = np.asarray([case.excretion for case in cases])
    groups = [case.day for case in cases]
    predicted = np.zeros(len(cases), dtype=bool)
    for train, test in splitter(n_splits=folds).split(x, y, groups):
        if len(set(y[train])) < 2:
            # 学習側に片方しかない分割は、その答えをそのまま返す。
            predicted[test] = y[train][0]
            continue
        model = classifier(max_depth=3, class_weight="balanced", random_state=0)
        predicted[test] = model.fit(x[train], y[train]).predict(x[test])
    return [bool(value) for value in predicted]


def evaluate(cases: Sequence[Case]) -> dict[str, Any]:
    """規則（旋回候補が1つ以上）と決定木の、排泄ありを当てる成績を並べる。"""
    truth = [case.excretion for case in cases]
    positives = sum(truth)
    folds = _folds(cases)
    classifier: dict[str, Any]
    if isinstance(folds, str):
        classifier = {"skipped": folds}
    else:
        classifier = {**metrics(truth, classify_out_of_fold(cases, folds)), "folds": folds}
    return {
        "cases": len(cases),
        "positives": positives,
        "days": len({case.day for case in cases}),
        "rule": metrics(truth, [case.circling > 0 for case in cases]),
        "classifier": classifier,
        "small_sample": positives < SMALL_SAMPLE,
    }
