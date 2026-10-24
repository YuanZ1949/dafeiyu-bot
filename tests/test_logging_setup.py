from __future__ import annotations

import logging
from pathlib import Path

from dafeiyu.__main__ import setup_logging
from dafeiyu.config import load_settings

BASE = """\
persona:
  name: 大肥鱼
  aliases: [肥鱼]
  max_reply_chars: 120
  max_reply_chars_technical: 300
  os_probability: 0.05
triggers:
  cooldown_seconds: 60
  sample_rate: 0.05
  quiet_hours: ["01:00", "07:00"]
  keywords: [怎么]
groups:
  whitelist: []
  blacklist: []
budget:
  daily_calls: 200
  daily_tokens: 300000
memory:
  window_size: 20
  impression_topk: 5
  extract_every: 20
logging:
  level: INFO
  dir: {log_dir}
"""


def test_httpx_is_quiet_but_app_loggers_stay_info(
    tmp_path: Path, monkeypatch
) -> None:
    """一条消息约 10 次 HTTP 调用，httpx 的 INFO 会把 bot.log 淹掉。"""
    config = tmp_path / "config.yaml"
    config.write_text(BASE.format(log_dir=tmp_path / "logs"), encoding="utf-8")
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8"
    )
    settings = load_settings(env_file=env_file, config_file=config)

    # 不真装 handler：只要断言配置意图，别在测试里留下打开的日志文件句柄。
    captured: dict = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: captured.update(kw))

    saved = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        setup_logging(settings)
        assert captured["level"] == logging.INFO
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        # 自己的日志不能被动过：只把 httpx/httpcore 降级，应用 logger 保持继承
        assert logging.getLogger("dafeiyu.service").level == logging.NOTSET
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
