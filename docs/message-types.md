# 消息类型能力矩阵

本文只描述**当前代码已经做到的事**。没做到的（视频完整转码（只抽帧看画面，见「入站媒体」）、微信 UI 剪贴板发图等）在矩阵里明确写「不支持」，不要按「以后会有」理解。

图例：

- **支持**：正文/发送链路能正确处理，失败也会给出明确降级。
- **只记录元数据**：正文只保留占位或摘要，`Attachment` 里可能有类型/名字/大小/时长，但不读文件内容。
- **占位**：只保证消息不丢、正文有可读标记；媒体本身不获取、不落盘。
- **不支持**：该方向没有实现，或实现会明确返回错误并回落。

## 能力矩阵

| 能力 | QQ 入站 | 微信入站 | QQ 出站 | 微信出站 |
| --- | --- | --- | --- | --- |
| 文本 | 支持 | 支持 | 支持 | 支持（`send_text`） |
| 图片 | 支持（`images.enabled` 时落盘并把路径写进正文；失败保留 `[图片]`） | 支持（`gateway.wechat.media.enabled` 时解密落盘给路径；否则占位） | 支持（`send_segments` 的 image 段） | 支持（hook 后端经 `send_media`；UIA 后端恒 `unsupported_media`，回落文本） |
| 语音 | 支持（`speech.enabled` 且 URL 下载由 `images.enabled` 门控时落盘 `voices/` 并转写，正文变 `[语音]（转写：…）`；依赖缺失、超时、解码失败保留 `[语音]` 占位） | 支持（`speech.enabled` 与 `gateway.wechat.media.enabled` 都打开时读取本地文件落盘并转写；否则占位） | 支持（`send_segments` 的 record 段，kind 为 `record`/`voice`） | 支持（hook 后端 `send_media` kind=voice；UIA 不支持） |
| 视频 | 支持（`images.enabled` 时下载 URL 原文件到 `videos/`，`annotate` 为真时用 ffmpeg 抽 4 帧到 `images/` 供 `read` 查看；失败保留 `[视频]`） | 支持（`gateway.wechat.media.enabled` 时复制原文件到 `videos/`，并用 ffmpeg 抽 4 帧到 `images/` 供 `read` 查看；失败保留 `[视频]`） | 支持（`send_segments` 的 video 段，路径同样受 `replies/` 与后缀白名单约束） | 支持（hook 后端 `send_media` kind=video；UIA 不支持） |
| 文件 | 只记录元数据（正文 `[文件] 名字`，记录名字/大小，不下载） | 占位（按 link/占位处理，不下载） | 支持（`upload_file`，模型附件 `kind=file`） | 支持（hook 后端 `send_media` kind=file；UIA 不支持） |
| 表情 / 贴纸 | 占位但带标识（`face` → `[表情:<id>]`，`mface` → `[表情:<摘要>]`，缺失时回退 `[表情]`；同时记录 sticker 名字/id） | 占位但带标识（`[表情:<desc>]`，无名字用 `[表情:<md5 前 8>]`，解析失败回退 `[表情]`）；贴纸图片在 `gateway.wechat.media.enabled` 时按图片落盘，模型可 `read` 查看 | 支持（模型附件 `type=sticker`：纯数字 `name` 出 `face` 段，其它文本出 `mface` 摘要段） | 支持（hook 后端 `send_media` kind=sticker；UIA 不支持） |
| @ | 支持（正文保留 `@名字`，并标记 `at_bot`） | 支持（正文保留原文，并标记 `at_bot`） | 支持（at 段；名字必须在本批次在场成员里，最多 3 个） | 支持（hook 后端 `send_at`；UIA 返回 `unsupported_at`，回落纯文本） |
| 合并转发 | 支持（递归渲染成「昵称: 内容」文本，深度/节点数有上限，超限占位） | 不支持（本地库没有对应 kind；appmsg 归到 quote/link） | 支持（模型附件 `type=forward`，`nodes` 最多 5 条、单条 200 字；失败降级为一行占位文本） | 不支持 |
| 戳一戳 / 进退群 / 禁言 | 支持解析（`NoticeEvent`，只记日志，不触发模型回复） | 不支持 | 不支持 | 不支持 |
| 卡片 / 链接 | 支持（json/xml 卡片渲染成文本摘要） | 支持（appmsg 的标题渲染为 link；引用消息渲染为 quote） | 不支持 | 不支持 |
| 位置 | 支持（渲染 `[位置] 标题` 摘要） | 占位（正文 `[位置]`） | 不支持 | 不支持 |

