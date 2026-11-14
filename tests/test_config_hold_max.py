"""persona.hold_max_seconds：攒着寿命上限的默认值、显式值与非法值。"""

from __future__ import annotations

import pytest

from dafeiyu.config import ConfigError, _build_persona


def test_hold_max_seconds_defaults_to_300() -> None:
    persona = _build_persona({"aliases": ["大肥鱼"]})
    assert persona.hold_max_seconds == 300


def test_hold_max_seconds_accepts_zero() -> None:
    persona = _build_persona({"aliases": ["大肥鱼"], "hold_max_seconds": 0})
    assert persona.hold_max_seconds == 0


def test_hold_max_seconds_honours_explicit_value() -> None:
    persona = _build_persona({"aliases": ["大肥鱼"], "hold_max_seconds": 120})
    assert persona.hold_max_seconds == 120


def test_hold_max_seconds_rejects_negative_without_echoing_value() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _build_persona({"aliases": ["大肥鱼"], "hold_max_seconds": -1})

    message = str(excinfo.value)
    assert "persona.hold_max_seconds" in message
    assert "-1" not in message
