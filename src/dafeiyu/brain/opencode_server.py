from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from pathlib import Path

import httpx

from .client import BrainError


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
            self._client = httpx.AsyncClient(timeout=1.0, trust_env=False)
        response = await self._client.get(f"{self.url}/api/info")
        # opencode v2 起 serve 强制 Basic 认证，401 同样说明服务已经就绪。
        return response.status_code < 500

    async def _read_stderr(self) -> str:
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            data = await asyncio.wait_for(self._proc.stderr.read(), timeout=1.0)
        except (asyncio.TimeoutError, OSError):
            return ""
        return data.decode("utf-8", errors="replace")

    async def start(self) -> None:
        if self.is_running:
            return
        self._db_abs().parent.mkdir(parents=True, exist_ok=True)
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.project_dir or None,
            env=self._build_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await self._wait_ready()
        except Exception:
            await self.stop()
            raise

    async def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.returncode is not None:
                detail = await self._read_stderr()
                raise BrainError(f"opencode serve 启动失败：{detail[:400]}")
            try:
                if await self._probe():
                    return
            except Exception as exc:
                last_error = str(exc)
            await asyncio.sleep(0.25)
        raise BrainError(f"opencode serve 启动超时：{last_error[:400]}")

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
