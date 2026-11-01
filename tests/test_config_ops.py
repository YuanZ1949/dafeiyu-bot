from __future__ import annotations

from pathlib import Path

import pytest

from dafeiyu.config import ConfigError, load_settings

BASE = """\
persona:
  name: 溟月
  aliases: [大肥鱼]
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
  dir: logs
"""

ENV = "BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n"


def load(tmp_path: Path, extra_yaml: str = ""):
    config = tmp_path / "config.yaml"
    config.write_text(BASE + extra_yaml, encoding="utf-8")
    env_file = tmp_path / "test.env"
    env_file.write_text(ENV, encoding="utf-8")
    return load_settings(env_file=env_file, config_file=config)


def test_ops_config_defaults(tmp_path: Path) -> None:
    settings = load(tmp_path)

    assert settings.alerts.enabled is False
    assert settings.alerts.check_seconds == 30
    assert settings.alerts.repeat_seconds == 21600
    assert settings.alerts.webhook.url == ""
    assert settings.alerts.webhook.format == "generic"
    assert settings.alerts.qq_admin == 0
    assert settings.alerts.rules.bot_down_seconds == 120
    assert settings.alerts.rules.wechat_down_seconds == 180
    assert settings.alerts.rules.disk_free_percent == 10

    assert settings.backup.enabled is True
    assert settings.backup.dir == "data/backups"
    assert settings.backup.daily_keep == 7
    assert settings.backup.weekly_keep == 4
    assert settings.backup.schedule_hour == 4
    assert settings.backup.max_total_gb == 20
    assert settings.backup.max_upload_mb == 512

    assert settings.audit.path == "data/audit.jsonl"
    assert settings.audit.max_bytes == 10485760
    assert settings.audit.backups == 3


def test_ops_config_reads_sections(tmp_path: Path) -> None:
    settings = load(
        tmp_path,
        extra_yaml="""\
alerts:
  enabled: true
  check_seconds: 10
  repeat_seconds: 600
  webhook:
    url: https://example.invalid/hook
    format: ntfy
  qq_admin: 12345
  rules:
    bot_down_seconds: 30
    wechat_down_seconds: 90
    disk_free_percent: 5
backup:
  dir: data/custom-backups
  daily_keep: 3
audit:
  path: data/custom-audit.jsonl
  max_bytes: 2097152
""",
    )

    assert settings.alerts.enabled is True
    assert settings.alerts.webhook.format == "ntfy"
    assert settings.alerts.qq_admin == 12345
    assert settings.alerts.rules.bot_down_seconds == 30
    assert settings.alerts.rules.wechat_down_seconds == 90
    assert settings.backup.dir == "data/custom-backups"
    assert settings.backup.daily_keep == 3
    assert settings.audit.path == "data/custom-audit.jsonl"
    assert settings.audit.max_bytes == 2097152


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("alerts:\n  check_seconds: 2\n", "alerts.check_seconds"),
        ("alerts:\n  check_seconds: 3601\n", "alerts.check_seconds"),
        ("alerts:\n  repeat_seconds: 60\n", "alerts.repeat_seconds"),
        ("alerts:\n  webhook:\n    format: slack\n", "alerts.webhook.format"),
        ("alerts:\n  qq_admin: -1\n", "alerts.qq_admin"),
        ("alerts:\n  rules:\n    disk_free_percent: 0\n", "alerts.rules.disk_free_percent"),
        ("alerts:\n  rules:\n    reply_error_count: 0\n", "alerts.rules.reply_error_count"),
        ("alerts:\n  rules:\n    wechat_down_seconds: 0\n", "alerts.rules.wechat_down_seconds"),
        ("backup:\n  dir: ''\n", "backup.dir"),
        ("backup:\n  schedule_hour: 24\n", "backup.schedule_hour"),
        ("backup:\n  daily_keep: -1\n", "backup.daily_keep"),
        ("backup:\n  max_total_gb: 0\n", "backup.max_total_gb"),
        ("backup:\n  max_upload_mb: 0\n", "backup.max_upload_mb"),
        ("audit:\n  path: ''\n", "audit.path"),
        ("audit:\n  max_bytes: 1024\n", "audit.max_bytes"),
        ("audit:\n  backups: 0\n", "audit.backups"),
    ],
)
def test_ops_config_rejects_bad_values(
    tmp_path: Path, extra: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, extra_yaml=extra)
