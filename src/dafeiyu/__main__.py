from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from .brain.client import BrainClient, BrainError
from .brain.opencode import OpenCodeBrain, resolve_opencode_bin
from .brain.persona import load_persona
from .config import ConfigError, Settings, load_settings
from .gateway.qq import QQAdapter
from .memory.store import Store
from .router import Router
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

    provider = settings.brain.provider
    if provider == "opencode":
        try:
            opencode_bin = resolve_opencode_bin(settings.opencode.bin)
        except BrainError as exc:
            raise ConfigError(str(exc)) from exc
        brain = OpenCodeBrain(
            bin_path=opencode_bin,
            store=store,
            agent=settings.opencode.agent,
            project_dir=settings.opencode.project_dir,
            timeout=settings.opencode.timeout_seconds,
        )
        impressions = None
    else:
        # trust_env=False：不让系统代理参与，避免 httpx 为代理额外构建 transport。
        brain = BrainClient(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            trust_env=False,
            reasoning_effort=settings.reasoning_effort or None,
        )
        impressions = ImpressionWorker(
            store, brain, every=settings.memory.extract_every
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

    scheduler = SummaryScheduler(store, brain) if provider == "local" else None
    summary_task = (
        asyncio.create_task(summary_loop(scheduler, interval=600))
        if scheduler is not None
        else None
    )

    await service.start()
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
        await service.stop()
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
