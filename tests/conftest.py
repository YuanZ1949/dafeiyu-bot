"""测试共用配置：控制口与 dashboard 的端到端用例必须跑同一份 YAML。"""

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
