# Gitea CI（Windows 自托管 runner）

本仓库的 CI 跑在**本机自托管的 Gitea runner** 上，使用 **host 模式**（不依赖 Docker）。
流水线定义在 `.gitea/workflows/ci.yml`，注册脚本是 `scripts/register-gitea-runner.ps1`。

## 为什么不用 Linux 容器

- 大量测试依赖 Windows 专有行为：PowerShell 脚本、计划任务、文件 ACL、`ntpath`、桌面 API。
  Linux 上这些要么被 `skipif` 跳过，要么跑出与生产不一致的结果（仓库里 28 个测试文件涉及 Windows 路径/进程）。
- 本机没有 Docker / WSL2，host 模式是唯一能直接跑起来的方式。
- 代价：Windows host **不支持 Docker actions**。workflow 只使用 v5.4.1 实际内置的
  `builtin:checkout`（该版本尚未发布 `builtin:upload-artifact`），不引用任何外部 JS action，也不需要联网下载 action。

## 前置条件

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| Gitea | >= 1.22.4（本机 1.27.3） | Actions 调度（`workflow_dispatch` 需要 1.22+） |
| Gitea Actions | `[actions] ENABLED=true` | 实例必须开启 |
| Gitea runner | v5.4.1 | 本文脚本固定此版本 |
| git | >= 2.34.1 | `builtin:checkout` |
| PowerShell | 7.x（`pwsh`） | workflow 的 `shell: pwsh` |
| Python | >= 3.11 | workflow 建 `.ci-venv` 并跑 pytest |

> runner 注册和 job 执行都用**当前登录用户**（计划任务模式），因此上面这些工具要在该用户的
> `PATH` 上。若改用 Windows 服务模式（LocalSystem），需要机器级安装 Python/git/pwsh。

## 一、在 Gitea 上开启 Actions 并取 token

1. 编辑 Gitea 的 `app.ini`，确认：

   ```ini
   [actions]
   ENABLED=true
   ```

   改完重启 Gitea。

2. 用管理员账号打开 `http://yuannas.local:3000/admin/actions/runners`，
   点「创建 Runner」，复制页面给出的 **registration token**。
   token 只用于注册，注册成功后 runner 用 `.runner` 文件里的凭据工作。

## 二、注册并启动 runner

在仓库根目录执行（推荐把 token 放环境变量，避免落进命令历史和进程列表）：

```powershell
$env:GITEA_RUNNER_REGISTRATION_TOKEN = '<粘贴 token>'
powershell -ExecutionPolicy Bypass -File scripts\register-gitea-runner.ps1
```

脚本会：

1. 检查 git / pwsh / Python 版本；
2. 把 `gitea-runner.exe` 下载到 `C:\gitea-runner`，并用固定的 SHA-256 校验；
3. 生成最小 `C:\gitea-runner\config.yaml`，并把 `runner.file` 固定为绝对路径
   `C:\gitea-runner\.runner`（runner 对显式 `--config` 指向不存在的文件会直接报错，
   且默认 `.runner` 是相对「当前工作目录」的）；
4. 执行
   `gitea-runner --config C:\gitea-runner\config.yaml register --no-interactive --instance http://yuannas.local:3000 --token *** --name <主机名> --labels windows-latest:host`；
5. 注册「登录时启动」的计划任务 `gitea-runner`，并立即启动。

先只想看命令、不实际执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register-gitea-runner.ps1 -DryRun
```

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-Instance` | `http://yuannas.local:3000` | Gitea 实例地址 |
| `-RunnerDir` | `C:\gitea-runner` | runner 二进制、`config.yaml`、`.runner`、日志所在目录 |
| `-RunnerName` | `$env:COMPUTERNAME` | 在 Gitea 里显示的 runner 名 |
| `-Labels` | `windows-latest:host` | 必须与 `ci.yml` 的 `runs-on` 匹配 |
| `-Autostart` | `Task` | `Task`（登录自启，当前用户）/ `Service`（开机自启，LocalSystem）/ `None` |
| `-Force` | 关 | 已注册时覆盖 `.runner` 重新注册；也会强制重新下载二进制 |
| `-ExpectedSha256` | v5.4.1 的官方值 | 传空字符串可跳过校验 |

