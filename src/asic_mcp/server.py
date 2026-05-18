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
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import pandas as pd
from fastmcp import FastMCP
from pydantic import Field

from . import catalog, curated, parquet_cache
from .client import (
    ASICAPIError,
    ASICClient,
    get_stale_signal,
    reset_stale_signal,
)
from .client import (
    _mark_stale as _mark_stale_signal,
)
from .discovery import DiscoveryError, DiscoverySpec, resolve_latest_url
from .models import ColumnDetail, DataResponse, DatasetDetail, DatasetSummary
from .parsing import ParseError, drop_blank_rows, read_csv, read_xlsx, stream_csv_to_parquet
from .shaping import build_response

# Curated IDs are uppercase letters + digits + underscore.
_DATASET_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Period strings: YYYY, YYYY-MM, YYYY-MM-DD.
_PERIOD_PATTERN = re.compile(r"^[0-9-]{4,10}$")
_VALID_FORMATS = {"records", "series", "csv"}

mcp = FastMCP("asic-mcp")

# Per-thread client cache. The gateway runs MCP tools from worker threads,
# each with its own asyncio event loop. A module-level singleton holding httpx
# state from a previous (now-closed) loop raises ``RuntimeError: Event loop is
# closed``. ``threading.local()`` gives each thread its own client bound to
# whichever loop is current on that thread when it's first constructed.
_thread_local = threading.local()

# Parsed-DataFrame cache. The byte cache short-circuits the network; this
# avoids re-parsing CSV/XLSX bytes on every warm call (~hundreds of ms even
# for 1 MB CSVs). Bounded LRU; eviction keeps memory under ~150-300 MB.
_DF_CACHE_MAX_ENTRIES = 8
_df_cache: OrderedDict[tuple, pd.DataFrame] = OrderedDict()
_df_cache_lock = asyncio.Lock()


def reset_df_cache_for_tests() -> None:
    """Drop the parsed-DataFrame cache + on-disk Parquet cache.

    Tests use this to start from clean.
    """
    _df_cache.clear()
    parquet_cache.reset_for_tests()


async def _get_client() -> ASICClient:
    """Return the per-thread client, constructing it on first use.

    Each worker thread gets its own ASICClient bound to its own event
    loop. Required because gateways like ausdata-api invoke us from
    worker threads with fresh asyncio loops per call (``asyncio.run()``);
    a module-global client would bind to the first loop and fail on
    subsequent calls with ``RuntimeError: Event loop is closed``.
    """
    client = getattr(_thread_local, "client", None)
    if client is None:
        client = ASICClient()
        _thread_local.client = client
    return client


async def reset_client_for_tests() -> None:
    """Drop the current thread's cached client.

    The server keeps one ASICClient per worker thread (see ``_get_client``).
    Tests that span multiple event loops on the same thread must clear it
    between loops or httpx will trip on a closed loop. Resets ONLY the
    calling thread's client.
    """
    client = getattr(_thread_local, "client", None)
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass
        try:
            del _thread_local.client
        except AttributeError:
            pass


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


async def _fetch_and_parse(
    cd: curated.CuratedDataset,
    *,
    kind: str = "data",
    filters: dict[str, Any] | None = None,
):
    """Download the dataset's primary resource and parse it into a DataFrame.

    The parsed DataFrame is cached in-process keyed by (url, parse-spec, body
    content hash). The hash makes the cache content-aware: if the byte cache
    serves stale bytes that get refreshed, the hash differs and we re-parse.

    `filters` is consumed only by the streaming path. The small-file path
    keeps the existing "load the whole frame, filter in shaping" model so
    its byte and df caches remain content-keyed rather than filter-keyed.

    For datasets flagged `streaming: true` in the curated YAML (ASIC_COMPANIES
    is the only one as of 0.6.14), we dispatch to `_fetch_and_parse_streaming`
    which bypasses the SQLite byte cache and uses chunked Arrow filter
    pushdown directly on the on-disk Parquet — peak memory ~80 MB regardless
    of source size or query shape.
    """
    if cd.streaming:
        return await _fetch_and_parse_streaming(cd, filters=filters or {})

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


