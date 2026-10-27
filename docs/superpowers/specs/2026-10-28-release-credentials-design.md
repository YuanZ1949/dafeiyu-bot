# 发布版凭据与本地模型接入设计（三档模式）

- 日期：2026-10-28
- 状态：待评审
- 关联：发布化改造（`3363ca3`）、启动/停止脚本化（`aba7c38`）

## 1. 背景

仓库已具备发布形态（模板配置、`init`/`doctor`、`setup.ps1`、LICENSE），但发给别人仍然撞三堵墙：

1. **opencode 要自己装**。`resolve_opencode_bin` 只找 PATH 与 npm 全局目录，而终端用户可能连 Node.js 都没有。opencode 本体是 196.3 MB 的 bun 单文件 exe（本机实测 `opencode v2.0.18`），运行时不需要 Node。
2. **凭据无处可填**。`config.yaml` 里没有 key 字段（设计如此），`.env` 要手写，而 opencode 自己的 `auth.json` 在 `%USERPROFILE%\.local\share\opencode\`——发布包不携带，用户也不知道该动它。
3. **本地模型只能在 `config.yaml` 里配**，面板只能选模型，改不了地址/密钥/是否自动拉起。

## 2. 目标

对方拿到 zip、跑一个脚本之后：

- 不需要 Node.js，不需要 npm，不需要手工下载 opencode，也不需要手工下载 NapCat；
- 默认能直接聊（免费模型），不需要任何凭据；
- 想用自己的 key 时，在面板里填三项即可；
- 想用本地模型时，在面板里填服务地址，能探测模型列表，选完就能用；
- 三种模式随时切换，改完立即生效，不重启 supervisor；
- 出问题时能一条命令导出诊断包，不必让他截图翻日志。

## 3. 非目标

- 不做 pip 包化（6 处 `REPO_ROOT = parents[3]` 依赖 editable 安装，属跨模块重构）。
- 不做跨平台（`icacls`、`taskkill`、`pythonw`、`node.exe` 都是 Windows 假设）。
- 不做单文件 exe / 开机自启 / CHANGELOG。
- 不改动 `C:\MyFile\AICoding\dafeiyu-release`（现有发布副本），本设计落地并验证后再单独部署。
- 不自动迁移旧数据（历史合成 `message_id` 不回填）。

## 4. 三档模式

三档统一由 **opencode** 当思考者，bot 侧只负责拼提示词、投递消息与存记忆。

| | ① 免费版（默认） | ② 自备 Key | ③ 本地模型 |
|---|---|---|---|
| 推理来源 | `opencode/deepseek-v4.1-flash-free` | 用户填的 OpenAI 兼容服务 | 用户填的本地 OpenAI 兼容服务 |
| 用户要填 | 无 | baseURL + model + apiKey | baseURL + model（key 可选） |
| 工具 / 看图 | 有 | 有 | 有（依赖模型能力声明） |
| 费用 | opencode 免费额度 | 用户自己的账单 | 电费 |
| 失效风险 | 免费额度取消 | 服务商改协议 | 上下文太小 |

注："看图/工具"在 ③ 档取决于模型能力声明，而不是端点：LM Studio / Ollama 的发现接口会上报 vision 与 tools；vLLM 的发现接口**不上报**，必须在生成的 provider 配置里显式写 `capabilities`（见 §7.3），否则等于没有。

**降级提示**：① 档在启动自检里探测免费模型是否可用，不可用时面板顶栏出一条黄色提示「免费模型当前不可用，请切换到自备 Key 或本地模型」，不阻塞启动。

## 5. 架构

### 5.1 配置层级

```
config.yaml（仓库模板 → 首次 init 落地，作为默认值；不存机密）
  ↓ 覆盖
data/settings.local.yaml（面板写入的非机密偏好：档位、baseURL、模型名、精简模式…）
  ↓ 机密
data/secrets/provider.key（API key，icacls 加固，仅当前用户可读）
  ↓ 生成
data/opencode-runtime/config/opencode/opencode.jsonc（opencode 实际读取的配置，由前两者生成）
```

- `data/settings.local.yaml` 与 `data/secrets/` 全部落在 `data/`（已 gitignore）。
- 机密文件沿用 `supervisor/control_token.py` 的加固方式（`icacls <path> /inheritance:r /grant:r <user>:(R,W)`），失败只告警不阻断。
- 生成文件带标记头 `// generated-by: dafeiyu`。`ensure_isolated_config` 当前语义是"已存在则一字不改"，本设计改为：
  - 内容等于旧 `MINIMAL_CONFIG`（或带生成标记）→ 直接重写；
  - 其他内容 → 备份为 `opencode.jsonc.bak.<时间戳>` 后重写，并在 `doctor` 里提示"检测到手工配置，已备份"。

