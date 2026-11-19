"""映像の動きと犬検出から1秒の観測を作る MotionSampler のテスト。"""

import numpy as np

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


def test_moving_then_curled_up_undetected_with_quiet_floor_is_still():
    sampler = MotionSampler(FLOOR)
    sampler.sample(scene(DOG), [DOG])
    moved = shifted(DOG, 12)
    assert sampler.sample(scene(moved), [moved]) == ("moving", True)
    # 動いたあと丸まって検出が外れても、床面が静かなら動いていない証拠として still
    for _ in range(3):
        assert sampler.sample(scene(moved), []) == ("still", False)


def test_ear_twitch_inside_last_box_while_undetected_is_moving():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    # 検出が外れたまま、最後の枠の中だけが少し動いた（耳を動かした程度）
    twitched = scene(DOG)
    twitched[55:61, 70:74] = 0
    assert sampler.sample(twitched, []) == ("moving", False)
    # 居場所は保つので、静かになれば still、再び検出すれば枠で判定する
    assert sampler.sample(twitched, []) == ("still", False)
    assert sampler.sample(twitched, [DOG]) == ("still", True)


def walked_to_edge(sampler: MotionSampler) -> None:
    """床面の右の縁まで検出されたまま歩き、縁を越えたところで検出が外れる。"""
    near = (100, 50, 130, 70)
    sampler.sample(scene(DOG), [DOG])
    sampler.sample(scene(near), [near])
    assert sampler.sample(scene(BEYOND), []) == ("left", False)


BEYOND = (130, 50, 160, 70)


def test_motion_reaching_the_edge_while_undetected_is_left():
    walked_to_edge(MotionSampler(FLOOR))


def test_left_stays_left_while_floor_is_quiet():
    sampler = MotionSampler(FLOOR)
    walked_to_edge(sampler)
    for _ in range(5):
        assert sampler.sample(scene(BEYOND), []) == ("left", False)
    # 検出のないまま床面に動きが出たら、居場所が分からないので unknown
    assert sampler.sample(scene(BEYOND, DOG), []) == ("unknown", False)


def test_undetected_walk_out_after_still_never_becomes_still():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    # 検出されないまま、最後の枠の外へ歩いた（縁には届いていない）
    away = shifted(DOG, -20)
    assert sampler.sample(scene(away), []) == ("unknown", False)
    # 犬が見えなくなり、そのあと空の床面が静かでも、居場所を捨てたので still に戻さない
    assert sampler.sample(scene(), []) == ("unknown", False)
    for _ in range(5):
        assert sampler.sample(scene(), []) == ("unknown", False)


def test_compact_blob_away_from_dog_is_motion():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    blob = scene(DOG)
    blob[30:36, 110:116] = 255
    assert sampler.sample(blob, []) == ("unknown", False)


def test_scattered_specks_do_not_count_as_floor_motion():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    # 圧縮や赤外線の点状の雑音。合計は前のしきい値（床面の0.2%、約25画素）を超えるが、
    # どれも小さな点なので床面の動きとはみなさない
    specks = scene(DOG)
    count = 0
    for y in range(16, 105, 8):
        for x in range(20, 140, 8):
            if not (44 < y < 76 and 52 < x < 98):
                specks[y, x] = 255
                count += 1
    assert count > 100
    assert sampler.sample(specks, []) == ("still", False)


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


def test_without_floor_uses_whole_frame():
    sampler = MotionSampler(None)
    corner = (0, 0, 20, 20)
    sampler.sample(scene(corner), [corner])
    assert sampler.sample(scene(corner), [corner]) == ("still", True)
