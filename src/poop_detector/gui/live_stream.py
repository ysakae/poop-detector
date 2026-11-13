"""監視の最新JPEGを、認証済みブラウザーへ1つの接続で配信する。"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from ..capture.live_preview import FRAME_INTERVAL, FRAME_MAX_AGE

if TYPE_CHECKING:
    from .server import GuiApp

SESSION_CHECK_INTERVAL = 3.0
REQUEST_INTERVAL = 1.0


def session_active(gui: "GuiApp", session_id: str) -> bool:
    session = gui.sessions.lookup(session_id)
    user = gui.accounts.users.get(session.user_id) if session and session.user_id else None
    return user is not None and user.active


async def live_frames(gui: "GuiApp", session_id: str) -> AsyncIterator[bytes]:
    preview = gui.preview
    last_stamp = None
    last_frame = time.monotonic()
    next_request = last_frame + REQUEST_INTERVAL
    next_check = last_frame + SESSION_CHECK_INTERVAL
    while (now := time.monotonic()) - last_frame < FRAME_MAX_AGE:
        if now >= next_check:
            if not await run_in_threadpool(session_active, gui, session_id):
                break
            next_check = now + SESSION_CHECK_INTERVAL
        if now >= next_request:
            await run_in_threadpool(preview.request_frame)
            next_request = now + REQUEST_INTERVAL
        frame = await run_in_threadpool(preview.latest, last_stamp)
        if frame is not None:
            jpeg, last_stamp = frame
            last_frame = now
            header = f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n"
            yield header.encode("ascii") + jpeg + b"\r\n"
        # 遅い接続でも古いフレームは溜めず、送信後にその時点の最新画像を読む。
        await asyncio.sleep(FRAME_INTERVAL / 2)
    yield b"--frame--\r\n"


async def live_stream(request: Request) -> Response:
    session_id = getattr(request.state, "session_id", None)
    if not session_id or request.headers.get("Sec-Fetch-Site") == "cross-site":
        return Response("アクセスできません", status_code=403)
    if request.method != "GET":
        return Response(status_code=405, headers={"Allow": "GET"})
    gui: GuiApp = request.app.state.gui
    await run_in_threadpool(gui.preview.request_frame)
    return StreamingResponse(
        live_frames(gui, session_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"X-Accel-Buffering": "no"},
    )
