# AGENTS.md

本文件供 AI 编码代理使用；面向人类的说明见 `README.md`。

## 代码探查：先用 CodeGraph

本仓库已建立 CodeGraph 索引（`.codegraph/`，已在 `.gitignore` 中，不要提交）。

遇到下列问题时，**先调用 `codegraph_explore`，不要直接 grep + 逐个 Read**：

- "X 在哪里实现"
- "谁调用了 X / 改 X 会影响哪些地方"
- 跨文件的调用链、影响面、相关测试

它一次返回：相关符号的**逐行原文源码** + 调用路径 + 影响面（调用方与测试文件）。
拿到结果后，不要对同一片代码再重复 grep / Read。

代码发生结构性改动（新增、删除、重命名函数或模块）后，更新索引：

```powershell
codegraph sync
```

## 改完必须验证

按顺序执行，前一步不通过不要进入下一步：

1. **LSP 诊断**：对改动的 `.py` 文件调用 `lsp_diagnostics`（basedpyright），确认没有新增 `error`；
2. **相关测试**：`.venv\Scripts\python -m pytest -q tests/test_<相关文件>.py`；
3. **全量测试**（涉及公共接口、类型或配置结构变更时）：`.venv\Scripts\python -m pytest -q`。

不允许"测试通过但类型错误一堆"就当作完成。

## 环境约定

- Python >= 3.11；一律使用仓库内的 `.venv`，不要往系统 Python 装依赖。
- 若 `.venv` 尚未创建：

  ```powershell
  python -m venv .venv
  .venv\Scripts\python -m pip install -e ".[dev]"
  ```

  需要微信适配器的可选依赖时：

  ```powershell
  .venv\Scripts\python -m pip install -e ".[dev,wechat]"
  ```

- 运行程序 / 测试统一用 `.venv\Scripts\python -m ...`，避免 PATH 里的其它 Python 干扰。
