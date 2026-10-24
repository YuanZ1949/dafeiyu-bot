# Spike S1：免费档模型会不会自己调 `webfetch`

- 日期：2026-10-24
- opencode：v2.0.18（`opencode --version`）
- 模型：`opencode/deepseek-v4.1-flash-free`（agent 文件里钉住）
- 环境：探针项目 `%TEMP%\opencode\probe-s1`，独立实例 `opencode serve --port 24399`（不碰 24310 上的 bot 实例）
- 探针 agent：`.opencode/agents/probe.md`（`mode: primary`、`steps: 4`、通配 deny + `webfetch`/`read`/`edit` 例外）

## 结论：**PASS（可以继续，不需要换模型）**

模型在 `steps: 4` 下**真的调用了 `webfetch`**，拿到网页内容后又发起第二步生成了最终文本，整轮 16.1 秒。

## 证据（一次真实调用）

prompt：

> 用 webfetch 抓取 https://example.com ，把页面里的 H1 标题原样告诉我。最后只输出一行 JSON：{"ok": true, "title": "<标题>"}

同一次会话里的 4 条 message（**服务端默认倒序返回**）：

```
idle       created=1792855187613  parts=[]
assistant  created=1792855184029  completed=1792855187608  parts=[reasoning, text]
assistant  created=1792855180151  completed=1792855183835  parts=[tool]
user       created=1792855180104
```

tool part（第二条 assistant）：

```json
{
  "type": "tool",
  "id": "call_00_tmzc0vsp6pjyqsf2yvf394cy",
  "name": "webfetch",
  "executed": false,
  "state": {
    "status": "completed",
    "input": { "url": "https://example.com", "format": "html" },
    "content": [{ "type": "text", "text": "<!doctype html>…<title>Example Domain</title>…" }],
    "metadata": { "contentType": "text/html; charset=utf-8", "truncated": false }
  }
}
```

最终文本（第三条 assistant 的 text part）：

```
{"ok": true, "title": null}
```

> `title` 为 null 是模型对 "H1" 的理解问题（example.com 只有 `<title>` 没有 `<h1>`），**不是工具失败**：`state.content` 里拿到了完整页面 HTML。S1 判据是「出现 webfetch tool part 且完成」，已满足。

## 对实现的影响（写进计划的事实）

1. **工具调用在免费档可用**，`steps: 4` 允许「工具 → 再生成」两步；不需要换模型，也不需要双 agent 路由。
2. tool part 的工具名在 **`name`** 字段（不是 `tool`），状态在 **`state.status`**（`pending` / `running` / `completed` / `error`），抓取入参在 `state.input`。`on_tool` 提示按 `name == "webfetch"` 判断。
3. 一次 wetch 后模型会**新开一条 assistant 消息**继续生成；判定「本轮结束」不能只看最后一条 assistant 的 `time.completed`（工具那条也是 completed）。
4. message 里还有 **`idle`** 类型（无 content），审计「外来消息」时必须把它和 `user`/`assistant` 一起白名单掉，否则每轮都误判。
5. `time.created` 是**毫秒**（`1792855180151`），`time.time()` 是秒 —— 所有窗口比较必须同单位，见 `2026-10-24-spike-api.md`。
6. token 记在 assistant 消息的 `tokens`：`{input, output, reasoning, cache:{read,write}}`；工具那步 `input=2433, output=58`，文本那步 `input=231, output=11, cache.read=2432`。**每一步单独计数，需要累加**。
