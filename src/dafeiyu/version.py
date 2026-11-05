"""版本号读取：优先安装元数据，取不到时回落到包内常量。"""

from __future__ import annotations

from importlib import metadata

from . import __version__


def get_version() -> str:
    """返回 dafeiyu 版本号；源码直跑（未安装）时用包内常量。"""
    try:
        return metadata.version("dafeiyu")
    except metadata.PackageNotFoundError:
        return __version__
