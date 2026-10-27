# 发布版凭据与本地模型接入 实施计划（P1–P3）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让拿到发行包的陌生用户不装 Node、不碰 `auth.json`，跑一个脚本后即可用「免费模型 / 自备 Key / 本地模型」三档中的任意一档聊天，且三档能在面板里随时切换并立即生效。

**Architecture:** 所有档位的思考者都是 opencode。唯一生效层是 **agent 文件**（`.opencode/agents/deepseek-qq.md`，钉住 model 与 permissions）+ `OPENCODE_CONFIG` 指向的 `opencode.jsonc`，两者都由 `brain/provider_config.py` 从「覆盖层 + 密钥文件」生成。配置三层：`config.yaml`（默认值）→ `data/settings.local.yaml`（面板写入的偏好）→ `data/secrets/provider.key`（机密）。切换档位的链路是：写覆盖层 → 重生两件生成物 → 重启 opencode 子进程 → 清会话映射 → 重建 brain。

**Tech Stack:** Python 3.11 / aiohttp / httpx / PyYAML / PowerShell 5.1 / opencode v2.0.18（bun 单文件 exe）

**Spec:** `docs/superpowers/specs/2026-10-28-release-credentials-design.md`（提交 `d45b51a`）

## Global Constraints

- Python `>=3.11`；仅 Windows（`icacls`、`taskkill`、`pythonw`、`node.exe`）。
- **不新增运行时依赖**：只用标准库 + 现有 `httpx` / `aiohttp` / `PyYAML` / `python-dotenv`。
- 模式枚举固定为 `free | key | local_model`。**禁止** `local`（与 `normalize_provider` 的 `local → token` 别名冲突）。
- 所有生成文件首行必须是 `// generated-by: dafeiyu`（agent md 用 `<!-- generated-by: dafeiyu -->`）。
- **机密绝不写进 `config.yaml` / 日志 / 诊断包**；key 只存 `data/secrets/provider.key`。
- 生成的三档配置**必须**带 `"plugins": []` 与显式 `limit.context` / `limit.output` / `capabilities`。
- permissions 必须是「`*` deny 打头 + 白名单」，**绝不** `read "*" allow`。
- 分支 `feat/qq-bot`；每任务一次提交；`git status` 必须干净后才进下一任务。
- 验收命令：`pytest -q`（当前基线 977 passed）与 `ruff check <改动文件>`。

## Review Focus

1. **用户机器上已有 opencode 且共享服务在跑** → 我们只认自己拉起的 `serve`（自有端口 + `OPENCODE_CONFIG`），任何状态检查都走 HTTP 直连自己的 `base_url`，不调用 `opencode` CLI 子命令。
2. **用户手工改过 `.opencode/agents/deepseek-qq.md`** → 首次生成必须备份为 `.bak.<时间戳>` 再覆盖，且 `doctor` 要提示，不能静默吃掉用户的改动。
3. **baseURL 尾斜杠 / 重复 `/v1` / 前后空格** → 探测与生成都必须经 `normalize_base_url()`，同一输入只能得到一个结果，不能拼出 `/v1/v1/models`。
4. **保存本地档时服务不可达** → 必须能「强制保存」；失败时覆盖层与密钥文件都不能留下半写状态（先写临时文件再替换）。
5. **切档后旧会话仍在用旧模型** → 切档链路必须调用 `store.clear_opencode_session()`，并在日志里打印新模型名。

---

## Phase P1 — 安装器与发行包

### Task 1: `resolve_opencode_bin` 增加 vendor 候选

**Files:**
- Modify: `src/dafeiyu/brain/opencode.py`（`resolve_opencode_bin`，约 60-110 行）
- Test: `tests/test_opencode_bin.py`

**Interfaces:**
- Consumes: `BrainError`（`brain/client.py`）
- Produces: `resolve_opencode_bin(configured: str = "") -> str`，候选顺序：`configured` → `INSTALL_ROOT/vendor/opencode/opencode.exe` → PATH 的 `opencode.exe` → `%APPDATA%/npm/node_modules/@opencode/cli/bin/opencode.exe` → `npm root -g`

- [ ] **Step 1: 写失败测试**

```python
def test_vendor_candidate_wins_over_path(tmp_path, monkeypatch):
    root = tmp_path
    exe = root / "vendor" / "opencode" / "opencode.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")
    monkeypatch.setattr(opencode, "INSTALL_ROOT", root)
    monkeypatch.setattr(opencode.shutil, "which", lambda name: None)
    assert opencode.resolve_opencode_bin("") == str(exe)


def test_missing_everything_raises_brainerror(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode, "INSTALL_ROOT", tmp_path)
    monkeypatch.setattr(opencode.shutil, "which", lambda name: None)
    monkeypatch.delenv("APPDATA", raising=False)
    with pytest.raises(BrainError):
        opencode.resolve_opencode_bin("")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_opencode_bin.py -v`
Expected: FAIL（`INSTALL_ROOT` 不存在 / 断言不成立）

- [ ] **Step 3: 实现**

