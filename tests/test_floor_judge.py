"""床の候補が嘔吐・下痢らしい跡かを判定器で確かめる（#274）を検証する。"""

from datetime import datetime, timedelta

import cv2
import numpy as np
import pytest

from poop_detector.config import Config
from poop_detector.features.floor_judge import (
    FLOOR_SCHEMA,
    HOURLY_LIMIT,
    FloorJudgeWorker,
    floor_prompt,
    parse_judgement,
)
from poop_detector.notifications import ToiletNotifier
from poop_detector.records.database import events_database
from poop_detector.records.floor_objects import FloorObjects
from poop_detector.video.judge_runs import JudgeRunLog

NOW = datetime(2026, 11, 22, 12, 0).astimezone()


def frame():
    image = np.full((240, 320, 3), 170, np.uint8)
    cv2.rectangle(image, (100, 80), (116, 96), (40, 60, 90), -1)
    return image


class Judge:
    name = "偽の判定器"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def check(self):
        pass

    def judge(self, folder, prompt, images, schema):
        self.calls.append((prompt, list(images), schema))
        for image in images:
            assert (folder / image).is_file()
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


class Notifier:
    def __init__(self):
        self.sent = []

    def send_floor_judgement(self, label, object_id, repeat):
        self.sent.append((label, object_id, repeat))
        return True


def setup(tmp_path, outputs, clock=lambda: NOW):
    database = events_database(tmp_path)
    objects = FloorObjects(database, tmp_path)
    judge = Judge(outputs)
    notifier = Notifier()
    worker = FloorJudgeWorker(
        objects,
        JudgeRunLog(database),
        lambda: (judge, "codex", None),
        notifier,
        tmp_path / "floor-work",
        clock=clock,
    )
    return worker, objects, judge, notifier, database


def vomit(reason="黄色い液体が広がっている"):
    return {"label": "vomit", "confidence": "high", "reason": reason}


# --- 判定と記録 ---


def test_worker_judges_an_unjudged_candidate_and_notifies_vomit(tmp_path):
    worker, objects, judge, notifier, database = setup(tmp_path, [vomit()])
    object_id = objects.add(frame(), (100, 80, 116, 96), 0.003)
    assert worker.poll_once()
    [row] = objects.list()
    assert (row.judge_status, row.judge_label) == ("done", "vomit")
    assert row.judge_reason == "黄色い液体が広がっている"
    _, images, schema = judge.calls[0]
    assert len(images) == 2  # 全体と、候補の周りの拡大
    assert schema == FLOOR_SCHEMA
    assert notifier.sent == [("嘔吐", object_id, 1)]
    usage = JudgeRunLog(database).count_since(NOW - timedelta(hours=1), ("floor",))
    assert usage == 1
    assert not worker.poll_once()


def test_second_vomit_within_a_day_is_marked_as_repeated(tmp_path):
    worker, objects, _, notifier, _ = setup(tmp_path, [vomit(), vomit()])
    objects.add(frame(), (100, 80, 116, 96), 0.003)
    worker.poll_once()
    second = objects.add(frame(), (100, 80, 116, 96), 0.003)
    worker.poll_once()
    assert notifier.sent[-1] == ("嘔吐", second, 2)


def test_toys_and_false_detections_are_recorded_without_notice(tmp_path):
    outputs = [{"label": "toy", "confidence": "medium", "reason": "ボール"}]
    worker, objects, _, notifier, _ = setup(tmp_path, outputs)
    objects.add(frame(), (100, 80, 116, 96), 0.003)
    worker.poll_once()
    assert objects.list()[0].judge_label == "toy"
    assert notifier.sent == []


def test_judge_failure_is_recorded_and_not_retried(tmp_path):
    worker, objects, _, notifier, database = setup(tmp_path, [OSError("CLI がない")])
    objects.add(frame(), (100, 80, 116, 96), 0.003)
    assert worker.poll_once()
    row = objects.list()[0]
    assert row.judge_status == "failed"
    assert row.judge_label is None
    assert not worker.poll_once()
    runs = JudgeRunLog(database).count_since(NOW - timedelta(hours=1), ("floor",))
    assert runs == 1
    assert notifier.sent == []


