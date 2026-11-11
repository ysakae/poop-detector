"""イベントの記録、目視の判定、評価指標を検証する。"""

import pytest
from support import spot_scene

from poop_detector.records.events import Store


def test_events_feedback_metrics_and_image(tmp_path):
    baseline, _ = spot_scene()
    store = Store(tmp_path)
    event = store.record(baseline, 0.4, "2026-11-01T00:00:00+00:00", 10.0)
    assert (tmp_path / store.list()[0]["image"]).exists()
    assert store.metrics()["precision"] is None
    store.classify(event, "feces")
    store.add_excretion("feces", "2026-11-01T01:00:00+00:00", missed=True)
    assert store.metrics()["precision"] == 1
    assert store.metrics()["recall"] == 0.5
    assert store.metrics()["missed"] == 1
    with pytest.raises(ValueError, match="記録が見つかりません"):
        store.classify("unknown", "false")


def test_metrics_delay_uses_ground_truth(tmp_path):
    baseline, _ = spot_scene()
    store = Store(tmp_path)
    event = store.record(baseline, 0.2, "2026-11-01T00:00:00+00:00", 1)
    store.classify(event, "feces", "2026-11-01T00:00:00+00:00", "2026-11-01T00:00:02+00:00")
    store.update(event, notified_at="2026-11-01T00:00:05+00:00")
    assert store.metrics()["start_delay_seconds"] == [5]
    assert store.metrics()["end_delay_seconds"] == [3]
