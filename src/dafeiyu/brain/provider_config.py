from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import BrainConfig, LocalModelConfig

# 生成物首行的标记：配置写盘前据此判断是不是我们自己的文件。
GENERATED_MARK = "// generated-by: dafeiyu"
# agent 文件是 markdown：标记不能放首行（opencode 只在文件以 `---` 开头时
# 解析前置区），所以它落在闭合 `---` 之后的正文首行。
AGENT_MARK = "<!-- generated-by: dafeiyu -->"
# 判定「是不是生成物」时扫描的行数上限：只覆盖前置区，不扫正文。
AGENT_MARK_SCAN_LINES = 120
AGENT_DESCRIPTION = "Deepseek娘 QQ 群聊人格（可联网查证、可在自己的工作区记笔记）"
# lean 档收尾：模型这一轮没有工具，得让人设自己知道，别去试。
AGENT_LEAN_NOTE = (
    "（本轮没有工具：不要读文件、不要查网页、不要调用任何工具，直接回话。）"
)
FREE_MODEL = "opencode/deepseek-v4.1-flash-free"

# 自备 Key / 本地模型两档都走 OpenAI 兼容 provider 插件，只是端点与驱动方式不同。
PROVIDER_PACKAGE = "@opencode/ai/providers/openai-compatible"
KEY_PROVIDER_ID = "dafeiyu-key"
LOCAL_PROVIDER_ID = "dafeiyu-local"
# 本地端点没写上下文窗口 / 输出预算时的兜底值（面板未覆盖时用）。
DEFAULT_CONTEXT_LIMIT = 32768
DEFAULT_OUTPUT_LIMIT = 8192
MODEL_CAPABILITIES = {"tools": True, "vision": False}
SUPPORTED_MODES = ("free", "key", "local_model")
# 相对配置目录的密钥文件引用；自备 Key 档的默认值（也是该档唯一的意义所在）。
DEFAULT_KEY_REL = "../../../secrets/provider.key"


def normalize_base_url(url: str) -> str:
    """把端点规整成 opencode 能直接吃的 baseURL。

    去首尾空白、去尾部多余的 `/`，并把重复的尾部 `/v1/v1` 收敛成一个 `/v1`
    （面板里手填端点时很容易多粘一段）。其它路径不做改动。
    """
    text = (url or "").strip()
    while text.endswith("/"):
        text = text[:-1]
    while text.endswith("/v1/v1"):
        text = text[: -len("/v1")]
    return text


def _model_entry(context: int | None = None) -> dict[str, Any]:
    """单个模型的 limit / capabilities；两档共用同一形状。"""
    return {
        "limit": {
            "context": context if context else DEFAULT_CONTEXT_LIMIT,
            "output": DEFAULT_OUTPUT_LIMIT,
        },
        "capabilities": dict(MODEL_CAPABILITIES),
    }


def _key_config(cfg: BrainConfig, key_rel: str | None) -> dict[str, Any]:
    """自备 Key 档：只引用密钥文件，绝不把密钥内容写进配置。"""
    model = cfg.key_model
    settings: dict[str, Any] = {"baseURL": normalize_base_url(cfg.key_base_url)}
    if key_rel:
        # key_rel 是相对配置目录的文件引用，不是密钥本身。
        settings["apiKey"] = f"{{file:{key_rel}}}"
    return {
        "model": f"{KEY_PROVIDER_ID}/{model}",
        "plugins": [],
        "providers": {
            KEY_PROVIDER_ID: {
                "package": PROVIDER_PACKAGE,
                "settings": settings,
                "models": {model: _model_entry()},
            }
        },
    }


def _local_config(
    local: LocalModelConfig | None, key_rel: str | None
) -> dict[str, Any]:
    """本地模型档：任意 OpenAI 兼容端点，可选带上本地服务的访问令牌。"""
    local = local if local is not None else LocalModelConfig()
    model = local.model
    settings: dict[str, Any] = {"baseURL": normalize_base_url(local.base_url)}
    if key_rel:
        settings["apiKey"] = f"{{file:{key_rel}}}"
    if local.lean:
        settings["lean"] = True
    context = (
        local.context if isinstance(local.context, int) and local.context > 0 else None
    )
    return {
        "model": f"{LOCAL_PROVIDER_ID}/{model}",
        "plugins": [],
        "providers": {
            LOCAL_PROVIDER_ID: {
                "package": PROVIDER_PACKAGE,
                "settings": settings,
                "models": {model: _model_entry(context)},
            }
        },
    }


