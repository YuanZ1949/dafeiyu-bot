# 大肥鱼 QQ 群聊机器人 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让一个 QQ 小号在群里以「溟月 / 大肥鱼」人设聊天：被叫到必回，其余时候自主判断是否插话，并记住群友与话题。

**Architecture:** 单进程 asyncio Python 服务。NapCat 通过 OneBot v11 反向 WebSocket 把 QQ 事件推给本服务；消息经统一模型、路由判定、上下文拼装、DeepSeek 单次调用（判断+生成）后由发送层回写 NapCat。记忆落 SQLite，印象与日报走异步旁路。

**Tech Stack:** Python 3.11+、asyncio、websockets（反向 WS 服务端）、httpx（DeepSeek）、PyYAML、python-dotenv、pytest、pytest-asyncio、respx

**Spec:** `docs/superpowers/specs/2026-10-24-im-bot-design.md`

## Global Constraints

- Python `>=3.11`；依赖仅限：`websockets`、`httpx`、`PyYAML`、`python-dotenv`；测试额外 `pytest`、`pytest-asyncio`、`respx`。不引入 openai SDK、不引入 ORM、不引入向量库。
- 所有测试**不联网**、不依赖真实 QQ 号或 API Key。
- 代码标识符用英文；用户可见文案、日志、docstring 用中文。
- 日期与「当日」一律按 `Asia/Shanghai` 计算。
- 密钥只从环境变量 / `.env` 读取，任何日志与异常信息不得打印 `DEEPSEEK_API_KEY`。
- 回复默认上限 120 字，技术问题 300 字；QQ 单条消息上限 500 字。
- 微信端 v1 只允许抛 `NotImplementedError`，不得有假实现。
- 每个 Task 结束时工作区必须可运行 `pytest` 且全绿。

## Review Focus

1. **混合消息段**：图片+文本、@全体、未知段类型同时出现时，解析不能抛异常，`@全体成员` 不能被当成 @机器人。
2. **模型返回异常**：DeepSeek 返回 markdown 包裹的 JSON、非法 JSON、空 reply 时，不能静默丢消息。
3. **并发与冷却**：同一群短时间多条消息到达时，冷却判定必须串行，不能同时放行多条。
4. **上下文超预算**：裁剪时**当前这条用户消息必须保留**，不能被裁掉。
5. **重启后状态**：进程重启后消息窗口、去重索引、当日用量仍然正确（WAL + 唯一索引 + 日期查询）。

---

### Task 1: 项目脚手架与配置加载

**Files:**
- Create: `pyproject.toml`, `src/dafeiyu/__init__.py`, `src/dafeiyu/config.py`, `config.yaml`, `.env.example`, `tests/test_config.py`

**Interfaces:**
- Produces: `Settings`（字段：`api_key:str, base_url:str, model:str, bot_qq:int, ws_host:str, ws_port:int, ws_token:str, db_path:str` 以及 `persona/triggers/groups/budget/memory/logging` 六个子配置）、`load_settings(env_file: Path = Path(".env"), config_file: Path = Path("config.yaml")) -> Settings`、`ConfigError(Exception)`

- [ ] **Step 1: 写失败测试**

