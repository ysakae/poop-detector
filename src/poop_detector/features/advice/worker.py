"""相談の回答とカロリー探しの仕事を取り出し、判定器（CLI）を呼んで結果を書き戻す。

応答の本文と指示文はログに出さない。
"""

import logging
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from ...records import advice
from ...records.database import Database
from ...records.events import Store
from ...timezones import local_now
from ...video.judge_failures import FailureCause, failure_cause
from ...video.judge_runs import JudgeRunLog
from ...video.providers import WebLookup, supports_lookup
from ...video.review import VideoJudge
from .context import build_context
from .jobs import (
    Job,
    claim_next,
    expire_stale,
    fail_job,
    finish_answer,
    finish_lookup,
)
from .prompts import (
    ANSWER_SCHEMA,
    LOOKUP_SCHEMA,
    answer_prompt,
    lookup_prompt,
    parse_answer,
    parse_lookup,
)

logger = logging.getLogger(__name__)

# 判定器・用途を記録する provider 名・model。
JudgeChoice = tuple[VideoJudge, str, str | None]
PURPOSES = ("advice", "food_lookup")
INVALID_OUTPUT = "invalid_output"


class AdviceWorker:
    """待ちの仕事を1件ずつ処理する。スレッドは持たず、poll_once を呼ばれて動く。"""

    def __init__(
        self,
        database: Database,
        store: Store,
        choose: Callable[[], JudgeChoice],
        pet_name: Callable[[], str],
        workdir: Path,
        clock: Callable[[], datetime] = local_now,
    ) -> None:
        self.database = database
        self.store = store
        self.choose = choose
        self.pet_name = pet_name
        self.workdir = workdir
        self.clock = clock
        self.runs = JudgeRunLog(database)

    def poll_once(self) -> bool:
        """仕事を1件処理したら True。なし・上限に達していれば False。"""
        now = self.clock()
        expire_stale(self.database, now)
        used = self.runs.count_since(now - timedelta(hours=1), PURPOSES)
        job = claim_next(self.database, now, used)
        if job is None:
            return False
        try:
            self._process(job)
        except Exception as error:
            # 仕事を running のまま残さない。文言は出さず、種類だけ残す。
            logger.warning("相談の仕事で問題が起きました（%s）", type(error).__name__)
            cause = failure_cause(error)
            fail_job(self.database, job, cause.kind, self.clock())
        return True

    def _process(self, job: Job) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.workdir) as folder:
            if job.kind == "answer":
                self._answer(job, Path(folder))
            else:
                self._lookup(job, Path(folder))

    def _record(self, purpose: str, choice: JudgeChoice, cause: FailureCause | None) -> None:
        judge, provider, model = choice
        self.runs.record(
            provider,
            model,
            purpose,
            failed=cause is not None,
            at=self.clock(),
            usage=getattr(judge, "last_usage", None),
            failure=cause.kind if cause else None,
            failure_detail=cause.detail if cause else None,
        )

    def _call(
        self,
        purpose: str,
        choice: JudgeChoice,
        call: Callable[[], object],
        parse: Callable[[object], Any],
    ) -> tuple[Any, FailureCause | None]:
        """判定器を呼んで解釈し、1行を記録する。失敗なら (None, 原因)。"""
        try:
            output = call()
        except Exception as error:
            cause = failure_cause(error)
        else:
            try:
                parsed = parse(output)
            except ValueError:
                cause = FailureCause(INVALID_OUTPUT)
            else:
                self._record(purpose, choice, None)
                return parsed, None
        self._record(purpose, choice, cause)
        return None, cause

    def _answer(self, job: Job, folder: Path) -> None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT thread_id, seq FROM advice_messages WHERE id=?", (job.id,)
            ).fetchone()
        rows = advice.messages(self.database, row["thread_id"])
        seq = int(row["seq"])
        question = next(m.body for m in rows if m.seq == seq - 1)
        history = [m for m in rows if m.seq < seq - 1]
        context = build_context(self.database, self.store, self.pet_name(), self.clock().date())
        choice = self.choose()
        judge, provider, model = choice
        prompt = answer_prompt(context, history, question)
        body, cause = self._call(
            "advice",
            choice,
            lambda: judge.judge(folder, prompt, (), ANSWER_SCHEMA),
            parse_answer,
        )
        if cause is not None:
            fail_job(self.database, job, cause.kind, self.clock())
            return
        finish_answer(self.database, job.id, body, provider, model, self.clock())

    def _lookup(self, job: Job, folder: Path) -> None:
        choice = self.choose()
        judge, _, _ = choice
        if not supports_lookup(judge):
            fail_job(self.database, job, "unsupported", self.clock())
            return
        with self.database.connect() as db:
            name = str(
                db.execute("SELECT name FROM food_lookups WHERE id=?", (job.id,)).fetchone()[0]
            )
        prompt = lookup_prompt(name)
        web = cast(WebLookup, judge)
        result, cause = self._call(
            "food_lookup",
            choice,
            lambda: web.lookup(folder, prompt, LOOKUP_SCHEMA),
            parse_lookup,
        )
        if cause is not None:
            fail_job(self.database, job, cause.kind, self.clock())
            return
        finish_lookup(self.database, job.id, result, self.clock())