### 5.2 运行时挂点（已存在，无需新增）

`brain/runtime_config.py::server_env()` 同时注入 `XDG_CONFIG_HOME` 与 `OPENCODE_CONFIG`，后者优先级更高。生成三档配置后，bot 重启时 opencode 自然读到新配置。

**隔离有效性已实测**（2026-10-28，`opencode v2.0.18`，`--standalone --print-logs --log-level debug`）：

| 变量 | 效果 | 依据 |
|---|---|---|
| `OPENCODE_CONFIG` | **决定配置来源**：设为临时文件后，日志不再监听 `%USERPROFILE%\.config\opencode`，也不再加载用户插件（`yzOpencode` / `OpencodeLiberation` / `oceanus`） | 决定性证据 |
| `XDG_CONFIG_HOME` | 单独设置**不够**——日志仍会同时监听用户全局目录并加载其插件 | 反例 |
| `XDG_DATA_HOME` | 只搬 `opencode.db`、`opencode.db-wal` 与 `log/opencode.log`，不影响配置 | 两次独立验证 |
| `auth.json` | **不在隔离范围**：`opencode auth list` 走的是已运行的共享后台服务，改 `HOME`/`USERPROFILE` 都测不出隔离 | 本设计不依赖它 |

结论：生产里两者都设（`server_env()`），所以**用户的全局 opencode 配置与插件不会污染发布版**。附带约束：凡是需要"读取我们自己实例状态"的检查，一律走 HTTP 直连我们自己的 `base_url`，**不要用 `opencode` CLI 子命令**——后者可能附到用户已运行的共享服务上，读到别人的配置。

热切换：`control.py::switch()` 走 `/api/brain`（面板 → supervisor → 控制口），已有 3 档切换骨架。本设计把 `token` 档改为"自备 Key（opencode）"，不再走 `BrainClient`。

### 5.3 面板

现有"大脑"卡片三选一（`index.html:729` `MODE_ORDER`），改造为：

- 三张模式卡，带一行说明（免费额度 / 用自己的 key / 本地跑）。
- ② 档字段：baseURL、model、apiKey（密码框，回显打码，留空表示不修改）。
- ③ 档字段：服务地址、模型下拉（探测按钮）、key（可选）、上下文长度（探测不到时手填）。
- 保存 → `POST /api/brain`（扩展字段）→ 写覆盖层 → 重新生成两件产物 → 重启 opencode → 清会话 → 返回当前生效配置（完整链路见 §7.5）。

## 6. P1 安装器与发行包

**发行 zip 只含代码**，且清单必须可执行而不是靠人记。新增 `scripts/package.ps1` 做白名单打包，包含：

```
src/  scripts/  .opencode/  config.example.yaml  .env.example  .gitignore
pyproject.toml  README.md  LICENSE
```

排除（deny 优先）：

```
data/  logs/  vendor/  .venv/  config.yaml  .env
tests/  docs/superpowers/  __pycache__/  .pytest_cache/
```

体积实测：`src/` 1.6 MB、`scripts/` <0.1 MB、`.opencode/` <0.1 MB，代码包约 **2 MB 级**（不是先前写的 30 MB —— 30 MB 是 `data/` 的体积，已排除）。`tests/`（3.7 MB）与 `docs/superpowers/`（开发过程产物）不进发行包。

**两个清单项是硬需求，漏了首装必失败**：

- `.opencode/` —— agent 定义（`deepseek-qq.md`）是运行必需的；PR 里没有任何其他地方提供它。
- `.env.example` —— `scripts/setup.ps1:38` 无条件 `Copy-Item .env.example .env`，且脚本设了 `$ErrorActionPreference = "Stop"`，缺文件即整段中断。

NapCat 与 opencode 都由安装脚本在用户机器上下载，理由：

- `vendor/napcat/napcat/` 内**没有 LICENSE 文件**，再分发依据不足；
- `vendor/napcat` 占 345 MB，是当前 377 MB 包体的 91%，用户的下载体验极差；
- opencode.exe 单个 196.3 MB，同理。

代价是首次安装必须联网。安装脚本对"下载失败"要给出代理参数与手动下载地址，不能只报红；同时支持"用户自行把 `NapCat.Shell.Windows.Node.zip` / `opencode.exe` 放到指定目录后重跑"，靠幂等跳过完成安装。

### 6.1 NapCat（已有脚本，纳入单一入口）

