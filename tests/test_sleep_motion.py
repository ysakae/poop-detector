"""映像の動きと犬検出から1秒の観測を作る MotionSampler のテスト。"""

import numpy as np
import pytest

from poop_detector.features.sleep_motion import MotionSampler

HEIGHT, WIDTH = 120, 160
# 床面は画面の 0.1〜0.9 の正方形（画素では x=16〜143、y=12〜107 付近）。
FLOOR = np.array([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]], dtype=np.float32)
DOG = (60, 50, 90, 70)


def scene(*boxes: tuple[int, int, int, int], noise: float = 0, seed: int = 0) -> np.ndarray:
    """黒い画像に、犬に見立てた白い矩形を置く。"""
    frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    for x1, y1, x2, y2 in boxes:
        frame[y1:y2, x1:x2] = 255
    if noise:
        jitter = np.random.default_rng(seed).normal(0, noise, frame.shape)
        frame = np.clip(frame.astype(np.float64) + jitter, 0, 255).astype(np.uint8)
    return frame


def shifted(box: tuple[int, int, int, int], dx: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    return x1 + dx, y1, x2 + dx, y2


def settled(sampler: MotionSampler, box: tuple[int, int, int, int] = DOG) -> None:
    """犬が同じ場所で静止している状態まで進める。"""
    sampler.sample(scene(box), [box])
    assert sampler.sample(scene(box), [box]) == ("still", True)


def test_first_box_is_moving_because_there_is_no_history():
    sampler = MotionSampler(FLOOR)
    assert sampler.sample(scene(DOG), [DOG]) == ("moving", True)


def test_still_box_is_still():
    sampler = MotionSampler(FLOOR)
    sampler.sample(scene(DOG), [DOG])
    assert sampler.sample(scene(DOG), [DOG]) == ("still", True)


def test_still_box_is_still_with_sensor_noise():
    sampler = MotionSampler(FLOOR)
    # 夜の赤外線映像のざらつきに見立てた雑音は、動きとみなさない
    sampler.sample(scene(DOG, noise=6, seed=1), [DOG])
    for seed in range(2, 6):
        assert sampler.sample(scene(DOG, noise=6, seed=seed), [DOG]) == ("still", True)


def test_moving_box_is_moving():
    sampler = MotionSampler(FLOOR)
    sampler.sample(scene(DOG), [DOG])
    moved = shifted(DOG, 12)
    assert sampler.sample(scene(moved), [moved]) == ("moving", True)


def test_small_motion_inside_box_is_moving():
    sampler = MotionSampler(FLOOR)
    sampler.sample(scene(DOG), [DOG])
    # 枠は同じでも、枠の中の画素が大きく変われば動きとみなす（頭を上げた、など）
    moving = scene(DOG)
    moving[50:60, 60:75] = 0
    assert sampler.sample(moving, [DOG]) == ("moving", True)


def test_grayscale_frames_work_the_same():
    sampler = MotionSampler(FLOOR)
    gray = scene(DOG)[:, :, 0]
    assert sampler.sample(gray, [DOG]) == ("moving", True)
    assert sampler.sample(gray, [DOG]) == ("still", True)


def test_lost_detection_without_motion_keeps_still():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    # 丸まって検出が外れても、床面が静かなら静止の継続とみなす
    for _ in range(5):
        assert sampler.sample(scene(DOG), []) == ("still", False)
    assert sampler.sample(scene(DOG), [DOG]) == ("still", True)


def test_lost_detection_after_moving_toward_edge_is_left():
    sampler = MotionSampler(FLOOR)
    box = DOG
    for _ in range(4):
        box = shifted(box, 12)
        sampler.sample(scene(box), [box])
    # 床面の右の縁に向かって動いたあと検出が外れたら、床面が静かでも出ていったとみなす
    assert sampler.sample(scene(box), []) == ("left", False)


def test_lost_detection_after_moving_with_floor_change_is_left():
    sampler = MotionSampler(FLOOR)
    sampler.sample(scene(DOG), [DOG])
    moved = shifted(DOG, -6)
    assert sampler.sample(scene(moved), [moved]) == ("moving", True)
    # 動いた直後に検出が外れ、床面の画素も変わった（犬の姿が消えた）
    assert sampler.sample(scene(), []) == ("left", False)


def test_left_stays_left_while_floor_is_quiet():
    sampler = MotionSampler(FLOOR)
    sampler.sample(scene(DOG), [DOG])
    moved = shifted(DOG, -6)
    sampler.sample(scene(moved), [moved])
    assert sampler.sample(scene(), []) == ("left", False)
    for _ in range(5):
        assert sampler.sample(scene(), []) == ("left", False)
    # 検出のないまま床面に動きが出たら、居場所が分からないので unknown
    assert sampler.sample(scene(DOG), []) == ("unknown", False)


def test_lost_detection_with_floor_motion_after_still_is_unknown():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    # 静止していた犬の検出が外れ、床面が動いた。出ていった証拠はないので absent にしない
    stirred = scene(DOG)
    stirred[50:60, 60:75] = 0
    assert sampler.sample(stirred, []) == ("unknown", False)
    # 居場所は保ったままなので、床面が静かになれば静止の継続に戻る
    assert sampler.sample(stirred, []) == ("still", False)


def test_unknown_when_no_history_and_no_detection():
    sampler = MotionSampler(FLOOR)
    assert sampler.sample(scene(), []) == ("unknown", False)
    assert sampler.sample(scene(), []) == ("unknown", False)
    assert sampler.sample(scene(DOG), []) == ("unknown", False)


def test_box_outside_floor_is_ignored():
    sampler = MotionSampler(FLOOR)
    outside = (0, 0, 10, 10)
    assert sampler.sample(scene(outside), [outside]) == ("unknown", False)
    assert sampler.sample(scene(outside), [outside]) == ("unknown", False)


def test_box_outside_floor_does_not_hide_the_dog_inside():
    sampler = MotionSampler(FLOOR)
    outside = (0, 0, 10, 10)
    sampler.sample(scene(DOG, outside), [DOG, outside])
    assert sampler.sample(scene(DOG, outside), [DOG, outside]) == ("still", True)


def test_two_boxes_are_unknown():
    sampler = MotionSampler(FLOOR)
    other = (100, 30, 120, 45)
    assert sampler.sample(scene(DOG, other), [DOG, other]) == ("unknown", False)


def test_reset_forgets_location():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    sampler.reset()
    assert sampler.sample(scene(DOG), []) == ("unknown", False)
    assert sampler.sample(scene(DOG), [DOG]) == ("moving", True)


def test_frame_size_change_is_not_compared():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    bigger = np.zeros((HEIGHT * 2, WIDTH * 2, 3), dtype=np.uint8)
    box = (120, 100, 180, 140)
    bigger[100:140, 120:180] = 255
    # 前の画像と大きさが違えば差分を測れないので、静止とはみなさない
    assert sampler.sample(bigger, [box]) == ("moving", True)


@pytest.mark.parametrize("boundary", [None])
def test_without_floor_uses_whole_frame(boundary):
    sampler = MotionSampler(boundary)
    corner = (0, 0, 20, 20)
    sampler.sample(scene(corner), [corner])
    assert sampler.sample(scene(corner), [corner]) == ("still", True)
