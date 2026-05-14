"""Client-layer tests for graceful degradation (CLAUDE.md quality dim #4).

When data.gov.au is unreachable, the client must fall back to the most-recent
cached payload (regardless of TTL) and mark the response as stale. The agent
keeps reasoning instead of crashing. Empty-cache case still raises ASICAPIError.
"""
from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
import httpx
import pytest

from asic_mcp.cache import Cache
from asic_mcp.client import (
    ASICAPIError,
    ASICClient,
    get_stale_signal,
    reset_stale_signal,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cache.db"


# ─── stale-fallback graceful degradation (CLAUDE.md quality dim #4) ──────


async def _prime_stale_cache(
    db_path: Path, url: str, payload: bytes, age_hours: float
) -> None:
    """Put `payload` into the cache as if it was fetched `age_hours` ago.

    Used to test the stale-fallback path: a regular cache.get() with a normal
    TTL will miss this row (because cached_at is older than the TTL window),
    but cache.get_stale() will still return it.
    """
    cache = Cache(db_path)
    await cache._ensure_init()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO http_cache (cache_key, payload, cached_at, kind) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
            "payload=excluded.payload, cached_at=excluded.cached_at",
            (url, payload, time.time() - age_hours * 3600, "data"),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_stale_fallback_serves_cached_payload_on_5xx(db_path: Path) -> None:
    """When data.gov.au returns 5xx and we have a cached payload past its
    TTL, serve the cached payload and mark the response as stale. Agents
    continue reasoning rather than crashing."""
    payload = b"REGISTER_NAME,LIQ_NUM\n\"Liquidator\",\"480116\"\n"
    url = "https://data.gov.au/data/dataset/file.csv"

    # Prime a 48h-old cache entry — past the 24h "data" TTL, so cache.get()
    # misses but cache.get_stale() will still return it.
    await _prime_stale_cache(db_path, url, payload, age_hours=48)

    reset_stale_signal()
    cache = Cache(db_path)
    async with ASICClient(
        cache=cache,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(503, text="Service Unavailable")
        ),
    ) as client:
        body = await client.fetch_resource(url)
        assert body == payload, "fallback must return the cached bytes verbatim"
        stale, reason = get_stale_signal()
        assert stale is True, "stale flag must be set after 5xx fallback"
        assert reason and "503" in reason, (
            f"stale_reason should mention the 5xx: {reason}"
        )
        assert "minute" in reason.lower(), (
            f"stale_reason should report age: {reason}"
        )


@pytest.mark.asyncio
async def test_stale_fallback_serves_cached_on_request_error(db_path: Path) -> None:
    """Same as 5xx test but for httpx.RequestError (DNS / connection refused / etc.)."""
    payload = b"REGISTER_NAME,LIQ_NUM\n\"Liquidator\",\"480116\"\n"
    url = "https://data.gov.au/data/dataset/file.csv"
    await _prime_stale_cache(db_path, url, payload, age_hours=48)

    reset_stale_signal()

    def raise_request_error(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    cache = Cache(db_path)
    async with ASICClient(
        cache=cache, transport=httpx.MockTransport(raise_request_error)
    ) as client:
        body = await client.fetch_resource(url)
        assert body == payload
        stale, reason = get_stale_signal()
        assert stale is True
        assert reason and "ConnectError" in reason


@pytest.mark.asyncio
async def test_raises_when_no_stale_cache_to_fall_back_to(db_path: Path) -> None:
    """Empty cache + upstream 5xx → still raises ASICAPIError (original behaviour
    when there's nothing to gracefully degrade to)."""
    reset_stale_signal()
    cache = Cache(db_path)
    async with ASICClient(
        cache=cache,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(503, text="Service Unavailable")
        ),
    ) as client:
        with pytest.raises(ASICAPIError, match="503"):
            await client.fetch_resource("https://data.gov.au/data/dataset/file.csv")


@pytest.mark.asyncio
async def test_cache_get_stale_returns_payload_and_timestamp(db_path: Path) -> None:
    """Cache.get_stale() returns (payload, cached_at) regardless of TTL —
    the building block for client's stale-fallback path."""
    from datetime import timedelta

    cache = Cache(db_path)
    await cache.set("https://example.org/x", b"hello", kind="data")
    # Normal `get` with a tiny TTL should miss
    fresh = await cache.get("https://example.org/x", ttl=timedelta(seconds=0))
    assert fresh is None
    # `get_stale` should return regardless of TTL
    stale = await cache.get_stale("https://example.org/x")
    assert stale is not None
    payload, cached_at = stale
    assert payload == b"hello"
    assert cached_at > 0
    # Non-existent key → None
    miss = await cache.get_stale("https://example.org/missing")
    assert miss is None
