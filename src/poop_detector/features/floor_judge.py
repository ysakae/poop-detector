"""床の候補（experimental.floor_objects）の画像を判定器に渡し、嘔吐・下痢らしい跡かを確かめる。

監視は候補を記録するだけで、判定器は GUI のサーバーの見張り（commands.context.floor_judge_watch）
が1件ずつ呼ぶ（呼び出しは時間がかかり、監視ループを止めないため）。画像を提供元に送るので、
floor.judge を有効にしたときだけ動く。判定は確率ではなく候補で、獣医師の判断の代わりではない。
"""

import logging
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import cv2

from ..records.floor_objects import FLOOR_LABELS, FloorObject, FloorObjects
from ..timezones import local_now
from ..video.judge_failures import failure_cause
from ..video.judge_runs import JudgeRunLog
from ..video.review import VideoJudge

logger = logging.getLogger(__name__)
PURPOSE = "floor"
# 1時間に判定器を呼ぶ回数の上限（動画の自動判定の review.max_per_hour とは別に数える）。
HOURLY_LIMIT = 6
# この時間より前に見つけた候補は判定しない（GUI を長く止めていたときに、古い候補をまとめて送らない）。
RESUME_HOURS = 24
# 続けて起きたかを数える時間。
REPEAT_HOURS = 24
# 拡大の画像で、候補の枠の周りに足す幅（枠の幅・高さに対する倍率）。
CROP_MARGIN = 1.5
CROP_WIDTH = 480
MAX_REASON = 200
# 知らせるラベル（嘔吐・下痢らしい跡）。
NOTICE_LABELS = ("vomit", "diarrhea")
JUDGE_LABELS = (*FLOOR_LABELS, "unknown")
CONFIDENCES = ("low", "medium", "high")
INVALID_OUTPUT = "invalid_output"

FLOOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["label", "confidence", "reason"],
    "properties": {
        "label": {"type": "string", "enum": list(JUDGE_LABELS)},
        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
        "reason": {"type": "string", "maxLength": MAX_REASON},
    },
}

JudgeChoice = tuple[VideoJudge, str, str | None]


def floor_prompt(pet_name: str) -> str:
    """判定器への指示。画像は全体（赤い枠付き）と、枠の周りの拡大の2枚。"""
    dog = f"犬（名前は{pet_name}）" if pet_name else "犬"
    return (
        f"室内のサークルを上から写したカメラの画像です。{dog}がいない間に、床に新しく現れて"
        "しばらく動かなかった物を、1枚目の赤い枠で示しています。2枚目は赤い枠の周りの拡大です。"
        "赤い枠の中の物が何かを、次のラベルから1つ選んでください。\n"
        "- vomit: 嘔吐物（液体や泡、未消化のフードが広がった跡）\n"
        "- diarrhea: 下痢（形のないゆるい便の跡）\n"
        "- foreign: 異物（ティッシュ・ビニール・おもちゃの破片など、誤飲のおそれのある物）\n"
        "- toy: おもちゃや寝具など、もともとある物が動いただけ\n"
        "- false: 影や光の反射、床の模様など、物がない\n"
        "- unknown: 画像からは判断できない\n"
        "確かでなければ unknown にしてください。confidence は low・medium・high、"
        f"reason は画像から見えた根拠を日本語で{MAX_REASON}文字以内で書いてください。"
    )


def parse_judgement(output: object) -> tuple[str, str]:
    """判定器の出力から (ラベル, 根拠) を取り出す。形が違えば ValueError。"""
    if not isinstance(output, dict):
        raise ValueError("判定の形が違います")
    label, confidence, reason = (output.get(k) for k in ("label", "confidence", "reason"))
    if label not in JUDGE_LABELS:
        raise ValueError("label が想定外です")
    if confidence not in CONFIDENCES:
        raise ValueError("confidence が想定外です")
    return str(label), str(reason or "")[:MAX_REASON]