在模块顶部加 `INSTALL_ROOT = Path(__file__).resolve().parents[2]`；把候选列表按上述顺序重建；`configured` 非空时保持原有语义（文件 → `shutil.which` → 抛错）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_opencode_bin.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/brain/opencode.py tests/test_opencode_bin.py
git commit -m "feat: opencode 解析增加 vendor 候选"
```

---

### Task 2: `scripts/setup-opencode.ps1` 下载单文件 exe

**Files:**
- Create: `scripts/setup-opencode.ps1`

**Interfaces:**
- Produces: `vendor/opencode/opencode.exe`；参数 `-Version <semver|latest>`、`-Proxy <url>`、`-Force`

- [ ] **Step 1: 写脚本**

结构照 `scripts/setup-napcat.ps1`：`$ErrorActionPreference = "Stop"`；目标 `Join-Path $PSScriptRoot "..\vendor\opencode\opencode.exe"`；幂等条件 `(Test-Path $exe) -and ((& $exe --version) -match '^\d+\.\d+\.\d+')` 且非 `-Force`；下载源用 npm 平台包直链 `https://registry.npmjs.org/@opencode/cli-windows-x64/-/cli-windows-x64-<ver>.tgz`（`-Version latest` 时先 `GET https://registry.npmjs.org/@opencode/cli-windows-x64/latest` 取 `version`）；解压 `.tgz` 后把 `package/bin/opencode.exe` 复制到目标；成功打印版本号，失败打印「可加 `-Proxy`，或手动下载后放到 vendor\opencode\opencode.exe 再重跑」。

- [ ] **Step 2: 手动验证（干净目录）**

Run: `powershell -ExecutionPolicy Bypass -File scripts\setup-opencode.ps1`
Expected: 打印 `opencode v<x.y.z> 已就绪`，`vendor/opencode/opencode.exe` 存在且 > 100 MB
Run: 再执行一次
Expected: 打印「已就绪，跳过」

- [ ] **Step 3: 提交**

```bash
git add scripts/setup-opencode.ps1
git commit -m "feat: setup-opencode.ps1 安装时下载 opencode.exe"
```

---

### Task 3: NapCat 拆成「下载」+「写 OneBot 配置」两阶段

**Files:**
- Modify: `scripts/setup-napcat.ps1:22-24`（幂等条件）、`:69-72`（缺 BOT_QQ 的抛错）、`:100-120`（写 onebot 配置段）

**Interfaces:**
- Produces: `scripts/setup-napcat.ps1 -DownloadOnly`（只下载解压）与默认行为（下载 + 写配置，写配置每次必做）

- [ ] **Step 1: 改幂等条件**

把「`napcat.bat` 存在就跳过」改成三个条件同时满足才跳过：`napcat.bat` 存在、`node.exe` 存在、`& node.exe --version` 退出码 0。

- [ ] **Step 2: 写配置每次必做**

新增 `-DownloadOnly` 开关：带该开关时读取 `BOT_QQ` 缺失也不抛错（直接 return 并提示「稍后运行不带 -DownloadOnly 的命令补写 OneBot 配置」）；不带开关时缺 `BOT_QQ` 仍抛错，但**无论** `onebot11_<BOT_QQ>.json` 是否已存在都重写一次（纯本地、可重入）。

- [ ] **Step 3: 手动验证两种顺序**

Run: `powershell -File scripts\setup-napcat.ps1 -DownloadOnly`（`.env` 里没有 BOT_QQ）
Expected: 解压成功、无异常退出
Run: 填好 `BOT_QQ` 后 `powershell -File scripts\setup-napcat.ps1`
Expected: 生成 `vendor/napcat/napcat/config/onebot11_<BOT_QQ>.json`，且内容含 `ws://127.0.0.1:8080`

- [ ] **Step 4: 提交**

```bash
git add scripts/setup-napcat.ps1
git commit -m "fix: NapCat 安装拆两阶段，补填 QQ 后能重建 OneBot 配置"
```

---

### Task 4: `package.ps1` 白名单打包

**Files:**
- Create: `src/dafeiyu/package_manifest.py`
- Create: `scripts/package.ps1`
- Test: `tests/test_package_manifest.py`

**Interfaces:**
- Produces:
  - `INCLUDE: tuple[str, ...]` = `("src", "scripts", ".opencode", "config.example.yaml", ".env.example", ".gitignore", "pyproject.toml", "README.md", "LICENSE")`
  - `EXCLUDE_DIRS: tuple[str, ...]` = `("__pycache__", ".pytest_cache", ".venv", "data", "logs", "vendor", "tests", "docs")`
  - `iter_payload(root: Path) -> list[Path]`（返回相对路径，已过滤 exclude 与 `config.yaml` / `.env`）
  - `scripts/package.ps1 -Out <zip>` 调用 `python -m dafeiyu.package_manifest --root . --out <zip>`

- [ ] **Step 1: 写失败测试**

```python
def test_payload_includes_runtime_essentials(tmp_path):
    for rel in ["src/dafeiyu/__init__.py", ".opencode/agents/deepseek-qq.md",
                ".env.example", "config.example.yaml", "pyproject.toml"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    names = {p.as_posix() for p in iter_payload(tmp_path)}
    assert ".opencode/agents/deepseek-qq.md" in names
    assert ".env.example" in names


def test_payload_excludes_secrets_and_heavy_dirs(tmp_path):
    for rel in ["data/dafeiyu.db", "vendor/napcat/node.exe", "config.yaml", ".env",
                "tests/test_x.py", "docs/superpowers/specs/a.md", "src/a.py"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    names = {p.as_posix() for p in iter_payload(tmp_path)}
    assert names == {"src/a.py"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_package_manifest.py -v`
Expected: FAIL（`ImportError: cannot import name 'iter_payload'`）

