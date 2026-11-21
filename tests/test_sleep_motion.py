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


BEYOND = (130, 50, 160, 70)


def walked_to_edge(sampler: MotionSampler) -> None:
    """床面の右の縁まで検出されたまま歩き、縁を越えたところで検出が外れる。"""
    near = (100, 50, 130, 70)
    sampler.sample(scene(DOG), [DOG])
    sampler.sample(scene(near), [near])
    assert sampler.sample(scene(BEYOND), []) == ("left", False)


def test_motion_reaching_the_edge_while_undetected_is_left():
    walked_to_edge(MotionSampler(FLOOR))


def test_left_stays_left_while_floor_is_quiet():
    sampler = MotionSampler(FLOOR)
    walked_to_edge(sampler)
    for _ in range(5):
        assert sampler.sample(scene(BEYOND), []) == ("left", False)
    # 出ていった直後（EXIT_GRACE_SECONDS の間）の床面の動きは、居場所が分からないので unknown
    assert sampler.sample(scene(BEYOND, DOG), []) == ("unknown", False)


def test_undetected_walk_then_vanish_in_place_never_counts_empty_floor_as_still():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    # 検出されないまま、最後の枠の外へ歩いた（縁には届いていない）。動いた先へ居場所を移す
    away = shifted(DOG, -20)
    assert sampler.sample(scene(away), []) == ("moving", False)
    assert sampler.sample(scene(away), []) == ("still", False)
    # 動きを伴わずに姿が消えたら居場所を捨て、空の床面が静かでも still に戻さない
    assert sampler.sample(scene(), []) == ("unknown", False)
    for _ in range(5):
        assert sampler.sample(scene(), []) == ("unknown", False)


NEAR_EDGE = (110, 50, 140, 70)


def test_undetected_exit_from_box_near_floor_edge_is_left():
    sampler = MotionSampler(FLOOR)
    settled(sampler, NEAR_EDGE)
    # 縁の近くで寝ていた犬が、検出されないまま床面の外へ出た。床面の中の動きは
    # 広げた枠に収まるが、枠の中の画像が犬のいたときと違うので出ていったとみなす
    outside = (145, 50, 160, 70)
    assert sampler.sample(scene(outside), []) == ("left", False)
    for _ in range(3):
        assert sampler.sample(scene(outside), []) == ("left", False)


def test_undetected_exit_at_image_edge_without_floor_is_left():
    sampler = MotionSampler(None)
    box = (125, 50, 155, 70)
    settled(sampler, box)
    assert sampler.sample(scene(), []) == ("left", False)
    for _ in range(3):
        assert sampler.sample(scene(), []) == ("left", False)


def test_ear_twitch_of_dog_sleeping_at_edge_keeps_location():
    sampler = MotionSampler(FLOOR)
    settled(sampler, NEAR_EDGE)
    twitched = scene(NEAR_EDGE)
    twitched[55:61, 120:124] = 0
    assert sampler.sample(twitched, []) == ("moving", False)
    for _ in range(3):
        assert sampler.sample(twitched, []) == ("still", False)
    assert sampler.sample(twitched, [NEAR_EDGE]) == ("still", True)


def test_undetected_exit_from_box_away_from_edge_is_unknown():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    # 床面の真ん中で、検出されないまま犬の姿が枠から消えた
    assert sampler.sample(scene(), []) == ("unknown", False)
    for _ in range(5):
        assert sampler.sample(scene(), []) == ("unknown", False)


def test_slow_lighting_change_while_undetected_drops_location():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    # 犬の画像は検出したときだけ覚えるので、検出のないまま明るさが PIXEL_DELTA を超えて
    # 変わると、犬がまだいても居場所を捨てる（静止と数えすぎない側に倒す）
    frame = scene(DOG).astype(np.int16)
    outcomes = [
        sampler.sample(np.clip(frame - step * 10, 0, 255).astype(np.uint8), [])[0]
        for step in range(1, 9)
    ]
    assert outcomes == ["still", "still"] + ["unknown"] * 6