class _UnsetKeyRel:
    """哨兵：区分「未传 key_rel」与「显式传 None」。

    自备 Key 档不传 key_rel 时用 `DEFAULT_KEY_REL`（文档化的默认引用）；
    本地档不传时保持不写 apiKey。显式传 None 两档都不写 apiKey。
    """

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试输出
        return "DEFAULT_KEY_REL"


_UNSET_KEY_REL = _UnsetKeyRel()


def build_config(
    mode: str,
    cfg: BrainConfig,
    *,
    key_rel: str | None | _UnsetKeyRel = _UNSET_KEY_REL,
    local: LocalModelConfig | None = None,
) -> dict[str, Any]:
    """按用户可见档位生成 opencode 配置的 JSON 数据。

    三档形状：

    - free：固定免费模型 + 空插件列表，不写 providers；
    - key：`providers.dafeiyu-key`，顶层 model 为 `dafeiyu-key/<cfg.key_model>`，
      密钥只以 `{file:<key_rel>}` 形式引用；省略 key_rel 时用文档化的
      `DEFAULT_KEY_REL`（`../../../secrets/provider.key`）；
    - local_model：`providers.dafeiyu-local`，顶层 model 为
      `dafeiyu-local/<local.model>`，可选 apiKey（本地服务需要令牌时）与 lean；
      本地档不继承 key 档的默认引用。

    两档 provider 的模型条目都带 `limit.context` / `limit.output` / `capabilities`。
    未知档位直接报错，避免悄悄落回免费形状。
    """
    if mode == "free":
        # 免费档不碰用户密钥与本地端点，key_rel / local 一律忽略。
        return {"model": FREE_MODEL, "plugins": []}
    if mode == "key":
        resolved = DEFAULT_KEY_REL if key_rel is _UNSET_KEY_REL else key_rel
        return _key_config(cfg, resolved)
    if mode == "local_model":
        resolved = None if key_rel is _UNSET_KEY_REL else key_rel
        return _local_config(local, resolved)
    supported = " / ".join(SUPPORTED_MODES)
    raise ValueError(f"不支持的大脑模式：{mode}（仅支持 {supported}）")


def render_config(data: dict) -> str:
    """渲染成落盘文本：首行标记 + 缩进 2 的 JSON，末尾带换行。"""
    return f"{GENERATED_MARK}\n{json.dumps(data, indent=2, ensure_ascii=False)}\n"


def config_path(config_home: str) -> Path:
    """bot 专用 opencode 配置路径；只拼路径，不建目录。"""
    return Path(config_home) / "opencode" / "opencode.jsonc"


def _agent_model(mode: str, cfg: BrainConfig, local: LocalModelConfig) -> str:
    """agent 前置区里钉住的模型 id；三档与 build_config 保持一致。"""
    if mode == "free":
        return FREE_MODEL
    if mode == "key":
        return f"{KEY_PROVIDER_ID}/{cfg.key_model}"
    if mode == "local_model":
        return f"{LOCAL_PROVIDER_ID}/{local.model}"
    supported = " / ".join(SUPPORTED_MODES)
    raise ValueError(f"不支持的大脑模式：{mode}（仅支持 {supported}）")


def _agent_permissions(lean: bool) -> list[str]:
    """agent 的 permissions 段：通配 deny 打底，白名单逐项开例外。

    顺序不能动：规则是「最后匹配者胜」，所以 deny 通配必须在最前，
    密钥文件的显式 deny 必须排在白名单之后。
    """
    lines = [
        "permissions:",
        "  # 通配 deny 必须放最前：规则是「最后匹配者胜」，后面才能用 allow 开例外。",
        "  # 逐项写 read/shell 会漏掉 MCP 工具（ast_* 之类），将来接上就漏。",
        '  - action: "*"',
        '    resource: "*"',
        "    effect: deny",
        "  # 读模型目录只是为了知道有哪些模型可选，不碰别处。",
        "  - action: read",
        '    resource: "~/.cache/opencode/models.json"',
        "    effect: allow",
    ]
    if not lean:
        lines += [
            "  # 长期记忆工作区：只能读写这一个目录。故意不开枚举类工具，",
            "  # 否则等于允许翻出 chats/ 下所有群与私聊的笔记。",
            "  - action: read",
            '    resource: "data/bot-workspace/**"',
            "    effect: allow",
            "  - action: edit",
            '    resource: "data/bot-workspace/**"',
            "    effect: allow",
            "  - action: webfetch",
            '    resource: "*"',
            "    effect: allow",
        ]
    for action in (
        "shell",
        "websearch",
        "skill",
        "subagent",
        "question",
        "external_directory",
        "execute",
    ):
        lines += [
            f"  - action: {action}",
            '    resource: "*"',
            "    effect: deny",
        ]
    lines += [
        "  # 密钥文件显式再堵一遍：将来白名单放宽时它们也不该被读到。",
    ]
    for resource in (
        ".env",
        "data/secrets/*",
        "data/*.password",
        "data/*.token",
        "data/dashboard.password",
        "data/supervisor.token",
    ):
        lines += [
            "  - action: read",
            f'    resource: "{resource}"',
            "    effect: deny",
        ]
    return lines