def test_invalid_output_fails_the_candidate(tmp_path):
    worker, objects, _, _, _ = setup(tmp_path, [{"label": "cake"}])
    objects.add(frame(), (100, 80, 116, 96), 0.003)
    worker.poll_once()
    assert objects.list()[0].judge_status == "failed"


def test_hourly_limit_stops_judging(tmp_path):
    worker, objects, _, _, database = setup(tmp_path, [vomit()])
    log = JudgeRunLog(database)
    for _ in range(HOURLY_LIMIT):
        log.record("codex", None, "floor", failed=False, at=NOW - timedelta(minutes=5))
    objects.add(frame(), (100, 80, 116, 96), 0.003)
    assert not worker.poll_once()
    assert objects.list()[0].judge_status is None


def test_old_candidates_are_not_judged(tmp_path):
    worker, objects, _, _, _ = setup(tmp_path, [vomit()], clock=lambda: NOW + timedelta(days=3))
    objects.add(frame(), (100, 80, 116, 96), 0.003)
    assert not worker.poll_once()


# --- 指示と解釈 ---


def test_parse_judgement_checks_label_confidence_and_reason():
    assert parse_judgement(vomit()) == ("vomit", "黄色い液体が広がっている")
    with pytest.raises(ValueError, match="label"):
        parse_judgement({"label": "cake", "confidence": "high", "reason": ""})
    with pytest.raises(ValueError, match="confidence"):
        parse_judgement({"label": "vomit", "confidence": "sure", "reason": ""})
    with pytest.raises(ValueError, match="形"):
        parse_judgement("嘔吐です")
    long = parse_judgement({"label": "unknown", "confidence": "low", "reason": "あ" * 500})
    assert long[0] == "unknown"
    assert len(long[1]) <= 200


def test_prompt_mentions_the_red_box_and_labels():
    prompt = floor_prompt("ポチ")
    assert "赤い枠" in prompt
    for label in ("vomit", "diarrhea", "foreign", "toy", "false", "unknown"):
        assert label in prompt
    assert "ポチ" in prompt


# --- 文面・設定・画面 ---


class Messages(ToiletNotifier):
    def __init__(self):
        super().__init__(True, "ポチ")
        self.messages = []

    def _deliver(self, message, *, early_alert, path=""):
        self.messages.append((message, path))
        return True


def test_floor_judgement_messages():
    notifier = Messages()
    notifier.send_floor_judgement("嘔吐", "abc", 1)
    notifier.send_floor_judgement("下痢", "def", 3)
    first, again = notifier.messages
    assert "嘔吐" in first[0]
    assert first[1] == "/floor?id=abc"
    assert "24時間で3回目" in again[0]


def test_floor_judge_is_off_by_default():
    assert Config().floor_judge is False


def test_floor_page_shows_the_judgement(gui):
    from support import exchange, login

    server, store, _ = gui
    objects = FloorObjects(store.database, server.app.root)
    object_id = objects.add(frame(), (100, 80, 116, 96), 0.003)
    objects.claim_unjudged(NOW - timedelta(days=1))
    objects.save_judgement(object_id, "diarrhea", "ゆるい便のような跡")
    cookie = login(server, role="member")
    _, _, body = exchange(server, "GET", "/floor", headers=cookie, auth=False)
    html = body.decode()
    assert "判定器：下痢" in html
    assert "ゆるい便のような跡" in html


def test_floor_judge_watch_runs_only_when_enabled(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from poop_detector.commands.context import CommandContext, floor_judge_watch

    judge = Judge([vomit()])
    built = []

    def build_judge(provider, model=""):
        built.append(provider)
        return judge

    def watching(check, interval):
        check()
        return nullcontext()

    monkeypatch.setattr("poop_detector.video.providers.build_judge", build_judge)
    monkeypatch.setattr("poop_detector.features.watchdog.watching", watching)
    objects = FloorObjects(events_database(tmp_path), tmp_path)
    objects.add(frame(), (100, 80, 116, 96), 0.003)
    notifier = Notifier()
    with floor_judge_watch(CommandContext(Config(data_dir=str(tmp_path))), notifier):
        pass
    assert built == []
    on = CommandContext(Config(data_dir=str(tmp_path), floor_judge=True))
    with floor_judge_watch(on, notifier):
        pass
    assert built == ["codex"]
    assert objects.list()[0].judge_label == "vomit"
    assert len(notifier.sent) == 1
