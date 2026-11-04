"""Windows 下隐藏子进程的控制台窗口。

supervisor / bot 现在由 ``pythonw.exe`` 启动（没有控制台）。此时任何 console
子系统的子进程（``icacls`` / ``netstat`` / ``taskkill`` / ``powershell`` /
``cmd``）都会**新建一个控制台窗口**，桌面上就是一闪而过的黑框；由计划任务
直接拉起的 ``cmd /c ...`` 更糟，窗口会一直挂到进程结束。

所有"只是跑个命令、不给人看"的 ``subprocess`` 调用都从这里取 creationflags。
"""

from __future__ import annotations

import os
import subprocess
from typing import TypedDict

__all__ = ["CREATE_NO_WINDOW", "HiddenKwargs", "hidden_kwargs"]

#: Windows 专有标志；非 Windows 上为 0（等价于不传）。
CREATE_NO_WINDOW: int = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
)


class HiddenKwargs(TypedDict, total=False):
    """``subprocess`` 的隐藏窗口参数。

    用 TypedDict 而不是 ``dict[str, int]``，这样 ``**hidden_kwargs()`` 展开到
    ``subprocess.run(..., creationflags=...)`` 时类型检查器能把键映射到正确的
    参数，而不是把 ``int`` 硬塞给每个参数。
    """

    creationflags: int


def hidden_kwargs() -> HiddenKwargs:
    """``subprocess.run`` / ``Popen`` 用的关键字参数；非 Windows 返回空 dict。"""
    if CREATE_NO_WINDOW:
        return {"creationflags": CREATE_NO_WINDOW}
    return {}