def creep(
    sampler: MotionSampler, box: tuple[int, int, int, int], step: int, distance: int
) -> list[str]:
    """検出のないまま step 画素ずつ（負なら左へ）動き、動くたびに1秒止まる。最後は空の床面が静かに続く。"""
    outcomes = []
    for moved in range(step, distance + step, step):
        frame = scene(shifted(box, moved))
        outcomes.append(sampler.sample(frame, [])[0])
        outcomes.append(sampler.sample(frame, [])[0])
    for _ in range(5):
        outcomes.append(sampler.sample(scene(), [])[0])
    return outcomes


def after_location_lost(outcomes: list[str]) -> list[str]:
    first = next(i for i, o in enumerate(outcomes) if o in ("left", "unknown"))
    return outcomes[first:]


@pytest.mark.parametrize("step", [3, 5, 8])
@pytest.mark.parametrize(
    ("boundary", "box"), [(FLOOR, NEAR_EDGE), (None, (125, 50, 155, 70))], ids=["floor", "frame"]
)
def test_creeping_exit_at_edge_ends_in_left(step, boundary, box):
    sampler = MotionSampler(boundary)
    settled(sampler, box)
    outcomes = creep(sampler, box, step, WIDTH - box[0])
    assert "still" not in after_location_lost(outcomes)
    assert outcomes[-5:] == ["left"] * 5


@pytest.mark.parametrize("step", [3, 5, 8])
def test_creeping_dog_away_from_edge_that_vanishes_ends_in_unknown(step):
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    # 床面の真ん中で、検出のないまま少しずつ左へ動き（縁の帯には届かない）、最後に姿が消える。
    # 動いている間は追ってよいが、消えたあとの空の床面は still と数えない
    outcomes = creep(sampler, DOG, -step, -15)
    assert outcomes[-5:] == ["unknown"] * 5


OUTSIDE = (145, 50, 160, 70)
# 床面の上の縁の帯の、最後の枠から離れた場所
TOP_BAND = (60, 12, 90, 24)


def left_near_edge(sampler: MotionSampler) -> None:
    """縁の近くで寝ていた犬が、検出されないまま床面の外へ出て left になる。"""
    settled(sampler, NEAR_EDGE)
    assert sampler.sample(scene(OUTSIDE), []) == ("left", False)


@pytest.mark.parametrize(
    "returned",
    [(128, 50, 143, 70), NEAR_EDGE, TOP_BAND],
    ids=["same-band", "old-box", "other-edge"],
)
def test_motion_long_after_left_is_tracked_as_a_returning_dog(returned):
    sampler = MotionSampler(FLOOR)
    left_near_edge(sampler)
    for _ in range(600):
        assert sampler.sample(scene(OUTSIDE), []) == ("left", False)
    # 静かな時間のあとの動きは、出ていく途中ではなく戻ってきた犬とみなして追う
    assert sampler.sample(scene(OUTSIDE, returned), []) == ("moving", False)


def test_motion_in_exit_area_within_grace_stays_left():
    sampler = MotionSampler(FLOOR)
    left_near_edge(sampler)
    for _ in range(5):
        assert sampler.sample(scene(OUTSIDE), []) == ("left", False)
    # 出ていった直後の、出口（最後の枠の近くの縁）の中の残りの動き
    assert sampler.sample(scene(OUTSIDE, (130, 55, 143, 65)), []) == ("left", False)


def test_motion_right_after_left_is_not_tracked_as_the_dog():
    sampler = MotionSampler(FLOOR)
    left_near_edge(sampler)
    # 出ていった直後の動きは、出ていく途中の動きかもしれないので、そこに犬がいるとはみなさない
    assert sampler.sample(scene(OUTSIDE, TOP_BAND), []) == ("unknown", False)
    # そのあと空の床面が静かでも still と数えない
    for _ in range(5):
        assert sampler.sample(scene(OUTSIDE), []) == ("unknown", False)


def test_compact_blob_away_from_dog_is_motion():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    blob = scene(DOG)
    blob[30:36, 110:116] = 255
    assert sampler.sample(blob, []) == ("moving", False)


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


def test_frame_too_small_to_locate_is_unknown():
    sampler = MotionSampler(FLOOR)
    tiny = np.zeros((1, 1, 3), dtype=np.uint8)
    assert sampler.sample(tiny, [(0, 0, 0, 0)]) == ("unknown", False)


