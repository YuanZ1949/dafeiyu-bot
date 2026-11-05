"""微信 4.x ``.dat``（V2 容器）用的 AES-ECB 原语（纯 Python）。

只做密码学，不认文件格式、不记录密钥。**不依赖任何第三方库，也不依赖
平台能力**：AES-128 / AES-192 / AES-256 都由本模块自己实现，Linux 与
Windows 行为一致，缺失 ``bcrypt.dll`` 也不会影响解码。

ECB 模式不需要 IV；调用方负责保证 ``data`` 是 16 字节的整数倍（这里也会
再校验一次）。加密函数只用于测试/工具，解密用于 :mod:`dafeiyu.gateway.
wechat_bridge.media.dat`。

性能取舍：V2 容器只有前 ~1 KiB 是 AES 密文，其余是明文与单字节 XOR，
所以按字节实现的 AES 足够用；这里刻意选择可读性而不是查表优化。
"""

from __future__ import annotations

__all__ = [
    "CipherError",
    "aes_cbc_decrypt",
    "aes_cbc_encrypt",
    "aes_ecb_decrypt",
    "aes_ecb_encrypt",
]

#: 允许的 AES 密钥长度：AES-128 / AES-192 / AES-256。
_KEY_SIZES = (16, 24, 32)
_BLOCK_SIZE = 16


class CipherError(RuntimeError):
    """AES-ECB 调用失败。

    所有消息都是中文且**不包含密钥/明文**，可以直接进日志。
    """


# ---------------------------------------------------------------------------
# GF(2^8) 与 S 盒（启动时按定义生成，避免手抄 256 项表出错）
# ---------------------------------------------------------------------------


def _xtime(value: int) -> int:
    """GF(2^8) 里乘 2（不可约多项式 0x11B）。"""
    value <<= 1
    if value & 0x100:
        value ^= 0x11B
    return value & 0xFF


def _mul(left: int, right: int) -> int:
    """GF(2^8) 乘法；只用于建表和逆 MixColumns 的固定系数。"""
    result = 0
    while right:
        if right & 1:
            result ^= left
        left = _xtime(left)
        right >>= 1
    return result


def _inverse(value: int) -> int:
    """GF(2^8) 乘法逆元（0 -> 0）：``value ** 254``。"""
    if value == 0:
        return 0
    result = 1
    base = value
    exponent = 254
    while exponent:
        if exponent & 1:
            result = _mul(result, base)
        base = _mul(base, base)
        exponent >>= 1
    return result


def _rotate(value: int, count: int) -> int:
    """8 位循环左移。"""
    return ((value << count) | (value >> (8 - count))) & 0xFF


def _build_tables() -> tuple[bytes, bytes]:
    sbox = bytearray(256)
    for value in range(256):
        inv = _inverse(value)
        sbox[value] = (
            inv
            ^ _rotate(inv, 1)
            ^ _rotate(inv, 2)
            ^ _rotate(inv, 3)
            ^ _rotate(inv, 4)
            ^ 0x63
        )
    inverse = bytearray(256)
    for value, mapped in enumerate(sbox):
        inverse[mapped] = value
    return bytes(sbox), bytes(inverse)


_SBOX, _INV_SBOX = _build_tables()

#: GF(2^8) 乘法表。列混合每轮要做 64 次常数乘法，直接查表比逐位运算快
#: 一个数量级（600 KB 的动态表情从 ~20 s 降到 ~9 s）。
_MUL2 = bytes(_mul(value, 2) for value in range(256))
_MUL3 = bytes(_mul(value, 3) for value in range(256))
_MUL9 = bytes(_mul(value, 9) for value in range(256))
_MUL11 = bytes(_mul(value, 11) for value in range(256))
_MUL13 = bytes(_mul(value, 13) for value in range(256))
_MUL14 = bytes(_mul(value, 14) for value in range(256))


# ---------------------------------------------------------------------------
# 轮函数
# ---------------------------------------------------------------------------


def _sub_bytes(state: list[int]) -> None:
    for index, value in enumerate(state):
        state[index] = _SBOX[value]


