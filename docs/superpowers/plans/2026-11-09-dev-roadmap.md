# 2026-11-09 开发路线图（P0–P3）

> 状态：待执行
> 基线：`feat/qq-bot` @ `ddd2ed2`；全量 `pytest -q` = **3300 passed, 20 skipped**（约 5 分 40 秒）
> 范围：CI 落地 + P0–P3 全部待办的可执行安排
> 相关文件：`.gitea/workflows/ci.yml`、`scripts/register-gitea-runner.ps1`、`docs/gitea-ci.md`

---

## 0. 全局约定（每项都适用）

- 分支只用 `feat/qq-bot`；每完成一项就一个 conventional commit（`feat:` / `fix:` / `docs:` / `chore:` / `ci:`）。
- **验证顺序**（前一步不过不进下一步）：
  1. 对改动的 `.py` 跑 `lsp_diagnostics`（basedpyright），确认没有**新增** error；
  2. 目标测试：`.venv\Scripts\python -m pytest -q tests/test_<相关>.py`；
  3. 全量测试：`.venv\Scripts\python -m pytest -q`。
- 红线：不得用 `# type: ignore` / `as any` / `# pyright: ignore` 压错误；不得删除或弱化测试；不得提交 `config.yaml`、`.omo/`、`data/`、`logs/`、`.env`。
- 新 `.ps1` 含中文必须是 **UTF-8 with BOM**，由 `tests/test_scripts_bom.py` 强制。
- 每波结束更新 `.tamako/risks.md` 对应条目状态（该文件不入库）。
- 标注「不做」的项不是拖延，是已确认的边界，不要重新立项。

---

## 1. 波次总览

| 波次 | 内容 | 产出 | 依赖 | 预估 |
| --- | --- | --- | --- | --- |
| W0 | P0-1 CI | workflow + runner 注册（自动部分已完成） | 人工取 token | S |
| W1 | P0-5 文档一致性 + P0-6 ruff | 文档修正、`[tool.ruff]`、CI lint 步骤 | W0 | S/M |
| W2 | P0-3 摘要回补 + P0-4 R14 | 记忆正确性、配置收尾 | 无 | S/M |
| W3 | P0-2 basedpyright 基线 | 148 → 0 error，CI 加类型门槛 | W1（ruff 先立） | L |
| W4 | P1-1 微信出站文件 | 小而完整的功能闭环（已完成） | 无 | S |
| W5 | P2-1 面板 E2E + P2-2 R7 + P2-4 覆盖率 | 工程质量 | W0/W3 | M |
| W6 | P3 运维项 | 安装器/告警/文档 | 按需 | M |

**建议顺序**：W0 → W1 → W2 → W4（先拿一个可感知功能）→ W3（大改单独成波）→ W5 → W6。
W3 也可以提前到 W1 之后，但它会碰 17 个文件，最好在 lint 稳定后单独做。

---

## 2. P0：优先做

### P0-1 Gitea CI（自动部分已完成，剩人工注册）

**目标**：每次 push / PR 自动跑全量 Windows pytest。

**现状 / 证据**：仓库此前无任何 CI（无 `.github`、无 `.gitea`）。全量测试只能手动跑。

**已交付**：

- `.gitea/workflows/ci.yml`：`windows-latest`（host）、`builtin:checkout`、建 `.ci-venv`、`pip install -e ".[dev,wechat]"` + 单独补 `silk-python`、`compileall`、`pytest -q --junitxml`、失败时打印 JUnit 摘要；45 分钟超时、按 ref 取消旧流水线。v5.4.1 只内置 `builtin:checkout`（`upload-artifact` 尚未发布），因此不做 artifact 上传。
- `scripts/register-gitea-runner.ps1`：下载 v5.4.1、SHA-256 校验、`register --no-interactive --labels windows-latest:host`、注册登录自启计划任务（可选 Windows 服务）。
- `docs/gitea-ci.md`：前置条件、注册、验证、排障、安全。

