from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path

import httpx

from ..netssl import default_ssl_context
from .client import BrainError

logger = logging.getLogger(__name__)


class OpenCodeServer:
    def __init__(
        self,
        bin_path: str,
        project_dir: str,
        db_path: str,
        host: str = "127.0.0.1",
        port: int = 24310,
        startup_timeout: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.bin = bin_path
        raw_dir = project_dir.strip()
        self.project_dir = (
            str(Path(raw_dir).expanduser().resolve()) if raw_dir else ""
        )
        self.db_path = db_path.strip()
        self.host = host.strip() or "127.0.0.1"
        self.port = int(port)
        self.startup_timeout = float(startup_timeout)
        self._env = dict(env or {})
        self._proc: asyncio.subprocess.Process | None = None
        self._client: httpx.AsyncClient | None = None
        self._output_tail: deque[str] = deque(maxlen=50)
        self._drain_tasks: list[asyncio.Task[None]] = []

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def command(self) -> list[str]:
        return [
            self.bin,
            "serve",
            "--hostname",
            self.host,
            "--port",
            str(self.port),
        ]

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def drain_tasks(self) -> list[asyncio.Task[None]]:
        """仍在读子进程输出的后台任务；stop() 之后必须为空。"""
        return list(self._drain_tasks)

    def recent_output(self, limit: int = 50) -> str:
        """最近若干行 serve 输出，用于启动失败时给出可读原因。"""
        if limit <= 0:
            return ""
        return "\n".join(list(self._output_tail)[-limit:])

    def _db_abs(self) -> Path:
        path = Path(self.db_path).expanduser()
        if not path.is_absolute():
            base = Path(self.project_dir) if self.project_dir else Path.cwd()
            path = base / path
        return path.resolve()

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self._env)
        env["OPENCODE_DB"] = str(self._db_abs())
        env.setdefault("NO_COLOR", "1")
        return env

    async def _probe(self) -> bool:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=1.0, trust_env=False, verify=default_ssl_context())
        response = await self._client.get(f"{self.url}/api/info")
        # opencode v2 起 serve 强制 Basic 认证，401 同样说明服务已经就绪。
        return response.status_code < 500

    async def _drain(self, stream: asyncio.StreamReader) -> None:
        """持续读走子进程输出。

        stdout/stderr 都是 PIPE：不读的话管道缓冲（Windows 上 64KB）写满后，
        serve 会阻塞在写日志上，表现为服务活着但不再响应。
        """
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._output_tail.append(text)
                    logger.debug("opencode serve: %s", text)
        except asyncio.CancelledError:
            raise
        except OSError:
            return

    def _port_in_use(self) -> bool:
        """端口上是否已有进程在监听。

        用连接探测而不是 bind：TIME_WAIT 不会接受连接，重启不会被误判；
        而端口上残留着上一次崩溃的 serve 时必须报错，否则就绪探测会把
        别人的服务当成自己的，会话写进另一个数据库。
        """
        try:
            with socket.create_connection((self.host, self.port), timeout=0.3):
                return True
        except OSError:
            return False

    async def start(self) -> None:
        if self.is_running:
            return
        if self._port_in_use():
            raise BrainError(
                f"opencode serve 端口 {self.port} 已被占用；"
                "请先关掉残留进程或换一个端口"
            )
        self._db_abs().parent.mkdir(parents=True, exist_ok=True)
        self._output_tail.clear()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.project_dir or None,
                env=self._build_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise BrainError(
                f"无法启动 opencode serve（{self.bin}）：{exc.strerror or exc}；"
                "请确认 opencode.bin 指向的文件存在，或已在 PATH 中"
            ) from exc
        self._proc = proc
        for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
            if stream is not None:
                self._drain_tasks.append(asyncio.create_task(self._drain(stream)))
        try:
            await self._wait_ready()
        except BaseException:
            # 取消（CancelledError 是 BaseException）同样要收进程，否则
            # serve 会占着端口活下来。stop() 用 shield 保护：外层再被取消
            # 也不能半途打断清理。
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(self.stop())
            raise

    async def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.returncode is not None:
                detail = self.recent_output(20) or "进程已退出，没有输出"
                raise BrainError(f"opencode serve 启动失败：{detail[:400]}")
            try:
                if await self._probe():
                    return
            # 探测异常只记录 last_error 并重试，最终统一抛 BrainError；不能把启动流程打挂。
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            await asyncio.sleep(0.25)
        detail = self.recent_output(20) or last_error
        raise BrainError(f"opencode serve 启动超时：{detail[:400]}")

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        tasks = self._drain_tasks
        self._drain_tasks = []
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