def test_undetected_dog_that_moves_into_view_and_lies_down_is_tracked():
    # 丸まると犬検出が外れる。検出が一度もなくても、床面の中で動いて止まった場所を居場所とする
    sampler = MotionSampler(FLOOR)
    assert sampler.sample(scene(), []) == ("unknown", False)
    assert sampler.sample(scene(DOG), []) == ("moving", False)
    for _ in range(3):
        assert sampler.sample(scene(DOG), []) == ("still", False)


def test_undetected_dog_walking_to_a_new_spot_keeps_being_tracked():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    spot = (30, 70, 60, 95)
    assert sampler.sample(scene(spot), []) == ("moving", False)
    for _ in range(3):
        assert sampler.sample(scene(spot), []) == ("still", False)


def test_dog_tracked_by_motion_that_then_vanishes_in_place_is_unknown():
    sampler = MotionSampler(FLOOR)
    sampler.sample(scene(), [])
    sampler.sample(scene(DOG), [])
    assert sampler.sample(scene(DOG), []) == ("still", False)
    # 動いて止まった場所の映像を覚えたあと、動きを伴わずに姿が消えたら居場所を捨てる
    assert sampler.sample(scene(), []) == ("unknown", False)
    for _ in range(3):
        assert sampler.sample(scene(), []) == ("unknown", False)


def test_tiny_motion_elsewhere_does_not_move_the_dog():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    speck = scene(DOG)
    speck[30:36, 110:116] = 255
    # 小さな物の動き（おもちゃなど）で居場所を移さない。犬の静止とも数えない
    assert sampler.sample(speck, []) == ("moving", False)
    assert sampler.sample(speck, []) == ("still", False)
    # 居場所は犬の枠のままなので、犬の姿が消えれば出ていったと分かる
    specks_only = scene()
    specks_only[30:36, 110:116] = 255
    assert sampler.sample(specks_only, []) == ("unknown", False)


def test_dog_at_floor_corner_counts_when_its_feet_are_on_the_floor():
    # 柵に寄りかかると枠の中心は床面の外に出るが、足元（枠の下端の中央）は床面の中にある
    sampler = MotionSampler(FLOOR)
    corner = (120, 0, 150, 20)
    sampler.sample(scene(corner), [corner])
    assert sampler.sample(scene(corner), [corner]) == ("still", True)


def test_motion_located_dog_is_given_a_dog_sized_box():
    # 寝返りの小さな動きから作った居場所でも、犬の大きさの枠にするので、
    # 縁の近くで寝ている犬の次の身じろぎを「出ていった」と取り違えない
    sampler = MotionSampler(FLOOR)
    sampler.sample(scene(), [])
    stir = (118, 50, 128, 60)
    assert sampler.sample(scene(stir), []) == ("moving", False)
    assert sampler.sample(scene(stir), []) == ("still", False)
    twitch = scene(stir)
    twitch[50:56, 130:136] = 255
    assert sampler.sample(twitch, []) == ("moving", False)
    assert sampler.sample(twitch, []) == ("still", False)


def test_large_undetected_motion_elsewhere_moves_the_dog_there():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    # 犬の枠の中は変わらないまま、離れた場所で犬らしい大きさの動きが出た
    other = (100, 40, 125, 55)
    assert sampler.sample(scene(DOG, other), []) == ("moving", False)
    # 居場所は動いた先へ移り、そこが静かになれば still
    assert sampler.sample(scene(DOG, other), []) == ("still", False)


def test_large_undetected_motion_reaching_the_edge_is_left():
    sampler = MotionSampler(FLOOR)
    settled(sampler)
    assert sampler.sample(scene(DOG, (125, 80, 150, 100)), []) == ("left", False)


def test_frame_size_change_while_undetected_drops_location():
    # 動きで追い始めたばかりで、まだ犬の姿を覚えていない居場所
    sampler = MotionSampler(FLOOR)
    sampler.sample(scene(), [])
    assert sampler.sample(scene(DOG), []) == ("moving", False)
    # 縦横比が変わると縮小した画像の大きさも変わり、前の画像と比べられない
    bigger = np.zeros((HEIGHT * 2, WIDTH, 3), dtype=np.uint8)
    assert sampler.sample(bigger, []) == ("unknown", False)
    assert sampler.sample(bigger, []) == ("unknown", False)
