"""監視プロセスとローカルGUIで最新フレームを1枚だけ共有する。"""

import logging
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import cv2

logger = logging.getLogger(__name__)
FRAME_INTERVAL = 0.25
FRAME_MAX_AGE = 3
REQUEST_MAX_AGE = 5


class LivePreview:
    def __init__(self, root):
        self.root = Path(root)
        self.frame_path = self.root / ".live-preview.jpg"
        self.request_path = self.root / ".live-preview-request"
        self.last_attempt = float("-inf")

    def publish(self, frame, now=None):
        now = time.monotonic() if now is None else now
        if frame is None or now - self.last_attempt < FRAME_INTERVAL:
            return
        self.last_attempt = now
        try:
            if time.time() - self.request_path.stat().st_mtime > REQUEST_MAX_AGE:
                return
            height, width = frame.shape[:2]
            if max(width, height) > 640:
                scale = 640 / max(width, height)
                size = (max(1, round(width * scale)), max(1, round(height * scale)))
                frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
            ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ok:
                return
            with tempfile.NamedTemporaryFile(
                dir=self.root, prefix=".live-preview-", delete=False
            ) as output:
                temporary = Path(output.name)
                try:
                    output.write(jpeg.tobytes())
                    output.flush()
                    temporary.replace(self.frame_path)
                finally:
                    temporary.unlink(missing_ok=True)
        except FileNotFoundError:
            return
        except (OSError, cv2.error):
            logger.warning("ライブ映像のプレビューを更新できません")
            self.last_attempt = now + REQUEST_MAX_AGE

    def read(self):
        try:
            self.request_path.touch(mode=0o600)
            with self.frame_path.open("rb") as frame:
                stamp = os.fstat(frame.fileno()).st_mtime
                if time.time() - stamp <= FRAME_MAX_AGE:
                    return frame.read(), datetime.fromtimestamp(stamp, UTC).isoformat()
        except OSError:
            pass
        return None
