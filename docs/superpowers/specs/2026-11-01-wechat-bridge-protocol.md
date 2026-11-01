# 微信桥 v1 线协议规范（冻结）

- 日期：2026-11-01
- 状态：已冻结（v1），作为 todo 10 / 11 / 13 的实现依据
- 范围：桌面微信桥的**信封定义**——事件（event）、指令（command）、回执（receipt）、健康（health）四类消息的字段、必填性、缺字段的下游后果，以及 `v` 版本与向后兼容规则
- 相关文件：`docs/superpowers/specs/2026-11-01-wechat-bridge-protocol.md`（本文件）；下游 `src/dafeiyu/wxg/`（桥接服务器，todo 11）、`src/dafeiyu/gateway/wechat.py`（adapter 侧客户端，todo 10，命名待实现时定）、驱动门面（todo 13）；参照 `src/dafeiyu/gateway/qq.py`（OneBot v11 echo 关联先例，`qq.py:426`、`qq.py:507-510`、`qq.py:267-334`）

## 1. 背景与目标

微信没有官方机器人 API。接入方式是本机桌面自动化：一个常驻**桥服务器**（bridge）读桌面微信的事件（收到的消息、撤回、登录状态），并把上层的发送指令打回到微信 UI 上。桥与主进程（dafeiyu）之间走**本地**连接，唯一约定的序列化格式是**换行分隔的 UTF-8 JSON 文本帧**（每行一个信封）。本文不引入任何二进制帧、protobuf 或 msgpack。

本规范冻结的是**信封**（envelope）：字段名、类型、必填性。传输如何建连、断线重连如何退化，是 todo 11 的实现细节，不属于本协议。

设计原则只有一条：**桥是可替换的**。它只做「读屏 → 事件」「指令 → 打字」两件事，所有业务语义（会话、记忆、回复决策）都在主进程侧。因此信封里不带任何业务字段，但**每一个字段的缺失后果必须写死**——桥的数据是从屏幕刮出来再拼起来的，缺一个字段就是一条静默丢数据的路径。

## 2. 事件信封 event（桥 → 主进程）

`type` 四选一：`"message"`（普通消息）、`"recall"`（撤回）、`"health"`（健康快照，字段见 §5，另行成节）、`"resync"`（桥重启/失联恢复后要求主进程重放会话）。

### 2.1 字段表

| 字段 | 类型 | 必填 | 含义 | 缺失时下游会坏在哪 |
| --- | --- | --- | --- | --- |
| `v` | int | 是 | 协议版本，v1 恒为 `1` | 主进程无法判定兼容性；未知大版本必须拒绝连接并告警 |
| `type` | string 枚举 `"message"\|"recall"\|"health"\|"resync"` | 是 | 信封种类 | 主进程按 `type` 分发解析路径，缺了整条消息只能丢弃并告警 |
| `channel` | string 恒 `"wechat"` | 是 | 通道标识；主进程按它选 adapter | 主进程只能按连接猜通道；多桥并存时路由错串 |
| `chat_type` | string 枚举 `"group"\|"private"` | 是 | 群聊或私聊；与 `models.ChatType` 对齐 | 会话落库时 `ChatType` 无法取值；群/私聊差异逻辑（@判定、静默策略）全废 |
| `chat_id` | string `"wxg:<alias>"` | 是 | 会话稳定内部键（别名，见 §2.2） | `models.chat_id` 前缀派生 `channel` 直接抛 `ValueError`；消息无法入库，记忆/摘要都指向不存在会话 |
| `chat_name` | string \| null | 是（bot 自己的消息可为 `null`，见 §2.2） | 微信里显示的群名 / 好友备注 | **发送直接瘫痪**：pyweixin 按 `send_messages_to_friend(friend=好友或群聊备注, ...)` 以显示名寻址，`chat_id` 别名对微信 UI 无意义，缺显示名则指令无处寻址、只能回 `delivered:"failed"`（见 §2.2 专节） |
| `user_id` | string `"wxg:<alias>"` | 是 | 发言者稳定内部键 | `Message.user_id`、印象卡、@某人 的稳定锚点全丢；同一人换名即成两个「人」 |
| `nickname` | string | 是 | 发言者当前显示昵称 | 模型 prompt 里只剩一个 opaque 别名，无法渲染「谁在说话」；重名/改名时人设接地全糊 |
| `text` | string | 是 | 消息正文（@ 段以原文保留，如 `"@小助手 你好"`） | 无正文的「消息」没有意义；时间线、trigram 召回、回复决策全部空转 |
| `at_bot` | bool | 是 | 本条消息是否 @ 了机器人（含 `@全体成员`） | 群聊 @ 触发优先级、官方直答路径失效；漏 `false` 会被当 @ 处理造成刷屏 |
| `reply_to_bot` | bool | 是 | 本条是否直接回复机器人上一条（微信「回复」可见在正文里，由桥从引用块判出） | 「接话」连续性断裂：bot 回复后会以为没人接话，静默策略错判 |
| `reply_to_text` | string \| null | 否 | 被引用的上文文本摘要（微信引用格式桥抓不到完整原文时给截断片段） | null 时模型少一层上文，可接受；但缺失不报错也不得当成空串扰乱对账 |
| `ts` | int | 是 | 事件发生的 unix 秒 | 时间线按 `ts` 排序；缺失会让顺序错乱、冷却窗口与批处理窗口全盘失准 |
| `message_id` | string `"<epoch>:<seq>"` | 是 | 事件在其桥会话内的单调序号（语义见 §7.1） | 去重失效：断线重放会重复入库；`recall` 也无法按 id 关联回原消息 |
| `is_bot` | bool | 是 | 本条是否是机器人自己发出的消息 | **自回复死循环**：bot 看不见自己的消息就会一直被自己触发，见 §7.2 |
| `session_epoch` | int | 是 | 桥进程的启动序号（语义见 §7.3） | 重启后无法识别「换了进程」，旧的去重表与新事件流混在一起，dedup 语义必乱 |

