"""模拟 NapCat 的 OneBot v11 客户端，验证「群消息 → bot → 群回复」整条链路。

用法（仓库根目录）：

    ./.venv/Scripts/python scripts/fake-napcat-probe.py --text "Deepseek娘 在吗"

它作为 OneBot 客户端连到 bot 的反向 WS 服务端（`NAPCAT_WS_PORT`，默认 8080），
发一条 `message_type=group` 事件，捕获 bot 下发的 `send_group_msg` 并打印文本，
同时按 OneBot 协议回 ack。加 `--out reply.json` 可把结果存成 UTF-8 JSON。

用途：QQ 尚未扫码登录（没有真 NapCat 连入）时，验收三种 provider 的群回复链路。
角色边界：只替代「NapCat ↔ QQ」这一跳；真实投递仍需扫码登录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from websockets.asyncio.client import connect  # noqa: E402

from dafeiyu.config import load_settings  # noqa: E402


def _text_of(message: object) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return "".join(
            str((seg.get("data") or {}).get("text", ""))
            for seg in message
            if isinstance(seg, dict) and seg.get("type") == "text"
        )
    return ""


async def probe(group_id: int, text: str, timeout: float) -> tuple[str | None, list[dict]]:
    settings = load_settings()
    url = f"ws://{settings.ws_host}:{settings.ws_port}/"
    headers = {"Authorization": f"Bearer {settings.ws_token}"} if settings.ws_token else {}
    event = {
        "time": int(time.time()),
        "self_id": settings.bot_qq,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": int(time.time() * 1000) % 2_000_000_000,
        "group_id": group_id,
        "user_id": 10001,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "font": 0,
        "sender": {"user_id": 10001, "nickname": "验收员", "card": "验收员", "role": "member"},
    }
    actions: list[dict] = []
    async with connect(url, additional_headers=headers, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps(event, ensure_ascii=False))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (left := deadline - loop.time()) > 0:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=left)
            except asyncio.TimeoutError:
                break
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(data, dict) or not data.get("action"):
                continue
            actions.append({"action": data["action"], "params": data.get("params") or {}})
            await ws.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": 900000 + len(actions)},
                        "echo": data.get("echo"),
                    }
                )
            )
            if data["action"] in ("send_group_msg", "send_private_msg"):
                return _text_of((data.get("params") or {}).get("message")), actions
    return None, actions


def main() -> int:
    try:  # Windows 控制台常是 GBK，表情符号不该把探针打断
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老 Python / 非 tty 场景
        pass
    parser = argparse.ArgumentParser(description="假 NapCat：验证群回复链路")
    parser.add_argument("--group", type=int, default=900000001, help="目标群号（假的即可）")
    parser.add_argument("--text", required=True, help="要发的群消息")
    parser.add_argument("--timeout", type=float, default=120.0, help="等待回复秒数")
    parser.add_argument("--tag", default="probe", help="输出文件名标记")
    parser.add_argument("--out", default="", help="把结果写成 UTF-8 JSON 的路径")
    args = parser.parse_args()

    reply, actions = asyncio.run(probe(args.group, args.text, args.timeout))
    result = {
        "tag": args.tag,
        "group_id": args.group,
        "text": args.text,
        "reply": reply,
        "actions": actions,
    }
    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if reply is None:
        print(f"[{args.tag}] NO REPLY within {args.timeout}s (actions={len(actions)})")
        return 1
    print(f"[{args.tag}] group={args.group} actions={len(actions)} reply_chars={len(reply)}")
    print(f"[{args.tag}] REPLY >>> {reply}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
