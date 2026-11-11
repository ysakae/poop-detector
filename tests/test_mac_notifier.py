"""macOS の通知の有効・無効と、送信の失敗の扱いを検証する。"""

import subprocess

from poop_detector.notifications import MacNotifier


def test_notification_disabled_and_safe_arguments():
    calls = []
    notifier = MacNotifier(enabled=False, runner=lambda *a, **kw: calls.append((a, kw)))
    assert not notifier.send()
    notifier.enabled = True
    assert notifier.send()
    assert calls[0][1]["shell"] is False


def test_notification_failure_does_not_crash():
    def fails(*args, **kwargs):
        raise subprocess.TimeoutExpired("osascript", 5)

    assert not MacNotifier(runner=fails).send()
