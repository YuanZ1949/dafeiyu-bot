from __future__ import annotations

from pathlib import Path

from dafeiyu.models import ChatType
from dafeiyu.timeutil import today_shanghai

MEMORY_TEMPLATE = "# 大肥鱼的长期记忆\n"
TRUNCATED_TEMPLATE = "<!-- truncated {date} -->"
TRUNCATION_SUFFIX = "（还有 {n} 行未展开）"


class Workspace:
    """机器人自维护的笔记目录：一份全局 MEMORY.md + 每个会话一份 chats/*.md。

    只做文件读写与裁剪，不碰数据库、不碰网络。所有容量参数都以「字符数」或
    「UTF-8 字节数」为单位，调用方负责决定注入多少。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # ---- 路径 ---------------------------------------------------------

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "chats").mkdir(exist_ok=True)
        mem = self.global_path()
        if not mem.exists():
            mem.write_bytes(MEMORY_TEMPLATE.encode("utf-8"))

    def global_path(self) -> Path:
        return self.root / "MEMORY.md"

    def chat_path(self, chat_type: ChatType, chat_id: int) -> Path:
        # chat_id 必须来自平台数字 ID；显式拒绝字符串，堵死 ../ 之类的路径注入。
        if not isinstance(chat_id, int):
            raise TypeError(f"chat_id must be int, got {type(chat_id).__name__}")
        return self.root / "chats" / f"{chat_type.value}-{chat_id}.md"

    # ---- 读取 ---------------------------------------------------------

    def read_segment(self, path: Path, limit_chars: int) -> str:
        """读取一段笔记，保留首行标题，其余从尾部往前按字符预算装入。"""
        path = Path(path)
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return ""
        title, body = lines[0], lines[1:]
        budget = max(0, limit_chars)

        kept: list[str] = []
        used = 0
        partial = False
        for line in reversed(body):
            sep = 1 if kept else 0
            if used + sep + len(line) <= budget:
                kept.append(line)
                used += sep + len(line)
                continue
            room = budget - used - sep
            if room > 1:
                kept.append(line[: room - 1])
                partial = True
            break

        hidden = len(body) - len(kept) + (1 if partial else 0)
        out = "\n".join([title, *reversed(kept)])
        if hidden > 0:
            out += "\n" + TRUNCATION_SUFFIX.format(n=hidden)
        return out

    def read_for(
        self,
        chat_type: ChatType,
        chat_id: int,
        *,
        global_chars: int,
        chat_chars: int,
    ) -> str:
        parts = [
            self.read_segment(self.global_path(), global_chars),
            self.read_segment(self.chat_path(chat_type, chat_id), chat_chars),
        ]
        return "\n\n".join(p for p in parts if p)

    # ---- 容量 ---------------------------------------------------------

    def enforce_limit(self, path: Path, max_bytes: int = 65536) -> bool:
        """超过字节上限时保留标题 + 截断注释 + 尾部行，原地重写并返回 True。"""
        path = Path(path)
        if not path.exists():
            return False
        raw = path.read_bytes()
        if len(raw) <= max_bytes:
            return False

        lines = raw.decode("utf-8", errors="replace").splitlines()
        title = lines[0] if lines else ""
        body = lines[1:]
        comment = TRUNCATED_TEMPLATE.format(date=today_shanghai())
        head = f"{title}\n{comment}\n"
        if len(head.encode("utf-8")) > max_bytes:
            # 极端情况下标题本身就超限：先裁标题，保证结果仍不越界。
            room = max(0, max_bytes - len(f"{comment}\n".encode("utf-8")))
            title = title.encode("utf-8")[:room].decode("utf-8", errors="ignore")
            head = f"{title}\n{comment}\n"

        budget = max(0, max_bytes - len(head.encode("utf-8")))
        kept: list[str] = []
        used = 0
        for line in reversed(body):
            encoded = line.encode("utf-8")
            sep = 1 if kept else 0
            if used + sep + len(encoded) <= budget:
                kept.append(line)
                used += sep + len(encoded)
                continue
            room = budget - used - sep
            if room > 0:
                part = encoded[:room].decode("utf-8", errors="ignore")
                if part:
                    kept.append(part)
            break

        # 用 write_bytes 而不是 write_text：Windows 会把 \n 翻成 \r\n，字节预算会多出 6 个字节。
        path.write_bytes((head + "\n".join(reversed(kept))).encode("utf-8"))
        return True