补充：

- 微信入站 `kind` 有白名单（text / image / voice / video / file / sticker / card / location / call / link / quote / record / forward）；未知值一律按 `text` 处理。
- QQ 入站未知消息段一律忽略，不影响同一条消息的其余部分。
- `Attachment` 与 `segments` 只在内存中流转，**不入库**；`messages.content` 仍是唯一持久载体。重启后只有正文里的占位/路径还在。

## 安全边界

### 出站附件（模型 → 群）

模型可以在回复 JSON 里带 `attachments: [{"type": "image"|"file"|"voice"|"video", "path": "replies/xxx.png"}]`，但路径必须同时满足：

- 只接受 `<workspace.dir>/replies/` 下的普通文件；路径先做形态检查（绝对路径 / 盘符 / `..` 一律拒绝），再 `resolve()` 后检查是否仍在 `replies/` 内（软链逃逸也会被挡住）；
- 后缀白名单：`.png` / `.jpg` / `.jpeg` / `.gif` / `.webp` / `.mp4` / `.mp3` / `.wav` / `.pdf` / `.txt` / `.zip`（不区分大小写）；
- 文件必须非空且不超过 **10 MiB**；
- 工作区关闭、目标缺失、解析失败一律跳过并留 warning，绝不抛错，也不读取/发送模型给的任意路径。

QQ 侧：图片/语音/视频走 OneBot 消息段，文件走 `upload_file`。表情用 `{"type": "sticker", "name": "..."}`：`name` 为 1–6 位非 0 数字时出 `face` 段，其它文本出 `mface` 摘要段；只校验名字非空，不读文件、不受 `replies/` 白名单限制。微信侧：UIA 传输对任何媒体都返回 `unsupported_media`，客户端不重试、不回落，调用方继续发文本；不做剪贴板发图。

入站表情的正文格式统一为 `[表情:<标识>]`：QQ `face` 用数字 id、`mface` 用摘要文本；微信优先用 `<emoji desc/attachedtext>`，为空时用 md5 前 8 位。标识只是让模型能区分「哪个表情」，真正的语义仍要靠贴纸图片本身（见下）。

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
  - **媒体发现**：4.x 的 `message/message_resource.db` 没有路径列，md5 在 `MessageResourceInfo.packed_info` 里；图片按 `msg/attach/<md5(会话)>/<YYYY-MM>/Img/<md5>.dat` 组合候选路径，以磁盘上真实存在为准。变体优先级 `_h` > `_t` > 原图：微信 4.x 的原图解密后常是私有 `wxgf` 容器，`read` 打不开，所以宁可用可读的 `_h`/`_t`。
  - **视频发现**：`local_type == 43` 时按顺序找 `msg/attach/<md5(当前会话)>/<YYYY-MM>/{Video,video}/<md5>*` → 其它会话目录下的同名 `Video/video` → `msg/{video,Video}/**/<md5>*`。同一组内多个候选按「非 `.dat` 优先 → 常见视频后缀优先 → 更大的文件优先」排序；`.dat` 不是图片格式时解码失败是正常情况，会保留原始字节。视频上限是独立的 **100 MiB**，不复用 `gateway.wechat.media.max_bytes`（默认 10 MiB）；超限或找不到文件时保留 `[视频]` 占位。
  - **视频物化**：原文件复制到 `<workspace.dir>/videos/<时间戳>-<hash>.<ext>`（保留原始字节；复制失败时退化为只把源路径写进正文）。`annotate` 为真时用 ffmpeg 的 `ffprobe`（没有就解析 `ffmpeg -i` 的 `Duration:`）读时长，在 10% / 35% / 60% / 85% 处各抽 1 帧，写出 `<workspace.dir>/images/video-<hash>-<i>.jpg`，最多 4 张。正文注记为 `[视频]（已抽帧 images/...、...，可用 read 查看；原文件 videos/...）`；没有 ffmpeg 或抽帧失败时写 `[视频]（原文件 videos/...，无法抽帧）`；原文件都读不到时保持 `[视频]`。`ffmpeg` 定位与表情共用规则：优先 `DAFEIYU_FFMPEG`，否则查 `PATH`。
  - **`.dat` 解码**：支持旧的单字节 XOR（`0x37/0x38/0x39`）与 4.x 的 V2 容器（`07 08 56 32 08 07` + `aes_size`/`xor_size` + `[AES-128-ECB 头][明文 middle][XOR 尾]`）。V2 的 AES key 由 `cfgDword` 派生（`md5(str(cfgDword) + wxid)[:16]`，`xor_key = cfgDword & 0xFF`）：`media/keygen.py` 先从 V2 文件尾反推 XOR key，再只读扫描 `Weixin.exe` 内存里低字节相同的对齐 u32，用强图片头 + 完整解码双重校验；结果缓存在 `data/wechat-bridge/localdb/media_key.json`，微信换 key 后缓存会自动失效重扫。服务启动时后台预热，单条消息未命中时把扫描丢到后台线程，事件环从不做内存扫描。扫描失败只保留 `[图片]` 占位。
  - **贴纸缓存解码**：type 47 的贴纸不在 `msg/attach` 里，而是按消息 XML 的 `<emoji md5="...">` 到 `<storage_root>/cache/<YYYY-MM>/Emoticon/<md5[:2]>/<md5>` 找。文件是裸 AES-128-CBC（`IV == key`、整文件单流、PKCS7、无文件头），key 由同一个 `cfgDword` 派生：`md5(f"{cfgDword}{wxid}EMOTICON").digest()[:16]`——注意与图片 `.dat` 的 ASCII-hex key 不同。明文是 `wxgf` 容器：固定 13 字节头（魔数/版本/宽高/帧数）之后还有一段长度随版本变化的附加数据（v0x12 为 9 字节，v0x13/v0x14 可达 1KB 且含假起始码），真正的 HEVC 流从第一个参数集 NAL（`00 00 00 01 40 01`）开始；由外部 `ffmpeg` 转成 PNG（多帧转 GIF）。`ffmpeg` 优先取环境变量 `DAFEIYU_FFMPEG`，否则查 `PATH`；缺失时保留 `[表情]` 占位，不影响入站。转码在 `asyncio.to_thread` 里执行，事件环不阻塞。
  - **无新增依赖**：AES 是仓库内纯 Python 实现（`media/aes.py`），不需要 `cryptography`/`Crypto`。