`scripts/setup-napcat.ps1` 已能从 GitHub release 下载 `NapCat.Shell.Windows.Node.zip`（当前硬编码 `v4.18.41`，支持 `-Proxy`），并自动写 `onebot11_<BOT_QQ>.json`。本设计不重写它，但**必须拆成两个可独立重入的阶段**：

1. **下载解压**（可跳过）：幂等条件从"`napcat.bat` 存在"改为"`napcat.bat` 与 `node.exe` 都存在，且 `node.exe --version` 成功"——否则解压中断过一次就永远跳过。
2. **写 OneBot 配置**（每次必做）：读 `.env` 的 `BOT_QQ` 生成 `onebot11_<BOT_QQ>.json`。这是纯本地的文件写入、可重入，所以**不受下载幂等影响**；否则用户先装 NapCat、后填 QQ，配置永远补不上。

另外两条：

- 版本过期时打一条提醒（不自动升级，避免把用户环境升坏）；
- 下载后探活一次，失败给出重跑命令。

### 6.2 opencode

新增 `scripts/setup-opencode.ps1`，照 `setup-napcat.ps1` 的结构：

- 参数：`-Version latest`、`-Proxy`、`-Force`；
- 目标：`vendor/opencode/opencode.exe`；
- 已存在且 `--version` 可执行 → 跳过（幂等）；
- 下载后校验：文件大小 > 100 MB、`opencode.exe --version` 输出包含 `v`；
- 失败给出可重试的明确提示（代理、手动下载地址）。

下载源优先官方 standalone 安装脚本的直链（Windows x64）。**实施前必须确认再分发许可与直链形式**，若不允许内置，则本脚本只做"安装时下载"，不放进 zip。

### 6.3 解析顺序

`brain/opencode.py::resolve_opencode_bin` 增加候选，顺序为：

1. `config.yaml` 的 `opencode.bin`（显式指定，最高优先）
2. `INSTALL_ROOT/vendor/opencode/opencode.exe`（新）
3. PATH 上的 `opencode.exe`
4. `%APPDATA%/npm/node_modules/@opencode/cli/bin/opencode.exe`
5. `npm root -g` 下的同名路径

### 6.4 串联

`scripts/setup.ps1` 从 4 段扩展为 **9 段**，顺序按依赖重排（`.env` 必须早于 NapCat）：

0. **Python 预检**：`python --version` 存在且 ≥3.11，识别 Microsoft Store 别名占位，失败给下载链接并退出（`python -m venv` 是第 1 段的第一句，之前没有机会报错）
1. venv
2. 依赖（`pip install -e .`）
3. `init` 配置（`config.yaml` 从模板生成，不覆盖已有文件）
4. `.env` —— 缺文件时进入**首次向导**（见 §13.1），产出 `BOT_QQ` 与默认档位
5. **NapCat 下载解压**（幂等；已就位则跳过）
6. **NapCat OneBot 配置**（每次必做，读第 4 段的 `BOT_QQ`）
7. **opencode**（`setup-opencode.ps1`，幂等）
8. `doctor`

另提供 `scripts/setup.bat`（照 `start.bat:6` 的写法包一层 `-ExecutionPolicy Bypass`），避免用户被 PowerShell 执行策略拦住。

`doctor` 增加一条：找不到 opencode 时给出 `scripts\setup-opencode.ps1` 的命令行，而不是 "npm i -g"。

## 7. P2 凭据三档

### 7.1 覆盖层格式

`data/settings.local.yaml`（下称"覆盖层"）由面板写入，`load_settings()` 合并它（见 §7.6）：

```yaml
brain:
  mode: free            # free | key | local_model
  key_mode:
    base_url: ""
    model: ""
  local_model:
    base_url: ""
    model: ""
    context: 32768
    output: 8192
    lean: false
```

**命名约束**：新模式值必须用 `local_model`，**不能用 `local`**——`brain/factory.py:20-22` 与 `runtime.py:41-42` 的 `normalize_provider()` 把 `"local"` 归一化成 `"token"`，用 `local` 会让本地档被静默解析成 DeepSeek 直连档。

两个字段的从属关系写清楚，避免打架：

- `brain.provider`（现有，`opencode | token | local_model`）= **底层思考者**，发布版固定 `opencode`；
- `brain.mode`（新增）= **用户可见的三档**，只决定喂给 opencode 的 provider 配置；
- 模板默认从 `provider: token` 改为 `provider: opencode` + `mode: free`（见 §12 的 P1 验收）。

### 7.2 唯一生效层：agent 文件（关键修正）

**这是本设计最容易做错的一点。** opencode 的配置合并顺序是 `opencode.jsonc → .opencode/agents/*.md`，agent 层最后应用；`permissions` 规则追加进数组、最后匹配者胜。

