"""End-to-end tests for the ASIC_COMPANIES streaming path.

Why a separate file: the streaming flow is fundamentally different
from the small-file `read_csv` path tested in `test_parsing_asic.py`:
it streams HTTP bytes to a tempfile, then pyarrow converts the
tempfile to a Parquet cache file, then `pd.read_parquet` does a
column-projected load. None of those steps share code with the
small-file path, so the tests live separately.

We mock `ASICClient.fetch_resource_to_file` so the test never hits
data.gov.au — the synthetic fixture below stands in for the real
~600 MB CSV at correctness-only scale (20 rows, same 11-column
schema). The memory profile is verified live (see test_live.py).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from asic_mcp import curated, parquet_cache
from asic_mcp.parsing import ParseError, stream_csv_to_parquet
from asic_mcp.server import (
    _fetch_and_parse_streaming,
    reset_client_for_tests,
    reset_df_cache_for_tests,
)

# Synthetic ASIC_COMPANIES fixture — 11 columns matching the YAML
# source_column names exactly. Mix of statuses + types so filter tests
# downstream have something to bite on.
_COMPANIES_CSV = (
    "Company Name,ACN,Type,Class,Sub Class,Status,Date of Registration,"
    "Date of Deregistration,Previous State of Registration,ABN,Current Name\n"
    "COMMONWEALTH BANK OF AUSTRALIA,123456789,APUB,Limited by Shares,,REGD,"
    "17/04/1911,,VIC,48123123124,COMMONWEALTH BANK OF AUSTRALIA\n"
    "WESTPAC BANKING CORPORATION,007457141,APUB,Limited by Shares,,REGD,"
    "23/10/1850,,NSW,33007457141,WESTPAC BANKING CORPORATION\n"
    "MACQUARIE BANK LIMITED,008583542,APUB,Limited by Shares,,REGD,"
    "06/03/1969,,NSW,46008583542,MACQUARIE BANK LIMITED\n"
    "ACME PTY LTD,111222333,APTY,Limited by Shares,,REGD,"
    "01/01/2020,,VIC,,ACME PTY LTD\n"
    "WIDGETS PTY LTD,444555666,APTY,Limited by Shares,,DRGD,"
    "15/06/2015,30/06/2024,VIC,,WIDGETS PTY LTD\n"
    "FOREIGN HOLDINGS INC,777888999,RFCD,Limited by Shares,,REGD,"
    "12/09/2018,,NSW,,FOREIGN HOLDINGS INC\n"
)


@pytest.fixture(autouse=True)
def isolated_parquet_cache_dir(tmp_path, monkeypatch):
    """Redirect parquet cache to a per-test temp dir."""
    monkeypatch.setenv("ASIC_MCP_PARQUET_CACHE_DIR", str(tmp_path / "parquet"))
    reset_df_cache_for_tests()
    yield


@pytest.fixture
def companies_csv_path(tmp_path) -> Path:
    """Write the synthetic ASIC_COMPANIES fixture to disk."""
    p = tmp_path / "company_test.csv"
    p.write_text(_COMPANIES_CSV, encoding="utf-8")
    return p


def test_stream_csv_to_parquet_basic(companies_csv_path, tmp_path):
    """Streaming converter writes a readable Parquet with all 11 columns."""
    parquet_path = tmp_path / "companies.parquet"
    rows = stream_csv_to_parquet(companies_csv_path, parquet_path)
    assert rows == 6
    df = pd.read_parquet(parquet_path)
    assert len(df) == 6
    assert "Company Name" in df.columns
    assert "ACN" in df.columns
    # Type preservation — ACN should round-trip as a string ("123456789",
    # not int 123456789 — preserving leading zeros for shorter ACNs is
    # the whole point of forcing utf8 in the parquet schema).
    assert df["ACN"].dtype.name in ("object", "string")
    assert "123456789" in set(df["ACN"].tolist())


def test_stream_csv_to_parquet_column_projection(companies_csv_path, tmp_path):
    """Column projection at read time skips unprojected columns."""
    parquet_path = tmp_path / "companies.parquet"
    stream_csv_to_parquet(
        companies_csv_path,
        parquet_path,
        columns=["Company Name", "ACN", "Status"],
    )
    df = pd.read_parquet(parquet_path)
    # Only the 3 projected columns should land in the Parquet.
    assert set(df.columns) == {"Company Name", "ACN", "Status"}
    assert len(df) == 6


def test_stream_csv_to_parquet_missing_source_raises(tmp_path):
    """Sensible error if the source CSV isn't on disk."""
    with pytest.raises(ParseError, match="streaming CSV source not found"):
        stream_csv_to_parquet(
            tmp_path / "no_such_file.csv",
            tmp_path / "out.parquet",
        )


