# 类型债清单（basedpyright）

本仓库历史上没有基于 basedpyright CLI 的检查入口（只能走编辑器的 LSP），所以这类问题
一直"看得见、没人算"。本页记录当前状态与**如何复测**；现在已补上 `scripts/typecheck.ps1`
作为可重复执行的入口。

## 度量方式

- 工具：LSP（basedpyright），逐文件 `diagnostics(severity=error)`；或安装 dev 依赖后
  运行 `scripts/typecheck.ps1`（内部调用 `python -m basedpyright src`）；
- 计数：按「行:列 + 规则名」去重（同一位置的同一规则只算一条）；
- 最近一次复测：**2026-11-24**，对下列文件 `severity=error` 均为 **0**。

## 存量（2026-11-24 复测）

| 文件 | 条数 | 备注 |
|---|---|---|
| `src/dafeiyu/control.py` | 0 | 历史 54 条已清（`request.json()` 一律先判空再收窄） |
| `src/dafeiyu/service.py` | 0 | 历史 16 条已清 |
| `src/dafeiyu/memory/store.py` | 0 | 历史 5 条已清 |
| `src/dafeiyu/supervisor/dashboard.py` | 0 | 历史 4 条已清 |
| `src/dafeiyu/config.py`、`keepalive.py` | 0 | — |

合计 **0** 条 error（历史基线 79 条，见下方「已清掉的部分」）。

复测范围不止这 4 个文件：另外抽查了 `__main__.py`、`gateway/wechat.py`、`gateway/qq.py`、
`media.py`、`reply_media.py`、`mirror.py`、`sender.py`、`links.py`、`hotconfig.py`、
`bootstrap.py`、`supervisor/manager.py`、`supervisor/main.py`、`workers/*.py`、
`brain/client.py`、`router.py` 共 20 个文件，`severity=error` 仅 1 条（见下「已知误报」）。

## 已知误报（不是缺陷，不要"修"）

- `src/dafeiyu/__main__.py:477` `reportArgumentType`：
  `MirrorRouter | None` 无法赋给参数 `MirrorRouter | None`，并递归输出
  `"MirrorRouter" is not assignable to "MirrorRouter"`。这是 basedpyright 在
  「先声明 `X | None = None`、再在 `if` 里赋值」时的已知自比较误报，两侧是同一个类
  （`from .mirror import MirrorRouter`）。运行时无影响，遇到升级 basedpyright 后可复测。

## 与「严格模式警告」区分

编辑器当前启用的是较严格的规则集，`severity=warning` 下还会看到大量
`reportAny` / `reportExplicitAny` / `reportUnknown*`（每文件数百条）。这些是
**strict 类型化**的渐进项（大量 `Any` 入参、`dict[str, object]` 未收窄），不是本页说的
79 条「真错误」，也不影响运行。要推进属于独立的大工程，需先定 `[tool.basedpyright]`
的 `typeCheckingMode` 口径，再分模块清理。

## 已清掉的部分

- **`control.py` / `service.py` / `store.py` / `dashboard.py` 的 79 条 error → 0**：
  历史主要成因是 `await request.json()` 得到 `dict | None` 直接下标、`store` 返回
  `dict[str, object]` 后直接 `int(...)`/`float(...)`、`create_task()` 收到非协程。
  代码已在后续迭代中逐步收窄（如 `request.json()` 后先 `isinstance(payload, dict)`、
  返回注解改 `dict[str, Any]`），2026-11-24 复测归零。
- **备份上传路径（`dashboard.py` `restore_backup`）— 8 条 → 0**，提交 `28cd504`：
  aiohttp 的 multipart 迭代类型是 `MultipartReader | BodyPartReader | None`，改成
  `isinstance(part, BodyPartReader)` 收窄再用；新增
  `tests/test_supervisor_ops_api.py::test_restore_streams_multi_chunk_upload`
  用 256 KiB 备份体压住多次 `read_chunk()` 的路径。

## 如何复测（防回退）

1. 编辑器：对目标文件调用 LSP `diagnostics(severity=error)`，条数应为 0（除上方已知误报）；
2. 命令行：`.venv\Scripts\python -m pip install -e ".[dev]"` 后运行
   `powershell -ExecutionPolicy Bypass -File scripts/typecheck.ps1`，退出码 0 表示干净；
3. 改动公共返回类型（`store` / `control`）时跑全量测试。

## 验收口径

- 目标文件 `severity=error` 条数**只减不增**；
- 新增行 error 必须为 0；已知误报不计入；
- 每改一个模块跑该模块相关测试；涉及公共返回类型时跑全量。