def _inv_sub_bytes(state: list[int]) -> None:
    for index, value in enumerate(state):
        state[index] = _INV_SBOX[value]


def _shift_rows(state: list[int]) -> list[int]:
    """状态按列优先存放：``state[4 * column + row]``。"""
    out = state[:]
    for row in range(1, 4):
        for column in range(4):
            out[4 * column + row] = state[4 * ((column + row) % 4) + row]
    return out


def _inv_shift_rows(state: list[int]) -> list[int]:
    out = state[:]
    for row in range(1, 4):
        for column in range(4):
            out[4 * ((column + row) % 4) + row] = state[4 * column + row]
    return out


def _mix_columns(state: list[int]) -> None:
    mul2, mul3 = _MUL2, _MUL3
    for column in range(4):
        offset = 4 * column
        a0, a1, a2, a3 = state[offset : offset + 4]
        state[offset] = mul2[a0] ^ mul3[a1] ^ a2 ^ a3
        state[offset + 1] = a0 ^ mul2[a1] ^ mul3[a2] ^ a3
        state[offset + 2] = a0 ^ a1 ^ mul2[a2] ^ mul3[a3]
        state[offset + 3] = mul3[a0] ^ a1 ^ a2 ^ mul2[a3]


def _inv_mix_columns(state: list[int]) -> None:
    mul9, mul11, mul13, mul14 = _MUL9, _MUL11, _MUL13, _MUL14
    for column in range(4):
        offset = 4 * column
        a0, a1, a2, a3 = state[offset : offset + 4]
        state[offset] = mul14[a0] ^ mul11[a1] ^ mul13[a2] ^ mul9[a3]
        state[offset + 1] = mul9[a0] ^ mul14[a1] ^ mul11[a2] ^ mul13[a3]
        state[offset + 2] = mul13[a0] ^ mul9[a1] ^ mul14[a2] ^ mul11[a3]
        state[offset + 3] = mul11[a0] ^ mul13[a1] ^ mul9[a2] ^ mul14[a3]


def _add_round_key(state: list[int], round_key: list[int]) -> None:
    for index in range(_BLOCK_SIZE):
        state[index] ^= round_key[index]


# ---------------------------------------------------------------------------
# 密钥扩展
# ---------------------------------------------------------------------------


class _Scheduler:
    """AES 密钥扩展；构造时算好所有轮密钥（字节列表）。"""

    __slots__ = ("round_keys", "rounds")

    def __init__(self, key: bytes) -> None:
        word_count = len(key) // 4
        self.rounds = word_count + 6
        words: list[list[int]] = [
            list(key[4 * index : 4 * index + 4]) for index in range(word_count)
        ]
        rcon = 1
        for index in range(word_count, 4 * (self.rounds + 1)):
            temp = words[index - 1][:]
            if index % word_count == 0:
                temp = temp[1:] + temp[:1]
                temp = [_SBOX[value] for value in temp]
                temp[0] ^= rcon
                rcon = _xtime(rcon)
            elif word_count > 6 and index % word_count == 4:
                temp = [_SBOX[value] for value in temp]
            words.append(
                [
                    words[index - word_count][offset] ^ temp[offset]
                    for offset in range(4)
                ]
            )
        self.round_keys: list[list[int]] = [
            words[4 * round_index] + words[4 * round_index + 1]
            + words[4 * round_index + 2] + words[4 * round_index + 3]
            for round_index in range(self.rounds + 1)
        ]


# ---------------------------------------------------------------------------
# 单块加解密
# ---------------------------------------------------------------------------


def _encrypt_block(round_keys: list[list[int]], block: bytes) -> bytes:
    state = list(block)
    _add_round_key(state, round_keys[0])
    for round_index in range(1, len(round_keys) - 1):
        _sub_bytes(state)
        state = _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, round_keys[round_index])
    _sub_bytes(state)
    state = _shift_rows(state)
    _add_round_key(state, round_keys[-1])
    return bytes(state)


