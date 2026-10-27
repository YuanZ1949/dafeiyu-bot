import pytest

from dafeiyu.brain import opencode
from dafeiyu.brain.client import BrainError


def test_vendor_candidate_wins_over_path(tmp_path, monkeypatch):
    root = tmp_path
    exe = root / "vendor" / "opencode" / "opencode.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")
    monkeypatch.setattr(opencode, "INSTALL_ROOT", root)
    monkeypatch.setattr(opencode.shutil, "which", lambda name: None)
    assert opencode.resolve_opencode_bin("") == str(exe)


def test_missing_everything_raises_brainerror(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode, "INSTALL_ROOT", tmp_path)
    monkeypatch.setattr(opencode.shutil, "which", lambda name: None)
    monkeypatch.delenv("APPDATA", raising=False)
    with pytest.raises(BrainError):
        opencode.resolve_opencode_bin("")