现有 `.opencode/agents/deepseek-qq.md:7` 钉死 `model: opencode/deepseek-v4.1-flash-free`，`:8-49` 定义了整套 permissions。因此**只写 `opencode.jsonc` 既切不了模型也改不了权限**：免费档看起来正常（agent 钉的就是免费模型），一旦切到 ②/③ 跑的还是免费模型，而且不报错。

结论：**agent 文件是 model 与 permissions 的唯一生效层**，由 `provider_config.py` 一并生成：

| 产物 | 路径 | 内容 |
|---|---|---|
| provider 配置 | `data/opencode-runtime/config/opencode/opencode.jsonc` | `$schema`、`plugins: []`、`providers`、`permissions`（与 agent 一致，作兜底） |
| agent 定义 | `.opencode/agents/deepseek-qq.md` | front-matter（`model` + `permissions`）+ 人设正文 |

- 人设正文抽到模板 `src/dafeiyu/brain/agent_template.md`，`provider_config` 只替换 front-matter，正文原样搬运（避免人设改动散落两处）。
- 两件产物都带 `generated-by: dafeiyu` 标记；遇到非生成态内容先备份 `.bak.<时间戳>` 再覆盖。
- **切换档位后必须清空会话映射**（`store.clear_opencode_session()`）：`/v2/docs/models` 明确"会话已选模型优先于配置默认值"，不清就会被旧会话拖住。

### 7.3 生成规则

`brain/provider_config.py`（新增）把 (mode, 覆盖层, secrets) 转成上面两件产物：

- `free`：`model: "opencode/deepseek-v4.1-flash-free"`，不写 `providers` 段。
- `key`：写自定义 provider `dafeiyu-key`，`package: "@opencode/ai/providers/openai-compatible"`，`settings.baseURL`，`settings.apiKey`，`models.<model>` 带 `capabilities` 与 `limit`。
- `local_model`：同 `key`，provider 名 `dafeiyu-local`，`apiKey` 可选（无 key 时不写）。

三条硬规则：

1. **`limit.context` 必须显式写**。opencode 对未声明模型按 200k/32k 假设（`/v2/docs/models`），会把 20k 提示词发给 8k 模型。
2. **`limit.output` 与 `capabilities` 一起写**（`tools` / `input` / `output`）。vLLM 的发现接口不上报 tools/vision，不显式声明就等于没有工具，与 §4 的表格矛盾。
3. **`{file:}` 必须用相对路径**（`../../../secrets/provider.key`）。官方只保证相对路径与 `/`、`~` 开头的形式，`C:\...` 不在保证范围且要在 JSONC 里转义。`doctor` 加一条校验：生成后 apiKey 解析结果非空且不以 `{file:` 开头。

### 7.4 工具收口

沿用 agent 文件里**已验证有效**的结构——`*` deny 打头 + 白名单 allow（`*` 放最前是必须的，因为最后匹配者胜）：

```jsonc
"permissions": [
  { "action": "*", "resource": "*", "effect": "deny" },
  { "action": "read",  "resource": "data/bot-workspace/**", "effect": "allow" },
  { "action": "edit",  "resource": "data/bot-workspace/**", "effect": "allow" },
  { "action": "webfetch", "resource": "*", "effect": "allow" },
  { "action": "read",  "resource": "~/.cache/opencode/models.json", "effect": "allow" }
]
```

要点：

- **绝不写 `read "*" allow` / `edit "*" allow`**。仓库根目录同时有 `.env`（key + 面板口令）、`data/secrets/provider.key`、`data/dashboard.password`、`data/supervisor.token`，全开等于给模型一个改写凭据的通道。
- 所有 action 都必须被显式覆盖（含 `shell`、`execute`、`external_directory`、`skill`、`subagent`、`question`、`websearch`）。无匹配规则默认 `ask`，而 bot 侧没有审批处理者，`ask` 在 headless 下等于挂到超时。
- 三档同一套规则；`lean` 只做减法（关工具），不做加法。

### 7.5 生效链路（改完立即生效）

opencode 子进程的环境与配置由 supervisor 在启动时确定（`supervisor/main.py:148-162`），而 `control.switch()` 只重建 bot 侧 brain，不碰 opencode 进程。所以完整链路是：

```
面板保存
  → POST /api/brain（supervisor 面板）
  → 校验 + 探活（直连用户填的 baseURL，不走 opencode CLI）
  → 写覆盖层 data/settings.local.yaml + data/secrets/provider.key
  → provider_config 重新生成（opencode.jsonc + agent md）
  → supervisor 重启 opencode 子进程（新增 POST /api/opencode/reload）
  → 等 /api/info ready（复用 startup_timeout_seconds）
  → bot 侧清空会话映射 + build_brain
  → 返回当前生效配置
```

