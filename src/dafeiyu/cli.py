"""运维 CLI：forget / stats / summary。"""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_settings
from .memory.store import Store
from .models import ChatType

CHAT_TYPES = ["group", "private"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dafeiyu", description="溟月 / 大肥鱼 运维 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    forget = sub.add_parser("forget", help="清空会话记忆")
    forget.add_argument("--chat", type=int, help="群号或用户号")
    forget.add_argument("--type", choices=CHAT_TYPES, default="group")
    forget.add_argument("--all", action="store_true", help="清空所有会话")

    stats = sub.add_parser("stats", help="查看今日调用与 token")
    stats.add_argument("--chat", type=int, required=True)
    stats.add_argument("--type", choices=CHAT_TYPES, default="group")

    summary = sub.add_parser("summary", help="查看某天的群摘要")
    summary.add_argument("--chat", type=int, required=True)
    summary.add_argument("--date", required=True, help="YYYY-MM-DD")
    summary.add_argument("--type", choices=CHAT_TYPES, default="group")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "forget" and not args.all and args.chat is None:
            parser.error("forget 需要 --chat 或 --all")
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0

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
