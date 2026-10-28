from __future__ import annotations

import pytest

from dafeiyu.settings_local import (
    OverrideError,
    normalize_mode,
    read_overrides,
    write_overrides,
)


def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "settings.local.yaml"
    write_overrides({"brain": {"mode": "key", "key": {"model": "deepseek-chat"}}}, p)
    assert read_overrides(p)["brain"]["mode"] == "key"


def test_read_missing_returns_empty(tmp_path):
    assert read_overrides(tmp_path / "nope.yaml") == {}


@pytest.mark.parametrize("bad", ["local", "", "FREE!"])
def test_normalize_mode_rejects_unknown(bad):
    with pytest.raises(OverrideError):
        normalize_mode(bad)


def test_normalize_mode_accepts_free_key_local_model():
    assert normalize_mode(" Local_Model ") == "local_model"


def test_write_creates_missing_parent_directory(tmp_path):
    p = tmp_path / "data" / "settings.local.yaml"

    written = write_overrides({"brain": {"mode": "free"}}, p)

    assert p.exists()
    assert written == p.resolve()
    assert read_overrides(p) == {"brain": {"mode": "free"}}


def test_read_malformed_yaml_raises_override_error(tmp_path):
    p = tmp_path / "settings.local.yaml"
    p.write_text("brain: [unclosed\n", encoding="utf-8")

    with pytest.raises(OverrideError) as excinfo:
        read_overrides(p)

    assert "YAML" in str(excinfo.value)


def test_read_blank_file_ok_but_non_mapping_rejected(tmp_path):
    p = tmp_path / "settings.local.yaml"

    p.write_text("# 只有注释\n\n", encoding="utf-8")
    assert read_overrides(p) == {}

    p.write_text("free\n", encoding="utf-8")
    with pytest.raises(OverrideError):
        read_overrides(p)

    p.write_text("- free\n- key\n", encoding="utf-8")
    with pytest.raises(OverrideError):
        read_overrides(p)