`recall` 事件只承载 `v, type, channel, chat_id, chat_name?: string|null, user_id, nickname: string|null, ts, message_id`：这里的 `message_id` 是**被撤回那条**消息的 id，必填，否则撤回通知对不上原消息、等于白来；`chat_name` 遵循 §2.2，`nickname`、`text` 等读不到时给 `null`。

### 2.2（专节）`chat_name`：按显示名寻址，缺了就发不出

事件里的 `chat_name` 和指令侧的 `at` 名单（见 §3）不只是装饰——**pyweixin 的发送按微信 UI 里的显示名匹配**，其签名是 `send_messages_to_friend(friend=好友或群聊备注, ...)`。`friend` 参数就是「好友或群聊备注」，即：搜索框里能命中的那个显示字符串。桥收到指令后，是把 `chat_name` 当作 `friend` 塞给搜索，让微信自己跳到那个会话再打字。

后果（实现者必须逐条领会）：

1. **`chat_name` 缺失 = 无法发送**。`chat_id`（`"wxg:<alias>"`）只是主进程内部的稳定键，桥拿它没有任何用——微信 UI 不认别名，只认显示名。没有 `chat_name`，没有 fallback，发送指令必然失败，回执必须带 `delivered:"failed"` 并说明原因。
2. **显示名不稳定 = 发送时灵时不灵**。用户在微信里改备注、群主改群名、好友改昵称，都会让 `friend` 参数匹配失败。桥的行为：发送前在会话列表里做一次模糊匹配，命中即发；未命中则回执 `ok:false`、`error_code:"chat_name_unresolved"`，同时立刻主动发一个 `resync` 事件，让主进程刷新别名映射。
3. **改名恢复流程**：主进程收到 `resync` 后丢弃旧 `chat_name` 缓存，桥重扫会话列表、按 `chat_id` 别名重新绑定新的显示名并补发 `health`。也就是说：**改名不会丢会话**（`chat_id` 不变），只会触发一次 resync 与指令级失败重试。若群聊**被解散或机器人被移出**，则 `resync` 后该 `chat_id` 永久失联，主进程侧标记会话离线，等用户重新拉群并重新 resync。
4. 因此：桥的事件里 `chat_name` 必填；bot 自己发的消息（`is_bot:true`）因走的不是「读屏」路径，`chat_name` 可为 `null`，但 `chat_id` 不可缺。

## 3. 指令信封 command（主进程 → 桥）

单条请求，桥处理后回一条同 `id` 的 receipt（§4）。参考先例：OneBot v11 的 `{"action","params","echo"}`（`gateway/qq.py:426` 发送、`qq.py:507-510` echo 关联、`qq.py:267-334` 事件解析）。

### 3.1 字段表

