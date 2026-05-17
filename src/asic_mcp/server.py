"""FastMCP server entrypoint for asic-mcp.

Five tools, mirroring abs-mcp, rba-mcp, apra-mcp, and ato-mcp so an agent that
uses multiple Australian government MCPs gets a uniform shape:

  - search_datasets     — fuzzy search the curated ASIC dataset catalogue
  - describe_dataset    — show columns, filters, and the canonical source URL
  - get_data            — query a dataset with filters / period / format
  - latest              — shortcut: most recent observation(s) per measure
  - list_curated        — enumerate the curated dataset IDs

Users speak plain English: `{"state": "nsw"}` and `"current_status": "current"`
land directly. The curated YAMLs translate aliases to ASIC's source column
headers and dimension values.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import re
from collections import OrderedDict
from datetime import date, timedelta
from typing import Annotated, Any, Literal

import pandas as pd
from fastmcp import FastMCP
from pydantic import Field

from . import catalog, curated
from .client import (
    ASICAPIError,
    ASICClient,
    get_stale_signal,
    reset_stale_signal,
)
from .discovery import DiscoveryError, DiscoverySpec, resolve_latest_url
from .models import ColumnDetail, DataResponse, DatasetDetail, DatasetSummary
from .parsing import drop_blank_rows, read_csv, read_xlsx
from .shaping import build_response

# Curated IDs are uppercase letters + digits + underscore.
_DATASET_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Period strings: YYYY, YYYY-MM, YYYY-MM-DD.
_PERIOD_PATTERN = re.compile(r"^[0-9-]{4,10}$")
_VALID_FORMATS = {"records", "series", "csv"}

mcp = FastMCP("asic-mcp")

_client: ASICClient | None = None
_client_lock = asyncio.Lock()

# Parsed-DataFrame cache. The byte cache short-circuits the network; this
# avoids re-parsing CSV/XLSX bytes on every warm call (~hundreds of ms even
# for 1 MB CSVs). Bounded LRU; eviction keeps memory under ~150-300 MB.
_DF_CACHE_MAX_ENTRIES = 8
_df_cache: OrderedDict[tuple, pd.DataFrame] = OrderedDict()
_df_cache_lock = asyncio.Lock()


def reset_df_cache_for_tests() -> None:
    """Drop the parsed-DataFrame cache. Tests use this to start from clean."""
    _df_cache.clear()


async def _get_client() -> ASICClient:
    global _client
    async with _client_lock:
        if _client is None:
            _client = ASICClient()
        return _client


async def reset_client_for_tests() -> None:
    """Drop the cached client. Tests that span event loops must clear it."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


def _suggest_dataset_id(bad_id: str) -> str:
    """Build a 'Did you mean X?' hint for an unknown dataset ID.

    Uses difflib's get_close_matches against curated.list_ids() so a typo
    like 'ASIC_FINANCIAL_ADVISOR' resolves to 'ASIC_FINANCIAL_ADVISERS'.
    Returns empty string if no close match clears the cutoff so we can
    skip the "Did you mean" clause cleanly.
    """
    try:
        known = curated.list_ids()
    except Exception:
        return ""
    matches = difflib.get_close_matches(bad_id.upper(), known, n=1, cutoff=0.6)
    return matches[0] if matches else ""


def _normalize_dataset_id(dataset_id: Any) -> str:
    if not isinstance(dataset_id, str):
        raise ValueError(
            f"dataset_id must be a string, got {type(dataset_id).__name__}. "
            "Search by keyword or enumerate the curated set to discover IDs."
        )
    norm = dataset_id.strip().upper()
    if not norm:
        raise ValueError(
            "dataset_id is empty. Enumerate the curated set to see available IDs."
        )
    if not _DATASET_ID_PATTERN.match(norm):
        raise ValueError(
            f"dataset_id {dataset_id!r} contains invalid characters — "
            "asic-mcp IDs use uppercase letters, digits, and underscores "
            "(e.g. 'ASIC_FINANCIAL_ADVISERS', 'ASIC_BANNED_PERSONS')."
        )
    return norm