`tests/test_config.py`：用 `tmp_path` 生成最小 `.env` 与 `config.yaml`，断言：
```python
def test_missing_api_key_raises(tmp_path):
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        load_settings(env_file=write_env(tmp_path, ""), config_file=write_yaml(tmp_path))

def test_env_overrides_yaml_and_validates(tmp_path):
    s = load_settings(env_file=write_env(tmp_path, "DEEPSEEK_API_KEY=sk-test\nDEEPSEEK_MODEL=deepseek-reasoner\n"),
                      config_file=write_yaml(tmp_path))
    assert s.model == "deepseek-reasoner"
    assert s.budget.daily_calls == 200
    assert s.memory.window_size == 20
```
另加：`sample_rate=1.5` 抛 `ConfigError`；`quiet_hours` 长度不为 2 抛 `ConfigError`；错误信息中不含 key 值。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_config.py -v` → 期望 `ModuleNotFoundError` / `ImportError`

- [ ] **Step 3: 实现**

`pyproject.toml` 用 setuptools，`package-dir = {"" = "src"}`，声明上述依赖与 `[tool.pytest.ini_options] asyncio_mode="auto"`。
`config.py`：dataclass 定义各子配置；`load_settings` 先读 yaml，再用 `dotenv_values(env_file)` 与环境变量覆盖；缺 `DEEPSEEK_API_KEY` 或 `BOT_QQ` 抛中文 `ConfigError`；校验 `0 < sample_rate <= 1`、`window_size >= 1`、`quiet_hours` 长度为 2、端口 1–65535。`config.yaml` 与 `.env.example` 按 spec §11 原样落盘（`.env.example` 值用占位符）。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_config.py -v` → 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 项目脚手架与配置加载"
```

---

### Task 2: 统一消息模型与 OneBot 事件解析

**Files:**
- Create: `src/dafeiyu/models.py`, `src/dafeiyu/gateway/__init__.py`, `src/dafeiyu/gateway/qq.py`（先只放 `parse_event`）、`tests/test_qq_parse.py`, `tests/fixtures/onebot_events.py`

**Interfaces:**
- Produces: `ChatType(str, Enum)`；`Segment(type:str, data:dict)`；`Message(chat_type, chat_id, user_id, nickname, content, message_id, segments, is_bot, at_bot, reply_to_bot, timestamp)`；`parse_event(event: dict, bot_qq: int, bot_message_ids: Collection[str] = ()) -> Message | None`；`Message.to_prompt_line() -> str`（返回 `昵称: 内容`）

- [ ] **Step 1: 写失败测试**

`tests/test_qq_parse.py` 覆盖 7 个用例：群文本、@机器人、@全体、引用机器人消息、私聊、`post_type != message` 返回 None、混合段（`text + image + face`）→ content 为 `文字[图片][表情]`。关键断言：
```python
def test_at_all_is_not_at_bot():
    m = parse_event(group_event(segments=[at("all"), text("在吗")]), bot_qq=10001)
    assert m.at_bot is False

def test_at_bot_sets_flag():
    m = parse_event(group_event(segments=[at("10001"), text("你好")]), bot_qq=10001)
    assert m.at_bot is True
    assert "你好" in m.content
```

- [ ] **Step 2: 运行确认失败** → `pytest tests/test_qq_parse.py -v` 报 `ImportError`

- [ ] **Step 3: 实现**

`parse_event` 只处理 `post_type == "message"`；`message_type` 映射 `group/private`；`message` 可能是 list（段数组）或 str（CQ 码），str 时直接作为 content；`at` 段 `data["qq"] == str(bot_qq)` 才算 `at_bot`；`reply` 段 `data["id"] in bot_message_ids` 才算 `reply_to_bot`；`sender.card or sender.nickname` 取昵称；`message_id` 转 str。**任何段类型都不得抛异常**，未知段忽略。

- [ ] **Step 4: 运行确认通过** → `pytest tests/test_qq_parse.py -v` 全 PASS

- [ ] **Step 5: 提交** → `git commit -m "feat: OneBot 事件解析与统一消息模型"`

---

### Task 3: SQLite 存储层

**Files:**
- Create: `src/dafeiyu/memory/__init__.py`, `src/dafeiyu/memory/store.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `Store(path)`，方法：`init_schema()`、`close()`、`add_message(m: Message) -> bool`、`recent_messages(chat_id:int, limit:int) -> list[Message]`、`count_messages(chat_id:int) -> int`、`upsert_impression(chat_id:int, subject_user_id:int|None, kind:str, content:str, confidence:float) -> None`、`search_impressions(chat_id:int, query:str, topk:int) -> list[Impression]`、`list_impressions(chat_id:int, topk:int) -> list[Impression]`、`upsert_summary(chat_id:int, date:str, content:str)`、`get_summary(chat_id:int, date:str) -> str|None`、`record_usage(chat_id:int, purpose:str, prompt_tokens:int, completion_tokens:int)`、`usage_today(chat_id:int, today:str) -> tuple[int,int]`、`forget_chat(chat_id:int)`、`forget_all()`
- Produces: `Impression(chat_id, subject_user_id, kind, content, confidence, updated_at)` 于 `models.py`

