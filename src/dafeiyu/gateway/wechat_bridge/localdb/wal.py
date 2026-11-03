"""SQLite WAL 只读解析（微信 4.1 实测格式）。

格式要点（与 ``wxwal_check.py`` 的复现完全一致）：

* 头 32 字节 = 8 个**大端** u32：magic、version(3007000)、page_size、
  checkpoint_seq、salt1、salt2、cksum1、cksum2；
* 校验和按 8 字节一组、以**小端** u32 读成 ``(w0, w1)``::

      s1 = (s1 + w0 + s2) & 0xFFFFFFFF
      s2 = (s2 + w1 + s1) & 0xFFFFFFFF

  头部从 ``(0, 0)`` 开始算 ``header[0:24]``；帧从头部校验和继续，依次喂
  ``frame[0:8]`` 和 ``frame[24:24+page_size]``。
* 帧 = 24 字节头（pgno、dbsize、salt1、salt2、cksum1、cksum2，全大端）
  + page_size 字节密文页。

**只有 salt 与 WAL 头一致、且校验和匹配的帧才有效。** 微信 checkpoint 后会
重置 WAL，文件里常残留上一代 salt 的帧；绝不能因为“文件里有帧”就重放。
只重放到最后一个 ``dbsize != 0`` 的 commit 帧（含）；没有 commit 帧时返回空。
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from pathlib import Path

from .sqlcipher import PAGE_SIZE, SALT_SIZE, decrypt_page

__all__ = [
    "WAL_FRAME_HEADER_SIZE",
    "WAL_HEADER_SIZE",
    "WAL_MAGIC",
    "WAL_VERSION",
    "WalError",
    "WalFrame",
    "WalImage",
    "read_wal",
    "read_wal_file",
    "wal_checksum",
]

logger = logging.getLogger(__name__)

_WORD_MASK = 0xFFFFFFFF

#: WAL 头长度。
WAL_HEADER_SIZE = 32
#: 每帧头长度（后面紧跟 page_size 字节页）。
WAL_FRAME_HEADER_SIZE = 24
#: SQLite WAL 版本号（多年未变）。
WAL_VERSION = 3007000
#: 两种合法 magic（0x377F0682 / 0x377F0683）。
WAL_MAGIC = (0x377F0682, 0x377F0683)


class WalError(RuntimeError):
    """WAL 结构非法或无法读取；消息为中文且不含密钥。"""


@dataclass(frozen=True)
class WalFrame:
    """一个**通过校验**且位于最后 commit 之前的 WAL 帧（页已解密）。"""

    index: int
    pgno: int
    dbsize: int
    salt1: int
    salt2: int
    page: bytes


@dataclass(frozen=True)
class WalImage:
    """一次 WAL 解析的结果。

    ``frames`` 已经按文件顺序排好，并且只包含「有效 + 在最后一个 commit 帧
    之前」的帧；``dbsize`` 是重放后的数据库页数，0 表示没有 commit。
    """

    page_size: int
    header_salt: tuple[int, int]
    frames: tuple[WalFrame, ...]
    dbsize: int
    frame_count: int

    @property
    def has_commit(self) -> bool:
        """是否存在可应用的 commit 帧。"""
        return self.dbsize > 0


def wal_checksum(data: bytes, s1: int = 0, s2: int = 0) -> tuple[int, int]:
    """SQLite WAL 校验和：8 字节一组、小端 u32，返回续算后的 ``(s1, s2)``。"""
    usable = len(data) - (len(data) % 8)
    if usable <= 0:
        return s1 & _WORD_MASK, s2 & _WORD_MASK
    words = struct.unpack(f"<{usable // 4}I", data[:usable])
    for index in range(0, len(words), 2):
        s1 = (s1 + words[index] + s2) & _WORD_MASK
        s2 = (s2 + words[index + 1] + s1) & _WORD_MASK
    return s1, s2


def _empty_image(page_size: int) -> WalImage:
    return WalImage(
        page_size=page_size,
        header_salt=(0, 0),
        frames=(),
        dbsize=0,
        frame_count=0,
    )


def read_wal(
    data: bytes,
    enc_key: bytes,
    *,
    db_salt: bytes,
    page_size: int = PAGE_SIZE,
) -> WalImage:
    """解析 WAL 字节串并解密要应用的帧。

    ``db_salt`` 是**数据库**的 SQLCipher salt（page 1 的 ``[0:16]``），不是
    WAL 头里的 salt1/salt2；后者只用来判断帧是否属于当前这一代 WAL。

    短于头长度的文件（正在写/刚被清空）返回空镜像；magic/version/页大小
    明显不对时抛 :class:`WalError`。头部校验和不匹配按空 WAL 处理。
    """
    if page_size <= 0 or page_size % 8:
        raise ValueError(f"WAL 页大小必须是 8 的整数倍，收到 {page_size}")
    if len(db_salt) != SALT_SIZE:
        raise ValueError(f"db_salt 必须是 {SALT_SIZE} 字节，收到 {len(db_salt)}")
    if len(data) < WAL_HEADER_SIZE:
        return _empty_image(page_size)

    (
        magic,
        version,
        stored_page_size,
        _checkpoint_seq,
        salt1,
        salt2,
        checksum1,
        checksum2,
    ) = struct.unpack(">8I", data[:WAL_HEADER_SIZE])

    if magic not in WAL_MAGIC:
        raise WalError(f"不是有效的 SQLite WAL：magic={magic:#010x}")
    if version != WAL_VERSION:
        raise WalError(f"不支持的 WAL 版本：{version}")
    if stored_page_size != page_size:
        raise WalError(
            f"WAL 页大小 {stored_page_size} 与预期 {page_size} 不一致"
        )

    s1, s2 = wal_checksum(data[:24], 0, 0)
    if (s1, s2) != (checksum1, checksum2):
        # 头部正在被重写时可能读到半截，按“当前 WAL 还没有可用内容”处理；
        # 下一轮 refresh 会重新读。
        logger.debug("WAL 头部校验和不匹配，本轮视为空 WAL")
        return _empty_image(page_size)

    frame_size = WAL_FRAME_HEADER_SIZE + page_size
    frame_count = (len(data) - WAL_HEADER_SIZE) // frame_size

    # (index, pgno, dbsize, salt1, salt2, raw_page)
    raw_frames: list[tuple[int, int, int, int, int, bytes]] = []
    last_commit = -1
    last_dbsize = 0

    for index in range(frame_count):
        offset = WAL_HEADER_SIZE + index * frame_size
        header = data[offset:offset + WAL_FRAME_HEADER_SIZE]
        pgno, dbsize, frame_salt1, frame_salt2, frame_cksum1, frame_cksum2 = (
            struct.unpack(">6I", header)
        )

        # 校验和链必须跨**所有**帧连续计算（包括 salt 不匹配的旧帧）。
        s1, s2 = wal_checksum(header[:8], s1, s2)
        page_start = offset + WAL_FRAME_HEADER_SIZE
        raw_page = data[page_start:page_start + page_size]
        s1, s2 = wal_checksum(raw_page, s1, s2)

        valid = (
            pgno > 0
            and frame_salt1 == salt1
            and frame_salt2 == salt2
            and s1 == frame_cksum1
            and s2 == frame_cksum2
        )
        if not valid:
            continue

        raw_frames.append((index, pgno, dbsize, frame_salt1, frame_salt2, raw_page))
        if dbsize:
            last_commit = index
            last_dbsize = dbsize

    if last_commit < 0:
        # 没有 commit 帧：事务未提交，一帧都不能应用。
        return _empty_image(page_size)

    frames: list[WalFrame] = []
    for index, pgno, dbsize, frame_salt1, frame_salt2, raw_page in raw_frames:
        if index > last_commit:
            break
        plain = decrypt_page(raw_page, enc_key, pgno, db_salt)
        frames.append(
            WalFrame(
                index=index,
                pgno=pgno,
                dbsize=dbsize,
                salt1=frame_salt1,
                salt2=frame_salt2,
                page=plain,
            )
        )

    return WalImage(
        page_size=page_size,
        header_salt=(salt1, salt2),
        frames=tuple(frames),
        dbsize=last_dbsize,
        frame_count=frame_count,
    )


def read_wal_file(
    path: Path | str,
    enc_key: bytes,
    *,
    db_salt: bytes,
    page_size: int = PAGE_SIZE,
) -> WalImage:
    """读取并解析 WAL 文件；文件不存在返回空镜像（微信 checkpoint 后会删）。"""
    wal_path = Path(path)
    try:
        data = wal_path.read_bytes()
    except FileNotFoundError:
        return _empty_image(page_size)
    except OSError as exc:
        raise WalError(f"读取 WAL 失败：{wal_path}：{exc}") from exc
    return read_wal(data, enc_key, db_salt=db_salt, page_size=page_size)