def _validate_filters(filters: Any) -> dict[str, Any]:
    if filters is None:
        return {}
    if isinstance(filters, str):
        import json as _json
        try:
            filters = _json.loads(filters)
        except _json.JSONDecodeError as exc:
            raise ValueError(
                f"filters must be a JSON object, got invalid JSON string: {exc}. "
                "Example: {\"current_status\": \"current\", \"state\": \"nsw\"}."
            ) from exc
    if not isinstance(filters, dict):
        raise ValueError(
            f"filters must be a dict, got {type(filters).__name__}. "
            "Example: {'current_status': 'current', 'state': 'nsw'}."
        )
    return filters


def _validate_period(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    # LLM clients routinely send JSON ints (e.g. {"start_period": 2024}). Coerce
    # 4-digit ints in a realistic year range to the canonical "YYYY" string at the
    # boundary so we don't surface a confusing type error downstream.
    if isinstance(value, bool):
        # bool is a subclass of int; reject it explicitly before the int branch.
        raise ValueError(
            f"{field_name} must be a string or int year, got bool. "
            f"Try {field_name}='2024' (year), '2024-05' (month), '2024-01-01' (date), "
            "or 2024 (int year)."
        )
    if isinstance(value, int):
        if 1900 <= value <= 2100:
            value = str(value)
        else:
            raise ValueError(
                f"{field_name} integer {value} out of range. "
                f"For year-only periods pass a 4-digit year like 2024, or use string "
                f"forms 'YYYY' (e.g. '2026'), 'YYYY-MM' (e.g. '2026-05'), or "
                f"'YYYY-MM-DD' (e.g. '2026-05-15'). Try {field_name}='2024'."
            )
    if not isinstance(value, str):
        raise ValueError(
            f"{field_name} must be a string or int year like '2026', '2026-05', or "
            f"'2026-05-15', got {type(value).__name__}. "
            f"Try {field_name}='2024' to bound by year, or '2024-01-01' "
            "for a precise date cutoff."
        )
    s = value.strip()
    if not s:
        return None
    if not _PERIOD_PATTERN.match(s):
        raise ValueError(
            f"{field_name} {value!r} has invalid format. "
            "Use 'YYYY' (e.g. '2026'), 'YYYY-MM' (e.g. '2026-05'), or "
            "an ISO date 'YYYY-MM-DD'."
        )
    return s


async def _resolve_download_url(cd: curated.CuratedDataset, client: ASICClient) -> str:
    """If the curated YAML declares a discovery block, try to resolve a fresh
    URL via CKAN. On any failure, silently fall back to the YAML default —
    discovery upgrades staleness; it must not introduce new failure modes.
    """
    if cd.url_template:
        return await _resolve_dated_url(cd, client)
    if not cd.discovery:
        return cd.download_url
    try:
        spec = DiscoverySpec(
            package_id=cd.discovery.get("package_id"),
            package_id_pattern=cd.discovery.get("package_id_pattern"),
            organization_id=cd.discovery.get("organization_id"),
            resource_name=cd.discovery.get("resource_name"),
            resource_name_pattern=cd.discovery.get("resource_name_pattern"),
            # Default the format filter to the curated dataset's format so
            # CKAN packages publishing multiple formats under the same name
            # (CSV + TSV + XLSX) resolve to the one we know how to parse.
            # YAML can override via discovery.resource_format if needed.
            resource_format=cd.discovery.get("resource_format", cd.format),
        )
        return await resolve_latest_url(client, spec)
    except DiscoveryError:
        return cd.download_url


async def _resolve_dated_url(cd: curated.CuratedDataset, client: ASICClient) -> str:
    """Probe a date-templated URL for the most recently published file.

    Used for daily-cadence ASIC publications (e.g. short position reports)
    which have predictable URL patterns like
    `https://download.asic.gov.au/short-selling/RR{date:YYYYMMDD}-001-...csv`
    but are subject to T+N business-day publishing delays.

    Iterates backward from today up to `url_template_lookback_days` calendar
    days, probing each URL with HEAD. Returns the first 200-OK URL.
    Falls back to `cd.download_url` if nothing in the window is found.

    Public holidays / weekends are handled by the probe (404s skipped); we
    don't bother with an Australian business-day calendar.
    """
    assert cd.url_template is not None
    today = date.today()
    for delta in range(cd.url_template_lookback_days + 1):
        cand = today - timedelta(days=delta)
        url = cd.url_template.replace("{date:YYYYMMDD}", cand.strftime("%Y%m%d"))
        try:
            ok = await client.head_ok(url)
        except Exception:  # network errors — fall through to next date
            continue
        if ok:
            return url
    # Nothing in the window worked — fall back to the literal download_url
    # in the YAML so the caller still sees a coherent error chain.
    return cd.download_url


async def _fetch_and_parse(cd: curated.CuratedDataset, *, kind: str = "data"):
    """Download the dataset's primary resource and parse it into a DataFrame.

    The parsed DataFrame is cached in-process keyed by (url, parse-spec, body
    content hash). The hash makes the cache content-aware: if the byte cache
    serves stale bytes that get refreshed, the hash differs and we re-parse.
    """
    client = await _get_client()
    url = await _resolve_download_url(cd, client)
    try:
        body = await client.fetch_resource(url, kind=kind)  # type: ignore[arg-type]
    except ASICAPIError as e:
        raise ValueError(
            f"Could not fetch dataset {cd.id} from data.gov.au ({e}). "
            "data.gov.au is the upstream — transient 5xx / DNS errors usually "
            "clear on retry within a few minutes. If a cached payload was "
            f"available, this call would have served it with stale=True; the "
            f"cache for {cd.id} is empty. Try again shortly or visit "
            f"{cd.source_url} to confirm the dataset is still published."
        ) from e

    # Content-aware cache key. We can't hash the whole body on every warm call
    # (sha256 over the 50MB Financial Advisers CSV is too slow), so we use a
    # 3-part signature: total byte length + hash of head + hash of tail. Same
    # length AND same head AND same tail = same file in practice.
    head = body[:8192]
    tail = body[-2048:] if len(body) > 8192 else b""
    body_sig = hashlib.sha256(head + tail).digest()
    cache_key = (
        url, cd.format, cd.sheet, cd.header_row, cd.data_start_row,
        len(body), body_sig,
    )

    async with _df_cache_lock:
        cached = _df_cache.get(cache_key)
        if cached is not None:
            _df_cache.move_to_end(cache_key)
            return cached

    # Run sync pandas parse off the event loop. ASIC_AFS_AUTH_REP (50k+ rows,
    # ~50MB CSV) otherwise blocks the async tool for seconds and times out
    # downstream consumers like the ausdata-api gateway. `asyncio.to_thread`
    # offloads to the default ThreadPoolExecutor; the event loop stays free
    # to serve other concurrent requests during the parse.
    if cd.format == "csv":
        df = await asyncio.to_thread(read_csv, body)
    else:
        if cd.sheet is None:
            raise ValueError(
                f"Dataset {cd.id!r} declares format='xlsx' but has no sheet name. "
                "Fix the curated YAML."
            )
        df = await asyncio.to_thread(
            read_xlsx,
            body,
            sheet=cd.sheet,
            header_row=cd.header_row,
            data_start_row=cd.data_start_row,
            max_rows=cd.max_rows,
        )
    # Trim trailing blank rows where every dimension is NaN.
    dim_source_cols = [c.source_column for c in cd.columns.values() if c.role == "dimension"]
    if dim_source_cols:
        df = drop_blank_rows(df, dim_source_cols)

    async with _df_cache_lock:
        _df_cache[cache_key] = df
        _df_cache.move_to_end(cache_key)
        while len(_df_cache) > _DF_CACHE_MAX_ENTRIES:
            _df_cache.popitem(last=False)

    return df


@mcp.tool
async def search_datasets(
    query: Annotated[
        str,
        Field(
            description=(
                "Free-text search query. Matches against dataset IDs, names, "
                "descriptions, and curated search keywords. Case-insensitive."
            ),
            examples=[
                "financial adviser",
                "afs licence",
                "banned",
                "credit licensee",
                "liquidator",
                "registered auditor",
            ],
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of results to return, ranked by relevance.",
            examples=[5, 10],
            ge=1,
            le=50,
        ),
    ] = 10,
) -> list[DatasetSummary]:
    """Fuzzy-search the curated ASIC dataset catalogue.

    v0.1 ships 7 hand-curated ASIC registers: Financial Advisers, AFS Licensees,
    AFS Authorised Representatives, Credit Licensees, Banned and Disqualified
    Persons, Banned and Disqualified Organisations, and Registered Liquidators.

    Examples:
        # Find the financial advisers register
        results = await search_datasets("financial adviser")
        # → [{id: 'ASIC_FINANCIAL_ADVISERS', name: 'Financial Advisers Register', ...}]

        # Discover what's available on enforcement-related lists
        results = await search_datasets("banned")

    Returns:
        List of DatasetSummary (id, name, description, update_frequency,
        is_curated), ranked by relevance.
    """
    if not isinstance(query, str):
        raise ValueError(
            f"query must be a string, got {type(query).__name__}. "
            "Try 'financial adviser', 'banned', 'afs', 'credit', or 'liquidator'."
        )
    if not query.strip():
        raise ValueError(
            "query is required. Try 'financial adviser', 'banned', 'afs licence', "
            "'credit', 'liquidator', or any other ASIC register topic."
        )
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError(
            f"limit must be a positive integer, got {limit!r} "
            f"({type(limit).__name__}). Try limit=10 for a sample, or "
            "limit=50 for richer results. Valid range: 1 to 50."
        )
    if limit < 1:
        raise ValueError(
            f"limit must be between 1 and 50, got {limit}. "
            "Try limit=10 for a quick scan or limit=50 for the full ranked list."
        )
    return catalog.search(query, limit=limit)


@mcp.tool
async def describe_dataset(
    dataset_id: Annotated[
        str,
        Field(
            description=(
                "Curated dataset ID. Use the search endpoint or search tool "
                "to discover, or the list-curated endpoint/tool to enumerate. "
                "Case-insensitive."
            ),
            examples=[
                "ASIC_FINANCIAL_ADVISERS",
                "ASIC_AFS_LICENSEE",
                "ASIC_BANNED_PERSONS",
                "ASIC_CREDIT_LICENSEE",
                "ASIC_LIQUIDATOR",
            ],
        ),
    ],
) -> DatasetDetail:
    """Describe a dataset's filterable dimensions, returnable measures, and source.

    Use this before calling get_data on a new dataset — it tells you the
    valid filter keys ('current_status', 'state', 'business_name'), the valid
    filter values ('current', 'ceased', 'nsw', 'vic'), and the source URL.

    Returns:
        DatasetDetail with id, name, description, period_coverage, list of
        dimensions, list of measures, source_url, and download_url.
    """
    norm_id = _normalize_dataset_id(dataset_id)
    cd = curated.get(norm_id)
    if cd is None:
        suggestion = _suggest_dataset_id(norm_id)
        hint = (
            f"Did you mean {suggestion!r}? "
            if suggestion
            else ""
        )
        valid = curated.list_ids()
        raise ValueError(
            f"Dataset {dataset_id!r} is not a curated asic-mcp dataset. "
            f"{hint}"
            f"Valid options: {valid}. "
            "Search by keyword or enumerate the curated set to discover IDs."
        )
    dims_out = [
        ColumnDetail(
            key=c.key,
            source_column=c.source_column,
            description=c.description,
            unit=c.unit,
            role=c.role,
        )
        for c in cd.columns.values()
        if c.role in ("dimension", "id")
    ]
    measures_out = [
        ColumnDetail(
            key=c.key,
            source_column=c.source_column,
            description=c.description,
            unit=c.unit,
            role=c.role,
        )
        for c in cd.columns.values()
        if c.role == "measure"
    ]
    return DatasetDetail(
        id=cd.id,
        name=cd.name,
        description=cd.description,
        is_curated=True,
        update_frequency=cd.update_frequency,
        period_coverage=cd.period_coverage,
        dimensions=dims_out,
        measures=measures_out,
        source_url=cd.source_url,
        download_url=cd.download_url,
    )


async def _get_data_impl(
    dataset_id: str,
    filters: Any,
    start_period: Any,
    end_period: Any,
    fmt: Any,
    last_n: int | None = None,
    include_full_authorisation: bool = False,
) -> DataResponse:
    # Reset the graceful-degradation flag at the start of each tool call so
    # we only report staleness introduced by THIS call's fetches.
    reset_stale_signal()
    norm_id = _normalize_dataset_id(dataset_id)
    cd = curated.get(norm_id)
    if cd is None:
        suggestion = _suggest_dataset_id(norm_id)
        hint = (
            f"Did you mean {suggestion!r}? "
            if suggestion
            else ""
        )
        valid = curated.list_ids()
        raise ValueError(
            f"Dataset {dataset_id!r} is not a curated asic-mcp dataset. "
            f"{hint}"
            f"Valid options: {valid}. "
            "Search by keyword or enumerate the curated set to discover IDs."
        )
    filters_d = _validate_filters(filters)
    start_v = _validate_period(start_period, "start_period")
    end_v = _validate_period(end_period, "end_period")
    if fmt is None:
        fmt_norm = "records"
    elif isinstance(fmt, str):
        fmt_norm = fmt.lower()
    else:
        raise ValueError(
            f"format must be a string, got {type(fmt).__name__}. "
            f"Valid options: {sorted(_VALID_FORMATS)}"
        )
    if fmt_norm not in _VALID_FORMATS:
        raise ValueError(
            f"Unknown format {fmt!r}. Valid options: {sorted(_VALID_FORMATS)}"
        )
    if start_v and end_v and start_v > end_v:
        raise ValueError(
            f"end_period ({end_v}) is before start_period ({start_v}). "
            "Try swapping them."
        )

    user_query: dict[str, Any] = {}
    if filters_d:
        user_query["filters"] = dict(filters_d)
    if start_v:
        user_query["start_period"] = start_v
    if end_v:
        user_query["end_period"] = end_v

    df = await _fetch_and_parse(cd, kind=cd.cache_kind)  # type: ignore[arg-type]
    resp = build_response(
        cd=cd,
        df=df,
        filters=filters_d,
        measures=None,
        start_period=start_v,
        end_period=end_v,
        fmt=fmt_norm,
        user_query=user_query,
        last_n=last_n,
        include_full_authorisation=include_full_authorisation,
    )
    # If the byte fetch served a stale-cache fallback because data.gov.au
    # was unreachable, propagate the staleness to the response so the agent
    # can surface it to the user.
    stale, reason = get_stale_signal()
    if stale:
        resp.stale = True
        resp.stale_reason = reason
    return resp


@mcp.tool
async def get_data(
    dataset_id: Annotated[
        str,
        Field(
            description="Curated dataset ID. Use the search or list-curated endpoint/tool to discover.",
            examples=[
                "ASIC_FINANCIAL_ADVISERS",
                "ASIC_AFS_LICENSEE",
                "ASIC_BANNED_PERSONS",
            ],
        ),
    ],
    filters: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Dimension filters. Keys are plain-English aliases from the dataset's "
                "describe_dataset response. Values are matched against the source data; "
                "pass a list to OR across values. Examples: "
                "{'current_status': 'current'}, {'state': 'nsw'}, "
                "{'licensee_name': ['Westpac Banking Corporation', 'NAB']}."
            ),
            examples=[
                {"current_status": "current"},
                {"state": "nsw"},
                {"licensee_name": "Commonwealth Bank of Australia"},
                {"current_status": "current", "state": ["nsw", "vic"]},
            ],
        ),
    ] = None,
    start_period: Annotated[
        str | int | None,
        Field(
            description=(
                "Inclusive start date for time-bounded register fields "
                "(date of registration, date banned, etc.). "
                "Format: 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD'. "
                "Bare int years like 2020 are coerced to '2020' automatically."
            ),
            examples=["2020", "2020-07", "2024-01-01", 2020],
        ),
    ] = None,
    end_period: Annotated[
        str | int | None,
        Field(
            description="Inclusive end date. Same format as start_period.",
            examples=["2026", "2026-12-31", 2026],
        ),
    ] = None,
    format: Annotated[
        Literal["records", "series", "csv"],
        Field(
            description=(
                "Response shape. 'records' (default): flat list of observations. "
                "'series': grouped by measure. 'csv': pandas CSV string in `csv` field."
            ),
            examples=["records", "series", "csv"],
        ),
    ] = "records",
    include_full_authorisation: Annotated[
        bool,
        Field(
            description=(
                "ASIC_AFS_LICENSEE only. The `authorisation` field carries 2-3 KB "
                "of license boilerplate per record (which financial services and "
                "products the licensee may deal in). To keep responses under the "
                "portfolio's 10k-token target, that field is truncated to ~200 "
                "chars by default with a [truncated] suffix. Pass True to receive "
                "the full text — useful when verifying scope conditions for a "
                "specific licensee. Ignored for the other ASIC datasets."
            ),
            examples=[False, True],
        ),
    ] = False,
) -> DataResponse:
    """Query a curated ASIC register dataset and return matching rows.

    Examples:
        # All currently registered financial advisers in NSW
        resp = await get_data(
            "ASIC_FINANCIAL_ADVISERS",
            filters={"current_status": "current", "state": "nsw"},
        )

        # AFS licensees whose legal name contains "Macquarie"
        resp = await get_data(
            "ASIC_AFS_LICENSEE",
            filters={"licensee_name": "Macquarie"},
        )

        # Persons banned or disqualified after 2024
        resp = await get_data(
            "ASIC_BANNED_PERSONS",
            start_period="2024-01-01",
        )

        # Full authorisation text for one AFS licensee
        resp = await get_data(
            "ASIC_AFS_LICENSEE",
            filters={"licensee_name": "Macquarie Bank Limited"},
            include_full_authorisation=True,
        )

    Returns:
        DataResponse with records (or csv), unit, period bounds, row_count,
        source URL, and CC-BY 3.0 AU attribution.
    """
    return await _get_data_impl(
        dataset_id, filters, start_period, end_period, format,
        include_full_authorisation=include_full_authorisation,
    )