# Hard cap on rows returned by a streaming-path call when no filter narrows
# the set. Prevents `get_data(ASIC_COMPANIES)` (no filter) from materialising
# all 4.3M rows. Mirrors the `_HARD_MAX_RECORDS` convention used in the
# bigger sisters. Latest()'s `limit` kwarg caps it further on the response side.
_STREAMING_HARD_MAX_RECORDS = 100_000

# Arrow batch size for chunked parquet reads. ~200k rows per batch keeps
# peak memory per batch under ~80 MB even with all 11 ASIC_COMPANIES
# columns held simultaneously, while keeping iteration overhead low.
_STREAMING_BATCH_SIZE = 200_000


async def _fetch_and_parse_streaming(
    cd: curated.CuratedDataset,
    *,
    filters: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Streaming variant for very large CSVs (ASIC_COMPANIES, ~600 MB).

    Pipeline:
      1. Resolve current download URL via CKAN discovery (same as small-file path).
      2. Ensure the on-disk Parquet cache is fresh (cold path: httpx.stream
         → tempfile → pyarrow CSV → ParquetWriter). The Parquet file IS the
         persistent cache for streaming datasets — we do not keep the
         materialised DataFrame in-process because for ASIC_COMPANIES that
         frame is ~480 MB (pyarrow-backed) and blows past Fly's 512 MB
         worker on its own.
      3. Apply filters at the Arrow level via batched parquet read. Peak
         resident memory per batch: ~80 MB. Matching rows are accumulated
         (typically <1 MB total for register lookups) and converted to a
         small pandas DataFrame.

    Graceful degradation: if the HTTP stream fails and the on-disk Parquet
    exists at all (regardless of TTL), serve from it and set the stale
    signal so `DataResponse.stale` / `.stale_reason` get populated.
    """
    client = await _get_client()
    url = await _resolve_download_url(cd, client)

    # Project to the source columns the curated YAML actually exposes.
    # Skipping unprojected columns at the pyarrow read stage is what makes
    # the 600 MB ASIC_COMPANIES CSV tractable.
    projected_cols = sorted({c.source_column for c in cd.columns.values()})

    cache_key = (
        "streaming-v1",
        url,
        cd.format,
        tuple(projected_cols),
    )
    parquet_path = parquet_cache.path_for(cache_key)

    # If the on-disk parquet is missing or stale, refresh it.
    parquet_fresh = False
    if parquet_path.is_file():
        try:
            age = time.time() - parquet_path.stat().st_mtime
            parquet_fresh = age <= parquet_cache.DEFAULT_TTL_SECONDS
        except OSError:
            parquet_fresh = False

    stale_fallback_in_use = False
    if not parquet_fresh:
        tmp_dir = Path(tempfile.gettempdir()) / "asic-mcp-streaming"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_csv = tmp_dir / (
            hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] + ".csv"
        )
        try:
            try:
                await client.fetch_resource_to_file(url, tmp_csv)
            except ASICAPIError as e:
                # Graceful degradation: parquet exists but is past TTL
                # AND the live fetch failed → serve the stale parquet.
                if parquet_path.is_file():
                    try:
                        age_min = max(
                            0,
                            int((time.time() - parquet_path.stat().st_mtime) / 60),
                        )
                    except OSError:
                        age_min = -1
                    _mark_stale_signal(
                        f"streaming fetch failed ({type(e).__name__}) for "
                        f"{cd.id}; serving stale Parquet cache from "
                        f"~{age_min} minute(s) ago"
                    )
                    stale_fallback_in_use = True
                else:
                    raise ValueError(
                        f"Could not stream dataset {cd.id} from data.gov.au "
                        f"({e}). data.gov.au is the upstream — transient "
                        "5xx / DNS errors usually clear on retry within a "
                        f"few minutes. No cached Parquet available for "
                        f"{cd.id}; try again shortly or visit {cd.source_url}."
                    ) from e

            if not stale_fallback_in_use:
                try:
                    await asyncio.to_thread(
                        stream_csv_to_parquet,
                        tmp_csv,
                        parquet_path,
                        columns=projected_cols,
                    )
                except ParseError as e:
                    raise ValueError(
                        f"Could not parse streamed dataset {cd.id}: {e}. "
                        "The upstream file may have changed shape — flag at "
                        "https://github.com/Bigred97/asic-mcp/issues."
                    ) from e
        finally:
            if tmp_csv.is_file():
                try:
                    tmp_csv.unlink()
                except OSError:
                    pass

    if not parquet_path.is_file():
        raise ValueError(
            f"Internal error: no Parquet cache for {cd.id} at {parquet_path} "
            f"after streaming fetch. Check disk space at "
            f"{parquet_cache.cache_dir()}."
        )

    return await asyncio.to_thread(
        _read_parquet_filtered, parquet_path, cd, filters or {}
    )


def _read_parquet_filtered(
    parquet_path: Path,
    cd: curated.CuratedDataset,
    filters: dict[str, Any],
) -> pd.DataFrame:
    """Chunked Arrow-level filter pushdown on the cached Parquet.

    Iterates the parquet in ~200k-row RecordBatches, building a per-batch
    boolean mask from the user's filters and keeping only matching rows.
    Result is converted to a pandas DataFrame with `string[pyarrow]` dtype
    so downstream shaping stays memory-efficient.

    When `filters` is empty (`latest(ASIC_COMPANIES)` with no narrowing),
    iteration stops after `_STREAMING_HARD_MAX_RECORDS` accumulated rows —
    a full register dump is never the right query shape, and the cap
    matches the convention used by ato/abs sisters.
    """
    import pyarrow as pa
    import pyarrow.parquet as pa_parquet

    pf = pa_parquet.ParquetFile(str(parquet_path))
    matched_batches: list[pa.RecordBatch] = []
    rows_total = 0
    have_filters = bool(filters)

    for batch in pf.iter_batches(batch_size=_STREAMING_BATCH_SIZE):
        if have_filters:
            mask = _build_arrow_filter_mask(batch, cd, filters)
            if mask is None:
                # No applicable filter columns landed in this batch
                # (unlikely — they all land in every batch). Keep
                # nothing; if every batch has no mask, the user's
                # filter is invalid and shaping will surface that.
                continue
            filtered = batch.filter(mask)
            if filtered.num_rows > 0:
                matched_batches.append(filtered)
                rows_total += filtered.num_rows
        else:
            # No filters — accumulate up to the hard cap.
            remaining = _STREAMING_HARD_MAX_RECORDS - rows_total
            if remaining <= 0:
                break
            if batch.num_rows > remaining:
                matched_batches.append(batch.slice(0, remaining))
                rows_total += remaining
                break
            matched_batches.append(batch)
            rows_total += batch.num_rows

    if not matched_batches:
        # Empty result — build an empty frame with the right columns so
        # _apply_aliases doesn't trip on missing source columns.
        empty_schema = pf.schema_arrow
        return pa.Table.from_batches([], schema=empty_schema).to_pandas(
            types_mapper=pd.ArrowDtype
        )

    table = pa.Table.from_batches(matched_batches)
    df = table.to_pandas(types_mapper=pd.ArrowDtype)

    dim_source_cols = [
        c.source_column for c in cd.columns.values() if c.role == "dimension"
    ]
    if dim_source_cols:
        df = drop_blank_rows(df, dim_source_cols)
    return df


def _build_arrow_filter_mask(batch, cd, filters):
    """Build an Arrow boolean mask for a RecordBatch from alias-keyed filters.

    Returns None if no filter matched a known source column on this batch.
    Returns a pyarrow.BooleanArray otherwise (rows that PASS all filters).

    Mirrors shaping._apply_filters semantics:
      - Free-form columns (id-role, or dimension-role without a curated
        `dimension_values` enum) default to case-insensitive substring
        match. ASIC CSVs store names uppercased ('ACME PTY LTD') so a bare
        'acme' must contains-match. Wildcards (`'*foo*'`, `'foo~'`) are
        stripped and treated identically — back-compat for callers that
        adopted the 0.6.x explicit-wildcard convention.
      - Enumerated dimensions (state, status, type) get exact-match
        equality after running through translate_filter_value (so
        `'current'` → `'REGD'`).
      - Lists are treated as OR-of-equalities (deliberate whitelist of
        canonical ids).

    Unknown filter keys are silently skipped here. shaping._apply_filters
    runs again on the small filtered DataFrame and surfaces the
    "Filter X is not a column" / "Did you mean Y?" hints with full UX.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    alias_to_source = {c.key: c.source_column for c in cd.columns.values()}
    contains_keys = {
        c.key
        for c in cd.columns.values()
        if c.role == "id" or (
            c.role == "dimension"
            and (cd.dimension_values.get(c.key) is None
                 or cd.dimension_values[c.key].values is None)
        )
    }

    schema_names = set(batch.schema.names)
    masks = []
    for alias, value in filters.items():
        if alias not in alias_to_source:
            continue
        source_col = alias_to_source[alias]
        if source_col not in schema_names:
            continue
        col = batch[source_col]

        if isinstance(value, list):
            resolved: list[str] = []
            for v in value:
                v_str = str(v).strip()
                if not v_str:
                    continue
                try:
                    resolved.append(
                        str(curated.translate_filter_value(cd, alias, v_str))
                    )
                except ValueError:
                    # Unknown value — let shaping surface the hint.
                    pass
            if resolved:
                masks.append(pc.is_in(col, pa.array(resolved, type=pa.string())))
            continue

        v_str = str(value).strip()
        if alias in contains_keys:
            # Free-form column: default to case-insensitive substring on
            # the source column (which is all-uppercase in ASIC CSVs).
            # Wildcards are stripped — they're a no-op in the bare-contains
            # world but don't break callers who learnt the 0.6.x convention.
            needle = v_str.replace("*", "").replace("~", "").strip()
            if needle:
                masks.append(pc.match_substring(col, needle, ignore_case=True))
            continue

        try:
            resolved_value = curated.translate_filter_value(cd, alias, v_str)
        except ValueError:
            # Unknown alias value — skip the predicate; shaping will
            # report the helpful "did you mean" error on the small
            # filtered (probably empty) DataFrame.
            continue
        masks.append(pc.equal(col, pa.scalar(str(resolved_value))))

    if not masks:
        return None
    mask = masks[0]
    for m in masks[1:]:
        mask = pc.and_(mask, m)
    return mask


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

    df = await _fetch_and_parse(
        cd, kind=cd.cache_kind, filters=filters_d  # type: ignore[arg-type]
    )
    # Streaming-path datasets have already applied filters at the parquet
    # level via Arrow predicate pushdown. Re-running them in shaping is a
    # no-op on the small filtered frame, but we still pass `filters_d`
    # through so the validation + "did you mean" hints fire on bad inputs.
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


async def prewarm_curated(
    dataset_ids: list[str] | None = None,
    *,
    max_concurrency: int = 2,
    log: Any = None,
) -> dict[str, str]:
    """Warm the on-disk Parquet + SQLite cache for curated ASIC datasets with
    bounded concurrency. Designed for gateway / Fly-worker startup.

    The big one is ASIC_COMPANIES — 600 MB streaming download, ~60-90s cold
    convert to Parquet, ~80 MB resident at peak. Warming this at init means
    customer cold-call latency drops from ~90s to sub-200ms.

    Mirrors abs-mcp 0.11.14 / ato-mcp 0.8.21 / apra-mcp 0.8.19 / wgea-mcp
    0.6.11 / aemo-mcp 0.4.14's `prewarm_curated()` signature so gateway init
    hooks can call all five sisters with the same shape.

    Parameters
    ----------
    dataset_ids:
        Curated dataset IDs to warm. Defaults to every curated dataset
        (`curated.list_ids()`). Unknown IDs raise ValueError.
    max_concurrency:
        Semaphore size. Default 2 (sized for 512MB worker). Bump to 4 on
        1 GB+ workers; ASIC_COMPANIES is by far the biggest member so
        even at conc=1 the prewarm completes in ~3 minutes for the full
        suite.
    log:
        Optional callable accepting a single string for progress lines.

    Returns
    -------
    Dict mapping dataset_id → "ok" / "error: ...". Errors are caught
    per-dataset so one failure doesn't abort the rest.
    """
    if dataset_ids is None:
        dataset_ids = curated.list_ids()
    known = set(curated.list_ids())
    unknown = [d for d in dataset_ids if d not in known]
    if unknown:
        raise ValueError(
            f"prewarm_curated received unknown dataset IDs: {unknown}. "
            f"Valid IDs: {sorted(known)}."
        )

    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    results: dict[str, str] = {}

    async def _warm_one(ds_id: str) -> None:
        async with sem:
            if log:
                log(f"[asic-mcp prewarm] warming {ds_id}")
            try:
                # Use latest() — same path the gateway hits. The default
                # limit=50 keeps response size sane while still exercising
                # the full fetch → parse → cache pipeline.
                await latest(dataset_id=ds_id)
                results[ds_id] = "ok"
                if log:
                    log(f"[asic-mcp prewarm] done    {ds_id}")
            except Exception as e:
                msg = f"{type(e).__name__}: {e!s}"[:200]
                results[ds_id] = f"error: {msg}"
                if log:
                    log(f"[asic-mcp prewarm] FAILED  {ds_id}: {msg}")

    await asyncio.gather(*[_warm_one(d) for d in dataset_ids])
    return results


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="asic-mcp",
        description="MCP server for the Australian Securities and Investments Commission registers.",
    )
    parser.add_argument(
        "--warmup",
        action="store_true",
        help=(
            "Warm the curated-dataset cache and exit. Use from gateway "
            "startup hooks to avoid OOM cascade on memory-constrained "
            "workers. Honours --warmup-concurrency (default 2). The big "
            "one is ASIC_COMPANIES (~600 MB streaming download, ~80 MB "
            "resident peak); with --warmup the customer's first call "
            "lands on a warm Parquet cache."
        ),
    )
    parser.add_argument(
        "--warmup-concurrency",
        type=int,
        default=2,
        help="Max parallel dataflow warms when --warmup is set (default 2).",
    )
    parser.add_argument(
        "--warmup-only",
        type=str,
        default=None,
        help=(
            "Comma-separated curated dataset IDs to warm. Defaults to every "
            "curated dataset. Example: --warmup-only ASIC_COMPANIES,ASIC_FINANCIAL_ADVISERS"
        ),
    )
    args = parser.parse_args()

    if args.warmup:
        ds_ids: list[str] | None = None
        if args.warmup_only:
            ds_ids = [s.strip() for s in args.warmup_only.split(",") if s.strip()]
        results = asyncio.run(prewarm_curated(
            ds_ids,
            max_concurrency=args.warmup_concurrency,
            log=lambda m: print(m, file=sys.stderr, flush=True),
        ))
        fails = {k: v for k, v in results.items() if not v.startswith("ok")}
        if fails:
            print(
                f"[asic-mcp prewarm] {len(fails)} dataset(s) failed: {sorted(fails)}",
                file=sys.stderr,
            )
        sys.exit(1 if fails else 0)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
