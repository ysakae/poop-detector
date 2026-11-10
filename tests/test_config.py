"""設定ファイルの検証を確認する。"""

import pytest

from poop_detector.config import Config


def test_config_rejects_invalid_values(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("fps: 0\n")
    with pytest.raises(ValueError):
        Config.load(path)