def build_agent(
    mode: str,
    *,
    lean: bool,
    template: str,
    cfg: BrainConfig | None = None,
    local: LocalModelConfig | None = None,
) -> str:
    """把 persona 模板渲染成 opencode agent 文件。

    `.opencode/agents/<name>.md` 是唯一真正生效的一层（model 与 permissions
    都以它为准），所以这里把它当生成物写：前置区按档位钉模型，权限固定为
    「通配 deny + 白名单」，正文原样取自 `agent_template.md`。

    布局契约：文件第一行必须是 `---`（opencode 只在这种情况下解析前置区，
    否则 model/permissions 整段失效、回落到默认值），因此 `AGENT_MARK` 落在
    闭合 `---` 之后的正文首行，再空一行接人设正文。

    lean=True 时去掉 read/edit 与 webfetch 白名单（工作区与联网都不开），
    并在正文末尾补一句「本轮没有工具」；deny 通配与密钥 deny 保留。
    """
    cfg = cfg if cfg is not None else BrainConfig(mode=mode)
    local = local if local is not None else LocalModelConfig()
    model = _agent_model(mode, cfg, local)
    head = [
        "---",
        f"description: {AGENT_DESCRIPTION}",
        "mode: primary",
        "# 4 步给「查网页 → 读结果 → 写笔记 → 出 JSON」留余量；最后一步仍会移除工具。",
        "steps: 4",
        "# 钉住模型：全局默认变了也不该静默改变 bot 行为。",
        f"model: {model}",
        *_agent_permissions(lean),
        "---",
        # 标记放在前置区之后：文件必须以 `---` 开头，opencode 才解析 model/permissions。
        AGENT_MARK,
    ]
    body = template if template.endswith("\n") else f"{template}\n"
    if lean:
        trimmed = body.rstrip("\n")
        body = f"{trimmed}\n\n{AGENT_LEAN_NOTE}\n"
    return "\n".join(head) + "\n\n" + body


def _looks_generated(text: str) -> bool:
    """前置区范围内出现我们的标记就算生成物。

    两种标记都必须认：agent 用 HTML 注释（不能放首行，否则 opencode 不解析
    前置区），jsonc 配置用 `//` 首行（`render_config` 的输出）。只认一种会让
    另一类生成物每次都被当成手改件、白备份一遍。

    标记在正文首行（前置区之后），不再位于文件开头，所以不能只用 startswith。
    窗口取 120 行而不是 60：permissions 有 14 条 deny/allow（每条 3 行），
    整个前置区就有 69 行，标记落在第 70 行；窗口太窄会把自家文件当成手改件、
    每次运行都白备份一遍。窗口仍只覆盖前置区，不会把正文里的偶发同名字串
    误判成生成物。
    """
    markers = (GENERATED_MARK, AGENT_MARK)
    return any(
        line.strip() in markers for line in text.splitlines()[:AGENT_MARK_SCAN_LINES]
    )


def write_generated(path: Path, content: str) -> bool:
    """写生成物，返回是否备份过用户手改的旧文件。

    - 文件不存在：直接写，False；
    - 内容一致：不写，False（纯幂等）；
    - 旧文件前 60 行里有我们的标记：直接覆盖，False；
    - 其余情况（用户手改过）：先复制成 `<name>.bak.<YYYYmmdd-HHMMSS>` 再写，True。
    """
    target = Path(path)
    existed = target.exists()
    old: str | None = None
    if existed:
        try:
            old = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 读不出来的旧文件一律当手改过：备份比覆盖安全。
            old = None
        if old == content:
            return False
    target.parent.mkdir(parents=True, exist_ok=True)
    backed_up = False
    if existed and not _looks_generated(old or ""):
        # 备份名用本地时间：用户在看目录时对得上自己刚改的时间。
        stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(target, target.with_name(f"{target.name}.bak.{stamp}"))
        backed_up = True
    target.write_text(content, encoding="utf-8", newline="\n")
    return backed_up


def agent_path(root: Path) -> Path:
    """bot 工程里的 agent 文件路径；只拼路径，不建目录。"""
    return Path(root) / ".opencode" / "agents" / "deepseek-qq.md"
