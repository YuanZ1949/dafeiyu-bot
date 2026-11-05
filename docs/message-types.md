# 消息类型能力矩阵

本文只描述**当前代码已经做到的事**。没做到的（语音转写、视频转码、微信 UI 剪贴板发图等）在矩阵里明确写「不支持」，不要按「以后会有」理解。

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
| 语音 | 占位（正文 `[语音]`；只记录来源/时长元数据，不转写） | 占位（正文 `[语音]`；不转写） | 支持（`send_segments` 的 record 段，kind 为 `record`/`voice`） | 支持（hook 后端 `send_media` kind=voice；UIA 不支持） |
| 视频 | 占位（正文 `[视频]`；只记录来源/时长元数据，不转码） | 占位（正文 `[视频]`；不转码） | 支持（`send_segments` 的 video 段，路径同样受 `replies/` 与后缀白名单约束） | 支持（hook 后端 `send_media` kind=video；UIA 不支持） |
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

### 入站媒体（群 → 本机）

- QQ 图片：下载/复制到 `<workspace.dir>/images/`，文件名由时间戳 + 来源 md5 生成，不采信 URL 路径段；下载失败、超过大小上限、没有可用来源都保留 `[图片]` 占位。
- 微信图片：只有 `gateway.wechat.media.enabled: true` 才会读取本地库发现的文件；路径必须 `resolve()` 到 `storage_root`（空 = 自动探测的镜像根目录）内，文件必须存在且大小在 `(0, max_bytes]`；`.dat` 解密失败保留占位。落盘位置同样是 `<workspace.dir>/images/`。type 47 贴纸同走这条管线（消息 `kind` 仍是 `sticker`，附件 `kind` 为 `image`），落盘后正文变成 `[表情:<标识>]（图存 images/...，可用 read 查看）`。
  - **媒体发现**：4.x 的 `message/message_resource.db` 没有路径列，md5 在 `MessageResourceInfo.packed_info` 里；按 `msg/attach/<md5(会话)>/<YYYY-MM>/Img/<md5>.dat` 组合候选路径，以磁盘上真实存在为准。变体优先级 `_h` > `_t` > 原图：微信 4.x 的原图解密后常是私有 `wxgf` 容器，`read` 打不开，所以宁可用可读的 `_h`/`_t`。
  - **`.dat` 解码**：支持旧的单字节 XOR（`0x37/0x38/0x39`）与 4.x 的 V2 容器（`07 08 56 32 08 07` + `aes_size`/`xor_size` + `[AES-128-ECB 头][明文 middle][XOR 尾]`）。V2 的 AES key 由 `cfgDword` 派生（`md5(str(cfgDword) + wxid)[:16]`，`xor_key = cfgDword & 0xFF`）：`media/keygen.py` 先从 V2 文件尾反推 XOR key，再只读扫描 `Weixin.exe` 内存里低字节相同的对齐 u32，用强图片头 + 完整解码双重校验；结果缓存在 `data/wechat-bridge/localdb/media_key.json`，微信换 key 后缓存会自动失效重扫。服务启动时后台预热，单条消息未命中时把扫描丢到后台线程，事件环从不做内存扫描。扫描失败只保留 `[图片]` 占位。
  - **贴纸缓存解码**：type 47 的贴纸不在 `msg/attach` 里，而是按消息 XML 的 `<emoji md5="...">` 到 `<storage_root>/cache/<YYYY-MM>/Emoticon/<md5[:2]>/<md5>` 找。文件是裸 AES-128-CBC（`IV == key`、整文件单流、PKCS7、无文件头），key 由同一个 `cfgDword` 派生：`md5(f"{cfgDword}{wxid}EMOTICON").digest()[:16]`——注意与图片 `.dat` 的 ASCII-hex key 不同。明文是 `wxgf` 容器，剥掉 22 字节头后是裸 HEVC；由外部 `ffmpeg` 转成 PNG（多帧转 GIF）。`ffmpeg` 优先取环境变量 `DAFEIYU_FFMPEG`，否则查 `PATH`；缺失时保留 `[表情]` 占位，不影响入站。转码在 `asyncio.to_thread` 里执行，事件环不阻塞。
  - **无新增依赖**：AES 是仓库内纯 Python 实现（`media/aes.py`），不需要 `cryptography`/`Crypto`。
- 微信/QQ 共用的图片目录按 `images.retention_days` 惰性清理：`> 0` 时在每次物化前删掉超期文件，`0` = 不清理。
- 微信文件：只追加一条 `[文件] 名字 (路径)` 注记，不复制、不读取内容。
- 语音不转写、视频不转码；不新增第三方依赖。

### 相关配置键

- `gateway.wechat.media.enabled`：微信入站图片落盘总开关，默认 `false`。
- `gateway.wechat.media.max_bytes`：解密后单张图片上限，默认 `10485760`（10 MiB）。
- `gateway.wechat.media.storage_root`：微信本地媒体根目录；空 = 自动探测。
- `gateway.wechat.enabled`：微信通道总开关，默认 `false`。
