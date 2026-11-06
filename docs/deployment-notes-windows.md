# 部署记录：Windows 本机部署踩坑与解决（2026-10-31）

本文记录在 **Windows 11（Insider 版本 10.0.29671）+ 火绒安全 6.0 + 无管理员常驻权限** 的机器上，
把大肥鱼从零部署到「开机自启、QQ 群正常收发」过程中遇到的真实问题、根因和解决办法，供后续
换机部署 / 排查参考。

> 文中路径是**本机示例**（项目最终位于 `C:\AIWork\dafeiyu-bot`），命令按实际路径替换即可。
> 不包含任何密钥；`.env` 里的值一律不要提交进仓库。

> **2026-10-31 更新**：文中 P4/P5/P7/P8/P10/P11 里能代码化的部分已经落地到仓库
> （见文末第 6 节）。新机器先按 README / `dafeiyu doctor` 的提示走即可；本文继续保留，
> 作为根因分析和人工排查参考。§5 的原改进建议已改写为「已落地 / 仍留文档」。

---

## 1. 最终部署结构

```
C:\AIWork\dafeiyu-bot\          # 仓库本体（标准布局，无 junction）
  .venv\                        # Python 3.12 虚拟环境（editable 安装本项目）
  vendor\napcat\                # NapCat v4.18.41 便携版（node.exe + napcat.bat）
  vendor\opencode\              # opencode v2.2.5 单文件 exe
  config.yaml                   # 本机配置（gitignored）
  .env                          # 凭据（gitignored）
  data\                         # SQLite / 日志 / 备份 / runtime（gitignored）
  logs\                         # 运行日志（gitignored）
```

- 自启：Windows 任务计划 `dafeiyu-bot`，登录后延迟 30 秒运行
  `powershell -File <repo>\data\runtime-launcher.ps1`
- 启动链：`runtime-launcher.ps1`（注入环境变量）→ `scripts/start-all.ps1` → supervisor（pythonw）
  → 托管 opencode / bot / NapCat 三个子进程
- 面板：`http://127.0.0.1:8787/`（账号 `admin`，口令来自 `.env` 的 `DAFEIYU_DASHBOARD_PASSWORD`）
- 模型：`brain.provider=opencode` + 面板切到「自备 Key」档（DeepSeek `deepseek-flash`）

---

## 2. 问题清单（现象 / 根因 / 解决）

### P1. `git clone` 失败：本机代理没启动

- **现象**：`Failed to connect to github.com:443 over proxy 127.0.0.1`；全局配置里
  `http.https://github.com.proxy=http://127.0.0.1:10808`，但该端口没有程序监听。
- **解决**：走直连。另外该机的 schannel TLS 在被限制的进程里会报
  `SEC_E_NO_CREDENTIALS`，所以 git 直连时还要切 OpenSSL 后端：

  ```powershell
  git -c http.sslBackend=openssl -c http.https://github.com.proxy= clone https://github.com/YuanZ1949/dafeiyu-bot.git
  # 或者写进仓库本地配置
  git config http.sslBackend openssl
  git config http.https://github.com.proxy ""
  ```

- **备注**：代理恢复后想重新走代理，删掉这两条本地配置即可。

### P2. GitHub 直连下载 NapCat 极慢（~20–50 KB/s）

- **现象**：单连接下载 118 MB 的 NapCat release 需要 1 小时以上。
- **解决**：用 Python（OpenSSL 栈）做 **多线程分片 Range 下载**，8 连接并行约 0.2–0.3 MB/s，
  12 分钟下完。仓库里的 `scripts/setup-napcat.ps1` 用 `curl.exe`/`Invoke-WebRequest`，在本机
  受限进程里 schannel 不可用，因此手写下载脚本（见本文 P4 的环境说明）。

### P3. pip 装包卡死 / 建 venv 失败

- **现象 A**：`python -m venv .venv` 报 `ensurepip ... returned non-zero exit status 1`。
  根因是在受限工作区里 `tempfile.mkdtemp()` 创建的目录（mode 0700）随后无法访问，
  ensurepip 清理临时目录时 `PermissionError`。
- **现象 B**：`pip install` 卡在 `Installing build dependencies: started` 不动。
  根因是 **pip 缓存目录在用户目录下、写入被拦截**（同 P4 的低完整性/沙箱问题）。
