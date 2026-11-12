"""選択した CLI に切り出し済みの画像と判定条件を渡す。"""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .codex_cli import CodexCliJudge

if TYPE_CHECKING:
    from .review import VideoJudge

RESPONSE_LIMIT = 1024 * 1024
CHECK_TIMEOUT = 15
JUDGE_TIMEOUT = 120
CLAUDE_LOGIN = "claude auth login で Claude Code CLI にログインしてください"
AGY_LOGIN = "agy acp --login で Antigravity CLI にログインしてください"


def _cli_environment() -> dict[str, str]:
    # CLI のログイン情報は各 CLI が管理する。監視設定や API キーは渡さない。
    keys = (
        "HOME",
        "PATH",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    )
    return {key: os.environ[key] for key in keys if key in os.environ}


def _executable(command: str, name: str) -> str:
    executable = shutil.which(command)
    if executable is None:
        raise ValueError(f"{name} をインストールし、{command} を PATH に追加してください")
    return str(Path(executable).resolve())


def _encoded_image(folder: Path, image: str) -> str:
    return base64.b64encode((folder / image).read_bytes()).decode("ascii")


def _result(output: object, schema: dict[str, Any], name: str) -> object:
    if not isinstance(output, dict) or output.keys() != set(schema["required"]):
        raise ValueError(f"{name} から有効な判定を取得できません")
    return output


