"""Smoke tests for prewarm_curated().

We only verify the helper's mechanics — error catching, semaphore
bounded concurrency, signature — without hitting data.gov.au. The
real prewarm flow against live data lives in tests/test_live.py.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from asic_mcp import server
from asic_mcp.server import prewarm_curated


@pytest.fixture(autouse=True)
def isolated_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("ASIC_MCP_PARQUET_CACHE_DIR", str(tmp_path / "parquet"))
    server.reset_df_cache_for_tests()
    yield


@pytest.mark.asyncio
async def test_unknown_dataset_id_raises():
    """Unknown IDs are rejected up front, before any HTTP."""
    with pytest.raises(ValueError, match="unknown dataset IDs"):
        await prewarm_curated(["NOT_A_DATASET"])


@pytest.mark.asyncio
async def test_per_dataset_error_isolation(monkeypatch):
    """A failure on one dataset doesn't abort the rest."""
    async def boom_latest(dataset_id, **kw):
        raise RuntimeError(f"forced failure on {dataset_id}")

    monkeypatch.setattr(server, "latest", boom_latest)

    # Pick two real IDs; both will fail under the mock but both must
    # land in the result dict.
    results = await prewarm_curated(
        ["ASIC_COMPANIES", "ASIC_FINANCIAL_ADVISERS"],
        max_concurrency=1,
    )
    assert set(results.keys()) == {"ASIC_COMPANIES", "ASIC_FINANCIAL_ADVISERS"}
    for v in results.values():
        assert v.startswith("error:")


@pytest.mark.asyncio
async def test_semaphore_bounded_concurrency(monkeypatch):
    """max_concurrency=1 serialises calls."""
    in_flight = {"current": 0, "peak": 0}

    async def slow_latest(dataset_id, **kw):
        in_flight["current"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["current"])
        await asyncio.sleep(0.05)
        in_flight["current"] -= 1

    monkeypatch.setattr(server, "latest", slow_latest)

    ids = ["ASIC_COMPANIES", "ASIC_FINANCIAL_ADVISERS", "ASIC_LIQUIDATOR"]
    t0 = time.perf_counter()
    await prewarm_curated(ids, max_concurrency=1)
    elapsed = time.perf_counter() - t0

    # With conc=1 and 3 × 50ms tasks, total should be >=150ms.
    assert elapsed >= 0.10
    assert in_flight["peak"] == 1


@pytest.mark.asyncio
async def test_default_dataset_list_covers_all(monkeypatch):
    """Calling with no IDs warms every curated dataset."""
    seen: list[str] = []

    async def record(dataset_id, **kw):
        seen.append(dataset_id)

    monkeypatch.setattr(server, "latest", record)

    from asic_mcp import curated
    expected = set(curated.list_ids())
    results = await prewarm_curated()
    assert set(results.keys()) == expected
    assert set(seen) == expected
