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
from typing import TYPE_CHECKING, Any, Protocol

from .codex_cli import JUDGE_TIMEOUT, LOOKUP_TIMEOUT, CodexCliJudge
from .judge_failures import JudgeCallError, mentions_rate_limit, rate_limited
from .token_usage import TokenUsage, parse_claude

if TYPE_CHECKING:
    from .review import VideoJudge

RESPONSE_LIMIT = 1024 * 1024
# 失敗の種類を決めるために読む、CLI の標準エラーの末尾の大きさ。中身は残さない。
STDERR_TAIL = 16 * 1024
STDERR_DRAIN_SECONDS = 2
# ACP の停止の理由（stopReason）のうち、記録の詳細に入れてよいもの。
STOP_REASONS = ("max_tokens", "max_turn_requests", "refusal", "cancelled")
CHECK_TIMEOUT = 15
CLAUDE_LOGIN = "claude auth login で Claude Code CLI にログインしてください"
AGY_LOGIN = "agy acp --login で Antigravity CLI にログインしてください"


__all__ = ["JUDGE_TIMEOUT", "LOOKUP_TIMEOUT"]


class WebLookup(Protocol):
    """ウェブ検索だけを許して調べさせられる判定器。"""

    name: str

    def lookup(self, folder: Path, prompt: str, schema: dict[str, Any]) -> object: ...


def supports_lookup(judge: object) -> bool:
    """判定器がウェブ検索の口（lookup）を持つか。"""
    return callable(getattr(judge, "lookup", None))


