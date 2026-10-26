from __future__ import annotations

import getpass
import logging
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
TOKEN_PATH = REPO_ROOT / "data" / "supervisor.token"


def _current_user() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - 取不到用户名时退回环境变量
        user = ""
    return user or os.environ.get("USERNAME") or os.environ.get("USER") or ""


def harden_token_file(path: Path) -> None:
    """把 token 文件收紧到当前用户可读写；失败只告警，不能阻断启动。

    Windows 用 icacls 去掉继承；POSIX 用 0o600。测试会 mock 掉真正的系统调用，
    不会修改测试机的 ACL。
    """
    try:
        if os.name == "nt":
            user = _current_user()
            if not user:
                # 找不到当前用户时不贸然去掉继承，避免把自己锁在文件外。
                log.warning("控制口令 ACL 加固跳过（%s）：无法确定当前用户", path)
                return
            args = [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{user}:(R,W)",
            ]
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                log.warning(
                    "控制口令 ACL 加固失败（%s）：%s",
                    path,
                    (result.stderr or result.stdout or "").strip(),
                )
        else:
            os.chmod(path, 0o600)
    except Exception as exc:  # noqa: BLE001 - ACL 失败绝不能影响启动
        log.warning("控制口令 ACL 加固失败（%s）：%s", path, exc)


class ControlToken:
    """control token 的唯一持有者：加载、首次生成、加固与原子轮换。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._value = ""

    @property
    def value(self) -> str:
        return self._value

    def load_or_create(self) -> str:
        existing = self._read_existing()
        if existing:
            self._value = existing
            # 已有文件同样收紧，不能只保护新生成的文件。
            harden_token_file(self.path)
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_tmp = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        os.close(fd)  # mkstemp 已按 0600 创建，POSIX 上临时文件不会先泄露权限
        tmp_path = Path(raw_tmp)
        try:
            tmp_path.write_text(value, encoding="utf-8")
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def load_or_create_token(path: Path = TOKEN_PATH) -> str:
    """兼容包装：旧调用方仍可直接拿字符串。"""
    return ControlToken(path).load_or_create()


__all__ = [
    "TOKEN_PATH",
    "ControlToken",
    "harden_token_file",
    "load_or_create_token",
]