| 字段 | 类型 | 必填 | 含义 | 缺失时下游会坏在哪 |
| --- | --- | --- | --- | --- |
| `v` | int | 是 | 协议版本，恒 `1` | 桥无法判定兼容性，未知大版本必须拒收并回 `error_code:"version_mismatch"` |
| `id` | string（uuid） | 是 | 指令唯一 id，receipt 原样回填（语义见 §7.6） | 主进程无法把回执对回指令，超时重发也无法去重——一次发送会打出两条 |
| `action` | string 枚举 `"send_text"\|"send_at"\|"probe"\|"stop"` | 是 | 指令动作 | 桥必须回 `ok:false` 的 receipt（§6 规则 5），**绝不能崩掉主循环** |
| `params` | object | 是 | 动作参数；`probe`/`stop` 可为 `{}` | 缺 `params` 视为参数无效，回 `ok:false, error_code:"bad_params"` |
| `params.chat_id` | string `"wxg:<alias>"` | 是（`probe`/`stop` 除外） | 目标会话键；桥需经别名映射找到显示名才能发送 | 无有效目标的指令无处可去，必失败（`chat_name_unresolved`，见 §2.2），绝不静默丢弃 |
| `params.text` | string | `send_text`/`send_at` 必填 | 要发送的正文 | 没正文的发送等于发空消息——要么拒绝（回 `bad_params`）要么发出可见垃圾，实现取拒绝 |
| `params.at` | string[] | 仅 `send_at` 必填 | 要 @ 的**显示名列表**（语义见 §7.5） | 缺了 @ 目标全部丢失，调用方意图被静默篡改；`probe`/`stop` 带它按未知字段忽略（§6 规则 2） |

`send_text`：把 `params.text` 发到 `chat_name(params.chat_id)` 对应会话。`send_at`：同 `send_text`，但在正文前把 `params.at` 中每个显示名按微信「@」控件的语义打出来（桥负责逐个填充）。`probe`：无副作用自检，桥立即回一条 receipt（`ok:true` 即链路通）。`stop`：让桥退出本次发送操作/释放 UI 焦点，回 `ok:true`。

## 4. 回执信封 receipt（桥 → 主进程）

每条 command 必定产生**恰好一条** receipt，`id` 原样回填。模型同 OneBot echo：`qq.py:507-510`——主进程用 pending 表 `id → future` 对回。

| 字段 | 类型 | 必填 | 含义 | 缺失时下游会坏在哪 |
| --- | --- | --- | --- | --- |
| `v` | int | 是 | 协议版本，恒 `1` | 同前；版本不对的回执必须丢弃并告警 |
| `id` | string（uuid） | 是 | 对应 command 的 `id`，原样回填 | pending 表查不到条目，回执变孤儿；发送流程永远等在超时上 |
| `ok` | bool | 是 | 整体成败 | 主进程不知该重试还是收工；`ok` 缺失按 `false` 处理并告警 |
| `error_code` | string \| null | 是（`ok:false` 时必非空） | 失败原因码，如 `"chat_name_unresolved"`、`"bad_params"`、`"version_mismatch"`、`"not_logged_in"`、`"timeout"` | 无原因码的失败无法区分「改名了」与「没登录」，主进程会瞎重试 |
| `delivered` | string 枚举 `"typed"\|"observed"\|"failed"` | 是 | 送达语义，见 §7.4 | 主进程无法区分「打字成功但还没看到气泡」和「根本没发出去」，重发策略必错 |
| `message_id` | string \| null | 否 | 本次发送对应的桥内本条 `message_id`（若后续在事件流里看见自己的气泡，可用它对账） | 与事件流对账断链；`is_bot` 的自见过滤失去凭据 |

`ok:false` 时 `delivered` 恒为 `"failed"`；`error_code` 可在 `ok:true` 时为 `null`。

## 5. 健康信封 health（桥 → 主进程）

两类来源：(a) 桥周期性上报；(b) `event` 的 `type:"health"` 一并携带，字段同表。

