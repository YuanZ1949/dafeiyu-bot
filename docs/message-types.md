# 消息类型能力矩阵

本文只描述**当前代码已经做到的事**。没做到的（视频完整转码（只抽帧看画面，见「入站媒体」）、微信 UI 剪贴板发图、微信出站引用/合并转发等）在矩阵里明确写「不支持」，不要按「以后会有」理解。

图例：

- **支持**：正文/发送链路能正确处理，失败也会给出明确降级。
- **只记录元数据**：正文只保留占位或摘要，`Attachment` 里可能有类型/名字/大小/时长，但不读文件内容。
- **占位**：只保证消息不丢、正文有可读标记；媒体本身不获取、不落盘。
- **不支持**：该方向没有实现，或实现会明确返回错误并回落。

## 能力矩阵

| 能力 | QQ 入站 | 微信入站 | QQ 出站 | 微信出站 |
| --- | --- | --- | --- | --- |
| 文本 | 支持 | 支持 | 支持 | 支持（`send_text`） |
| 图片 | 支持（`images.enabled` 时落盘并把路径写进正文；失败保留 `[图片]`） | 支持（`gateway.wechat.media.enabled` 时解密落盘给路径；否则占位） | 支持（`send_segments` 的 image 段） | 支持（hook 后端经 `send_media`；UIA 后端对 image 同样恒 `unsupported_media`，正文照发） |
| 语音 | 支持（`speech.enabled` 且 URL 下载由 `images.enabled` 门控时落盘 `voices/` 并转写，正文变 `[语音]（转写：…）`；依赖缺失、超时、解码失败保留 `[语音]` 占位） | 支持（`speech.enabled` 与 `gateway.wechat.media.enabled` 都打开时读取本地文件落盘并转写；否则占位） | 支持（`send_segments` 的 record 段，kind 为 `record`/`voice`） | 支持（hook 后端 `send_media` kind=voice；UIA 不支持） |
| 视频 | 支持（`images.enabled` 时下载 URL 原文件到 `videos/`，`annotate` 为真时用 ffmpeg 抽 4 帧到 `images/` 供 `read` 查看；失败保留 `[视频]`） | 支持（`gateway.wechat.media.enabled` 时复制原文件到 `videos/`，并用 ffmpeg 抽 4 帧到 `images/` 供 `read` 查看；失败保留 `[视频]`） | 支持（`send_segments` 的 video 段，路径同样受 `replies/` 与后缀白名单约束） | 不支持（hook 的 `send_media` 只认 image/file/voice/sticker，`kind=video` 明确回 `unsupported_media`；UIA 也不支持。附件不发出，正文照发） |
| 文件 | 只记录元数据（正文 `[文件] 名字`，记录名字/大小，不下载） | 占位（按 link/占位处理，不下载） | 支持（`upload_file`，模型附件 `kind=file`） | 支持（hook 后端：模型附件 `kind=file` 经 `upload_file` → `send_media` 的 `kind=file`；桥回 `unsupported_media` 时附件跳过、正文照发。UIA 后端仍不支持） |
| 表情 / 贴纸 | 占位但带标识（`face` 在 QQ 内置表情表内时显示名称 `[表情:<名称>]`，表外保留数字 id `[表情:<数字 id>]`；`mface` → `[表情:<摘要>]`，缺失时回退 `[表情]`；同时记录 sticker 名字/id） | 占位但带标识（`[表情:<desc>]`；`desc` 是 base64 protobuf 时解出中文名，无名字用 `[表情:<md5 前 8>]`，解析失败回退 `[表情]`）；贴纸图片在 `gateway.wechat.media.enabled` 时按图片落盘（缓存未命中则用 `cdnurl` 下载），模型可 `read` 查看；两者都拿不到时正文追加 `（未缓存）` | 支持（模型附件 `type=sticker`：`name` 可用**名称**（按内置表情表反查，如 `微笑`）或**数字 id**（1–6 位非 0）出 `face` 段；两者都未命中的文本出 `mface` 摘要段，mface 只有 summary，属 best-effort——缺 url/key/emoji_id 字段） | 支持（hook 后端 `send_media` kind=sticker：`path` 必须指向真实 GIF（GIF 头校验、≤1 MiB、≤1024px），非 GIF/超限被 hook 侧回 `hook_rejected` 时自动降级为图片再发；无 `path` 跳过实图发送、正文照发；`send_emoticon`）。UIA 后端不支持任何媒体 |
| @ | 支持（正文保留 `@名字`，并标记 `at_bot`） | 支持（正文保留原文，并标记 `at_bot`） | 支持（at 段；名字必须在本批次在场成员里，最多 3 个） | 支持（hook 后端 `send_at`；UIA 返回 `unsupported_at`，回落纯文本） |
| 合并转发 | 支持（递归渲染成「昵称: 内容」文本，深度/节点数有上限，超限占位） | 占位（appmsg `type 19` 渲染为 `[合并转发] <title>`，无标题时仅 `[合并转发]`；不展开聊天记录，`kind` 仍是 `link`） | 支持（模型附件 `type=forward`，`nodes` 最多 5 条、单条 200 字；失败降级为一行占位文本） | 不支持（无转发发送原语；附件降级为正文末尾一行 `[合并转发 N 条]`，见下文） |
| 引用回复（quote） | 支持（`reply` 段解析出 `reply_to_message_id` / `reply_to_user_id`，`reply_to_bot` 参与点名判定） | 支持（`kind=quote`，引用块渲染成 `reply_to_text` / `reply_to_author`） | 支持（`persona.quote_reply` 为真时首片带 `quote_id`，走 `send_segments`） | 不支持（桥只有 `send_text` / `send_at` / `send_media`，没有引用原语；回落纯文本，需在正文复述，见下文） |
| 戳一戳 / 进退群 / 禁言 | 支持解析（`NoticeEvent`）：戳一戳回固定短句、进退群可发欢迎语，都不走模型；禁言只记日志 | 不支持 | 不支持 | 不支持 |
| 卡片 / 链接 | 支持（json/xml 卡片渲染成文本摘要） | 支持（appmsg 的标题渲染为 link；视频号分享（`<finderFeed>`）渲染 `[视频号] 昵称：描述`，不下载视频/封面；引用消息渲染为 quote） | 不支持 | 不支持 |
| 位置 | 支持（渲染 `[位置] 标题` 摘要） | 占位（正文 `[位置]`） | 不支持 | 不支持 |