@mcp.tool
async def latest(
    dataset_id: Annotated[
        str,
        Field(
            description="Curated dataset ID.",
            examples=[
                "ASIC_FINANCIAL_ADVISERS",
                "ASIC_BANNED_PERSONS",
                "ASIC_AFS_LICENSEE",
            ],
        ),
    ],
    filters: Annotated[
        dict[str, Any] | None,
        Field(
            description="Same filter shape as get_data. Useful for narrowing to one entity.",
            examples=[
                {"licensee_name": "Westpac Banking Corporation"},
                {"adviser_number": "1234567"},
                {"current_status": "current"},
            ],
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum rows to return. ASIC registers can be huge — "
                "ASIC_AFS_AUTH_REP alone has ~360,000 rows. Without a cap the "
                "response blows an agent's context window. Pass filters to "
                "narrow the search; raise `limit` only if you genuinely need "
                "a bulk dump. Truncated responses set DataResponse.truncated_at "
                "to the original row count so agents can detect + surface it."
            ),
            ge=1,
            le=10000,
            examples=[50, 100, 500],
        ),
    ] = 50,
    include_full_authorisation: Annotated[
        bool,
        Field(
            description=(
                "ASIC_AFS_LICENSEE only. The `authorisation` field carries 2-3 KB "
                "of license boilerplate per record. To stay under the portfolio's "
                "10k-token target, that field is truncated to ~200 chars by "
                "default. Pass True to receive the full text — typically used "
                "when narrowing to one licensee. Ignored for the other ASIC datasets."
            ),
            examples=[False, True],
        ),
    ] = False,
) -> DataResponse:
    """Return the most recent observation(s) per measure for a dataset.

    For register data, "latest" is the current weekly/monthly snapshot
    capped at `limit` rows (default 50). For time-bounded fields
    (date_banned, date_ceased), latest returns the most recent matching
    record per entity.

    Pass `filters` to drill into one entity (no truncation hits) or raise
    `limit` to request more rows.

    Examples:
        # Current registered status for one adviser (precise filter, no truncation)
        resp = await latest(
            "ASIC_FINANCIAL_ADVISERS",
            filters={"adviser_number": "1234567"},
        )

        # First 50 most-recent banned persons (truncated; full count in resp.truncated_at)
        resp = await latest("ASIC_BANNED_PERSONS")

        # Get 500 rows in one go (still capped, but bigger window)
        resp = await latest("ASIC_AFS_LICENSEE", limit=500)

        # Full authorisation text for one AFS licensee
        resp = await latest(
            "ASIC_AFS_LICENSEE",
            filters={"licensee_name": "Macquarie Bank Limited"},
            include_full_authorisation=True,
        )
    """
    resp = await _get_data_impl(
        dataset_id, filters, None, None, "records", last_n=1,
        include_full_authorisation=include_full_authorisation,
    )
    # Cap the response so a 360k-row register doesn't bomb the agent's
    # context. Surface the original count via truncated_at so the agent
    # knows to add filters or raise limit if they really wanted more.
    original = len(resp.records)
    if original > limit:
        resp.records = resp.records[:limit]
        resp.row_count = limit
        resp.truncated_at = original
    return resp


@mcp.tool
def list_curated() -> list[str]:
    """List every curated dataset ID in this version of asic-mcp.

    These are the datasets where get_data accepts plain-English filter keys
    and returns aliased, well-typed columns. Each ID is documented via
    describe_dataset.

    Returns:
        Sorted list of dataset IDs.
    """
    return curated.list_ids()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
