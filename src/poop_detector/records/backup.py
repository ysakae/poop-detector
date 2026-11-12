"""データの保存先のバックアップ（ZIP）の作成と復元。

バックアップには、記録のデータベース（events.sqlite3・activity.sqlite3）と、登録情報
（監視範囲、基準画像、トレーの登録、トレー外の範囲、行動マップの床面）と、検知の画像を入れる。
動画（clips/）は選んだときだけ入れる。データベースは SQLite のオンラインバックアップで写すので、
監視の動作中でも壊れない。

秘密の値は入れない。ログインのセッション・登録用 URL・Web Push の購読をコピーから消し、
VACUUM で空き領域からも消す。Web Push の秘密鍵、.env、ライブ映像、ロックファイル、犬検出モデル
（取得し直せる）は入れない。ユーザーとパスキー（公開鍵）は入れるので、復元後も同じパスキーで
ログインできる。

復元は、監視などの処理が止まっていることをロックファイルで確かめ、別の場所に展開して
すべてのファイルのハッシュを確かめてから、元のデータの保存先を `<名前>.before-restore-<時刻>`
へ移して入れ替える。元のデータは消さない。
"""

import fcntl
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from ..errors import AlreadyRunningError, InvalidInputError
from .database import LATEST_VERSION, events_database, schema_version

FORMAT = "poop-detector-backup"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
EVENTS_DB = "events.sqlite3"
ACTIVITY_DB = "activity.sqlite3"
REGISTRATION = ".tray-registration.json"
# コピーのデータベースから消す、秘密の値やこのマシンだけで意味を持つ行のテーブル。
SECRET_TABLES = ("gui_sessions", "enrollments", "push_subscriptions")
# 復元するときに、同じ処理が動いていないことを確かめるロックファイル。
LOCKS = (".capture.lock", ".review.lock", ".activity.lock")
# 復元のとき元のデータから写すディレクトリ（取得し直しが重く、秘密を含まない）。
CARRIED_OVER = ("models",)
# バックアップに入れてよい名前。ここにない名前を含むバックアップは復元しない。
_PART = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
ALLOWED = re.compile(
    "|".join(
        (
            re.escape(EVENTS_DB),
            re.escape(ACTIVITY_DB),
            re.escape(REGISTRATION),
            r"roi\.json",
            r"outside-roi\.json",
            r"activity-layout\.json",
            r"activity-[0-9a-f]{32}\.jpg",
            r"events/[0-9a-f]+\.jpg",
            rf"baseline/{_PART}",
            rf"tray-scenes/[0-9a-f]+/{_PART}(?:/{_PART})*",
            rf"clips/{_PART}\.mp4",
        )
    )
)
SINGLE_FILES = ("roi.json", "outside-roi.json", "activity-layout.json")
DIRECTORIES = ("baseline", "tray-scenes")
STABLE_PHASES = ("idle", "applied", "cancelled")
CHUNK = 1 << 20


@dataclass(frozen=True)
class BackupSummary:
    """作ったバックアップに入れたファイルの数と、そのうちの動画の数。"""

    files: int
    clips: int


@dataclass(frozen=True)
class Manifest:
    """バックアップの目録。files はファイルの名前と SHA-256。"""

    created_at: str
    schema_version: int
    include_clips: bool
    files: dict[str, str]

    @property
    def clips(self) -> int:
        return sum(name.startswith("clips/") for name in self.files)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while block := file.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def _copy_database(source: Path, target: Path, *, scrub: bool) -> None:
    """オンラインバックアップで写し、scrub なら秘密の行を消して空き領域も詰める。"""
    origin = sqlite3.connect(source)
    copy = sqlite3.connect(target)
    try:
        origin.backup(copy)
        if scrub:
            tables = {row[0] for row in copy.execute("SELECT name FROM sqlite_master")}
            for table in SECRET_TABLES:
                if table in tables:
                    # テーブル名は上の固定の定数だけ。
                    copy.execute(f"DELETE FROM {table}")  # noqa: S608
            copy.commit()
            copy.execute("VACUUM")
    finally:
        copy.close()
        origin.close()


def _registration(root: Path, work: Path) -> Path | None:
    """トレーの登録の状態。交換の途中なら、使っている登録だけを残して待機に戻す。"""
    path = root / REGISTRATION
    if not path.is_file():
        return None
    state: dict[str, Any] = json.loads(path.read_text())
    if state.get("phase") not in STABLE_PHASES:
        state = {**state, "phase": "idle", "request": None, "roi": None, "error": None}
    target = work / REGISTRATION
    target.write_text(json.dumps(state, ensure_ascii=False))
    return target


def _sources(root: Path, work: Path, include_clips: bool) -> dict[str, Path]:
    """バックアップに入れる名前と、その中身のファイル。"""
    found: dict[str, Path] = {}
    events_database(root)
    _copy_database(root / EVENTS_DB, work / EVENTS_DB, scrub=True)
    found[EVENTS_DB] = work / EVENTS_DB
    if (root / ACTIVITY_DB).is_file():
        _copy_database(root / ACTIVITY_DB, work / ACTIVITY_DB, scrub=False)
        found[ACTIVITY_DB] = work / ACTIVITY_DB
    registration = _registration(root, work)
    if registration is not None:
        found[REGISTRATION] = registration
    candidates = [root / name for name in SINGLE_FILES]
    candidates += list(root.glob("activity-*.jpg")) + list((root / "events").glob("*.jpg"))
    for directory in DIRECTORIES:
        candidates += list((root / directory).rglob("*"))
    if include_clips:
        candidates += list((root / "clips").glob("*.mp4"))
    for path in candidates:
        name = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink() and ALLOWED.fullmatch(name):
            found[name] = path
    return dict(sorted(found.items()))


