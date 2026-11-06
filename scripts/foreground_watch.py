"""前台窗口变化监视器（只读）。

用途
----
轮询当前前台窗口，仅在前台窗口发生变化时输出一行：

    2026-11-06T21:30:12.345+08:00 pid=12345 exe=Weixin.exe title=微信  ★WECHAT

微信（Weixin.exe / WeChat.exe）加 ``★WECHAT`` 标记，其它进程不带标记。
stdout 打印一行，同时追加写入 UTF-8 日志文件（每行 flush）。

用法
----
    .venv\\Scripts\\python scripts\\foreground_watch.py                  # 一直跑，Ctrl+C 结束
    .venv\\Scripts\\python scripts\\foreground_watch.py --seconds 300 --out logs\\foreground-watch.log
    .venv\\Scripts\\python scripts\\foreground_watch.py --interval 0.1 --seconds 60

参数
----
--interval   轮询间隔秒数，默认 0.3
--seconds    总运行时长秒数，默认 0 表示一直跑到 Ctrl+C
--out        日志文件路径，默认 logs/foreground-watch.log

只读保证
--------
本脚本只调用 Win32 的只读 API：
GetForegroundWindow / GetWindowThreadProcessId / OpenProcess +
QueryFullProcessImageNameW / GetWindowTextW / CloseHandle。
绝不调用 SetForegroundWindow、ShowWindow、SetWindowPos，
不会激活、移动、置顶或任何方式触碰任何窗口，也不会写注册表/启动进程。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from datetime import datetime
from pathlib import Path

user32 = ctypes.windll.user32  # type: ignore[attr-defined]
kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WECHAT_EXES = {"weixin.exe", "wechat.exe"}
DEFAULT_OUT = Path("logs/foreground-watch.log")


def _get_window_title(hwnd: int) -> str:
    try:
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        got = user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value if got > 0 else ""
        return title if title else "?"
    except Exception:
        return "?"


def _get_process_exe(hwnd: int) -> tuple[str, int]:
    pid = ctypes.c_ulong(0)
    try:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    except Exception:
        return "?", 0
    pid_num = pid.value or 0
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_num)
    if not handle:
        # 拒绝访问/进程已退出时，pid 仍可能有值，exe 用 ? 占位
        return "?", pid_num
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            exe = Path(buf.value).name if buf.value else "?"
            return exe or "?", pid_num
        return "?", pid_num
    except Exception:
        return "?", pid_num
    finally:
        kernel32.CloseHandle(handle)


def snapshot(hwnd: int) -> dict[str, str | int]:
    exe, pid = _get_process_exe(hwnd)
    title = _get_window_title(hwnd)
    return {"hwnd": hwnd, "pid": pid, "exe": exe, "title": title}


class Log:
    def __init__(self, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        self._path = out
        self._fh = out.open("a", encoding="utf-8")
        self._fh.write(
            f"# foreground-watch started {datetime.now().astimezone().isoformat(timespec='milliseconds')}\n"
        )
        self._fh.flush()

    def write(self, line: str) -> None:
        self._fh.write(line + "\n")
        self._fh.flush()
        try:
            print(line, flush=True)
        except OSError:
            pass  # stdout 已关（例：无控制台），文件仍在记录

    def close(self) -> None:
        self._fh.close()


def render(info: dict[str, str | int]) -> str:
    exe = str(info["exe"])
    line = (
        f"{datetime.now().astimezone().isoformat(timespec='milliseconds')}"
        f" pid={info['pid']} exe={exe} title={info['title']}"
    )
    if exe.lower() in WECHAT_EXES:
        line += "  ★WECHAT"
    return line


def run(interval: float, seconds: int, log: Log) -> int:
    prev_hwnd: int | None = None
    end_tick: float | None = None
    import time

    if seconds > 0:
        end_tick = time.monotonic() + seconds
    changes = 0
    while True:
        if end_tick is not None and time.monotonic() >= end_tick:
            break
        hwnd = user32.GetForegroundWindow()
        if hwnd != prev_hwnd:
            if hwnd:  # hwnd==0（无前台）不记录，避免死窗口查询
                log.write(render(snapshot(hwnd)))
                changes += 1
            prev_hwnd = hwnd
        time.sleep(interval)
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="前台窗口变化监视器（只读）")
    parser.add_argument("--interval", type=float, default=0.3)
    parser.add_argument("--seconds", type=int, default=0)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    log = Log(args.out)
    exit_code = 0
    try:
        run(max(args.interval, 0.01), args.seconds, log)
    except KeyboardInterrupt:
        log.write("# stopped by Ctrl+C")
    except Exception as exc:  # 不让任何边界情况导致异常退出
        log.write(f"# watcher error: {exc!r}")
        exit_code = 1
    finally:
        log.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