> 换 runner 版本时，必须同步更新 `-Version` 和 `-ExpectedSha256`（官方 `checksums.txt` 里
> `gitea-runner-<版本>-windows-amd64.exe` 那一行），否则脚本会因哈希不匹配中止。

### 服务模式（可选）

需要开机即跑、不依赖登录会话时：

```powershell
# 管理员终端
powershell -ExecutionPolicy Bypass -File scripts\register-gitea-runner.ps1 -Autostart Service
```

LocalSystem 不会继承用户级 Python。要么机器级安装 Python/git/pwsh，要么继续用默认的计划任务模式。

## 三、验证

1. 打开 Gitea 仓库的 **Actions** 页，应看到 runner 在线（`/admin/actions/runners` 里状态为 Idle）。
2. 推送任意提交，或手动点 **Run workflow**（`workflow_dispatch`）。
3. 成功后应有一个绿色的 `pytest (Windows host)` job；失败时 job 日志末尾会打印 `junit.xml` 摘要。

本地复现 CI 做的事：

```powershell
# 干净环境模拟（可选；CI 每次都新建）
python -m venv .ci-venv
.\.ci-venv\Scripts\python.exe -m pip install --upgrade pip
.\.ci-venv\Scripts\python.exe -m pip install -e ".[dev,wechat]"
.\.ci-venv\Scripts\python.exe -m pip install "silk-python>=0.2.9"
.\.ci-venv\Scripts\python.exe -m compileall -q src
.\.ci-venv\Scripts\python.exe -m pytest -q --junitxml=junit.xml
```

本地全量基线（2026-11-09）：`3300 passed, 20 skipped`，约 5 分 40 秒。CI 超时设为 45 分钟。
本地 `exec` 实测同为 `3300 passed, 20 skipped`（5 分 42 秒），`Job succeeded`。

### 不推送也能本地空跑工作流

```powershell
# 关键：务必用 --env-file 指向一个空文件，否则 exec 会把仓库根目录的 .env
# 读进 job 环境，config 类测试会因为 BOT_QQ / DEEPSEEK_API_KEY 等被真实值覆盖而红。
$empty = Join-Path $env:TEMP 'ci-empty.env'
[IO.File]::WriteAllText($empty, '')
C:\gitea-runner\gitea-runner.exe exec -W .gitea/workflows/ci.yml -j test -i -self-hosted --env-file $empty
```

它会复制当前工作树到临时 workspace，然后按 workflow 执行。注意两点：

- `--dryrun` 只跳过 builtin 动作、仍会执行 `run:` 步骤，而且会让 workspace 为空，**不能**用它验证完整流程。
- exec 默认 `--env-file .env`（act 的行为）。仓库里有 `.env` 时，job 环境会被真实配置污染，
  `tests/test_config*.py`、`tests/test_dashboard_auth_config.py` 会失败——这不是 CI 的问题，
  真实 Gitea 的 workspace 是干净 clone，没有 `.env`。

### 已知的机器状态依赖

- `vendor/napcat/node.exe` 不在仓库里（`.gitignore`）。`build_specs()` 要求它存在，因此
  `tests/test_supervisor_specs.py` / `tests/test_wechat_failure_paths.py` 通过 `conftest.py`
  的 `fake_napcat` fixture 造一个临时安装目录并 monkeypatch `NAPCAT_DIR`，
  不依赖开发机装没装 NapCat。
- `tests/test_wechat_localdb_crypto.py` 会找 `sys.base_prefix/DLLs/sqlite3.dll`；本机 Anaconda
  把它放在 `Library/bin`，所以该文件 18 条用例在本地和 CI 都是 skip（不是 CI 引入的差异）。
- `tests/test_speech.py::test_real_silk_roundtrip` 需 `pysilk`（`silk-python`）。它在 `stt`
  extra 里，而 CI 不装 `stt`（`faster-whisper` 太重），所以 workflow 的安装步骤单独补了
  `silk-python`；否则这条用例会在 CI 静默 skip。

## 四、排障

