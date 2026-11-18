"""ほかのプロセスが読むファイルを、同じディレクトリ内で一括して置き換える。"""

import shutil
from contextlib import ExitStack
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_bytes(path: Path, data: bytes, *, preserve_mode: bool = False) -> None:
    """書き終えて閉じたファイルを公開する。保存先のディレクトリは呼び出し側で用意する。"""
    with ExitStack() as cleanup:
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            cleanup.callback(temporary.unlink, missing_ok=True)
            stream.write(data)
        if preserve_mode and path.exists():
            shutil.copymode(path, temporary)
        temporary.replace(path)
