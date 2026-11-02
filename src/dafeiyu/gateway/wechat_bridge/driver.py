"""pyweixin 的唯一入口：8 个函数，全部惰性 import。

安装的发行包是 `pywechat127`，import 名仍然是 `pyweixin`（`pip install pywechat127`
之后 `import pyweixin` 才可用）。本模块之外**禁止** `import pyweixin`，这样：

- 没装桌面依赖的机器照样能 import 本模块（包括本文件）；
- `python -m dafeiyu.gateway.wechat_bridge --help` 在未安装依赖时也能退出 0；
- 换驱动（升 pyweixin、或换成别的 UIA 封装）只需要改这一个文件。

实测/文档化的 API 面（来自 pyweixin README 与 `pyweixin/WeChatAuto.py`；安装后以
实际版本的签名为准，若有出入改这里并更新本文档）：

- ``Tools.about_weixin()`` -> 版本/昵称等信息的 dict（中文键）
- ``Tools.get_weixin_version()`` -> 版本字符串（优先用它；旧版没有时回退 about）
- ``Messages.send_messages_to_friend(friend=显示名, messages=[文本], close_weixin=False)``
  —— 按**显示名**寻址，没有返回值；`messages` 必须是 **list**，传字符串会逐字符发送
- ``Messages.pull_messages(friend=显示名, number=N)``（实测 1.9.8 要求 `number`）、
  ``Messages.check_new_messages()``；`pull_messages` 内部走
  ``utils.traverse_message``——**从最新一条开始、逐条按 'up' 往上翻**，所以原始返回
  顺序是「新 → 旧」，本模块在 ``snapshot`` 里翻成时间正序（旧 → 新）再交给上层
- ``Monitor.listen_on_chat(dialog_window, duration, groupMembers)`` 返回 7 个计数键
  （新消息总数/文本数量/文件数量/图片数量/视频数量/链接数量/文本内容），
  **没有「消息发送人」键**，发送人只能从条目字段里取
- ``Navigator.open_dialog_window(friend=显示名)``、``Navigator.open_seperate_dialog_window``
- ``Contacts.get_groupMembers_info(...)``
- ``GlobalConfig.close_weixin`` / ``GlobalConfig.is_maximize``

本层只做三件事：调用、把返回结构规整成稳定形状、把异常统一翻译成
``DriverError(code)``。**不做任何业务判断**（去重、身份、节流都在上层），也不
在这里猜「同名群是不是同一家」。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ChatHandle",
    "DriverError",
    "PULL_MESSAGES",
    "about",
    "at_members",
    "is_running",
    "language",
    "last_synthetic_input_age",
    "list_sessions",
    "open_chat",
    "send_text",
    "snapshot",
]

#: 每轮快照拉取的最近消息条数。太小会漏消息，太大每 8 秒滚动 UI 代价高。
PULL_MESSAGES = 30

#: 捕获一次时钟函数：测试替换 ``driver.time`` 时不影响这里，也避免和
#: ``send_text`` 的 ``time.sleep`` 打桩互相牵连。
_wall_clock = time.time

#: 桥自己最近一次注入键鼠事件的时间戳（wall clock）。
#: pyautogui 的按键/点击走 SendInput，同样会刷新 Windows 的空闲计时，所以
#: ``sender`` 的前台保护必须能把「自己刚按的」和「人刚按的」区分开——否则轮询
#: 刚翻完消息，紧接着的回复就会被自己按的键拦成 ``foreground_busy``。
_last_synthetic_input_at: float | None = None


def _mark_synthetic_input() -> None:
    global _last_synthetic_input_at
    _last_synthetic_input_at = _wall_clock()


def last_synthetic_input_age() -> float | None:
    """桥自己上一次注入键鼠事件距今多少秒；从未注入过返回 None。"""
    mark = _last_synthetic_input_at
    if mark is None:
        return None
    return max(0.0, _wall_clock() - mark)


class DriverError(Exception):
    """驱动层统一异常：调用方只需要看 `code`，不需要认识 pyweixin 的异常类型。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        # 只截前 200 字：异常文本可能夹带聊天内容，日志里不需要全文。
        self.detail = detail[:200]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


@dataclass(frozen=True)
class ChatHandle:
    """`open_chat()` 的返回值：会话名 + 底层窗口对象。

    上层需要判定「前台窗口是不是目标会话」时会用到 `window`；`snapshot()` 只需要
    `name`。把两者绑在一起，避免调用方自己拼 (name, window) 二元组。
    """

    name: str
    window: Any


