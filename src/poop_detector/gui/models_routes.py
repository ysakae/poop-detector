"""管理 › 判定器とモデル。動画の自動判定の判定器と、犬検出モデルの状態を示し、判定器を選び直す。

判定器の選び直しは、管理 › 設定と同じ設定ファイルの書き換え（settings_file.write）と検証を使う。
判定器が使える状態かは、監視と同じ check（選択した CLI の起動とログイン）で確かめ、
キーやトークンの値は出さない。比較（video-benchmark）、評価セットの作成と固定（benchmark-dataset）、
モデルの取得（setup-dog-model）、処理負荷の確認（check-dog）は時間がかかるか送信を伴うので CLI に
残し、ここでは保存済みの結果と評価セットを読むだけ。録画ごとの回答は、台帳にある録画だけを出す。
ルートの表への登録はこのモジュールを読み込んだときに行う（asgi.py が読み込む）。
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..commands.datasets import actual_signatures, resolve
from ..config import Config, ConfigError
from ..features.dog_status import load_check, model_status
from ..settings_file import SettingsConflictError, changes, digest, write
from ..video import benchmark_sets
from ..video.benchmark_results import BenchmarkResult, load_results
from ..video.benchmark_sets import LABELS, SPLIT_NAMES
from ..video.clips import CLIP_LABELS, ClipStore
from ..video.judge_failures import FAILURE_NAMES
from ..video.judge_models import UNAVAILABLE, list_models
from ..video.judge_runs import JudgeRunUsage
from ..video.plan_usage import PlanUsageBoard, QuotaWindow
from ..video.providers import PROVIDER_NAMES, build_judge
from ..video.review_status import ReviewStatus, review_status
from .pages import Query, local_time, message_page
from .routes import Form, audit, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

PAGE = "/manage/models"
PLAN_USAGE = f"{PAGE}/plan-usage"
CATALOG = f"{PAGE}/catalog"
CUSTOM_MODEL = ":custom"
DOG_MODEL_DIR = Path("models") / "dog-yolox"


def _percent(value: float | None) -> str:
    return "―" if value is None else f"{value:.1%}"


def readiness(config: Config) -> str | None:
    """判定器が使えない理由。使えれば None。文言はこのパッケージの案内だけで、値を含まない。"""
    try:
        build_judge(config.review_provider, config.review_model).check()
    except ValueError as error:
        return str(error)
    return None


PURPOSE_NAMES = {"review": "監視・保存済み動画", "benchmark": "判定器の比較"}


def _review_status(status: ReviewStatus) -> dict[str, Any]:
    """監視中の判定の状況を、画面の文に整える。state は色付きのラベルの種類（ok・warn）。"""
    limit = f"上限 {status.max_per_hour}回" if status.max_per_hour else "上限なし"
    if status.capped:
        scope, scope_state = "上限に達したため、枠が空くまで判定を待っています", "warn"
    elif status.postponed:
        scope, scope_state = f"{'、'.join(status.postponed)}を後回しにしています", "warn"
    else:
        scope, scope_state = "すべての動画を判定します", "ok"
    if status.outage_since:
        judge = f"{local_time(status.outage_since)} から失敗が続いています（停止として記録）"
        judge_state = "warn"
    elif status.judge_failures:
        judge, judge_state = (
            f"直近の呼び出しが {status.judge_failures}回続けて失敗しています",
            "warn",
        )
    else:
        judge, judge_state = "続けての失敗はありません", "ok"
    return {
        "calls": f"{status.used}回（{limit}）",
        "scope": scope,
        "scope_state": scope_state,
        "waiting": "、".join(f"{name} {count}本" for name, count in status.waiting),
        "failed": f"{status.failed}本",
        "judge": judge,
        "judge_state": judge_state,
        "fallback": "、".join(status.fallback) or "なし（review.fallback）",
    }


def _failure_breakdown(clips: ClipStore, now: datetime) -> str:
    """直近24時間に監視と video-review で失敗した呼び出しの、種類ごとの回数。"""
    counts = clips.judge_runs.failure_counts(now - timedelta(hours=24))
    if not counts:
        return "失敗はありません"
    return "、".join(
        f"{FAILURE_NAMES.get(kind, kind) if kind else '不明'} {count}回" for kind, count in counts
    )


def _tokens(usage: JudgeRunUsage) -> str:
    """直近30日の、1回の呼び出しあたりのトークン数の平均。CLI が数を出した呼び出しがなければ「―」。"""
    if not usage.measured:
        return "―"
    average = [
        round(total / usage.measured)
        for total in (usage.input_tokens, usage.cached_tokens, usage.output_tokens)
    ]
    return f"入力 {average[0]:,}（キャッシュ {average[1]:,}）／出力 {average[2]:,}"


def _judges(handler: "Handler") -> list[dict[str, Any]]:
    return [
        {
            "provider": PROVIDER_NAMES.get(usage.provider, usage.provider),
            "model": usage.model or "（既定）",
            "purpose": PURPOSE_NAMES.get(usage.purpose, usage.purpose),
            "counts": [
                usage.today,
                usage.last_7_days,
                usage.last_30_days,
                usage.this_month,
                usage.total,
            ],
            "failures": usage.failures,
            "last_at": local_time(usage.last_at),
            "last_failed": usage.last_failed,
            "tokens": _tokens(usage),
        }
        for usage in handler.app.clips.judge_runs.usage()
    ]


# 消費の割合がこれ以上なら、グラフの色を注意・警告に変える。
QUOTA_WARN, QUOTA_HIGH = 70, 90


def _remaining(resets_at: datetime, now: datetime) -> str:
    """次に戻るまでの時間を、大きいほうの2つの単位で示す（日と時間、時間と分、分だけ）。"""
    minutes = int((resets_at - now).total_seconds() // 60)
    if minutes <= 0:
        return "まもなく戻ります"
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        span = f"{days}日{hours}時間" if hours else f"{days}日"
    elif hours:
        span = f"{hours}時間{minutes}分" if minutes else f"{hours}時間"
    else:
        span = f"{minutes}分"
    return f"あと{span}で戻る（{resets_at.astimezone().strftime('%m/%d %H:%M')}）"


def quota_view(label: str, window: QuotaWindow | None, now: datetime) -> dict[str, Any]:
    """1つの制限の窓のグラフの値。制限がなければ percent を None にする。"""
    if window is None:
        return {"label": label, "percent": None}
    percent = window.used_percent
    level = "high" if percent >= QUOTA_HIGH else "warn" if percent >= QUOTA_WARN else "normal"
    resets = "" if window.resets_at is None else _remaining(window.resets_at, now)
    return {"label": label, "percent": percent, "level": level, "resets": resets}


def _plans(board: PlanUsageBoard, now: datetime) -> dict[str, Any]:
    """直近に問い合わせたプランの使用量。CLI ごとに、制限のまとまりと5時間・週間の窓を並べる。"""
    usages = list(board.snapshot().values())
    providers = [
        {
            "name": PROVIDER_NAMES.get(usage.provider, usage.provider),
            "problem": None if usage.groups else usage.problem,
            "groups": [
                {
                    "name": group.name,
                    "windows": [
                        quota_view("5時間", group.five_hour, now),
                        quota_view("週間", group.weekly, now),
                    ],
                }
                for group in usage.groups
            ],
        }
        for usage in usages
    ]
    checked = max((usage.checked_at for usage in usages), default=None)
    return {
        "providers": providers,
        "checked_at": local_time(checked.isoformat()) if checked else "",
    }


ANSWER_NAMES = {**CLIP_LABELS, "uncertain": "判定不能", "error": "エラー"}


def _answers(result: BenchmarkResult, clips: ClipStore) -> list[dict[str, Any]]:
    """録画ごとの正解と回答。結果のファイルは人が作ったものなので、台帳にある録画だけを出す。"""
    rows = []
    for case in result.cases:
        if clips.get(case.clip_id) is None:
            continue
        rows.append(
            {
                "id": case.clip_id,
                "label": ANSWER_NAMES[case.label],
                "split": SPLIT_NAMES.get(case.split, "―"),
                "tags": "、".join(case.tags) or "―",
                "answers": [
                    {"name": ANSWER_NAMES[answer], "miss": answer != case.label}
                    for answer in case.answers
                ],
            }
        )
    return rows


def _benchmarks(results: list[BenchmarkResult], clips: ClipStore) -> list[dict[str, Any]]:
    return [
        {
            "created_at": local_time(result.created_at),
            "dataset": result.dataset,
            "dataset_version": result.dataset_version,
            "judges": [
                {
                    "name": judge.name,
                    "model": judge.model,
                    "cli_version": judge.cli_version or "不明",
                }
                for judge in result.judges
            ],
            # 録画ごとの見比べは、新しい結果だけに出す。
            "answers": _answers(result, clips) if index == 0 else [],
            "scores": [
                {
                    "name": score.name,
                    "cases": score.cases,
                    "feces": f"{_percent(score.feces_precision)} / {_percent(score.feces_recall)}",
                    "urine": f"{_percent(score.urine_precision)} / {_percent(score.urine_recall)}",
                    "uncertain": score.uncertain,
                    "errors": score.errors,
                }
                for score in result.scores
            ],
        }
        for index, result in enumerate(results)
    ]


def _datasets(root: Path, clips: ClipStore) -> tuple[list[dict[str, Any]], int]:
    """保存先の評価セットの件数と、評価の前に直すべきこと。"""
    found, unreadable = benchmark_sets.load_datasets(root)
    shown = []
    for file_name, dataset in found:
        summary = benchmark_sets.coverage(dataset)
        problems = benchmark_sets.inspect(dataset, clips, actual_signatures(resolve(dataset, root)))
        shown.append(
            {
                "file": file_name,
                "name": dataset.name,
                "version": "下書き" if dataset.version is None else f"版{dataset.version}",
                "created_at": local_time(dataset.created_at),
                "labels": [CLIP_LABELS[label] for label in LABELS],
                "splits": [
                    {"name": SPLIT_NAMES[split], "counts": list(counts.values())}
                    for split, counts in summary["by_split"].items()
                ],
                "tags": [
                    {"name": tag, "counts": list(counts.values())}
                    for tag, counts in summary["by_tag"].items()
                ],
                "summary": summary,
                "problems": problems.lines(),
            }
        )
    return shown, unreadable


def _dog(root: Path) -> dict[str, Any]:
    directory = root / DOG_MODEL_DIR
    status = model_status(directory)
    check = load_check(directory)
    return {
        "absent": status.absent,
        "verified": status.verified,
        "missing": status.missing,
        "mismatched": status.mismatched,
        "check": None
        if check is None
        else {
            "checked_at": local_time(check.checked_at),
            "repeat": check.repeat,
            "dogs": check.dogs,
            "inference": f"{check.mean_inference_ms:.1f} ms",
            "cpu": f"{check.cpu_percent:.1f}%",
            "rss": f"{check.rss_mib:.0f} MiB",
        },
    }


def _render(
    handler: "Handler",
    path: Path | None,
    *,
    status: int = 200,
    provider: str | None = None,
    model: str | None = None,
    error: str | None = None,
    notice: str | None = None,
) -> None:
    app = handler.app
    current = Config.load(path) if path is not None else Config()
    results, unreadable = load_results(app.root)
    datasets, unreadable_sets = _datasets(app.root, app.clips)
    now = datetime.now(UTC)
    page = render_template(
        "models.html",
        title=app.title,
        token=handler.csrf,
        page=PAGE,
        editable=path is not None,
        base=digest(path) if path is not None else "",
        current_name=PROVIDER_NAMES.get(current.review_provider, current.review_provider),
        current_model=current.review_model or "（既定）",
        problem=readiness(current),
        review_enabled=current.video_review_enabled,
        providers=list(PROVIDER_NAMES.items()),
        provider=current.review_provider if provider is None else provider,
        model=current.review_model if model is None else model,
        custom_model=CUSTOM_MODEL,
        catalog=CATALOG,
        review_status=_review_status(review_status(app.clips, app.outages, current, now)),
        failure_breakdown=_failure_breakdown(app.clips, now),
        tray_stay_counts=app.clips.assessments.tray_stay_counts(
            (now - timedelta(days=7)).isoformat()
        ),
        judges=_judges(handler),
        plans=_plans(app.plan_usage, now),
        plan_action=PLAN_USAGE,
        benchmarks=_benchmarks(results, app.clips),
        unreadable=unreadable,
        datasets=datasets,
        unreadable_sets=unreadable_sets,
        dog=_dog(app.root),
        error=error,
        notice=notice,
    )
    handler.respond(status, page)


@get(PAGE, role="admin")
def models_page(handler: "Handler", query: Query) -> None:
    notice = None
    if first(query, "saved") == "1":
        notice = "保存しました。動いている監視は、次の動画の判定から新しい判定器を使います。"
    try:
        _render(handler, handler.app.config_path, notice=notice)
    except ConfigError as error:
        page = message_page(
            "設定ファイルを読めません",
            str(error),
            "/manage",
            "管理へ戻る",
            title=handler.app.title,
            wrap_link=True,
        )
        handler.respond(400, page)


@post(PAGE, role="admin")
def choose_judge(handler: "Handler", form: Form) -> None:
    """判定器とモデルを設定ファイルに書く。検証は設定の表（config.SETTINGS）の規則。"""
    path = handler.app.config_path
    if path is None:
        handler.respond(400, "設定ファイルの場所が分からないため、保存できません")
        return
    provider = form.fields.get("provider", "").strip()
    model = form.fields.get("model", "").strip()
    if model == CUSTOM_MODEL:
        model = form.fields.get("custom_model", "").strip()
    current = Config.load(path)
    try:
        config = replace(current, review_provider=provider, review_model=model)
        found = changes(current, config)
        if not found:
            _render(handler, path, notice="変更はありません。")
            return
        write(path, config, base=form.fields.get("base", ""))
    except SettingsConflictError as error:
        _render(handler, path, status=409, error=str(error))
        return
    except ConfigError as error:
        _render(handler, path, status=400, provider=provider, model=model, error=str(error))
        return
    summary = "、".join(f"{c.name}: {c.before or '（空）'} → {c.after or '（空）'}" for c in found)
    audit(handler, "judge.select", summary)
    handler.redirect(f"{PAGE}?saved=1")


@post(CATALOG, role="admin")
def model_catalog(handler: "Handler", form: Form) -> None:
    provider = form.fields.get("provider", "")
    if provider not in PROVIDER_NAMES:
        handler.respond(400, "判定器を選んでください")
        return
    try:
        models = list_models(provider)
    except ValueError:
        handler.respond(503, UNAVAILABLE)
        return
    handler.respond(200, json.dumps({"models": models}), "application/json; charset=utf-8")


@post(PLAN_USAGE, role="admin")
def ask_plan_usage(handler: "Handler", _form: Form) -> None:
    """各 CLI にプランの使用量を問い合わせる（モデルは呼ばない）。数秒かかるので押したときだけ。"""
    handler.app.plan_usage.refresh()
    handler.redirect(PAGE)