- [ ] **Step 1: 写失败测试**

`tests/test_store.py`：`tmp_path` 建库；断言重复 `message_id` 的 `add_message` 返回 False 且只存一条；`recent_messages` 返回从旧到新且长度受限；中文印象可被 FTS5 关键词召回（`search_impressions(1,"显卡",5)`）；`usage_today` 两次记录后返回 `(2, 30)`；`forget_chat` 后消息、印象、摘要均为空；重开同一路径 `Store` 数据仍在。

- [ ] **Step 2: 运行确认失败** → `pytest tests/test_store.py -v`

- [ ] **Step 3: 实现**

按 spec §6 的 DDL 建表；`PRAGMA journal_mode=WAL`、`busy_timeout=5000`；`add_message` 用 `INSERT OR IGNORE` 并按 `rowcount` 返回布尔；`search_impressions` 用 `impressions_fts MATCH ?` 并按 `bm25()` 排序，查询失败（FTS 语法）时降级为 `LIKE`。

- [ ] **Step 4: 运行确认通过** → `pytest tests/test_store.py -v`

- [ ] **Step 5: 提交** → `git commit -m "feat: SQLite 记忆存储层"`

---

### Task 4: 上下文拼装与预算裁剪

**Files:**
- Create: `src/dafeiyu/memory/context.py`, `tests/test_context.py`

**Interfaces:**
- Consumes: `Store.recent_messages/list_impressions/get_summary`
- Produces: `estimate_tokens(text:str) -> int`（CJK 按 1 字 ≈ 0.7 token、其余按 4 字符 ≈ 1 token，向上取整）；`ContextBundle(system:str, messages:list[dict], estimated_tokens:int)`；`build_context(store, chat_id:int, window_size:int, impression_topk:int, system:str, today:str) -> ContextBundle`

- [ ] **Step 1: 写失败测试**

断言：窗口 25 条只取最近 20；消息按 `{"role":"user","content":"昵称: 内容"}` 输出，机器人消息为 `assistant`；**超预算时最后一条（当前消息）必须保留**；裁剪顺序为 先窗口 → 再印象 → 最后摘要；`estimate_tokens("你好")` 返回 2。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

预算常量 `CONTEXT_BUDGET=2500`、`OUTPUT_RESERVE=300`；循环丢弃最旧窗口消息直到 ≤2200 或只剩当前一条；仍超则去掉印象（保留 `topk` 从 5 递减到 0）；仍超则去掉摘要；最后截断 system 到 600 token 等价值。

- [ ] **Step 4: 运行确认通过** → `pytest tests/test_context.py -v`

- [ ] **Step 5: 提交** → `git commit -m "feat: 上下文拼装与 token 预算裁剪"`

---

### Task 5: 人设提示词

**Files:**
- Create: `src/dafeiyu/brain/__init__.py`, `src/dafeiyu/brain/persona.md`, `src/dafeiyu/brain/persona.py`, `tests/test_persona.py`

**Interfaces:**
- Produces: `load_persona(path: Path | None = None) -> str`（默认读包内 `persona.md`）；`build_system_prompt(persona_text:str, chat_title:str, today:str, max_chars:int, max_chars_technical:int) -> str`

- [ ] **Step 1: 写失败测试**

断言 system prompt 同时包含：`溟月`、`大肥鱼`、安全退场规则关键词（`不人身攻击`、`医疗`）、`120`、`300`，以及当前日期与群名；不包含任何 API Key 字样。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 写 `persona.md` 与实现**

