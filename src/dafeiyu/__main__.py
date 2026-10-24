from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from .brain.client import BrainError
from .brain.factory import build_brain, current_provider, local_model_name
from .brain.opencode import resolve_opencode_bin
from .brain.opencode_server import OpenCodeServer
from .brain.persona import load_persona
from .config import ConfigError, Settings, load_settings
from .control import start_control_server
from .gateway.qq import QQAdapter
from .memory.store import Store
from .router import Router
from .runtime import load_runtime
from .service import Service
from .workers.impressions import ImpressionWorker
from .workers.summary import SummaryScheduler, summary_loop

log = logging.getLogger(__name__)


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
        impressions = (
            ImpressionWorker(store, brain, every=settings.memory.extract_every)
            if provider in ("token", "local_model")
            else None
        )

        adapter = QQAdapter(
            host=settings.ws_host,
            port=settings.ws_port,
            token=settings.ws_token,
            bot_qq=settings.bot_qq,
        )
        router = Router(settings, store)
        service = Service(
            settings, store, brain, adapter, router, persona_text, impressions
        )

        scheduler = (
            SummaryScheduler(store, brain)
            if provider in ("token", "local_model")
            else None
        )
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