补充：

- 微信入站 `kind` 有白名单（text / image / voice / video / file / sticker / card / location / call / link / quote / record / forward）；未知值一律按 `text` 处理。
- QQ 入站未知消息段一律忽略，不影响同一条消息的其余部分。
- `Attachment` 与 `segments` 只在内存中流转，**不入库**；`messages.content` 仍是唯一持久载体。重启后只有正文里的占位/路径还在。

## 安全边界

### 出站附件（模型 → 群）

- 不支持**文生图 / 绘图 API**：机器人不会调用任何模型生成图片，出站图片只会是工作区里已经存在的文件（模型用文件工具写进 `replies/` 的，或本会话收到过并落盘的图）。
- 不允许**跨会话图片引用**：模型只能引用本会话最近消息里出现过的 `images/` 路径，其它会话收到的图一律拒绝（见下）。

模型可以在回复 JSON 里带 `attachments: [{"type": "image"|"file"|"voice"|"video", "path": "replies/xxx.png"}]`，但路径必须同时满足：

- 只接受两类根目录下的普通文件：`<workspace.dir>/replies/` 恒可用；`<workspace.dir>/images/` 下的 `<name>` 只在「本会话最近 **200 条**消息正文里逐字出现过 `images/<name>`」时放行（归属校验）。**跨会话的 images/ 路径**、200 条窗口之外的旧路径、以及正文从未出现过的路径一律拒绝；没有记忆存储可用时（`store=None`）`images/` 全部拒绝，只有 `replies/` 通过；
- 路径先做形态检查（绝对路径 / 盘符 / `..` 一律拒绝），再 `resolve()` 后检查是否仍在对应根目录内（软链逃逸也会被挡住）；
- 后缀白名单：`.png` / `.jpg` / `.jpeg` / `.gif` / `.webp` / `.mp4` / `.mp3` / `.wav` / `.pdf` / `.txt` / `.zip`（不区分大小写）；
- 文件必须非空且不超过 **10 MiB**；
- 工作区关闭、目标缺失、解析失败一律跳过并留 warning，绝不抛错，也不读取/发送模型给的任意路径。

