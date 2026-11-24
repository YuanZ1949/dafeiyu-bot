from __future__ import annotations

import getpass
import logging
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

from ..winproc import hidden_kwargs

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
TOKEN_PATH = REPO_ROOT / "data" / "supervisor.token"


def _current_user() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - 取不到用户名时退回环境变量
        user = ""
    return user or os.environ.get("USERNAME") or os.environ.get("USER") or ""


def harden_token_file(path: Path) -> bool:
    """把 token 文件收紧到当前用户可读写删；失败只告警，不能阻断启动。

    Windows 用 icacls 去掉继承；POSIX 用 0o600。测试会 mock 掉真正的系统调用，
    不会修改测试机的 ACL。

    授予里必须带 DELETE（``(R,W,D)``）：加固后的文件还要被 ``os.replace`` 覆盖、
    被 ``unlink`` 清除，二者都需要文件自身的删除权限；父目录未必授予
    DELETE_CHILD（例如只继承 ``(M)`` 的目录）。D 只给属主本人，不会让其他用户
    多读一个字节。

    改 DACL 需要 WRITE_DAC：文件属主是当前用户时由所有者隐式拥有。历史上在
    管理员进程里创建的文件属主是 ``BUILTIN\\Administrators``，普通权限下改不动
    ACL（``icacls`` 报「拒绝访问」）。这时按原内容原子重写一次，属主回到当前
    用户再收紧；重写失败才放弃。返回是否已收紧。
    """
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
            return True
        user = _current_user()
        if not user:
            # 找不到当前用户时不贸然去掉继承，避免把自己锁在文件外。
            log.warning("控制口令 ACL 加固跳过（%s）：无法确定当前用户", path)
            return False
        result = _icacls_harden(path, user)
        if result is not None and result.returncode == 0:
            return True
        if _rewrite_as_current_user(path):
            result = _icacls_harden(path, user)
            if result is not None and result.returncode == 0:
                log.info("控制口令文件属主不是当前用户，已重写并收紧 ACL：%s", path)
                return True
        detail = ""
        if result is not None:
            detail = (result.stderr or result.stdout or "").strip()
        log.warning(
            "控制口令 ACL 加固失败（%s）：%s", path, detail or "icacls 不可用"
        )
        return False
    except Exception as exc:  # noqa: BLE001 - ACL 失败绝不能影响启动
        log.warning("控制口令 ACL 加固失败（%s）：%s", path, exc)
        return False


def _icacls_harden(path: Path, user: str) -> subprocess.CompletedProcess[str] | None:
    """执行一次 icacls 收紧；进程起不来时返回 None（调用方负责告警）。"""
    args = [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"{user}:(R,W,D)",
    ]
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("icacls 调用失败（%s）：%s", path, exc)
        return None


def _rewrite_as_current_user(path: Path) -> bool:
    """按原字节原子重写，把属主变回当前用户；失败返回 False。

    只在 ACL 加固被拒绝时使用：文件内容一个字节都不变，只是让当前进程成为
    新文件的创建者，从而拿到改 DACL 所需的属主身份。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    try:
        _atomic_write_bytes(path, data)
    except OSError as exc:
        log.debug("控制口令文件重写失败（%s）：%s", path, exc)
        return False
    return True


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """temp + os.replace：任何失败都不会留下半截内容覆盖旧文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    os.close(fd)  # mkstemp 已按 0600 创建，POSIX 上临时文件不会先泄露权限
    tmp_path = Path(raw_tmp)
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class ControlToken:
    """control token 的唯一持有者：加载、首次生成、加固与原子轮换。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._value = ""

    @property
    def value(self) -> str:
        return self._value

    def load(self) -> str:
        """只读已有文件并加固；文件不存在时返回空串，绝不创建。

        面板改密后 ``data/dashboard.password`` 优先于 ``.env``，启动时只需要
        「读到就用」，不能因为读一次就顺手生成一个空文件把 env 引导顶掉。
        """
        existing = self._read_existing()
        if existing:
            self._value = existing
            # 已有文件同样收紧，不能只保护新生成的文件。
            harden_token_file(self.path)
        return existing

    def load_or_create(self) -> str:
        existing = self.load()
        if existing:
            return existing

        token = secrets.token_urlsafe(24)
        try:
            self._write_atomic(token)
        except OSError as exc:
            # 与旧实现一致：写不了盘也让本次进程内存生效。
            log.warning("控制口令无法写入 %s：%s（本次仅内存生效）", self.path, exc)
            self._value = token
            return token
        self._value = token
        harden_token_file(self.path)
        return token

    def rotate(self) -> str:
        """生成新 token 并原子落盘；任一步失败都保留旧值并抛出。"""
        old = self._value
        new = secrets.token_urlsafe(24)
        try:
            self._write_atomic(new)
            harden_token_file(self.path)
        except BaseException:
            self._value = old
            if old:
                try:
                    self._write_atomic(old)
                except OSError as exc:
                    log.critical("控制口令回滚写盘失败：%s", exc)
            raise
        self._value = new
        return new

    def set(self, value: str) -> None:
        """写回指定 token（dashboard 在重启失败时回滚用）。

        先写盘成功再更新内存；写失败时内存保持原值并向上抛出，调用方记日志。
        """
        previous = self._value
        try:
            self._write_atomic(value)
        except BaseException:
            self._value = previous
            raise
        self._value = value
        harden_token_file(self.path)

    def _read_existing(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _write_atomic(self, value: str) -> None:
        """temp + os.replace：任何失败都不会留下半截 token 覆盖旧文件。"""
        _atomic_write_bytes(self.path, value.encode("utf-8"))


def load_or_create_token(path: Path = TOKEN_PATH) -> str:
    """兼容包装：旧调用方仍可直接拿字符串。"""
    return ControlToken(path).load_or_create()


__all__ = [
    "TOKEN_PATH",
    "ControlToken",
    "harden_token_file",
    "load_or_create_token",
]
