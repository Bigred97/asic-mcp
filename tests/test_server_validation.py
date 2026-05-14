"""Server-level validation guards on each MCP tool.

Mirrors abs-mcp / rba-mcp `test_server_validation` — confirms each tool
rejects nonsense input cleanly (with a ValueError carrying a 'Try X' hint)
rather than crashing partway through with an obscure error.
"""
from __future__ import annotations

import pytest

from asic_mcp import server


@pytest.mark.asyncio
async def test_search_datasets_empty_query():
    with pytest.raises(ValueError, match="query is required"):
        await server.search_datasets("")


@pytest.mark.asyncio
async def test_search_datasets_whitespace_query():
    with pytest.raises(ValueError, match="query is required"):
        await server.search_datasets("   ")


@pytest.mark.asyncio
async def test_search_datasets_non_string_query():
    with pytest.raises(ValueError, match="must be a string"):
        await server.search_datasets(123)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_search_datasets_limit_too_small():
    with pytest.raises(ValueError, match="between 1 and 50"):
        await server.search_datasets("financial adviser", limit=0)


@pytest.mark.asyncio
async def test_search_datasets_limit_is_bool():
    with pytest.raises(ValueError, match="positive integer"):
        await server.search_datasets("financial adviser", limit=True)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_describe_dataset_unknown_id():
    with pytest.raises(ValueError, match="not a curated"):
        await server.describe_dataset("DOES_NOT_EXIST")


@pytest.mark.asyncio
async def test_describe_dataset_bad_chars():
    with pytest.raises(ValueError, match="invalid characters"):
        await server.describe_dataset("../etc/passwd")


@pytest.mark.asyncio
async def test_describe_dataset_empty_id():
    with pytest.raises(ValueError, match="empty"):
        await server.describe_dataset("")


@pytest.mark.asyncio
async def test_get_data_unknown_id():
    with pytest.raises(ValueError, match="not a curated"):
        await server.get_data("DOES_NOT_EXIST")