- [ ] **Step 3: 实现**

`iter_payload` 遍历 `INCLUDE` 中的每一项；目录用 `rglob("*")`；相对路径的**第一段**命中 `EXCLUDE_DIRS` 或文件名是 `config.yaml` / `.env` 则跳过；返回排序后的列表。`main()` 用 `zipfile.ZipFile(..., ZIP_DEFLATED)` 写包并打印文件数与字节数。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_package_manifest.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/package_manifest.py scripts/package.ps1 tests/test_package_manifest.py
git commit -m "feat: package.ps1 白名单打包，排除 data/vendor/tests"
```

---

### Task 5: `setup.ps1` 九段串联 + `setup.bat` + doctor 文案

**Files:**
- Modify: `scripts/setup.ps1`（整体重排）
- Create: `scripts/setup.bat`
- Modify: `src/dafeiyu/bootstrap.py`（opencode 缺失时指向 `setup-opencode.ps1`）

**Interfaces:**
- Consumes: Task 2 的 `setup-opencode.ps1`、Task 3 的 `setup-napcat.ps1 -DownloadOnly`
- Produces: `scripts/setup.ps1` 阶段 0–8；`scripts/setup.bat` = `powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*`

- [ ] **Step 1: 重排顺序**

0 Python 预检（`python --version` 解析 `3.11`+，失败给 `https://www.python.org/downloads/` 并退出）→ 1 venv → 2 依赖 → 3 `init` 配置 → 4 `.env` 向导（缺 `.env` 时复制模板并交互问 `BOT_QQ`，写回）→ 5 NapCat 下载（`-DownloadOnly`）→ 6 NapCat 写 OneBot 配置 → 7 opencode 下载 → 8 `doctor` 并透传退出码。

- [ ] **Step 2: doctor 文案**

`bootstrap.py` 中 opencode 缺失分支的 `Issue.fix` 改为 `scripts\\setup-opencode.ps1`（不再出现 `npm i -g`）。

- [ ] **Step 3: 手动验证**

Run: 干净目录（`%TEMP%\opencode\relcheck2`）复制发行包内容后 `scripts\setup.bat`
Expected: 八段全部打印；末尾 `doctor` 只剩「缺 DEEPSEEK_API_KEY 不适用」类提示，无 opencode/NapCat 缺失

- [ ] **Step 4: 提交**

```bash
git add scripts/setup.ps1 scripts/setup.bat src/dafeiyu/bootstrap.py
git commit -m "feat: setup.ps1 九段串联并新增 setup.bat"
```

---

### Task 6: 模板默认档改为「opencode + 免费模式」

**Files:**
- Modify: `config.example.yaml`（`brain` 段）、`tests/conftest.py`、`tests/test_config_phase2.py`

**Interfaces:**
- Produces: 模板 `brain: {provider: opencode, mode: free}`；`Settings.brain.mode: str = "free"`

- [ ] **Step 1: 写失败测试**

```python
def test_example_config_defaults_to_free_opencode():
    text = Path("config.example.yaml").read_text(encoding="utf-8")
    assert "provider: opencode" in text
    assert "mode: free" in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_config_phase2.py -k example_config -v`
Expected: FAIL（模板里还是 `provider: token`）

- [ ] **Step 3: 改模板 + dataclass**

`BrainConfig` 增加 `mode: str = "free"`；`config.example.yaml` 的 `brain:` 段改为 `provider: opencode` + `mode: free` + 一行注释「free=opencode 免费模型，无需任何 key」。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest -q`
Expected: 全绿（若 `conftest.py` 的测试配置断言旧默认，按新值同步）

- [ ] **Step 5: 提交**

```bash
git add config.example.yaml tests/test_config_phase2.py src/dafeiyu/config.py
git commit -m "feat: 模板默认免费档 opencode + mode free"
```

---

## Phase P2 — 三档配置

### Task 7: 覆盖层读写

**Files:**
- Create: `src/dafeiyu/settings_local.py`
- Test: `tests/test_settings_local.py`

**Interfaces:**
- Produces:
  - `VALID_MODES: tuple[str, ...] = ("free", "key", "local_model")`
  - `class OverrideError(Exception)`
  - `normalize_mode(value: str) -> str`（strip + casefold；非法抛 `OverrideError`）
  - `read_overrides(path: Path | None = None) -> dict[str, Any]`（文件不存在返回 `{}`）
  - `write_overrides(mapping: dict[str, Any], path: Path | None = None) -> Path`（同目录 `mkstemp` → 写 → `os.replace`）
  - 默认路径 `data/settings.local.yaml`（`INSTALL_ROOT = Path(__file__).resolve().parents[2]`）

- [ ] **Step 1: 写失败测试**

```python
def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "settings.local.yaml"
    write_overrides({"brain": {"mode": "key", "key": {"model": "deepseek-chat"}}}, p)
    assert read_overrides(p)["brain"]["mode"] == "key"


def test_read_missing_returns_empty(tmp_path):
    assert read_overrides(tmp_path / "nope.yaml") == {}


@pytest.mark.parametrize("bad", ["local", "", "FREE!"])
def test_normalize_mode_rejects_unknown(bad):
    with pytest.raises(OverrideError):
        normalize_mode(bad)


def test_normalize_mode_accepts_free_key_local_model():
    assert normalize_mode(" Local_Model ") == "local_model"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_settings_local.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_settings_local.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/settings_local.py tests/test_settings_local.py