# 异常类名 -> 稳定错误码。类名匹配比 isinstance 稳：不同 pyweixin 版本里这些
# 异常可能不在同一个模块路径下，但类名基本不变。
_ERROR_CODES: dict[str, str] = {
    "NotFriendError": "not_friend",
    "NotFriend": "not_friend",
    "WeChatNotFoundError": "wechat_not_found",
    "WeChatNotRunningError": "wechat_not_running",
    "WeChatVersionError": "unsupported_version",
    "WeChatWindowError": "window_unavailable",
    "TimeoutError": "timeout",
    "Timeout": "timeout",
    "PermissionError": "permission",
    "FileNotFoundError": "not_found",
}


def _translate(exc: BaseException, default: str = "driver_error") -> DriverError:
    code = _ERROR_CODES.get(type(exc).__name__, default)
    return DriverError(code, f"{type(exc).__name__}: {exc}")


def _call(fn: Callable[[], Any], default: str = "driver_error") -> Any:
    """执行一次 pyweixin 调用，把任意异常翻译成 `DriverError`。"""
    try:
        return fn()
    except DriverError:
        raise
    except BaseException as exc:
        raise _translate(exc, default) from exc


def _pyweixin() -> Any:
    """惰性 import：只有真正调用驱动函数时才需要桌面依赖。"""
    try:
        import pyweixin
    except ImportError as exc:
        raise DriverError(
            "pyweixin_missing",
            "未安装 pd 依赖：pip install 'dafeiyu[wechat]'",
        ) from exc
    return pyweixin


def _pyweixin_or_none() -> Any | None:
    try:
        return _pyweixin()
    except DriverError as exc:
        if exc.code == "pyweixin_missing":
            return None
        raise


def _attr(obj: Any, name: str) -> Any | None:
    return getattr(obj, name, None) if obj is not None else None


def _pick(mapping: Any, keys: tuple[str, ...]) -> Any:
    """从可能带中文键的 dict 里按优先级取第一个非空值。"""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping:
            value = mapping[key]
            if value is not None and value != "":
                return value
    return None


_VERSION_KEYS = (
    "version",
    "Version",
    "微信版本",
    "版本",
    "版本号",
    "WeChat Version",
)
_NICKNAME_KEYS = ("nickname", "NickName", "微信昵称", "昵称")
_PLATFORM_KEYS = ("platform", "Platform", "平台")
_LANGUAGE_KEYS = ("language", "Language", "语言", "语言设置")
_TEXT_KEYS = ("text", "content", "内容", "消息内容", "文本内容", "message", "msg")
_SENDER_KEYS = (
    "nickname",
    "sender",
    "from",
    "消息发送人",
    "发送人",
    "昵称",
    "群昵称",
    "备注",
)
_KIND_KEYS = ("kind", "type", "类型", "消息类型")
_REPLY_KEYS = ("reply_to", "quote", "引用", "被引用内容", "引用内容")
_REPLY_AUTHOR_KEYS = ("reply_author", "quote_author", "引用人", "被引用人", "引用者")
_TS_KEYS = ("ts", "time", "timestamp", "时间", "发送时间")


def _about_raw() -> Any:
    return _call(lambda: _pyweixin().Tools.about_weixin(), "about_failed")


def _version_raw() -> str | None:
    """优先用 `Tools.get_weixin_version()`；旧版/替身没有时回退 about_weixin。"""
    tools = _attr(_pyweixin(), "Tools")
    getter = _attr(tools, "get_weixin_version")
    if not callable(getter):
        return None
    value = _call(getter, "about_failed")
    text = str(value).strip() if value is not None else ""
    return text or None


def about() -> dict[str, str | None]:
    """微信版本/昵称/平台。只返回可以安全塞进 health 信封的字符串。"""
    version = _version_raw()
    raw = _about_raw()
    if not isinstance(raw, dict):
        text = str(raw).strip() if raw else ""
        return {"version": version or text or None, "nickname": None, "platform": None}
    def _text(keys: tuple[str, ...]) -> str | None:
        value = _pick(raw, keys)
        return str(value) if value is not None else None

    return {
        "version": version or _text(_VERSION_KEYS),
        "nickname": _text(_NICKNAME_KEYS),
        "platform": _text(_PLATFORM_KEYS),
    }


