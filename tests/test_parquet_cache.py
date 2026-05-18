"""Tests for the on-disk Parquet cache.

The cache is used in two roles:
  - Warm cache for parsed DataFrames produced by the small-file path
    (write_df → read_df with TTL gating).
  - Cold-path destination for the streaming CSV → Parquet flow
    (we test the streaming integration end-to-end in
    test_streaming_companies.py).

Here we only exercise the cache primitives in isolation.
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import pytest

from asic_mcp import parquet_cache


@pytest.fixture(autouse=True)
def isolated_parquet_cache_dir(tmp_path, monkeypatch):
    """Redirect the parquet cache to a per-test temp dir."""
    monkeypatch.setenv("ASIC_MCP_PARQUET_CACHE_DIR", str(tmp_path))
    yield tmp_path


def test_round_trip_basic():
    """Write a DataFrame, read it back fresh."""
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    key = ("test", "round-trip")
    parquet_cache.write(key, df)
    got = parquet_cache.read_if_fresh(key)
    assert got is not None
    pd.testing.assert_frame_equal(got.reset_index(drop=True), df)


def test_returns_none_on_miss():
    """An unknown key returns None, not an exception."""
    got = parquet_cache.read_if_fresh(("nonexistent", "key"))
    assert got is None


def test_ttl_expiry():
    """A parquet file older than ttl_seconds is treated as a miss."""
    df = pd.DataFrame({"a": [1]})
    key = ("ttl", "test")
    parquet_cache.write(key, df)
    # Backdate mtime so the cache sees it as expired.
    path = parquet_cache.cache_dir() / (
        parquet_cache._key_to_filename(key)
    )
    old_time = time.time() - (15 * 24 * 60 * 60)  # 15 days ago
    path.touch()
    import os
    os.utime(path, (old_time, old_time))
    # Default TTL is 14d, so 15d-old reads None.
    assert parquet_cache.read_if_fresh(key) is None


def test_read_stale_ignores_ttl():
    """read_stale returns the cached frame even when expired."""
    df = pd.DataFrame({"a": [1, 2]})
    key = ("stale", "test")
    parquet_cache.write(key, df)
    path = parquet_cache.cache_dir() / parquet_cache._key_to_filename(key)
    old_time = time.time() - (30 * 24 * 60 * 60)
    import os
    os.utime(path, (old_time, old_time))
    got = parquet_cache.read_stale(key)
    assert got is not None
    stale_df, cached_at = got
    pd.testing.assert_frame_equal(stale_df.reset_index(drop=True), df)
    # Cached-at should be roughly the backdated mtime (within 1s).
    assert abs(cached_at - old_time) < 1


def test_read_stale_returns_none_when_missing():
    """No cache file → read_stale returns None too."""
    assert parquet_cache.read_stale(("totally", "absent")) is None


def test_column_projection():
    """Projection at read time only materialises the requested columns."""
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"], "c": [10.0, 20.0]})
    key = ("proj", "test")
    parquet_cache.write(key, df)
    got = parquet_cache.read_if_fresh(key, columns=["a", "c"])
    assert got is not None
    assert list(got.columns) == ["a", "c"]


def test_path_for_returns_deterministic_path():
    """path_for is what the streaming path uses to write directly."""
    key = ("any", "key")
    p1 = parquet_cache.path_for(key)
    p2 = parquet_cache.path_for(key)
    assert p1 == p2
    assert isinstance(p1, Path)
    assert p1.suffix == ".parquet"


def test_reset_for_tests_clears_files():
    """reset_for_tests scrubs the cache dir."""
    parquet_cache.write(("a",), pd.DataFrame({"x": [1]}))
    parquet_cache.write(("b",), pd.DataFrame({"x": [2]}))
    assert len(list(parquet_cache.cache_dir().glob("*.parquet"))) == 2
    parquet_cache.reset_for_tests()
    assert len(list(parquet_cache.cache_dir().glob("*.parquet"))) == 0
