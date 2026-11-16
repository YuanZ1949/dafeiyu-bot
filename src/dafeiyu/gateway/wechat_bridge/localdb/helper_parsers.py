"""helper 解析桥（可选）：转发 wxbot_framework 的 XML 解析并只输出脱敏结果。

只有 Windows 且 helper 目录同时存在 ``native/text_service.py`` 与
``wxbot_framework/friend_requests.py`` 时才会改动 ``sys.path`` 并导入固定
的四个模块；其他情况一律视为不可用。任何解析失败都返回空值并只记一条
日志（模块级闩锁），绝不把原始 XML、验证凭据或异常正文写进日志，也不向
轮询方抛错。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_HELPER_ROOT = r"C:\MyFile\AICoding\helper"

# 允许导入的 helper 模块白名单；除此之外不做任何动态导入。
_HELPER_MODULES = (
    "wxbot_framework.friend_requests",
    "wxbot_framework.group_events",
    "wxbot_framework.recall",
    "wxbot_framework.native_profiles",
)

# _modules 为 None 表示不可用或尚未探测成功；_probe_done 记录探测已结束，
# 避免每次调用都重复检查目录和导入。
_modules: dict[str, Any] | None = None
_probe_done = False
_failure_logged = False


def _helper_root() -> Path:
    return Path(os.environ.get("WECHAT_HELPER_ROOT") or DEFAULT_HELPER_ROOT)


def _note_failure(message: str, *args: object) -> None:
    """失败只记一次；后续同类失败静默，避免轮询刷屏。"""
    global _failure_logged
    if _failure_logged:
        return
    _failure_logged = True
    log.info(message, *args)


def _bootstrap(root: Path) -> bool:
    """复制 wechat_hook_server 的标记检查：两个标记都在才动 sys.path。"""
    if not (root / "native" / "text_service.py").is_file():
        return False
    if not (root / "wxbot_framework" / "friend_requests.py").is_file():
        return False
    for path in (root / "native", root):
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return True


def _ensure_loaded() -> dict[str, Any] | None:
    """探测并导入 helper；成功返回模块表，失败返回 None 且只记一条日志。"""
    global _modules, _probe_done
    if _probe_done:
        return _modules
    if sys.platform != "win32":
        _note_failure("非 Windows 平台，不加载 helper 解析桥")
        _probe_done = True
        return None
    root = _helper_root()
    if not _bootstrap(root):
        _note_failure("helper 目录缺少标记文件，解析桥不可用：%s", root)
        _probe_done = True
        return None
    modules: dict[str, Any] = {}
    try:
        for name in _HELPER_MODULES:
            __import__(name)
            modules[name.rpartition(".")[2]] = sys.modules[name]
    except Exception as exc:  # noqa: BLE001 - 解析桥可用性探测必须吞掉一切导入错误
        _note_failure("helper 模块导入失败：%s", type(exc).__name__)
        _probe_done = True
        return None
    _modules = modules
    _probe_done = True
    return modules


def _reset_bridge() -> None:
    """仅供测试：清空探测结果与日志闩锁，并卸载已导入的 helper 模块。"""
    global _modules, _probe_done, _failure_logged
    _modules = None
    _probe_done = False
    _failure_logged = False
    for name in [key for key in sys.modules if key.startswith("wxbot_framework")]:
        del sys.modules[name]


def helper_available() -> bool:
    return _ensure_loaded() is not None


def supported_versions() -> tuple[str, ...]:
    """helper 已核对的微信版本号；不可用时返回空元组。"""
    modules = _ensure_loaded()
    if modules is None:
        return ()
    return tuple(str(profile.version)
                 for profile in modules["native_profiles"].CORE_PROFILES)


def parse_friend_request(content: str) -> dict[str, str | int] | None:
    """解析 type=37 好友申请；只返回 sender_id/nickname/verification/scene/opcode。

    encryptusername、ticket 与原 XML 属于临时验证凭据，一律不进入结果。
    """
    modules = _ensure_loaded()
    if modules is None:
        return None
    try:
        request = modules["friend_requests"].parse_friend_request(content)
    except Exception as exc:  # noqa: BLE001 - 轮询路径不得抛错
        _note_failure("好友申请解析失败：%s", type(exc).__name__)
        return None
    return {"sender_id": request.sender_id, "nickname": request.nickname,
            "verification": request.verification, "scene": request.scene,
            "opcode": request.opcode}


def parse_group_system_message(content: str, msg_type: int, room_id: str,
                               self_id: str = "") -> tuple[dict[str, str], ...]:
    """解析原生 10000/10002 群系统消息，返回成员进出事件的字典元组。"""
    modules = _ensure_loaded()
    if modules is None:
        return ()
    try:
        events = modules["group_events"].parse_group_system_message(
            content, msg_type=msg_type, room_id=room_id, self_id=self_id)
    except Exception as exc:  # noqa: BLE001 - 轮询路径不得抛错
        _note_failure("群系统消息解析失败：%s", type(exc).__name__)
        return ()
    return tuple({"kind": event.kind, "room_id": event.room_id,
                  "member_id": event.member_id, "nickname": event.nickname,
                  "source": event.source} for event in events)


def parse_recall(content: str, chat_id: str) -> dict[str, str] | None:
    """解析撤回通知；返回 message_id/chat_id/notice，不匹配时为 None。"""
    modules = _ensure_loaded()
    if modules is None:
        return None
    try:
        return modules["recall"].parse_recall(content, chat_id=chat_id)
    except Exception as exc:  # noqa: BLE001 - 轮询路径不得抛错
        _note_failure("撤回通知解析失败：%s", type(exc).__name__)
        return None
