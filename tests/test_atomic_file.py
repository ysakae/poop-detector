"""共有ファイルの置換と、失敗時の後始末を検証する。"""

from pathlib import Path

import pytest


def test_replace_publishes_complete_contents_and_keeps_requested_mode(tmp_path):
    from poop_detector.atomic_file import write_bytes

    path = tmp_path / "設定"
    path.write_bytes(b"old")
    path.chmod(0o640)
    write_bytes(path, b"new", preserve_mode=True)
    assert path.read_bytes() == b"new"
    assert path.stat().st_mode & 0o777 == 0o640


def test_failed_replace_keeps_previous_contents(tmp_path, monkeypatch):
    from poop_detector.atomic_file import write_bytes

    path = tmp_path / "frame.jpg"
    path.write_bytes(b"previous")

    def fail_replace(source, target):
        assert source.read_bytes() == b"next"
        assert target == path
        raise OSError("置換失敗")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="置換失敗"):
        write_bytes(path, b"next")
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]
