"""按 host 沉淀链接失败结论（chat-driven-behaviour-fixes T8）。

「这个站直连读不到、下次直接换路子」是跨批次、跨进程都成立的结论：同一篇
文章被抓失败三次是三次纯浪费。本模块把结论记在链接模块自己的
``data/link-cache/host-failures.json`` 里（与 T1 的 URL 缓存同目录，**不进
SQLite、不动任何表结构**），供 ``LinkPromptResolver`` 解析前查表、按层失败
后追加/升级。

数据形态（**一个 host 一条记录**，重复写入只更新同一条）::

    {
      "version": 1,
      "hosts": {
        "www.zhihu.com": {
          "verdict": "needs_browser",   # 或 search_only
          "shape": "403",               # 失败形态：403 / timeout / empty / challenge
          "reason": "http_403",         # 原始 reason（沿用 T6 的词汇）
          "layer": "http",              # 这条结论来自哪一层
          "first_seen_at": 1700000000.0,
          "updated_at": 1700000000.0
        }
      }
    }

两种结论：``needs_browser``（HTTP 层不行，直接进浏览器层）与
``search_only``（本机两层都到不了，直接产出模型侧兜底指令）。

读写都必须「坏文件绝不打断回复主链路」：解析失败按空表处理，写失败静默
放弃；写盘沿用 ``workspace.enforce_limit`` 的临时文件 + ``os.replace`` 范式，
进程中途崩溃不会留下半截 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dafeiyu.config import LinkConfig

__all__ = [
    "FAILURE_FILE_NAME",
    "NEEDS_BROWSER",
    "SEARCH_ONLY",
    "HostFailureStore",
    "HostVerdict",
    "failure_shape",
    "url_host",
]

#: 与 T1 的 URL 缓存同目录。文件名不是 ``<sha256>.json``，所以缓存淘汰
#: （``LinkResolver._evict`` 只碰那种形态）绝不会把它删掉。
FAILURE_FILE_NAME = "host-failures.json"

#: 结论一：直连（HTTP 层）不行，下次直接进浏览器层。
NEEDS_BROWSER = "needs_browser"
#: 结论二：本机两层都到不了，下次直接产出模型侧兜底指令（搜索 / 请对方补充）。
SEARCH_ONLY = "search_only"

_VERDICTS = frozenset({NEEDS_BROWSER, SEARCH_ONLY})

#: 去掉层前缀后仍原样保留的形态词；其余数字形态（403/412…）直接当形态。
_NAMED_SHAPES = frozenset({"challenge", "empty", "network", "too_large", "error"})


def failure_shape(reason: str) -> str:
    """把 T6 的 ``reason`` 归一成失败形态：``http_403`` → ``403``、``browser_timeout`` → ``timeout``。

    形态是给人看/给排障用的分类，**不参与结论判定**（结论只看 host + 是否
    过期）；所以这里宁可真宽：认不出的 reason 归到 ``other``。
    """
    token = (reason or "").strip().lower()
    if token.startswith("http_"):
        token = token[len("http_") :]
    elif token.startswith("browser_"):
        token = token[len("browser_") :]
    if not token:
        return "unknown"
    if "timeout" in token or "timed_out" in token:
        return "timeout"
    if token.isdigit():
        return token
    return token if token in _NAMED_SHAPES else "other"


def url_host(url: str) -> str:
    """取 URL 的 host（小写、不带端口与 userinfo）；解析不出来返回空串。

    host 为空（相对地址、纯文字）时调用方不记录也不查表——没有 host
    就没有「同一个站」这层知识。
    """
    try:
        hostname = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return hostname.strip().lower()


@dataclass(frozen=True)
class HostVerdict:
    """一条 host 级失败结论（已归一化，可直接决策）。"""

    host: str
    verdict: str
    shape: str = ""
    reason: str = ""
    layer: str = ""
    first_seen_at: float = 0.0
    updated_at: float = 0.0


class HostFailureStore:
    """``host-failures.json`` 的读写门面：按 host 一记录，带 TTL，绝不抛异常。

    ``clock`` / ``path`` 是测试注入点：生产恒用 ``time.time`` 与
    ``<cache_dir>/host-failures.json``（TTL 要跨进程重启有效，必须用墙钟）。
    """

    def __init__(
        self,
        config: LinkConfig,
        *,
        clock: Callable[[], float] | None = None,
        path: str | Path | None = None,
    ) -> None:
        self._config = config
        self._clock: Callable[[], float] = clock or time.time
        self._path = (
            Path(path) if path is not None else Path(config.cache_dir) / FAILURE_FILE_NAME
        )
        self._hosts: dict[str, dict[str, Any]] = self._load()

    @property
    def path(self) -> Path:
        """落盘位置（测试与排障用）。"""
        return self._path

    # ---- 读 ---------------------------------------------------------------

    def get(self, host: str, *, at: float | None = None) -> HostVerdict | None:
        """取一条**未过期**的结论；缺失 / 过期 / 损坏一律 ``None``（= 没有知识）。

        过期不等于删除：文件里的旧记录留着，下一次真实尝试后会被覆盖
        （失败刷新）或清掉（成功推翻）。
        """
        key = _normalize_host(host)
        entry = self._hosts.get(key)
        if entry is None:
            return None
        verdict = self._to_verdict(key, entry)
        now = self._clock() if at is None else at
        if verdict.updated_at + self._config.host_failure_ttl_seconds <= now:
            return None
        return verdict

    def snapshot(self) -> dict[str, HostVerdict]:
        """所有记录的原始快照（**不**按 TTL 过滤；测试与排障用，不进提示词）。"""
        return {host: self._to_verdict(host, entry) for host, entry in self._hosts.items()}

    # ---- 写 ---------------------------------------------------------------

    def record_http_failure(
        self, host: str, reason: str, *, at: float | None = None
    ) -> HostVerdict | None:
        """HTTP 层失败：**立刻**记 ``needs_browser``（下次直接走浏览器层）。"""
        return self._record(host, NEEDS_BROWSER, "http", reason, at)

    def record_browser_failure(
        self, host: str, reason: str, *, at: float | None = None
    ) -> HostVerdict | None:
        """浏览器层也失败：把**同一条**记录**升级**为 ``search_only``，不新增条目。"""
        return self._record(host, SEARCH_ONLY, "browser", reason, at)

    def forget(self, host: str) -> bool:
        """删掉一条记录（一次真实尝试成功推翻了旧结论时调用）。"""
        key = _normalize_host(host)
        if self._hosts.pop(key, None) is None:
            return False
        self._save()
        return True

    def _record(
        self, host: str, verdict: str, layer: str, reason: str, at: float | None
    ) -> HostVerdict | None:
        key = _normalize_host(host)
        if not key:
            return None
        now = self._clock() if at is None else at
        previous = self._hosts.get(key) or {}
        first_seen = previous.get("first_seen_at")
        if not isinstance(first_seen, (int, float)):
            first_seen = now
        entry = {
            "verdict": verdict,
            "shape": failure_shape(reason),
            "reason": str(reason or ""),
            "layer": layer,
            "first_seen_at": float(first_seen),
            "updated_at": float(now),
        }
        # 以 host 为键覆盖写：同 host+形态重复写不会多一条，升级也落在同一条上。
        self._hosts[key] = entry
        self._save()
        return self._to_verdict(key, entry)

    # ---- 落盘 -------------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        """读文件：缺失 / 非 JSON / 结构不对都当空表，绝不把异常抛给回复链路。"""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw_hosts = data.get("hosts")
        if not isinstance(raw_hosts, dict):
            return {}
        hosts: dict[str, dict[str, Any]] = {}
        for raw_host, raw_entry in raw_hosts.items():
            if not isinstance(raw_host, str) or not isinstance(raw_entry, dict):
                continue
            key = _normalize_host(raw_host)
            verdict = raw_entry.get("verdict")
            updated = raw_entry.get("updated_at")
            if not key or verdict not in _VERDICTS:
                continue
            if not isinstance(updated, (int, float)):
                continue
            first_seen = raw_entry.get("first_seen_at")
            hosts[key] = {
                "verdict": str(verdict),
                "shape": str(raw_entry.get("shape") or ""),
                "reason": str(raw_entry.get("reason") or ""),
                "layer": str(raw_entry.get("layer") or ""),
                "first_seen_at": float(first_seen)
                if isinstance(first_seen, (int, float))
                else float(updated),
                "updated_at": float(updated),
            }
        return hosts

    def _save(self) -> None:
        """原子写：临时文件 + fsync + ``os.replace``；任何 OSError 只丢这次记录。"""
        payload = json.dumps(
            {"version": 1, "hosts": self._hosts},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        path = self._path
        tmp: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            tmp = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            # 失败知识是锦上添花：写不进去就不写，绝不影响这次回复。
            pass
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _to_verdict(host: str, entry: dict[str, Any]) -> HostVerdict:
        return HostVerdict(
            host=host,
            verdict=str(entry.get("verdict") or ""),
            shape=str(entry.get("shape") or ""),
            reason=str(entry.get("reason") or ""),
            layer=str(entry.get("layer") or ""),
            first_seen_at=float(entry.get("first_seen_at") or 0.0),
            updated_at=float(entry.get("updated_at") or 0.0),
        )


def _normalize_host(host: str) -> str:
    return (host or "").strip().lower()