def _decrypt_block(round_keys: list[list[int]], block: bytes) -> bytes:
    state = list(block)
    _add_round_key(state, round_keys[-1])
    for round_index in range(len(round_keys) - 2, 0, -1):
        state = _inv_shift_rows(state)
        _inv_sub_bytes(state)
        _add_round_key(state, round_keys[round_index])
        _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    _inv_sub_bytes(state)
    _add_round_key(state, round_keys[0])
    return bytes(state)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def _check_key(key: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(key, (bytes, bytearray, memoryview)):
        raise TypeError("key 必须是 bytes")
    value = bytes(key)
    if len(value) not in _KEY_SIZES:
        raise ValueError(f"AES 密钥必须是 16/24/32 字节，收到 {len(value)} 字节")
    return value


def _check_data(data: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data 必须是 bytes")
    value = bytes(data)
    if not value:
        raise ValueError("AES-ECB 数据不能为空")
    if len(value) % _BLOCK_SIZE:
        raise ValueError(
            f"AES-ECB 数据长度必须是 16 的整数倍，收到 {len(value)}"
        )
    return value


def _ecb_crypt(key: bytes, data: bytes, *, encrypt: bool) -> bytes:
    scheduler = _Scheduler(key)
    crypt = _encrypt_block if encrypt else _decrypt_block
    out = bytearray(len(data))
    for offset in range(0, len(data), _BLOCK_SIZE):
        out[offset : offset + _BLOCK_SIZE] = crypt(
            scheduler.round_keys, data[offset : offset + _BLOCK_SIZE]
        )
    return bytes(out)


def aes_ecb_encrypt(
    key: bytes | bytearray | memoryview,
    data: bytes | bytearray | memoryview,
) -> bytes:
    """AES-ECB 加密（无填充，``data`` 长度必须是 16 的整数倍）。"""
    return _ecb_crypt(_check_key(key), _check_data(data), encrypt=True)


def aes_ecb_decrypt(
    key: bytes | bytearray | memoryview,
    data: bytes | bytearray | memoryview,
) -> bytes:
    """AES-ECB 解密（无填充，``data`` 长度必须是 16 的整数倍）。"""
    return _ecb_crypt(_check_key(key), _check_data(data), encrypt=False)


def _check_iv(iv: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(iv, (bytes, bytearray, memoryview)):
        raise TypeError("iv 必须是 bytes")
    value = bytes(iv)
    if len(value) != _BLOCK_SIZE:
        raise ValueError(f"IV 必须是 16 字节，收到 {len(value)} 字节")
    return value


def aes_cbc_decrypt(
    key: bytes | bytearray | memoryview,
    iv: bytes | bytearray | memoryview,
    data: bytes | bytearray | memoryview,
) -> bytes:
    """AES-CBC 解密（无填充，``data`` 长度必须是 16 的整数倍）。

    微信表情缓存用的就是 AES-128-CBC、``IV == key``；这里只做密码学原语，
    填充与格式识别由调用方负责。
    """
    raw = _check_data(data)
    previous = _check_iv(iv)
    scheduler = _Scheduler(_check_key(key))
    out = bytearray(len(raw))
    for offset in range(0, len(raw), _BLOCK_SIZE):
        block = raw[offset : offset + _BLOCK_SIZE]
        plain = _decrypt_block(scheduler.round_keys, block)
        out[offset : offset + _BLOCK_SIZE] = bytes(
            value ^ mask for value, mask in zip(plain, previous, strict=True)
        )
        previous = block
    return bytes(out)


def aes_cbc_encrypt(
    key: bytes | bytearray | memoryview,
    iv: bytes | bytearray | memoryview,
    data: bytes | bytearray | memoryview,
) -> bytes:
    """AES-CBC 加密（无填充）；主要供测试构造样本。"""
    raw = _check_data(data)
    previous = _check_iv(iv)
    scheduler = _Scheduler(_check_key(key))
    out = bytearray(len(raw))
    for offset in range(0, len(raw), _BLOCK_SIZE):
        block = bytes(
            value ^ mask
            for value, mask in zip(
                raw[offset : offset + _BLOCK_SIZE], previous, strict=True
            )
        )
        cipher = _encrypt_block(scheduler.round_keys, block)
        out[offset : offset + _BLOCK_SIZE] = cipher
        previous = cipher
    return bytes(out)