- **解决**：
  - 用**系统 Python** 在**干净目录**（工作区之外，或最终目标目录）创建 venv；
  - pip 加参数：`--no-cache-dir --no-build-isolation`；
  - 先装 `setuptools wheel`，再 `pip install -e .`（避免 build isolation 再起一个 pip）。

  ```powershell
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install --no-cache-dir setuptools wheel
  .venv\Scripts\python.exe -m pip install --no-cache-dir --no-build-isolation -e .
  ```

### P4. 核心问题：工作区内的可执行文件被降权为 Low 完整性（本次最大的坑）

- **现象**：`.venv\Scripts\python.exe`、`vendor\napcat\node.exe`、`vendor\opencode\opencode.exe`
  写用户目录（`Documents`、`AppData`、`Temp`、`.local`）全部 `EPERM/PermissionError`；
  写项目目录 `C:\AIWork\...` 或 `C:\AICoding` 却正常。PowerShell / cmd / git / tar 则完全正常。
- **诊断方法**（很关键，以后复现用）：
  1. 用 Python `ctypes` 读当前进程的令牌完整性级别（`GetTokenInformation(TokenIntegrityLevel)`）：
     - 被拦的进程：`S-1-16-4096`（**Low**）
     - 正常进程：`S-1-16-8192`（**Medium**）
  2. `icacls <目录>` 查看目录的 `Mandatory Label`：
     - 问题工作区：`Mandatory Label\Low Mandatory Level:(OI)(CI)(NW)`
     - 干净目录：无 Mandatory Label 行
  3. 对照实验：同一个 `python.exe` 复制到工作区外运行是 Medium、在工作区内是 Low；
     **复制出来**（新文件）会变 Medium，**移动出来**（保留文件身份）仍然是 Low。
     往工作区里覆盖可执行文件也不会恢复。
- **根因**：本机的 DSH（DeepSeek Harness）沙箱给工作区目录打上了 **Low 完整性标签并继承**，
  工作区内创建/继承的可执行文件以 Low 完整性运行；而用户目录是 Medium 完整性，低完整性进程
  不能写入（Mandatory Integrity Control 的 no-write-up）。**这不是火绒的问题**（见 P5），
  也**不受会话里把权限切到「完全访问/无沙箱」影响**——标签是建工作区时落下的持久化配置。
- **临时解法（本次先用的）**：把可执行文件放在工作区外，用**目录联接（junction）**接回仓库原路径：

  ```powershell
  # 运行时外置
  Move-Item .venv            C:\dafeiyu-runtime\venv
  Move-Item vendor\napcat    C:\dafeiyu-runtime\napcat
  Move-Item vendor\opencode  C:\dafeiyu-runtime\opencode
  cmd /c mklink /J .venv            C:\dafeiyu-runtime\venv
  cmd /c mklink /J vendor\napcat    C:\dafeiyu-runtime\napcat
  cmd /c mklink /J vendor\opencode  C:\dafeiyu-runtime\opencode
  ```

  移动（而非复制）出去的文件仍带 Low 标签、仍会被降权，所以**外置的目录要重新复制/解压一份**，
  或直接在外置目录重新 `python -m venv` / 解压 zip。
- **根治（推荐）**：**换一个干净的工作区目录**（本次换成 `C:\AIWork`）。新建的工作区没有
  Low 标签，实测其中 exe 为 Medium、能写用户目录；随后把项目整体搬过去，运行时放回仓库内
  （`.venv`、`vendor\napcat`、`vendor\opencode` 恢复为真实目录，删除所有 junction），并重装
  editable 包（`.pth` 里是旧绝对路径）：

  ```powershell
  .venv\Scripts\python.exe -m pip install --no-cache-dir --no-build-isolation -e .
  ```

- **注意**：如果之后又把 DSH 切回 `workspace-write` 模式并在该目录开新会话，可能再次给目录
  打 Low 标签，机器人会重新 EPERM。保持「完全访问」或换干净工作区可避免。
- **已固化**：`dafeiyu doctor` 会读当前进程的完整性级别并实际写一次 `%TEMP%`；Low 或写不进去时
  直接报「运行环境疑似被沙箱降权（Low 完整性）」，文案明确写「先查完整性级别，别怀疑杀软」。
  `scripts/setup.ps1` 在建 `.venv` 之前也会跑同一个探测（`src/dafeiyu/env_probe.py`），
  安装阶段就能看到提示，不必等到 NapCat / opencode 报 EPERM。

