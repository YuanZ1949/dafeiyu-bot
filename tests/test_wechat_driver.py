"""驱动门面契约：假 pyweixin 返回文档化形状，异常统一翻译成 DriverError。

这里不装真的 pyweixin（PyPI 上没有可安装的 `pyweixin`，桌面依赖走
`pywechat127`），所以用 `sys.modules` 注入假模块；驱动只依赖函数名与返回结构，
不依赖真实微信。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from dafeiyu.gateway.wechat_bridge import driver
from dafeiyu.gateway.wechat_bridge.driver import ChatHandle, DriverError


class NotFriendError(Exception):
    """模拟 pyweixin 的「查无此人」异常。"""


class FakeTools:
    def __init__(self) -> None:
        self.running = True
        self.about_value: object = {
            "微信版本": "4.1.6.14",
            "微信昵称": "小助手",
            "平台": "windows",
        }
        self.language_value = "简体中文"
        self.version_value: object = None

    def about_weixin(self) -> object:
        return self.about_value

    def get_weixin_version(self) -> object:
        return self.version_value

    def get_weixin_language(self) -> str:
        return self.language_value

    def is_weixin_running(self) -> bool:
        return self.running


class FakeMessages:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        # pyweixin 的 pull_messages 是「新 → 旧」：先选中最新一条，再逐条按 'up'
        # 往上翻（utils.traverse_message）。snapshot() 负责翻回时间正序。
        self.pulled: dict[str, object] = {
            "测试群": [
                {"发送人": "阿猫", "内容": "你好", "引用": "上一条"},
                "裸字符串条目",
            ]
        }
        self.new_messages: object = {
            "测试群": [{"昵称": "阿猫", "文本内容": "你好", "类型": "text"}]
        }
        self.pulled_numbers: list[int] = []

    def check_new_messages(self) -> object:
        return self.new_messages

    def pull_messages(self, friend: str, number: int) -> object:
        self.pulled_numbers.append(number)
        return self.pulled.get(friend, [])

    def send_messages_to_friend(
        self, friend: str, messages: list[str], close_weixin: bool | None = None
    ) -> None:
        self.sent.append(
            {"friend": friend, "messages": messages, "close_weixin": close_weixin}
        )


class FakeWindow:
    """模拟 pywinauto WindowSpecification 里驱动会用到的最小面。"""

    def __init__(self, class_name: str = "mmui::MainWindow") -> None:
        self._class_name = class_name
        self.alive = True

    def exists(self, timeout: float | None = None) -> bool:
        return self.alive

    def class_name(self) -> str:
        if not self.alive:
            raise RuntimeError("window gone")
        return self._class_name


class FakeNavigator:
    def __init__(self) -> None:
        self.window = FakeWindow()
        self.open_weixin_calls = 0

    def open_weixin(self, is_maximize: bool | None = None, window_size=None):
        # 真实实现会 restore + 置顶 + 居中，这是轮询时窗口乱跳的来源。
        self.open_weixin_calls += 1
        return self.window

    def open_dialog_window(self, friend: str) -> dict:
        # pyweixin 的 open_dialog_window 内部会先解析主窗口。
        self.open_weixin()
        if friend == "查无此人":
            raise NotFriendError("no such friend")
        return {"title": friend}


class FakeWeChatAuto:
    """`pyweixin.WeChatAuto` 里的模块级函数（未读探测用）。"""

    def __init__(self) -> None:
        self.unread_total_value: object = 0
        self.unread_sessions_value: object = {}
        self.window_seen: object = None

    def get_new_message_num(self, main_window=None, close_weixin: bool | None = None):
        self.window_seen = main_window
        return self.unread_total_value

    def scan_for_new_messages(
        self,
        main_window=None,
        delay: float = 0.1,
        close_weixin: bool | None = None,
    ):
        self.window_seen = main_window
        return self.unread_sessions_value


class FakeContacts:
    def get_groupMembers_info(self, group_name: str) -> list[dict]:
        assert group_name == "测试群"
        return [{"群昵称": "阿猫"}, {"群昵称": "阿猫"}, {"群昵称": "阿狗"}]


class FakeGlobalConfig:
    def __init__(self) -> None:
        # 故意先置为 True：驱动必须显式翻回 False，不能赌默认值。
        self.close_weixin = True
        self.is_maximize = True


@pytest.fixture
def fake(monkeypatch) -> types.ModuleType:
    module = types.ModuleType("pyweixin")
    module.Tools = FakeTools()
    module.Messages = FakeMessages()
    module.Navigator = FakeNavigator()
    module.Contacts = FakeContacts()
    module.GlobalConfig = FakeGlobalConfig()
    module.WeChatAuto = FakeWeChatAuto()
    monkeypatch.setitem(sys.modules, "pyweixin", module)
    # 主窗口缓存是模块级全局：每个用例从「未缓存」开始，避免互相串味。
    monkeypatch.setattr(driver, "_main_window_cache", None)
    monkeypatch.setattr(driver, "_last_synthetic_input_at", None)
    return module


def test_about_normalises_chinese_keys(fake) -> None:
    assert driver.about() == {
        "version": "4.1.6.14",
        "nickname": "小助手",
        "platform": "windows",
    }


def test_about_prefers_dedicated_version_api(fake) -> None:
    fake.Tools.version_value = "4.1.15.13"

    assert driver.about()["version"] == "4.1.15.13"


def test_about_reads_bare_chinese_version_key(fake) -> None:
    fake.Tools.about_value = {"版本": "4.1.15.13", "语言": "简体中文"}

    assert driver.about()["version"] == "4.1.15.13"


def test_about_tolerates_non_dict_response(fake) -> None:
    fake.Tools.about_value = "4.1.6"

    assert driver.about() == {
        "version": "4.1.6",
        "nickname": None,
        "platform": None,
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("简体中文", "zh-CN"),
        ("zh_CN", "zh-CN"),
        ("zh-cn", "zh-CN"),
        ("繁體中文", "zh-TW"),
        ("English", "en-US"),
        ("fr-FR", "fr-FR"),
    ],
)
def test_language_is_normalised(fake, raw: str, expected: str) -> None:
    fake.Tools.language_value = raw

    assert driver.language() == expected


def test_language_unknown_is_loud(fake) -> None:
    fake.Tools.language_value = ""

    with pytest.raises(DriverError) as excinfo:
        driver.language()
    assert excinfo.value.code == "language_unknown"


def test_is_running_reflects_driver(fake) -> None:
    assert driver.is_running() is True

    fake.Tools.running = False
    assert driver.is_running() is False


def test_is_running_is_false_without_pyweixin(monkeypatch) -> None:
    # sys.modules 里放 None：任何 import pyweixin 都会立刻 ImportError。
    monkeypatch.setitem(sys.modules, "pyweixin", None)

    assert driver.is_running() is False


def test_list_sessions_normalises_entries(fake) -> None:
    assert driver.list_sessions() == {
        "测试群": [
            {
                "nickname": "阿猫",
                "text": "你好",
                "kind": "text",
                "reply_to": "",
                "reply_author": "",
                "ts": 0,
            }
        ]
    }

    fake.Messages.new_messages = {}
    assert driver.list_sessions() == {}


def test_open_chat_returns_handle(fake) -> None:
    handle = driver.open_chat("测试群")

    assert isinstance(handle, ChatHandle)
    assert handle.name == "测试群"
    assert handle.window == {"title": "测试群"}


def test_open_chat_translates_not_friend(fake) -> None:
    with pytest.raises(DriverError) as excinfo:
        driver.open_chat("查无此人")

    error = excinfo.value
    assert error.code == "not_friend"
    assert isinstance(error.__cause__, NotFriendError)
    assert not isinstance(error, NotFriendError)


def test_snapshot_pulls_and_normalises(fake) -> None:
    result = driver.snapshot(ChatHandle(name="测试群", window=None))

    assert result["chat_name"] == "测试群"
    assert result["counts"] == {"total": 2}
    assert fake.Messages.pulled_numbers == [driver.PULL_MESSAGES]
    # 驱动给的是「新 → 旧」，snapshot 必须翻成时间正序（旧 → 新）。
    # 裸字符串条目：不猜发送人，只保留文本。
    assert result["messages"][0] == {
        "nickname": "",
        "text": "裸字符串条目",
        "kind": "text",
        "reply_to": "",
        "reply_author": "",
        "ts": 0,
    }
    assert result["messages"][1] == {
        "nickname": "阿猫",
        "text": "你好",
        "kind": "text",
        "reply_to": "上一条",
        "reply_author": "",
        "ts": 0,
    }


def test_snapshot_returns_chronological_order(fake) -> None:
    """回归：顺序反了会让 align 每轮 overlap=0，把每条新消息都当 resync 丢掉。"""
    fake.Messages.pulled["测试群"] = [
        {"发送人": "阿猫", "内容": "第三条"},
        {"发送人": "阿猫", "内容": "第二条"},
        {"发送人": "阿猫", "内容": "第一条"},
    ]

    result = driver.snapshot(ChatHandle(name="测试群", window=None))

    assert [m["text"] for m in result["messages"]] == ["第一条", "第二条", "第三条"]


def test_snapshot_marks_self_injected_input(fake, monkeypatch) -> None:
    """`pull_messages` 会按 'up' 翻消息，要留下「这是我们自己按的」时间戳。"""
    times = iter([100.0, 100.25])
    monkeypatch.setattr(driver, "_wall_clock", lambda: next(times))

    driver.snapshot(ChatHandle(name="测试群", window=None))

    assert driver.last_synthetic_input_age() == pytest.approx(0.25)


def test_snapshot_reads_installed_chinese_sender_key(fake) -> None:
    fake.Messages.pulled["测试群"] = [
        {"消息发送人": "小菜鱼", "消息内容": "hi", "消息类型": "text"}
    ]

    result = driver.snapshot(ChatHandle(name="测试群", window=None))

    assert result["messages"][0]["nickname"] == "小菜鱼"
    assert result["messages"][0]["text"] == "hi"


def test_snapshot_requires_handle_name(fake) -> None:
    with pytest.raises(DriverError) as excinfo:
        driver.snapshot(object())  # type: ignore[arg-type]

    assert excinfo.value.code == "invalid_argument"


def test_send_text_forces_global_config_and_applies_delay(
    fake, monkeypatch
) -> None:
    class RecordingTime:
        def __init__(self) -> None:
            self.sleeps: list[float] = []

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)

    clock = RecordingTime()
    monkeypatch.setattr(driver, "time", clock)

    driver.send_text("测试群", "在吗", 0.8)

    assert clock.sleeps == [0.8]
    assert fake.Messages.sent == [
        {"friend": "测试群", "messages": ["在吗"], "close_weixin": False}
    ]
    assert fake.GlobalConfig.close_weixin is False
    assert fake.GlobalConfig.is_maximize is False


def test_send_text_skips_zero_delay(fake, monkeypatch) -> None:
    class RecordingTime:
        def __init__(self) -> None:
            self.sleeps: list[float] = []

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)

    clock = RecordingTime()
    monkeypatch.setattr(driver, "time", clock)

    driver.send_text("测试群", "在吗", 0)

    assert clock.sleeps == []
    assert fake.Messages.sent[0]["messages"] == ["在吗"]


class _FakeChild:
    def __init__(self, present: bool) -> None:
        self.present = present

    def exists(self, timeout: float | None = None) -> bool:
        return self.present


class FakeChatWindow:
    """微信主窗口的「多选」状态替身：多选时输入框消失、工具栏出现。"""

    def __init__(self) -> None:
        self.multiselect = False
        self.keys: list[str] = []

    def exists(self, timeout: float | None = None) -> bool:
        return True

    def class_name(self) -> str:
        return "mmui::MainWindow"

    def set_focus(self) -> None:  # pragma: no cover - 只是记录调用
        return None

    def type_keys(self, keys: str) -> None:
        self.keys.append(keys)
        if keys == "{ESC}":
            self.multiselect = False

    def child_window(self, **kwargs):
        present = not self.multiselect and kwargs.get("auto_id") == "chat_input_field"
        return _FakeChild(present)

    def descendants(self, **kwargs):
        if (
            self.multiselect
            and kwargs.get("class_name") == driver._MULTISELECT_TOOLBAR_CLASS
        ):
            return [object()]
        return []


def test_snapshot_exits_stuck_multiselect(fake, monkeypatch) -> None:
    """回归：pyweixin 的 ESC 可能被前台窗口抢走，多选留着会让后续发送全失败。"""
    chat_window = FakeChatWindow()
    chat_window.multiselect = True
    fake.Navigator.window = chat_window

    driver.snapshot(ChatHandle(name="测试群", window=None))

    assert chat_window.keys == ["{ESC}"]
    assert chat_window.multiselect is False


def test_send_text_retries_once_after_esc_multiselect(fake, monkeypatch) -> None:
    """输入框被多选顶掉时 pyweixin 在键入前就抛 NotFriendError：退出多选后可重试。"""
    chat_window = FakeChatWindow()
    fake.Navigator.window = chat_window
    attempts: list[list[str]] = []

    def send_messages_to_friend(
        friend: str, messages: list[str], close_weixin: bool | None = None
    ) -> None:
        attempts.append(list(messages))
        if len(attempts) == 1:
            chat_window.multiselect = True
            raise NotFriendError("非正常好友,无法发送消息")
        fake.Messages.sent.append(
            {"friend": friend, "messages": list(messages), "close_weixin": close_weixin}
        )

    fake.Messages.send_messages_to_friend = send_messages_to_friend

    driver.send_text("测试群", "在吗", 0)

    assert chat_window.keys == ["{ESC}"]
    assert len(attempts) == 2
    # 只真正发出一条：第一次在键入前就失败了。
    assert fake.Messages.sent == [
        {"friend": "测试群", "messages": ["在吗"], "close_weixin": False}
    ]


def test_send_text_does_not_retry_not_friend_without_multiselect(
    fake, monkeypatch
) -> None:
    chat_window = FakeChatWindow()
    fake.Navigator.window = chat_window
    attempts: list[str] = []

    def send_messages_to_friend(friend: str, messages: list[str], **kwargs) -> None:
        attempts.append(friend)
        raise NotFriendError("非正常好友,无法发送消息")

    fake.Messages.send_messages_to_friend = send_messages_to_friend

    with pytest.raises(DriverError) as excinfo:
        driver.send_text("测试群", "在吗", 0)

    assert excinfo.value.code == "not_friend"
    assert len(attempts) == 1
    # 不是多选造成的：不要乱按 ESC（正常界面下 ESC 会把微信收进托盘）。
    assert chat_window.keys == []


def test_at_members_dedupes_in_order(fake) -> None:
    assert driver.at_members(ChatHandle(name="测试群", window=None)) == ["阿猫", "阿狗"]


def test_unread_total_reads_sidebar_badge(fake) -> None:
    fake.WeChatAuto.unread_total_value = "7"

    assert driver.unread_total() == 7
    assert fake.WeChatAuto.window_seen is fake.Navigator.window


def test_unread_total_tolerates_unparsable_badge(fake) -> None:
    fake.WeChatAuto.unread_total_value = None

    assert driver.unread_total() == 0


def test_unread_sessions_returns_counts(fake) -> None:
    fake.WeChatAuto.unread_sessions_value = {"测试群": 3, "坏值": "x", "另一个群": "2"}

    assert driver.unread_sessions() == {"测试群": 3, "另一个群": 2}
    assert fake.WeChatAuto.window_seen is fake.Navigator.window


def test_unread_sessions_scalar_response_is_loud(fake) -> None:
    fake.WeChatAuto.unread_sessions_value = 3

    with pytest.raises(DriverError) as excinfo:
        driver.unread_sessions()
    assert excinfo.value.code == "bad_response"


def test_main_window_is_cached_between_calls(fake) -> None:
    first = driver.unread_total()
    second = driver.unread_sessions()

    assert first == 0 and second == {}
    # 只解析一次主窗口：第二次走缓存，不再触发 restore/置顶/居中。
    assert fake.Navigator.open_weixin_calls == 1


def test_main_window_is_re_resolved_when_window_dies(fake) -> None:
    driver.unread_total()
    fake.Navigator.window.alive = False
    fake.Navigator.window = FakeWindow()

    driver.unread_total()

    assert fake.Navigator.open_weixin_calls == 2


def test_open_chat_does_not_reopen_main_window(fake) -> None:
    """回归：每次 open_chat 都会 restore + 置顶 + 居中，轮询时窗口反复乱跳。"""
    driver.open_chat("测试群")
    driver.open_chat("测试群")

    assert fake.Navigator.open_weixin_calls == 1


def test_open_chat_restores_open_weixin_after_use(fake) -> None:
    driver.open_chat("测试群")
    before = fake.Navigator.open_weixin_calls

    fake.Navigator.open_weixin()

    assert fake.Navigator.open_weixin_calls == before + 1


def test_snapshot_and_send_also_reuse_main_window(fake) -> None:
    driver.snapshot(ChatHandle(name="测试群", window=None))
    driver.send_text("测试群", "在吗", 0)

    assert fake.Navigator.open_weixin_calls == 1


def test_no_window_jump_falls_back_when_window_unavailable(fake, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise driver.DriverError("not_logged_in", "微信停在登录窗口")

    monkeypatch.setattr(driver, "_main_window", boom)

    # 拿不到主窗口时不能把轮询整个搞挂：按原行为继续打开会话。
    assert driver.open_chat("测试群").name == "测试群"


def test_driver_functions_raise_pyweixin_missing(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "pyweixin", None)

    with pytest.raises(DriverError) as excinfo:
        driver.about()
    assert excinfo.value.code == "pyweixin_missing"

    with pytest.raises(DriverError) as excinfo:
        driver.send_text("测试群", "在吗", 0)
    assert excinfo.value.code == "pyweixin_missing"


def test_package_imports_without_pyweixin() -> None:
    """惰性 import 的端到端证据：在 meta_path 上封杀 pyweixin，包仍然可导入。"""
    src = Path(driver.__file__).resolve().parents[3]
    env = dict(os.environ, PYTHONPATH=str(src))
    code = (
        "import importlib.abc, sys\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'pyweixin' or name.startswith('pyweixin.'):\n"
        "            raise ImportError('blocked for laziness test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "import dafeiyu.gateway.wechat_bridge as pkg\n"
        "import dafeiyu.gateway.wechat_bridge.driver as d\n"
        "print('ok', d.__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "ok dafeiyu.gateway.wechat_bridge.driver" in result.stdout


_PYWEIXIN_IMPORT_FORMS = (
    re.compile(r"^\s*(?:import|from)\s+pyweixin\b", re.MULTILINE),
    re.compile(r"importlib\.import_module\(\s*[\"']pyweixin[\"']"),
    re.compile(r"__import__\(\s*[\"']pyweixin[\"']"),
)


def test_only_driver_module_mentions_pyweixin() -> None:
    repo = Path(__file__).resolve().parents[1]
    this_file = Path(__file__).resolve()
    offenders = []

    for root in (repo / "src" / "dafeiyu", repo / "tests"):
        for path in sorted(root.rglob("*.py")):
            if path.resolve() in {this_file, Path(driver.__file__).resolve()}:
                continue
            text = path.read_text(encoding="utf-8")
            if any(form.search(text) for form in _PYWEIXIN_IMPORT_FORMS):
                offenders.append(str(path.relative_to(repo)))

    assert offenders == []
