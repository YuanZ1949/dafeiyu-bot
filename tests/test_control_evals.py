"""控制口只读评测趋势：最近 10 条、坏文件跳过、目录缺失不 500。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import TEST_CONFIG_YAML

from dafeiyu import control as control_module
from dafeiyu.config import load_settings
from dafeiyu.control import BrainControl, create_app
from dafeiyu.memory.store import Store


class FakeService:
    def __init__(self) -> None:
        self.brain = None
        self.impressions = None


class FakeAdapter:
    connected = True


@asynccontextmanager
async def running(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def make_control(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(TEST_CONFIG_YAML, encoding="utf-8")
    env = tmp_path / "test.env"
    env.write_text("BOT_QQ=10001\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    settings = load_settings(env_file=env, config_file=config)
    store = Store(tmp_path / "test.db")
    store.init_schema()
    control = BrainControl(
        settings,
        store,
        FakeService(),
        FakeAdapter(),
        runtime_path=str(tmp_path / "runtime.json"),
    )
    return control, create_app(control)


def write_result(
    path: Path,
    *,
    generated_at: str,
    total: int,
    passed: int,
    failed: int,
    skipped: int,
    accuracy: float | None,
) -> None:
    payload = {
        "dataset": "data/evals/golden_replies.json",
        "mode": "offline",
        "model": "offline",
        "generated_at": generated_at,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "accuracy": accuracy,
        },
        "results": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


async def test_evals_returns_latest_ten_and_skips_bad_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = tmp_path / "results"
    results.mkdir()
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for index in range(12):
        generated_at = (start + timedelta(minutes=index)).isoformat()
        passed = index
        failed = 2
        skipped = 1
        write_result(
            results / f"run-{index:02d}.json",
            generated_at=generated_at,
            total=passed + failed + skipped,
            passed=passed,
            failed=failed,
            skipped=skipped,
            accuracy=passed / (passed + failed),
        )
    # 坏文件：非 JSON、0 字节、缺 generated_at、summary 形状不对。
    (results / "broken.json").write_text("{not json", encoding="utf-8")
    (results / "empty.json").write_text("", encoding="utf-8")
    write_result(
        results / "no-time.json",
        generated_at="",
        total=3,
        passed=1,
        failed=1,
        skipped=1,
        accuracy=0.5,
    )
    (results / "bad-summary.json").write_text(
        json.dumps(
            {"generated_at": "2026-11-01T00:00:00+00:00", "summary": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(control_module, "EVALS_RESULTS_DIR", results)

    _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/evals")
        data = await resp.json()

    assert resp.status == 200
    items = data["items"]
    assert len(items) == 10
    # 最新在前：11..02，坏文件与最旧的两条被挡在外面。
    assert [item["name"] for item in items] == [
        f"run-{index:02d}.json" for index in range(11, 1, -1)
    ]
    newest = items[0]
    assert newest["generated_at"] == (start + timedelta(minutes=11)).isoformat()
    assert newest["total"] == 14
    assert newest["passed"] == 11
    assert newest["failed"] == 2
    assert newest["skipped"] == 1
    assert newest["accuracy"] == pytest.approx(11 / 13)
    assert "broken.json" not in {item["name"] for item in items}
    assert "empty.json" not in {item["name"] for item in items}
    assert "no-time.json" not in {item["name"] for item in items}
    assert "bad-summary.json" not in {item["name"] for item in items}


async def test_evals_keeps_null_accuracy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = tmp_path / "results"
    results.mkdir()
    write_result(
        results / "all-skipped.json",
        generated_at="2026-11-01T00:00:00+00:00",
        total=4,
        passed=0,
        failed=0,
        skipped=4,
        accuracy=None,
    )
    monkeypatch.setattr(control_module, "EVALS_RESULTS_DIR", results)

    _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/evals")
        data = await resp.json()

    assert resp.status == 200
    assert data == {
        "items": [
            {
                "name": "all-skipped.json",
                "generated_at": "2026-11-01T00:00:00+00:00",
                "total": 4,
                "passed": 0,
                "failed": 0,
                "skipped": 4,
                "accuracy": None,
            }
        ]
    }


async def test_evals_missing_directory_is_empty_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control_module, "EVALS_RESULTS_DIR", tmp_path / "missing")

    _, app = make_control(tmp_path)
    async with running(app) as client:
        resp = await client.get("/api/evals")
        data = await resp.json()

    assert resp.status == 200
    assert data == {"items": []}
