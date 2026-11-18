"""Mac の Finder から手動記録を開く。"""

import subprocess
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from poop_detector.errors import UserError
from poop_detector.gui.server import GuiServer, make_server

# CLI で起動する GUI と同じ Mac で併用できるようにする。
MAC_PORT = 18765
MAC_COOKIE_NAME = "poop_detector_mac_session"


@contextmanager
def open_record_book(server: GuiServer) -> Iterator[None]:
    """最初の管理者の登録リンクは、アプリを起動した人のブラウザーにだけ渡す。"""
    base = f"http://localhost:{server.server_port}/"
    accounts = server.app.accounts
    code = None
    if not accounts.users.list():
        code = accounts.enrollments.issue("admin", name="飼い主", created_by="mac-app")
    try:
        if not webbrowser.open(f"{base}enroll?code={code}" if code else base):
            raise UserError("ブラウザーを開けませんでした。既定のブラウザーを確認してください。")
        yield
    finally:
        if code:
            accounts.enrollments.revoke(code)


def run(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (
        make_server(root, MAC_PORT, manual_only=True, cookie_name=MAC_COOKIE_NAME) as server,
        open_record_book(server),
    ):
        server.serve_forever()


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
