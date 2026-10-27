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

热切换：`control.py::switch()` 走 `/api/brain`（面板 → supervisor → 控制口），已有 3 档切换骨架。本设计把 `token` 档改为"自备 Key（opencode）"，不再走 `BrainClient`。

### 5.3 面板

现有"大脑"卡片三选一（`index.html:729` `MODE_ORDER`），改造为：

- 三张模式卡，带一行说明（免费额度 / 用自己的 key / 本地跑）。
- ② 档字段：baseURL、model、apiKey（密码框，回显打码，留空表示不修改）。
- ③ 档字段：服务地址、模型下拉（探测按钮）、key（可选）、上下文长度（探测不到时手填）。
- 保存 → `POST /api/brain`（扩展字段）→ 写覆盖层 → 重建 brain → 返回当前生效配置。

## 6. P1 安装器与发行包

**发行 zip 只含代码**：`src/`、`scripts/`、`docs/`、`tests/`、`config.example.yaml`、`pyproject.toml`、`README.md`、`LICENSE`，约 30 MB。

NapCat 与 opencode 都由安装脚本在用户机器上下载，理由：

- `vendor/napcat/napcat/` 内**没有 LICENSE 文件**，再分发依据不足；
- `vendor/napcat` 占 345 MB，是当前 377 MB 包体的 91%，用户的下载体验极差；
- opencode.exe 单个 196.3 MB，同理。

代价是首次安装必须联网。安装脚本对"下载失败"要给出代理参数与手动下载地址，不能只报红。

### 6.1 NapCat（已有脚本，纳入单一入口）

`scripts/setup-napcat.ps1` 已能从 GitHub release 下载 `NapCat.Shell.Windows.Node.zip`（当前硬编码 `v4.18.41`，支持 `-Proxy`），并自动写 `onebot11_<BOT_QQ>.json`。本设计不重写它，只做三件事：

1. 把它作为 `setup.ps1` 的固定阶段调用（幂等：目录已存在则跳过）；
2. 版本过期时打一条提醒（不自动升级，避免把用户环境升坏）；
3. 下载后用 `vendor/napcat/node.exe --version` 探活一次。

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

`scripts/setup.ps1` 从 4 段扩展为 7 段：

1. venv
2. 依赖（`pip install -e .`）
3. **NapCat**（`setup-napcat.ps1`，幂等）
4. **opencode**（`setup-opencode.ps1`，幂等）
5. `init` 配置
6. `.env` —— 缺文件时进入**首次向导**（见 §13.1），而不是只打印提示
7. `doctor`

`doctor` 增加一条：找不到 opencode 时给出 `scripts\setup-opencode.ps1` 的命令行，而不是 "npm i -g"。

## 7. P2 凭据三档

### 7.1 覆盖层格式

`data/settings.local.yaml`：

```yaml
brain:
  mode: free          # free | key | local
  key_mode:
    base_url: ""
    model: ""
  local:
    base_url: ""
    model: ""
    context: 32768
    lean: false
```

`data/secrets/provider.key` 只存一行 key（无换行、权限加固）。

### 7.2 生成规则

`brain/provider_config.py`（新增）把 (mode, 覆盖层, secrets) 生成 opencode 配置：

- `free`：`model: "opencode/deepseek-v4.1-flash-free"`，不写 `providers` 段。
- `key`：写自定义 provider `dafeiyu-key`，`package: "@opencode/ai/providers/openai-compatible"`，`settings.baseURL`，`settings.apiKey: "{file:<abs>/data/secrets/provider.key}"`，`models.<model>` 带 `capabilities` 与 `limit`。
- `local`：同 `key`，provider 名 `dafeiyu-local`，`apiKey` 可选（无 key 时不写）。

`limit.context` **必须显式写**。opencode 对未声明的模型按 200k 上下文假设（见 `/v2/docs/models`），会把 20k 提示词发给 8k 模型。

### 7.3 工具收口

三档统一写入（本地档默认开工具，符合用户决定）：

