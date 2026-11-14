# 面板改版（2026-11-15）

控制台仍然是 **`src/dafeiyu/supervisor/dashboard/index.html` 单文件**：没有前端框架、没有构建步骤、
没有新增第三方浏览器依赖。本轮改的是信息架构、主题令牌、消息气泡的宽度算法，以及攒着 / 话题状态 /
保活三块只读观测。

## 1. 六个分区

页面按 6 个分区顺序排在同一个 `main` 里，顶栏下面是 `position: sticky` 的分区导航
（`#pane-nav`），点按钮是 `scrollIntoView` 定位，不是前端路由，也没有隐藏/销毁卡片。
宽屏每区两列（`grid-template-columns: repeat(2, minmax(0, 1fr))`），窄屏单列；
网格轨道显式写 `minmax(0, 1fr)`，长摘要不会再把整页撑出横向滚动。

| 分区 | 卡片 | 说明 |
| --- | --- | --- |
| 总览 `#pane-overview` | 进程、登录二维码、健康总览、告警事件、**保活** | 进程卡三合一；保活是本轮新增的只读卡 |
| 记忆 `#pane-memory` | 记忆（按日摘要 + 群印象）、用量趋势、黄金集趋势、记忆搜索 | 编辑 / 合并 / 删除用户消息的入口仍在记忆卡底部 |
| 会话 `#pane-chats` | 会话管理、**正在攒着**、**话题状态**、微信私聊、群白名单、群互通（镜像） | 后三张是可写配置卡，攒着 / 话题状态只读 |
| 投递 `#pane-delivery` | 决策流、触发规则调试器、批次预览、聊天行为、心智 | 决策流新增「攒着」筛选页签 |
| 配置 `#pane-config` | 大脑模式、运行配置、微信账号、安全、备份与恢复 | 大脑模式是三档单选，切档写 `data/runtime.json` |
| 日志 `#pane-logs` | 日志 | 按 supervisor / opencode / bot / napcat 分来源，支持级别与关键字筛选 |

顶栏（`header`，也是 sticky）右侧还挂着：全局搜索 `#global-search`、全部启动 / 重启 / 停止、
主题切换 `#btn-theme`、退出登录、退出控制台。

### 全局搜索

输入即搜（防抖 200 ms），结果落在 `#search-panel`，分三组：

- **会话**：取 `/api/sessions?limit=200` 后在前端过滤（接口没有关键字参数）；
- **消息**：走 `/api/memory/search?q=`（3 字以上中文 trigram FTS，1–2 字回落 LIKE）；
- **决策**：取 `/api/decisions?limit=200` 后在前端过滤 `reason` / `reply_preview` / `chat_id`。

会话与决策都只覆盖最近 200 条，每组最多显示 20 条，面板上会写明「会话与决策只检索最近 200 条」。
Esc 或点面板外部关闭。

## 2. 主题令牌

- 主题挂在 `<html data-theme="dark|light">`，默认 `dark`；
- 用户选择只存在浏览器 `localStorage` 的 **`dafeiyu.theme`** 一个键里，不写服务端、不进备份；
- **色值只允许出现在 `:root` 与 `[data-theme="light"]` 两个令牌块内**，其它规则一律 `var(--token)`；
- 浅色块与深色块**同名一一对应**；尺寸令牌（字号、列表高度、固定宽度、卡片间距）两边共用，不重复定义。

令牌分四组：

| 组 | 令牌 | 用途 |
| --- | --- | --- |
| 基础色 | `--bg` `--panel` `--panel-2` `--line` `--text` `--muted` | 背景、卡片、分隔线、正文、次要文字 |
| 语义色 | `--ok` `--warn` `--bad` `--accent` `--hot` | 状态点、告警、强调、热聊标记 |
| 派生色 | `--accent-hot` `--danger-deep` `--stopped-deep` `--surface-deep` `--toast-deep` `--mode-active` `--mode-glow` `--bubble-ok` `--bubble-warn` `--tag-*-bg` `--overlay` `--drawer-shadow` | 按钮悬停、危险态、抽屉遮罩、气泡底色 |
| 尺寸 | `--fs-lg/md/sm/xs` `--list-h-*` `--w-*` `--card-pad` `--card-radius` `--card-gap` | 字号阶梯 16/14/12/11、列表高度、固定列宽、卡片内边距与间距 |

浅色主题的正文对白底对比度 ≥ 4.5:1。切主题只改 `data-theme`，不重载页面。

## 3. 消息气泡的宽度与对齐

会话时间线抽屉（`#timeline-drawer`）里的气泡改成排版算法，不再用固定宽度：

| 规则 | 位置 | 作用 |
| --- | --- | --- |
| `display: grid; justify-items: start` | `.timeline-body` | 气泡是 grid item，默认靠左且**不被拉伸** |
| `width: fit-content` | `.timeline-msg` | 宽度由内容决定：短句窄、长句宽，同一行不同气泡宽度可以不同 |
| `max-width: min(72ch, 88%)` | `.timeline-msg` | 上限取「72 个字符宽」与「容器 88%」中更小的那个，手机与桌面同一套规则 |
| `justify-self: end` | `.timeline-msg.bot` | 机器人发言靠右，右边缘对齐容器内容右边缘 |
| `grid-column: 1 / -1; justify-self: stretch` | `.timeline-date` | 日期分隔条单独占满一行，不参与气泡左右对齐 |

通道配色只区分「哪条通道」，不区分左右：人在左、bot 在右的格局不变；`data-channel="qq"` 用
`--accent`（蓝），`data-channel="wechat"` 用 `--ok`（绿），同时体现在左侧边条与顶部边条上。
气泡底色只有两种语义：bot 用 `--bubble-ok`，搜索命中的消息用 `--bubble-warn`。