class FloorNoticeSender(Protocol):
    """嘔吐・下痢らしい跡を知らせる通知先。"""

    def send_floor_judgement(self, label: str, object_id: str, repeat: int) -> bool: ...


class FloorJudgeWorker:
    """未判定の床の候補を1件ずつ判定する。スレッドは持たず、poll_once を呼ばれて動く。"""

    def __init__(
        self,
        objects: FloorObjects,
        runs: JudgeRunLog,
        choose: Callable[[], JudgeChoice],
        notifier: FloorNoticeSender,
        workdir: Path,
        *,
        pet_name: str = "",
        clock: Callable[[], datetime] = local_now,
    ) -> None:
        self.objects, self.runs, self.choose = objects, runs, choose
        self.notifier, self.workdir = notifier, workdir
        self.pet_name, self.clock = pet_name, clock

    def poll_once(self) -> bool:
        """候補を1件判定したら True。なし・上限に達していれば False。"""
        now = self.clock()
        if self.runs.count_since(now - timedelta(hours=1), (PURPOSE,)) >= HOURLY_LIMIT:
            return False
        candidate = self.objects.claim_unjudged(now - timedelta(hours=RESUME_HOURS))
        if candidate is None:
            return False
        judge, provider, model = self.choose()
        try:
            label, reason = self._judge(judge, candidate)
        except Exception as error:
            # 文言は出さず（CLI の応答を含みうる）、種類だけを残す。
            cause = failure_cause(error) if not isinstance(error, ValueError) else None
            kind = cause.kind if cause else INVALID_OUTPUT
            logger.warning("床の候補を判定できませんでした（%s）", type(error).__name__)
            self.runs.record(
                provider,
                model,
                PURPOSE,
                failed=True,
                at=now,
                failure=kind,
                failure_detail=cause.detail if cause else None,
            )
            self.objects.fail_judgement(candidate.id, kind)
            return True
        usage = getattr(judge, "last_usage", None)
        self.runs.record(provider, model, PURPOSE, failed=False, at=now, usage=usage)
        self.objects.save_judgement(candidate.id, label, reason)
        if label in NOTICE_LABELS:
            repeat = self.objects.count_judged((label,), now - timedelta(hours=REPEAT_HOURS))
            self.notifier.send_floor_judgement(FLOOR_LABELS[label], candidate.id, repeat)
        return True

    def _judge(self, judge: VideoJudge, candidate: FloorObject) -> tuple[str, str]:
        self.workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.workdir) as name:
            folder = Path(name)
            image = cv2.imread(str(self.objects.image_path(candidate.id)))
            if image is None:
                raise OSError("床の候補の画像を読めません")
            cv2.imwrite(str(folder / "whole.jpg"), image)
            cv2.imwrite(str(folder / "closeup.jpg"), _closeup(image, candidate.box))
            output = judge.judge(
                folder, floor_prompt(self.pet_name), ["whole.jpg", "closeup.jpg"], FLOOR_SCHEMA
            )
        return parse_judgement(output)


def _closeup(image: Any, box: tuple[float, float, float, float]) -> Any:
    """正規化した枠の周りを CROP_MARGIN だけ広げて切り出し、CROP_WIDTH に拡大する。"""
    height, width = image.shape[:2]
    x1, y1, x2, y2 = box[0] * width, box[1] * height, box[2] * width, box[3] * height
    margin_x, margin_y = (x2 - x1) * CROP_MARGIN, (y2 - y1) * CROP_MARGIN
    left, top = max(0, int(x1 - margin_x)), max(0, int(y1 - margin_y))
    right, bottom = min(width, int(x2 + margin_x) + 1), min(height, int(y2 + margin_y) + 1)
    crop = image[top:bottom, left:right]
    scale = CROP_WIDTH / max(1, crop.shape[1])
    size = (CROP_WIDTH, max(1, round(crop.shape[0] * scale)))
    return cv2.resize(crop, size, interpolation=cv2.INTER_CUBIC)
