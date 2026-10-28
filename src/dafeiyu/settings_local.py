"""仪表盘写下的本机覆盖层（data/settings.local.yaml）。

``config.yaml`` 是用户手改的正式配置；仪表盘改一项就整份重写它会毁掉注释，
所以覆盖值单独存一份，加载时盖在正式配置之上。写入必须是原子的：仪表盘
随时可能被关闭，半截 YAML 会让下一次启动直接失败。
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

INSTALL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = INSTALL_ROOT / "data" / "settings.local.yaml"

VALID_MODES: tuple[str, ...] = ("free", "key", "local_model")


class OverrideError(Exception):
    """覆盖层内容非法。消息给人看，不回显覆写进去的密钥类字段值。"""


def normalize_mode(value: str) -> str:
    """把 brain.mode 归一成 free / key / local_model。

    大小写和首尾空格都容忍（用户在 yaml 里手写时常带空格）；除此以外的
    任何写法——包括历史拼法 "local"——一律拒绝，免得悄悄跑错档位。
    """
    if not isinstance(value, str):
        raise OverrideError(f"mode 必须是字符串，可选值：{' / '.join(VALID_MODES)}。")
    normalized = value.strip().casefold()
    if normalized not in VALID_MODES:
        raise OverrideError(f"mode 只支持：{' / '.join(VALID_MODES)}。")
    return normalized


def read_overrides(path: Path | None = None) -> dict[str, Any]:
    """读覆盖层；文件不存在或只有注释/空白时返回空字典。

    顶层必须是映射：调用方直接 ``["brain"]["mode"]`` 取值，标量或列表
    在这里就报错，比让它在下游抛 TypeError 好解释。
    """
    target = _resolve(path)
    try:
        text = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # 只取结构化位置信息：YAMLError 的 str() 会带出错行源码，密钥可能被回显。
        raise OverrideError(_parse_error_message(target, exc)) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise OverrideError("覆盖层顶层必须是映射（key: value），不能是标量或列表。")
    return loaded


def write_overrides(mapping: dict[str, Any], path: Path | None = None) -> Path:
    """整份重写覆盖层，返回最终路径。

    先写同目录临时文件再 ``os.replace``：中途断电/关窗只会留下一个临时
    文件，目标文件要么是旧内容，要么是完整的新内容，不会出现半份 YAML。
    """
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(mapping, allow_unicode=True, sort_keys=False)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".settings.local-", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    return target.resolve()


def _resolve(path: Path | None) -> Path:
    return DEFAULT_PATH if path is None else Path(path)


def _parse_error_message(target: Path, exc: yaml.YAMLError) -> str:
    """拼一条能定位到行列、但不含源码的解析错误。

    PyYAML 的 ``str(exc)`` 会带上出错行及其上下文，覆盖层里可能是密钥；
    这里只用 problem（描述）与 problem_mark（行列）。
    """
    detail = getattr(exc, "problem", None) or "无法解析"
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return f"覆盖层 YAML 解析失败：{target.resolve()}（{detail}）。"
    return (
        f"覆盖层 YAML 解析失败：{target.resolve()} "
        f"第 {mark.line + 1} 行第 {mark.column + 1} 列：{detail}。"
    )
