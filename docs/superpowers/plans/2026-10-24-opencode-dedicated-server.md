# OpenCode 专用实例 + 群 forget + 错误回聊天（Phase 1）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让机器人使用一个随自己启停的专用 OpenCode 服务，支持群管理员 `/forget`，并在 OpenCode 调用失败时向聊天窗口返回一句短提示。

**Architecture:** Phase 1 由 bot 进程通过 `OpenCodeServer` 拉起 `opencode serve --hostname 127.0.0.1 --port 24310`，使用独立 `OPENCODE_DB`；`OpenCodeBrain` 调用时追加 `--server http://127.0.0.1:24310`。Phase 2 再把进程所有权移交给 supervisor，`OpenCodeServer` 类复用。

**Tech Stack:** Python 3.11、asyncio、httpx、pytest、OpenCode CLI 2.0.18。

**Spec:** `docs/superpowers/specs/2026-10-24-opencode-supervisor-dashboard-design.md`

---

## Global Constraints

- 工作目录：`C:\MyFile\AICoding\dafeiyu-bot`，分支 `feat/qq-bot`。
- 不改变群聊触发策略（@、关键词、采样）和私聊预算逻辑。
- 不删除 OpenCode 侧旧 session；`/forget` 只做软忘记。
- 不把原始 stderr、JSON、路径、token 发到聊天。
- 保持 `brain.provider: local` 路径不变。
- 每个 task 结束后运行对应测试；最后运行完整 `pytest -q`。
- 每个 task 通过后提交一次，提交信息见各 task。
- 不新增依赖；`httpx` 已在 `pyproject.toml`。

## Review Focus

- `--server` 必须出现在 `opencode run` 的子命令参数中、文本参数之前。
- `OpenCodeServer.stop()` 必须可重复调用，并且启动失败时也会清理子进程。
- `manage_server: true` 与 `server_url` 非空必须报配置错误，避免“管理一个服务却连另一个服务”。
- 群 `/forget` 必须先检查管理员，再清任何数据。
- 错误回复只包含固定短句，完整异常只进日志。
- `main()` 的清理顺序：取消 summary task → 停 bot → 停 OpenCode → 关 store。
- 现有 `test_brain_error_does_not_send_or_crash` 会与新行为冲突，必须改名并更新断言。

---

## File Structure

| 文件 | 责任 |
| --- | --- |
| `src/dafeiyu/config.py` | 新增 `OpenCodeConfig` 的 server/manage/db/host/port 字段与校验 |
| `src/dafeiyu/brain/opencode_server.py` | 新增专用 `opencode serve` 进程管理器 |
| `src/dafeiyu/brain/opencode.py` | `OpenCodeBrain` 支持 `server_url`，命令追加 `--server` |
| `src/dafeiyu/__main__.py` | 启动/停止专用 serve，注入 server URL，整理清理顺序 |
| `src/dafeiyu/service.py` | 群 `/forget`、共用 `_forget_chat`、错误短回复 |
| `config.yaml` | 启用 `manage_server`、独立 DB、24310 端口 |
| `tests/test_config.py` | 配置默认值、解析、非法值 |
| `tests/test_opencode_server.py` | 进程命令、环境、启动就绪、超时、停止 |
| `tests/test_opencode_brain.py` | `--server` 命令断言 |
| `tests/test_service.py` | 群 forget 权限、错误回复 |
| `tests/test_review_fixes.py` | 现有 usage 记录测试保持通过 |

---

## Task 1: 配置字段与校验

**Files:**
- Modify: `src/dafeiyu/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_config.py` 的 `test_opencode_provider_does_not_require_deepseek_key` 里追加默认值断言：

```python
assert s.opencode.manage_server is False
assert s.opencode.server_url == ""
assert s.opencode.db_path == "data/opencode-bot.db"
assert s.opencode.host == "127.0.0.1"
assert s.opencode.port == 24310
assert s.opencode.startup_timeout_seconds == 30.0
```

在 `test_opencode_section_is_parsed` 的 YAML 字符串里追加：

```yaml
  manage_server: true
  server_url: ""
  db_path: data/test-opencode.db
  host: 127.0.0.1
  port: 24310
  startup_timeout_seconds: 5
```

并断言：

```python
assert s.opencode.manage_server is True
assert s.opencode.server_url == ""
assert s.opencode.db_path == "data/test-opencode.db"
assert s.opencode.host == "127.0.0.1"
assert s.opencode.port == 24310
assert s.opencode.startup_timeout_seconds == 5.0
```

新增三个测试：

