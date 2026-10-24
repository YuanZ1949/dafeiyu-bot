import pytest

from dafeiyu.gateway.wechat import WeChatAdapter
from dafeiyu.models import ChatType


async def test_send_raises_not_implemented() -> None:
    adapter = WeChatAdapter()
    with pytest.raises(NotImplementedError, match="微信接入未实现"):
        await adapter.send(ChatType.GROUP, 1, "在吗")


async def test_start_raises_not_implemented() -> None:
    adapter = WeChatAdapter()
    with pytest.raises(NotImplementedError, match="微信接入未实现"):
        await adapter.start()


async def test_handler_can_be_assigned() -> None:
    adapter = WeChatAdapter()

    async def handler(message):  # pragma: no cover - 只验证可赋值
        return None

    adapter.on_message = handler
    assert adapter.on_message is handler