### P5. 火绒导致的「误诊」（本机还装了火绒 6.0）

- **现象**：P4 的 EPERM 看起来像杀软拦截；`sc.exe stop HipsDaemon` 失败（火绒自我保护）；
  在火绒信任区添加程序/目录也没有效果；火绒日志/DB 里也查不到 python/node 的拦截记录。
- **结论**：**火绒是无辜的**，真因是 P4 的 Low 完整性。以后遇到「写用户目录 EPERM」先查
  进程完整性级别和目录 Mandatory Label，再怀疑杀软。
- **副作用**：`sc stop` 被自我保护挡住是正常的；要彻底确认只能从火绒界面退出（并处理 UAC），
  不要在部署时把时间花在这上面。
- **已固化**：P4 的自检（`dafeiyu doctor` / `scripts/setup.ps1`）会先指出「Low 完整性」这个
  真因，避免再把 EPERM 误判成杀软；排查顺序建议固定为「完整性级别 → 临时目录 → 杀软」。

### P6. NapCat 扫码登录成功但立刻崩溃

- **现象**：日志 `二维码已被扫描` → `Worker进程已登录成功` → 立刻
  `EPERM: mkdir 'C:\Users\<user>\Documents\Tencent Files\NapCat\temp'` → worker 退出 → 回到二维码。
- **根因**：同 P4，NapCat 的 QQ 数据目录在用户 `Documents` 下，Low 完整性写不进去。
- **解决**：P4 根治（干净工作区 + Medium 完整性）后自然恢复；在此之前用过
  `mklink /J "Documents\Tencent Files\NapCat"` 重定向到项目目录的临时办法，已撤销。
- **验证**：日志出现 `[OneBot11] 配置加载` → `WebSocket反向服务 ws://127.0.0.1:8080 已启动`
  → `OneBot11 适配器初始化完成`，bot 日志出现 `NapCat 已连接`。

### P7. opencode v2 的 serve 强制 Basic 认证：bot 全是 401

- **现象**：`免费模型探测失败：HTTPStatusError`；面板切模型失败；opencode 日志里
  `POST /api/session http.status=401`；`opencode serve` 启动时打印随机 `server password`。
- **根因**：opencode v2 的 serve 强制 Basic 认证（用户名固定 `opencode`，密码由环境变量
  `OPENCODE_SERVER_PASSWORD` 决定）。supervisor 拉起 opencode 时只透传 `os.environ` + 配置隔离
  变量，**没有把口令传下去**，opencode 每次随机生成一个密码，bot 拿不到 → 401。
- **解决**：在启动器里固定一份口令，整条进程树继承（opencode 子进程和 bot 读同一个环境变量）：

  ```powershell
  $env:OPENCODE_SERVER_PASSWORD = '<随机 32 位十六进制>'
  ```

  验证：`curl -u opencode:<口令> http://127.0.0.1:24310/api/info` 返回 200（无认证返回 401）。

- **已固化（2026-10-31）**：supervisor 现在在 `build_specs` 里解析一次口令，并把同一个
  `OPENCODE_SERVER_PASSWORD` 同时写进 opencode 与 bot 两个子进程的环境变量
  （`src/dafeiyu/brain/opencode_auth.py`）。取值顺序：`config.yaml` 的
  `opencode.server_password` → `.env` 的同名变量 → 都没有时每次启动生成一个 32 位十六进制
  随机口令（只放进程环境，不落盘、不写日志）。因此**不再依赖 `data/runtime-launcher.ps1`
  手动 set**：直接双击 `scripts\start.bat` 或跑 `start-all.ps1` 也不会再 401。
  启动器里保留 `$env:OPENCODE_SERVER_PASSWORD` 仍然兼容，只是变成「想固定口令给外部调试」
  的可选做法。bot 自己拉起 serve（`opencode.manage_server: true`）的路径也已同样处理。
- **坑中坑（P8）**：一开始把口令写在启动器里但没生效，原因是脚本编码问题。

### P8. PowerShell 5.1 读无 BOM 的 UTF-8 脚本会「吞行」

