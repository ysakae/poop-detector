"""犬検出モデルの取得と、静止画での動作確認。"""

import argparse
import json
import time

import cv2
import psutil

from .context import CommandContext


def setup_dog_model(_args: argparse.Namespace, context: CommandContext) -> int:
    from ..dog import setup_model

    print(f"犬検出モデルを確認しました: {setup_model(context.dog_model_dir)}")
    return 0


def check_dog(args: argparse.Namespace, context: CommandContext) -> int:
    """静止画で犬を検出し、検出枠と推論時間・負荷を JSON で表示する。"""
    from ..dog import DogDetector

    if not 1 <= args.repeat <= 100:
        raise ValueError("繰り返し回数は1〜100です")
    frame = cv2.imread(args.image)
    if frame is None:
        raise ValueError("画像を読み込めません")
    model = DogDetector(context.dog_model_dir, context.config.dog_confidence)
    # 初回の推論は準備の時間を含むため、計測から外す。
    boxes = model.predict(frame)
    process = psutil.Process()
    process.cpu_percent()
    start = time.perf_counter()
    for _ in range(args.repeat):
        boxes = model.predict(frame)
    print(
        json.dumps(
            {
                "dog_boxes": boxes,
                "mean_inference_ms": (time.perf_counter() - start) * 1000 / args.repeat,
                "cpu_percent_inference_loop": process.cpu_percent(),
                "rss_mib": process.memory_info().rss / 1024**2,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
