"""用户自备 provider API key 的唯一落盘位置（data/secrets/provider.key）。

档位切换、仪表盘表单、doctor 都从这里读写，所以这两点必须在同一处定死：
权限从文件出现的第一刻起就只属于当前用户，替换是原子的——中途失败既不能
留下半截密钥，也不能让最终路径短暂地人人可读。密钥值本身不进日志、不进异常
消息。
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from .supervisor.control_token import harden_token_file

log = logging.getLogger(__name__)

INSTALL_ROOT = Path(__file__).resolve().parents[2]
SECRETS_DIR = INSTALL_ROOT / "data" / "secrets"
KEY_PATH = SECRETS_DIR / "provider.key"


class SecretWriteError(OSError):
    """密钥写盘失败。消息只带路径和错误类名，绝不回显密钥值。"""


def write_secret(value: str, path: Path = KEY_PATH) -> Path:
    """把 provider key 写入 ``path`` 并返回该路径。

    ``value`` 会先 ``strip()``。strip 后为空表示“没有配置密钥”：删掉已有文件
    （文件本来就不存在也算成功），不会留下空文件。写入顺序固定为
    临时文件 → 加固临时文件 → ``os.replace``：先收回权限再换名，最终路径从
    出现那一刻起就不是人人可读，不存在可被抢读的窗口。任何失败都会清掉临时
    文件并抛 :class:`SecretWriteError`（仅含路径与 OS 错误类名）。
    """
    target = Path(path)
    cleaned = value.strip()
    if not cleaned:
        target.unlink(missing_ok=True)
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    os.close(fd)  # mkstemp 本身按 0600 建文件，POSIX 上不会先以宽权限落盘
    tmp_path = Path(raw_tmp)
    try:
        tmp_path.write_text(cleaned, encoding="utf-8")
        harden_token_file(tmp_path)
        os.replace(tmp_path, target)
    except BaseException as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        if not isinstance(exc, OSError):
            raise
        raise SecretWriteError(
            f"provider key 写入失败（{target}）：{type(exc).__name__}"
        ) from exc
    return target


def read_secret(path: Path = KEY_PATH) -> str:
    """读回 provider key；文件不存在（或读不了）返回 ``""``。

    ``""`` 就是“当前没有配置密钥”的状态，调用方不需要区分“文件不存在”和
    “内容为空”；需要区分时用 :func:`secret_exists`。读时容忍 BOM 和首尾空白，
    因为用户可能用记事本手改过这个文件。
    """
    target = Path(path)
    try:
        return target.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        log.warning("provider key 读取失败（%s）：%s", target, type(exc).__name__)
        return ""


def secret_exists(path: Path = KEY_PATH) -> bool:
    """是否已经配了可用的密钥（文件存在且有非空内容）。"""
    return bool(read_secret(path))


def mask(value: str) -> str:
    """给界面/日志用的脱敏：长度 ≤8 全遮，否则只留前 4 后 4。"""
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


__all__ = [
    "INSTALL_ROOT",
    "KEY_PATH",
    "SECRETS_DIR",
    "SecretWriteError",
    "mask",
    "read_secret",
    "secret_exists",
    "write_secret",
]
