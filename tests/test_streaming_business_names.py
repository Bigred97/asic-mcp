"""End-to-end tests for the ASIC_BUSINESS_NAMES streaming path.

Mirrors `test_streaming_companies.py`. Added in 0.6.15 alongside the
`streaming: true` flip on the BUSINESS_NAMES YAML — customer audit
showed `/v1/data/asic/ASIC_BUSINESS_NAMES?limit=2` timing out at 26.7s
on the gateway because the ~400 MB CSV was still on the old
`pd.read_csv(BytesIO(body))` path. The 3-layer streaming pipeline
landed in 0.6.14 is fully data-driven from the curated `streaming`
flag, so the fix is YAML-only — these tests pin that the dispatch
actually fires for BUSINESS_NAMES.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from asic_mcp import curated, parquet_cache
from asic_mcp.server import (
    _fetch_and_parse_streaming,
    reset_client_for_tests,
    reset_df_cache_for_tests,
)

# Synthetic ASIC_BUSINESS_NAMES fixture — 7 columns matching the YAML
# source_column names exactly. Mix of statuses + states so the filter
# pushdown tests have something to bite on.
_BUSINESS_NAMES_CSV = (
    "REGISTER_NAME,BN_NAME,BN_STATUS,BN_REG_DT,BN_CANCEL_DT,BN_STATE_NUM,"
    "BN_STATE_OF_REG,BN_ABN\n"
    "Business Names,ACME TRADING,Registered,01/01/2020,,N12345,NSW,12345678901\n"
    "Business Names,WIDGETS CO,Registered,15/06/2019,,V67890,VIC,23456789012\n"
    "Business Names,OLD CORNER STORE,Cancelled,20/03/2010,15/08/2023,Q11111,QLD,34567890123\n"
    "Business Names,SYDNEY CONSULTING,Registered,05/11/2021,,N22222,NSW,45678901234\n"
    "Business Names,PERTH TRADERS,Cancelled,12/09/2015,30/06/2024,W33333,WA,56789012345\n"
    "Business Names,BRISBANE SOLUTIONS,Registered,18/07/2022,,Q44444,QLD,67890123456\n"
)


@pytest.fixture(autouse=True)
def isolated_parquet_cache_dir(tmp_path, monkeypatch):
    """Redirect parquet cache to a per-test temp dir."""
    monkeypatch.setenv("ASIC_MCP_PARQUET_CACHE_DIR", str(tmp_path / "parquet"))
    reset_df_cache_for_tests()
    yield


@pytest.fixture
def business_names_csv_path(tmp_path) -> Path:
    """Write the synthetic ASIC_BUSINESS_NAMES fixture to disk."""
    p = tmp_path / "business_names_test.csv"
    p.write_text(_BUSINESS_NAMES_CSV, encoding="utf-8")
    return p


def test_business_names_yaml_has_streaming_flag():
    """Regression: BUSINESS_NAMES must stay on the streaming path.

    Without this flag the gateway hits a 26.7s timeout on the 512 MB
    Fly worker because pd.read_csv loads the entire ~400 MB body into
    memory. Pinning the flag means a future YAML edit that drops it
    fails CI loudly.
    """
    cd = curated.get("ASIC_BUSINESS_NAMES")
    assert cd.streaming is True, (
        "ASIC_BUSINESS_NAMES must have streaming: true in its curated YAML "
        "(0.6.15+). Without it the ~400 MB CSV OOMs a 512 MB Fly worker "
        "and the gateway times out at ~27s even on limit=2 queries."
    )


@pytest.mark.asyncio
async def test_server_streaming_path_with_mocked_client_business_names(
    business_names_csv_path, monkeypatch
):
    """End-to-end: BUSINESS_NAMES uses mocked HTTP stream → parquet → df."""
    cd = curated.get("ASIC_BUSINESS_NAMES")
    assert cd.streaming is True

    async def fake_fetch_to_file(self, url, dest_path, **kw):
        import shutil
        shutil.copyfile(business_names_csv_path, dest_path)
        return business_names_csv_path.stat().st_size

    monkeypatch.setattr(
        "asic_mcp.client.ASICClient.fetch_resource_to_file",
        fake_fetch_to_file,
    )

    async def fake_resolve(cd, client):
        return "https://example.test/business_names_test.csv"

    monkeypatch.setattr(
        "asic_mcp.server._resolve_download_url", fake_resolve
    )

    df = await _fetch_and_parse_streaming(cd)
    assert len(df) == 6
    # Source columns survive the streaming round-trip.
    assert "BN_NAME" in df.columns
    assert "BN_STATUS" in df.columns
    assert "BN_STATE_OF_REG" in df.columns
    # Streaming path should have populated the parquet cache.
    projected = sorted({c.source_column for c in cd.columns.values()})
    cache_key = (
        "streaming-v1",
        "https://example.test/business_names_test.csv",
        cd.format,
        tuple(projected),
    )
    cached = parquet_cache.read_if_fresh(cache_key)
    assert cached is not None
    assert len(cached) == 6

    await reset_client_for_tests()


@pytest.mark.asyncio
async def test_server_streaming_path_filter_pushdown_business_names(
    business_names_csv_path, monkeypatch
):
    """Arrow-level filter pushdown narrows BUSINESS_NAMES rows correctly.

    Replicates the gateway's customer call shape: filtered latest()
    against the streaming path. Without filter pushdown the warm read
    would still materialise all rows; this test pins that the alias
    'state' resolves to BN_STATE_OF_REG and the predicate fires at
    the arrow batch level.
    """
    cd = curated.get("ASIC_BUSINESS_NAMES")

    async def fake_fetch_to_file(self, url, dest_path, **kw):
        import shutil
        shutil.copyfile(business_names_csv_path, dest_path)
        return business_names_csv_path.stat().st_size

    async def fake_resolve(cd, client):
        return "https://example.test/bn_filter_test.csv"

    monkeypatch.setattr(
        "asic_mcp.client.ASICClient.fetch_resource_to_file",
        fake_fetch_to_file,
    )
    monkeypatch.setattr(
        "asic_mcp.server._resolve_download_url", fake_resolve
    )

    # 'nsw' → NSW via dimension_values translation; pushdown to
    # BN_STATE_OF_REG should keep only the 2 NSW rows.
    df = await _fetch_and_parse_streaming(cd, filters={"state": "nsw"})
    assert len(df) == 2
    assert set(df["BN_STATE_OF_REG"].tolist()) == {"NSW"}

    await reset_client_for_tests()


@pytest.mark.asyncio
async def test_business_name_case_insensitive_substring(
    business_names_csv_path, monkeypatch
):
    """Regression for 0.6.16: `business_name='acme'` must return rows.

    The customer audit on 0.6.15 found that
    `?business_name=acme&limit=5` returned 0 rows even though ASIC's
    BUSINESS_NAMES register stores 'ACME TRADING' (uppercase). Root cause
    was that id-role columns were dispatching to `pc.equal(col, "acme")`
    against an uppercase Arrow column — guaranteed empty. The 0.6.16
    fix swaps the default to `pc.match_substring(..., ignore_case=True)`
    so bare values from the gateway just work.

    The fixture above has `ACME TRADING` as row 1. This test pins that
    lowercase 'acme' resolves it via the Arrow filter pushdown.
    """
    cd = curated.get("ASIC_BUSINESS_NAMES")

    async def fake_fetch_to_file(self, url, dest_path, **kw):
        import shutil
        shutil.copyfile(business_names_csv_path, dest_path)
        return business_names_csv_path.stat().st_size

    async def fake_resolve(cd, client):
        return "https://example.test/bn_acme_test.csv"

    monkeypatch.setattr(
        "asic_mcp.client.ASICClient.fetch_resource_to_file",
        fake_fetch_to_file,
    )
    monkeypatch.setattr(
        "asic_mcp.server._resolve_download_url", fake_resolve
    )

    # Lowercase, partial — must still hit ACME TRADING.
    df = await _fetch_and_parse_streaming(
        cd, filters={"business_name": "acme"}
    )
    assert len(df) >= 1
    assert any("ACME" in v for v in df["BN_NAME"].astype("string").tolist())

    # Mixed-case substring must also resolve.
    df_mixed = await _fetch_and_parse_streaming(
        cd, filters={"business_name": "AcMe"}
    )
    assert len(df_mixed) == len(df)

    # And the explicit-wildcard form (back-compat with 0.6.x callers)
    # produces the same result.
    df_wild = await _fetch_and_parse_streaming(
        cd, filters={"business_name": "acme*"}
    )
    assert len(df_wild) == len(df)

    # Substring 'sydney' must catch 'SYDNEY CONSULTING' regardless of case.
    df_syd = await _fetch_and_parse_streaming(
        cd, filters={"business_name": "sydney"}
    )
    assert len(df_syd) == 1
    assert df_syd["BN_NAME"].iloc[0] == "SYDNEY CONSULTING"

    # Negative: a needle that exists in NO row must return zero.
    df_none = await _fetch_and_parse_streaming(
        cd, filters={"business_name": "zzzz_not_in_register_zzzz"}
    )
    assert len(df_none) == 0

    await reset_client_for_tests()


@pytest.mark.asyncio
async def test_server_streaming_path_uses_parquet_warm_cache_business_names(
    business_names_csv_path, monkeypatch
):
    """Second call should hit the parquet cache, not re-stream the URL."""
    cd = curated.get("ASIC_BUSINESS_NAMES")

    fetch_count = {"n": 0}

    async def fake_fetch_to_file(self, url, dest_path, **kw):
        import shutil
        fetch_count["n"] += 1
        shutil.copyfile(business_names_csv_path, dest_path)
        return business_names_csv_path.stat().st_size

    async def fake_resolve(cd, client):
        return "https://example.test/bn_warm_cache_test.csv"

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