**人工待办（必须有 Gitea 管理员权限）**：

- [ ] 确认 Gitea `app.ini` 有 `[actions] ENABLED=true`（本机 1.27.3；runner 能注册并连上 3000 端口，基本可判定已开启）。
- [x] 从 `http://yuannas.local:3000/admin/actions/runners` 复制 registration token。
- [x] `$env:GITEA_RUNNER_REGISTRATION_TOKEN='<token>'; powershell -ExecutionPolicy Bypass -File scripts\register-gitea-runner.ps1`
      → `Runner registered successfully.`，`C:\gitea-runner\.runner` 已生成，计划任务 `gitea-runner` 为 `Running`（PID 3876，6 条到 `192.168.2.200:3000` 的连接）。
- [ ] 确认 `/admin/actions/runners` 里 runner 为 Idle，且 label 是 `windows-latest:host`。
- [ ] 注册完成后重新生成一次 registration token（旧 token 已在聊天里出现过）。
- [ ] 推送一次或手动 Run workflow，确认绿色；失败时看日志末尾的 `junit.xml` 摘要。
- [x] 本地端到端空跑（`gitea-runner exec -W .gitea/workflows/ci.yml -j test -i -self-hosted`）：见下方「本地 exec 结果」。

**验收**：Actions 页出现成功 job；失败时日志能定位到具体测试。

**本地 exec 结果（2026-11-09，`--env-file <空文件>`）**：

```
Checkout / Report toolchain / Create CI virtualenv / Install deps / Byte-compile / Run pytest  全部 Success
Run pytest: 3300 passed, 20 skipped, 7 warnings in 342.32s (0:05:42)
Job succeeded  (EXEC_EXIT_CODE=0)
```

与本地 `.venv` 全量基线**完全一致**（`3300 passed, 20 skipped`）。

验证过程中发现并修掉的**三个真实 CI 阻塞**（都只有干净 checkout 才暴露，本地跑不出来）：

1. **`vendor/napcat` 缺失** → `build_specs()` 直接抛 `ConfigError`，14 条测试红。
   `vendor/` 被 `.gitignore` 忽略，任何干净 checkout 都没有。修法：`tests/conftest.py` 新增
   `fake_napcat` fixture（临时目录造 `node.exe` + `index.js` 并 monkeypatch `NAPCAT_DIR`），
   `test_supervisor_specs.py` 用 autouse 包一层，`test_wechat_failure_paths.py` 里的
   `build_specs` 用例显式取该 fixture。需要「没装 NapCat」语义的用例仍然自己覆盖 `NAPCAT_DIR`。
2. **本地 exec 会把仓库 `.env` 读进 job 环境** → `test_config*` / `test_dashboard_auth_config`
   被真实 `BOT_QQ` / `DEEPSEEK_API_KEY` / 面板口令覆盖而红。真实 Gitea workspace 没有 `.env`，
   所以这是本地验证的假阴性；已在 `docs/gitea-ci.md` 记下「exec 必须加 `--env-file <空文件>`」。
3. **`pysilk` 未装** → `test_speech.py::test_real_silk_roundtrip` 在 CI 里静默 skip，少一条覆盖。
   workflow 的安装步骤单独补 `silk-python>=0.2.9`（不装 `stt` extra：`faster-whisper` 会拖进
   torch 级体积）。

**工作量**：S（自动部分已完成，人工约 10 分钟）。

---

### P0-2 basedpyright 基线清理 → CI 类型门槛

**目标**：`src/dafeiyu` 达到 **0 error**，然后把 basedpyright 加进 CI。

**现状 / 证据（2026-11-09 实测 `basedpyright --level error src/dafeiyu`）**：

```
148 errors, 0 warnings, 0 notes
```

按文件：

