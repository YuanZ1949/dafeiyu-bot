from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .brain.client import BrainError
from .brain.factory import build_brain, current_provider, local_model_name
from .brain.opencode import resolve_opencode_bin
from .brain.opencode_server import OpenCodeServer
from .brain.persona import load_persona
from .config import ConfigError, Settings, load_settings
from .control import start_control_server
from .gateway.qq import QQAdapter
from .memory.store import Store
from .memory.workspace import Workspace
from .router import Router
from .runtime import load_runtime
from .service import Service
from .workers.impressions import ImpressionWorker
from .workers.summary import SummaryScheduler, summary_loop

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Workers:
    """摘要/印象两条旁路；不支持的脑类型给 None。"""

    impressions: ImpressionWorker | None = None
    scheduler: SummaryScheduler | None = None


def build_workers(
    settings: Settings,
    store: Store,
    brain: Any,
    provider: str,
    *,
    brain_getter: Callable[[], Any] | None = None,
) -> Workers:
    """判据是脑有没有 ``complete_detailed``，不是 provider 名字。

    opencode 的 CLI 传输没有这个方法，按 provider 判断会建出一个
    运行时才炸的 worker；HTTP 传输有，所以 opencode 也能跑旁路。
    """
    if not hasattr(brain, "complete_detailed"):
        log.debug("brain=%s 不支持 complete_detailed，跳过摘要/印象旁路", provider)
        return Workers()
    return Workers(
        impressions=ImpressionWorker(store, brain, every=settings.memory.extract_every),
        scheduler=SummaryScheduler(store, brain, brain_getter=brain_getter),
    )


def setup_logging(settings: Settings) -> None:
    log_dir = Path(settings.logging.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(log_dir / "dafeiyu.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    # 一条消息约 10 次 HTTP 调用（1 次 prompt + 8~9 次轮询），httpx 的 INFO
    # 会把 bot.log 淹掉，真正有用的行被埋。4xx/5xx 仍在 WARNING 以上，不丢错。
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def main() -> None:
    settings = load_settings()
    setup_logging(settings)

    persona_path = settings.persona.prompt_file
    try:
        persona_text = load_persona(Path(persona_path) if persona_path else None)
    except OSError as exc:
        raise ConfigError(f"人设文件无法读取：{persona_path}") from exc
    if not persona_text:
        raise ConfigError(f"人设文件为空：{persona_path or '内置 persona.md'}")

    db_path = Path(settings.db_path)
    if db_path.parent and str(db_path.parent) != ".":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)
    store.init_schema()
    store.migrate_opencode_sessions()

    provider = current_provider(settings, load_runtime())
    server: OpenCodeServer | None = None
    service: Service | None = None
    control_runner = None
    service_started = False
    summary_task: asyncio.Task | None = None

    try:
        runtime = load_runtime()
        effective_server_url = settings.opencode.server_url
        if provider == "opencode" and settings.opencode.manage_server:
            try:
                opencode_bin = resolve_opencode_bin(settings.opencode.bin)
            except BrainError as exc:
                raise ConfigError(str(exc)) from exc
            server = OpenCodeServer(
                bin_path=opencode_bin,
                project_dir=settings.opencode.project_dir,
                db_path=settings.opencode.db_path,
                host=settings.opencode.host,
                port=settings.opencode.port,
                startup_timeout=settings.opencode.startup_timeout_seconds,
            )
            try:
                await server.start()
            except BrainError as exc:
                raise ConfigError(str(exc)) from exc
            effective_server_url = server.url

        brain = build_brain(
            settings,
            store,
            provider,
            local_model_name(settings, runtime) if provider == "local_model" else "",
            server_url=effective_server_url,
        )

        adapter = QQAdapter(
            host=settings.ws_host,
            port=settings.ws_port,
            token=settings.ws_token,
            bot_qq=settings.bot_qq,
        )
        router = Router(settings, store)
        workspace = None
        if settings.workspace.enabled:
            workspace = Workspace(settings.workspace.dir)
            try:
                workspace.ensure()
            except OSError as exc:
                raise ConfigError(f"工作区目录创建失败：{exc}") from exc
        service = Service(
            settings,
            store,
            brain,
            adapter,
            router,
            persona_text,
            workspace=workspace,
            injection=settings.injection,
        )

        # 旁路要在 service 建好之后接，brain_getter 才能在 dashboard 换脑时跟上。
        workers = build_workers(
            settings, store, brain, provider, brain_getter=lambda: service.brain
        )
        service.impressions = workers.impressions
        scheduler = workers.scheduler
        summary_task = (
            asyncio.create_task(summary_loop(scheduler, interval=600))
            if scheduler is not None
            else None
        )

        await service.start()
        service_started = True
        control_runner = await start_control_server(
            settings, store, service, adapter
        )
        log.info(
            "%s已上线（brain=%s），等待 NapCat 反向 WS 连接 ws://%s:%s（QQ %s）",
            settings.persona.name,
            provider,
            settings.ws_host,
            settings.ws_port,
            settings.bot_qq,
        )
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            log.info("收到退出信号")
    finally:
        if summary_task is not None:
            summary_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await summary_task
        if control_runner is not None:
            await control_runner.cleanup()
        if service_started and service is not None:
            await service.stop()
        if server is not None:
            await server.stop()
        store.close()


def run() -> int:
    try:
        asyncio.run(main())
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