- **现象**：启动器里明明有 `$env:OPENCODE_SERVER_PASSWORD = '...'`，但 supervisor 里
  `env has OPENCODE_SERVER_PASSWORD: False`，opencode 仍然随机口令、bot 仍 401。
- **根因**：Windows PowerShell 5.1 对**无 BOM 的 UTF-8** 按系统 ANSI（GBK）解码，中文注释
  的字节序列在行尾可能和换行组合成非法 GBK 序列，导致**下一行被吞进注释**——赋值语句整行丢失。
- **解决**：所有含中文的 `.ps1` 一律保存为 **UTF-8 with BOM**：

  ```powershell
  $text = [IO.File]::ReadAllText($p, [Text.UTF8Encoding]::new($false))
  [IO.File]::WriteAllText($p, $text, [Text.UTF8Encoding]::new($true))
  ```

  验证：`[IO.File]::ReadAllBytes($p)[0..2]` 为 `ef bb bf`；启动器日志里 `pw_set=True`。
- **已固化**：`dafeiyu doctor` 会扫描 `scripts/*.ps1` 和 `data/runtime-launcher.ps1`，含非 ASCII
  却没有 BOM 时给出「PowerShell 脚本缺少 UTF-8 BOM」告警；`tests/test_scripts_bom.py` 把
  「仓库内 `scripts/*.ps1` 含中文必须带 BOM」钉成回归测试，改脚本时再犯会直接红。

### P9. 任务计划注册失败 / 账户名映射告警

- **现象 A**：`Register-ScheduledTask` / `schtasks /create` 报
  `账户名与安全标识间无任何映射完成`（`ERROR_NONE_MAPPED`）。本机真实本地账户名是 `yuan`，
  而当前登录会话/环境变量里是 `50456`（同一 SID），按名字注册任务会失败。
- **解决**：用 **XML + 用户 SID** 直接注册，绕开名字解析：

  ```powershell
  # XML 里 <UserId>S-1-5-21-...-1001</UserId>，LogonTrigger + 30s 延迟
  [IO.File]::WriteAllText($xmlPath, (Get-Content $xmlUtf8 -Raw), [Text.Encoding]::Unicode)
  schtasks /create /tn "dafeiyu-bot" /xml $xmlPath /f
  ```

- **现象 B**（无害）：supervisor 启动时告警
  `控制口令 ACL 加固失败（...supervisor.token）：50456: 账户名与安全标识间无任何映射完成`。
  这是 `icacls /grant <USERNAME>` 用到了不可解析的名字；只影响 token/密钥文件的 ACL 收紧，
  失败会降级为继承权限，不影响运行。想消掉可把启动器的 `USERNAME` 指向可解析的名字（如 `yuan`），
  或注销重登让名字缓存刷新。

### P10. `.env` 编码损坏 + 面板登录 + 模型档位

- **`.env`**：从旧机器拷来的 `.env` 是 GBK/有损编码（中文注释变 `???`），且**缺少
  `DEEPSEEK_API_KEY` 行**，导致 `api_key` 解析为空。按 `.env.example` 重写为 UTF-8（保留
  BOT_QQ / 面板口令等），LM Studio 场景补 `DEEPSEEK_API_KEY=lm-studio`。
- **面板登录**：不是注册制，账号固定 `admin`，口令在 `.env` 的 `DAFEIYU_DASHBOARD_PASSWORD`
  （绑定 `0.0.0.0` 时强制登录）。连续输错 5 次触发 5 分钟限流。Windows 下 `.env` 没有默认
  打开程序，双击打不开属正常，右键「打开方式→记事本」或用 `notepad <path>`。
- **模型档位**（`config.yaml` 的 `brain` + 面板切换）：
  - `free`：依赖 opencode 免费模型（需要 opencode 侧可用/认证；本机修好 P7 前一直不可用）；
  - `local_model`：需要本机 LM Studio/兼容端点（如 `http://127.0.0.1:1234/v1`）在位，否则
    切档报 `连不上本地模型端点`。切到这一档时端点没起会自动拉起，切走后自动停止
    （`local_model.stop_when_unused: false` 可关掉自动停止，让 LM Studio 常驻）；
  - `key`：自备 Key（DeepSeek 官方或兼容网关），本机最终使用这一档并验证可正常回复。
