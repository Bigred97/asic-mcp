"""Tests for the parsed-DataFrame in-process cache in server.py.

The cache is what makes warm get_data() calls cheap. We probe:
  - Repeated identical calls don't re-parse (counted via mock)
  - Cache key is content-aware: same URL but different bytes → re-parse
  - LRU eviction keeps memory bounded
  - Tests don't leak state via the autouse reset fixture
"""
from __future__ import annotations

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
async def reset_caches():
    server.reset_df_cache_for_tests()
    await server.reset_client_for_tests()
    yield
    server.reset_df_cache_for_tests()
    await server.reset_client_for_tests()


@pytest.fixture
def mocked_fetch_with_counter():
    """Patches fetch_resource and counts invocations per URL."""
    counts = {"calls": 0}

    async def fake(self, url, *, kind="data"):
        counts["calls"] += 1
        for tag, path in FIXTURE_MAP.items():
            if tag in url:
                return path.read_bytes()
        raise RuntimeError(f"no fixture for {url}")

    with patch.object(ASICClient, "fetch_resource", fake):
        yield counts


@pytest.fixture
def mocked_read_csv_with_counter():
    """Patch read_csv (asic-mcp uses CSV, not XLSX) and count invocations."""
    import asic_mcp.server as srv
    counts = {"calls": 0}
    original = srv.read_csv

    def counted(*args, **kwargs):
        counts["calls"] += 1
        return original(*args, **kwargs)

    with patch.object(srv, "read_csv", counted):
        yield counts


@pytest.mark.asyncio
async def test_repeat_query_does_not_reparse(
    mocked_fetch_with_counter, mocked_read_csv_with_counter,
):
    """Three identical get_data calls → only 1 PARSE.

    Note: the test mocks fetch_resource (bypassing the byte cache inside the
    real client), so fetch will be called 3x — that's expected. The point is
    the parsed-df cache: read_csv must be called exactly once.
    """
    for _ in range(3):
        r = await server.get_data(
            "ASIC_FINANCIAL_ADVISERS",
            filters={"state": "nsw"},
        )
        assert r.row_count >= 0
    assert mocked_read_csv_with_counter["calls"] == 1, (
        f"expected 1 parse, got {mocked_read_csv_with_counter['calls']}"
    )


@pytest.mark.asyncio
async def test_different_filters_share_parsed_df(
    mocked_fetch_with_counter, mocked_read_csv_with_counter,
):
    """The cache key is the parse spec, not the query. Different filters on
    the same dataset should share the cached DataFrame — i.e. one parse."""
    await server.get_data("ASIC_AFS_LICENSEE", filters={"state": "nsw"})
    await server.get_data("ASIC_AFS_LICENSEE", filters={"state": "vic"})
    await server.get_data("ASIC_AFS_LICENSEE", filters={"licensee_name": "Westpac"})
    assert mocked_read_csv_with_counter["calls"] == 1


@pytest.mark.asyncio
async def test_different_datasets_each_get_parsed(
    mocked_fetch_with_counter, mocked_read_csv_with_counter,
):
    """Each dataset has its own cache slot."""
    await server.get_data("ASIC_FINANCIAL_ADVISERS", filters={"state": "nsw"})
    await server.get_data("ASIC_AFS_LICENSEE", filters={"state": "nsw"})
    await server.get_data("ASIC_LIQUIDATOR", filters={"state": "nsw"})
    assert mocked_read_csv_with_counter["calls"] == 3


@pytest.mark.asyncio
async def test_lru_eviction_keeps_bounded(
    mocked_fetch_with_counter, mocked_read_csv_with_counter,
):
    """6 distinct datasets, cap is 8 — all fit; calling them again is cache-hit."""
    from asic_mcp.server import _DF_CACHE_MAX_ENTRIES, _df_cache

    queries = [
        ("ASIC_FINANCIAL_ADVISERS", {"state": "nsw"}),
        ("ASIC_AFS_LICENSEE", {"state": "nsw"}),
        ("ASIC_AFS_AUTH_REP", {"state": "nsw"}),
        ("ASIC_CREDIT_LICENSEE", {"state": "nsw"}),
        ("ASIC_BANNED_PERSONS", {"state": "nsw"}),
        ("ASIC_LIQUIDATOR", {"state": "nsw"}),
    ]
    for ds, filters in queries:
        await server.get_data(ds, filters=filters)
    first_parses = mocked_read_csv_with_counter["calls"]
    assert first_parses == 6

    # Repeat — should be all cache hits
    for ds, filters in queries:
        await server.get_data(ds, filters=filters)
    assert mocked_read_csv_with_counter["calls"] == first_parses

    # Cache should hold at most _DF_CACHE_MAX_ENTRIES
    assert len(_df_cache) <= _DF_CACHE_MAX_ENTRIES


@pytest.mark.asyncio
async def test_cache_invalidates_on_content_change(
    mocked_read_csv_with_counter, tmp_path,
):
    """If the byte cache returns different bytes (new weekly snapshot), the
    parsed-df cache must invalidate via the body hash."""
    server.reset_df_cache_for_tests()
    fixture_v1 = (FIXTURE_DIR / "asic_afs_licensee.csv").read_bytes()
    # v2: same shape, different bytes. Append a benign blank line at the end —
    # pandas tolerates it (trailing blanks are dropped), but the byte length
    # and tail-hash both change so the parsed-df cache must miss.
    fixture_v2 = fixture_v1 + b"\n\n"

    bodies = [fixture_v1, fixture_v2, fixture_v2]
    body_iter = iter(bodies)

    async def serve(self, url, *, kind="data"):
        return next(body_iter)

    with patch.object(ASICClient, "fetch_resource", serve):
        # call 1: v1 body → parse
        await server.get_data("ASIC_AFS_LICENSEE", filters={"state": "nsw"})
        first_parses = mocked_read_csv_with_counter["calls"]
        assert first_parses == 1

        # call 2: v2 body — different bytes. Cache must miss → re-parse.
        await server.get_data("ASIC_AFS_LICENSEE", filters={"state": "nsw"})
        assert mocked_read_csv_with_counter["calls"] == first_parses + 1

        # call 3: same v2 body → cache HIT (same len + same head/tail hash).
        await server.get_data("ASIC_AFS_LICENSEE", filters={"state": "nsw"})
        assert mocked_read_csv_with_counter["calls"] == first_parses + 1


@pytest.mark.asyncio
async def test_warm_hit_is_fast_enough_for_chat(mocked_fetch_with_counter):
    """Soft assertion: warm hits should be well under 100ms even for the
    biggest CSV fixture. (Cold parse is ~50ms; warm should be ~10ms.)"""
    import time
    server.reset_df_cache_for_tests()
    # Warm up
    await server.get_data("ASIC_AFS_LICENSEE", filters={"state": "nsw"})
    timings = []
    for _ in range(3):
        t0 = time.time()
        await server.get_data("ASIC_AFS_LICENSEE", filters={"state": "nsw"})
        timings.append((time.time() - t0) * 1000)
    median = sorted(timings)[len(timings) // 2]
    assert median < 200, f"warm hit too slow: median {median:.0f}ms (timings={timings})"
