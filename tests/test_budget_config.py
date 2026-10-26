"""预算配置的默认值、归一化与校验。"""

from __future__ import annotations

import pytest
import yaml
from conftest import TEST_CONFIG_YAML

from dafeiyu.config import ConfigError, load_settings


def write(tmp_path, budget: dict):
    cfg = yaml.safe_load(TEST_CONFIG_YAML)
    cfg["budget"] = budget
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    return load_settings(env_file=env, config_file=path)


def test_budget_defaults(tmp_path):
    s = write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000})
    b = s.budget
    assert (b.global_daily_calls, b.monthly_calls, b.monthly_cost_limit) == (0, 0, 0.0)
    assert (b.price_input_per_million, b.price_output_per_million) == (0.0, 0.0)
    assert b.warning_ratios == (0.5, 0.8, 1.0)
    assert b.on_exceed == "degrade"
    assert b.currency == "CNY"


def test_budget_values_are_loaded(tmp_path):
    s = write(
        tmp_path,
        {
            "daily_calls": 10,
            "daily_tokens": 1000,
            "global_daily_calls": 100,
            "global_daily_tokens": 20000,
            "monthly_calls": 300,
            "monthly_tokens": 40000,
            "monthly_cost_limit": 5.5,
            "currency": " USD ",
            "price_input_per_million": 1.0,
            "price_output_per_million": 2.0,
            "warning_ratios": [0.9],
            "on_exceed": " Stop ",
        },
    )
    b = s.budget
    assert (b.global_daily_calls, b.global_daily_tokens) == (100, 20000)
    assert (b.monthly_calls, b.monthly_tokens) == (300, 40000)
    assert b.monthly_cost_limit == 5.5
    assert b.currency == "USD"
    assert (b.price_input_per_million, b.price_output_per_million) == (1.0, 2.0)
    assert b.warning_ratios == (0.9,)
    assert b.on_exceed == "stop"


def test_warning_ratios_sorted_unique(tmp_path):
    s = write(
        tmp_path,
        {"daily_calls": 10, "daily_tokens": 1000, "warning_ratios": [1.0, 0.5, 0.8, 0.5]},
    )
    assert s.budget.warning_ratios == (0.5, 0.8, 1.0)


@pytest.mark.parametrize("ratio", [0, 1.1, -0.1])
def test_warning_ratio_out_of_range(tmp_path, ratio):
    with pytest.raises(ConfigError):
        write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000, "warning_ratios": [ratio]})


def test_on_exceed_enum(tmp_path):
    with pytest.raises(ConfigError):
        write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000, "on_exceed": "block"})


def test_cost_limit_requires_price(tmp_path):
    with pytest.raises(ConfigError):
        write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000, "monthly_cost_limit": 10})


def test_negative_budget_values_rejected(tmp_path):
    with pytest.raises(ConfigError):
        write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000, "monthly_calls": -1})


def test_daily_limits_must_be_positive(tmp_path):
    with pytest.raises(ConfigError):
        write(tmp_path, {"daily_calls": 0, "daily_tokens": 1000})


def test_warning_ratios_cannot_be_empty(tmp_path):
    with pytest.raises(ConfigError):
        write(tmp_path, {"daily_calls": 10, "daily_tokens": 1000, "warning_ratios": []})