- **已固化**：`dafeiyu doctor` 会先把 `.env` 按 UTF-8 解码检查，不是合法 UTF-8 时直接报
  「`.env` 编码不是 UTF-8」并提示按 `.env.example` 重写；`config.yaml` 读不出 UTF-8 也不再抛
  裸异常，而是给同类的可执行提示。README「五分钟上手」补了 `.env` 在 Windows 下没有默认打开
  程序、面板口令位置/自动生成、free/local/key 三档依赖，减少首次部署的猜测。

### P11. 自启与手动启动的差异

- 自启仍推荐走 `data\runtime-launcher.ps1`（负责设定仓库路径、UTF-8 与日志后调用
  `scripts/start-all.ps1`）。**但自启/手动启动的 401 差异已不再存在**：P7 修复后 supervisor
  自己生成并同时注入口令，直接双击 `scripts\start.bat` 或跑 `start-all.ps1` 也能正常认证。
  启动器里再写 `$env:OPENCODE_SERVER_PASSWORD` 只用于「想固定口令」的场景。
- 任务计划建议：登录后延迟 30 秒（等网络/桌面就绪）、`MultipleInstancesPolicy=IgnoreNew`、
  `ExecutionTimeLimit=PT10M`（start-all 最长等 60 秒就退出）；进程由 supervisor 自己守护重启。

---

## 3. 排查手册（对照症状 → 先看哪里）

| 症状 | 优先检查 | 命令 / 位置 |
| --- | --- | --- |
| python/node/opencode 写用户目录 EPERM | 进程完整性级别、工作区 Mandatory Label | 先跑 `dafeiyu doctor`；`icacls <工作区>`；ctypes 读 `TokenIntegrityLevel` |
| 面板打不开 | supervisor 是否启动、8787 是否监听 | `logs\supervisor\supervisor.log`、`netstat -ano \| findstr :8787` |
| bot 日志出现 `NapCat 已连接` 才说明 QQ 通了 | NapCat 日志、8080 监听/连接 | `logs\supervisor\napcat.log`、`netstat -ano \| findstr :8080` |
| 模型不可用 / 切换失败 | opencode 认证、端点可达性 | opencode 日志里的 401；`curl -u opencode:<pw> http://127.0.0.1:24310/api/info` |
| 面板登录失败 | `.env` 里的口令、限流 | `data\audit.jsonl` 里 `auth.login` 记录 |
| 启动脚本行为异常（环境变量没生效） | `.ps1` 是否为 UTF-8 with BOM | 先跑 `dafeiyu doctor` 看 BOM 告警；`[IO.File]::ReadAllBytes($p)[0..2]` |
| `.env` 中文变 `???` / 凭据像没填 | 文件编码（应为 UTF-8） | 先跑 `dafeiyu doctor`，会直接报「`.env` 编码不是 UTF-8」 |
| 开机没自启 | 任务计划状态、启动器日志 | `schtasks /query /tn dafeiyu-bot`、`logs\supervisor\launcher.log` |

---

## 4. 换机部署清单（建议顺序）

1. 装 Python 3.11+（3.12 已验证）；**在干净目录**（不要在被沙箱降权的工作区里）创建 `.venv`。
   跑 `scripts/setup.ps1` 时它会在建 venv 前自检完整性级别和 `%TEMP%` 可写性。
2. `python -m venv .venv` → `pip install --no-cache-dir setuptools wheel` → `pip install --no-cache-dir --no-build-isolation -e .`
3. `python -m dafeiyu.cli init` 生成 `config.yaml`；`Copy-Item .env.example .env` 后填值。
4. `scripts/setup-napcat.ps1` 下载 NapCat（网络慢参考 P2）；`scripts/setup-opencode.ps1` 下载 opencode。
5. 确认运行环境**可写用户目录**：`dafeiyu doctor` 的「运行环境疑似被沙箱降权」项不应出现
   （等价手工检查：临时文件写入 `%TEMP%` 应成功）。
6. 启动后确认 opencode 认证：`curl -u opencode:<口令> http://127.0.0.1:24310/api/info` 为 200。
   **无需再手动注入口令**：supervisor 默认生成随机口令并同时给 serve 与 bot；要固定口令就写
   `opencode.server_password` 或 `.env` 的 `OPENCODE_SERVER_PASSWORD`（P7）。
7. 任务计划注册（P9 的 XML+SID 方案），重启验证自启。
8. `dafeiyu.cli doctor` + 面板登录 + 扫码 + 群里 @ 测试。