```python
def test_opencode_manage_server_rejects_server_url(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "triggers:",
        "brain:\n  provider: opencode\n"
        "opencode:\n  manage_server: true\n"
        "  server_url: http://127.0.0.1:24310\n"
        "triggers:",
    )
    with pytest.raises(ConfigError, match="server_url"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


def test_opencode_port_must_be_valid(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "triggers:",
        "brain:\n  provider: opencode\nopencode:\n  port: 0\ntriggers:",
    )
    with pytest.raises(ConfigError, match="opencode.port"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))


def test_opencode_startup_timeout_must_be_positive(tmp_path: Path) -> None:
    env = write_env(tmp_path, "BOT_QQ=10001\n")
    yaml = BASE_YAML.replace(
        "triggers:",
        "brain:\n  provider: opencode\n"
        "opencode:\n  startup_timeout_seconds: 0\ntriggers:",
    )
    with pytest.raises(ConfigError, match="startup_timeout_seconds"):
        load_settings(env_file=env, config_file=write_yaml(tmp_path, yaml))
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
.\.venv\Scripts\python -m pytest tests/test_config.py -q
```

Expected: FAIL，缺少新字段/校验。

- [ ] **Step 3: 实现字段**

在 `OpenCodeConfig` 追加：

```python
    manage_server: bool = False
    server_url: str = ""
    db_path: str = "data/opencode-bot.db"
    host: str = "127.0.0.1"
    port: int = 24310
    startup_timeout_seconds: float = 30.0
```

在 `load_settings()` 解析 `opencode` 段时增加：

```python
        manage_server = raw.get("manage_server", False)
        if not isinstance(manage_server, bool):
            raise ConfigError("opencode.manage_server 必须是布尔值")
        server_url = str(raw.get("server_url") or "").strip()
        db_path = str(raw.get("db_path") or "data/opencode-bot.db").strip()
        host = str(raw.get("host") or "127.0.0.1").strip()
        port = _int(raw.get("port", 24310), "opencode.port")
        startup_timeout = _float(
            raw.get("startup_timeout_seconds", 30.0),
            "opencode.startup_timeout_seconds",
        )
        if manage_server and server_url:
            raise ConfigError(
                "opencode.manage_server 为 true 时 server_url 必须为空"
            )
        if manage_server and not db_path:
            raise ConfigError("opencode.manage_server 为 true 时 db_path 不能为空")
        if not 1 <= port <= 65535:
            raise ConfigError("opencode.port 必须在 1-65535 之间")
        if startup_timeout <= 0:
            raise ConfigError("opencode.startup_timeout_seconds 必须大于 0")
```

构造 `OpenCodeConfig` 时传入这些字段。

- [ ] **Step 4: 运行测试确认通过**

```powershell
.\.venv\Scripts\python -m pytest tests/test_config.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/dafeiyu/config.py tests/test_config.py
git commit -m "feat: add dedicated opencode server config"
```

---

## Task 2: `OpenCodeServer` 进程管理器

**Files:**
- Create: `src/dafeiyu/brain/opencode_server.py`
- Test: `tests/test_opencode_server.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_opencode_server.py`：

```python
import asyncio

import pytest

from dafeiyu.brain.client import BrainError
from dafeiyu.brain.opencode_server import OpenCodeServer


class FakeProc:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.stderr = None

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


def test_command_and_env(tmp_path):
    server = OpenCodeServer(
        "opencode.exe",
        str(tmp_path),
        "data/bot.db",
        port=24310,
    )
    assert server.command == [
        "opencode.exe",
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        "24310",
    ]
    assert server.url == "http://127.0.0.1:24310"
    env = server._build_env()
    assert env["OPENCODE_DB"] == str((tmp_path / "data" / "bot.db").resolve())
    assert env["NO_COLOR"] == "1"


async def test_start_waits_until_probe_ready(monkeypatch, tmp_path):
    proc = FakeProc()
    calls = 0

    async def fake_exec(*args, **kwargs):
        return proc

    async def probe():
        nonlocal calls
        calls += 1
        return calls >= 2

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    server = OpenCodeServer(
        "opencode.exe", str(tmp_path), "data/bot.db", startup_timeout=1.0
    )
    monkeypatch.setattr(server, "_probe", probe)

    await server.start()
    assert calls == 2
    assert server.is_running is True

    await server.stop()
    assert proc.terminated is True
    assert server.is_running is False


async def test_start_timeout_stops_process(monkeypatch, tmp_path):
    proc = FakeProc()

    async def fake_exec(*args, **kwargs):
        return proc

    async def probe():
        return False

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    server = OpenCodeServer(
        "opencode.exe", str(tmp_path), "data/bot.db", startup_timeout=0.01
    )
    monkeypatch.setattr(server, "_probe", probe)

    with pytest.raises(BrainError, match="启动超时"):
        await server.start()
    assert proc.terminated is True


async def test_start_fails_when_process_exits(monkeypatch, tmp_path):
    proc = FakeProc(returncode=1)

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    server = OpenCodeServer(
        "opencode.exe", str(tmp_path), "data/bot.db", startup_timeout=1.0
    )

    with pytest.raises(BrainError, match="启动失败"):
        await server.start()
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
.\.venv\Scripts\python -m pytest tests/test_opencode_server.py -q
```

Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现**