QQ 侧：图片/语音/视频走 OneBot 消息段，文件走 `upload_file`。表情用 `{"type": "sticker", "name": "...", "path": "可选"}`：`name` 为 1–6 位非 0 数字或内置表情名称（如 微笑）时出 `face` 段，其它文本出 `mface` 摘要段（mface 只有 summary，best-effort，没有 url/key/emoji_id）；只校验名字非空。带 `path` 的贴纸先走与图片相同的路径白名单（`replies/` 或已归属的 `images/`，GIF/大小限制由 hook 侧校验，非 GIF/超限降级为图片）；`path` 合法但 `name` 为空的贴纸不发 QQ 表情段（不拿文件名伪装 mface）。
微信侧分两种后端：**hook** 后端走 `send_media`，支持图片/语音/文件/贴纸（贴纸要求 `path` 指向真实 GIF）；**UIA** 后端对**所有媒体**（图片/语音/视频/文件/贴纸）都明确返回 `unsupported_media`（`gateway/wechat_bridge/sender.py`），没有 sticker→image 这类降级链，客户端不重试，调用方继续发文本；不做剪贴板发图。

入站表情的正文格式统一为 `[表情:<标识>]`：QQ `face` 在内置表情表内用显示名称、表外保留数字 id，`mface` 用摘要文本；微信优先用 `<emoji desc/attachedtext>`，为空时用 md5 前 8 位。标识只是让模型能区分「哪个表情」，真正的语义仍要靠贴纸图片本身（见下）。

### 微信出站：引用与合并转发（不支持）

微信通道**没有**「引用某条消息」和「合并转发」的出站原语，这不是配置开关的问题：

- 桥的**发送类**指令白名单只有 `send_text` / `send_at` / `send_media`（`src/dafeiyu/gateway/wechat_bridge/server.py` 的 `_KNOWN_ACTIONS` 还含 `probe` / `stop` / `resume` / `ack` / `hello` 等协议动作）；hook 后端的 HTTP 路由也只有 `/send_text`、`/send_at`、`/send_media`（`scripts/wechat_hook_server.py`），其它路径直接 404。
- helper 仓（默认 `C:\MyFile\AICoding\helper`，可用 `WECHAT_HELPER_ROOT` 覆盖）的 `helper/native/*.h` 只有图片/文件/语音/表情的发送头（`image_send.h`、`file_send.h`、`voice_send.h`、`emoticon_send.h`），没有 quote/forward 发送头；`wxbot_framework/forwarded.py` 与 `forwarded_receiver.py` 只解析**入站**的合并转发（type 49 / `app_type=19`），不提供发送能力。
- 客户端侧同样没有：`gateway/wechat.py` 只实现 `send` / `send_at` / `send_media`，没有 `send_segments`、`send_forward`；`upload_file` 只是 `send_media(kind="file")` 的薄封装，不提供引用/转发原语。

具体表现：

- **引用回复**：`persona.quote_reply` 只在适配器提供 `send_segments` 时才会把 `quote_id` 传下去（`Service._send_chunk`）。微信适配器没有这个能力，所以即使 `quote_reply: true` 也只发纯文本；要「引用」某句话，只能在正文里复述（例如「你刚才说的 X……」）。
- **合并转发**：模型的 `{"type": "forward", "nodes": [...]}` 附件先走 `Service._send_chunk` 的转发分支；微信适配器没有 `send_forward`，于是降级为正文末尾一行 `[合并转发 N 条]`，其余文字照发。只有 QQ 适配器实现了 `send_group_forward_msg` / `send_private_forward_msg`。
- **微信入站引用仍然可用**：桥把引用块解析成 `reply_to_text` / `reply_to_author`（消息 `kind=quote`），模型能看到被引用内容，只是回复时无法挂回原消息。

#### 为什么不能拿 `message_id` 去引用：`epoch:seq` 合成值

微信桌面端不暴露稳定的消息 id。`src/dafeiyu/gateway/wechat_bridge/align.py` 的做法是顺序对齐后本地编号：

- 每轮读会话最近的 `K = 50` 条 `(nickname, text)`，用「上次窗口的后缀 = 本次窗口的前缀」做重叠匹配；只有重叠之后的新条目才编号。
- `seq` 从上次的编号继续（`+1, +2 …`），`message_id = f"{session_epoch}:{seq}"`（`align.py` 的 `_build_message`，约 246 行）。`session_epoch` 是随机的 31 位 int，只在一段连续对齐内唯一；对齐失败（`overlap_low` / `window_empty`）会换新 epoch、`seq` 归零，并额外发一条 `resync` 事件。
- 因此这个 id 是**本地合成值**：可以当数据库主键、去重键、会话内 `reply_to_message_id` 的匹配依据，但它不是微信的消息句柄，**不能**交给微信去引用/转发；跨 `resync` 也不保证稳定。同一个 `message_id` 只在「同一 epoch + 同一会话」内有效。
- 副作用：文件名不能直接用 `message_id`（含 `:`，Windows 非法），所以 TTS 落盘用 `voice-<uuid>.mp3`。