---

## 5. 后续改进建议（2026-10-31 更新）

以下原建议**已落地**：

- ~~supervisor 启动 opencode 时由配置项统一注入口令~~ → 已实现：`src/dafeiyu/brain/opencode_auth.py`
  + `supervisor/main.py` 的 `build_specs`，同一个 `OPENCODE_SERVER_PASSWORD` 注入 serve 与 bot；
  `opencode.server_password` / `.env` 可固定，留空自动生成（P7/P11）。
- ~~`scripts/setup*.ps1` 加临时文件自检~~ → 已实现：`src/dafeiyu/env_probe.py` 读完整性级别 +
  试写 `%TEMP%`；`scripts/setup.ps1` 建 venv 前自检，`dafeiyu doctor` 也查（P4/P5）。
- ~~所有 `.ps1` 在 dev 流程里校验 UTF-8 BOM~~ → 已实现：`dafeiyu doctor` 对
  `scripts/*.ps1` / `data/runtime-launcher.ps1` 检查 BOM；`tests/test_scripts_bom.py` 钉死回归（P8）。
- ~~README「五分钟上手」补充 `.env`/面板口令/档位依赖~~ → 已补；另外补了 `.env` 非 UTF-8 的
  doctor 检查（P10）和更新部署说明。

**仍靠文档 / 机器级操作、没有硬塞进代码的**：

- P1 代理 / schannel、P2 GitHub 下载慢：取决于具体机器网络和代理，安装脚本只保留 `-Proxy`
  与重试；极端慢时按 P2 的思路自建 Range 下载。
- P9A 任务计划 XML+SID 注册：属于账户/计划任务层，未做安装器；按 P9 的 XML 方案手工注册。
- P9B `icacls` 账户名映射告警：无害，仍会打印；不影响运行，想消掉可让启动器传入可解析的用户名。
- P6 NapCat 数据目录重定向：不自动改用户的 `Documents\Tencent Files`，靠 P4 根治（干净工作区）。
- P3 `ensurepip` / pip 缓存失败：根因仍是 P4 的 Low 完整性；doctor 只能提示，无法替用户修目录权限。

---

## 6. 已固化到代码/脚本里的保护（2026-10-31）

| 原问题 | 落点 | 行为 |
| --- | --- | --- |
| P4/P5 沙箱降权 | `src/dafeiyu/env_probe.py`；`dafeiyu doctor`；`scripts/setup.ps1` | 读 TokenIntegrityLevel + 试写 `%TEMP%`；Low/不可写时明确提示「先查完整性，别怀疑杀软」，并指向本文 P4/P5 |
| P7/P11 opencode 401 | `src/dafeiyu/brain/opencode_auth.py`；`supervisor/main.py`；`__main__.py`；`config.example.yaml`；`.env.example` | serve 与 bot 共用同一口令；留空每次启动随机生成、不落盘；配置或 `.env` 可固定；不再依赖启动器 |
| P8 PS 5.1 吞行 | `src/dafeiyu/bootstrap.py`；`tests/test_scripts_bom.py` | 中文 `.ps1` 无 BOM 时 doctor 告警；测试钉死 `scripts/*.ps1` 必须 UTF-8 with BOM |
| P10 `.env` 编码损坏 | `src/dafeiyu/bootstrap.py`；README | `.env` / `config.yaml` 非 UTF-8 给 error 与可执行修复提示；README 补打开方式、面板口令、档位依赖 |
| 部署中断留半套代码 | `scripts/deploy.ps1` | SHA256 后、重启前先跑目标目录 `dafeiyu doctor`；有阻塞项 throw 并走既有回滚；健康检查仍要求 `gateway.connected=true` |

相关回归测试：`tests/test_env_probe.py`、`tests/test_bootstrap.py`、`tests/test_supervisor_specs.py`、
`tests/test_opencode_auth.py`、`tests/test_scripts_bom.py`、`tests/test_deploy_script.py`。

> 注意：`.opencode/agents/*.md` 与 `data/settings.local.yaml` 是本机部署状态（当前为 key 档），
> 会让「仓库内 agent 文件与 free 模板一致」「默认 provider」这类测试失败；这是本机数据导致的，
> 与本次固化无关。部署到新机器时由 `ensure_agent_files` 按当前档位重新生成。