git commit -m "feat: 覆盖层 settings.local.yaml 读写"
```

---

### Task 8: 密钥文件（先加固后替换）

**Files:**
- Create: `src/dafeiyu/secrets_store.py`
- Test: `tests/test_secrets_store.py`

**Interfaces:**
- Produces:
  - `SECRETS_DIR: Path`（`data/secrets`）、`KEY_PATH: Path`（`data/secrets/provider.key`）
  - `write_secret(value: str, path: Path = KEY_PATH) -> Path`（strip；空值删除文件；`mkstemp(dir=path.parent)` → 写 → `harden_token_file` → `os.replace`）
  - `read_secret(path: Path = KEY_PATH) -> str`
  - `secret_exists(path: Path = KEY_PATH) -> bool`
  - `mask(value: str) -> str`（长度 ≤8 返回 `"***"`，否则 `value[:4] + "***" + value[-4:]`）
- Consumes: `supervisor.control_token.harden_token_file`

- [ ] **Step 1: 写失败测试**

```python
def test_write_then_read(tmp_path, monkeypatch):
    target = tmp_path / "secrets" / "provider.key"
    calls = []
    monkeypatch.setattr(secrets_store, "harden_token_file", lambda p: calls.append(p))
    write_secret("sk-abcdef123456", target)
    assert read_secret(target) == "sk-abcdef123456"
    assert calls, "必须先加固临时文件再替换"
    assert "sk-abcdef123456" not in "".join(x.name for x in target.parent.iterdir())


def test_empty_value_removes_file(tmp_path):
    target = tmp_path / "provider.key"
    write_secret("sk-x", target)
    write_secret("   ", target)
    assert not target.exists()


def test_mask_short_value():
    assert mask("short") == "***"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_secrets_store.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_secrets_store.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/secrets_store.py tests/test_secrets_store.py
git commit -m "feat: provider key 文件先加固后替换"
```

---

### Task 9: `load_settings` 合入覆盖层

**Files:**
- Modify: `src/dafeiyu/config.py`（`load_settings`，约 701-730 / 840-860）
- Test: `tests/test_settings_overrides.py`

**Interfaces:**
- Consumes: Task 7 的 `read_overrides`
- Produces: `Settings.brain.mode`；`Settings.brain.key_base_url` / `key_model`；覆盖层可改 `local_model.base_url` / `local_model.model` / `local_model.context` / `local_model.lean`；非法 mode 抛 `ConfigError`

- [ ] **Step 1: 写失败测试**

```python
def test_override_sets_mode(tmp_path, monkeypatch):
    write_overrides({"brain": {"mode": "key", "key": {"base_url": "https://api.x/v1", "model": "m"}}},
                    tmp_path / "settings.local.yaml")
    monkeypatch.setattr(config, "OVERRIDES_PATH", tmp_path / "settings.local.yaml")
    s = config.load_settings(config_file=CONFIG_FIXTURE, env_file=ENV_FIXTURE)
    assert s.brain.mode == "key"
    assert s.brain.key_model == "m"


def test_invalid_mode_in_overrides_raises_configerror(tmp_path, monkeypatch):
    write_overrides({"brain": {"mode": "local"}}, tmp_path / "settings.local.yaml")
    monkeypatch.setattr(config, "OVERRIDES_PATH", tmp_path / "settings.local.yaml")
    with pytest.raises(config.ConfigError, match="free"):
        config.load_settings(config_file=CONFIG_FIXTURE, env_file=ENV_FIXTURE)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_settings_overrides.py -v`
Expected: FAIL（`BrainConfig` 无 `mode` / 覆盖层未合入）

- [ ] **Step 3: 实现**

在 `load_settings` 读完 `config.yaml` 之后、构造 `Settings` 之前插入 `_apply_overrides(raw, read_overrides(OVERRIDES_PATH))`：只接受白名单键（`brain.mode`、`brain.key.base_url`、`brain.key.model`、`local_model.base_url`、`local_model.model`、`local_model.context`、`local_model.lean`），其余键忽略；`mode` 经 `normalize_mode` 转换，异常包成 `ConfigError`。

同时清理 `data/runtime.json` 里的 provider 覆盖：覆盖层一旦给出 `brain.mode`，`runtime.json` 中的旧 provider 值不再参与（否则面板看到的档位与运行时不符）。`BrainConfig` 新增 `key_base_url: str = ""`、`key_model: str = ""`；`LocalModelConfig` 新增 `context: int = 0`、`lean: bool = False`。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_settings_overrides.py -v`
Expected: 2 passed
Run: `pytest -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/config.py tests/test_settings_overrides.py
git commit -m "feat: load_settings 合入覆盖层与 mode 枚举"
```

---

### Task 10: 生成 `opencode.jsonc`（免费档）

**Files:**
- Create: `src/dafeiyu/brain/provider_config.py`
- Test: `tests/test_provider_config.py`

**Interfaces:**
- Produces:
  - `GENERATED_MARK = "// generated-by: dafeiyu"`
  - `FREE_MODEL = "opencode/deepseek-v4.1-flash-free"`
  - `build_config(mode: str, cfg: BrainConfig, *, key_rel: str | None = None, local: LocalModelConfig | None = None) -> dict[str, Any]`
  - `render_config(data: dict) -> str`（首行为 `GENERATED_MARK`，JSON 缩进 2）
  - `config_path(config_home: str) -> Path`（`<config_home>/opencode/opencode.jsonc`）

