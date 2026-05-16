"""Adversarial / fuzz inputs into every public tool.

These probe boundaries the unit-validation tests don't reach: very long
strings, Unicode (emoji, RTL, combining marks), path-traversal attempts,
URL-injection characters in filter values, type confusion (bool vs int,
NaN, infinity), and edge integer values for `limit`.

Goal: every weird input either returns a clean result OR raises a ValueError
with an actionable message. Nothing should crash with a stack trace, a 500,
or silently return wrong data.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from asic_mcp import curated, server
from asic_mcp.client import ASICClient

FIXTURE_DIR = Path(__file__).parent / "fixtures"
# Map CKAN package-id fragment → fixture filename. _fake_fetch picks the
# first match against any URL substring; that lets us serve the right
# fixture for each dataset's resource URL.
FIXTURE_MAP = {
    "f2b7c2c1-f4ef-4ae9": FIXTURE_DIR / "asic_financial_advisers.csv",
    "ab7eddce-84df-4098": FIXTURE_DIR / "asic_afs_licensee.csv",
    "a7bbbf64-e2ef-4d96": FIXTURE_DIR / "asic_afs_auth_rep.csv",
    "fa0b0d71-b8b8-4af8": FIXTURE_DIR / "asic_credit_licensee.csv",
    "e08a07dc-e1e7-4ab9": FIXTURE_DIR / "asic_banned_persons.csv",
    "a5fde808-ba32-4cee": FIXTURE_DIR / "asic_banned_orgs.csv",
    "388c5a74-fa9e-4b48": FIXTURE_DIR / "asic_liquidator.csv",
}


async def _fake_fetch(self, url, *, kind="data"):
    for tag, path in FIXTURE_MAP.items():
        if tag in url:
            return path.read_bytes()
    raise RuntimeError(f"no fixture for {url}")


@pytest.fixture
def mocked_client():
    with patch.object(ASICClient, "fetch_resource", _fake_fetch):
        yield


# ---------------------------------------------------------------------------
# search_datasets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_query", [
    None,
    123,
    1.5,
    True,
    [],
    {},
    object(),
    b"financial adviser",
])
async def test_search_datasets_rejects_non_string_query(bad_query):
    with pytest.raises(ValueError):
        await server.search_datasets(bad_query)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("ws", ["", "   ", "\t\t", "\n\n", " \r\n "])
async def test_search_datasets_rejects_blank(ws):
    with pytest.raises(ValueError, match="query is required"):
        await server.search_datasets(ws)


@pytest.mark.asyncio
async def test_search_datasets_handles_huge_query():
    huge = "financial adviser " * 2000  # ~36KB
    r = await server.search_datasets(huge, limit=3)
    assert isinstance(r, list)


@pytest.mark.asyncio
async def test_search_datasets_handles_unicode():
    for q in ["税収", "🏠 financial adviser", "Tërritørÿ", "𝓛𝓲𝓬𝓮𝓷𝓼𝓮𝓮", "naïve"]:
        r = await server.search_datasets(q, limit=3)
        assert isinstance(r, list)


@pytest.mark.asyncio
async def test_search_datasets_handles_special_chars():
    """Things that would break naive SQL/URL handling."""
    for q in [
        "advisers'; DROP TABLE x;--",
        "<script>alert(1)</script>",
        "../../etc/passwd",
        "../%2e%2e/passwd",
        "%00",
        "\x00banned",
    ]:
        r = await server.search_datasets(q, limit=3)
        assert isinstance(r, list)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_limit", [0, -1, -100, False, 1.5, "10", None])
async def test_search_datasets_rejects_bad_limit(bad_limit):
    with pytest.raises(ValueError):
        await server.search_datasets("banned", limit=bad_limit)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_search_datasets_huge_limit_clipped_by_pydantic():
    from pydantic import ValidationError
    try:
        r = await server.search_datasets("banned", limit=10**6)
        assert len(r) <= len(curated.list_ids())
    except (ValueError, ValidationError):
        pass  # expected


# ---------------------------------------------------------------------------
# describe_dataset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", [
    None, 123, 1.5, True, [], {}, b"ASIC_BANNED",
])
async def test_describe_rejects_non_string(bad_id):
    with pytest.raises(ValueError, match="must be a string"):
        await server.describe_dataset(bad_id)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", [
    "../etc/passwd",
    "ASIC/FINANCIAL_ADVISERS",
    "ASIC%20FINANCIAL_ADVISERS",
    "ASIC FINANCIAL ADVISERS",
    "asic$financial",
    "ASIC;FINANCIAL",
    "ASIC\x00FINANCIAL",
    "🚀ASIC_FINANCIAL",
    "?dataset=ASIC_FINANCIAL",
])
async def test_describe_rejects_invalid_chars(bad_id):
    with pytest.raises(ValueError, match="invalid characters"):
        await server.describe_dataset(bad_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("ws_id", ["", "   ", "\t", "\n"])
async def test_describe_rejects_blank(ws_id):
    with pytest.raises(ValueError, match="empty"):
        await server.describe_dataset(ws_id)


@pytest.mark.asyncio
async def test_describe_case_insensitive():
    """Server normalizes to upper."""
    d_upper = await server.describe_dataset("ASIC_FINANCIAL_ADVISERS")
    d_lower = await server.describe_dataset("asic_financial_advisers")
    d_mixed = await server.describe_dataset("Asic_Financial_Advisers")
    d_padded = await server.describe_dataset("  ASIC_FINANCIAL_ADVISERS  ")
    assert (
        d_upper.id == d_lower.id == d_mixed.id == d_padded.id
        == "ASIC_FINANCIAL_ADVISERS"
    )


@pytest.mark.asyncio
async def test_describe_every_curated_dataset():
    """No dataset should error on describe — every YAML must validate."""
    for dataset_id in curated.list_ids():
        d = await server.describe_dataset(dataset_id)
        assert d.id == dataset_id
        assert d.name
        assert d.description
        assert d.source_url.startswith("https://")
        # Register datasets have dimensions but no measures.
        assert d.dimensions, f"{dataset_id} has no dimensions"


# ---------------------------------------------------------------------------
# get_data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_filters", [
    "not a dict",
    ["state", "nsw"],
    42,
    3.14,
    True,
])
async def test_get_data_rejects_non_dict_filters(bad_filters):
    # String filters first try JSON-decode (added in 0.6.1+); invalid JSON
    # raises "filters must be a JSON object" while non-string non-dict
    # raises "filters must be a dict". Both are valid rejections — pattern
    # matches either.
    with pytest.raises(ValueError, match="filters must be (a dict|a JSON object)"):
        await server.get_data("ASIC_FINANCIAL_ADVISERS", filters=bad_filters)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_period", [
    "??", "-1", "abcd", "2024'", "2024;",
    "2024/01", "2024.01", "https://evil/2024",
    "𝟚𝟘𝟚𝟜",
])
async def test_get_data_rejects_bad_periods(bad_period):
    with pytest.raises(ValueError, match="invalid format"):
        await server.get_data("ASIC_FINANCIAL_ADVISERS", start_period=bad_period)


@pytest.mark.asyncio
async def test_get_data_strips_period_whitespace():
    """Leading/trailing whitespace on periods is stripped silently."""
    try:
        await server.get_data("ASIC_FINANCIAL_ADVISERS", start_period="2024 ")
    except ValueError as e:
        assert "invalid format" not in str(e)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_format", ["json", "PARQUET", "table", "PROTOBUF", "", " "])
async def test_get_data_rejects_bad_format(bad_format):
    with pytest.raises(ValueError, match="Unknown format"):
        await server.get_data("ASIC_FINANCIAL_ADVISERS", format=bad_format)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_data_filter_with_url_injection_chars(mocked_client):
    """Filter values containing &, ?, /, # should still be safe."""
    r = await server.get_data(
        "ASIC_AFS_LICENSEE",
        filters={"licensee_name": "Westpac?&=/#"},
    )
    assert r.row_count == 0