def _normalize_language(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise DriverError("language_unknown", "读不到微信界面语言")
    folded = text.casefold().replace("_", "-")
    if folded in {"zh-cn", "zh-hans", "简体中文", "cn"}:
        return "zh-CN"
    if folded in {"zh-tw", "zh-hant", "繁體中文", "繁体中文"}:
        return "zh-TW"
    if folded in {"en", "en-us", "english"}:
        return "en-US"
    return text


def language() -> str:
    """微信界面语言，规整成 `zh-CN` / `zh-TW` / `en-US` 形态。

    任务 17 拿它和配置里的 `language` 比对，不匹配要 `degraded=true`；所以读不到时
    必须显式报错，不能默默返回一个默认值把风险抹掉。
    """
    wx = _pyweixin()
    tools = _attr(wx, "Tools")
    getter = _attr(tools, "get_weixin_language")
    if callable(getter):
        raw = _call(getter, "language_failed")
    else:
        # 退路：有些版本只在 about_weixin() 的返回里带语言。
        raw = _pick(_about_raw(), _LANGUAGE_KEYS)
    return _normalize_language(raw)


def is_running() -> bool:
    """微信进程在跑吗。

    未安装 pyweixin 时返回 False（健康探测要把它当成 `no_wechat`，不是崩溃）。
    """
    wx = _pyweixin_or_none()
    if wx is None:
        return False
    tools = _attr(wx, "Tools")
    checker = _attr(tools, "is_weixin_running")
    if callable(checker):
        return bool(_call(checker, "probe_failed"))
    try:
        _about_raw()
    except DriverError:
        return False
    return True


def _normalize_entry(entry: Any) -> dict[str, Any]:
    """把一条消息条目规整成 `{nickname, text, kind, reply_to, reply_author, ts}`。

    桥的数据是从屏幕刮出来再拼的：缺字段就是一条静默丢数据的路径，所以这里
    只把**没有**的东西留成空串，不做假设性填充（尤其不猜发送人）。

    `reply_author` 单独留着：上层只有拿到「引用块作者」才能判定「这是不是回复
    机器人」（task 15）；只有引用原文时一律按非回复处理。
    """
    if isinstance(entry, str):
        return {
            "nickname": "",
            "text": entry,
            "kind": "text",
            "reply_to": "",
            "reply_author": "",
            "ts": 0,
        }
    if not isinstance(entry, dict):
        return {
            "nickname": "",
            "text": str(entry),
            "kind": "text",
            "reply_to": "",
            "reply_author": "",
            "ts": 0,
        }
    sender = _pick(entry, _SENDER_KEYS)
    text = _pick(entry, _TEXT_KEYS)
    kind = _pick(entry, _KIND_KEYS)
    reply = _pick(entry, _REPLY_KEYS)
    reply_author = _pick(entry, _REPLY_AUTHOR_KEYS)
    ts = _pick(entry, _TS_KEYS)
    try:
        timestamp = int(ts) if ts is not None else 0
    except (TypeError, ValueError):
        timestamp = 0
    return {
        "nickname": str(sender) if sender is not None else "",
        "text": str(text) if text is not None else "",
        "kind": str(kind or "text"),
        "reply_to": str(reply) if reply is not None else "",
        "reply_author": str(reply_author) if reply_author is not None else "",
        "ts": timestamp,
    }


def _normalize_entries(entries: Any) -> list[dict[str, Any]]:
    if entries is None:
        return []
    if isinstance(entries, str):
        return [_normalize_entry(entries)]
    try:
        return [_normalize_entry(item) for item in entries]
    except TypeError as exc:
        raise DriverError("bad_response", f"消息条目不是可迭代对象: {entries!r}") from exc


def list_sessions() -> dict[str, list[dict[str, Any]]]:
    """当前有新消息的会话：`{显示名: [规整后的消息条目]}`。

    这是「哪些会话值得轮询」的发现入口；空 dict 表示没有新消息。
    """
    raw = _call(lambda: _pyweixin().Messages.check_new_messages(), "list_failed")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DriverError("bad_response", f"check_new_messages 返回 {type(raw).__name__}")
    return {str(name): _normalize_entries(entries) for name, entries in raw.items()}


def open_chat(name: str) -> ChatHandle:
    """按显示名打开会话窗口，返回句柄；找不到人/群时 `code="not_friend"`。"""
    if not str(name or "").strip():
        raise DriverError("invalid_argument", "会话显示名为空")
    wx = _pyweixin()
    try:
        window = _call(
            lambda: wx.Navigator.open_dialog_window(friend=name),
            "open_chat_failed",
        )
    finally:
        # 搜索/点击会话都会注入键鼠事件，记一笔供前台保护区分来源。
        _mark_synthetic_input()
    if window is None:
        raise DriverError("chat_not_found", f"打不开会话：{name}")
    return ChatHandle(name=str(name), window=window)


def snapshot(handle: ChatHandle) -> dict[str, Any]:
    """拉取目标会话最近的消息（供 align 层做最长后缀对齐）。

    返回 `{"chat_name", "messages", "counts": {"total"}}`；`messages` 是规整后的
    `{nickname, text, kind, reply_to}` 列表，**按时间正序（旧 → 新）**。哪个字段缺
    就留空，绝不猜。

    `pull_messages` 底层是「选中最新一条、再按 'up' 往上翻」，原始顺序是
    **新 → 旧**；这里翻转一次，因为 `align.SessionAligner` 的最长后缀=前缀匹配和
    `sender._bubble_present` 的「从新往旧找」都假定列表是时间正序。
    """
    name = getattr(handle, "name", "")
    if not name:
        raise DriverError("invalid_argument", "snapshot 需要一个带 name 的 ChatHandle")
    wx = _pyweixin()
    try:
        raw = _call(
            lambda: wx.Messages.pull_messages(friend=name, number=PULL_MESSAGES),
            "snapshot_failed",
        )
    finally:
        # `pull_messages` 会按最多 30 次 'up' 翻消息，这些按键会刷新系统空闲计时。
        _mark_synthetic_input()
    # pyweixin 返回「新 → 旧」，翻成时间正序；不这么做 align 每次都只能看到
    # overlap=0，于是把每条新消息都当成 resync 静默丢掉。
    messages = list(reversed(_normalize_entries(raw)))
    return {
        "chat_name": str(name),
        "messages": messages,
        "counts": {"total": len(messages)},
    }


def send_text(name: str, text: str, delay: float) -> None:
    """按显示名发一条文本。

    `delay` 是「整段粘贴后停多久再按发送」（秒）。发送前显式把
    `GlobalConfig.close_weixin` / `is_maximize` 置回 False：上游把这两个全局开关
    当成未定义默认值来赌，一旦用户配置被翻转过，微信会在发送后自己关掉/最大化。
    """
    if not str(name or "").strip():
        raise DriverError("invalid_argument", "会话显示名为空")
    if text is None or text == "":
        raise DriverError("invalid_argument", "消息内容为空")
    wx = _pyweixin()
    _force_global_config(wx)
    if delay > 0:
        time.sleep(delay)
    try:
        _call(
            lambda: wx.Messages.send_messages_to_friend(
                friend=name, messages=[text], close_weixin=False
            ),
            "send_failed",
        )
    finally:
        # 粘贴 + Enter 同样是注入输入；后续的发送判断不该被它自己拦住。
        _mark_synthetic_input()


def _force_global_config(wx: Any) -> None:
    config = _attr(wx, "GlobalConfig")
    if config is None:
        raise DriverError("pyweixin_missing", "pyweixin.GlobalConfig 不可用")
    for attr in ("close_weixin", "is_maximize"):
        try:
            setattr(config, attr, False)
        except Exception as exc:
            raise _translate(exc, "config_write_failed") from exc


def force_global_config() -> None:
    """启动时固定 GlobalConfig 默认值（`pyweixin` 的惰性 import 只在 driver 内部）。

    ``pyweixin`` 未安装时静默跳过（`send_text` 前还会再固定一次）；
    `GlobalConfig` 不可用或写入失败时抛 `DriverError`，由调用方记录。
    """
    wx = _pyweixin_or_none()
    if wx is None:
        return
    _force_global_config(wx)


def _call_first_kwarg(fn: Callable[..., Any], names: tuple[str, ...], value: Any) -> Any:
    """不同版本的 `get_groupMembers_info` 形参名不一致，按候选名逐个试。"""
    for name in names:
        try:
            return fn(**{name: value})
        except TypeError:
            continue
    try:
        return fn(value)
    except BaseException as exc:
        raise _translate(exc, "members_failed") from exc


def at_members(handle: ChatHandle) -> list[str]:
    """群成员显示名列表（供 `at_bot` 判定与同名群冲突检查）。

    去重且保持原顺序；读不到时抛 `DriverError`，由上层决定是降级还是拒绝。
    """
    name = getattr(handle, "name", "")
    if not name:
        raise DriverError("invalid_argument", "at_members 需要一个带 name 的 ChatHandle")
    wx = _pyweixin()
    contacts = _attr(wx, "Contacts")
    getter = _attr(contacts, "get_groupMembers_info")
    if not callable(getter):
        raise DriverError("pyweixin_missing", "pyweixin.Contacts.get_groupMembers_info 不可用")
    raw = _call_first_kwarg(getter, ("group_name", "friend", "name", "chat_name"), name)
    members: list[str] = []
    seen: set[str] = set()
    for item in _normalize_entries(raw):
        whoever = item["nickname"] or item["text"]
        if whoever and whoever not in seen:
            seen.add(whoever)
            members.append(whoever)
    return members