- [ ] **Step 1: 写失败测试**

```python
def test_free_mode_config_shape():
    cfg = BrainConfig()
    out = build_config("free", cfg)
    assert out["model"] == "opencode/deepseek-v4.1-flash-free"
    assert out["plugins"] == []
    assert "providers" not in out


def test_render_starts_with_generated_mark():
    text = render_config(build_config("free", BrainConfig()))
    assert text.splitlines()[0] == GENERATED_MARK
    assert json.loads("\n".join(text.splitlines()[1:]))["plugins"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_provider_config.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_provider_config.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/brain/provider_config.py tests/test_provider_config.py
git commit -m "feat: 生成 opencode 配置（免费档）"
```

---

### Task 11: 生成 `opencode.jsonc`（自备 Key / 本地模型）

**Files:**
- Modify: `src/dafeiyu/brain/provider_config.py`
- Test: `tests/test_provider_config.py`

**Interfaces:**
- Produces: `build_config("key" | "local_model", cfg, key_rel="../../../secrets/provider.key")` 生成 `providers.dafeiyu-key` / `providers.dafeiyu-local`，含 `package`、`settings.baseURL`、`settings.apiKey`（`{file:<rel>}`，仅 `key` 档与本地档填了 key 时）、`models.<name>.limit.context` / `.limit.output` / `.capabilities`

- [ ] **Step 1: 写失败测试**

```python
def test_key_mode_uses_file_reference():
    cfg = BrainConfig(mode="key", key_base_url="https://api.x/v1/", key_model="deepseek-chat")
    out = build_config("key", cfg, key_rel="../../../secrets/provider.key")
    prov = out["providers"]["dafeiyu-key"]
    assert prov["package"] == "@opencode/ai/providers/openai-compatible"
    assert prov["settings"]["baseURL"] == "https://api.x/v1"
    assert prov["settings"]["apiKey"] == "{file:../../../secrets/provider.key}"
    assert "sk-" not in json.dumps(out)


def test_local_mode_writes_limit_and_capabilities():
    local = LocalModelConfig(base_url="http://127.0.0.1:1234/v1", model="qwen3", context=32768)
    out = build_config("local_model", BrainConfig(mode="local_model"), local=local)
    model = out["providers"]["dafeiyu-local"]["models"]["qwen3"]
    assert model["limit"]["context"] == 32768
    assert model["limit"]["output"] == 8192
    assert model["capabilities"]["tools"] is True
    assert "apiKey" not in out["providers"]["dafeiyu-local"]["settings"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_provider_config.py -v`
Expected: FAIL（`KeyError: 'providers'`）

- [ ] **Step 3: 实现**

baseURL 一律经 `normalize_base_url()`。该函数**本任务里定义在 `provider_config.py`**（`normalize_base_url(url: str) -> str`：strip、去尾 `/`、把重复的尾部 `/v1/v1` 收敛成 `/v1`），Task 15 的 `model_probe.py` 从 `provider_config` 导入它，不重复实现。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_provider_config.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/brain/provider_config.py tests/test_provider_config.py
git commit -m "feat: 生成自备 Key / 本地模型 provider 配置"
```

---

### Task 12: agent 文件生成（唯一生效层）

**Files:**
- Create: `src/dafeiyu/brain/agent_template.md`
- Modify: `src/dafeiyu/brain/provider_config.py`
- Test: `tests/test_agent_generation.py`

**Interfaces:**
- Produces:
  - `AGENT_MARK = "<!-- generated-by: dafeiyu -->"`
  - `build_agent(mode: str, *, lean: bool, template: str) -> str`
  - `write_generated(path: Path, content: str) -> bool`（返回是否备份过；非生成态且内容不同 → 备份 `.bak.<YYYYmmdd-HHMMSS>`）
  - `agent_path(root: Path) -> Path`（`<root>/.opencode/agents/deepseek-qq.md`）

- [ ] **Step 1: 写失败测试**

```python
def test_build_agent_pins_model_for_mode():
    text = build_agent("key", lean=False, template=MIN_TEMPLATE)
    assert text.splitlines()[0] == AGENT_MARK
    assert "model: dafeiyu-key/" in text
    assert "model: opencode/deepseek-v4.1-flash-free" not in text


def test_build_agent_denies_shell_and_secrets():
    text = build_agent("free", lean=False, template=MIN_TEMPLATE)
    assert 'action: "*"' in text and "effect: deny" in text
    for secret in (".env", "data/secrets", "data/dashboard.password", "data/supervisor.token"):
        assert secret in text
    assert 'read: "*"' not in text


def test_write_generated_backs_up_manual_file(tmp_path):
    p = tmp_path / "deepseek-qq.md"
    p.write_text("手写的", encoding="utf-8")
    assert write_generated(p, AGENT_MARK + "\n新内容") is True
    assert list(tmp_path.glob("deepseek-qq.md.bak.*"))
    assert p.read_text(encoding="utf-8").startswith(AGENT_MARK)


