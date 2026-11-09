"""测试共用配置：控制口与 dashboard 的端到端用例必须跑同一份 YAML。"""

from __future__ import annotations

import pytest


# 本机覆盖层（data/settings.local.yaml）是**部署状态**，不属于测试输入：
# 面板一旦写过 groups.whitelist / brain.mode，所有临时配置都会被盖上，
# 让「同一份 YAML 得到同一结果」的用例随开发机状态漂移。默认指向一个
# 不存在的路径；需要覆盖层语义的用例自行 monkeypatch 同一个属性。
@pytest.fixture(autouse=True)
def _isolate_local_overrides(monkeypatch, tmp_path_factory):
    from dafeiyu import config as config_module

    missing = tmp_path_factory.mktemp("no-overrides") / "settings.local.yaml"
    monkeypatch.setattr(config_module, "OVERRIDES_PATH", missing)


# `build_specs()` 要求 `vendor/napcat/node.exe` 存在，而 vendor 目录是**本机安装状态**
# （.gitignore 里，CI 干净 workspace 一定没有）。不隔离的话，同一份测试在开发机绿、
# 在 CI 红 14 个。需要「没装 NapCat」语义的用例自行 monkeypatch NAPCAT_DIR 覆盖。
@pytest.fixture
def fake_napcat(tmp_path, monkeypatch):
    from dafeiyu.supervisor import main as supervisor_main

    node_dir = tmp_path / "fake-napcat"
    node_dir.mkdir()
    (node_dir / "node.exe").write_bytes(b"")
    (node_dir / "index.js").write_text("// stub\n", encoding="utf-8")
    monkeypatch.setattr(supervisor_main, "NAPCAT_DIR", node_dir)
    return node_dir


TEST_CONFIG_YAML = """\
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
brain:
  provider: token
local_model:
  base_url: http://127.0.0.1:1/v1
  model: ""
  api_key: local
"""

# 面板默认绑定 0.0.0.0（会强制登录）。功能类用例不该被鉴权挡住，所以把它们
# 钉在回环；鉴权策略本身由 test_dashboard_auth_config 覆盖。
TEST_DASHBOARD_LOOPBACK_YAML = """\
supervisor:
  dashboard:
    host: 127.0.0.1
"""
