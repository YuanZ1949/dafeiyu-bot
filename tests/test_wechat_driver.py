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


class FakeNavigator:
    def open_dialog_window(self, friend: str) -> dict:
        if friend == "查无此人":
            raise NotFriendError("no such friend")
        return {"title": friend}


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
    monkeypatch.setitem(sys.modules, "pyweixin", module)
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


def test_at_members_dedupes_in_order(fake) -> None:
    assert driver.at_members(ChatHandle(name="测试群", window=None)) == ["阿猫", "阿狗"]


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
