from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .brain.client import BrainClient, BrainError
from .brain.factory import build_brain, current_provider, local_model_name
from .brain.lmstudio import ensure_server, stop_server
from .brain.opencode import resolve_opencode_bin
from .brain.opencode_auth import password_env, resolve_password
from .brain.opencode_server import OpenCodeServer
from .brain.persona import load_persona
from .brain.runtime_config import server_env
from .config import ConfigError, Settings, load_settings
from .control import MemoryActions, start_control_server
from .gateway.base import Adapter
from .gateway.channels import wechat_enabled_for
from .gateway.qq import QQAdapter
from .gateway.wechat import WeChatAdapter
from .memory.store import Store
from .memory.workspace import Workspace
from .models import ChatType
from .router import Router
from .runtime import load_runtime
from .service import Service
from .supervisor.audit import AuditLog
from .version import get_version
from .workers.failure import FailureBackoff, MemoryPaused
from .workers.followup import followup_loop
from .workers.impressions import ImpressionWorker
from .workers.observer import ObserverWorker, observer_loop
from .workers.outcome import OutcomeWorker, outcome_loop
from .workers.reflector import ReflectorWorker, reflector_loop
from .workers.reminders import reminder_loop
from .workers.retention import retention_loop
from .workers.summary import SummaryScheduler, summary_loop

log = logging.getLogger(__name__)

# WAL 定期落盘周期：与 memory.retention_days 无关，防止 -wal 一直涨。
CHECKPOINT_INTERVAL_SECONDS = 6 * 3600


@dataclass(frozen=True)
class Workers:
    """摘要/印象两条记忆旁路，以及心智层三条旁路；不支持的脑类型给 None。"""

    impressions: ImpressionWorker | None = None
    scheduler: SummaryScheduler | None = None
    observer: ObserverWorker | None = None
    reflector: ReflectorWorker | None = None
    outcome: OutcomeWorker | None = None


def build_workers(
    settings: Settings,
    store: Store,
    brain: Any,
    provider: str,
    *,
    brain_getter: Callable[[], Any] | None = None,
    workspace: Workspace | None = None,
) -> Workers:
    """判据是脑有没有 ``complete_detailed``，不是 provider 名字。

    opencode 的 CLI 传输没有这个方法，按 provider 判断会建出一个
    运行时才炸的 worker；HTTP 传输有，所以 opencode 也能跑旁路。
    """
    backoff = FailureBackoff(persistence=store)
    impressions: ImpressionWorker | None = None
    scheduler: SummaryScheduler | None = None
    if hasattr(brain, "complete_detailed"):
        # 两条旁路共用一份退避：配额熔断必须一次管住摘要与印象，且写入库跨重启存活。
        impressions = ImpressionWorker(
            store,
            brain,
            every=settings.memory.extract_every,
            brain_getter=brain_getter,
            backoff=backoff,
        )
        scheduler = SummaryScheduler(
            store, brain, brain_getter=brain_getter, backoff=backoff
        )
    else:
        # 静默跳过会让用户以为「记忆坏了」：把模式和缺失的方法说清楚。
        log.warning(
            "brain=%s（transport=%s）没有 complete_detailed，"
            "摘要/印象旁路已跳过：记忆不会自动生成，面板只能看到历史数据",
            provider,
            getattr(settings.opencode, "transport", "-"),
        )

    observer: ObserverWorker | None = None
    reflector: ReflectorWorker | None = None
    outcome: OutcomeWorker | None = None
    mind = settings.mind
    if mind.enabled:
        # spec §6.3：Observer 默认只用本地模型，别和主链路抢同一份免费额度。
        mind_brain = brain
        mind_getter = brain_getter
        if mind.observer_provider == "local":
            local = settings.local_model
            if local.base_url and local.model:
                mind_brain = BrainClient(
                    api_key=local.api_key or "local",
                    base_url=local.base_url,
                    model=local.model,
                    trust_env=False,
                    max_tokens=local.max_tokens,
                    timeout=local.timeout_seconds,
                    reasoning_effort=local.reasoning_effort or None,
                )
                mind_getter = None
            else:
                log.warning(
                    "mind.observer_provider=local 但 local_model 未配置，"
                    "Observer/Reflector 已跳过"
                )
                mind_brain = None
        if mind_brain is not None and hasattr(mind_brain, "complete_detailed"):
            if mind.observer_enabled:
                # 独立退避：Observer 撞配额不能把主链路一起熔断。
                observer = ObserverWorker(
                    store,
                    mind_brain,
                    mind,
                    backoff=FailureBackoff(persistence=store),
                    brain_getter=mind_getter,
                )
            reflector = ReflectorWorker(
                store,
                mind_brain,
                mind,
                workspace,
                backoff=FailureBackoff(persistence=store),
                brain_getter=mind_getter,
            )
        # 结果打标是纯 SQL，与脑无关；心智层打开就算，重启也能继续对账。
        outcome = OutcomeWorker(store)

    return Workers(
        impressions=impressions,
        scheduler=scheduler,
        observer=observer,
        reflector=reflector,
        outcome=outcome,
    )