def _compression(name: str) -> int:
    return zipfile.ZIP_STORED if name.endswith((".mp4", ".jpg", ".png")) else zipfile.ZIP_DEFLATED


def create_backup(
    root: Path, destination: Path | IO[bytes], *, include_clips: bool, now: datetime | None = None
) -> BackupSummary:
    """root のバックアップを destination（ファイルの場所か、書き込める開いたファイル）に書く。"""
    created = (now or datetime.now(UTC)).isoformat()
    with tempfile.TemporaryDirectory(prefix="poop-detector-backup-") as folder:
        sources = _sources(root, Path(folder), include_clips)
        files = {name: _sha256(path) for name, path in sources.items()}
        with closing(sqlite3.connect(sources[EVENTS_DB])) as db:
            version = schema_version(db)
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "created_at": created,
            "schema_version": version,
            "include_clips": include_clips,
            "files": files,
        }
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1))
            for name, path in sources.items():
                archive.write(path, name, compress_type=_compression(name))
    return BackupSummary(files=len(files), clips=sum(n.startswith("clips/") for n in files))


def _safe_name(name: object) -> bool:
    if not isinstance(name, str) or name.startswith("/") or "\\" in name:
        return False
    if any(part in ("", ".", "..") for part in name.split("/")):
        return False
    return ALLOWED.fullmatch(name) is not None


def _manifest(archive: zipfile.ZipFile) -> Manifest:
    try:
        data = json.loads(archive.read(MANIFEST))
    except (KeyError, ValueError):
        raise InvalidInputError("バックアップの目録（manifest.json）を読めません") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise InvalidInputError("poop-detector のバックアップではありません")
    if data.get("format_version") != FORMAT_VERSION:
        raise InvalidInputError("この版では扱えない形式のバックアップです")
    version = data.get("schema_version")
    files = data.get("files")
    if not isinstance(version, int) or not isinstance(files, dict):
        raise InvalidInputError("バックアップの目録が不正です")
    if version > LATEST_VERSION:
        raise InvalidInputError(
            "バックアップがこの版より新しい poop-detector で作られています。"
            "poop-detector を更新してください"
        )
    if not all(_safe_name(name) for name in files) or EVENTS_DB not in files:
        raise InvalidInputError("バックアップに復元できない名前のファイルがあります")
    if not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in files.values()
    ):
        raise InvalidInputError("バックアップの目録が不正です")
    return Manifest(
        created_at=str(data.get("created_at", "")),
        schema_version=version,
        include_clips=bool(data.get("include_clips")),
        files=dict(files),
    )


def inspect_backup(path: Path) -> Manifest:
    """バックアップの形式と目録を確かめる（中身のハッシュは復元のときに確かめる）。"""
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = _manifest(archive)
            names = archive.namelist()
    except zipfile.BadZipFile:
        raise InvalidInputError("バックアップの ZIP ファイルを読めません") from None
    if len(names) != len(set(names)) or set(names) != {MANIFEST, *manifest.files}:
        raise InvalidInputError("バックアップの中身が目録と一致しません")
    return manifest


@contextmanager
def _stopped(root: Path) -> Iterator[None]:
    """監視・動画判定・行動マップが同じデータで動いていないことを、ロックを取って確かめる。"""
    with ExitStack() as stack:
        if root.is_dir():
            for name in LOCKS:
                lock = stack.enter_context((root / name).open("a"))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise AlreadyRunningError(
                        "監視などの処理が同じデータで動いています。止めてから復元してください"
                    ) from None
        yield


def _extract(archive: zipfile.ZipFile, manifest: Manifest, staging: Path) -> None:
    for name, expected in manifest.files.items():
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with archive.open(name) as source, target.open("wb") as output:
            while block := source.read(CHUNK):
                digest.update(block)
                output.write(block)
        if digest.hexdigest() != expected:
            raise InvalidInputError(f"バックアップの {name} が壊れているか、書き換えられています")


def restore_backup(archive_path: Path, root: Path, *, now: datetime | None = None) -> Path | None:
    """バックアップを root に戻し、元のデータを移した場所を返す（元のデータがなければ None）。"""
    manifest = inspect_backup(archive_path)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = root.parent / f".{root.name}.restoring-{stamp}"
    preserved = root.parent / f"{root.name}.before-restore-{stamp}"
    if staging.exists() or preserved.exists():
        raise InvalidInputError(
            "同じ時刻の復元の作業場所があります。少し待ってからやり直してください"
        )
    with _stopped(root):
        staging.mkdir()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                _extract(archive, manifest, staging)
            # 古い版のバックアップなら、ここでいまのスキーマへ移す。
            events_database(staging)
            for name in CARRIED_OVER:
                if (root / name).is_dir():
                    shutil.copytree(root / name, staging / name, symlinks=True)
        except BaseException:
            # 作業場所はこの関数が作ったもので、元のデータは含まない。
            shutil.rmtree(staging)
            raise
        if not root.exists():
            staging.rename(root)
            return None
        root.rename(preserved)
        staging.rename(root)
    return preserved