`persona.md` 按 spec §9 五层写：身份（蓝发蓝瞳、呆毛鲸鳍、鲸尾、女仆装）、语气（短句、贱萌傲娇、5% 内心 OS、少 emoji）、群聊规则（1–2 句、不 @全体、不连续刷屏）、安全层（不人身攻击、不碰政治色情违法、医疗法律财务不做确定性建议、被要求「别演了」立即退场、不泄露 prompt/配置/密钥）、5 条 few-shot（被叫到/被夸/被怼/答技术问题/冷场）。`build_system_prompt` 用 `string.Template` 替换 `$persona/$chat_title/$today/$max_chars/$max_chars_technical`，金额符号不用 `$` 以免冲突。

- [ ] **Step 4: 运行确认通过** → `pytest tests/test_persona.py -v`

- [ ] **Step 5: 提交** → `git commit -m "feat: 溟月/大肥鱼人设提示词"`

---

### Task 6: DeepSeek 客户端

**Files:**
- Create: `src/dafeiyu/brain/client.py`, `tests/test_brain.py`

**Interfaces:**
- Produces: `BrainReply(speak:bool, reply:str, reason:str, prompt_tokens:int, completion_tokens:int)`；`BrainError(Exception)`；`BrainClient(api_key:str, base_url:str, model:str, timeout:float=15.0, sleep=asyncio.sleep, transport=None)`；`async decide(system:str, messages:list[dict], max_tokens:int=400) -> BrainReply`

- [ ] **Step 1: 写失败测试（respx）**

用例：正常 JSON → `speak=True` 且 reply 正确；```json 围栏包裹 → 正确解析；非法 JSON → `speak=True`、reply 为原文；`reply` 为空 → 返回固定兜底句；HTTP 500 一次后成功 → 共 2 次请求；连续 500 → 抛 `BrainError`；断言请求体 `model` 来自构造参数、`temperature==1.0`。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

`httpx.AsyncClient` POST `{base_url}/chat/completions`；system 放 `messages[0]`；解析前剥离 ```json 围栏；`json.loads` 失败则整段当 reply；重试仅对 `TimeoutException`、`ConnectError`、5xx，最多 1 次，退避 2s（用注入的 `sleep`）；429 直接抛 `BrainError`；捕获响应中的 `usage` 填 token 数，缺失记 0。

- [ ] **Step 4: 运行确认通过** → `pytest tests/test_brain.py -v`

- [ ] **Step 5: 提交** → `git commit -m "feat: DeepSeek 客户端与失败降级"`

---

### Task 7: 回复整形与发送分片

**Files:**
- Create: `src/dafeiyu/sender.py`, `tests/test_sender.py`

**Interfaces:**
- Produces: `shape_reply(text:str, max_chars:int=120, max_chars_technical:int=300, is_technical:bool=False) -> list[str]`；`is_technical_question(text:str) -> bool`

- [ ] **Step 1: 写失败测试**

断言：100 字原样返回单元素；200 字（无标点）截到 120 并带 `…`；含句号的 200 字在最后一个句号处截断且不超 120；`is_technical=True` 时上限 300；800 字结果按 500 字分片，每片 ≤500；空字符串返回 `[]`。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

先 `strip()`；超限时在 `max_chars` 内找最后一个 `[。！？!?；;\n]` 截断，找不到则硬切并补 `…`；分片优先在换行/句末切。

- [ ] **Step 4: 运行确认通过** → `pytest tests/test_sender.py -v`

- [ ] **Step 5: 提交** → `git commit -m "feat: 回复截断与分片"`

---

### Task 8: 触发路由与护栏

**Files:**
- Create: `src/dafeiyu/router.py`, `tests/test_router.py`

