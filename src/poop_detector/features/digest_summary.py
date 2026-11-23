"""留守中のまとめに、判定器による短い文章の要約を付ける（digest.summary）。

GUI のサーバーの見張り（commands.context.digest_summary_watch）が、終わった外出と、
画面で頼まれた作り直しを1件ずつ処理する。既定では年表の文字だけを送り、
digest.summary_images を有効にしたときだけ静止画を MAX_IMAGES 枚まで添える。
要約は記録を見返すきっかけで、獣医師の判断の代わりではない。
"""

import logging
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..records.away_periods import AwayPeriods
from ..records.digest_summaries import DigestSummaries
from ..records.events import Store
from ..timezones import local_now
from ..video.clips import ClipStore
from ..video.judge_failures import failure_cause
from ..video.judge_runs import JudgeRunLog
from ..video.review import VideoJudge
from .digest import DigestSnapshots
from .digest_timeline import TimelineEntry, collect

logger = logging.getLogger(__name__)
PURPOSE = "digest"
HOURLY_LIMIT = 4
# この時間より前に終わった外出は、頼まれない限り要約しない。
RESUME_HOURS = 24
MAX_IMAGES = 4
MAX_SUMMARY = 400
MAX_POINTS = 5
MAX_POINT = 80
# 指示に並べる出来事の数の上限（長い外出で指示が長くなりすぎないように）。
MAX_ENTRIES = 80
INVALID_OUTPUT = "invalid_output"

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "points"],
    "properties": {
        "summary": {"type": "string", "maxLength": MAX_SUMMARY},
        "points": {
            "type": "array",
            "maxItems": MAX_POINTS,
            "items": {"type": "string", "maxLength": MAX_POINT},
        },
    },
}

JudgeChoice = tuple[VideoJudge, str, str | None]


def _length(minutes: float) -> str:
    hours, rest = divmod(round(minutes), 60)
    return f"{hours}時間{rest}分" if hours else f"{rest}分"


def summary_prompt(
    pet_name: str,
    start: datetime,
    end: datetime,
    entries: Sequence[TimelineEntry],
    asleep_minutes: int | None,
    images: int,
) -> str:
    """判定器への指示。年表の文字を並べ、静止画があればその枚数を伝える。"""
    dog = f"犬（名前は{pet_name}）" if pet_name else "犬"
    lines = [f"{e.at.astimezone():%H:%M} {e.text}" for e in entries[:MAX_ENTRIES]]
    if len(entries) > MAX_ENTRIES:
        lines.append(f"（ほかに{len(entries) - MAX_ENTRIES}件）")
    facts = [
        f"外出: {start.astimezone():%m/%d %H:%M}〜{end.astimezone():%m/%d %H:%M}"
        f"（{_length((end - start).total_seconds() / 60)}）",
    ]
    if asleep_minutes is not None:
        facts.append(f"寝ていた時間: {_length(asleep_minutes)}")
    pictures = (
        f"\n添付の{images}枚は、外出の間のカメラの静止画を時刻の順に間引いたものです。"
        if images
        else ""
    )
    return (
        f"家族が外出していた間の、室内のサークルにいる{dog}の記録です。"
        "見守りのカメラが自動で記録したもので、誤りを含むことがあります。\n"
        + "\n".join(facts)
        + "\n出来事:\n"
        + ("\n".join(lines) if lines else "（記録された出来事はありません）")
        + pictures
        + "\n\n家族が帰宅してすぐ読めるよう、留守中の様子を日本語で3〜5文に要約してください"
        f"（{MAX_SUMMARY}文字以内）。points には、家族が確かめたほうがよい点を"
        f"{MAX_POINTS}つまで、それぞれ{MAX_POINT}文字以内で挙げてください（なければ空）。"
        "記録にないことは書かず、病気の診断や確率のような言い方はしないでください。"
    )


def parse_summary(output: object) -> tuple[str, list[str]]:
    """判定器の出力から (要約, 気になる点) を取り出す。形が違えば ValueError。"""
    if not isinstance(output, Mapping):
        raise ValueError("要約の形が違います")
    summary, points = output.get("summary"), output.get("points")
    if not isinstance(summary, str) or not isinstance(points, list):
        raise ValueError("要約の形が違います")
    cleaned = [str(point)[:MAX_POINT] for point in points if str(point).strip()][:MAX_POINTS]
    return summary[:MAX_SUMMARY], cleaned


class DigestSummaryWorker:
    """要約を1件ずつ作る。スレッドは持たず、poll_once を呼ばれて動く。"""

    def __init__(
        self,
        store: Store,
        clips: ClipStore,
        root: Path,
        summaries: DigestSummaries,
        periods: AwayPeriods,
        runs: JudgeRunLog,
        choose: Callable[[], JudgeChoice],
        workdir: Path,
        *,
        images: bool = False,
        pet_name: str = "",
        clip_labels: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = local_now,
    ) -> None:
        self.store, self.clips, self.root = store, clips, root
        self.summaries, self.periods, self.runs = summaries, periods, runs
        self.choose, self.workdir, self.images = choose, workdir, images
        self.pet_name, self.clip_labels, self.clock = pet_name, clip_labels or {}, clock

    def poll_once(self) -> bool:
        """要約を1件作ったら（失敗も含む）True。なし・上限に達していれば False。"""
        now = self.clock()
        if self.runs.count_since(now - timedelta(hours=1), (PURPOSE,)) >= HOURLY_LIMIT:
            return False
        period_id = self.summaries.claim(now - timedelta(hours=RESUME_HOURS))
        if period_id is None:
            return False
        period = self.periods.get(period_id)
        judge, provider, model = self.choose()
        try:
            if period is None or period.ended_at is None:
                raise ValueError("外出の区間がありません")
            summary, points = self._summarize(judge, period_id, period.started_at, period.ended_at)
        except Exception as error:
            # 文言は出さず（CLI の応答を含みうる）、種類だけを残す。
            cause = None if isinstance(error, ValueError) else failure_cause(error)
            kind = cause.kind if cause else INVALID_OUTPUT
            logger.warning("留守中のまとめの要約を作れませんでした（%s）", type(error).__name__)
            self.runs.record(
                provider,
                model,
                PURPOSE,
                failed=True,
                at=now,
                failure=kind,
                failure_detail=cause.detail if cause else None,
            )
            self.summaries.fail(period_id, kind)
            return True
        usage = getattr(judge, "last_usage", None)
        self.runs.record(provider, model, PURPOSE, failed=False, at=now, usage=usage)
        self.summaries.finish(period_id, summary, points, provider, model)
        return True

    def _summarize(
        self, judge: VideoJudge, period_id: str, start: datetime, end: datetime
    ) -> tuple[str, list[str]]:
        content = collect(self.store, self.clips, self.root, start, end, self.clip_labels)
        self.workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.workdir) as name:
            folder = Path(name)
            images = self._copy_images(period_id, folder) if self.images else []
            prompt = summary_prompt(
                self.pet_name, start, end, content.entries, content.asleep_minutes, len(images)
            )
            output = judge.judge(folder, prompt, images, SUMMARY_SCHEMA)
        return parse_summary(output)

    def _copy_images(self, period_id: str, folder: Path) -> list[str]:
        frames = DigestSnapshots(self.root).frames(period_id)
        if not frames:
            return []
        step = max(1, len(frames) // MAX_IMAGES)
        chosen = frames[::step][:MAX_IMAGES]
        names = []
        for index, frame in enumerate(chosen):
            name = f"snapshot-{index + 1}.jpg"
            shutil.copyfile(frame, folder / name)
            names.append(name)
        return names