class ClaudeCodeJudge:
    """Claude Code CLI に画像を添付し、構造化された判定を受け取る。"""

    name = "Claude Code CLI"

    def __init__(self, model: str) -> None:
        self.model = model

    def check(self) -> None:
        executable = _executable("claude", self.name)
        try:
            # PATH から解決した Claude Code を、固定の引数で実行する。
            result = subprocess.run(  # noqa: S603
                [executable, "auth", "status"],
                env=_cli_environment(),
                cwd=tempfile.gettempdir(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=CHECK_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Claude Code CLI のログイン状態を確認できません") from None
        if result.returncode:
            raise ValueError(CLAUDE_LOGIN)

    def judge(
        self, folder: Path, prompt: str, images: Sequence[str], schema: dict[str, Any]
    ) -> object:
        executable = _executable("claude", self.name)
        message = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    *(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": _encoded_image(folder, image),
                            },
                        }
                        for image in images
                    ),
                    {"type": "text", "text": prompt},
                ],
            },
        }
        try:
            # シェルを介さず、画像を標準入力で渡し、内蔵ツールとカスタマイズを無効にする。
            process = subprocess.run(  # noqa: S603
                [
                    executable,
                    "--print",
                    "--input-format",
                    "stream-json",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--safe-mode",
                    "--tools",
                    "",
                    "--permission-mode",
                    "dontAsk",
                    "--no-session-persistence",
                    "--json-schema",
                    json.dumps(schema),
                    *([f"--model={self.model}"] if self.model else []),
                ],
                input=(json.dumps(message) + "\n").encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=folder,
                env=_cli_environment(),
                check=False,
                timeout=JUDGE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise ValueError(f"{self.name} の判定がタイムアウトしました") from None
        except OSError:
            raise ValueError(f"{self.name} を起動できません") from None
        try:
            if process.returncode or len(process.stdout) > RESPONSE_LIMIT:
                raise ValueError
            response = json.loads(process.stdout.rstrip().rsplit(b"\n", 1)[-1])
            if (
                response["type"] != "result"
                or response["subtype"] != "success"
                or response.get("is_error")
            ):
                raise ValueError
            return _result(response["structured_output"], schema, self.name)
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{self.name} から有効な判定を取得できません") from None


class _AntigravitySession:
    """Antigravity ACP の標準入出力で、1回の判定に必要な要求だけを扱う。"""

    def __init__(self, writer: asyncio.StreamWriter, reader: asyncio.StreamReader) -> None:
        self.writer, self.reader = writer, reader
        self.request_id = 0
        self.answer: list[str] = []
        self.answer_size = 0

    async def request(self, method: str, **params: Any) -> dict[str, Any]:
        self.request_id += 1
        request = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        self.writer.write((json.dumps(request) + "\n").encode())
        await self.writer.drain()
        while line := await self.reader.readline():
            message: object = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Antigravity CLI の応答形式が不正です")
            if message.get("method") == "session/update":
                self._update(message["params"]["update"])
            elif "method" in message and "id" in message:
                # クライアント側でファイル操作、端末の起動、権限の承認を行わない。
                raise ValueError("Antigravity CLI が判定中にツールの使用を要求しました")
            elif message.get("id") == self.request_id:
                return message
        raise ValueError("Antigravity CLI から応答がありません")

    def _update(self, update: dict[str, Any]) -> None:
        event = update["sessionUpdate"]
        if event in ("tool_call", "tool_call_update"):
            raise ValueError("Antigravity CLI が判定中にツールの使用を要求しました")
        if event == "agent_message_chunk" and update["content"]["type"] == "text":
            text = update["content"]["text"]
            self.answer_size += len(text)
            if self.answer_size > RESPONSE_LIMIT:
                raise ValueError("Antigravity CLI の判定結果が長すぎます")
            self.answer.append(text)


class AntigravityJudge:
    """Antigravity CLI の ACP に画像を添付し、判定の JSON を受け取る。"""

    name = "Antigravity CLI"

    def __init__(self, model: str) -> None:
        self.model = model

    def check(self) -> None:
        self._run([], CHECK_TIMEOUT)

    def judge(
        self, folder: Path, prompt: str, images: Sequence[str], schema: dict[str, Any]
    ) -> object:
        content = [
            {
                "type": "text",
                "text": prompt
                + "\n次の JSON Schema に従い、JSON オブジェクトだけを返してください。\n"
                + json.dumps(schema, ensure_ascii=False),
            },
            *(
                {"type": "image", "mimeType": "image/jpeg", "data": _encoded_image(folder, image)}
                for image in images
            ),
        ]
        text = self._run(content, JUDGE_TIMEOUT).strip()
        if text.startswith("```json\n") and text.endswith("\n```"):
            text = text.removeprefix("```json\n").removesuffix("\n```")
        try:
            return _result(json.loads(text), schema, self.name)
        except ValueError:
            raise ValueError(f"{self.name} から有効な判定を取得できません") from None

    def _run(self, content: list[dict[str, str]], timeout: int) -> str:
        executable = _executable("agy", self.name)
        try:
            return asyncio.run(self._exchange(executable, content, timeout))
        except TimeoutError:
            raise ValueError(f"{self.name} の応答がタイムアウトしました") from None
        except (OSError, KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError):
            raise ValueError(f"{self.name} と通信できません") from None

    async def _exchange(self, executable: str, content: list[dict[str, str]], timeout: int) -> str:
        # 空の作業ディレクトリを使い、判定対象は画像ブロックとしてのみ渡す。
        with tempfile.TemporaryDirectory(prefix="poop-agy-") as folder:
            cwd = str(Path(folder).resolve())
            process = await asyncio.create_subprocess_exec(
                executable,
                "acp",
                "--sandbox",
                "--include-default-customizations=false",
                cwd=cwd,
                env=_cli_environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=RESPONSE_LIMIT,
            )
            try:
                if process.stdin is None or process.stdout is None:
                    raise OSError
                session = _AntigravitySession(process.stdin, process.stdout)
                async with asyncio.timeout(timeout):
                    return await self._judge_in_session(session, cwd, content)
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                await process.wait()

    async def _judge_in_session(
        self, session: _AntigravitySession, cwd: str, content: list[dict[str, str]]
    ) -> str:
        initialized = await session.request("initialize", protocolVersion=1, clientCapabilities={})
        capabilities = initialized.get("result", {}).get("agentCapabilities", {})
        if initialized.get("result", {}).get("protocolVersion") != 1 or not capabilities.get(
            "promptCapabilities", {}
        ).get("image"):
            raise ValueError("画像入力に対応する Antigravity CLI に更新してください")
        opened = await session.request("session/new", cwd=cwd, mcpServers=[])
        if opened.get("error", {}).get("code") == -32000:
            raise ValueError(AGY_LOGIN)
        if "result" not in opened:
            raise ValueError("Antigravity CLI のセッションを開始できません")
        session_id = opened["result"]["sessionId"]
        mode = await session.request("session/set_mode", sessionId=session_id, modeId="plan")
        if "result" not in mode:
            raise ValueError("Antigravity CLI をプランモードに切り替えられません")
        if self.model:
            selected = await session.request(
                "session/set_model", sessionId=session_id, modelId=self.model
            )
            if "result" not in selected:
                raise ValueError(
                    "Antigravity CLI のモデルを選べません。agy models を確認してください"
                )
        if content:
            result = await session.request("session/prompt", sessionId=session_id, prompt=content)
            if result.get("result", {}).get("stopReason") != "end_turn":
                raise ValueError("Antigravity CLI から有効な判定を取得できません")
        await session.request("session/close", sessionId=session_id)
        return "".join(session.answer)


# 設定の review.provider の値と、画面に出す判定器の名前。
PROVIDER_NAMES = {
    "codex": CodexCliJudge.name,
    "claude": ClaudeCodeJudge.name,
    "gemini": AntigravityJudge.name,
}


def build_judge(provider: str, model: str = "") -> "VideoJudge":
    if provider == "codex":
        return CodexCliJudge(model=model)
    if provider == "claude":
        return ClaudeCodeJudge(model)
    if provider == "gemini":
        return AntigravityJudge(model)
    raise ValueError("判定器には codex、claude、gemini のいずれかを指定してください")