创建 `src/dafeiyu/brain/opencode_server.py`，实现：

```python
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
            self._client = httpx.AsyncClient(timeout=1.0)
        response = await self._client.get(f"{self.url}/api/info")
        return response.status_code == 200

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
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
.\.venv\Scripts\python -m pytest tests/test_opencode_server.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/dafeiyu/brain/opencode_server.py tests/test_opencode_server.py
git commit -m "feat: manage dedicated opencode serve process"
```

---

## Task 3: `OpenCodeBrain` 支持 `--server`

**Files:**
- Modify: `src/dafeiyu/brain/opencode.py`
- Test: `tests/test_opencode_brain.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_opencode_brain.py` 增加：

```python
def test_build_command_contains_server(store: Store) -> None:
    brain = OpenCodeBrain(
        "C:/opencode.exe",
        store,
        agent="fish",
        server_url="http://127.0.0.1:24310",
    )
    assert brain._build_command("你好", "ses_1") == [
        "C:/opencode.exe",
        "run",
        "--server",
        "http://127.0.0.1:24310",
        "--agent",
        "fish",
        "--format",
        "json",
        "--session",
        "ses_1",
        "你好",
    ]
```

保留现有 `test_build_command_contains_agent_and_session` 的无 server 断言不变。

- [ ] **Step 2: 运行测试确认失败**

```powershell
.\.venv\Scripts\python -m pytest tests/test_opencode_brain.py -q
```

Expected: FAIL，`server_url` 参数不存在。

- [ ] **Step 3: 实现**

在 `OpenCodeBrain.__init__` 增加参数：

```python
        server_url: str = "",
```

并保存：

```python
        self.server_url = server_url.strip()
```

把 `_build_command` 改为：

```python
    def _build_command(self, text: str, session_id: str | None) -> list[str]:
        cmd = [self.bin, "run"]
        if self.server_url:
            cmd.extend(["--server", self.server_url])
        cmd.extend(["--agent", self.agent, "--format", "json"])
        if session_id:
            cmd.extend(["--session", session_id])
        cmd.append(text)
        return cmd
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
.\.venv\Scripts\python -m pytest tests/test_opencode_brain.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/dafeiyu/brain/opencode.py tests/test_opencode_brain.py
git commit -m "feat: let opencode brain target a dedicated server"
```

---

## Task 4: `main()` 启停专用 serve

**Files:**
- Modify: `src/dafeiyu/__main__.py`

- [ ] **Step 1: 实现生命周期**

导入：

```python
from .brain.opencode_server import OpenCodeServer
```

在 `main()` 中，`store.init_schema()` 之后声明：

```python
    server: OpenCodeServer | None = None
    service: Service | None = None
    service_started = False
    summary_task: asyncio.Task | None = None
```

把从 `provider = settings.brain.provider` 到 `await asyncio.Event().wait()` 的主体放进 `try`，`finally` 里按顺序清理：

```python
    finally:
        if summary_task is not None:
            summary_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await summary_task
        if service_started and service is not None:
            await service.stop()
        if server is not None:
            await server.stop()
        store.close()
```

在 `provider == "opencode"` 分支中：

```python
        manage_server = settings.opencode.manage_server
        if manage_server:
            server = OpenCodeServer(
                bin_path=opencode_bin,
                project_dir=settings.opencode.project_dir,
                db_path=settings.opencode.db_path,
                host=settings.opencode.host,
                port=settings.opencode.port,
                startup_timeout=settings.opencode.startup_timeout_seconds,
            )
            await server.start()
            server_url = server.url
        else:
            server_url = settings.opencode.server_url

        brain = OpenCodeBrain(
            bin_path=opencode_bin,
            store=store,
            agent=settings.opencode.agent,
            project_dir=settings.opencode.project_dir,
            timeout=settings.opencode.timeout_seconds,
            server_url=server_url,
        )
```

`service = Service(...)` 之后、`await service.start()` 之后设置：

```python
        await service.start()
        service_started = True
```