**Interfaces:**
- Produces: `TriggerDecision(call_model:bool, hard:bool, reason:str)`；`Command(name:str, args:list[str])`；`parse_command(text:str) -> Command|None`；`Router(settings, store, clock: Callable[[], datetime] = now_shanghai, rng: random.Random | None = None)`，方法 `hard_trigger(msg) -> bool`、`rule_filter(msg, now) -> tuple[bool,str]`、`should_handle(msg) -> TriggerDecision`、`note_bot_spoke(chat_id:int)`、`note_reply(chat_id:int)`、`note_no_reply(chat_id:int)`、`quiet(chat_id:int, minutes:int)`、`wake(chat_id:int)`、`is_quiet(chat_id:int, now) -> bool`

- [ ] **Step 1: 写失败测试**

用例：@机器人/含别名/引用机器人 → 硬触发；黑名单群任何情况都不回；白名单非空时未列出的群不回；机器人 30s 前说过话 → 规则筛拒绝，61s 前 → 通过；静默时段（`02:00`）拒绝；当日调用达上限拒绝（硬触发也拒绝）；`rng` 固定返回 0.01 < 0.05 时通过采样；连续 3 次 `note_no_reply` 后 `rng` 返回 0.08（<0.1 但 >0.05）仍通过采样（降频即采样率减半）；`parse_command("/闭嘴")` 返回 `Command("闭嘴", [])`。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

顺序：黑名单 → 硬触发（命中则跳过冷却/静默/采样，但仍受预算约束）→ 白名单 → 冷却（读 `_last_bot_spoke`）→ 静默 → 预算（`store.usage_today`）→ 采样/疑问词/话题热度。静默时段支持跨午夜（`start > end` 时 `now >= start or now < end`）。每群状态存内存 dict，重启丢失可接受。`note_no_reply` 使连续未回应计数 +1，达到 3 时设置 `_downshift_until = now + 30min`，期间采样率取 `sample_rate / 2`；`note_reply` 清零计数。

- [ ] **Step 4: 运行确认通过** → `pytest tests/test_router.py -v`

- [ ] **Step 5: 提交** → `git commit -m "feat: 三层触发与预算护栏"`

---

### Task 9: OneBot 反向 WebSocket 适配器

**Files:**
- Create: `src/dafeiyu/gateway/base.py`, `src/dafeiyu/gateway/qq.py`（追加 `QQAdapter`）、`tests/test_qq_adapter.py`

**Interfaces:**
- Produces: `Adapter` 协议（`start()`, `stop()`, `send(chat_type:ChatType, chat_id:int, text:str)`, `on_message(handler)`）；`QQAdapter(host:str, port:int, token:str, bot_qq:int)`；`async call_action(action:str, params:dict) -> dict`

- [ ] **Step 1: 写失败测试**

用 `websockets.connect` 连到适配器临时端口（`port=0` 后取实际端口）：
```python
async def test_event_reaches_handler_and_send_emits_action():
    # 客户端发 group 事件 → handler 收到 Message
    # 调 adapter.send(...) → 客户端收到 {"action":"send_group_msg","params":{"group_id":1,"message":"hi"},"echo":...}
    # 客户端回 {"status":"ok","retcode":0,"data":{"message_id":9},"echo":...}
    # await send 正常返回，且 9 进入 bot_message_ids
```
另测：错误 token 连接被 403 关闭；私聊发 `send_private_msg`；`stop()` 后端口释放。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

`websockets.serve(handler, host, port)`；握手时校验 `Authorization: Bearer <token>`（token 为空则跳过）；每帧 `json.loads`；`post_type=="message"` → `parse_event` → `handler`；带 `echo` 且无 `post_type` → 解析为 pending future 的结果；`send` 用 `asyncio.wait_for` 等响应 10s，超时抛 `GatewayError`；连接断开时清空 pending futures（置异常）。

- [ ] **Step 4: 运行确认通过** → `pytest tests/test_qq_adapter.py -v`

- [ ] **Step 5: 提交** → `git commit -m "feat: OneBot 反向 WebSocket 适配器"`

