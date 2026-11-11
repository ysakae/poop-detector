"""排尿の可視変化と排泄記録の種類・時刻を検証する。"""

import cv2
import numpy as np
import pytest

from poop_detector.detection.difference import Detector
from poop_detector.records.events import Store


def test_chromatic_change_with_equal_brightness_is_candidate():
    base = np.full((200, 200, 3), 200, np.uint8)
    changed = base.copy()
    changed[80:101, 80:101] = [60, 240, 176]
    assert abs(int(cv2.cvtColor(changed, cv2.COLOR_BGR2GRAY)[90, 90]) - 200) < 5
    assert Detector(base).analyze(changed).kind == "candidate"


def test_classify_and_manual_records(tmp_path):
    store = Store(tmp_path)
    event = store.record(np.full((50, 50, 3), 180, np.uint8), 0.1, None, 1)
    store.classify(event, "urine", "2026-11-08T23:00:00+09:00", note="シートの濡れを確認")
    assert store.list()[0]["excretion_type"] == "urine"
    assert store.list()[0]["feedback"] == 1
    store.add_excretion("feces", "2026-11-08T23:10:00+09:00")
    store.add_excretion("both", "2026-11-08T23:20:00+09:00", missed=True)
    assert len(store.records()) == 3
    assert store.metrics()["excretion_counts"] == {"feces": 2, "urine": 2, "unknown": 0}
    assert store.metrics()["missed"] == 1
    store.classify(event, "false")
    assert len(store.records()) == 2
    assert next(row for row in store.list() if row["id"] == event)["actual_start"] == (
        "2026-11-08T23:00:00+09:00"
    )
    store.classify(event, "unknown")
    assert next(row for row in store.list() if row["id"] == event)["feedback"] is None


def test_invalid_record_is_not_saved(tmp_path):
    store = Store(tmp_path)
    for kind, start, end, message in [
        ("invalid", "2026-11-08T23:00:00+09:00", None, "排泄の種類"),
        ("urine", "2026-11-08T23:00:00", None, "タイムゾーンが必要"),
        ("feces", "2026-11-08T23:00:00+09:00", "2026-11-08T22:00:00+09:00", "完了は開始以降"),
    ]:
        with pytest.raises(ValueError, match=message):
            store.add_excretion(kind, start, end)
    assert store.list() == []


def test_legacy_positive_keeps_feces_label(tmp_path):
    store = Store(tmp_path)
    event = store.record(np.full((50, 50, 3), 180, np.uint8), 0.1, None, 1)
    # 種類を記録する前の版では、排泄ありを feedback=1 だけで表していた。
    store.update(event, feedback=1)
    assert store.records()[0]["excretion_type"] == "feces"


def test_cli_feedback_preserves_existing_time_and_note(tmp_path):
    from poop_detector.main import main

    store = Store(tmp_path)
    event = store.record(np.full((50, 50, 3), 180, np.uint8), 0.1, None, 1)
    store.classify(event, "urine", "2026-11-08T23:00:00+09:00", note="確認済み")
    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {tmp_path}\n")
    assert main(["--config", str(config), "feedback", event, "true", "--type", "both"]) == 0
    row = store.list()[0]
    assert row["actual_start"] == "2026-11-08T23:00:00+09:00"
    assert row["note"] == "確認済み"