注意：`summary_task` 的创建也要放进 `try` 内，避免异常时绕过清理。

- [ ] **Step 2: 语法与现有测试**

```powershell
.\.venv\Scripts\python -m compileall -q src
.\.venv\Scripts\python -m pytest tests/test_opencode_brain.py tests/test_config.py -q
```

Expected: PASS。

- [ ] **Step 3: 手动验收（不提交）**

在 `config.yaml` 设置 `manage_server: true` 后启动 bot。另开一个终端：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:24310/api/info
```

Expected: HTTP 200。

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*opencode*serve*' } |
  Select-Object ProcessId, CommandLine
```

Expected: 看到一个 `opencode serve --hostname 127.0.0.1 --port 24310` 子进程。

停止 bot（Ctrl+C）后：

```powershell
Test-NetConnection -ComputerName 127.0.0.1 -Port 24310
```

Expected: `TcpTestSucceeded : False`。

- [ ] **Step 4: 提交**

```powershell
git add src/dafeiyu/__main__.py
git commit -m "feat: start and stop dedicated opencode with the bot"
```

---

## Task 5: 群 `/forget`

**Files:**
- Modify: `src/dafeiyu/service.py`
- Test: `tests/test_service.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_service.py` 导入：

```python
from dafeiyu.service import FORGET_REPLY
```

新增：

```python
async def test_forget_group_clears_memory_for_admin(tmp_path):
    settings = make_settings(
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({2002}))
    )
    service, store, brain, adapter, _ = build(
        tmp_path, settings=settings, brain=FakeOpenCodeBrain()
    )
    store.set_opencode_session(ChatType.GROUP, 1001, "ses_group")
    store.add_message(make_msg(content="记住我", message_id="gf0"))

    await service.handle(make_msg(content="/forget", message_id="gf1"))

    assert store.get_opencode_session(ChatType.GROUP, 1001) is None
    assert brain.forgotten == [(ChatType.GROUP, 1001)]
    assert adapter.sent[-1] == (ChatType.GROUP, 1001, FORGET_REPLY)


async def test_forget_group_requires_admin(tmp_path):
    settings = make_settings(
        groups=GroupsConfig(frozenset(), frozenset(), frozenset({2002}))
    )
    service, store, brain, adapter, _ = build(
        tmp_path, settings=settings, brain=FakeOpenCodeBrain()
    )
    store.set_opencode_session(ChatType.GROUP, 1001, "ses_group")

    await service.handle(
        make_msg(user_id=99, content="/forget", message_id="gf2")
    )

    assert store.get_opencode_session(ChatType.GROUP, 1001) == "ses_group"
    assert brain.forgotten == []
    assert adapter.sent == []
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
.\.venv\Scripts\python -m pytest tests/test_service.py -q
```

Expected: FAIL，`FORGET_REPLY` 不存在且群 forget 未处理。

- [ ] **Step 3: 实现**

在 `service.py` 顶部常量区增加：

```python
FORGET_REPLY = "记忆清空了，本鱼失忆中……🫧"
```

新增方法：

```python
    async def _forget_chat(self, msg: Message) -> None:
        self.store.forget_chat(msg.chat_type, msg.chat_id)
        forget = getattr(self.brain, "forget", None)
        if forget is not None:
            result = forget(msg.chat_type, msg.chat_id)
            if inspect.isawaitable(result):
                await result
        await self.adapter.send(msg.chat_type, msg.chat_id, FORGET_REPLY)
```

把私聊 `forget` 分支改为调用 `await self._forget_chat(msg)`。

群聊分支：

```python
        if msg.chat_type is ChatType.GROUP:
            if cmd not in ("闭嘴", "shutup", "醒来", "wake", "forget"):
                return False
            if msg.user_id not in self.settings.groups.admins:
                log.info(
                    "非管理员尝试执行群指令 %s/%s: user=%s cmd=%s",
                    msg.chat_id,
                    msg.message_id,
                    msg.user_id,
                    cmd,
                )
                return True
            if cmd == "forget":
                await self._forget_chat(msg)
                return True
            if cmd in ("闭嘴", "shutup"):
                self.router.quiet(msg.chat_type, msg.chat_id, SHUTUP_MINUTES)
            else:
                self.router.wake(msg.chat_type, msg.chat_id)
            return True
```

确认 `service.py` 已导入 `inspect`；没有则添加。

- [ ] **Step 4: 运行测试确认通过**

```powershell
.\.venv\Scripts\python -m pytest tests/test_service.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/dafeiyu/service.py tests/test_service.py
git commit -m "feat: support group forget for admins"
```