def build_memory_actions(store: Store, workers: Workers) -> MemoryActions | None:
    """把 worker 包装成控制口能调用的三个动作；旁路全关时返回 None（路由 503）。"""
    if workers.impressions is None and workers.scheduler is None:
        return None

    async def summarize(chat_type: ChatType, chat_id: str, date: str):
        if workers.scheduler is None:
            raise RuntimeError("摘要旁路未启用（当前 brain 无 complete_detailed）")
        return await workers.scheduler.summarize_now(chat_type, chat_id, date)

    async def extract(chat_type: ChatType, chat_id: str):
        if workers.impressions is None:
            raise RuntimeError("印象旁路未启用（当前 brain 无 complete_detailed）")
        if workers.impressions.paused:
            raise MemoryPaused("配额暂停中，稍后再试")
        return await workers.impressions.extract(chat_type, chat_id)

    def unblock() -> None:
        # 只清库不够：FailureBackoff 在内存里记着单调时钟截止时间，配额暂停
        # 期间不会回查数据库，不清内存就得等自然到期才真正恢复。
        seen: set[int] = set()
        for worker in (
            workers.scheduler,
            workers.impressions,
            workers.observer,
            workers.reflector,
        ):
            if worker is None:
                continue
            backoff = getattr(worker, "_backoff", None)
            if backoff is None or id(backoff) in seen:
                continue
            seen.add(id(backoff))
            reset = getattr(backoff, "reset", None)
            if callable(reset):
                reset()

    return MemoryActions(
        summarize=summarize,
        extract=extract,
        delete_impression=store.delete_impression,
        unblock=unblock,
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


async def checkpoint_loop(
    store: Store,
    interval: float = CHECKPOINT_INTERVAL_SECONDS,
    stop: asyncio.Event | None = None,
) -> None:
    """每 interval 秒把 WAL 截断一次；启动时不立即执行，stop 置位即退出。"""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            # wait_for 正常返回说明 stop 被置位：这一轮不再 checkpoint。
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            # checkpoint 自己会等锁（最多 200ms）：放到工作线程，别卡住事件循环。
            result = await asyncio.to_thread(store.checkpoint)
            if result.error:
                log.warning("WAL checkpoint 失败：%s", result.error)
            elif result.busy:
                log.warning("WAL checkpoint 被占用（busy=%s），下一轮再试", result.busy)
            elif result.log < 0:
                log.debug("WAL checkpoint 跳过：数据库不是 WAL 模式")
            else:
                log.debug(
                    "WAL checkpoint 完成（log=%s, checkpointed=%s, truncated=%s）",
                    result.log,
                    result.checkpointed,
                    result.truncated,
                )
        except Exception:
            log.exception("WAL checkpoint 失败")


def ensure_manage_server_password(settings: Settings) -> tuple[Settings, str]:
    """bot 自己拉起 serve（manage_server）时统一 serve 口令（P7）。

    口令优先取配置值（config.py 已把 OPENCODE_SERVER_PASSWORD 作为缺省回退读进
    ``settings.opencode.server_password``）；都没有就生成一个 32 位十六进制口令，
    并写回 settings，保证随后 build_brain 建 HTTP 客户端时用的是同一份口令。
    返回 (可能被替换的 settings, 口令)，口令只进子进程 env，不落盘不写日志。
    """
    password = resolve_password(settings.opencode.server_password)
    if not settings.opencode.server_password:
        settings = replace(
            settings,
            opencode=replace(settings.opencode, server_password=password),
        )
    return settings, password


def _build_qq_adapter(settings: Settings) -> QQAdapter:
    return QQAdapter(
        host=settings.ws_host,
        port=settings.ws_port,
        token=settings.ws_token,
        bot_qq=settings.bot_qq,
    )


def build_adapters(settings: Settings) -> dict[str, Adapter]:
    """按 ``gateway.provider`` 构造启用的通道。

    未启用的通道在这里完全不构造：``WeChatAdapter`` 一旦建出来，配置就会被
    读取、桥端口就有被连接的风险；provider 没选微信时连 ``settings.wechat``
    都不碰。
    """
    provider = settings.gateway.provider
    if provider == "qq":
        return {"qq": _build_qq_adapter(settings)}
    if provider == "both":
        adapters: dict[str, Adapter] = {"qq": _build_qq_adapter(settings)}
        if wechat_enabled_for(settings):
            adapters["wechat"] = WeChatAdapter(settings.wechat)
        else:
            log.warning(
                "gateway.provider=both 但 gateway.wechat.enabled=false，"
                "微信通道未启用，本次只上线 QQ"
            )
        return adapters
    if wechat_enabled_for(settings):
        return {"wechat": WeChatAdapter(settings.wechat)}
    log.warning(
        "gateway.provider=wechat 但 gateway.wechat.enabled=false，降级为 qq"
    )
    return {"qq": _build_qq_adapter(settings)}


def log_active_channels(
    settings: Settings, adapters: dict[str, Adapter], brain_provider: str
) -> None:
    """每个启用通道一行启动日志；微信只报适配器和桥端口，不打印 token。"""
    if "qq" in adapters:
        log.info(
            "%s已上线（brain=%s），QQ 通道适配器就绪：等待 NapCat 反向 WS 连接 "
            "ws://%s:%s（QQ %s）",
            settings.persona.name,
            brain_provider,
            settings.ws_host,
            settings.ws_port,
            settings.bot_qq,
        )
    if "wechat" in adapters:
        log.info(
            "%s已上线（brain=%s），微信通道适配器就绪：等待桥 "
            "ws://%s:%s/wechat-bridge（群范围以面板白名单 groups.whitelist 为准）",
            settings.persona.name,
            brain_provider,
            settings.wechat.bridge_host,
            settings.wechat.bridge_port,
        )


def make_chat_titles() -> Callable[[str], str]:
    """`wxg:<alias>` → 微信群显示名；只用于面板展示，读不到就返回空串。

    QQ 的 `qq:<号>` 本来就是可读的群号，不需要翻译。peers.json 由微信桥
    维护；它缺失或损坏时白名单功能不受影响，面板只显示裸 id。
    """
    registry: Any = None

    def resolve(chat_id: str) -> str:
        nonlocal registry
        if not chat_id.startswith("wxg:") or chat_id == "wxg:self":
            return ""
        if registry is None:
            try:
                from .gateway.wechat_bridge.identity import PeerRegistry

                registry = PeerRegistry()
            except Exception:  # noqa: BLE001 - 展示信息失败不影响白名单
                return ""
        peer = registry.by_alias(chat_id[4:])
        return str(getattr(peer, "display_name", "") or "").strip()

    return resolve


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
    summary_task: asyncio.Task[None] | None = None
    retention_task: asyncio.Task[None] | None = None
    checkpoint_task: asyncio.Task[None] | None = None
    mind_tasks: list[asyncio.Task[None]] = []

    try:
        runtime = load_runtime()
        if provider == "local_model":
            # 上次是本地模型：启动时就把端点准备好，别等第一条消息超时。
            await ensure_server(
                settings.local_model,
                local_model_name(settings, runtime),
            )
        elif settings.local_model.stop_when_unused:
            # 上次不是本地档：清掉历史上切走时残留的 LM Studio 进程。
            # 端点本来就不在时这里只探活一次，不会真调 lms。
            await stop_server(settings.local_model)
        effective_server_url = settings.opencode.server_url
        if provider == "opencode" and settings.opencode.manage_server:
            # P7：bot 自己拉起 serve 时也要把 Basic 口令同时给 serve 与 HTTP 客户端，
            # 否则客户端没口令、serve 随机生成，仍然全 401。
            settings, server_password = ensure_manage_server_password(settings)
            try:
                opencode_bin = resolve_opencode_bin(settings.opencode.bin)
            except BrainError as exc:
                raise ConfigError(str(exc)) from exc
            # server_env 留空时返回 None（沿用启动环境）；口令必须叠加在它上面。
            server_proc_env = server_env(settings.opencode.config_home) or {}
            server_proc_env.update(password_env(server_password))
            server = OpenCodeServer(
                bin_path=opencode_bin,
                project_dir=settings.opencode.project_dir,
                db_path=settings.opencode.db_path,
                host=settings.opencode.host,
                port=settings.opencode.port,
                startup_timeout=settings.opencode.startup_timeout_seconds,
                env=server_proc_env,
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

        adapters = build_adapters(settings)
        router = Router(settings, store)
        workspace = None
        if settings.workspace.enabled:
            workspace = Workspace(settings.workspace.dir)
            try:
                workspace.ensure()
            except OSError as exc:
                raise ConfigError(f"工作区目录创建失败：{exc}") from exc
        # bot 与 supervisor 追加写同一份 JSONL；写失败由 AuditLog 自身吞掉。
        audit = AuditLog(
            Path(settings.audit.path),
            max_bytes=settings.audit.max_bytes,
            backups=settings.audit.backups,
        )
        service = Service(
            settings,
            store,
            brain,
            adapters=adapters,
            router=router,
            persona_text=persona_text,
            workspace=workspace,
            injection=settings.injection,
            audit=audit,
        )

        # 旁路要在 service 建好之后接，brain_getter 才能在 dashboard 换脑时跟上。
        workers = build_workers(
            settings,
            store,
            brain,
            provider,
            brain_getter=lambda: service.brain,
            workspace=workspace,
        )
        service.impressions = workers.impressions
        scheduler = workers.scheduler
        summary_task = (
            asyncio.create_task(summary_loop(scheduler, interval=600))
            if scheduler is not None
            else None
        )
        # 心智层三条旁路：全部独立任务，任一失败只记日志，不阻塞主链路。
        if workers.observer is not None:
            mind_tasks.append(
                asyncio.create_task(
                    observer_loop(
                        workers.observer,
                        interval_seconds=settings.mind.observer_interval_seconds,
                    )
                )
            )
        if workers.reflector is not None:
            mind_tasks.append(
                asyncio.create_task(reflector_loop(workers.reflector, interval=600))
            )
        if workers.outcome is not None:
            mind_tasks.append(
                asyncio.create_task(outcome_loop(workers.outcome, interval=600))
            )
        # 提醒轮询是主动链路：只走 outbox，不调用模型，也不需要 mind 开关。
        mind_tasks.append(asyncio.create_task(reminder_loop(service)))
        # 承诺跟进默认关：开关关时 loop 立即返回，任务本身不占资源。
        mind_tasks.append(asyncio.create_task(followup_loop(service)))
        retention_task = (
            asyncio.create_task(
                retention_loop(store, settings.memory.retention_days)
            )
            if settings.memory.retention_days > 0
            else None
        )
        # WAL 落盘与 retention 无关：即使永久保留消息，-wal 也要定期截断。
        checkpoint_task = asyncio.create_task(checkpoint_loop(store))

        await service.start()
        service_started = True
        control_runner = await start_control_server(
            settings,
            store,
            service,
            adapters.get("qq"),
            memory_actions=build_memory_actions(store, workers),
            chat_titles=make_chat_titles(),
        )
        log_active_channels(settings, adapters, provider)
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            log.info("收到退出信号")
    finally:
        if summary_task is not None:
            summary_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await summary_task
        if retention_task is not None:
            retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retention_task
        if checkpoint_task is not None:
            checkpoint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await checkpoint_task
        for task in mind_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if control_runner is not None:
            await control_runner.cleanup()
        if service_started and service is not None:
            await service.stop()
        if server is not None:
            await server.stop()
        store.close()


def run() -> int:
    # `python -m dafeiyu --version` 必须在配置加载前返回，不能拉起 asyncio 主循环。
    if "--version" in sys.argv[1:]:
        print(f"dafeiyu {get_version()}")
        return 0
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