```jsonc
"permissions": [
  { "action": "shell", "resource": "*", "effect": "deny" },
  { "action": "external_directory", "resource": "*", "effect": "deny" },
  { "action": "read", "resource": "*", "effect": "allow" },
  { "action": "edit", "resource": "*", "effect": "allow" },
  { "action": "webfetch", "resource": "*", "effect": "allow" }
]
```

含义：工作区内读写 + 可抓网页，禁 shell，禁出工作区。

## 8. P3 本地接入

### 8.1 探测

面板"刷新模型"→ `GET <baseURL>/models`：

- 能拿到 `data[].id` 列表；
- 上下文长度按来源分级：LM Studio / Ollama 的发现接口能报 context（若走 opencode 内置 `lmstudio`/`ollama` provider 则自动获得）；vLLM 用 `max_model_len`；通用端点通常不报 → 面板让用户手填，默认 8192 并标注"未探测到"。

### 8.2 上下文门槛（实测依据）

`data/opencode-bot.db` 里 20 个真实会话的首轮输入 tokens：p10 = 2,597，中位数 = 11,336，p90 = 21,062，最大 = 26,248。固定开销主要来自 agent 提示词（`deepseek-qq.md` 8,275 字节）、persona（5,862 字节）、opencode 系统提示与工具定义。

于是：

| 上下文 | 判定 | 面板表现 |
|---|---|---|
| < 16k | 红 | 允许保存，强提示"基本不可用" |
| 16k–32k | 黄 | 提示"会频繁压缩上下文" |
| ≥ 32k | 绿 | 正常 |

### 8.3 精简模式（`lean: true`）

给 8k–16k 的机器留活路：关闭工具、跳过 persona 全文（只保留前 N 行）、记忆只注入摘要。目标把首轮压到 3–5k（p10 的 2,597 证明可行）。仅在本地档生效。

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

- `provider_config` 三档生成的 JSONC 结构断言（含 `limit`、`permissions`、`{file:}` 引用）。
- `resolve_opencode_bin` 的候选顺序（用 `tmp_path` 造 vendor 目录）。
- 覆盖层读写：默认值 → 局部覆盖 → 回退。
- 机密文件写入 + 权限函数被调用的桩断言。
- 上下文分级函数（<16k 红 / 16–32k 黄 / ≥32k 绿）。
- `ensure_isolated_config` 的三种情况（旧 MINIMAL 覆盖、带标记覆盖、手工内容备份）。

集成：

- 面板 `POST /api/brain` 三档切换 → 生成文件内容正确 → `switch()` 被调用。
- 本地档探测失败 → 返回可读错误，覆盖层不被污染。

手动活体（部署前）：

1. 干净目录 `init` → `setup-opencode.ps1` → `doctor` 全绿；
2. 免费档对话一次，确认能看图（发一张图，模型能描述）；
3. 切自备 key，错 key 立即报错、正确 key 能聊；
4. 本地档接 LM Studio，选模型、看上下文分级、跑一轮带工具的对话。

## 11. 风险与未决

1. **免费模型是否零凭据**：文档说未登录时 `opencode` provider 不存在，但本机 `auth.json` 没有 `opencode` 条目却在用该模型。我用临时的 `XDG_*`/`HOME` 隔离跑 `opencode models` 时仍然列出了 `lm/*`（来自 `auth.json`），说明隔离没生效、不可作为证据。**验证方法**：在另一台干净机器（或临时 Windows 用户）跑一次 `opencode models`，看是否出现 `opencode/deepseek-v4.1-flash-free`。结论出来前，发布说明里 ① 档标注"依赖 opencode 免费额度，可能需登录"。
2. **opencode 再分发许可**：若不支持内置，P1 只能是"安装时下载"（本设计已默认如此），zip 内不含 exe。
3. **项目目录与图片路径**：bot 目前把图片写到 `data/bot-workspace/images/`，提示词里给的是相对路径。若为收口把 `opencode.project_dir` 改成 `data/bot-workspace`，相对路径会失效——实施前必须核对图片注入方式，再做决定（默认本次不改 `project_dir`，只用 `permissions` 收口）。
4. **包体**：发行 zip 只含代码（~30 MB）；NapCat（345 MB）与 opencode（196 MB）改为安装时下载。
5. **下载源的可用性**：NapCat 走 GitHub release、opencode 走官方直链，国内网络可能超时。两个安装脚本都必须支持 `-Proxy`，失败时打印手动下载地址，并允许用户把 zip/exe 放到指定目录后重跑（幂等跳过）。
6. **`setup-napcat.ps1` 的版本是硬编码的**（`$Version = "v4.18.41"`）。NapCat 升级到不兼容 OneBot 反向 WS 的版本会直接影响可用性，因此本设计不自动升级，只在 `doctor` 里提示"当前版本低于最新"。

