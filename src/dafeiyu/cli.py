"""运维 CLI：forget / stats / summary。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .bootstrap import (
    CONFIG_NAME,
    ENV_NAME,
    format_issues,
    has_errors,
    init_config,
    preflight,
)
from .config import ConfigError, load_settings
from .env_probe import (
    format_wechat_config,
    format_wechat_report,
    probe_wechat,
    wechat_ok,
)
from .ids import normalize_chat_id
from .memory.store import Store
from .models import ChatType
from .version import get_version

CHAT_TYPES = ["group", "private"]


def _chat_id_arg(raw: str) -> str:
    try:
        return normalize_chat_id(raw, label="--chat")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dafeiyu", description="溟月 / 大肥鱼 运维 CLI")
    # 只打印版本就退出，不碰配置与网络；放在 subparsers 之前，
    # argparse 的 required=True 也不会拦截 --version。
    parser.add_argument(
        "--version", action="version", version=f"dafeiyu {get_version()}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    forget = sub.add_parser("forget", help="清空会话记忆")
    forget.add_argument("--chat", type=_chat_id_arg, help="群号或用户号")
    forget.add_argument("--type", choices=CHAT_TYPES, default="group")
    forget.add_argument("--all", action="store_true", help="清空所有会话")

    stats = sub.add_parser("stats", help="查看今日调用与 token")
    stats.add_argument("--chat", type=_chat_id_arg, required=True)
    stats.add_argument("--type", choices=CHAT_TYPES, default="group")

    summary = sub.add_parser("summary", help="查看某天的群摘要")
    summary.add_argument("--chat", type=_chat_id_arg, required=True)
    summary.add_argument("--date", required=True, help="YYYY-MM-DD")
    summary.add_argument("--type", choices=CHAT_TYPES, default="group")

    init = sub.add_parser("init", help="从 config.example.yaml 生成 config.yaml")
    init.add_argument("--config", default=CONFIG_NAME, help="目标路径，默认 config.yaml")
    init.add_argument("--force", action="store_true", help="已存在时覆盖")

    doctor = sub.add_parser("doctor", help="检查配置、依赖与端口，给出修复建议")
    doctor.add_argument("--config", default=CONFIG_NAME, help="配置文件路径")
    doctor.add_argument("--env-file", default=ENV_NAME, help="环境变量文件路径")
    doctor.add_argument("--no-ports", action="store_true", help="跳过端口占用检查")
    doctor.add_argument(
        "--json", action="store_true", help="以 JSON 输出体检结果（供脚本解析）"
    )
    return parser


def _cmd_init(args: argparse.Namespace) -> int:
    try:
        result = init_config(args.config, force=args.force)
    except FileNotFoundError as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1
    if result.created:
        print(f"已生成 {result.path}（模板：{result.source}）")
        print("下一步：在 .env 里填 BOT_QQ 等凭据，然后运行 dafeiyu doctor 体检。")
    else:
        print(f"{result.path} 已存在，未覆盖（需要重置请加 --force）。")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    # Windows 重定向时 stdout 默认是 locale（GBK），报告里的 ✗ 和中文会直接
    # UnicodeEncodeError；固定 UTF-8，脚本解析和管道输出都能拿到合法文本。
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    issues = preflight(args.config, args.env_file, check_ports=not args.no_ports)
    try:
        settings = load_settings(
            env_file=Path(args.env_file), config_file=Path(args.config)
        )
    except (ConfigError, UnicodeDecodeError, OSError):
        # preflight 已经在 issues 里报告了配置问题，这里只降级为「无微信配置」。
        settings = None

    checks = probe_wechat(settings.wechat if settings is not None else None)
    ok = wechat_ok(checks)

    if args.json:
        print(json.dumps({"checks": checks, "ok": ok}, ensure_ascii=False))
        return 0 if ok and not has_errors(issues) else 1

    print(format_issues(issues))
    if settings is not None:
        print(format_wechat_config(settings.wechat))
    print(format_wechat_report(checks))
    return 1 if has_errors(issues) or not ok else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "forget" and not args.all and args.chat is None:
            parser.error("forget 需要 --chat 或 --all")
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0

    if args.command == "init":
        return _cmd_init(args)
    if args.command == "doctor":
        return _cmd_doctor(args)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1

    store = Store(settings.db_path)
    store.init_schema()
    try:
        chat_type = ChatType(args.type)
        if args.command == "forget":
            if args.all:
                store.forget_all()
                print("所有会话的记忆已清空。🫧")
            else:
                store.forget_chat(chat_type, args.chat)
                print(f"已清空 {args.type} {args.chat} 的记忆。🫧")
            return 0

        if args.command == "stats":
            calls, tokens = store.usage_today(chat_type, args.chat, Store.today())
            remaining = max(0, settings.budget.daily_calls - calls)
            print(f"今天调用 {calls} 次，token {tokens}，剩余 {remaining} 次额度。🐋")
            return 0

        content = store.get_summary(chat_type, args.chat, args.date)
        print(content if content else "（这一天没有摘要）")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