@pytest.mark.asyncio
async def test_get_data_filter_with_huge_value(mocked_client):
    r = await server.get_data(
        "ASIC_AFS_LICENSEE",
        filters={"licensee_name": "X" * 10000},
    )
    assert r.row_count == 0


@pytest.mark.asyncio
async def test_get_data_filter_with_unicode(mocked_client):
    """Unicode filter values must not crash."""
    r = await server.get_data(
        "ASIC_AFS_LICENSEE",
        filters={"licensee_name": "Bürger King 🍔 株式会社"},
    )
    assert r.row_count == 0


@pytest.mark.asyncio
async def test_get_data_empty_filter_dict_returns_all(mocked_client):
    """{} filters should NOT raise — it means 'no filter applied'."""
    r = await server.get_data("ASIC_AFS_LICENSEE", filters={})
    assert r.row_count > 0


@pytest.mark.asyncio
async def test_get_data_unknown_filter_raises(mocked_client):
    with pytest.raises(ValueError, match="is not a column on"):
        await server.get_data(
            "ASIC_AFS_LICENSEE", filters={"nonsense_dimension": "x"},
        )


@pytest.mark.asyncio
async def test_get_data_periods_equal_allowed(mocked_client):
    """start == end is allowed."""
    r = await server.get_data(
        "ASIC_FINANCIAL_ADVISERS", start_period="2024", end_period="2024",
    )
    assert isinstance(r.row_count, int)


@pytest.mark.asyncio
async def test_get_data_period_swap_caught():
    with pytest.raises(ValueError, match="before start_period"):
        await server.get_data(
            "ASIC_FINANCIAL_ADVISERS", start_period="2025", end_period="2020",
        )


# ---------------------------------------------------------------------------
# latest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_latest_unknown_dataset_raises():
    with pytest.raises(ValueError, match="not a curated"):
        await server.latest("DOES_NOT_EXIST")


@pytest.mark.asyncio
async def test_latest_passes_validation_through(mocked_client):
    """latest() shares validation with get_data — confirm it fails the same way.
    String filters first try JSON-decode (0.6.1+); invalid JSON raises
    "filters must be a JSON object". Either rejection is acceptable."""
    with pytest.raises(ValueError, match="filters must be (a dict|a JSON object)"):
        await server.latest("ASIC_FINANCIAL_ADVISERS", filters="bad")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# list_curated
# ---------------------------------------------------------------------------

def test_list_curated_idempotent():
    ids1 = server.list_curated()
    ids2 = server.list_curated()
    assert ids1 == ids2
    assert ids1 == sorted(ids1)