| 字段 | 类型 | 必填 | 含义 | 缺失时下游会坏在哪 |
| --- | --- | --- | --- | --- |
| `v` | int | 是 | 协议版本，恒 `1` | 同前 |
| `type` | string 恒 `"health"` | 是 | 信封种类 | 主进程只按 `type` 分发；错认会当成对账回执污染 pending 表 |
| `connected` | bool | 是 | 传输层是否连通（桥自身视角） | 状态页/自动重连的判断入口；缺失只能靠心跳兜底，人工排障变盲 |
| `logged_in` | bool | 是 | 桌面微信是否已登录 | 未登录时主进程应静默积压发送；不知道这一点会反复打字失败刷告警 |
| `desktop` | string 枚举 `"interactive"\|"locked"\|"no_wechat"` | 是 | 桌面状态：可交互 / 已锁屏 / 微信未启动 | **打字自动化在锁屏下行为不同甚至必败**；不知道这条状态会一直重试 |
| `wechat_version` | string \| null | 否 | 微信版本号 | UI 自动化强烈依赖微信版本；缺失时已知缺陷无法按版本短路 |
| `language` | string \| null | 否 | 微信界面语言（如 `"zh_CN"`） | 桥若按界面控件文字定位，语言变了还不能立刻自报，排障无从下手 |
| `queue_depth` | int | 是 | 桥内待处理指令数 | 积压不被看见，主进程会继续加塞，最后整个发送链路僵死 |
| `last_event_at` | int \| null | 是（未收到过事件时为 `null`） | 最近一次事件时间戳（unix 秒） | 主进程无法判「桥是不是死了」，电话拨号式重连不会触发 |
| `last_send_at` | int \| null | 是（未发送过时为 `null`） | 最近一次成功发送时间戳（unix 秒） | 「别名失效」与「根本连不上」无法区分，resync 不会适时触发 |

## 6. 版本与向后兼容规则

1. `v` 字段必填，v1 恒为字面量 `1`。所有 JSON 示例里 `"v": 1` 必须出现且为 int。
2. **未知字段必须忽略**：桥和主进程遇到信封里不认识的字段，不得报错、不得拒绝整条信封，按没有该字段处理并继续（可打一条 debug 日志）。
3. **可选字段的新增不升 `v`**。任何一方在 v1 信封上增加 optional 字段（如未来加 `reply_to_message_id`），旧对端按规则 2 忽略即可，协议仍是 v1。
4. **以下任何一条都必须升 `v`（做成 v2）并走显式协商**：
   - 删除字段或给字段改名；
   - 改变既有字段的类型或取值枚举（含给 enum 加值——已有对端未知值会被拒收）；
   - 把 optional 字段改成必填；
   - 改变任何字段的语义（字段名不变但含义变了）。
5. **未知枚举值的容忍**（与规则 2 配套但不相同）：未知 `type` 的 event 必须忽略且落日志；未知 `action` 的 command 必须回 `ok:false, error_code:"bad_action"` 的 receipt（保证 `id` 有回声），同样落日志。**任何一种未知值都绝不允许把桥的主循环或主进程的读循环崩掉**——协议错误是常态而非异常。
6. 版本握手：连接建立后对端首条信封若带非 `1` 的 `v`，直接关闭连接，桥侧上报 `error_code:"version_mismatch"`；v2 引入时由本文件的更新版另行冻结。

## 7. 关键语义（不得误读）

### 7.1 `message_id` 是「发生序号」，不是 UI id

`"<epoch>:<seq>"` 中，`epoch` 就是该桥进程的 `session_epoch`，`seq` 在进程内从 1 起单调递增；跨重启 `epoch` 变化，所以整个序列在多次重启之间也不会重复。它**不是微信服务端的消息 id**（桌面微信拿不到也不需要那种 id）。用途：主进程侧事件去重（断线重放靠它去重复）、`recall` 关联回原消息、以及 bot 气泡对账（receipt 的 `message_id` ↔ 后续 event 的 `message_id`）。主进程对它只许比对相等，不许做算术或截段解析。

### 7.2 `is_bot` 防自回复死循环

桌面读屏路径**必然**会读到机器人自己打出去的气泡。`is_bot:true` 是桥对「这条是我打出去的」的判定，主进程必须据此跳过回复调度。桥也可能漏判（如桥重启后读到了旧气泡）——所以主进程还须配合 receipt/`message_id` 对账做第二道过滤，双保险。**无论哪一道失效，结果都是她自己在群里跟自己聊到天亮**——这是本项目已知的最恶性的失效模式，实现者必须同时做两道。

### 7.3 `session_epoch` 区分桥重启

桥进程每次启动时 `session_epoch` 自增 1。这份序号的用途：配合 `message_id` 的 `epoch` 段，让主进程识别「桥换了进程」，旧 epoch 的去重表可以整个作废，不会跨进程累积；也用于人工排障（排「还是收不到消息」这类问题时，能判断中间桥是不是重启过一轮）。