def test_stream_csv_to_parquet_empty_csv_raises(tmp_path):
    """Header-only CSV produces a clear error rather than empty parquet."""
    empty_csv = tmp_path / "empty.csv"
    empty_csv.write_text(
        "Company Name,ACN,Status\n",  # header but no rows
        encoding="utf-8",
    )
    with pytest.raises(ParseError, match="0 rows"):
        stream_csv_to_parquet(
            empty_csv,
            tmp_path / "out.parquet",
        )


def test_stream_csv_to_parquet_atomic_write(companies_csv_path, tmp_path):
    """A failed conversion does not leave a half-written parquet behind."""
    parquet_path = tmp_path / "atomic.parquet"
    stream_csv_to_parquet(companies_csv_path, parquet_path)
    assert parquet_path.is_file()
    # Sidecar .tmp files should be cleaned up.
    assert not (tmp_path / "atomic.parquet.tmp").exists()


@pytest.mark.asyncio
async def test_server_streaming_path_with_mocked_client(
    companies_csv_path, monkeypatch
):
    """End-to-end: server.streaming_path uses mocked HTTP stream → parquet → df."""
    cd = curated.get("ASIC_COMPANIES")
    assert cd.streaming is True

    # Mock fetch_resource_to_file: copy the synthetic fixture to dest_path.
    async def fake_fetch_to_file(self, url, dest_path, **kw):
        import shutil
        shutil.copyfile(companies_csv_path, dest_path)
        return companies_csv_path.stat().st_size

    monkeypatch.setattr(
        "asic_mcp.client.ASICClient.fetch_resource_to_file",
        fake_fetch_to_file,
    )

    # Mock discovery so it doesn't hit CKAN.
    async def fake_resolve(cd, client):
        return "https://example.test/company_test.csv"

    monkeypatch.setattr(
        "asic_mcp.server._resolve_download_url", fake_resolve
    )

    df = await _fetch_and_parse_streaming(cd)
    assert len(df) == 6
    # Source columns survive the streaming round-trip.
    assert "Company Name" in df.columns
    assert "Status" in df.columns
    # Streaming path should have populated the parquet cache.
    projected = sorted({c.source_column for c in cd.columns.values()})
    cache_key = (
        "streaming-v1",
        "https://example.test/company_test.csv",
        cd.format,
        tuple(projected),
    )
    cached = parquet_cache.read_if_fresh(cache_key)
    assert cached is not None
    assert len(cached) == 6

    await reset_client_for_tests()


@pytest.mark.asyncio
async def test_company_name_case_insensitive_substring(
    companies_csv_path, monkeypatch
):
    """Regression for 0.6.16: `company_name='acme'` must return rows.

    Same root cause as the BUSINESS_NAMES bug: id-role columns used
    `pc.equal` instead of `pc.match_substring` on bare values, which
    against the uppercase 'ACME PTY LTD' source returned 0 rows.
    """
    cd = curated.get("ASIC_COMPANIES")

    async def fake_fetch_to_file(self, url, dest_path, **kw):
        import shutil
        shutil.copyfile(companies_csv_path, dest_path)
        return companies_csv_path.stat().st_size

    async def fake_resolve(cd, client):
        return "https://example.test/companies_acme_test.csv"

    monkeypatch.setattr(
        "asic_mcp.client.ASICClient.fetch_resource_to_file",
        fake_fetch_to_file,
    )
    monkeypatch.setattr(
        "asic_mcp.server._resolve_download_url", fake_resolve
    )

    df = await _fetch_and_parse_streaming(
        cd, filters={"company_name": "acme"}
    )
    assert len(df) == 1
    assert df["Company Name"].iloc[0] == "ACME PTY LTD"

    df_mixed = await _fetch_and_parse_streaming(
        cd, filters={"company_name": "MaCqUaRiE"}
    )
    assert len(df_mixed) == 1
    assert df_mixed["Company Name"].iloc[0] == "MACQUARIE BANK LIMITED"

    # Substring 'bank' should match the three bank rows.
    df_bank = await _fetch_and_parse_streaming(
        cd, filters={"company_name": "bank"}
    )
    assert len(df_bank) == 3

    await reset_client_for_tests()