这些不变量由 `tests/test_dashboard_visual.py` 在真实浏览器里断言（气泡左右边缘与
`.timeline-body` 内容边缘的偏差、日期条跨列、窄屏无横向溢出），改样式时必须一起跑。

## 4. 攒着视图与两个动作

卡片「正在攒着」`#holds-card` 读 `GET /api/holds`（supervisor 代理到 bot 的同名接口），
数据来自 `Store.pending_hold_info()` 的**实时聚合**，不是决策流：

- 决策流对同类 `held` 有 60 秒去重，按行数统计「攒了多久」必然偏小，所以不能复用；
- 每项字段：`chat_type`、`chat_id`、`count`、`oldest_ts`、`age_seconds`、`state`、`checkpoint`、`held_since`；
- `state` 只按该会话**最新一条**决策判定：`held` = 真在攒着，`pending` = 只是排队待投、还没进入攒着判定。

卡片默认只列 `state = held`，「显示全部待投」把 `pending` 也列出来。每行带 `data-hold-seconds` /
`data-hold-count` / `data-hold-state` 等属性，便于机器断言「攒了多久」。

两个动作（都写决策流水，都会立刻回读决策流）：

| 动作 | 接口 | 语义 |
| --- | --- | --- |
| 放行 | `POST /api/sessions/<chat_type>/<chat_id>/release` | 先记一条 `manual_release` 决策，再走正常批次处理（`force_release=True`）。**只跳过攒着判定**，预算 / 静默 / 收尾窗口照旧拦截；会真调一次模型，所以 supervisor 给这个动作单独放宽超时 |
| 丢弃 | `POST /api/sessions/<chat_type>/<chat_id>/discard` | 取该会话**全量**未投递消息（不受 20 条窗口限制），`mark_skipped(..., "hold_discard")` 标记跳过，消息行本身不删，记一条 `manual_discard` 决策。不可撤销，前端二次确认 |

攒着寿命与出口的完整语义见 README「2.5 攒着（纯热聊）的生命周期」。

## 5. 话题状态与保活的数据来源

**话题状态** `#thread-states-card` 读 `GET /api/thread-states`：只读展示 `chat_state` 里
**已经落库**的收尾信号，不是 router 的实时判定，面板不介入话题判定。字段：

- `chat_type` / `chat_id`；
- `closed_left_seconds`：收尾窗口剩余秒数，`> 0` 显示「已收尾 · 剩余 N 秒」，否则「进行中」；
- `last_closed_at`：最近一次收尾时间（秒级时间戳），无记录时显示「还没有收尾记录」。

卡片标题旁的提示会汇总「N 个已收尾 · M 个进行中」。

**保活** `#keepalive-card` 不额外发请求，复用既有的 `/api/health/detail` 轮询里
`bot.keepalive` 一帧：`enabled`、`ok`、`interval_seconds`、`last_at`、`last_error`、`text`。
`enabled === false` 显示「保活未启用」；`ok` 为 `false` / `true` / 未知分别对应「最近一次保活失败」/
「保活正常」/「还没发过保活」。没有开关，配置在 bot 侧。

## 6. 视觉验收脚本

`scripts/qa_dashboard_shots.py` 是只读 QA 脚本（Edge + CDP），不写库、不改配置：

```powershell
.venv\Scripts\python scripts/qa_dashboard_shots.py --out-dir .omo/evidence/visual/run1
```

参数：`--base-url`（默认 `http://127.0.0.1:8787`）、`--out-dir`（默认
`.omo/evidence/visual/<时间戳>`）、`--user`、`--password`、`--timeout`（默认 60 秒）、
`--browser`、`--viewports both|desktop|mobile`、`--debug`。

口令来源优先级：`--password` → 环境变量 `DAFEIYU_DASHBOARD_PASSWORD` → `data/dashboard.password`
（只读）。脚本不落盘口令。

流程与产物：

1. 先 `GET /api/auth/session` 探活，面板不可达直接退出，不启动浏览器；
2. 401 时用 `POST /api/auth/login` 换会话 Cookie，再写进 headless Edge；
3. 桌面 1280×900、手机 390×844 两档，每档两张 PNG：整页截图 + 打开会话时间线抽屉后的视口截图；
4. `metrics.json`：每个 `.timeline-msg` 的 `getBoundingClientRect()`、`.timeline-body` 的内容左右
   边缘、`data-channel`、日期条宽度，以及 `page`（`innerWidth` / `overflowX`）与 1 字 / 10 字探针；
   本机没有会话时写 `"bubbles": []` 与 `"reason": "no-session"`，不报错；
5. `interactions.json`：主题切换与 `localStorage` 持久化、6 个分区导航、三个接口的状态码
   （`/api/holds`、`/api/thread-states`、`/api/sessions`）。

退出码：`0` 成功、`1` 参数错误、`2` 面板不可达、`3` 需要登录但没口令、`4` 登录失败、
`5` 找不到 Edge/Chrome、`6` 浏览器或 CDP 起不来、`7` 截图 / 度量失败。

## 7. 已知限制

- 攒着 / 话题状态两个接口是**本轮新增**，跑在本机 8787 上的旧 supervisor 进程没有这两个代理，
  会返回 404；重启 supervisor 后才有。面板本身不依赖它们启动，卡片会显示「读取失败」。
- 通道配色按「至少一个通道被标记」验收：本机只有微信通道，QQ 蓝 / 微信绿两套同时出现的场景
  需要在有 QQ 会话的环境里人工看一眼。
- 主题只做深 / 浅两套，没有跟随系统 `prefers-color-scheme`；默认深色是刻意选择，避免旧截图失效。