@pytest.mark.asyncio
async def test_get_data_filters_must_be_dict():
    with pytest.raises(ValueError, match="filters must be a dict"):
        await server.get_data(
            "ASIC_FINANCIAL_ADVISERS", filters=["state", "nsw"],  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_get_data_bad_period_format():
    with pytest.raises(ValueError, match="invalid format"):
        await server.get_data("ASIC_BANNED_PERSONS", start_period="?garbage?")


@pytest.mark.asyncio
async def test_get_data_period_swap():
    with pytest.raises(ValueError, match="before start_period"):
        await server.get_data(
            "ASIC_FINANCIAL_ADVISERS", start_period="2024", end_period="2020",
        )


@pytest.mark.asyncio
async def test_get_data_bad_format():
    with pytest.raises(ValueError, match="Unknown format"):
        await server.get_data("ASIC_FINANCIAL_ADVISERS", format="parquet")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_data_non_string_format():
    with pytest.raises(ValueError, match="format must be a string"):
        await server.get_data("ASIC_FINANCIAL_ADVISERS", format=123)  # type: ignore[arg-type]


def test_list_curated_returns_sorted_ids():
    ids = server.list_curated()
    assert ids == sorted(ids)
    assert "ASIC_FINANCIAL_ADVISERS" in ids
    assert "ASIC_BANNED_PERSONS" in ids
    assert len(ids) == 7


# --- Int-year coercion (Wave 1 interop fix) ----------------------------------

def test_validate_period_accepts_int_year():
    """Bare int years are coerced to 'YYYY' string at the boundary."""
    assert server._validate_period(2024, "start_period") == "2024"
    assert server._validate_period(2026, "end_period") == "2026"
    assert server._validate_period(1907, "start_period") == "1907"
    assert server._validate_period(2100, "end_period") == "2100"


def test_validate_period_int_out_of_range_raises_helpful():
    """Out-of-range int years raise with a useful hint, not a TypeError."""
    with pytest.raises(ValueError, match="out of range"):
        server._validate_period(1800, "start_period")
    with pytest.raises(ValueError, match="out of range"):
        server._validate_period(2200, "end_period")
    with pytest.raises(ValueError, match="YYYY"):
        server._validate_period(99, "start_period")


def test_validate_period_rejects_bool_with_hint():
    """bool is a subclass of int but must NOT be coerced silently."""
    with pytest.raises(ValueError, match="bool"):
        server._validate_period(True, "start_period")


# ─── latest() limit truncation (regression: context-bombing on large registers) ───

@pytest.mark.asyncio
async def test_latest_truncates_large_response_to_limit(monkeypatch):
    """Regression: `latest()` on ASIC_AFS_AUTH_REP used to return all ~360k
    rows, blowing any agent's context window. With v0.1.1+ it caps at
    `limit` (default 50) and surfaces the original count in `truncated_at`.
    """
    from datetime import UTC, datetime

    from asic_mcp.models import DataResponse, Observation

    # Fake a 1,000-row response from _get_data_impl
    fake_records = [
        Observation(value=float(i), dimensions={"licence_number": f"L{i:05d}"})
        for i in range(1000)
    ]
    fake_resp = DataResponse(
        dataset_id="ASIC_AFS_AUTH_REP",
        dataset_name="ASIC AFS Authorised Representative Register",
        records=fake_records,
        row_count=1000,
        retrieved_at=datetime.now(UTC),
        source_url="https://data.gov.au/example",
    )

    async def _fake_impl(*args, **kwargs):
        return fake_resp

    monkeypatch.setattr(server, "_get_data_impl", _fake_impl)

    resp = await server.latest("ASIC_AFS_AUTH_REP", limit=50)
    assert len(resp.records) == 50, "limit=50 should cap to 50 records"
    assert resp.row_count == 50, "row_count should reflect the truncated count"
    assert resp.truncated_at == 1000, "truncated_at preserves the original row count"


@pytest.mark.asyncio
async def test_latest_no_truncation_when_under_limit(monkeypatch):
    """If the underlying response fits under `limit`, no truncation
    happens and `truncated_at` stays None."""
    from datetime import UTC, datetime

    from asic_mcp.models import DataResponse, Observation

    small_resp = DataResponse(
        dataset_id="ASIC_LIQUIDATOR",
        dataset_name="ASIC Liquidator Register",
        records=[Observation(value=float(i), dimensions={"id": str(i)}) for i in range(10)],
        row_count=10,
        retrieved_at=datetime.now(UTC),
        source_url="https://data.gov.au/example",
    )

    async def _fake_impl(*args, **kwargs):
        return small_resp

    monkeypatch.setattr(server, "_get_data_impl", _fake_impl)

    resp = await server.latest("ASIC_LIQUIDATOR", limit=50)
    assert len(resp.records) == 10, "10 records, limit=50, no truncation"
    assert resp.truncated_at is None, "truncated_at is None when no truncation occurred"


# Note on bounds: `limit` ge=1/le=10000 is enforced by FastMCP/Pydantic at the
# MCP protocol boundary — not when calling the function directly from Python.
# The truncation logic above is what asic-mcp owns and is exercised by the two
# preceding tests. Boundary behaviour is the framework's contract, not ours.


# ─── error-message hint regressions (CLAUDE.md quality dim #5) ───
# Rejections should suggest the correction via "Did you mean X?" + "Try X"
# rather than just describing the failure.

@pytest.mark.asyncio
async def test_describe_unknown_dataset_suggests_close_match():
    """A typo'd dataset id should trigger a 'Did you mean' hint pointing at
    the closest curated id."""
    with pytest.raises(ValueError) as exc_info:
        # One-char typo: ADVISER -> ADVISOR
        await server.describe_dataset("ASIC_FINANCIAL_ADVISOR")
    msg = str(exc_info.value)
    assert "Did you mean" in msg
    assert "ASIC_FINANCIAL_ADVISERS" in msg
    # And the hint should still point at discovery tools
    assert "list_curated" in msg or "search_datasets" in msg


def test_unknown_filter_suggests_close_match_via_shaping():
    """Mistyped filter keys ('adviser_no' instead of 'adviser_number')
    should surface a difflib 'Did you mean' suggestion plus a pointer at
    describe_dataset(). Asserts against shaping._apply_filters directly so
    the test stays network-free."""
    import pandas as pd

    from asic_mcp import curated as curated_mod
    from asic_mcp import shaping

    cd = curated_mod.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    df = pd.DataFrame({c.source_column: [] for c in cd.columns.values()})
    with pytest.raises(ValueError) as exc_info:
        shaping._apply_filters(df, cd, {"adviser_no": "1234567"})
    msg = str(exc_info.value)
    assert "is not a column on" in msg
    assert "Did you mean" in msg
    assert "adviser_number" in msg
    assert "describe_dataset" in msg