`ensure_isolated_config`（`runtime_config.py:33-49`）从"已存在一字不改"改为"**只有带生成标记才重写**"；启动期不得再用 `MINIMAL_CONFIG` 覆盖生成结果，否则每次重启都会把用户档位打回免费。

### 7.6 覆盖层接入点

覆盖层必须在 `load_settings()` 内部合并——它是 bot、supervisor、`start-all.ps1`、`doctor` 的唯一配置入口（`config.py:701`），放在外面会导致"面板显示已切换、doctor 还按老配置报错"。

现存冲突：`data/runtime.json` 的 `runtime.provider` 优先级高于 `config.yaml`（`factory.py:25-26`）。处置：

- `switch()` 写覆盖层时**清掉 runtime.json 里的 provider/model**（保留文件本身，只作兼容读）；
- 覆盖层是模式配置的唯一真相源，`runtime.json` 不再承担"切档"职责。

## 8. P3 本地接入

### 8.1 探测

面板"刷新模型"→ `GET <baseURL>/models`：

- 能拿到 `data[].id` 列表；
- 上下文长度按来源分级：LM Studio / Ollama 的发现接口能报 context（若走 opencode 内置 `lmstudio`/`ollama` provider 则自动获得）；vLLM 用 `max_model_len`；通用端点通常不报 → 面板让用户手填，默认 8192 并标注"未探测到"。

### 8.2 上下文门槛（口径与推论）

**测量口径必须先固定，否则数字不可复现。** 现有数据来自 `data/opencode-bot.db` 的 `session_v2.tokens_input`（20 个有 usage 的会话，opencode 档、transport=http）：p10 = 2,597、中位数 = 11,336、p90 = 21,062、最大 = 26,248，对应输出只有 48～1,140。

已知口径不自洽：文本固定开销（`deepseek-qq.md` 8,275 字节 + persona 5,862 字节 ≈ 4–5k tokens）大于 p10 的 2,597，说明 p10 那批会话要么没带完整系统提示，要么不是同一条链路。因此：

- 本设计的阈值**不以绝对值背书**，而是以 p90 为锚做比例：黄线 = p90 × 0.75 ≈ 16k，绿线 = p90 × 1.5 ≈ 32k；
- P3 实施第一步要**重测一次**并记录口径（哪张表、哪些会话、是否含 system + 工具定义），把数字更新进本节；
- 面板显示按三色，但文案写明"按经验值估计"。

| 上下文 | 判定 | 面板表现 |
|---|---|---|
| < 16k | 红 | 允许保存，强提示"基本不可用" |
| 16k–32k | 黄 | 提示"会频繁压缩上下文" |
| ≥ 32k | 绿 | 正常 |

### 8.3 精简模式（`lean: true`）

目标是给 8k–16k 的机器留活路，但**它不是免费的**。逐项定义（每一格都要落到具体代码位置）：

| 维度 | 正常模式 | lean 模式 | 落点 |
|---|---|---|---|
| 工具 | 开（工作区读写 + webfetch） | **全关**（permissions 只留 `*` deny） | 生成的 agent md front-matter |
| 看图 | 有（图片物化后在 content 里给相对路径，靠 `read` 打开） | **没有**——关工具就丢了看图 | 同上 |
| 人设 | agent 正文全文 | 只保留前 N 行（默认 40 行） | `agent_template.md` 截断 |
| 记忆注入 | 完整（timeline + 印象） | 只注入摘要 | `memory/context.build_injection` 开关 |

必须在面板上直说：**lean 下她看不到图片，也不能查网页**。否则用户会以为"选个精简"只是省点钱。

顺带修一个现存陷阱：`persona.prompt_file` 在 opencode 档**本来就不生效**（`opencode_http.py:255` 的 `reply` 不收 system，`service.py:1094-1108` 只给接受 system 的 brain 注入）。人设真正生效的位置是 agent md 正文，本设计把它显式化。

## 9. 错误处理与降级

| 情况 | 行为 |
|---|---|
| 免费模型不可用 | 顶栏黄色提示，不阻塞；面板提供切换到 ② 的按钮 |
| 自备 key 401/403 | 保存时立即探活一次，失败不落盘，返回明确错误 |
| 本地服务连不上 | 保存时探活失败 → 提示"地址不可达"，允许强制保存 |
| 上下文未探测到 | 默认 8192 + 黄色标注，允许手填覆盖 |
| 机密文件写入失败 | 只告警，内存生效，提示"重启后失效" |
| 生成配置与手工配置冲突 | 备份 `.bak.<时间戳>` 后覆盖，`doctor` 提示 |
| 二进制下载失败 | 打印 `-Proxy` 用法与手动下载地址；检测到 `vendor/opencode/opencode.exe` 已就位时跳过下载 |