def _cli_environment() -> dict[str, str]:
    # CLI のログイン情報は各 CLI が管理する。監視設定や API キーは渡さない。
    # USER・LOGNAME は、macOS のキーチェーンにあるログイン情報を CLI が読むのに要る。
    keys = (
        "HOME",
        "USER",
        "LOGNAME",
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
        raise JudgeCallError(
            f"{name} をインストールし、{command} を PATH に追加してください", "launch"
        )
    return str(Path(executable).resolve())


def _encoded_image(folder: Path, image: str) -> str:
    return base64.b64encode((folder / image).read_bytes()).decode("ascii")


def _result(output: object, schema: dict[str, Any], name: str) -> object:
    if not isinstance(output, dict) or output.keys() != set(schema["required"]):
        raise JudgeCallError(f"{name} から有効な判定を取得できません", "invalid_output")
    return output


def _rpc_failure(name: str, message: dict[str, Any]) -> JudgeCallError:
    """ACP の要求が成果を返さなかったときの失敗。エラーの番号と停止の理由だけを詳細に残す。"""
    text = f"{name} から有効な判定を取得できません"
    error = message.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        detail = f"code:{code}" if isinstance(code, int) and not isinstance(code, bool) else None
        kind = "rate_limited" if mentions_rate_limit(error) else "rejected"
        return JudgeCallError(text, kind, detail)
    reason = message.get("result", {}).get("stopReason")
    return JudgeCallError(text, "rejected", f"stop:{reason}" if reason in STOP_REASONS else None)


async def _collected(task: "asyncio.Task[bytes]") -> bytes:
    """読み終えた標準エラー。CLI の子プロセスが開いたままでも、少し待って諦める。"""
    try:
        return await asyncio.wait_for(task, STDERR_DRAIN_SECONDS)
    except TimeoutError:
        return b""


async def _tail(stream: asyncio.StreamReader | None) -> bytes:
    """標準エラーを読み切り、末尾だけを返す（読まないとパイプが詰まって CLI が止まる）。"""
    kept = b""
    while stream is not None and (chunk := await stream.read(4096)):
        kept = (kept + chunk)[-STDERR_TAIL:]
    return kept


class ClaudeCodeJudge:
    """Claude Code CLI に画像を添付し、構造化された判定を受け取る。"""

    name = "Claude Code CLI"

    def __init__(self, model: str, effort: str = "") -> None:
        self.model = model
        # 推論の深さ（low など）。空なら CLI の既定。
        self.effort = effort
        # 直近の呼び出しで使ったトークン数。CLI が出さなければ None。
        self.last_usage: TokenUsage | None = None

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
        return self._invoke(folder, prompt, images, schema, tools="", timeout=JUDGE_TIMEOUT)

    def lookup(self, folder: Path, prompt: str, schema: dict[str, Any]) -> object:
        """ウェブ検索と取得のツールだけを許して調べさせる（画像なし）。"""
        return self._invoke(
            folder, prompt, (), schema, tools="WebSearch,WebFetch", timeout=LOOKUP_TIMEOUT
        )

    def _invoke(
        self,
        folder: Path,
        prompt: str,
        images: Sequence[str],
        schema: dict[str, Any],
        *,
        tools: str,
        timeout: int,
    ) -> object:
        executable = _executable("claude", self.name)
        self.last_usage = None
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
                    tools,
                    *(["--allowedTools", tools] if tools else []),
                    "--permission-mode",
                    "dontAsk",
                    "--no-session-persistence",
                    "--json-schema",
                    json.dumps(schema),
                    *([f"--model={self.model}"] if self.model else []),
                    *(["--effort", self.effort] if self.effort else []),
                ],
                input=(json.dumps(message) + "\n").encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=folder,
                env=_cli_environment(),
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise JudgeCallError(f"{self.name} の判定がタイムアウトしました", "timeout") from None
        except OSError:
            raise JudgeCallError(f"{self.name} を起動できません", "launch") from None
        invalid = f"{self.name} から有効な判定を取得できません"
        if process.returncode:
            raise JudgeCallError(invalid, "exit", f"exit:{int(process.returncode)}")
        try:
            if len(process.stdout) > RESPONSE_LIMIT:
                raise ValueError
            response = json.loads(process.stdout.rstrip().rsplit(b"\n", 1)[-1])
            self.last_usage = parse_claude(response.get("usage"))
            failed = (
                response["type"] != "result"
                or response["subtype"] != "success"
                or response.get("is_error")
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise JudgeCallError(invalid, "invalid_output") from None
        if failed:
            # 利用枠の超過は、結果の本文（result）の言い回しでしか分からない。
            raise rate_limited(JudgeCallError(invalid, "rejected"), response.get("result"))
        try:
            return _result(response["structured_output"], schema, self.name)
        except KeyError:
            raise JudgeCallError(invalid, "invalid_output") from None


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
                raise JudgeCallError("Antigravity CLI の応答形式が不正です", "connection")
            if message.get("method") == "session/update":
                self._update(message["params"]["update"])
            elif "method" in message and "id" in message:
                # クライアント側でファイル操作、端末の起動、権限の承認を行わない。
                raise JudgeCallError(
                    "Antigravity CLI が判定中にツールの使用を要求しました", "tool_request"
                )
            elif message.get("id") == self.request_id:
                return message
        raise JudgeCallError("Antigravity CLI から応答がありません", "connection", "eof")

    def _update(self, update: dict[str, Any]) -> None:
        event = update["sessionUpdate"]
        if event in ("tool_call", "tool_call_update"):
            raise JudgeCallError(
                "Antigravity CLI が判定中にツールの使用を要求しました", "tool_request"
            )
        if event == "agent_message_chunk" and update["content"]["type"] == "text":
            text = update["content"]["text"]
            self.answer_size += len(text)
            if self.answer_size > RESPONSE_LIMIT:
                raise JudgeCallError("Antigravity CLI の判定結果が長すぎます", "invalid_output")
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
        answer, stderr = self._run(content, JUDGE_TIMEOUT)
        text = answer.strip()
        if text.startswith("```json\n") and text.endswith("\n```"):
            text = text.removeprefix("```json\n").removesuffix("\n```")
        invalid = JudgeCallError(f"{self.name} から有効な判定を取得できません", "invalid_output")
        try:
            output = json.loads(text)
        except ValueError:
            # 利用枠の超過は、判定の代わりに本文で知らせることがある。
            raise rate_limited(invalid, text, stderr) from None
        return _result(output, schema, self.name)

    def _run(self, content: list[dict[str, str]], timeout: int) -> tuple[str, str]:
        """判定の本文と、標準エラーの末尾（失敗の種類を見分けるためだけに使う）を返す。"""
        executable = _executable("agy", self.name)
        try:
            return asyncio.run(self._exchange(executable, content, timeout))
        except (OSError, KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError):
            raise JudgeCallError(f"{self.name} と通信できません", "connection") from None

    async def _exchange(
        self, executable: str, content: list[dict[str, str]], timeout: int
    ) -> tuple[str, str]:
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
                stderr=asyncio.subprocess.PIPE,
                limit=RESPONSE_LIMIT,
            )
            diagnostics = asyncio.create_task(_tail(process.stderr))
            failure: JudgeCallError | None = None
            try:
                if process.stdin is None or process.stdout is None:
                    raise OSError
                session = _AntigravitySession(process.stdin, process.stdout)
                async with asyncio.timeout(timeout):
                    answer = await self._judge_in_session(session, cwd, content)
            except TimeoutError:
                failure = JudgeCallError(f"{self.name} の応答がタイムアウトしました", "timeout")
            except JudgeCallError as error:
                failure = error
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                await process.wait()
                stderr = (await _collected(diagnostics)).decode("utf-8", "replace")
            if failure is None:
                return answer, stderr
            # 標準エラーの言い回しで、利用枠の超過や混雑による失敗を見分ける。
            raise rate_limited(failure, stderr)

    async def _judge_in_session(
        self, session: _AntigravitySession, cwd: str, content: list[dict[str, str]]
    ) -> str:
        initialized = await session.request("initialize", protocolVersion=1, clientCapabilities={})
        capabilities = initialized.get("result", {}).get("agentCapabilities", {})
        if initialized.get("result", {}).get("protocolVersion") != 1 or not capabilities.get(
            "promptCapabilities", {}
        ).get("image"):
            raise JudgeCallError(
                "画像入力に対応する Antigravity CLI に更新してください", "unsupported"
            )
        opened = await session.request("session/new", cwd=cwd, mcpServers=[])
        if opened.get("error", {}).get("code") == -32000:
            raise JudgeCallError(AGY_LOGIN, "login")
        if "result" not in opened:
            raise JudgeCallError("Antigravity CLI のセッションを開始できません", "rejected")
        session_id = opened["result"]["sessionId"]
        mode = await session.request("session/set_mode", sessionId=session_id, modeId="plan")
        if "result" not in mode:
            raise JudgeCallError("Antigravity CLI をプランモードに切り替えられません", "rejected")
        if self.model:
            selected = await session.request(
                "session/set_model", sessionId=session_id, modelId=self.model
            )
            if "result" not in selected:
                raise JudgeCallError(
                    "Antigravity CLI のモデルを選べません。agy models を確認してください",
                    "rejected",
                )
        if content:
            result = await session.request("session/prompt", sessionId=session_id, prompt=content)
            if result.get("result", {}).get("stopReason") != "end_turn":
                raise _rpc_failure(self.name, result)
        await session.request("session/close", sessionId=session_id)
        return "".join(session.answer)


# 設定の review.provider の値と、画面に出す判定器の名前。
PROVIDER_NAMES = {
    "codex": CodexCliJudge.name,
    "claude": ClaudeCodeJudge.name,
    "gemini": AntigravityJudge.name,
}


# 判定器に指定できる推論の深さ。空は CLI の既定。
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


def build_judge(provider: str, model: str = "", effort: str = "") -> "VideoJudge":
    """判定器を作る。effort は推論の深さで、空なら CLI の既定。"""
    if effort and effort not in EFFORTS:
        raise ValueError(f"推論の深さには {'、'.join(EFFORTS)} のいずれかを指定してください")
    if provider == "codex":
        return CodexCliJudge(model=model, effort=effort)
    if provider == "claude":
        return ClaudeCodeJudge(model, effort)
    if provider == "gemini":
        if effort:
            raise ValueError("Antigravity CLI の推論の深さは、モデル名で選んでください")
        return AntigravityJudge(model)
    raise ValueError("判定器には codex、claude、gemini のいずれかを指定してください")
