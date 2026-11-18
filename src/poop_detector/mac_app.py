"""Mac の Finder から手動記録を開く。"""

import subprocess
import threading
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from poop_detector.errors import UserError
from poop_detector.gui.server import GuiServer, make_server

# CLI で起動する GUI と同じ Mac で併用できるようにする。
MAC_PORT = 18765
MAC_COOKIE_NAME = "poop_detector_mac_session"


@contextmanager
def open_record_book(server: GuiServer) -> Iterator[Callable[[], None]]:
    """最初の管理者の登録リンクは、アプリを起動した人のブラウザーにだけ渡す。"""
    base = f"http://localhost:{server.server_port}/"
    accounts = server.app.accounts
    code: str | None = None

    def show() -> None:
        nonlocal code
        if accounts.users.list():
            if code:
                accounts.enrollments.revoke(code)
                code = None
        elif code is None or accounts.enrollments.peek(code) is None:
            code = accounts.enrollments.issue("admin", name="飼い主", created_by="mac-app")
        if not webbrowser.open(f"{base}enroll?code={code}" if code else base):
            raise UserError("ブラウザーを開けませんでした。既定のブラウザーを確認してください。")

    try:
        yield show
    finally:
        if code:
            accounts.enrollments.revoke(code)


def _show_window(show: Callable[[], None]) -> None:
    """ブラウザーを閉じた後にも記録を開き直せる、小さなウィンドウを残す。"""
    from tkinter import Tk, messagebox, ttk

    window = Tk()
    window.title("トイレ記録")
    window.resizable(False, False)

    def open_browser() -> None:
        try:
            show()
        except UserError as error:
            messagebox.showerror("記録を開けませんでした", str(error), parent=window)

    frame = ttk.Frame(window, padding=24)
    frame.pack()
    ttk.Button(frame, text="ブラウザーで記録を開く", command=open_browser).pack()
    window.after_idle(open_browser)
    window.mainloop()


def run(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (
        make_server(root, MAC_PORT, manual_only=True, cookie_name=MAC_COOKIE_NAME) as server,
        open_record_book(server) as show,
    ):
        worker = threading.Thread(target=server.serve_forever, name="poop-detector-gui")
        worker.start()
        try:
            _show_window(show)
        finally:
            server.shutdown()
            worker.join()


def main() -> None:
    try:
        run(Path.home() / "Library" / "Application Support" / "Poop Detector")
    except (OSError, UserError):
        # Finder から起動すると標準エラー出力が見えないので、画面に案内を出す。
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                'display alert "記録を開けませんでした" message '
                '"以前に開いた記録アプリを終了し、既定のブラウザーを確認してから開き直してください。"',
            ],
            check=False,
        )


if __name__ == "__main__":
    main()