| 文件 | error |
| --- | --- |
| `src/dafeiyu/control.py` | 62 |
| `src/dafeiyu/service.py` | 18 |
| `src/dafeiyu/supervisor/dashboard.py` | 13 |
| `src/dafeiyu/memory/store.py` | 9 |
| `src/dafeiyu/workers/impressions.py` | 7 |
| `src/dafeiyu/brain/client.py` | 7 |
| `src/dafeiyu/router.py` | 7 |
| `src/dafeiyu/gateway/wechat_bridge/__main__.py` | 6 |
| `src/dafeiyu/supervisor/audit.py` | 4 |
| `src/dafeiyu/brain/provider_config.py` | 3 |
| `src/dafeiyu/supervisor/manager.py` | 3 |
| `src/dafeiyu/gateway/qq.py` | 2 |
| `src/dafeiyu/brain/runtime_config.py` | 2 |
| `src/dafeiyu/env_probe.py` | 2 |
| `models.py` / `supervisor/main.py` / `images.py` | 各 1 |

典型类别：`payload.get()` 可空未收窄、`object` 不可 `await`、`Expected type arguments for generic class "Task"/"dict"`、`tuple[ChatType, str]` 与 `tuple[ChatType, int]` 不匹配（`workers/impressions.py`，这可能是**真实 latent bug**，不只是标注问题）。

**实施要点（分批，每批一个 commit）**：

1. `basedpyright --level error src/dafeiyu > .omo/typecheck-baseline.txt` 存档基线（不入库）。
2. 先修 `workers/impressions.py`、`memory/store.py`、`router.py`：这些是类型/键不一致，先确认是标注错还是逻辑错；逻辑错要顺手加回归测试。
3. 修 `control.py`（62 条，最大头）：
   - 给控制口 payload 定义 `TypedDict`/dataclass（或 pydantic 模型），不要在业务层反复 `payload.get("x")`；
   - 对 `object` 值用 `isinstance` / `inspect.isawaitable` 收窄后再 `await`；
   - 抽 `_require_str(payload, key)` 之类的收窄辅助函数，避免每处 `assert`。
4. 修 `service.py`、`dashboard.py`、`brain/*`、`wechat_bridge/*` 的剩余项。
5. `pyproject.toml` 的 `dev` extra 加 `basedpyright`（现在只在全局 anaconda 里有，`.venv` 没有）。
6. CI 在 `pytest` 前加一步：
   ```yaml
   - name: Type check
     shell: pwsh
     run: |
       & .\.ci-venv\Scripts\basedpyright.exe --level error src
       exit $LASTEXITCODE
   ```

**验收**：`basedpyright --level error src/dafeiyu` = 0 error；全量 pytest 仍 3300 passed；CI 类型步骤绿。

**工作量**：L（148 条，集中在 control.py；建议单独一波，别和其他功能混）。

**风险**：`impressions.py` 的 `tuple[ChatType, str]` vs `tuple[ChatType, int]` 若确认是逻辑错，修它可能改变行为，必须补测试。

---

### P0-3 摘要回补窗口（R2）

**目标**：长停机后不产生永久记忆缺口，且代码/注释/风险登记一致。

**现状 / 证据**：

- `src/dafeiyu/workers/summary.py:43` 注释写「最近 7 天」，但 `:44` 实际 `BACKFILL_DAYS = 14`（注释过期）。
- `:46` `MAX_BACKFILL_PER_RUN = 3`；`pending_summary_slots()` 的窗口在 `memory/store.py:3553` clamp 到 `[1, 90]` 天。
- 风险登记 `.tamako/risks.md` R2 仍描述「最多 3 天」。

**先核对再改**（避免重复实现）：

1. 确认 worker 轮询间隔（`SummaryScheduler` 的后台循环）与每轮 3 个槽位组合下，30 天停机需要多少轮补完。
2. 若 `BACKFILL_DAYS=14` 已覆盖 R2 的原始问题，任务就变成「修注释 + 更新风险状态 + 补一个回归测试」，不要改逻辑。
3. 若仍需加快追平：优先在「存在积压」时提高 `MAX_BACKFILL_PER_RUN` 或增加每日轮次，而不是无限拉大 `BACKFILL_DAYS`（窗口越大，每轮 SQL 扫描越重）。

