"""判定器の比較で、推論の深さ・画像の大きさ・トークン数を条件に加える処理を確認する。"""

import json
import subprocess

import cv2
import numpy as np
import pytest

from poop_detector.commands.benchmark import parse_judge
from poop_detector.commands.context import CommandError
from poop_detector.detection.roi import ROI
from poop_detector.video.benchmark import Case, Scene, evaluate
from poop_detector.video.codex_cli import CodexCliJudge
from poop_detector.video.providers import build_judge
from poop_detector.video.review import SheetOptions, write_sheet
from poop_detector.video.token_usage import TokenUsage

ANSWER = {
    "label": "none",
    "activity": "other",
    "start_seconds": None,
    "end_seconds": None,
    "evidence": "none",
    "reason": "変化なし",
}


def video(path, frames=40, size=(640, 400)):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5, size)
    assert writer.isOpened()
    for index in range(frames):
        writer.write(np.full((size[1], size[0], 3), 40 + index, np.uint8))
    writer.release()
    return path


def test_judge_spec_takes_an_optional_effort():
    assert parse_judge("codex:model-a") == ("codex", "model-a", "")
    assert parse_judge("claude:sonnet@low") == ("claude", "sonnet", "low")
    for broken in ("codex", "codex:", "codex:model@", "codex:model@fast", "other:model"):
        with pytest.raises(CommandError):
            parse_judge(broken)


def test_codex_passes_the_effort_as_a_config_override(tmp_path):
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        (tmp_path / "result.json").write_text("{}", encoding="utf-8")

    CodexCliJudge(run, model="model-a", effort="low").judge(tmp_path, "指示", [], {})
    CodexCliJudge(run, model="model-a").judge(tmp_path, "指示", [], {})
    with_effort, without = seen
    index = with_effort.index("-c")
    assert with_effort[index + 1] == 'model_reasoning_effort="low"'
    assert "-c" not in without


def test_claude_passes_the_effort_and_gemini_takes_it_from_the_model_name(tmp_path, monkeypatch):
    executable = tmp_path / "claude"
    executable.touch()
    monkeypatch.setattr("shutil.which", lambda _: str(executable))
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        response = {"type": "result", "subtype": "success", "structured_output": ANSWER}
        return subprocess.CompletedProcess(args, 0, json.dumps(response).encode() + b"\n")

    monkeypatch.setattr("subprocess.run", run)
    schema = {"required": list(ANSWER)}
    build_judge("claude", "sonnet", "low").judge(tmp_path, "指示", [], schema)
    build_judge("claude", "sonnet").judge(tmp_path, "指示", [], schema)
    assert seen[0][seen[0].index("--effort") + 1] == "low"
    assert "--effort" not in seen[1]
    with pytest.raises(ValueError, match="モデル名"):
        build_judge("gemini", "gemini-model-low", "low")


def test_sheet_options_reduce_the_frames_and_the_tile_size(tmp_path):
    path = video(tmp_path / "clip.mp4")
    roi = ROI(0, 0, 1, 1)
    default = tmp_path / "default.jpg"
    assert len(write_sheet(path, default, roi, 8)) == 5
    small = tmp_path / "small.jpg"
    stamps = write_sheet(path, small, roi, 8, sheet=SheetOptions(max_frames=3, tile_width=240))
    assert len(stamps) == 3
    # 既定は幅480、指定すると幅240（高さは 480:300 の比で決まる）。上の26画素は秒数の帯。
    assert cv2.imread(str(default)).shape[:2] == (2 * (300 + 26), 4 * 480)
    assert cv2.imread(str(small)).shape[:2] == (150 + 26, 3 * 240)
    with pytest.raises(ValueError, match="コマ数"):
        SheetOptions(max_frames=1)
    with pytest.raises(ValueError, match="幅"):
        SheetOptions(tile_width=4000)


class Judge:
    name = "偽の判定器"

    def __init__(self, usage):
        self.usage = usage
        self.last_usage = None
        self.images = []

    def check(self):
        pass

    def judge(self, folder, prompt, images, schema):
        self.images.append(list(images))
        self.last_usage = self.usage
        return ANSWER


def test_benchmark_reports_tokens_per_judge_and_applies_the_sheet_options(tmp_path):
    path = video(tmp_path / "clip.mp4")
    row = {"id": "c1", "duration": 8, "source": "detected"}
    cases = [Case(row, path, "none", ()), Case({**row, "id": "c2"}, path, "none", ())]
    measured, silent = Judge(TokenUsage(20000, 15000, 300)), Judge(None)
    rows, summaries = evaluate(
        cases,
        {"codex:a": measured, "gemini:b": silent},
        {"": Scene(ROI(0, 0, 1, 1))},
        sheet=SheetOptions(difference=False),
    )
    assert rows[0]["results"]["codex:a"]["tokens"] == {
        "input": 20000,
        "cached": 15000,
        "output": 300,
    }
    assert "tokens" not in rows[0]["results"]["gemini:b"]
    assert summaries["codex:a"]["tokens"] == {
        "measured": 2,
        "input": 20000,
        "cached": 15000,
        "output": 300,
    }
    assert summaries["gemini:b"]["tokens"] is None
    assert measured.images == [["frames.jpg"], ["frames.jpg"]]
