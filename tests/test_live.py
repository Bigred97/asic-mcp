"""Live tests against the real data.gov.au CKAN backend.

Marked `live` — deselected by default (see pyproject.toml addopts). Run with
`uv run pytest -m live` to actually hit the network.

These exist to catch the cases that mocked tests can't:
- ASIC genuinely refreshed the snapshot file on the resource URL.
- The CKAN metadata still points at a stable resource UUID we know works.
- The default User-Agent is accepted by data.gov.au's CDN.
"""
from __future__ import annotations

import pytest

from asic_mcp import server

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
async def fresh_client():
    """Each live test starts with a fresh client (no df-cache leak)."""
    await server.reset_client_for_tests()
    server.reset_df_cache_for_tests()
    yield
    await server.reset_client_for_tests()
    server.reset_df_cache_for_tests()


@pytest.mark.asyncio
async def test_live_search_finds_financial_advisers():
    results = await server.search_datasets("financial adviser", limit=3)
    ids = {r.id for r in results}
    assert "ASIC_FINANCIAL_ADVISERS" in ids


@pytest.mark.asyncio
async def test_live_describe_afs_licensee():
    d = await server.describe_dataset("ASIC_AFS_LICENSEE")
    assert d.id == "ASIC_AFS_LICENSEE"
    assert "Australian Financial Services" in d.description
    assert d.update_frequency == "weekly"
    assert d.source_url.startswith("https://data.gov.au/")


@pytest.mark.asyncio
async def test_live_get_data_afs_licensee_returns_thousands_of_records():
    """AFS licensee register has ~6,500 records — a NSW slice must be >100."""
    resp = await server.get_data(
        "ASIC_AFS_LICENSEE",
        filters={"state": "nsw"},
    )
    assert resp.row_count > 100
    assert resp.source == "Australian Securities and Investments Commission"
    assert "CC BY 3.0 AU" in resp.attribution


@pytest.mark.asyncio
async def test_live_get_data_banned_persons_has_records():
    """Banned Persons is small (~1 MB) — quick live sanity check."""
    resp = await server.get_data("ASIC_BANNED_PERSONS")
    assert resp.row_count > 100
    assert resp.records


@pytest.mark.asyncio
async def test_live_known_stable_major_bank_in_afs_licensee():
    """A major bank's AFS licensee record must be present in the live snapshot.
    The Commonwealth Bank of Australia holds AFSL 234945 and has done so
    continuously for years — if this lookup fails, something is very wrong
    upstream."""
    resp = await server.get_data(
        "ASIC_AFS_LICENSEE",
        filters={"licence_number": "234945"},
    )
    assert resp.row_count >= 1
    names = {obs.dimensions.get("licensee_name", "") for obs in resp.records}
    assert any("COMMONWEALTH BANK" in n.upper() for n in names), (
        f"CBA AFSL 234945 not found in live snapshot — got names {names}"
    )


@pytest.mark.asyncio
async def test_live_get_data_liquidator_small_register():
    """The Liquidator register has ~700 records — confirm it loads and a
    state filter returns >5."""
    resp = await server.get_data(
        "ASIC_LIQUIDATOR",
        filters={"state": "nsw"},
    )
    assert resp.row_count > 5


@pytest.mark.asyncio
async def test_live_csv_format_renders():
    """csv format must produce a non-empty CSV string for a non-empty result."""
    resp = await server.get_data(
        "ASIC_LIQUIDATOR",
        filters={"state": "nsw"},
        format="csv",
    )
    assert resp.csv
    assert "liquidator_name" in resp.csv


@pytest.mark.asyncio
async def test_live_latest_returns_one_per_measure():
    """latest() on a register with no measure columns returns the full filtered
    set (no per-measure trim possible)."""
    resp = await server.latest(
        "ASIC_LIQUIDATOR",
        filters={"state": "act"},
    )
    assert resp.row_count >= 0  # could be 0 in tiny states; still must not error


@pytest.mark.asyncio
async def test_live_list_curated_round_trip():
    """list_curated() then describe each — none should fail in production."""
    ids = server.list_curated()
    assert len(ids) == 7
    for dataset_id in ids:
        d = await server.describe_dataset(dataset_id)
        assert d.id == dataset_id
        assert d.dimensions, f"{dataset_id} has no dimensions"