**改动点**：

- `summary.py`：注释 7 → 14；如调参，常量旁写清预算理由。
- `tests/test_summary_backfill.py`：加「停机 N 天，M 轮后缺口清零」的用例。
- `.tamako/risks.md`：R2 状态改为 `closed` 或写清剩余边界。

**验收**：`.venv\Scripts\python -m pytest -q tests/test_summary_backfill.py tests/test_summary.py` 通过；全量通过；注释与常量一致。

**工作量**：M。

---

### P0-4 R14 retention 收尾

**目标**：本机 `memory.retention_days` 从 0（不清理）改为 180，并留备份。

**现状 / 证据**：`src/dafeiyu/config.py:121` 默认已是 `180`；但本机 `config.yaml:233` 显式写 `0`。`config.yaml` 被 `.gitignore` 忽略，**这是机器配置动作，不是代码提交**。

**步骤**：

- [ ] 备份：`Copy-Item config.yaml config.yaml.bak-20261109`
- [ ] 把 `memory.retention_days: 0` 改为 `180`（`images.retention_days: 7` 保持）。
- [ ] 重启 supervisor（或热重载，按现有机制）。
- [ ] 验证：`dafeiyu doctor` 与面板不再显示「永不清理」；查日志确认清理任务按 180 天执行。
- [ ] `.tamako/risks.md` R14 状态改为 `closed`，记录备份文件名。

**验收**：运行态配置为 180；清理逻辑无异常；备份存在。

**工作量**：S。**注意**：不要把这个改动提交进仓库。

---

### P0-5 文档一致性

**目标**：README / `docs/message-types.md` 与代码实际能力一致。

**已知矛盾**：

1. `README.md:562` 写「不支持撤回 / 编辑事件」，但 `src/dafeiyu/gateway/wechat.py` 已有 `RecallEvent` + `on_recall()` 入站撤回处理。应改为「**支持撤回入站；编辑事件不支持**」。
2. `README.md` 约 562–563 行同名群表述前后不一致：一处说「只能人工区分」，另一处说「由成员集合区分 alias」。以 `wechat_bridge` 注册表（`identity.py` / `by_alias`）的实际行为为准，统一成一句。
3. `docs/message-types.md` 微信矩阵里「文件出站」目前写不支持；若 W4 完成 P1-1，要同步改成支持（见 P1-1）。

**步骤**：逐条对照代码确认 → 改文档 → `grep` 全文再搜「撤回 / 不支持 / 同名」确认无残留矛盾。

**验收**：文档描述与 `wechat.py`、`wechat_bridge/identity.py`、`scripts/wechat_hook_server.py` 一致。

**工作量**：S。

---

### P0-6 ruff 配置 + CI lint

**目标**：把已装但未配置的 ruff 变成真门槛。

**现状 / 证据**：`.venv` 有 `ruff 0.17.3`；`pyproject.toml` 只有 `[tool.setuptools]`、`[tool.pytest.ini_options]`，**没有 `[tool.ruff]`**；`dev` extra 也没声明 ruff。

**步骤**：

1. `pyproject.toml`：
   - `dev` extra 加 `ruff>=0.17`；
   - 加 `[tool.ruff]`：`target-version = "py311"`、`line-length` 对齐现有风格（先量一下最长行，别一上来 reformat 全仓）；
   - `[tool.ruff.lint]` 先只开低争议规则（`E9`、`F`、`I`），不要一次开全部。
2. 先跑 `ruff check src tests` 看存量，评估是否要 `--fix`；**不要**在同一个 commit 里既改规则又大规模改代码。
3. CI 加一步：
   ```yaml
   - name: Lint
     shell: pwsh
     run: |
       & .\.ci-venv\Scripts\ruff.exe check src tests
       exit $LASTEXITCODE
   ```
4. 若存量多，先加 `[tool.ruff.lint.per-file-ignores]` 或按目录分批，**禁止**全仓 `# noqa`。