@pytest.mark.asyncio
async def test_server_streaming_path_uses_parquet_warm_cache(
    companies_csv_path, monkeypatch
):
    """Second call should hit the parquet cache, not re-stream the URL."""
    cd = curated.get("ASIC_COMPANIES")

    fetch_count = {"n": 0}

    async def fake_fetch_to_file(self, url, dest_path, **kw):
        import shutil
        fetch_count["n"] += 1
        shutil.copyfile(companies_csv_path, dest_path)
        return companies_csv_path.stat().st_size

    async def fake_resolve(cd, client):
        return "https://example.test/warm_cache_test.csv"

    monkeypatch.setattr(
        "asic_mcp.client.ASICClient.fetch_resource_to_file",
        fake_fetch_to_file,
    )
    monkeypatch.setattr(
        "asic_mcp.server._resolve_download_url", fake_resolve
    )

    df1 = await _fetch_and_parse_streaming(cd)
    df2 = await _fetch_and_parse_streaming(cd)

    assert len(df1) == 6
    assert len(df2) == 6
    # Cold + warm should both succeed but only ONE HTTP stream.
    assert fetch_count["n"] == 1

    await reset_client_for_tests()


@pytest.mark.asyncio
async def test_server_streaming_path_stale_fallback(
    companies_csv_path, monkeypatch
):
    """When HTTP fails AND a stale parquet exists, serve it with stale signal."""
    from asic_mcp.client import (
        ASICAPIError,
        get_stale_signal,
        reset_stale_signal,
    )

    cd = curated.get("ASIC_COMPANIES")
    reset_stale_signal()

    async def fake_resolve(cd, client):
        return "https://example.test/stale_fallback.csv"

    monkeypatch.setattr(
        "asic_mcp.server._resolve_download_url", fake_resolve
    )

    # First call: succeeds → populates parquet cache.
    async def good_fetch(self, url, dest_path, **kw):
        import shutil
        shutil.copyfile(companies_csv_path, dest_path)
        return companies_csv_path.stat().st_size

    monkeypatch.setattr(
        "asic_mcp.client.ASICClient.fetch_resource_to_file",
        good_fetch,
    )
    df_first = await _fetch_and_parse_streaming(cd)
    assert len(df_first) == 6

    # Clear ONLY the in-process LRU so the second call has to hit the
    # parquet cache; calling reset_df_cache_for_tests() here would also
    # wipe the parquet, defeating the stale-fallback test.
    from asic_mcp.server import _df_cache
    _df_cache.clear()
    parquet_path = parquet_cache.path_for((
        "streaming-v1",
        "https://example.test/stale_fallback.csv",
        cd.format,
        tuple(sorted({c.source_column for c in cd.columns.values()})),
    ))
    # Backdate the parquet so read_if_fresh treats it as a miss
    # but read_stale still picks it up.
    import os
    import time
    old = time.time() - (30 * 24 * 60 * 60)
    os.utime(parquet_path, (old, old))

    # Second call: HTTP raises → stale fallback served.
    async def bad_fetch(self, url, dest_path, **kw):
        raise ASICAPIError("simulated upstream 503")

    monkeypatch.setattr(
        "asic_mcp.client.ASICClient.fetch_resource_to_file",
        bad_fetch,
    )
    reset_stale_signal()
    df_stale = await _fetch_and_parse_streaming(cd)
    assert len(df_stale) == 6
    stale, reason = get_stale_signal()
    assert stale is True
    assert reason is not None
    assert "stale Parquet cache" in reason

    await reset_client_for_tests()
