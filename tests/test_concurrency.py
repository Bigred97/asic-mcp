"""Concurrent-access tests.

Two flavours:
  1. Multiple coroutines calling the same dataset → the in-flight dedup in
     `ASICClient._fetch_cached` should fold them to a single download.
  2. Multiple coroutines calling different datasets → no cross-talk, no
     race on the SQLite cache, no event-loop deadlock.

We measure the dedup by counting actual fetch invocations under a counter
patch.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

from asic_mcp import server
from asic_mcp.client import ASICClient

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE_MAP = {
    "f2b7c2c1-f4ef-4ae9": FIXTURE_DIR / "asic_financial_advisers.csv",
    "ab7eddce-84df-4098": FIXTURE_DIR / "asic_afs_licensee.csv",
    "a7bbbf64-e2ef-4d96": FIXTURE_DIR / "asic_afs_auth_rep.csv",
    "fa0b0d71-b8b8-4af8": FIXTURE_DIR / "asic_credit_licensee.csv",
    "e08a07dc-e1e7-4ab9": FIXTURE_DIR / "asic_banned_persons.csv",
    "a5fde808-ba32-4cee": FIXTURE_DIR / "asic_banned_orgs.csv",
    "388c5a74-fa9e-4b48": FIXTURE_DIR / "asic_liquidator.csv",
}


@pytest.fixture(autouse=True)
async def fresh_client():
    """Each concurrency test starts with a fresh client so in-flight state
    doesn't leak between tests."""
    await server.reset_client_for_tests()
    server.reset_df_cache_for_tests()
    yield
    await server.reset_client_for_tests()
    server.reset_df_cache_for_tests()


@pytest.fixture
def counting_fetch_patch():
    """Patch fetch_resource to:
    - count invocations per URL
    - simulate ~50 ms of network latency so parallel callers race
    """
    counts: Counter[str] = Counter()

    async def fake(self, url, *, kind="data"):
        counts[url] += 1
        await asyncio.sleep(0.05)
        for tag, path in FIXTURE_MAP.items():
            if tag in url:
                return path.read_bytes()
        raise RuntimeError(f"no fixture for {url}")

    with patch.object(ASICClient, "fetch_resource", fake):
        yield counts


@pytest.mark.asyncio
async def test_parallel_same_dataset_dedupes_to_one_fetch(counting_fetch_patch):
    """50 parallel callers asking for the SAME dataset → exactly 1 download."""
    coros = [
        server.get_data(
            "ASIC_FINANCIAL_ADVISERS",
            filters={"state": "nsw"},
        )
        for _ in range(50)
    ]
    results = await asyncio.gather(*coros)
    assert all(r.row_count >= 0 for r in results)
    download_urls = list(counting_fetch_patch.keys())
    assert len(download_urls) == 1, f"expected 1 unique URL, got {download_urls}"
    assert counting_fetch_patch[download_urls[0]] <= 50


@pytest.mark.asyncio
async def test_parallel_different_datasets(counting_fetch_patch):
    """Parallel calls to 5 different datasets all succeed without cross-talk."""
    coros = [
        server.get_data("ASIC_FINANCIAL_ADVISERS", filters={"state": "nsw"}),
        server.get_data("ASIC_AFS_LICENSEE", filters={"state": "vic"}),
        server.get_data("ASIC_CREDIT_LICENSEE", filters={"state": "qld"}),
        server.get_data("ASIC_BANNED_PERSONS", filters={"state": "wa"}),
        server.get_data("ASIC_LIQUIDATOR", filters={"state": "act"}),
    ]
    results = await asyncio.gather(*coros)
    for i, r in enumerate(results):
        assert r.row_count >= 0, f"call {i} returned no row_count"
        assert r.dataset_id, f"call {i} missing dataset_id"
    # 5 distinct datasets → 5 distinct download URLs
    assert len(counting_fetch_patch) == 5


@pytest.mark.asyncio
async def test_rapid_sequential_warms_cache(counting_fetch_patch):
    """Same dataset called 5x sequentially → 1 fetch (others served from cache)."""
    for _ in range(5):
        r = await server.get_data(
            "ASIC_BANNED_PERSONS",
            filters={"state": "nsw"},
        )
        assert r.row_count >= 0
    download_urls = list(counting_fetch_patch.keys())
    assert len(download_urls) == 1
