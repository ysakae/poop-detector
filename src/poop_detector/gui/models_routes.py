"""管理 › 判定器とモデル。動画の自動判定の判定器と、犬検出モデルの状態を示し、判定器を選び直す。

判定器の選び直しは、管理 › 設定と同じ設定ファイルの書き換え（settings_file.write）と検証を使う。
判定器が使える状態かは、監視と同じ check（選択した CLI の起動とログイン）で確かめ、
キーやトークンの値は出さない。比較（video-benchmark）、評価セットの作成と固定（benchmark-dataset）、
モデルの取得（setup-dog-model）、処理負荷の確認（check-dog）は時間がかかるか送信を伴うので CLI に
残し、ここでは保存済みの結果と評価セットを読むだけ。録画ごとの回答は、台帳にある録画だけを出す。
ルートの表への登録はこのモジュールを読み込んだときに行う（asgi.py が読み込む）。
"""

from dataclasses import replace
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
from ..video.providers import PROVIDER_NAMES, build_judge
from .pages import Query, local_time, message_page
from .routes import Form, audit, first, get, post
from .templating import render_template

if TYPE_CHECKING:
    from .exchange import Exchange as Handler

PAGE = "/manage/models"
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


def _judges(handler: "Handler") -> list[dict[str, Any]]:
    return [
        {
            "provider": PROVIDER_NAMES.get(usage.provider, usage.provider),
            "model": usage.model or "（既定）",
            "total": usage.total,
            "errors": usage.errors,
            "last_at": local_time(usage.last_at),
            "last_failed": usage.last_failed,
        }
        for usage in handler.app.clips.assessments.by_judge()
    ]


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
        judges=_judges(handler),
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