替代做法：把要引用的内容当作文本复述给模型/群友；需要「转发」时用多段文字或 `[合并转发 N 条]` 占位说明。不要声称微信侧真的发出了引用或转发卡片。

### 站内语音消息（出站 TTS）

`speech.tts_enabled: true` 且装了可选依赖 `.[tts]`（edge-tts）时，模型回复里 `voice: true` 的单段回复会被合成为语音；**默认关闭**，不装依赖时永远只有文字。

- **触达条件**：成型后的回复只有 1 段、没有 @ 目标、工作区打开（`workspace.dir` 非空）。多段回复或带 @ 的回复不转语音，照常发文字——语音段承载不了 @ 与多段语义。
- **落盘**：`<workspace.dir>/replies/voice-<uuid>.mp3`；文件名不用 `message_id`（微信的 `epoch:seq` 含 `:`，在 Windows 上是非法文件名）。先写 `<name>.tmp` 再原子替换，失败时清理临时文件，不留半截 mp3。
- **QQ 出站**：`build_onebot_segments` 把 `voice`/`record` 附件映射成 OneBot 的 `record` 段；语音段承载全部正文，发送成功就不再补发文字。
- **微信出站**：桥适配器没有消息段能力，改走 `send_media(kind="voice", path=..., name=...)`；推送成功同样不再补发文字，桥明确回 `unsupported_media` 时退回文本。
- **入站语音（STT）不变**：`speech.enabled` 的下载/解码/转写管线仍是原来的逻辑，见上文「入站媒体」；出站 TTS 与入站转写互不影响。
- **失败降级**：依赖缺失、合成异常、30 秒超时、超过 `speech.tts_max_chars`、文件为空或超过 10 MiB、段/媒体发送失败——一律退回**纯文本**，不重试、不抛错。
- **历史与 outbox**：机器人历史（`_store_bot_chunks`）和待发队列（`enqueue_outbox`）保存的是**原文**，不是空正文或语音路径，因此上下文连续、补发也仍是文字。

注意：启用后回复文本会发往云端 TTS（edge-tts / 微软语音），这是它默认关闭的原因；没装可选依赖时不会合成，也不会假装合成成功。
### 入站媒体（群 → 本机）