**验收**：`ruff check src tests` 通过；CI lint 步骤绿。

**工作量**：S/M（取决于存量）。

---

## 3. P1：能力补齐

### P1-1 微信出站发文件（已完成：走 `upload_file` → `send_media(kind=file)`）

> 状态：**已完成**（2026-11-09）。`WeChatAdapter.upload_file` 直接复用 `send_media(chat_type, chat_id, "file", path, name=name)`；桥回 `unsupported_media` 时返回 `None`、不抛错，其它失败仍抛 `GatewayError`，由 `Service._send_chunk` 记日志后继续发文本。`service.py` 分支结构未改动。

**目标**：模型附件 `kind=file` 在微信侧真正发出去，而不是被静默跳过。

**现状 / 证据**：

- `src/dafeiyu/service.py:2342-2356` 已经按 `adapter.upload_file` 分发文件类附件；`QQAdapter` 有实现（`gateway/qq.py:614`）。
- `WeChatAdapter` **没有** `upload_file`，所以文件附件走到 `media = kind != "file"` 后被丢弃。
- hook 后端 `scripts/wechat_hook_server.py:199-206` 的 `send_media` 已支持 `file -> send_file`；桥的 `WeChatAdapter.send_media`（`gateway/wechat.py:700`）可以直接发 `kind="file"`。

**实施要点**：

1. `src/dafeiyu/gateway/wechat.py` 给 `WeChatAdapter` 加：
   ```python
   async def upload_file(self, chat_type, chat_id, path, *, name="") -> None:
       await self.send_media(chat_type, chat_id, "file", path, name=name)
   ```
   （签名对齐 `QQAdapter.upload_file`，`service._send_chunk` 无需改动；`unsupported_media` 时 `send_media` 返回 False，`upload_file` 不抛错。）
2. 失败语义：`send_media` 返回 False（`unsupported_media`）时 `upload_file` 不抛错，由 `_send_chunk` 继续发文本；其它发送失败仍抛 `GatewayError`，同样被 `_send_chunk` 捕获——保持「附件是加分项」。
3. 测试：
   - `tests/test_wechat_adapter.py`：断言 `upload_file` 发出 `action=send_media, kind=file`；unsupported 时返回 `None` 且不抛。
   - `tests/test_service_wechat_media.py`：文件附件走 `upload_file`，失败时正文照发。
4. 文档：`docs/message-types.md` 文件行「微信出站」改为支持（hook 后端；UIA 仍不支持）。

**验收**：目标测试 + 全量通过；真实环境发一个文件到微信群可见；失败时文本仍送达。

**工作量**：S。**依赖**：无。

---

### P1-2 微信出站引用 / 合并转发（阻塞，不做）

**现状**：helper 的 native 头文件没有 quote/forward 发送原语；微信 `message_id` 是本地 `epoch:seq` 合成值，不是可引用的服务端 id。`docs/message-types.md` 已写明不支持。

**结论**：不在本仓库解决。保持现有降级（正文复述 / `[合并转发 N 条]` 占位）。等 helper 提供发送能力再立项。

**工作量**：阻塞。

---

### P1-3 微信出站视频（阻塞，不做）

**现状**：hook `send_media` 的 kind 白名单只有 `image/file/voice/sticker`；`kind=video` 明确 `unsupported_media`。helper 也无视频发送头。

**结论**：不做；文档已写。附件不发出、正文照发。

**工作量**：阻塞。

---

### P1-4 UIA 后端媒体（不做）

**现状**：UIA 路径对所有媒体返回 `unsupported_media`。理论可做「剪贴板发图」，但 hook 已是主路径，`docs/message-types.md` 明确不做。

**结论**：不做。

---

### P1-5 微信撤回 / 编辑

**现状**：

- **撤回入站已支持**：`gateway/wechat.py` 解析 `recall` → `RecallEvent` → `on_recall()`。
- **撤回伪造不做**：`gateway/wechat_bridge/poller.py:38` 明确「快照 diff 只知道新消息与 resync，本轮不伪造 recall」。
- **编辑事件**：本地库镜像没有对应事件，代码完全不支持。

