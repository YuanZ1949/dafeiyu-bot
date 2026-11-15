# 类型债清单（basedpyright）

本仓库没有基于 basedpyright CLI 的检查入口（只能走编辑器的 LSP），所以这类问题一直
"看得见、没人算"。本页把已知存量记下来，便于按文件、按成因分批判清理——**不是**缺陷
清单，里面的代码都在跑，只是类型层面说不清。

## 度量方式

- 工具：LSP（basedpyright），逐文件 `diagnostics(severity=error)`；
- 计数：按「行:列 + 规则名」去重（同一位置的同一规则只算一条）；
- 日期：2026-11-15，基线 `41f7e7f` 之后的代码状态。

## 存量

| 文件 | 条数 | 主要规则 | 典型成因 |
|---|---|---|---|
| `src/dafeiyu/control.py` | 54 | `reportArgumentType` 39、`reportOptionalMemberAccess` 11、`reportOperatorIssue` 5、`reportOptionalSubscript` 4、`reportGeneralTypeIssues` 2、`reportMissingTypeArgument` 1 | HTTP 处理器里 `await request.json()` 得到 `dict \| None` 直接下标；`store` 返回 `dict[str, object]` 后直接 `int(...)` / `float(...)` |
| `src/dafeiyu/service.py` | 16 | `reportArgumentType` 7、`reportAttributeAccessIssue` 4、`reportGeneralTypeIssues` 4、`reportMissingTypeArgument` 2、`reportCallIssue` 1 | `create_task()` 收到非协程（`object` 不可 await）；`asyncio.Task` 缺泛型参数 |
| `src/dafeiyu/memory/store.py` | 5 | `reportArgumentType` 9（位置去重后 5） | 聚合查询的 `dict[str, object]` 返回类型：调用方要 `int` / `float` |
| `src/dafeiyu/supervisor/dashboard.py` | 4 | `reportOptionalMemberAccess`、`reportOptionalCall`、`reportArgumentType`、`reportMissingTypeArgument` | `app["x"]` 可能为 `None`；`create_task(None)` |
| `src/dafeiyu/config.py`、`keepalive.py` | 0 | — | — |

合计 79 条（`dashboard.py` 已从 13 条降到 4 条，见下）。

## 已清掉的部分

- **备份上传路径（`dashboard.py` `restore_backup`）— 8 条 → 0**，提交 `28cd504`：
  aiohttp 的 multipart 迭代类型是 `MultipartReader | BodyPartReader | None`，改成
  `isinstance(part, BodyPartReader)` 收窄再用；新增
  `tests/test_supervisor_ops_api.py::test_restore_streams_multi_chunk_upload`
  用 256 KiB 备份体压住多次 `read_chunk()` 的路径。

## 处置建议（按性价比排序）

1. **先改返回注解，再改调用点**：`control.py` 的 39 条 `reportArgumentType` 大半来自
   `store` 的 `dict[str, object]`。把这类返回类型定成 `dict[str, Any]`（本轮
   `thread_states()` 就是这么消掉 2 条的），一处注解能带走十几条。
2. **`request.json()` 一律先判空**：`dict | None` 后直接下标是 `reportOptionalSubscript`
   的主要来源；判空返回 400 顺带把「请求体不是对象」的运行时分支补上。
3. **`create_task` 前确认拿到协程**：`service.py` / `dashboard.py` 的
   `reportGeneralTypeIssues`（`object` 不可 await）都属于这一类，改法是把
   「返回 `Awaitable` 的工厂」注解成 `Awaitable[...]` 而不是 `object`。
4. **泛型参数**：`asyncio.Task` / `dict` 缺参数，纯注解，零运行时风险，可以随手修。

## 清理一轮的验收口径

- LSP 对目标文件 `severity=error` 条数**只减不增**；
- `git blame` 逐行归因，确认减少的是基线行、新增行 error 为 0（本轮 F2 的做法）；
- 每改一个模块跑该模块相关测试；涉及公共返回类型（`store` / `control`）时跑全量；
- 每处改动做反向验证：改回旧注解 → 目标 error 复现 → 还原。
