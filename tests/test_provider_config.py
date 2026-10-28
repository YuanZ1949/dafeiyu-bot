import json

import pytest

from dafeiyu.brain.provider_config import GENERATED_MARK, build_config, render_config
from dafeiyu.config import BrainConfig


def test_free_mode_config_shape():
    cfg = BrainConfig()
    out = build_config("free", cfg)
    assert out["model"] == "opencode/deepseek-v4.1-flash-free"
    assert out["plugins"] == []
    assert "providers" not in out


def test_render_starts_with_generated_mark():
    text = render_config(build_config("free", BrainConfig()))
    assert text.splitlines()[0] == GENERATED_MARK
    assert json.loads("\n".join(text.splitlines()[1:]))["plugins"] == []


@pytest.mark.parametrize("mode", ["key", "local_model", "bogus"])
def test_unimplemented_modes_raise(mode):
    with pytest.raises(ValueError, match=mode):
        build_config(mode, BrainConfig())
