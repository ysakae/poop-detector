"""犬検出モデルの取得と、静止画での動作確認。"""

import argparse
import json
import time
from datetime import UTC, datetime

import cv2
import psutil

from ..errors import InvalidInputError
from .context import CommandContext


def setup_dog_model(_args: argparse.Namespace, context: CommandContext) -> int:
    from ..features.dog import setup_model

    print(f"犬検出モデルを確認しました: {setup_model(context.dog_model_dir)}")
    return 0


def check_dog(args: argparse.Namespace, context: CommandContext) -> int:
    """静止画で犬を検出し、検出枠と推論時間・負荷を JSON で表示する。"""
    from ..features.dog import DogDetector
    from ..features.dog_status import save_check

    if not 1 <= args.repeat <= 100:
        raise InvalidInputError("繰り返し回数は1〜100です")
    frame = cv2.imread(args.image)
    if frame is None:
        raise InvalidInputError("画像を読み込めません")
    model = DogDetector(context.dog_model_dir, context.config.dog_confidence)
    # 初回の推論は準備の時間を含むため、計測から外す。
    boxes = model.predict(frame)
    process = psutil.Process()
    process.cpu_percent()
    start = time.perf_counter()
    for _ in range(args.repeat):
        boxes = model.predict(frame)
    measured = {
        "dog_boxes": boxes,
        "mean_inference_ms": (time.perf_counter() - start) * 1000 / args.repeat,
        "cpu_percent_inference_loop": process.cpu_percent(),
        "rss_mib": process.memory_info().rss / 1024**2,
    }
    print(json.dumps(measured, ensure_ascii=False, indent=2))
    # 管理 › 判定器とモデルに出すため、直近の結果を保存する。
    save_check(
        context.dog_model_dir,
        measured,
        repeat=args.repeat,
        at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    return 0
