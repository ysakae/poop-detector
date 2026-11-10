"""監視用の環境変数をカレントディレクトリの.envに保存する。"""

import argparse
import os
import sys
from codecs import BOM_UTF8
from pathlib import Path
from tempfile import TemporaryDirectory

from dotenv import dotenv_values, set_key

from poop_detector.config import CONNECTION_KEYS


def snapshot(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(".envは通常のファイルにしてください")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def save_environment(path, values, *, replace):
    original = snapshot(path)
    encoding = "utf-8-sig" if original and original.startswith(BOM_UTF8) else "utf-8"
    with TemporaryDirectory(prefix=".env.export-", dir=path.parent) as folder:
        staged = Path(folder) / ".env"
        staged.write_bytes(original or b"")
        existing = dotenv_values(staged, interpolate=False, encoding=encoding)
        written, kept = [], []
        for key, value in values.items():
            if existing.get(key) == value:
                continue
            if existing.get(key) and not replace:
                kept.append(key)
                continue
            set_key(staged, key, value, encoding=encoding)
            written.append(key)
        if written:
            staged.chmod(0o600)
            # 作業中に別の操作で変更されたファイルを上書きしない。
            if snapshot(path) != original:
                raise ValueError("作業中に.envが変更されました")
            staged.replace(path)
    return written, kept


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replace", action="store_true", help="既存の対象項目を上書きする")
    args = parser.parse_args()
    values = {key: os.environ[key] for key in CONNECTION_KEYS if os.environ.get(key)}
    if not values:
        print("保存できる空でない環境変数がありません。")
        return 0
    try:
        written, kept = save_environment(Path(".env"), values, replace=args.replace)
    except (OSError, ValueError):
        print(
            ".envを更新できませんでした。ファイルの種類と書き込み権限を確認してください。",
            file=sys.stderr,
        )
        return 1
    if written:
        print(".envに保存しました。" + "、".join(written))
    if kept:
        print(
            "既存値を保持しました。上書きする場合は --replace を指定してください。"
            + "、".join(kept)
        )
    if not written and not kept:
        print(".envには同じ値が保存されています。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
