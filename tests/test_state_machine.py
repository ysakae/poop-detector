"""継続確認・重複抑止・解除の状態遷移を検証する。"""

from poop_detector.detection.state_machine import Machine


def test_machine_persistence_duplicates_and_clear():
    machine = Machine(verify_seconds=2, clear_seconds=2)
    assert not machine.update("occupied", 0)
    assert machine.state == "OCCUPIED"
    assert not machine.update("candidate", 1)
    assert machine.update("candidate", 3)
    assert machine.state == "SUSPECTED"
    assert not machine.update("candidate", 50)
    machine.update("empty", 61)
    machine.update("empty", 63)
    assert machine.state == "EMPTY"
    assert not machine.update("candidate", 64)
    assert machine.update("candidate", 66)


def test_machine_has_no_acknowledged_state():
    """飼い主の確認は記録の項目で、監視の状態遷移には影響しない。"""
    assert not hasattr(Machine(), "acknowledge")


def test_interruption_resets_verification():
    machine = Machine(verify_seconds=2)
    machine.update("candidate", 0)
    machine.update("unstable", 1)
    assert not machine.update("candidate", 3)
    assert machine.update("candidate", 5)