## 10. 测试计划

单元：

- `provider_config` 三档生成的**两件产物**都要断言：`opencode.jsonc`（含 `providers`、`limit.context`/`limit.output`、`capabilities`、`{file:}` 相对路径、`plugins: []`）与 **agent md 的 front-matter**（`model` 三档各自正确、permissions 白名单与 `*` deny 打头）。
- `resolve_opencode_bin` 的候选顺序（用 `tmp_path` 造 vendor 目录）。
- 覆盖层读写：默认值 → 局部覆盖 → 回退；`load_settings()` 合并后四个消费者看到同一结果。
- 模式枚举归一化：`free | key | local_model` 各自映射正确，且 **`local` 不会被当成合法模式值**（防 B6 回归）。
- 机密文件写入：mkstemp → 写 → 加固 → replace 的顺序被调用；加固失败不阻断但有可读提示。
- 上下文分级函数（<16k 红 / 16–32k 黄 / ≥32k 绿）。
- `ensure_isolated_config` 的三种情况（旧 MINIMAL 覆盖、带标记覆盖、手工内容备份）。
- `scripts/package.ps1` 的清单：`.opencode/`、`.env.example` 必须在内，`data/`、`vendor/`、`tests/` 必须在外（用一条真实目录跑并比对文件列表）。

集成：

- 面板 `POST /api/brain` 三档切换 → 覆盖层与两件产物内容正确 → opencode 重启被调用 → 会话映射被清空。
- 本地档探测失败 → 返回可读错误，覆盖层不被污染。
- `setup.ps1` 顺序：缺 `.env` 时 NapCat 的"写 OneBot 配置"阶段能拿到向导刚写入的 `BOT_QQ`（模拟：临时目录 + 预置 `.env`）。

手动活体（部署前）：

1. 干净目录：`setup.bat` 九个阶段全通 → `doctor` 只剩预期项；
2. 免费档对话一次，确认能看图（发一张图，模型能描述）；
3. 切自备 key：错 key 立即报错且不落盘；正确 key 能聊，**日志里出现的是新模型名**（验证 B3 修复：不能只换配置不换模型）；
4. 本地档接 LM Studio 与一个非 LM 的 OpenAI 兼容端点，各跑一轮带工具的对话；
5. `lean: true` 后再问同一句：确认工具消失、看图能力消失（预期行为，不是 bug）。

## 11. 风险与未决

1. **免费模型可用性**：用户确认免费模型**无需登录、可直接调用**（本机长期在用 `opencode/deepseek-v4.1-flash-free`，且 `auth.json` 里没有 `opencode` 条目）。剩余风险是**上游变更**：opencode 升级或取消免费额度（`OPENCODE_DISABLE_FREE_MODELS` 开关存在）。缓解：§4 的降级提示（探测失败 → 顶栏黄条 + 一键切档），发布说明标注"依赖 opencode 免费额度"。
2. **opencode 再分发许可**：若不支持内置，P1 只能是"安装时下载"（本设计已默认如此），zip 内不含 exe。
3. **项目目录与图片路径**：bot 目前把图片写到 `data/bot-workspace/images/`，提示词里给的是相对路径。若为收口把 `opencode.project_dir` 改成 `data/bot-workspace`，相对路径会失效——实施前必须核对图片注入方式，再做决定（默认本次不改 `project_dir`，只用 `permissions` 收口）。
4. **包体**：发行 zip 只含代码（~30 MB）；NapCat（345 MB）与 opencode（196 MB）改为安装时下载。
5. **下载源的可用性**：NapCat 走 GitHub release、opencode 走官方直链，国内网络可能超时。两个安装脚本都必须支持 `-Proxy`，失败时打印手动下载地址，并允许用户把 zip/exe 放到指定目录后重跑（幂等跳过）。
6. **`setup-napcat.ps1` 的版本是硬编码的**（`$Version = "v4.18.41"`）。NapCat 升级到不兼容 OneBot 反向 WS 的版本会直接影响可用性，因此本设计不自动升级，只在 `doctor` 里提示"当前版本低于最新"。

## 12. 交付顺序与回滚