- QQ 图片：下载/复制到 `<workspace.dir>/images/`，文件名由时间戳 + 来源 md5 生成，不采信 URL 路径段；下载失败、超过大小上限、没有可用来源都保留 `[图片]` 占位。
- 微信图片：只有 `gateway.wechat.media.enabled: true` 才会读取本地库发现的文件；路径必须 `resolve()` 到 `storage_root`（空 = 自动探测的镜像根目录）内，文件必须存在且大小在 `(0, max_bytes]`；`.dat` 解密失败保留占位。落盘位置同样是 `<workspace.dir>/images/`。type 47 贴纸同走这条管线（消息 `kind` 仍是 `sticker`，附件 `kind` 为 `image`），落盘后正文变成 `[表情:<标识>]（图存 images/...，可用 read 查看）`。
  - **媒体发现**：4.x 的 `message/message_resource.db` 没有路径列，md5 在 `MessageResourceInfo.packed_info` 里；图片按 `msg/attach/<md5(会话)>/<YYYY-MM>/Img/<md5>.dat` 组合候选路径，以磁盘上真实存在为准。变体优先级 **原图 > `_h` > `_t`**：原图优先（最高分辨率），原图解不开（缺密钥/wxgf 转码失败）时按 `fallbacks` 顺序退回 `_h` / `_t`，而不是一开始就拿 180px 的缩略图。只有 `_t` 缓存、原图未落盘的老消息无法修复画质。
  - **视频发现**：`local_type == 43` 时按顺序找 `msg/attach/<md5(当前会话)>/<YYYY-MM>/{Video,video}/<md5>*` → 其它会话目录下的同名 `Video/video` → `msg/{video,Video}/**/<md5>*`。同一组内多个候选按「非 `.dat` 优先 → 常见视频后缀优先 → 更大的文件优先」排序；`.dat` 不是图片格式时解码失败是正常情况，会保留原始字节。视频上限是独立的 **100 MiB**，不复用 `gateway.wechat.media.max_bytes`（默认 10 MiB）；超限或找不到文件时保留 `[视频]` 占位。
  - **视频物化**：原文件复制到 `<workspace.dir>/videos/<时间戳>-<hash>.<ext>`（保留原始字节；复制失败时退化为只把源路径写进正文）。`annotate` 为真时用 ffmpeg 的 `ffprobe`（没有就解析 `ffmpeg -i` 的 `Duration:`）读时长，在 10% / 35% / 60% / 85% 处各抽 1 帧，写出 `<workspace.dir>/images/video-<hash>-<i>.jpg`，最多 4 张。正文注记为 `[视频]（已抽帧 images/...、...，可用 read 查看；原文件 videos/...）`；没有 ffmpeg 或抽帧失败时写 `[视频]（原文件 videos/...，无法抽帧）`；原文件都读不到时保持 `[视频]`。`ffmpeg` 定位与表情共用规则：优先 `DAFEIYU_FFMPEG`，否则查 `PATH`。
  - **`.dat` 解码**：支持旧的单字节 XOR（`0x37/0x38/0x39`）与 4.x 的 V2 容器（`07 08 56 32 08 07` + `aes_size`/`xor_size` + `[AES-128-ECB 头][明文 middle][XOR 尾]`）。V2 的 AES key 由 `cfgDword` 派生（`md5(str(cfgDword) + wxid)[:16]`，`xor_key = cfgDword & 0xFF`）：`media/keygen.py` 先从 V2 文件尾反推 XOR key，再只读扫描 `Weixin.exe` 内存里低字节相同的对齐 u32，用强图片头 + 完整解码双重校验；结果缓存在 `data/wechat-bridge/localdb/media_key.json`，微信换 key 后缓存会自动失效重扫。服务启动时后台预热，单条消息未命中时把扫描丢到后台线程，事件环从不做内存扫描。扫描失败只保留 `[图片]` 占位。V2 容器解出的明文若是微信私有 `wxgf`（原图），不直接落盘，而是交给 `emoticon.wxgf_to_image` 把其中的 HEVC 转成 PNG（`asyncio.to_thread`，事件环不阻塞）；转码失败才退回 `_h` / `_t`。图片原图按**静图**处理（`still=True`）：帧数 `<= 2` 的 `wxgf` 只输出体积最大的单帧 PNG——实测静态照片的容器常是 2 帧、首帧几乎纯白（`YAVG=235`、总时长 0.06s），按动图抽帧只会抽到白帧。
  - **贴纸缓存解码**：type 47 的贴纸不在 `msg/attach` 里，而是按消息 XML 的 `<emoji md5="...">` 到 `<storage_root>/cache/<YYYY-MM>/Emoticon/<md5[:2]>/<md5>` 找。文件是裸 AES-128-CBC（`IV == key`、整文件单流、PKCS7、无文件头），key 由同一个 `cfgDword` 派生：`md5(f"{cfgDword}{wxid}EMOTICON").digest()[:16]`——注意与图片 `.dat` 的 ASCII-hex key 不同。明文是 `wxgf` 容器：固定 13 字节头（魔数/版本/宽高/帧数）之后还有一段长度随版本变化的附加数据（v0x12 为 9 字节，v0x13/v0x14 可达 1KB 且含假起始码），真正的 HEVC 流从第一个参数集 NAL（`00 00 00 01 40 01`）开始；由外部 `ffmpeg` 转成 PNG（多帧转 GIF）。`ffmpeg` 优先取环境变量 `DAFEIYU_FFMPEG`，否则查 `PATH`；缺失时保留 `[表情]` 占位，不影响入站。转码在 `asyncio.to_thread` 里执行，事件环不阻塞。少量缓存文件解密后**直接就是 PNG/JPEG/GIF**（非 `wxgf`），这类载荷原样使用、不再过 ffmpeg；密钥迟到时只要消息带附件就会登记重试，纯表情消息同样能补上图片。缓存未命中时退回消息 XML 的 `cdnurl`（明文 http，实测可直接 GET；只接受 `*.tc.qq.com` / `*.qpic.cn`），由物化层下载成图片；`desc` / `attachedtext` 若是 base64 protobuf（商店表情），会解出其中的中文名而不是按前 20 字符截断成乱码；缓存与 `cdnurl` 都拿不到时，正文追加 `（未缓存）`，避免模型对着 md5/商店 id 编造画面。
  - **无新增依赖**：AES 是仓库内纯 Python 实现（`media/aes.py`），不需要 `cryptography`/`Crypto`。
  - **视频号分享**：appmsg `type 51` 的 `<title>` 是「当前版本不支持展示该内容，请升级到最新版本。」这类模板文案，真正内容在 `<finderFeed>`（`nickname` / `desc` / `mediaCount` / `mediaList/media/videoPlayDuration`）。渲染成一行 `[视频号] 昵称：描述（视频时长 N 秒）`（图集用「共 N 张图」），`kind` 仍是 `link`。`<media><url>` 是带 token 的临时直链、封面也只有 200px 级，因此**不下载**视频本体和封面；XML 解析失败时用正则兜底取昵称/描述。
