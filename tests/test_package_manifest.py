from dafeiyu.package_manifest import iter_payload


def test_payload_includes_runtime_essentials(tmp_path):
    for rel in ["src/dafeiyu/__init__.py", ".opencode/agents/deepseek-qq.md",
                ".env.example", "config.example.yaml", "pyproject.toml"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    names = {p.as_posix() for p in iter_payload(tmp_path)}
    assert ".opencode/agents/deepseek-qq.md" in names
    assert ".env.example" in names


def test_payload_excludes_secrets_and_heavy_dirs(tmp_path):
    for rel in ["data/dafeiyu.db", "vendor/napcat/node.exe", "config.yaml", ".env",
                "tests/test_x.py", "docs/superpowers/specs/a.md", "src/a.py"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    names = {p.as_posix() for p in iter_payload(tmp_path)}
    assert names == {"src/a.py"}
