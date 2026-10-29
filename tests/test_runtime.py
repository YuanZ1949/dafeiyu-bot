from pathlib import Path

from dafeiyu.runtime import RuntimeState, load_runtime, save_runtime


def test_missing_file_returns_empty_state(tmp_path: Path) -> None:
    state = load_runtime(tmp_path / "runtime.json")
    assert state == RuntimeState(provider="", local_model_model="")


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "runtime.json"
    save_runtime(RuntimeState(provider="local_model", local_model_model="qwen"), path)
    loaded = load_runtime(path)
    assert loaded.provider == "local_model"
    assert loaded.local_model_model == "qwen"
    assert path.exists()


def test_corrupt_file_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_runtime(path) == RuntimeState()


def test_unknown_provider_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text('{"provider": "gpt"}', encoding="utf-8")
    assert load_runtime(path).provider == ""


def test_local_alias_is_normalized(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text('{"provider": "local"}', encoding="utf-8")
    assert load_runtime(path).provider == "token"


def test_with_provider_keeps_model_when_leaving_local(tmp_path: Path) -> None:
    state = RuntimeState(provider="local_model", local_model_model="qwen")
    assert state.with_provider("token").local_model_model == "qwen"
    assert state.with_provider("local_model", "").local_model_model == "qwen"
    assert state.with_provider("local_model", "llama").local_model_model == "llama"


def test_non_object_payload_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text('["provider"]', encoding="utf-8")
    assert load_runtime(path) == RuntimeState()


def test_free_available_is_never_persisted(tmp_path: Path) -> None:
    """自检结果只在内存里：落盘一个陈旧的 True 就是重启后的谎话。"""
    path = tmp_path / "runtime.json"
    save_runtime(
        RuntimeState(provider="opencode", local_model_model="qwen", free_available=False),
        path,
    )

    text = path.read_text(encoding="utf-8")

    assert "free_available" not in text
