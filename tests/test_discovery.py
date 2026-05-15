"""Discovery module tests.

Discovery is the auto-update path: it resolves a fresh CKAN URL at fetch
time so when ASIC publishes a new weekly snapshot, the curated YAML doesn't
need a code change. The contract is strict:

  - On success: return the freshest matching URL.
  - On any failure (network, malformed CKAN, no match, off-host): raise
    DiscoveryError. Callers MUST fall back to the YAML default.

These tests use respx so they're fast (no live network).
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from asic_mcp.cache import Cache
from asic_mcp.client import ASICClient
from asic_mcp.discovery import (
    DiscoveryError,
    DiscoverySpec,
    _pick_resource,
    _year_from_text,
    resolve_latest_url,
)


@pytest.fixture
def fresh_cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache.db")


# ---------------------------------------------------------------------------
# Helper unit tests (no network)
# ---------------------------------------------------------------------------

def test_year_from_text_extracts_highest():
    assert _year_from_text("ASIC Financial Advisers 2024-25") == 2024
    assert _year_from_text("Released 2019, covers 2024 data") == 2024
    assert _year_from_text("no year here") is None
    assert _year_from_text("") is None
    assert _year_from_text(None) is None  # type: ignore[arg-type]


def test_pick_resource_exact_name_match():
    resources = [
        {"name": "Financial Advisers Dataset - Current", "url": "https://data.gov.au/data/x/fa.csv"},
        {"name": "Financial Advisers Dataset - Help File", "url": "https://data.gov.au/data/x/help.pdf"},
    ]
    spec = DiscoverySpec(
        package_id="asic-financial-adviser",
        resource_name="Financial Advisers Dataset - Current",
    )
    m = _pick_resource(resources, spec)
    assert m is not None
    assert m["url"] == "https://data.gov.au/data/x/fa.csv"


def test_pick_resource_no_match_returns_none():
    resources = [{"name": "Other Resource", "url": "https://data.gov.au/data/x/other.csv"}]
    spec = DiscoverySpec(
        package_id="asic-financial-adviser",
        resource_name="Missing One",
    )
    assert _pick_resource(resources, spec) is None


def test_pick_resource_filters_by_format_when_multiple_formats_share_a_name():
    """ASIC Banned Organisations publishes the same resource name as CSV, TSV,
    and XLSX side-by-side. Without a format filter, _pick_resource would
    return whichever resource comes first — feeding XLSX bytes into the
    CSV reader. With resource_format set, the right one wins."""
    resources = [
        {
            "name": "Banned and Disqualified Organisations - Current",
            "format": "XLSX",
            "url": "https://data.gov.au/data/x/bd_org.xlsx",
        },
        {
            "name": "Banned and Disqualified Organisations - Current",
            "format": "CSV",
            "url": "https://data.gov.au/data/x/bd_org.csv",
        },
        {
            "name": "Banned and Disqualified Organisations - Current",
            "format": "TSV",
            "url": "https://data.gov.au/data/x/bd_org.tsv",
        },
    ]
    spec = DiscoverySpec(
        package_id="asic-banned-disqualified-org",
        resource_name="Banned and Disqualified Organisations - Current",
        resource_format="csv",
    )
    m = _pick_resource(resources, spec)
    assert m is not None
    assert m["url"].endswith(".csv")


def test_pick_resource_skips_non_dict_entries():
    resources = [
        "not a dict",  # type: ignore[list-item]
        None,
        {"name": "Right One", "url": "https://data.gov.au/data/x/file.csv"},
    ]
    spec = DiscoverySpec(package_id="x", resource_name="Right One")
    m = _pick_resource(resources, spec)
    assert m is not None
    assert m["url"] == "https://data.gov.au/data/x/file.csv"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_requires_package_id_or_pattern(fresh_cache: Cache):
    async with ASICClient(cache=fresh_cache) as client:
        with pytest.raises(DiscoveryError, match="package_id"):
            await resolve_latest_url(
                client,
                DiscoverySpec(resource_name="x"),
            )


@pytest.mark.asyncio
async def test_resolve_requires_resource_name_or_pattern(fresh_cache: Cache):
    async with ASICClient(cache=fresh_cache) as client:
        with pytest.raises(DiscoveryError, match="resource_name"):
            await resolve_latest_url(
                client,
                DiscoverySpec(package_id="asic-financial-adviser"),
            )


# ---------------------------------------------------------------------------
# Happy paths via respx
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_resolve_with_exact_package_id_and_name(fresh_cache: Cache):
    respx.get(
        url__startswith="https://data.gov.au/data/api/3/action/package_show",
    ).mock(
        return_value=httpx.Response(200, json={
            "success": True,
            "result": {
                "name": "asic-financial-adviser",
                "resources": [
                    {"name": "Financial Advisers Dataset - Help File", "url": "https://data.gov.au/data/x/help.pdf"},
                    {"name": "Financial Advisers Dataset - Current", "url": "https://data.gov.au/data/x/fa.csv"},
                ],
            },
        })
    )
    async with ASICClient(cache=fresh_cache) as client:
        url = await resolve_latest_url(
            client,
            DiscoverySpec(
                package_id="asic-financial-adviser",
                resource_name="Financial Advisers Dataset - Current",
            ),
        )
    assert url == "https://data.gov.au/data/x/fa.csv"


# ---------------------------------------------------------------------------
# Failure paths — every one MUST raise DiscoveryError (callers fall back).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_resolve_404_raises_discovery_error(fresh_cache: Cache):
    respx.get(
        url__startswith="https://data.gov.au/data/api/3/action/package_show",
    ).mock(return_value=httpx.Response(404))
    async with ASICClient(cache=fresh_cache) as client:
        with pytest.raises(DiscoveryError):
            await resolve_latest_url(
                client,
                DiscoverySpec(
                    package_id="missing-pkg",
                    resource_name="anything",
                ),
            )


@pytest.mark.asyncio
@respx.mock
async def test_resolve_no_matching_resource_raises(fresh_cache: Cache):
    respx.get(
        url__startswith="https://data.gov.au/data/api/3/action/package_show",
    ).mock(
        return_value=httpx.Response(200, json={
            "success": True,
            "result": {
                "name": "asic-financial-adviser",
                "resources": [{"name": "Other Resource", "url": "https://data.gov.au/data/x/other.csv"}],
            },
        })
    )
    async with ASICClient(cache=fresh_cache) as client:
        with pytest.raises(DiscoveryError, match="no resource matched"):
            await resolve_latest_url(
                client,
                DiscoverySpec(
                    package_id="asic-financial-adviser",
                    resource_name="Missing Resource",
                ),
            )


@pytest.mark.asyncio
@respx.mock
async def test_resolve_malformed_url_raises(fresh_cache: Cache):
    respx.get(
        url__startswith="https://data.gov.au/data/api/3/action/package_show",
    ).mock(
        return_value=httpx.Response(200, json={
            "success": True,
            "result": {
                "name": "x",
                "resources": [{"name": "Right One", "url": "file:///etc/passwd"}],
            },
        })
    )
    async with ASICClient(cache=fresh_cache) as client:
        with pytest.raises(DiscoveryError, match="invalid url"):
            await resolve_latest_url(
                client,
                DiscoverySpec(package_id="x", resource_name="Right One"),
            )


# ---------------------------------------------------------------------------
# Host pinning — discovery must only accept data.gov.au origins.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_resolve_off_host_url_rejected(fresh_cache: Cache):
    """If CKAN returns a valid-looking HTTPS URL pointing at attacker.com,
    discovery must refuse it. Defense-in-depth against a compromised CKAN."""
    respx.get(
        url__startswith="https://data.gov.au/data/api/3/action/package_show",
    ).mock(
        return_value=httpx.Response(200, json={
            "success": True,
            "result": {
                "name": "x",
                "resources": [{"name": "Right One", "url": "https://attacker.com/evil.csv"}],
            },
        })
    )
    async with ASICClient(cache=fresh_cache) as client:
        with pytest.raises(DiscoveryError, match="not on data.gov.au"):
            await resolve_latest_url(
                client,
                DiscoverySpec(package_id="x", resource_name="Right One"),
            )


@pytest.mark.asyncio
@respx.mock
async def test_resolve_data_gov_au_subdomain_allowed(fresh_cache: Cache):
    """www.data.gov.au is also valid."""
    respx.get(
        url__startswith="https://data.gov.au/data/api/3/action/package_show",
    ).mock(
        return_value=httpx.Response(200, json={
            "success": True,
            "result": {
                "name": "x",
                "resources": [{"name": "Right One", "url": "https://www.data.gov.au/data/path/file.csv"}],
            },
        })
    )
    async with ASICClient(cache=fresh_cache) as client:
        url = await resolve_latest_url(
            client,
            DiscoverySpec(package_id="x", resource_name="Right One"),
        )
    assert url.startswith("https://www.data.gov.au/")


def test_is_data_gov_au_host_check():
    from asic_mcp.discovery import _is_data_gov_au
    assert _is_data_gov_au("https://data.gov.au/data/x.csv") is True
    assert _is_data_gov_au("https://www.data.gov.au/data/x.csv") is True
    assert _is_data_gov_au("https://cdn.data.gov.au/data/x.csv") is True
    assert _is_data_gov_au("https://DATA.gov.au/data/x.csv") is True  # case-insensitive
    # Off-host
    assert _is_data_gov_au("https://attacker.com/evil.csv") is False
    assert _is_data_gov_au("https://data.gov.au.attacker.com/x.csv") is False  # suffix attack
    assert _is_data_gov_au("https://notdata.gov.au/x.csv") is False
    assert _is_data_gov_au("https://ev.il/data.gov.au") is False
    # Garbage
    assert _is_data_gov_au("not a url") is False
    assert _is_data_gov_au("") is False


# ---------------------------------------------------------------------------
# Server-side fallback: discovery failure must NOT break get_data.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_server_falls_back_to_yaml_url_when_discovery_fails(tmp_path):
    """When CKAN is unreachable but the YAML has a default URL, get_data
    should still succeed (resolving to the bundled fallback)."""
    from unittest.mock import patch

    from asic_mcp import curated as cmod
    from asic_mcp.client import ASICAPIError
    from asic_mcp.server import _resolve_download_url
    cmod.reset_registry()
    cd = cmod.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    cache = Cache(tmp_path / "fallback.db")
    async with ASICClient(cache=cache) as client:
        async def boom(*a, **kw):
            raise ASICAPIError("mocked failure")
        with patch.object(ASICClient, "fetch_package", boom), \
             patch.object(ASICClient, "_fetch_cached", boom):
            url = await _resolve_download_url(cd, client)
    assert url == cd.download_url