### 7.4 `delivered` 的三态，两两不许混淆

- `"typed"`：桥确实把文本打进了微信 UI（本轮操作成功）。**不等于对方看见**。
- `"observed"`：主进程随后在事件流里看到了自己的气泡（`is_bot:true` 且 `message_id` 对上）。**这是真送出的证据**，`typed` 只能算「曾经发出」。
- `"failed"`：本轮没打出去，`ok` 必为 `false`。

主进程把 `"typed"` 当作「送达」用于转发/持久化时，必须允许它稍后被「没看到气泡」的事实反悔（`"observed"` 才是终态）；重发去重靠这对账链路。**把 `"typed"` 当 `"observed"` 用，会导致重发后重复刷屏。**

### 7.5 `at` 数组里是显示名

`send_at` 的 `params.at` 携带的是微信 UI 能搜到的**显示名**（群成员备注/昵称），和 `chat_name`（§2.2）一脉相承——微信的 @ 交互也是按显示名找人的。名字不在群里时会匹配失败，桥逐个尝试，把结果集中写进 receipt 的 `error_code`（如 `at_unresolved`），`ok` 仍为 `true`，主进程凭 `error_code` 决定要不要提示用户。

### 7.6 `id` 关联：一条指令恰好一条回执

每条 command 带一个 uuid 形态的 `id`；桥处理完后 receipt **原样回填**这个 `id`，主进程像 OneBot v11 的 echo 关联（`gateway/qq.py:507-510`）那样维护 `id → future` 的 pending 表：发出时登记（`qq.py:426` 的 `echo`），收到 receipt 时查表唤醒，超时则判失败。超时重发必须用**新的 `id`**——旧 id 的 receipt 可能迟到，如果重发沿用旧 id，两条 receipt 会撞同一个 pending 表项，一次发送会打出两条消息。

## 8. JSON 示例（每块独立可解析）

事件（群聊消息，有人 @ 机器人）：

```json
{
  "v": 1,
  "type": "message",
  "channel": "wechat",
  "chat_type": "group",
  "chat_id": "wxg:laoyouqun",
  "chat_name": "老友群",
  "user_id": "wxg:zhangsan",
  "nickname": "张三",
  "text": "@小助手 周末去哪吃",
  "at_bot": true,
  "reply_to_bot": false,
  "reply_to_text": null,
  "ts": 1761888000,
  "message_id": "3:126",
  "is_bot": false,
  "session_epoch": 3
}
```

指令（send_at）：

```json
{
  "v": 1,
  "id": "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",
  "action": "send_at",
  "params": {
    "chat_id": "wxg:laoyouqun",
    "text": "周六老地方，我带饺子。",
    "at": ["李四"]
  }
}
```

回执（发送成功，已打字）：

```json
{
  "v": 1,
  "id": "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",
  "ok": true,
  "error_code": null,
  "delivered": "typed",
  "message_id": "3:127"
}
```

健康快照：

```json
{
  "v": 1,
  "type": "health",
  "connected": true,
  "logged_in": true,
  "desktop": "interactive",
  "wechat_version": "3.9.12.17",
  "language": "zh_CN",
  "queue_depth": 0,
  "last_event_at": 1761888100,
  "last_send_at": 1761888113
}
```

## 9. 与实现的关系

本文件是 **todo 10 / 11 / 13 的唯一实现依据**，实施顺序与落点：

- **todo 10（adapter 客户端，主进程侧）**：在 `src/dafeiyu/gateway/` 下新建微信 adapter，把本协议的四张表转成 `Adapter`（`src/dafeiyu/gateway/base.py:13-22` 的进程内 Protocol）能吃的东西；回声关联照抄 `gateway/qq.py` 的 echo/pending 模式（`qq.py:426`、`qq.py:507-510`），只是 `echo` 换成本协议的 `id`。事件解析入口对应 `qq.py:267-334` 的骨架。
- **todo 11（桥服务器）**：按本协议实现桌面侧常驻进程，事件流按 §2 吐 JSON 行，接收指令按 §3 执行、receipt 按 §4 回填 `id`，健康快照按 §5 周期上报；显示名寻址与 resync 按 §2.2 落地。
- **todo 13（驱动门面）**：把 adapter 以上的一层（发送接口、健康轮询、会话生命周期）封装成 dafeiyu 内部可调用的门面。凡本文件与三个下游实现的冲突，以本文件为准；歧义在实现 PR 里提回本文件改版，不许代码各改各的。
