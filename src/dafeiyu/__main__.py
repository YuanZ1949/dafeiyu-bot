from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .brain.client import BrainClient
from .brain.persona import load_persona
from .config import Settings, load_settings
from .gateway.qq import QQAdapter
from .memory.store import Store
from .router import Router
from .service import Service
from .workers.impressions import ImpressionWorker

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

    db_path = Path(settings.db_path)
    if db_path.parent and str(db_path.parent) != ".":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)
    store.init_schema()

    brain = BrainClient(
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=settings.model,
    )
    adapter = QQAdapter(
        host=settings.ws_host,
        port=settings.ws_port,
        token=settings.ws_token,
        bot_qq=settings.bot_qq,
    )
    router = Router(settings, store)
    impressions = ImpressionWorker(store, brain, every=settings.memory.extract_every)
    service = Service(
        settings, store, brain, adapter, router, load_persona(), impressions
    )

    await service.start()
    log.info(
        "溟月已上线，等待 NapCat 反向 WS 连接 ws://%s:%s（QQ %s）",
        settings.ws_host,
        settings.ws_port,
        settings.bot_qq,
    )
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        log.info("收到退出信号")
    finally:
        await service.stop()
        store.close()


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
