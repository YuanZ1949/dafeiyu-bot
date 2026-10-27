from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..memory.store import Store
from ..models import ChatType
from .client import BrainError, BrainReply, parse_brain_reply

# 仓库根；发布包里 opencode.exe 放在 vendor/opencode/ 下随包分发。
INSTALL_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class _RunResult:
    returncode: int
    stdout: str
    stderr: str
    session_id: str
    text: str
    finish_reason: str
    error: str


def resolve_opencode_bin(configured: str = "") -> str:
    """找到真正的 opencode.exe。

    Windows 下 npm 会生成 opencode.cmd / opencode.ps1，直接传中文参数容易
    被 shim 弄坏；这里优先返回 exe 本体。没装 Node/npm 的用户用随包下载的
    ``vendor/opencode/opencode.exe``。
    """
    value = configured.strip()
    if value:
        path = Path(value).expanduser()
        if path.is_file():
            return str(path)
        found = shutil.which(value)
        if found:
            return found
        raise BrainError(f"opencode.bin 不存在：{value}")

    candidates: list[Path] = [INSTALL_ROOT / "vendor" / "opencode" / "opencode.exe"]
    found = shutil.which("opencode.exe")
    if found:
        candidates.append(Path(found))

    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            Path(appdata)
            / "npm"
            / "node_modules"
            / "@opencode"
            / "cli"
            / "bin"
            / "opencode.exe"
        )

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm:
        try:
            command = [npm, "root", "-g"]
            if npm.casefold().endswith(".cmd"):
                command = ["cmd.exe", "/d", "/c", npm, "root", "-g"]
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            if lines:
                candidates.append(
                    Path(lines[-1])
                    / "@opencode"
                    / "cli"
                    / "bin"
                    / "opencode.exe"
                )
        except (OSError, subprocess.SubprocessError):
            pass

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    raise BrainError(
        "找不到 opencode.exe；请在 config.yaml 的 opencode.bin 填完整路径"
    )


def _is_stale_session(detail: str) -> bool:
    low = detail.casefold()
    return "session" in low and (
        "not found" in low or "unknown" in low or "404" in low
    )


class OpenCodeBrain:
    """把 OpenCode session 当成聊天模型使用。

    每个 (chat_type, chat_id) 对应一个 OpenCode session。消息通过
    ``opencode run --format json`` 发送，解析事件流里的 text part。
    """

    # CLI 传输同样加载 agent 文件，工具权限由 agent 配置决定。
    supports_tools = True

    def __init__(
        self,
        bin_path: str,
        store: Store,
        agent: str = "deepseek-qq",
        project_dir: str = "",
        timeout: float = 90.0,
        env: Mapping[str, str] | None = None,
        server_url: str = "",
    ) -> None:
        self.bin = bin_path
        self.store = store
        self.agent = agent.strip() or "deepseek-qq"
        self.server_url = server_url.strip()
        self.project_dir = str(Path(project_dir).expanduser().resolve()) if project_dir.strip() else ""
        if self.project_dir and not Path(self.project_dir).is_dir():
            raise BrainError(f"opencode.project_dir 不存在：{self.project_dir}")
        self.timeout = float(timeout)
        self._env = dict(env or {})

    async def aclose(self) -> None:
        return None

    async def forget(self, chat_type: ChatType, chat_id: int) -> None:
        self.store.clear_opencode_session(chat_type, chat_id)

    async def reply(
        self,
        chat_type: ChatType,
        chat_id: int,
        text: str,
    ) -> BrainReply:
        prompt = text.strip()
        if not prompt:
            raise BrainError("用户消息为空")

        session_id = self.store.get_opencode_session(chat_type, chat_id)
        result = await self._run(prompt, session_id)

        if result.returncode != 0:
            detail = result.error or result.stderr.strip() or result.stdout.strip()
            if session_id and _is_stale_session(detail):
                # OpenCode 的 session 被删掉或数据库重置：清掉映射，新开一个。
                self.store.clear_opencode_session(chat_type, chat_id)
                session_id = None
                result = await self._run(prompt, None)

            if result.returncode != 0:
                detail = result.error or result.stderr.strip() or result.stdout.strip()
                raise BrainError(f"OpenCode 调用失败：{detail[:400]}")

        if result.session_id:
            self.store.set_opencode_session(chat_type, chat_id, result.session_id)

        if not result.text:
            raise BrainError("OpenCode 没有返回文本")

        return parse_brain_reply(
            result.text,
            prompt_tokens=0,
            completion_tokens=0,
            finish_reason=result.finish_reason,
        )

    def _build_command(self, text: str, session_id: str | None) -> list[str]:
        command = [self.bin, "run"]
        if self.server_url:
            command += ["--server", self.server_url]
        command += [
            "--agent",
            self.agent,
            "--format",
            "json",
        ]
        if session_id:
            command += ["--session", session_id]
        command.append(text)
        return command

    async def _run(self, text: str, session_id: str | None) -> _RunResult:
        command = self._build_command(text, session_id)
        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        env.update(self._env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.project_dir or None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise BrainError(f"无法启动 OpenCode：{exc.__class__.__name__}") from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
            raise BrainError(
                f"OpenCode 超时（{self.timeout:.0f} 秒），本条放弃"
            ) from exc

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        parsed = self._parse_events(stdout)
        return _RunResult(
            returncode=proc.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            session_id=parsed["session_id"],
            text=parsed["text"],
            finish_reason=parsed["finish_reason"],
            error=parsed["error"],
        )

    @staticmethod
    def _parse_events(stdout: str) -> dict[str, str]:
        session_id = ""
        texts: list[str] = []
        finish_reason = ""
        errors: list[str] = []

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue

            sid = event.get("sessionID") or event.get("sessionId")
            if isinstance(sid, str) and sid:
                session_id = sid

            event_type = event.get("type")
            if event_type == "text":
                part = event.get("part")
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        texts.append(text)
            elif event_type == "step_finish":
                part = event.get("part")
                if isinstance(part, dict):
                    reason = part.get("reason")
                    if isinstance(reason, str):
                        finish_reason = reason
            elif event_type == "error":
                error = event.get("error")
                if isinstance(error, dict):
                    message = error.get("message")
                    if isinstance(message, str) and message:
                        errors.append(message)

        return {
            "session_id": session_id,
            "text": texts[-1] if texts else "",
            "finish_reason": finish_reason,
            "error": "；".join(errors),
        }