- 入站图片降采样（QQ / 微信入站共通，`imaging.py`）：落盘前用 ffprobe 探测最长边，超过 `images.max_edge`（默认 **1280**）时用 ffmpeg 等比缩小并更新 `Attachment.size`；`0` = 不降采样。JPEG 二次编码显式用 `-q:v 3`（mjpeg 默认量化偏保守，1280px 也会肉眼发糊），PNG/GIF 走各自默认参数。GIF 不处理（`-frames:v 1` 会把动画压成单帧，保留动画）；缺 ffmpeg/ffprobe、探测失败或 ffmpeg 出错时保留原图，不影响消息本身。是否抽/缩在行内静默降级，不产生额外错误。
- GIF 入站图片的抽帧会先**本地数帧**（解析 GIF 块结构，不依赖 ffmpeg）：`<= 2` 帧按静图处理，只在候选帧里保留体积最大的一张，注记写 `[图片]（图存 images/...，可用 read 查看；原文件 ...）` 而不是「动图」；`> 2` 帧仍按 `0/8/24` 抽帧并注记「动图，已抽帧」。帧数解析失败（坏数据）沿用旧策略。
- 微信/QQ 共用的图片目录按 `images.retention_days` 惰性清理：`> 0` 时在每次物化前删掉超期文件，`0` = 不清理。
- 微信文件：只追加一条 `[文件] 名字 (路径)` 注记，不复制、不读取内容。
- **微信合并转发**：appmsg `type 19` 正文渲染为 `[合并转发] <title>`（无标题时仅 `[合并转发]`），不展开聊天记录；`kind` 仍是 `link`，XML 解析失败也不抛异常。
- **语音转写**：`speech.enabled: true` 时，语音先落盘到 `<workspace.dir>/voices/` 再转写。SILK（QQ `record`）由 `pysilk` 解码成裸 PCM 再交给 ffmpeg；其它格式直接交给 ffmpeg；最后进本地 faster-whisper（默认 `base`、`int8`、`vad_filter=True`，可用 `initial_prompt` 引导简体）。转写失败只保留 `[语音]`，不影响同一条消息的其它段。`speech.enabled: false` 时完全不处理：不下载、不落盘、不改正文。
- 视频不完整转码，只复制原文件并按固定百分比抽帧（见上）。转写依赖是可选的 `.[stt]` extra（faster-whisper + silk-python）；不装也能启动，只是语音保持占位。

### 相关配置键

- `gateway.wechat.media.enabled`：微信入站图片/视频落盘总开关，默认 `false`。
- `gateway.wechat.media.max_bytes`：解密后单张图片上限，默认 `10485760`（10 MiB）；视频不走这个键，固定上限 100 MiB。
- `gateway.wechat.media.storage_root`：微信本地媒体根目录；空 = 自动探测。
- `gateway.wechat.enabled`：微信通道总开关，默认 `false`。
- `images.enabled`：QQ 图片落盘开关，同时门控 QQ 语音/视频的 URL 下载，默认 `true`。
- `images.max_edge`：入站图片最长边上限，默认 `1280`；超过则用 ffmpeg 等比缩小，`0` = 不降采样，GIF 不处理（保动画），缺 ffmpeg 时保留原图。
- `speech.enabled`：语音转写总开关，默认 `false`；关闭时语音完全不处理。
- `speech.model`：faster-whisper 模型名或本地目录，默认 `base`。
- `speech.language` / `speech.device` / `speech.compute_type` / `speech.beam_size`：转写参数，默认 `zh` / `cpu` / `int8` / `1`。
- `speech.max_seconds`：超过这个时长的语音不转写（只记原文件路径），默认 `60`。
- `speech.timeout_seconds`：单条语音下载/解码/转写总超时，默认 `90`。
- `speech.max_bytes`：单条语音文件大小上限，默认 `20971520`（20 MiB）。
- `speech.initial_prompt`：引导模型输出简体中文的提示；留空 = 不传。
- `speech.hf_endpoint`：HuggingFace 镜像地址；国内直连 `huggingface.co` 会超时，建议填 `https://hf-mirror.com`。