| 现象 | 原因 / 处理 |
| --- | --- |
| job 一直 `Waiting`，没有 runner 接 | `runs-on: windows-latest` 没匹配到任何 label。检查注册时的 `-Labels` 是否为 `windows-latest:host`（大小写敏感）。用 `gitea-runner --config C:\gitea-runner\config.yaml daemon --labels windows-latest:host` 可临时覆盖。 |
| job 卡在 checkout | `git` 不可用或版本 < 2.34.1；或 runner 主机解析不到 `yuannas.local`。先在 runner 主机上 `git ls-remote http://yuannas.local:3000/yuan/dafeiyu-bot.git` 验证。 |
| `pwsh: command not found` | 没装 PowerShell 7，或不在该用户的 `PATH`。 |
| `Python >= 3.11 not found` | 该用户 `PATH` 上没有 `py`/`python`，或版本过低。装 Python 3.11+ 后重跑。 |
| `pip install` 很慢 / 超时 | 配 pip 镜像（见下）或给 runner 主机配代理。 |
| `tests/test_wechat_localdb_repository.py` import 失败 | 没装 `wechat` extra（缺 `zstandard`）。workflow 已装 `.[dev,wechat]`，本地复现别漏。 |
| 服务模式 `sc.exe start` 失败 | 看 `C:\gitea-runner\gitea-runner.log`；若是权限/工具缺失，改用 `-Autostart Task`。 |
| 想重新注册 | 先停掉任务/服务，再 `-Force`。`sc.exe delete gitea-runner` 可删服务；计划任务用 `Unregister-ScheduledTask -TaskName gitea-runner`。 |
| 本地 `exec` 里出现零星失败（套接字/端口类） | 同一 workspace 上同时跑了两个 pytest（例如上一次 `exec` 被中断后子进程没退）。`test_wechat_hook_server` 之类会抢端口。先 `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` 清掉残留 pytest 再重跑。 |
| `open config file ... config.yaml: The system cannot find the file specified` | `--config` 指向了不存在的文件。当前脚本会先生成最小配置；若手工注册，先写 `config.yaml` 再注册。 |
| 本地 `exec` 里 `test_config*` / `test_dashboard_auth_config` 报「DID NOT RAISE」「assert '3351825424' == '10001'」 | exec 把仓库 `.env` 读进了 job 环境。加 `--env-file <空文件>` 重跑（见上文）。 |
| `NapCat 未安装：<workspace>\vendor\napcat\node.exe 不存在` | 干净 checkout 没有 `vendor/`。测试已用 `fake_napcat` fixture 隔离；若你新写的测试直接调 `build_specs`，记得也加这个 fixture。 |

pip 镜像示例（可选）：

```powershell
$env:PIP_INDEX_URL = 'https://pypi.tuna.tsinghua.edu.cn/simple'
powershell -ExecutionPolicy Bypass -File scripts\register-gitea-runner.ps1
```

## 五、安全

- **runner token**：只用于注册。不要提交、不要贴进 issue、不要写进仓库文件。推荐用环境变量传入。
- **`.runner` 文件**：注册成功后生成，里面是 runner 的长期凭据。它已经在 `.gitignore` 的
  `C:\gitea-runner` 目录之外、不在仓库里；若你把 `-RunnerDir` 指到仓库内，务必确认它被忽略。
- **fork PR**：Gitea 对来自 fork 的 PR 的 secret 处理与 GitHub 类似，默认不暴露；但 host 模式的
  runner 会直接在宿主机执行被检出的代码。**只给可信仓库开启 PR 触发**，或改成只在 `push` 时跑。
- runner 以当前用户身份运行 job，意味着 job 里的代码拥有该用户的文件权限。不要让不可信的 PR 触发 CI。

## 六、这份 CI 覆盖什么、不覆盖什么

覆盖：

- 全量 `pytest`（Windows 语义）；
- 真实 SILK 编解码一条（`silk-python` 单独装，不拖 `stt` extra）；
- `compileall` 语法检查；
- 失败时把 `junit.xml` 打到 job 日志（v5.4.1 没有内置 artifact 上传）。

**暂不覆盖**（后续可在 `docs/superpowers/plans/2026-11-09-dev-roadmap.md` 的 P0/P2 里补）：

- `basedpyright` 类型门槛（现有基线尚有既存错误，先清理再加）；
- `ruff` lint 门槛（尚未在 `pyproject.toml` 声明配置）；
- 覆盖率门槛；
- 真实浏览器渲染（面板 E2E）。