- 微信/QQ 共用的图片目录按 `images.retention_days` 惰性清理：`> 0` 时在每次物化前删掉超期文件，`0` = 不清理。
- 微信文件：只追加一条 `[文件] 名字 (路径)` 注记，不复制、不读取内容。
- **语音转写**：`speech.enabled: true` 时，语音先落盘到 `<workspace.dir>/voices/` 再转写。SILK（QQ `record`）由 `pysilk` 解码成裸 PCM 再交给 ffmpeg；其它格式直接交给 ffmpeg；最后进本地 faster-whisper（默认 `base`、`int8`、`vad_filter=True`，可用 `initial_prompt` 引导简体）。转写失败只保留 `[语音]`，不影响同一条消息的其它段。`speech.enabled: false` 时完全不处理：不下载、不落盘、不改正文。
- 视频不完整转码，只复制原文件并按固定百分比抽帧（见上）。转写依赖是可选的 `.[stt]` extra（faster-whisper + silk-python）；不装也能启动，只是语音保持占位。

### 相关配置键

- `gateway.wechat.media.enabled`：微信入站图片/视频落盘总开关，默认 `false`。
- `gateway.wechat.media.max_bytes`：解密后单张图片上限，默认 `10485760`（10 MiB）；视频不走这个键，固定上限 100 MiB。
- `gateway.wechat.media.storage_root`：微信本地媒体根目录；空 = 自动探测。
- `gateway.wechat.enabled`：微信通道总开关，默认 `false`。
- `images.enabled`：QQ 图片落盘开关，同时门控 QQ 语音/视频的 URL 下载，默认 `true`。
- `speech.enabled`：语音转写总开关，默认 `false`；关闭时语音完全不处理。
- `speech.model`：faster-whisper 模型名或本地目录，默认 `base`。
- `speech.language` / `speech.device` / `speech.compute_type` / `speech.beam_size`：转写参数，默认 `zh` / `cpu` / `int8` / `1`。
- `speech.max_seconds`：超过这个时长的语音不转写（只记原文件路径），默认 `60`。
- `speech.timeout_seconds`：单条语音下载/解码/转写总超时，默认 `90`。
- `speech.max_bytes`：单条语音文件大小上限，默认 `20971520`（20 MiB）。
- `speech.initial_prompt`：引导模型输出简体中文的提示；留空 = 不传。
- `speech.hf_endpoint`：HuggingFace 镜像地址；国内直连 `huggingface.co` 会超时，建议填 `https://hf-mirror.com`。
