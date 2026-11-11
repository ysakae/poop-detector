"""犬のトイレの変化を検知して通知するローカル監視ツール。"""

import os

# FFmpeg の診断に RTSP の認証情報が含まれることを防ぐため、OpenCV を読み込む前に抑制する。
# パッケージのどのモジュールを最初に読み込んでも効くよう、ここで設定する。
os.environ["OPENCV_LOG_LEVEL"] = "SILENT"
os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "-8"

from .platform_support import ensure_supported

# 非対応の OS では、fcntl などの読み込みエラーより先に理由を伝える。
ensure_supported()
