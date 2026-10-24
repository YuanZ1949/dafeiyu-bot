from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("data/runtime.json")
PROVIDERS = ("opencode", "token", "local_model")


@dataclass(frozen=True)
class RuntimeState:
    """dashboard 上做出的运行时选择，覆盖 config.yaml 的默认值。"""

    provider: str = ""
    local_model_model: str = ""

    def with_provider(self, provider: str, model: str = "") -> "RuntimeState":
        if provider == "local_model":
            return RuntimeState(provider=provider, local_model_model=model or self.local_model_model)
        return RuntimeState(provider=provider, local_model_model=self.local_model_model)


def load_runtime(path: str | Path = DEFAULT_PATH) -> RuntimeState:
    target = Path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RuntimeState()
    except (OSError, ValueError):
        log.warning("runtime.json 无法读取或格式非法，回落到 config.yaml")
        return RuntimeState()
    if not isinstance(raw, dict):
        log.warning("runtime.json 顶层不是对象，回落到 config.yaml")
        return RuntimeState()
    provider = str(raw.get("provider") or "").strip().casefold()
    if provider == "local":
        provider = "token"
    if provider not in PROVIDERS:
        provider = ""
    return RuntimeState(
        provider=provider,
        local_model_model=str(raw.get("local_model_model") or "").strip(),
    )


def save_runtime(state: RuntimeState, path: str | Path = DEFAULT_PATH) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    payload = {
        "provider": state.provider,
        "local_model_model": state.local_model_model,
        "updatedAt": _now_iso(),
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