## 跨群镜像

`mirror` 配置段（默认关闭）把**恰好一对**群——一个 QQ 群和一个微信群——的发言双向转发：人类消息渲染成 `昵称：内容`，机器人回复原样出现，媒体尽力真发、发不了给可读占位。本节只写镜像自身的行为；单一通道内的收发能力见上文「能力矩阵」，这里不重复。

镜像只搬运群消息：私聊与机器人自己的入站消息（`is_bot`）不参与。启用后启动时会按配置推导每个方向的能力矩阵打印到日志（`mirror capability qq->wechat …` / `mirror capability wechat->qq …`），只读配置、不做网络探测；能力不足只警告，不拒绝启动。

### 方向矩阵

整个机制是**尽力而为**：前置条件满足就真发，不满足就降级为可读占位，不报错、不中断原消息处理。

| kind | QQ → 微信 | 微信 → QQ |
| --- | --- | --- |
| text | 总是转发 | 总是转发 |
| image | `outbound: hook`（或 `auto` 且已配置 hook 地址）+ `images.enabled` + `workspace.enabled` 都满足时经 hook `send_media` 真发；否则 `[图片]` 占位 | `local_db_enabled` + `media.enabled` 都满足时按 QQ 图片段真发；否则 `[图片]` 占位 |
| voice / record | 在图片条件上**另需 `speech.enabled`**（QQ 语音的 URL 下载在语音管线内，关闭时不下载）；满足时 `send_media kind=voice` 真发，否则 `[语音]` 占位 | `local_db_enabled` + `media.enabled` 满足时按 QQ 语音段真发（文件来自微信本地库，不需要 `speech.enabled`）；否则 `[语音]` 占位 |
| video | 微信 hook 没有视频发送原语（`kind=video` 明确回 `unsupported_media`），**恒为 `[视频]` 占位**；URL 下载不受 `speech.enabled` 门控，但物化了也发不出去 | `local_db_enabled` + `media.enabled` 满足时按 QQ 视频段真发；否则 `[视频]` 占位 |
| file | 需要 `outbound: hook`；QQ 人类入站的文件消息本身没有文件内容（见上文矩阵「只记录元数据」），通常只有 `[文件 名字]` 说明行 | `local_db_enabled` + `media.enabled` 满足且解析出本机可读文件时走 `upload_file`；否则 `[文件 名字]` 占位 |
| sticker | 入站 `face`/`mface` 表情没有可发送的媒体内容，**恒为 `[表情]` 占位**；机器人主动发送的带路径贴纸附件会经 `send_media kind=sticker` 发送，被 hook 拒收时降级当图片再试一次 | 贴纸图片按图片链路处理（落盘后附件 `kind` 为 `image`）；发不出去时与图片一样只留占位说明行 |
| 合并转发 | 只发一行 `[合并转发 N 条]` 占位，不搬节点 | 同左（微信入站的合并转发本就只渲染 `[合并转发] <标题>`，见上文） |

微信侧媒体文件来自本地库发现的 `.dat`/缓存文件：**必须解码成功才会发送**；解不开（缺密钥、扫描失败、转码失败）就把附件路径清空、只发占位，绝不把原始 `.dat` 字节交给目标通道。

### 能力前置条件

