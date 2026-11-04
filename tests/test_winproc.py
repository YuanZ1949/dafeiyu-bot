"""``dafeiyu.winproc``：pythonw 下隐藏子进程控制台窗口的开关。"""

from __future__ import annotations

import os
import subprocess

from dafeiyu.winproc import CREATE_NO_WINDOW, hidden_kwargs


def test_create_no_window_is_int() -> None:
    assert isinstance(CREATE_NO_WINDOW, int)


def test_hidden_kwargs_matches_platform() -> None:
    kwargs = hidden_kwargs()
    if os.name == "nt":
        assert kwargs == {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    else:
        assert kwargs == {}