---

### Task 10: 主链路编排

**Files:**
- Create: `src/dafeiyu/service.py`, `src/dafeiyu/__main__.py`, `tests/test_service.py`

**Interfaces:**
- Consumes: 以上全部
- Produces: `Service(settings, store, brain, adapter, router, persona_text)`，方法 `async start()`、`async handle(msg: Message)`、`async stop()`；`__main__.py` 提供 `asyncio.run(main())`

- [ ] **Step 1: 写失败测试（全部 mock）**

用例：硬触发 → brain 调 1 次、adapter.send 1 次、usage 记录 1 条；`speak=False` → 不发送但仍记 usage；私聊绕过冷却（连续两条都回）；`/闭嘴` 不调 brain、不发送、后续消息被静默；重复 `message_id` 第二次直接返回；`BrainError` 时不发送且不崩溃；**同群两个 `handle` 并发执行时只发送 1 条**（假 brain 内 `await asyncio.sleep(0.05)`，第二个调用被冷却挡下）。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

`handle` 顺序：`store.add_message`（False → return）→ `parse_command`（群内 `/` 开头，先过权限）→ `router.should_handle` → `build_context` → `brain.decide` → `record_usage` → 若 `speak` 则 `shape_reply` 并逐片 `adapter.send` → `router.note_bot_spoke` → 触发 `impressions.maybe_extract`（`asyncio.create_task`，异常吞掉记日志）。每群一个 `asyncio.Lock` 包住「判定→发送」，保证冷却原子性。`__main__` 负责按 `settings.logging` 配置 `logging`（文件 `logs/dafeiyu.log` + 控制台）、加载配置、建库、拼 system prompt、注册 handler、启动摘要调度器、`SIGINT` 优雅退出。

- [ ] **Step 4: 运行确认通过** → `pytest tests/test_service.py -v`

- [ ] **Step 5: 提交** → `git commit -m "feat: 主链路编排与私聊/指令处理"`

---

### Task 11: 群友印象抽取

**Files:**
- Create: `src/dafeiyu/workers/__init__.py`, `src/dafeiyu/workers/impressions.py`, `tests/test_impressions.py`

**Interfaces:**
- Produces: `ImpressionWorker(store, brain, every:int=20)`，方法 `async maybe_extract(chat_id:int) -> int`（返回新增条数）、`async extract(chat_id:int) -> int`

- [ ] **Step 1: 写失败测试**

假 brain 返回 `[{"user_id":123,"kind":"nickname","content":"外号叫鱼片","confidence":0.8}]`；喂 19 条消息 → 0 次调用；第 20 条 → 1 次调用且 store 有该印象；返回非法 JSON → 不抛异常、返回 0；同一印象重复抽取不产生重复行。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

每群维护 `_seen_count` 内存计数；达到 `every` 时取最近 40 条消息拼成抽取 prompt（system 用固定中文指令，要求只输出 JSON 数组）；`json.loads` 失败或元素字段缺失则跳过该元素；`content` 截到 60 字。

- [ ] **Step 4: 运行确认通过** → `pytest tests/test_impressions.py -v`

- [ ] **Step 5: 提交** → `git commit -m "feat: 群友印象异步抽取"`

---

### Task 12: 每日摘要

**Files:**
- Create: `src/dafeiyu/workers/summary.py`, `tests/test_summary.py`

**Interfaces:**
- Produces: `async summarize_chat(store, brain, chat_id:int, date:str) -> str`；`SummaryScheduler(store, brain, clock=now_shanghai, hour:int=4)`，方法 `async run_pending() -> int`（返回生成条数）

- [ ] **Step 1: 写失败测试**

假 brain 返回 250 字文本 → 入库后截到 200 字；当日无消息的群不调用 brain；`clock` 固定 03:59 时 `run_pending` 返回 0，04:00 时返回 1；重复调用同一天不重复生成。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