---

## Task 6: OpenCode 错误回聊天

**Files:**
- Modify: `src/dafeiyu/service.py`
- Test: `tests/test_service.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_service.py` 导入：

```python
from dafeiyu.service import BRAIN_ERROR_REPLY
```

把 `test_brain_error_does_not_send_or_crash` 改名为：

```python
async def test_brain_error_sends_short_reply(tmp_path):
    service, _, _, adapter, _ = build(
        tmp_path, brain=FakeBrain(error=BrainError("boom"))
    )
    await service.handle(make_msg())

    assert adapter.sent == [(ChatType.GROUP, 1001, BRAIN_ERROR_REPLY)]
```

保留 `tests/test_review_fixes.py::test_brain_error_records_usage` 不变。

- [ ] **Step 2: 运行测试确认失败**

```powershell
.\.venv\Scripts\python -m pytest tests/test_service.py::test_brain_error_sends_short_reply -q
```

Expected: FAIL，`BRAIN_ERROR_REPLY` 不存在。

- [ ] **Step 3: 实现**

在 `service.py` 常量区增加：

```python
BRAIN_ERROR_REPLY = "本鱼刚刚和大脑断线了…你过一会儿再问我？🐋"
```

修改 `_reply` 的异常分支：

```python
        except BrainError as exc:
            # 失败也占一次调用额度，避免异常时无限重试烧钱。
            self.store.record_usage(msg.chat_type, msg.chat_id, "error", 0, 0)
            log.warning("模型调用失败 %s/%s: %s", msg.chat_id, msg.message_id, exc)
            await self.adapter.send(
                msg.chat_type, msg.chat_id, BRAIN_ERROR_REPLY
            )
            return
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
.\.venv\Scripts\python -m pytest tests/test_service.py tests/test_review_fixes.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/dafeiyu/service.py tests/test_service.py
git commit -m "feat: send short chat reply when opencode fails"
```

---

## Task 7: 配置、完整验证、文档

**Files:**
- Modify: `config.yaml`
- Modify: `docs/superpowers/specs/2026-10-24-opencode-supervisor-dashboard-design.md`（如实现与设计有偏差）
- Test: 全量

- [ ] **Step 1: 更新 `config.yaml`**

在 `opencode:` 下增加：

```yaml
  manage_server: true
  server_url: ""
  db_path: data/opencode-bot.db
  host: 127.0.0.1
  port: 24310
  startup_timeout_seconds: 30
```

- [ ] **Step 2: 全量测试**

```powershell
.\.venv\Scripts\python -m pytest -q
```

Expected: 全部 PASS。

- [ ] **Step 3: 手动验收**

1. 启动 bot，确认 `http://127.0.0.1:24310/api/info` 200。
2. 群管理员发 `/forget`，确认回复 `FORGET_REPLY`，下一条消息创建新 session。
3. 非管理员发 `/forget`，确认无回复、旧 session 不变。
4. 停掉 OpenCode serve，再发一条会触发回复的消息，确认聊天收到 `BRAIN_ERROR_REPLY`，日志里有完整异常。
5. 正常停止 bot，确认 24310 端口关闭。

- [ ] **Step 4: 提交**

```powershell
git add config.yaml docs/superpowers/specs/2026-10-24-opencode-supervisor-dashboard-design.md
git commit -m "chore: enable dedicated opencode server config"
```

---

## Phase 2 / Phase 3 入口

Phase 1 完成后，下一份计划只做 supervisor + dashboard，不再改 OpenCode session 语义：

- 新增 `src/dafeiyu/supervisor.py`（或 `src/dafeiyu/dashboard/`），复用 `OpenCodeServer`。
- `config.yaml` 增加 `supervisor.dashboard`。
- `start-all.ps1` 改为隐藏启动 supervisor。
- dashboard 增加状态、日志 SSE、二维码、会话管理、启停控制。

不要在本计划里提前实现 dashboard，避免 Phase 1 的验收被前端问题盖住。

---

## 实现期修订（2026-10-24）

执行本计划时发现三处环境事实与计划假设不符，已按 Ruling 落地（详见 `.superpowers/sdd/2026-10-24-opencode-dedicated-server/progress.md`）：

1. 端口 4310 被本机 `QQ.exe` 占用，且 `14214-14313` 是 Windows 保留端口段 → 默认端口改为 **24310**。
2. opencode v2.0.18 的 `serve` 强制 Basic 认证，`/api/info` 未认证返回 401 → 就绪判据为 **状态码 < 500**。
3. 系统代理会劫持 `127.0.0.1` 请求 → 探测使用 `trust_env=False`。
