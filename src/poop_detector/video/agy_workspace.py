"""Antigravity CLI の一時領域を分離し、強制終了で残った領域を回収する。"""

import fcntl
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

RUN_PREFIX = "poop-agy-"
STALE_SECONDS = 24 * 60 * 60


@contextmanager
def _locked_directory(path: Path) -> Iterator[int]:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


def _reclaim(pool: Path) -> None:
    cutoff = time.time() - STALE_SECONDS
    for path in pool.glob(f"{RUN_PREFIX}*"):
        # 他の実行による同時回収、使用中のロック、シンボリックリンクなどは飛ばす。
        with suppress(OSError):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mtime >= cutoff:
                continue
            with _locked_directory(path):
                shutil.rmtree(path)


@contextmanager
def antigravity_workspace() -> Iterator[tuple[Path, int]]:
    """専用の一時領域と、CLI にも引き継ぐ使用中のロックを返す。"""
    uid = os.getuid()
    pool = Path(tempfile.gettempdir()).resolve() / f"poop-detector-agy-{uid}"
    pool.mkdir(mode=0o700, exist_ok=True)
    info = pool.lstat()
    if info.st_uid != uid or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise PermissionError("Antigravity CLI の一時領域を保護できません")
    _reclaim(pool)
    with tempfile.TemporaryDirectory(prefix=RUN_PREFIX, dir=pool) as folder:
        root = Path(folder)
        with _locked_directory(root) as lease:
            yield root, lease