**结论**：

- 只做 P0-5 的文档修正（把「不支持撤回」改对）。
- 「编辑事件」需要先调研微信本地库是否有 edit 标志/修订时间戳；若没有，维持不做。不要为了对齐 QQ 而伪造。

**工作量**：文档 S；编辑调研 L（默认不启动）。

---

### P1-6 视频完整转码（不做）

**现状**：入站视频只复制原文件 + ffmpeg 抽 4 帧。完整转码成本高、收益低。

**结论**：不做。

---

### P1-7 跨群镜像 QQ ↔ 微信群（已计划）

**现状**：QQ 群与微信群之间没有任何跨群中继，两个群的成员互相看不到对方。完整执行计划见 `.omo/plans/qq-wechat-group-mirror.md`（9 个实现 todo + 4 个验收 todo；默认关闭的 `mirror` 配置段、恰好一对群双向转发、媒体尽力而为降级占位）。

**边界**：不回填历史、不镜像撤回/编辑/引用/@；不支持多对群或广播；方向能力矩阵与占位规则见 `docs/message-types.md` 的「跨群镜像」。

**工作量**：L。

---

## 4. P2：工程质量与可观测性

### P2-1 面板浏览器 E2E

**目标**：用真实浏览器验证面板渲染与交互，补上「只对 HTML/JS 源码做正则断言」的盲区。

**现状 / 证据**：`tests/test_dashboard_html.py` 全部是字符串/正则断言（例如直接匹配 `renderFreeBanner(data)` 函数体），没有 DOM 渲染、没有点击、没有轮询行为。

**实施要点**：

1. `dev` extra 加 `playwright`（或单独 `e2e` extra）；CI 里 `python -m playwright install chromium`。
2. 起一个最小 supervisor dashboard（复用现有测试夹具，或 `http.server` 托管 `supervisor/dashboard/index.html` + 假 API）。
3. 用例：
   - 三种模式卡只有一张选中；
   - 凭据字段是 password 且从不回显已存密钥；
   - 轮询节流（用 `page.clock` 或拦截请求计数）；
   - 免费档 banner 只在明确 `unavailable` 时出现。
4. 标记 `@pytest.mark.e2e`，默认本地跳过；CI 单独 job（避免每次全量都装浏览器）。

**验收**：E2E job 绿；能捕获一次「把 renderFreeBanner 改坏」的回归。

**工作量**：M。**依赖**：W0（CI）。

---

### P2-2 R7 event-loop 警告

**目标**：全量测试不再出现 `Event loop is closed` 资源警告。

**现状**：全量 pytest 稳定出现 2 条警告（风险登记 R7）。

**步骤**：

1. `pytest -q -W error::ResourceWarning`（或 `-W error`）定位到具体测试。
2. 修 teardown：确保 `await adapter.close()`、取消后台任务、`await ws.close()` 在 loop 关闭前完成。
3. 不要用 `filterwarnings` 忽略。

**验收**：全量 pytest 0 条该警告。

**工作量**：S。

---

### P2-3 R8 / R9 / R10 / R12（接受项，按需）

| 编号 | 问题 | 处理 |
| --- | --- | --- |
| R8 | 摘要表头时区 | 记录触发条件；出现跨时区显示问题再修 |
| R9 | 超长历史按 token 切分 | 现在按行/块切；等小模型真实溢出再改 |
| R10 | 私聊是否注入印象 | 先保持现状，产品确认后再动 |
| R12 | `last_seen_at` 迁移时间 | 已有回填；观察迁移耗时，异常再优化 |

**结论**：不主动做；每项在风险登记里写清「什么现象出现才启动」。

**工作量**：各 S（触发时）。

---

### P2-4 覆盖率门槛

**目标**：CI 有覆盖率数字和最低阈值，防止后续改动裸奔。

**步骤**：