- **QQ → 微信媒体需要 `gateway.wechat.outbound: hook`**（或 `auto` 且已配置 hook 地址）。**UIA 出站只可靠发送文本**：UIA 后端对所有媒体都明确返回 `unsupported_media`（`gateway/wechat_bridge/sender.py`，见上文「安全边界」），这种配置下镜像只会发占位文本。
- **微信 → QQ 媒体需要 `gateway.wechat.local_db_enabled: true` + `gateway.wechat.media.enabled: true`**；任一关闭，微信侧媒体只能是占位。
- QQ 入站的 URL 媒体物化需要 `images.enabled: true` + `workspace.enabled: true`（即 `workspace.dir` 非空）；**语音/record 的 URL 物化另需 `speech.enabled: true`**，关闭时只发 `[语音]` 占位；video 的 URL 下载不受 `speech.enabled` 门控。
- 微信 `.dat` 必须解码成功才发送，解不开只发占位（见上）。

### 占位规则

- 占位文本固定为：`[图片]`、`[语音]`、`[视频]`、`[文件 名字]`、`[表情]`、`[合并转发 N 条]`。
- 人类发言渲染为 `昵称：内容`（全角冒号）；机器人发言不带前缀、原样出现。
- 纯媒体消息（没有可转发的正文）只发一条占位说明行；有正文时占位附在正文后面。
- 长文本复用发送器的 `_split`（500 字、按句界）切分，不做 markdown 清洗。

### 不搬运的内容

- **@ 与引用（reply）段丢弃**：跨平台后 @ 关系不成立；`send_at` 触发的发送也按纯文本镜像。
- **合并转发只发占位**：不搬任何节点内容。
- **撤回不镜像**：一边撤回，另一边保留原样。
- **启动后不回填历史**：只镜像启用之后新发生的消息。

### 队列 / 顺序 / 去重

- 入队发生在 `Service.handle` 的**第一个 await 之前**（同步入队不可变快照），因此镜像顺序与消息到达顺序一致，「文字 + 图片」连发不会乱序；媒体下载只发生在队列 worker 里。
- 每方向一条独立队列（容量 **1000**），各一个 worker 串行发送；单方向突发 30 条文本的端到端延迟预算 ≤ 3 分钟。
- 积压超过 **5 分钟**（300 秒）的条目出队时直接丢弃（计入 `expired`），断桥恢复后不会补发一批过期消息。
- 入站按 `(chat_id, message_id)` 做 10 分钟内存窗口幂等；进程重启后桥重投的帧由 `message_exists` 查库兜住，**不会重复镜像**。
- 出站回显指纹是一次性、**10 秒** TTL，且只消费「发送者 = 本通道 bot 账号」的入站消息；人类在 10 秒内说了相同的话**不会**被吞。

### 存储隔离

镜像发出的消息在目标群按 `is_bot=1` + `mirror=1` 落库：

- 只进入微信桥的**回声种子**查询（`recent_bot_messages`），重启后回声仍自洽；
- **不进入提示词上下文**：`_history_before` 在 SQL 层排除 `mirror=1`；
- **不被当作「bot 的上一句」**：`_previous_message` 在 SQL 层排除（`include_mirror=False`）；`recent_bot_messages_in`（重复守卫用）同样排除。
- 已知口径：镜像行**会计入活跃度统计**（按 `messages` 表统计的消息量等数字包含镜像行），这是明确接受的取舍，不做排除。

### 文件与部署约束

- 镜像发送用的是**本机可读路径**：NapCat、微信桥、hook 服务必须与 bot 同机部署，不做跨机器文件传输。
- 附件路径按 `workspace.dir` 解析（相对路径不许带 `..`，`resolve()` 后不得逃出 workspace）；**`workspace.dir` 必须配置**，留空时路径解析一律失败、只发占位，不会回退到启动目录。
- 改 `mirror` 配置（或微信限速四项）后，必须**同时重启 bot 与微信桥**才生效；桥进程只在启动时读一次配置。

### 启用方式

引用 `config.example.yaml` 的 `mirror` 段（键名与注释以该文件为准）：

```yaml
mirror:
  enabled: false                # 改 true 启用
  pairs:
    - qq: "qq:123456789"        # QQ 群号，带 qq: 前缀
      wechat: "wxg:0d5c1f2a3b4c" # 微信会话别名（桥本地铸造），带 wxg: 前缀
```

- `pairs` 只接受**恰好一对**；`enabled: false` 时 `pairs` 被忽略，可先填好备用。
- `enabled: true` 时启动即校验，不满足直接报错拒绝启动：`gateway.provider: both`、`gateway.wechat.enabled: true`、微信四项已去节流（`send_min_interval_seconds` / `send_max_per_hour` / `send_max_per_day` 全 `0`，`quiet_hours` 起止相同）。
