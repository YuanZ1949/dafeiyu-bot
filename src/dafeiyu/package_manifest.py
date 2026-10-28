"""发布包白名单：决定哪些文件进 zip，哪些永远不进。

发布包不含 ``config.yaml`` / ``.env`` / ``data/``（用户机密与运行数据），
也不含 ``vendor/``、``tests/``、``docs/`` 这类安装期下载或开发期才需要的东西。
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

# 白名单：只有这些顶层项（及其内容）可能进包。顺序仅影响遍历，输出会排序。
INCLUDE: tuple[str, ...] = (
    "src",
    "scripts",
    ".opencode",
    "config.example.yaml",
    ".env.example",
    ".gitignore",
    "pyproject.toml",
    "README.md",
    "LICENSE",
)

# 目录名命中即整棵子树跳过（不区分层级，嵌套的 __pycache__ 也算）。
EXCLUDE_DIRS: tuple[str, ...] = (
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "data",
    "logs",
    "vendor",
    "tests",
    "docs",
)

# 就算出现在白名单目录里也绝不打包的文件名。
EXCLUDE_FILES: tuple[str, ...] = ("config.yaml", ".env")


def _is_payload(rel: Path) -> bool:
    """相对路径是否可以进包：任一目录段命中黑名单，或文件名是机密文件。"""
    if rel.name in EXCLUDE_FILES:
        return False
    return not any(part in EXCLUDE_DIRS for part in rel.parts[:-1])


def iter_payload(root: Path) -> list[Path]:
    """返回相对 ``root`` 的待打包文件列表（已过滤黑名单，按 posix 序排序）。"""
    root = Path(root)
    payload: list[Path] = []
    for item in INCLUDE:
        base = root / item
        if not base.exists():
            # LICENSE 之类可能不存在：白名单缺项是正常的，静默跳过。
            continue
        if base.is_dir():
            for path in base.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(root)
                if _is_payload(rel):
                    payload.append(rel)
        elif _is_payload(Path(item)):
            payload.append(Path(item))
    return sorted(payload, key=lambda p: p.as_posix())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dafeiyu.package_manifest",
        description="按白名单把仓库打成发布 zip（排除 data/vendor/tests 与机密文件）。",
    )
    parser.add_argument("--root", default=".", help="仓库根目录（默认当前目录）")
    parser.add_argument("--out", required=True, help="输出 zip 路径")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"根目录不存在或不是目录：{root}", file=sys.stderr)
        return 2

    payload = iter_payload(root)
    if not payload:
        print(f"白名单在 {root} 下没有匹配到任何文件", file=sys.stderr)
        return 1

    out = Path(args.out)
    if out.is_dir():
        print(f"--out 指向的是目录，需要文件路径：{out}", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in payload:
            zf.write(root / rel, rel.as_posix())

    print(f"已打包 {len(payload)} 个文件，{out.stat().st_size} 字节 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