1. `dev` extra 加 `pytest-cov`。
2. 先测量基线：`.venv\Scripts\python -m pytest -q --cov=src/dafeiyu --cov-report=term-missing`。
3. 阈值设在基线**略低**（例如基线 62%，门槛先 58%），避免 Windows 专有分支波动导致假红。
4. CI 加 `--cov --cov-fail-under=<阈值>`。
5. 面板/报告可选上传 `coverage.xml`。

**验收**：CI 输出覆盖率并执行门槛。

**工作量**：S/M。

---

## 5. P3：部署与运维（多为机器级）

### P3-1 P9A 一键安装器 / 计划任务注册

**现状**：`scripts/register-supervisor-task.ps1` 已能注册 supervisor 计划任务，但没有「从零到能跑」的安装器；`docs/deployment-notes-windows.md:296` 明确 P9A 仍靠手工。

**可做**：新增 `scripts/install.ps1`，串起：检查 Python/git → 建 `.venv` → `pip install -e ".[dev,wechat]"` → 从 `config.example.yaml` 生成 `config.yaml`（已存在则备份）→ 注册 supervisor 任务 → 跑 `dafeiyu doctor`。

**注意**：机器级操作多，脚本只能提示不能代修（ACL/完整性/代理）；保持幂等、带 `-DryRun`；含中文必须 BOM。

**验收**：干净机器上一条命令到 `doctor` 通过（除环境阻塞项）。

**工作量**：M。

---

### P3-2 P6 NapCat 数据目录重定向

**现状**：不自动改用户 `Documents\Tencent Files`；根因是 P4 的 Low 完整性，已由干净工作区根治。

**结论**：维持文档说明，不做自动迁移（迁移用户数据风险高）。

**工作量**：不做 / 文档 S。

---

### P3-3 代理 / schannel / ensurepip 文档化

**现状**：`docs/deployment-notes-windows.md` 的 P1/P2/P3 已记录；安装脚本保留 `-Proxy` 与重试。

**可做**：把「新机器首次部署」的代理、schannel、pip 缓存三个坑整理成一节 checklist；不改代码。

**工作量**：S。

---

### P3-4 微信 helper 版本不匹配告警

**现状**：helper 用 `Weixin.dll` size + SHA-256 指纹（`helper/native/compatibility.json`）；版本不匹配时目前靠日志/健康帧。

**可做**：在 hook 健康帧或面板加一个显式「helper 版本不匹配」状态，指向升级指引。

**注意**：升级适配本身在 helper 仓，不在本仓库。

**验收**：人为改指纹后，面板/告警显示明确提示。

**工作量**：S。

---

## 6. 明确不做（已写进边界，勿重复立项）

- 微信出站引用 / 合并转发：helper 无发送原语。
- 微信出站视频：helper/hook 无视频发送。
- UIA 媒体（剪贴板发图）：hook 是主路径，文档明确不做。
- 视频完整转码：只抽帧足够。
- 文生图 / 绘图 API：需要外部付费服务与内容安全策略。
- 向量库 / embedding 召回：项目选择 FTS + 规则路线。
- 跨会话图片引用：已有归属校验，放开会乱发图。

---

## 7. 每波收尾检查单

```powershell
# 1) 类型
basedpyright --level error src\dafeiyu

# 2) 目标测试（替换 <file>）
.venv\Scripts\python -m pytest -q tests\test_<file>.py

# 3) 全量
.venv\Scripts\python -m pytest -q

# 4) 新增/修改的 .ps1 若是中文，确认 BOM
.venv\Scripts\python -m pytest -q tests\test_scripts_bom.py
```

全部通过后再 `git add` 对应文件并提交；**不要**把 `config.yaml`、`.omo/`、`data/`、`logs/`、`.env` 带进 commit。

---

## 8. 变更记录

| 日期 | 内容 |
| --- | --- |
| 2026-11-09 | 初版：纳入 CI 落地结果、148 条 basedpyright 实测基线、P0–P3 安排 |