取该群当日消息（`messages.created_at LIKE 'YYYY-MM-DD%'`），拼 prompt 要求「200 字以内，记话题、结论、梗、外号」；结果 `[:200]`；`summaries` 表 `UNIQUE(chat_id,date)` 冲突则跳过。

- [ ] **Step 4: 运行确认通过** → `pytest tests/test_summary.py -v`

- [ ] **Step 5: 提交** → `git commit -m "feat: 每日群摘要"`

---

### Task 13: 微信占位适配器与 CLI

**Files:**
- Create: `src/dafeiyu/gateway/wechat.py`, `src/dafeiyu/cli.py`, `tests/test_wechat.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `WeChatAdapter`，与 `Adapter` 协议同签名；`cli.main(argv: list[str] | None = None) -> int`，子命令 `forget --chat <id> | --all`、`stats --chat <id>`、`summary --chat <id> --date <YYYY-MM-DD>`

- [ ] **Step 1: 写失败测试**

`WeChatAdapter.send` / `start` 均抛 `NotImplementedError` 且消息含「微信接入未实现」；`cli.main(["forget","--chat","1"])` 后该群消息为 0；`stats` 输出含 `调用` 与 `token`；未知子命令返回 2。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

`wechat.py` docstring 写明三条候选路径（WeChatPadPro / wechatauto-replica / 企微），类继承同一协议并明确 `raise NotImplementedError("微信接入未实现，见 docstring")`。`cli.py` 用 `argparse`，复用 `load_settings` 与 `Store`。

- [ ] **Step 4: 运行确认通过** → `pytest tests/test_wechat.py tests/test_cli.py -v`

- [ ] **Step 5: 提交** → `git commit -m "feat: 微信占位适配器与运维 CLI"`

---

### Task 14: 启动脚本、README 与实机验证

**Files:**
- Create: `scripts/start.ps1`, `README.md`
- Modify: `.gitignore`（确认含 `data/`、`logs/`、`.venv/`）

**Interfaces:**
- Produces: 可照做的部署文档；`scripts/start.ps1` 创建/复用 venv、安装依赖、启动 `python -m dafeiyu`

- [ ] **Step 1: 写启动脚本**

`start.ps1`：若 `.venv` 不存在则 `python -m venv .venv`；`.venv\Scripts\python -m pip install -q -e .`；检查配置文件存在，缺则提示复制示例并退出 1；`Set-Location` 到仓库根；`& .venv\Scripts\python -m dafeiyu`。

- [ ] **Step 2: 写 README**

包含：项目简介、架构图（贴 spec §5 简版）、前置条件（Python 3.11、NapCat）、NapCat 安装与反向 WS 配置（`ws://127.0.0.1:8080/onebot/v11/ws`）、配置说明（每个字段一行）、启动步骤、四个指令、常见故障（扫码失败 / WS 连不上 / 群消息无响应 / 429）、隐私与 `forget`、封号风险提示。

- [ ] **Step 3: 按 README 实跑**

Run: `powershell -File scripts/start.ps1`
Expected: 日志出现 `已连接 NapCat`（无 NapCat 时出现明确的等待提示，不崩溃）；`pytest` 全绿。

- [ ] **Step 4: 全量测试**

Run: `pytest -q` → 全部 PASS，无 skip（除标注需要真实环境的用例，且数量为 0）

- [ ] **Step 5: 提交** → `git commit -m "docs: 部署文档与一键启动脚本"`

---

## 交付后的人工验收（不属于自动测试）

1. NapCat 扫码登录 QQ 小号，拉进测试群。
2. 群里 @它 连续问 3 次，记录时间戳，中位数 < 10s。
3. 静置 10 分钟观察它是否会在没有 @ 的情况下插话，且不刷屏。
4. 改 `DEEPSEEK_MODEL` 重启，日志显示新模型名。
5. 跑 `python -m dafeiyu forget --chat <群号>`，再问它「我们刚才聊了啥」，确认已失忆。
