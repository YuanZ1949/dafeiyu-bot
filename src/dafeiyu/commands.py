"""命令注册表：名单、作用域、权限、冷却与帮助文案的唯一来源。

命令解析复用 :func:`dafeiyu.router.parse_command`；这里只负责把命令名
（含别名）映射到 :class:`CommandSpec`，未知斜杠命令仍交给原有模型/批次路径。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ChatType
from .router import parse_command

_GROUP = ChatType.GROUP
_PRIVATE = ChatType.PRIVATE
_BOTH = frozenset({_GROUP, _PRIVATE})


@dataclass(frozen=True)
class CommandSpec:
    name: str
    aliases: frozenset[str]
    scopes: frozenset[ChatType]
    admin_only: bool
    cooldown_seconds: int
    help: str


_COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        name="help",
        aliases=frozenset({"帮助"}),
        scopes=_BOTH,
        admin_only=False,
        cooldown_seconds=5,
        help="帮助",
    ),
    CommandSpec(
        name="stats",
        aliases=frozenset({"统计"}),
        scopes=_BOTH,
        admin_only=False,
        # 私聊运行时固定 5 秒（见 Service._command_cooldown）。
        cooldown_seconds=60,
        help="今日统计",
    ),
    CommandSpec(
        name="status",
        aliases=frozenset({"状态"}),
        scopes=_BOTH,
        admin_only=True,
        cooldown_seconds=30,
        help="运行状态",
    ),
    CommandSpec(
        name="budget",
        aliases=frozenset({"预算"}),
        scopes=_BOTH,
        admin_only=True,
        cooldown_seconds=30,
        help="预算",
    ),
    CommandSpec(
        name="shutup",
        aliases=frozenset({"闭嘴"}),
        scopes=frozenset({_GROUP}),
        admin_only=True,
        cooldown_seconds=0,
        help="静音 60 分钟",
    ),
    CommandSpec(
        name="wake",
        aliases=frozenset({"醒来"}),
        scopes=frozenset({_GROUP}),
        admin_only=True,
        cooldown_seconds=0,
        help="解除静音",
    ),
    CommandSpec(
        name="forget",
        aliases=frozenset(),
        scopes=_BOTH,
        # 群内仅管理员；私聊所有人。asymmetric 权限由 Service/help_text 显式放行。
        admin_only=True,
        cooldown_seconds=0,
        help="清空当前会话",
    ),
)


class CommandRegistry:
    def __init__(self) -> None:
        self._lookup: dict[str, CommandSpec] = {}
        for spec in _COMMANDS:
            self._lookup[spec.name.casefold()] = spec
            for alias in spec.aliases:
                self._lookup[alias.casefold()] = spec

    def match(
        self, text: str, chat_type: ChatType | None = None
    ) -> tuple[CommandSpec, str] | None:
        parsed = parse_command(text)
        if parsed is None:
            return None
        spec = self._lookup.get(parsed.name.casefold())
        if spec is None:
            return None
        if chat_type is not None and chat_type not in spec.scopes:
            return None
        return spec, " ".join(parsed.args)

    def is_known(self, name: str, chat_type: ChatType) -> bool:
        token = name.strip().removeprefix("/")
        token = token.split(maxsplit=1)[0] if token else ""
        if not token:
            return False
        spec = self._lookup.get(token.casefold())
        return spec is not None and chat_type in spec.scopes

    def help_text(self, chat_type: ChatType, *, is_admin: bool) -> str:
        available: list[CommandSpec] = []
        admin_commands: list[CommandSpec] = []
        for spec in _COMMANDS:
            if chat_type not in spec.scopes:
                continue
            if not spec.admin_only:
                available.append(spec)
            elif spec.name == "forget" and chat_type is ChatType.PRIVATE:
                # 私聊 /forget 对所有人生效（含管理员），放在可用命令里。
                available.append(spec)
            elif is_admin:
                admin_commands.append(spec)

        lines = ["可用命令：" + self._format(available)]
        if admin_commands:
            lines.append("管理员：" + self._format(admin_commands))
        return "\n".join(lines)

    @staticmethod
    def _format(specs: list[CommandSpec]) -> str:
        return " · ".join(f"/{spec.name} {spec.help}" for spec in specs)
