"""犬のトイレの変化を検知して通知するローカル監視ツール。"""

from .platform_support import ensure_supported

# 非対応の OS では、fcntl などの読み込みエラーより先に理由を伝える。
ensure_supported()