1. **P1 安装器与发行包**。验收不是"doctor 全绿"，而是**doctor 只剩与档位无关的项**：本阶段必须同时把模板默认从 `provider: token` 改为 `provider: opencode` + `mode: free`，否则 `config.py:844` 在无 `DEEPSEEK_API_KEY` 时直接抛 `ConfigError`，`doctor` 永远返回 1，P1 无法独立验收。
2. **P2 三档配置生成 + 面板 + 机密文件**（免费档与自备 key 可用）。含 B3 修复：agent md 生成 + 切换清会话。
3. **P3 本地接入 + 精简模式**（第一步重测上下文口径，见 §8.2）。
4. **P4 产品化包**（向导 / 诊断包 / 升级脚本 / 口令可见性）。
5. 全量测试绿 → 部署到 release 副本 → 活体验证。
6. 回滚：停副本 → 旧目录 `scripts\start.bat`；复制前保留 `data/` 备份。

每阶段的产出都必须能独立验证（有新测试、有可执行的验收命令），不允许"三个包一起做完才知道行不行"。

## 13. P4 产品化包

### 13.1 首次向导

现状：`setup.ps1` 发现缺 `.env` 时只是复制模板并退出，用户还得自己打开文件找 `BOT_QQ=`。

改为交互式四问（每问都有默认值，回车即用默认）：

1. 「机器人 QQ 号」→ 校验纯数字（10–11 位）→ 写 `BOT_QQ=`
2. 「用免费模型 / 自己的 key / 本地模型」→ 默认免费；选后两者时提示"稍后可在面板里填"
3. 「面板只给本机用，还是也给局域网用」→ **默认局域网**（保留既有产品决定，见下）
4. 「确认」→ 写 `.env`，并把面板口令打印一次

关于第 3 问：面板默认仍然是 `0.0.0.0 + 强制登录`（这是你此前明确要求的"默认给局域网提供服务"），代价是首发会撞登录墙。缓解不靠改默认值，靠**口令可见**：向导与 `start.bat` 都会把自动生成的口令打印到控制台（见 §13.5），登录页上也加一句"首次启动的口令在启动窗口里"。

非交互环境（`-NonInteractive` 或检测不到控制台）自动降级为"复制模板 + 打印下一步"，绝不阻塞。

### 13.2 诊断包 `scripts/diag.ps1`

一条命令产出 `data/diag/diag-<时间戳>.zip`，内含：

- `version.txt`：`dafeiyu` 版本、Python 版本、opencode 版本、NapCat 版本、Windows 版本
- `doctor.txt`：`dafeiyu doctor` 的完整输出
- `logs/`：`logs/dafeiyu.log`、`logs/supervisor/*.log`（各保留最后 2000 行）
- `config.redacted.yaml`：`config.yaml` 的脱敏副本（key/password/token 字段替换为 `***`）
- `env.keys.txt`：**只列键名**，不列值

脱敏复用现成实现，不要在 PowerShell 里重写正则：`supervisor/redact.py` + `supervisor/logs.py:59-61` 已经是 supervisor 日志的单一脱敏入口。因此诊断包做成 **`dafeiyu diag` 子命令**（Python 侧），`scripts/diag.ps1` 只是薄包装。

两处必须特殊处理：

- `logs/dafeiyu.log` 是裸 `FileHandler`（`__main__.py:106-117`），**没有走脱敏**；打包前对它单独跑一遍 `redact`。
- 打包用 **deny 列表**而不是"没列进去"：显式排除 `data/secrets/`、`data/dashboard.password`、`data/supervisor.token`、`data/*.db*`、`.env`、`config.yaml`（只放脱敏副本）。

`diag.ps1` 产出的 zip 里不应出现任何形如 `sk-`、`Bearer ` 的字符串——实施时加一条自动校验（打包后扫一遍，命中即删除该文件并报错）。

### 13.3 版本可见（已具备，只需验证）

核实结果：`control.py:455` 的 health 快照已带 `version`，面板 `index.html:1250` 已渲染 `v0.1.0`。本设计不新增功能，只补：

- 一条测试断言 `dafeiyu.__version__` 与 `pyproject.toml` 的 `version` 一致；
- 发布说明里写明"面板右上角可看到版本号"；
- `dafeiyu --version` 子命令（若 `argparse` 尚未提供）。

### 13.4 升级脚本 `scripts/update.ps1`

1. 读 `data/supervisor.pid`，若在运行 → 调 `stop.ps1`
2. 拉取指定版本（发行方式二选一：zip 覆盖 / `git pull` —— **实施前必须先定**，脚本第 2 步完全依赖它）
3. 保留 `data/`、`config.yaml`、`.env`、**`vendor/`**，只替换代码
4. `pip install -e .` 刷新依赖
5. `doctor` → 若非 0 则**不启动**，回滚提示
6. `start.bat`