def test_write_generated_is_idempotent(tmp_path):
    p = tmp_path / "deepseek-qq.md"
    write_generated(p, AGENT_MARK + "\nA")
    assert write_generated(p, AGENT_MARK + "\nA") is False
    assert not list(tmp_path.glob("*.bak.*"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_agent_generation.py -v`
Expected: FAIL（函数不存在）

- [ ] **Step 3: 实现**

`agent_template.md` 存人设正文（从现有 `.opencode/agents/deepseek-qq.md` 的 `:8-49` 抽出，去掉 model 与 permissions 两段），`build_agent` 用模板渲染 front-matter：`model:` 按档位取 `opencode/deepseek-v4.1-flash-free` / `dafeiyu-key/<key_model>` / `dafeiyu-local/<local_model>`；`permissions` 固定为 `*` deny + 白名单（`data/bot-workspace/**` read/edit、`webfetch`、`~/.cache/opencode/models.json` read、显式 deny `.env`/`data/secrets/*`/`data/*.password`/`data/*.token`）；`lean=True` 时去掉 read/edit 与 webfetch 白名单并在正文尾部追加「本轮没有工具」一句。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_agent_generation.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/brain/agent_template.md src/dafeiyu/brain/provider_config.py tests/test_agent_generation.py
git commit -m "feat: agent 文件成为生成物，钉住 model 与 permissions"
```

---

### Task 13: `ensure_isolated_config` 让位给生成物

**Files:**
- Modify: `src/dafeiyu/brain/runtime_config.py:33-49`
- Test: `tests/test_runtime_config.py`

**Interfaces:**
- Produces: `ensure_isolated_config(config_home: str) -> Path` 新语义：文件不存在 → 写 MINIMAL；首行是 `// generated-by: dafeiyu` → 不动；等于旧 MINIMAL 原文 → 不动；其他 → 备份 `.bak.<ts>` 后写 MINIMAL 并返回备份路径（供 doctor 提示）

- [ ] **Step 1: 写失败测试**

```python
def test_generated_file_is_left_alone(tmp_path):
    p = provider_config.config_path(str(tmp_path))
    p.parent.mkdir(parents=True)
    p.write_text("// generated-by: dafeiyu\n{}", encoding="utf-8")
    ensure_isolated_config(str(tmp_path))
    assert p.read_text(encoding="utf-8").startswith("// generated-by: dafeiyu")


def test_manual_file_is_backed_up(tmp_path):
    p = provider_config.config_path(str(tmp_path))
    p.parent.mkdir(parents=True)
    p.write_text('{"model":"x"}', encoding="utf-8")
    ensure_isolated_config(str(tmp_path))
    assert list(p.parent.glob("opencode.jsonc.bak.*"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_runtime_config.py -v`
Expected: FAIL（生成物被覆盖成 MINIMAL）

- [ ] **Step 3: 实现**

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_runtime_config.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/brain/runtime_config.py tests/test_runtime_config.py
git commit -m "fix: 隔离配置不再覆盖生成物"
```

---

### Task 14: 控制口 `POST /api/brain` 扩展三档

**Files:**
- Modify: `src/dafeiyu/control.py:507-543`（`switch`）、`:987-992`（路由处理）
- Test: `tests/test_control_brain_mode.py`

**Interfaces:**
- Consumes: Task 7/8/10/11/12 的写入与生成函数
- Produces: `BrainControl.switch_mode(payload: dict[str, Any]) -> dict[str, Any]`，payload 形如
  ```json
  {"mode": "key", "key": {"base_url": "...", "model": "...", "api_key": "..."},
   "local": {"base_url": "...", "model": "...", "context": 32768, "lean": false, "api_key": ""},
   "force": false}
  ```
  返回 `{"mode": "...", "model": "...", "reload": "ok|pending", "warnings": [...]}`

- [ ] **Step 1: 写失败测试**

```python
async def test_switch_to_key_writes_everything(tmp_path, monkeypatch, control):
    monkeypatch.setattr(control_mod, "OVERRIDES_PATH", tmp_path / "settings.local.yaml")
    out = await control.switch_mode({"mode": "key", "key": {"base_url": "https://api.x/v1", "model": "m", "api_key": "sk-test-123456"}})
    assert out["mode"] == "key"
    assert read_overrides(tmp_path / "settings.local.yaml")["brain"]["mode"] == "key"
    assert read_secret(...) == "sk-test-123456"
    assert "sk-test-123456" not in json.dumps(out)


async def test_invalid_mode_returns_400(control):
    with pytest.raises(SwitchError) as exc:
        await control.switch_mode({"mode": "local"})
    assert exc.value.status == 400


async def test_failed_probe_leaves_no_partial_write(tmp_path, monkeypatch, control):
    monkeypatch.setattr(control_mod, "OVERRIDES_PATH", tmp_path / "settings.local.yaml")
    with pytest.raises(SwitchError):
        await control.switch_mode({"mode": "local_model",
                                   "local": {"base_url": "http://127.0.0.1:1/v1", "model": "x"}})
    assert not (tmp_path / "settings.local.yaml").exists()


async def test_switch_clears_session_cache(control, store_spy):
    await control.switch_mode({"mode": "free"})
    assert store_spy.cleared == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_control_brain_mode.py -v`
Expected: FAIL（`switch_mode` 不存在）

- [ ] **Step 3: 实现**

顺序固定：校验 → （非 force 时）探活 → 写 key → 写覆盖层 → 重生 jsonc + agent → `reload`（调用 supervisor `POST /api/opencode/reload`，失败只记 `warnings`）→ `store.clear_opencode_session()` → 重建 brain。任一步失败都不留半写状态。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_control_brain_mode.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/control.py tests/test_control_brain_mode.py
git commit -m "feat: 控制口支持三档切换并重生生成物"
```

---

### Task 15: 模型探测与上下文分级

**Files:**
- Create: `src/dafeiyu/model_probe.py`
- Modify: `src/dafeiyu/control.py`（`/api/brain/models` 支持任意 baseURL）
- Test: `tests/test_model_probe.py`

**Interfaces:**
- Produces:
  - `normalize_base_url(url: str) -> str`（strip、去尾 `/`、去掉重复的尾部 `/v1/v1`）
  - `class ProbeError(Exception)`
  - `async def probe_models(base_url: str, *, api_key: str = "", timeout: float = 8.0) -> list[str]`
  - `context_grade(context: int, *, p90: int = 21062) -> str`（`< 0.75*p90` → `"red"`，`< 1.5*p90` → `"yellow"`，否则 `"green"`）

- [ ] **Step 1: 写失败测试**

```python
def test_normalize_strips_trailing_slash_and_duplicate_v1():
    assert normalize_base_url(" http://h:1234/v1/ ") == "http://h:1234/v1"
    assert normalize_base_url("http://h:1234/v1/v1/") == "http://h:1234/v1"


def test_probe_never_shells_out(monkeypatch):
    # Review Focus #1：探测只能走 HTTP，不能调 opencode CLI（会连到用户已运行的共享服务）
    def boom(*a, **k):
        raise AssertionError("不允许调用 subprocess")
    monkeypatch.setattr(model_probe.subprocess, "run", boom, raising=False)
    monkeypatch.setattr(model_probe, "_get", FakeGet([{"id": "m"}]))
    asyncio.run(probe_models("http://h:1234/v1"))


def test_context_grade_boundaries():
    assert context_grade(8192) == "red"
    assert context_grade(20000) == "yellow"
    assert context_grade(32768) == "green"


async def test_probe_models_reads_openai_ids(monkeypatch):
    monkeypatch.setattr(model_probe, "_get", FakeGet([{"id": "qwen3"}, {"id": "llama"}]))
    assert await probe_models("http://h:1234/v1") == ["qwen3", "llama"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_model_probe.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

用 `httpx.AsyncClient`；`GET {base}/models`，`Authorization: Bearer <key>`（key 为空则不发）；非 200 或 JSON 无 `data` → `ProbeError("地址不可达或不是 OpenAI 兼容接口")`；超时同上。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_model_probe.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/model_probe.py tests/test_model_probe.py src/dafeiyu/control.py
git commit -m "feat: 本地服务模型探测与上下文分级"
```

---

### Task 16: supervisor reload 接口 + 面板代理

**Files:**
- Modify: `src/dafeiyu/supervisor/dashboard.py`（新增路由）
- Modify: `src/dafeiyu/supervisor/main.py`（把 manager 传给 `create_app`）
- Test: `tests/test_dashboard_reload.py`

**Interfaces:**
- Produces: `POST /api/opencode/reload` → `{"ok": true, "pid": <int>}`；失败 `{"ok": false, "error": "..."}` + 503

- [ ] **Step 1: 写失败测试**

```python
async def test_reload_restarts_opencode_and_waits_ready(client, fake_manager):
    resp = await client.post("/api/opencode/reload")
    assert resp.status == 200
    assert (await resp.json())["ok"] is True
    assert fake_manager.restarted == ["opencode"]


async def test_reload_reports_failure(client, fake_manager_failing):
    resp = await client.post("/api/opencode/reload")
    assert resp.status == 503
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_dashboard_reload.py -v`
Expected: FAIL（404）

- [ ] **Step 3: 实现**

处理函数调用 `manager.restart("opencode")`，然后轮询 `ready_url`（`http://{host}:{port}/api/info`）最多 `startup_timeout_seconds`；路由挂在鉴权中间件之后（默认需登录）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_dashboard_reload.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/supervisor/dashboard.py src/dafeiyu/supervisor/main.py tests/test_dashboard_reload.py
git commit -m "feat: 面板提供 opencode 重载接口"
```

---

### Task 17: 面板三档 UI

**Files:**
- Modify: `src/dafeiyu/supervisor/dashboard/index.html:729-1110`（大脑卡片）、`:2200-2240`（提交逻辑）
- Test: `tests/test_dashboard_html.py`

**Interfaces:**
- Consumes: Task 14 的 payload 结构、Task 15 的探测
- Produces: 三张模式卡（`data-mode="free|key|local_model"`）、`#key-base-url` / `#key-model` / `#key-api-key` / `#local-base-url` / `#local-model-select` / `#local-context` / `#local-lean` / `#probe-btn`；保存时 `POST /api/brain` 带 `mode` 与对应字段

- [ ] **Step 1: 写失败测试**

```python
def test_html_has_three_mode_cards():
    html = Path("src/dafeiyu/supervisor/dashboard/index.html").read_text(encoding="utf-8")
    for mode in ("free", "key", "local_model"):
        assert f'data-mode="{mode}"' in html
    assert "#local-context" not in html  # 用 id 属性而不是 CSS 选择器
    assert 'id="local-context"' in html
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_dashboard_html.py -v`
Expected: FAIL（没有 `data-mode` 三档）

- [ ] **Step 3: 实现**

保留现有 `MODE_ORDER` 结构；新增字段只在选中对应卡时显示；`api_key` 输入框 `type="password"`、`placeholder` 显示已保存时的掩码；探测按钮调 `/api/brain/models?base_url=...` 填充下拉；`local-context` 失焦时本地计算红/黄/绿并用 CSS class 上色。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_dashboard_html.py -v`
Expected: 1 passed
Run: `pytest -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add src/dafeiyu/supervisor/dashboard/index.html tests/test_dashboard_html.py
git commit -m "feat: 面板三档模式卡片与本地模型探测"
```

---

### Task 18: 免费档可用性自检与降级提示（spec §4、§9 第一行）

**Files:**
- Modify: `src/dafeiyu/model_probe.py`（新增 `probe_free_model`）
- Modify: `src/dafeiyu/control.py`（`health_snapshot` 与 `/api/brain` 暴露 `free_available`）
- Modify: `src/dafeiyu/supervisor/dashboard/index.html`（顶栏黄条）
- Test: `tests/test_free_probe.py`

**Interfaces:**
- Produces:
  - `async def probe_free_model(client: Any, *, model: str = FREE_MODEL, timeout: float = 8.0) -> bool`
  - `FREE_PROBE_TTL = 600`（秒）
  - `control.RuntimeState.free_available: bool | None`（`None` = 未探测）
- Consumes: Task 11 的 `FREE_MODEL`；Task 10 的 `GENERATED_MARK`

**行为约定：**
- 探测只走 HTTP（直连我们自己的 opencode `server_url`），**不调 `opencode` CLI**（Review Focus #1）。
- 探测 = 一次最小补全（空/单字符 prompt + 极小输出），因此会消耗极少量 tokens；结果缓存 `FREE_PROBE_TTL`。
- 任何异常（401/403/404/429/超时/连接失败/model 不存在）→ `False`；**不抛出、不阻塞启动**。
- 只在 `mode == "free"` 且 opencode 已 ready 时探测；失败仅置标志，机器人照常运行。

- [ ] **Step 1: 写失败测试**

```python
async def test_probe_returns_false_on_auth_error(monkeypatch):
    monkeypatch.setattr(model_probe, "_get", FailingGet(401))
    monkeypatch.setattr(model_probe, "_post", FailingPost(401))
    assert await probe_free_model(object()) is False


async def test_probe_returns_true_on_success(monkeypatch):
    monkeypatch.setattr(model_probe, "_post", OkayPost())
    assert await probe_free_model(object()) is True


async def test_probe_never_raises_on_timeout(monkeypatch):
    monkeypatch.setattr(model_probe, "_post", TimeoutPost())
    assert await probe_free_model(object()) is False


def test_probe_uses_http_not_subprocess(monkeypatch):
    monkeypatch.setattr(model_probe.subprocess, "run", _boom, raising=False)
    asyncio.run(probe_free_model(object()))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_free_probe.py -v`
Expected: FAIL（`probe_free_model` 不存在）

- [ ] **Step 3: 实现 `probe_free_model`**

复用 `model_probe` 里已有的 httpx 调用封装；按 `timeout` 截断；全部异常吞掉返回 `False`。`control.py` 在 opencode ready 后启动一次探测任务并把结果写进 `RuntimeState.free_available`，`/api/brain` 快照带上该字段。

- [ ] **Step 4: 面板黄条**

`index.html`：`data.free_available === false` 时在顶栏渲染 `<div id="free-banner" class="warn">免费模型当前不可用，请切换到「自备 Key」或「本地模型」</div>`，附一个跳到大脑卡片的按钮；`true` 或 `null` 时不渲染。

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_free_probe.py -v`
Expected: 4 passed
Run: `pytest -q`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add src/dafeiyu/model_probe.py src/dafeiyu/control.py src/dafeiyu/supervisor/dashboard/index.html tests/test_free_probe.py
git commit -m "feat: 免费档可用性自检与面板降级提示"
```

---

## Phase P3 — 活体验收

### Task 19: 端到端活体验收（不写代码）

**Files:** 无（只跑命令与看日志）

- [ ] **Step 1: 干净目录装配**

把 `scripts\package.ps1` 产出的 zip 解压到 `%TEMP%\opencode\relcheck3`，跑 `scripts\setup.bat`，填 `BOT_QQ`。
Expected: 0 个阻塞项；`vendor/opencode/opencode.exe` 与 `vendor/napcat/napcat/napcat.bat` 都存在

- [ ] **Step 2: 免费档**

启动 → 面板显示「免费」→ 群里发一句 → 回复正常；`logs/dafeiyu.log` 里 opencode 请求的 model 是 `opencode/deepseek-v4.1-flash-free`。

- [ ] **Step 3: 自备 Key**

面板切「自备 Key」，填错误 key → 立即报 401、覆盖层未被写坏；换正确 key → 能聊；日志里的 model 换成新名字。

- [ ] **Step 4: 本地档**

起 LM Studio（或任意 OpenAI 兼容服务）→ 填地址 → 点探测出模型列表 → 选模型 → 聊天；上下文分级显示颜色正确。

- [ ] **Step 5: 落记录**

把三次验收的关键日志行与截图路径写进 `.tamako/evidence.md`，并 `git status` 确认干净。

---

## 后续（本计划不含，另立计划）

- **P4 产品化包**：首次向导四问、`dafeiyu diag`、`dafeiyu --version`、`update.ps1`、口令可见性、备份说明（spec §13）。
- **§14 接话判断能力**：`Message.reply_to_user_id` 落库、precision 标注与验收（spec §14）。