## 12. 交付顺序与回滚

1. P1 安装器与发行包（可独立验证：干净目录 → 7 段 setup → doctor 全绿）。
2. P2 三档配置生成 + 面板 + 机密文件（免费档与自备 key 可用）。
3. P3 本地接入 + 精简模式。
4. P4 产品化包（向导 / 诊断包 / 升级脚本 / 口令可见性）。
5. 全量测试绿 → 部署到 release 副本 → 活体验证。
6. 回滚：停副本 → 旧目录 `scripts\start.bat`；复制前保留 `data/` 备份。

## 13. P4 产品化包

### 13.1 首次向导

现状：`setup.ps1` 发现缺 `.env` 时只是复制模板并退出，用户还得自己打开文件找 `BOT_QQ=`。

改为交互式三步（每步都有默认值，回车即用默认）：

1. 「机器人 QQ 号」→ 校验纯数字 → 写 `BOT_QQ=`
2. 「用免费模型还是自己的 key」→ 默认免费，不自作主张写空 key 字段
3. 「面板端口 / 是否允许局域网」→ 默认 8787 / 允许

非交互环境（`-NonInteractive` 或检测到无控制台）自动降级为"复制模板 + 打印下一步"，绝不阻塞。

### 13.2 诊断包 `scripts/diag.ps1`

一条命令产出 `data/diag/diag-<时间戳>.zip`，内含：

- `version.txt`：`dafeiyu` 版本、Python 版本、opencode 版本、NapCat 版本、Windows 版本
- `doctor.txt`：`dafeiyu doctor` 的完整输出
- `logs/`：`logs/dafeiyu.log`、`logs/supervisor/*.log`（各保留最后 2000 行）
- `config.redacted.yaml`：`config.yaml` 的脱敏副本（key/password/token 字段替换为 `***`）
- `env.keys.txt`：**只列键名**，不列值

脱敏必须在 PowerShell 侧完成（不依赖模型判断）。`diag.ps1` 不打包 `data/*.db`（体积与隐私）。

### 13.3 版本可见（已具备，只需验证）

核实结果：`control.py:455` 的 health 快照已带 `version`，面板 `index.html:1250` 已渲染 `v0.1.0`。本设计不新增功能，只补：

- 一条测试断言 `dafeiyu.__version__` 与 `pyproject.toml` 的 `version` 一致；
- 发布说明里写明"面板右上角可看到版本号"；
- `dafeiyu --version` 子命令（若 `argparse` 尚未提供）。

### 13.4 升级脚本 `scripts/update.ps1`

1. 读 `data/supervisor.pid`，若在运行 → 调 `stop.ps1`
2. 拉取指定版本（zip 或 `git pull`，取决于发行方式）
3. 保留 `data/`、`config.yaml`、`.env`，只替换代码
4. `pip install -e .` 刷新依赖
5. `doctor` → 若非 0 则**不启动**，回滚提示
6. `start.bat`

`data/` 与用户配置是唯一不可覆盖的资产，脚本里对这三项做显式排除，并在覆盖前把 `config.yaml` 复制成 `config.yaml.bak.<时间戳>`。

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

这一节只定方向与评估方式，具体实现与阈值需要在独立一轮里用真实群聊数据验证，不能靠拍脑袋调参。