`vendor/` 必须在保留清单里：NapCat 的登录态与缓存就在 `vendor/napcat/` 下（`supervisor/main.py:40-41` 的 QR/缓存路径），opencode.exe 也在 `vendor/opencode/`。不保留 = 用户要重新扫码 + 重下 196 MB。

覆盖前把 `config.yaml` 复制成 `config.yaml.bak.<时间戳>`；`data/` 与用户配置是唯一不可覆盖的资产。

另新增 `dafeiyu --version`（`cli.py:25` 的 subparser 是 `required=True`，目前没有这个参数），供 `diag` 与升级脚本读取。

### 13.5 面板口令可见性

自动生成的口令落在 `data/dashboard.password`（`lan_access.ensure_dashboard_password`）。

- `start-all.ps1` 就绪后：若本次启动新建了该文件（或日志出现"已生成"），控制台打印**文件路径 + 口令内容**，并提示"把它抄下来，浏览器登录用"；
- 已存在则只打印路径；
- 口令来自 `.env`（`DAFEIYU_DASHBOARD_PASSWORD`）时**绝不打印**。

### 13.6 备份与恢复（已具备，只需写进发布说明）

核实结果：`config.yaml` 与 `config.example.yaml` 的 `backup.enabled` 都是 `true`，`data/backups/` 里已有定时产物（`dafeiyu-20261027-040019-scheduled.db`，04:00 触发）。本设计**不新增功能**，只在 README 的发布说明里写清"默认每天 04:00 自动备份数据库到 `data/backups/`，保留策略见 `backup.keep`"。

## 14. 接话判断能力（独立工作流，不靠硬限制）

用户明确否决"只响应点名"这类硬限制：**要让她更会判断自己该不该开口，而不是把嘴堵上。**

现状（`config.example.yaml`）：`groups.whitelist: []`（等于所有群）、`triggers.mode: model`、`sample_rate: 0.05`、`keywords` 一批问句词、`batch_seconds 12` / `addressed_debounce 8` / `addressed_max_wait 30`。

这套默认值的问题不是"太灵敏"，而是**判断依据太薄**：模型只看到一小批消息 + 是否被点名，没有"这句话是在对谁说"、"这个话题是否已经有人接"、"上一轮她插话后有没有人理"。

设计方向（不进本次 P1–P4，单独一轮）：

1. **判定输入补全**：送审时附带最近 N 条群聊的说话人归属、被回复的目标、当前是否已有他人回应；
2. **显式沉默选项**：判定输出从"回 / 不回"扩展为"回 / 不回 / 想看但没把握"，最后一种触发更高门槛而不是直接开口；
3. **反馈闭环**：记录她插话后 5 分钟内的群内反应（有人接、无人理、被要求闭嘴），作为后续判定的经验，而不是靠调 `sample_rate`；
4. **评估方法**：用日志里 `批次送审 … addressed=False` 的历史批次做人工标注样本集，比较改前/改后的开口准确率与"被打断"次数。

### 14.1 必须先补的输入（数据模型缺口）

"这句话在对谁说"目前在数据里**不存在**：`Message` 只有 `at_bot` / `reply_to_bot`（`models.py:19-30`）；`gateway/qq.py:151-162` 解析出了 `reply_qq`，但只用来判 `reply_to_bot`，目标 QQ 没落库。所以这一轮要包含：

- `models.py` 加 `reply_to_user_id`（或 `reply_to_message_id`）；
- `gateway/qq.py` 落库时带上；
- `memory/timeline.format_message_line` 把它渲染出来（当前只有 `昵称：内容`，`timeline.py:33-44`）。

顺带修正本节上文的一个说法：prompt 里**已经有**昵称，缺的是"引用目标 / 是否已有人回应 / 她上次开口后的反应"。

### 14.2 可判定的验收标准（否则就是愿望）

- **指标**：`precision = 标注为"该开口"且实际开口的批次数 / 全部开口批次数`；辅以 `被打断次数`（开口后 5 分钟内被 @ 要求闭嘴，或被完全无视）。
- **样本**：从 DB join 日志取最近 7 天的 `addressed=False` 批次，抽样 ≥200 条，人工双标（同一批两人独立标注，分歧仲裁）。
- **冻结变量**：`groups.whitelist`、`triggers.mode`、`sample_rate`、`batch_seconds` 全部不变——只改判定输入与提示词，否则无法归因。
- **通过线**：precision 提升 ≥10 个百分点，且被打断次数不增加。达不到就回滚提示词，不进主分支。

这一节只定方向与评估方式，具体实现与阈值需要在独立一轮里用真实群聊数据验证，不能靠拍脑袋调参。

