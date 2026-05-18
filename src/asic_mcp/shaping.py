"""Translate a parsed DataFrame into the public DataResponse shape.

Wraps two layout-specific transforms:
  - `shape_wide`:        one-row-per-entity tables (Individuals T6, ACNC register,
                         corporate transparency). Each (row, measure) cell becomes
                         one Observation.
  - `shape_transposed`:  metric-rows × year-columns tables (GST T1, SuperFunds T1).
                         Each (metric, period) cell becomes one Observation, with
                         a `period` field carried through.

In both cases, the public API exposes plain-English column aliases (e.g.
`median_taxable_income`) instead of ATO's verbose source headers. Filtering
happens on aliases too.
"""
from __future__ import annotations

import difflib
import math
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from .curated import (
    CuratedDataset,
    dimension_columns,
    id_columns,
    measure_columns,
    resolve_measure_keys,
    translate_filter_value,
)
from .models import DataResponse, Observation

# Per-dataset bloat-trim rules for free-text fields whose source values run
# multiple KB per record (license boilerplate, ban conditions, etc.). Without
# truncation a single ASIC_AFS_LICENSEE record can run 2-3 KB just for the
# `authorisation` field, blowing the portfolio's <10k-token response budget
# on the very first row. Callers who genuinely need the full text opt in via
# the tool-level `include_full_authorisation=True` kwarg, which propagates
# through `_get_data_impl` into this module's `build_response`.
_AUTHORISATION_TRUNCATE_CHARS = 200
_TRUNCATION_SUFFIX = (
    "...[truncated, use include_full_authorisation=true to see full text]"
)
# (dataset_id, dimension_key) → max chars for the auto-truncate behaviour.
_TRUNCATE_RULES: dict[tuple[str, str], int] = {
    ("ASIC_AFS_LICENSEE", "authorisation"): _AUTHORISATION_TRUNCATE_CHARS,
}


def _truncate_record_text(
    record: Observation, rules: dict[str, int]
) -> None:
    """Truncate long free-text dimension values on a single Observation in place.

    `rules` maps {dimension_key: max_chars}. Values are only truncated when
    they exceed `max_chars`; shorter values pass through unchanged so the
    suffix never appears unnecessarily.
    """
    for dim_key, max_chars in rules.items():
        v = record.dimensions.get(dim_key)
        if not isinstance(v, str) or len(v) <= max_chars:
            continue
        record.dimensions[dim_key] = v[:max_chars] + _TRUNCATION_SUFFIX


def _safe_value(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def _safe_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return str(v)


def _apply_aliases(df: pd.DataFrame, cd: CuratedDataset) -> pd.DataFrame:
    """Rename source columns to their curated aliases.

    For wide layouts: drop columns not in the curated set so the response
    stays tight (we only return columns the YAML promises).
    For transposed layouts: preserve unaliased columns — they're the period
    columns (years/months) which carry the actual data values and aren't
    enumerable in the YAML.
    """
    rename_map: dict[str, str] = {}
    for col in cd.columns.values():
        if col.source_column in df.columns:
            rename_map[col.source_column] = col.key
    missing = [
        c.source_column for c in cd.columns.values() if c.source_column not in df.columns
    ]
    if missing:
        raise ValueError(
            f"Dataset {cd.id!r} expected these columns but they were not in the "
            f"parsed table: {missing[:5]}{'...' if len(missing) > 5 else ''}. "
            "The upstream file may have changed shape — flag at "
            "https://github.com/Bigred97/asic-mcp/issues."
        )
    out = df.rename(columns=rename_map)
    if cd.layout == "wide":
        # Wide: drop unaliased columns; we only ship curated ones.
        keep = [c.key for c in cd.columns.values() if c.key in out.columns]
        return out[keep].copy()
    # Transposed: keep period columns intact alongside the renamed metric/unit.
    return out.copy()


def _coerce_dtypes(df: pd.DataFrame, cd: CuratedDataset) -> pd.DataFrame:
    """Apply per-column dtype hints from the curated YAML.

    Date columns (declared `dtype: date` OR auto-detected by `*_date` name
    suffix) are normalised from ASIC's dd/mm/yyyy source format to ISO
    YYYY-MM-DD — matches the portfolio "Cross-Agency Joining" period
    convention so customers can join ASIC dates with ABS/RBA/ATO dates.
    """
    for col in cd.columns.values():
        if col.key not in df.columns:
            continue
        try:
            if col.dtype in ("int", "integer"):
                df[col.key] = pd.to_numeric(df[col.key], errors="coerce").astype("Int64")
            elif col.dtype in ("float", "number"):
                df[col.key] = pd.to_numeric(df[col.key], errors="coerce")
            elif col.dtype in ("string", "str"):
                df[col.key] = _to_clean_string(df[col.key])
                # After string coercion: auto-normalise *_date columns from
                # dd/mm/yyyy to ISO YYYY-MM-DD if the source data matches.
                if col.key.endswith("_date") or col.dtype == "date":
                    df[col.key] = _normalize_ddmmyyyy_to_iso(df[col.key])
            elif col.dtype == "date":
                df[col.key] = _normalize_ddmmyyyy_to_iso(_to_clean_string(df[col.key]))
        except (ValueError, TypeError):
            # Lenient — coercion failures fall through; the data ships
            # in whatever dtype pandas inferred originally.
            pass
    return df


def _normalize_ddmmyyyy_to_iso(series: pd.Series) -> pd.Series:
    """Convert ASIC dd/mm/yyyy date strings to ISO YYYY-MM-DD.

    Leaves missing values (<NA>) and already-ISO values untouched. ASIC
    publishes all register dates in DD/MM/YYYY format; the portfolio
    standard is ISO YYYY-MM-DD for cross-source joins. Non-parseable
    values pass through unchanged (lenient).
    """
    # Use pandas datetime parser with the explicit dd/mm/yyyy format,
    # then re-format to ISO. errors='coerce' converts unparseable to NaT
    # which we then re-merge with the original string (preserving
    # already-ISO values like '2024-08-08' or sentinels like 'CURRENT').
    parsed = pd.to_datetime(series, format="%d/%m/%Y", errors="coerce")
    iso = parsed.dt.strftime("%Y-%m-%d")
    # Where parsing succeeded, use ISO; elsewhere keep the original string.
    return iso.where(parsed.notna(), series)


def _to_clean_string(series: pd.Series) -> pd.Series:
    """Coerce a column to pandas StringDtype, clean of formatting noise.

    Two transforms:
    1. Digit-only columns (ABN, postcode) come from pandas as floats; plain
       `.astype('string')` yields '94600082111.0'. We route whole-number
       floats through Int64 first to drop the trailing '.0'.
    2. Text columns (state, charity_size) often arrive with trailing/leading
       whitespace from the source file ('NT ' instead of 'NT'). We strip
       once here so every downstream filter comparison sees the clean form.
    """
    if pd.api.types.is_numeric_dtype(series):
        rounded = series.dropna()
        if not rounded.empty and (rounded.astype("float64") % 1 == 0).all():
            return series.astype("Int64").astype("string")
    return series.astype("string").str.strip()


def _apply_filters(
    df: pd.DataFrame, cd: CuratedDataset, filters: dict[str, Any]
) -> pd.DataFrame:
    """Filter rows by user-supplied dimension values.

    For each filter, translate the value via the curated dimension_values map
    (if any), then match against the renamed dimension column.
    """
    if not filters:
        return df

    valid_dim_keys = {c.key for c in cd.columns.values() if c.role in ("dimension", "id")}
    # id-role columns are free-form (entity names, ABNs, licence numbers) —
    # customers querying by name typically want case-insensitive substring
    # matching ('commonwealth bank' → COMMONWEALTH BANK OF AUSTRALIA), not
    # exact match. Wildcards ('commonwealth*', '*bank*', 'commonwealth~')
    # opt into contains-style matching; bare id values stay exact for
    # backward compat.
    id_role_keys = {c.key for c in cd.columns.values() if c.role == "id"}
    out = df
    for user_key, user_val in filters.items():
        if user_key not in valid_dim_keys:
            valid = sorted(valid_dim_keys)
            close = difflib.get_close_matches(user_key, valid, n=1, cutoff=0.6)
            hint = f"Did you mean {close[0]!r}? " if close else ""
            raise ValueError(
                f"Filter {user_key!r} is not a column on {cd.id}. "
                f"{hint}"
                f"Valid filters: {', '.join(valid[:10])}"
                f"{'...' if len(valid) > 10 else ''}. "
                f"Use the describe endpoint or describe tool to see all filter columns for {cd.id!r}."
            )
        # Lists mean "OR" across values.
        if isinstance(user_val, list):
            if not user_val:
                raise ValueError(
                    f"Filter {user_key!r} has an empty list. "
                    "Pass at least one value, or omit the filter."
                )
            resolved = [translate_filter_value(cd, user_key, str(v).strip()) for v in user_val]
            mask = out[user_key].astype("string").isin(resolved)
            unresolved_value = ", ".join(str(v).strip() for v in user_val)
        else:
            v_str = str(user_val).strip()
            # Wildcard substring match for id-role columns (company_name,
            # licensee_name, adviser_number, etc.). Matches apra-mcp's
            # permissive wildcard pattern. Trailing '*', leading '*', or '~'
            # all signal "substring contains, case-insensitive".
            is_wildcard = (
                user_key in id_role_keys
                and (v_str.endswith("*") or v_str.startswith("*") or "~" in v_str)
            )
            if is_wildcard:
                needle = v_str.replace("*", "").replace("~", "").strip()
                if not needle:
                    raise ValueError(
                        f"Filter {user_key!r}: wildcard value reduced to empty "
                        "after stripping '*' / '~'. Pass a substring to match, "
                        f"e.g. {{{user_key!r}: 'commonwealth*'}} or "
                        f"{{{user_key!r}: 'macquarie~'}}."
                    )
                mask = out[user_key].astype("string").str.contains(
                    needle, case=False, na=False, regex=False,
                )
                unresolved_value = v_str
            else:
                resolved = translate_filter_value(cd, user_key, v_str)
                # Use string comparison so postcode (numeric in source) and "2600" both match.
                mask = out[user_key].astype("string") == str(resolved)
                unresolved_value = v_str
        next_out = out.loc[mask]
        # High-confidence "Did you mean?" for free-form dim typos.
        # Cutoff 0.7 strict; matches ato-mcp 0.8.13 / aihw-mcp 0.4.13.
        if next_out.empty and not out.empty:
            dv = cd.dimension_values.get(user_key)
            has_enum = dv is not None and dv.values
            if not has_enum:
                actual_values = out[user_key].dropna().astype(str).unique().tolist()
                # Year-shaped dims get range hints instead of fuzzy
                # suggestions ('2022' → '2002' is misleading).
                user_int = None
                if unresolved_value.isdigit() and len(unresolved_value) == 4:
                    try:
                        user_int = int(unresolved_value)
                    except ValueError:
                        pass
                actual_ints: list[int] = []
                if user_int is not None:
                    for v in actual_values:
                        if v.isdigit() and len(v) == 4:
                            try:
                                actual_ints.append(int(v))
                            except ValueError:
                                continue
                if user_int is not None and actual_ints and user_int not in actual_ints:
                    lo, hi = min(actual_ints), max(actual_ints)
                    direction = (
                        f"requested {user_int} is BEFORE the earliest published year"
                        if user_int < lo else
                        f"requested {user_int} is AFTER the latest published year"
                    )
                    raise ValueError(
                        f"No data for {user_key}={user_int} on dataset {cd.id!r}. "
                        f"Valid range: {lo}-{hi} — {direction}. "
                        f"Use the describe endpoint or describe tool to see the full value list on {cd.id!r}."
                    )
                suggestion = difflib.get_close_matches(
                    unresolved_value, actual_values, n=3, cutoff=0.7
                )
                if suggestion:
                    others = (
                        f" Other close matches: {', '.join(repr(s) for s in suggestion[1:])}."
                        if len(suggestion) > 1
                        else ""
                    )
                    raise ValueError(
                        f"No matches for {unresolved_value!r} in {user_key!r} on dataset {cd.id!r}. "
                        f"Did you mean {suggestion[0]!r}?{others} "
                        f"Use the describe endpoint or describe tool for the full value list on {cd.id!r}."
                    )
        out = next_out
    return out.reset_index(drop=True)


def shape_wide(
    df: pd.DataFrame,
    cd: CuratedDataset,
    measures: list[str],
) -> list[Observation]:
    """One Observation per (row, measure) cell in a wide layout.

    Each row carries every dimension (renamed to its alias) on the
    observation. Multiple measures requested means multiple observations
    per source row — one per measure.

    Register-style datasets (the ASIC suite) have no measure columns —
    every field is dimensional. In that case we emit exactly one
    Observation per row carrying all the dimensions, with `value` and
    `measure` left null. Downstream agents read the row content from
    `dimensions` directly.
    """
    if df.empty:
        return []
    dims = [c.key for c in dimension_columns(cd)]
    ids = [c.key for c in id_columns(cd)]
    dim_keys = dims + ids
    measure_by_key = {c.key: c for c in measure_columns(cd)}

    records: list[Observation] = []
    for _, row in df.iterrows():
        dim_vals: dict[str, Any] = {}
        for k in dim_keys:
            raw = row[k]
            v = _safe_str(raw)
            if v is not None:
                dim_vals[k] = v
        if not measures:
            # Dimension-only (register) dataset. Skip rows that have no
            # dimension content at all — they're artefacts of trailing blanks.
            if not dim_vals:
                continue
            records.append(
                Observation(
                    period=None,
                    value=None,
                    measure=None,
                    dimensions=dim_vals,
                    unit=None,
                )
            )
            continue
        for mk in measures:
            mc = measure_by_key.get(mk)
            if mc is None:
                continue
            cell = row[mk] if mk in df.columns else None
            value = _safe_value(cell)
            if value is None:
                continue
            records.append(
                Observation(
                    period=None,
                    value=value,
                    measure=mk,
                    dimensions=dim_vals,
                    unit=mc.unit,
                )
            )
    return records


def shape_transposed(
    df: pd.DataFrame,
    cd: CuratedDataset,
    measures: list[str],
    start_period: str | None,
    end_period: str | None,
) -> list[Observation]:
    """One Observation per (metric, period) cell in a transposed layout.

    The metric label sits in `metric_label_column` (typically column A).
    A unit string may sit in `unit_column` (typically column B). Every
    remaining column is a period (year or month).

    `measures` here is interpreted as a filter on the metric label — only
    rows whose `metric_label_column` value matches one of `measures` (or
    its declared alias) come through. If `measures` is empty, all rows
    are returned.
    """
    if df.empty:
        return []
    if cd.metric_label_column is None:
        raise ValueError(
            f"Dataset {cd.id!r} declares layout='transposed' but no "
            "metric_label_column — fix the curated YAML."
        )

    # The YAML's metric_label_column / unit_column refer to source columns.
    # _apply_aliases has since renamed them to their curated keys, so resolve
    # the alias here before any df access.
    label_alias: str | None = None
    unit_alias: str | None = None
    label_curated = None
    for c in cd.columns.values():
        if c.source_column == cd.metric_label_column:
            label_alias = c.key
            label_curated = c
        if cd.unit_column and c.source_column == cd.unit_column:
            unit_alias = c.key
    if label_alias is None:
        raise ValueError(
            f"Dataset {cd.id!r} declares metric_label_column "
            f"{cd.metric_label_column!r} but no curated column matches that "
            "source_column — fix the YAML so the metric label has a `columns:` entry."
        )
    label_col = label_alias
    unit_col = unit_alias

    # ATO ships some metric labels with stray whitespace (e.g. "Net GST ").
    # Normalize once so both filter matching and the response display use
    # clean values, and YAML aliases don't have to mirror typos.
    if label_col in df.columns:
        df = df.copy()
        df[label_col] = df[label_col].astype("string").str.strip()

    period_cols = [
        c for c in df.columns
        if c != label_col and c != unit_col
    ]
    if start_period or end_period:
        period_cols = [
            c for c in period_cols
            if _period_in_range(str(c), start_period, end_period)
        ]

    # Match measures against the metric label column. Build an alias->canonical
    # map from the curated `dimension_values` for the metric label column.
    metric_alias_map: dict[str, str] | None = None
    if label_curated is not None:
        dv = cd.dimension_values.get(label_curated.key)
        if dv is not None and dv.values is not None:
            metric_alias_map = dv.values

    if measures:
        wanted_labels: set[str] = set()
        for m in measures:
            if metric_alias_map and m in metric_alias_map:
                wanted_labels.add(metric_alias_map[m])
            else:
                wanted_labels.add(m)
        filtered = df[df[label_col].astype("string").isin(wanted_labels)]
    else:
        filtered = df

    records: list[Observation] = []
    for _, row in filtered.iterrows():
        label = _safe_str(row[label_col])
        if label is None:
            continue
        unit = _safe_str(row[unit_col]) if unit_col and unit_col in df.columns else None
        # If we have a reverse map, prefer to surface the curated alias.
        display_metric = label
        if metric_alias_map:
            reverse = {v: k for k, v in metric_alias_map.items()}
            display_metric = reverse.get(label, label)
        for period_col in period_cols:
            value = _safe_value(row[period_col])
            if value is None:
                continue
            records.append(
                Observation(
                    period=str(period_col),
                    value=value,
                    measure=display_metric,
                    dimensions={"metric_source_label": label},
                    unit=unit,
                )
            )
    return records


def _period_in_range(p: str, start: str | None, end: str | None) -> bool:
    """Lenient period-in-range check.

    ATO periods come in many shapes: 'YYYY' (2024), 'YYYY-YY' (2022-23),
    'YYYY-MM-DD HH:MM:SS' (Excel datetime stringified for monthly tables),
    'YYYY-MM' (rare). We extract YYYY or YYYY-MM and compare lexicographically.
    Free-form / unparseable periods always pass through.

    Boundary semantic: `end_period="2024"` against a monthly source must INCLUDE
    all of 2024-NN. A naive string compare `"2024-06" > "2024"` returns True
    (excluding June), so we right-pad shorter end normalisations to the
    widest match in their granularity (e.g. "2024" → "2024-12" when the
    period under test has a month component).
    """
    norm = _normalize_period(p)
    if norm is None:
        return True
    if start:
        ns = _normalize_period(start)
        if ns is not None and norm < ns:
            return False
    if end:
        ne = _normalize_period(end)
        if ne is not None:
            # If end is year-only but the period has a month, treat end as
            # the LAST month of that year so 2024-NN ≤ 2024 means "any month
            # within or before 2024".
            if len(ne) == 4 and len(norm) > 4:
                ne = ne + "-99"
            if norm > ne:
                return False
    return True


def _normalize_period(p: str) -> str | None:
    """Return YYYY or YYYY-MM for comparison, or None if we can't parse it.

    Disambiguation rule for the `YYYY-NN` shape:
      - if NN is 01-12, it's a month  → return 'YYYY-MM'
      - if NN is 13-99, it's the YY suffix of an ATO financial year ('2022-23')
        → return the starting year 'YYYY'
      - if NN is 00 or non-numeric, fall through
    """
    s = p.strip()
    if not s:
        return None
    # 'YYYY-NN' — could be monthly or financial-year. Distinguish by NN.
    if len(s) == 7 and s[4] == "-" and s[:4].isdigit() and s[5:].isdigit():
        try:
            suffix = int(s[5:])
        except ValueError:
            return None
        if 1 <= suffix <= 12:
            return s  # YYYY-MM (month)
        if 13 <= suffix <= 99:
            return s[:4]  # YYYY-YY financial year — use start year
        return None  # NN = 00 isn't a real period
    # 'YYYY-MM-DD HH:MM:SS' (Excel datetime ISO) — take YYYY-MM.
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:7]
    # 'YYYY'.
    if len(s) == 4 and s.isdigit():
        return s
    return None


def records_to_csv(records: list[Observation]) -> str:
    """Flatten Observations to a CSV string. Header is union of all keys."""
    if not records:
        return ""
    # Collect all dim keys across records (some rows may omit some dims).
    dim_keys: list[str] = []
    seen: set[str] = set()
    for r in records:
        for k in r.dimensions:
            if k not in seen:
                seen.add(k)
                dim_keys.append(k)
    cols = ["period", "measure", "value", "unit", *dim_keys]
    df = pd.DataFrame(
        [
            {
                "period": r.period,
                "measure": r.measure,
                "value": r.value,
                "unit": r.unit,
                **{k: r.dimensions.get(k) for k in dim_keys},
            }
            for r in records
        ],
        columns=cols,
    )
    return df.to_csv(index=False)


def records_to_series(records: list[Observation]) -> list[dict[str, Any]]:
    """Group records by measure into [{measure, unit, observations: [...]}]."""
    groups: dict[str, dict[str, Any]] = {}
    for r in records:
        key = r.measure or "value"
        g = groups.setdefault(key, {"measure": key, "unit": r.unit, "observations": []})
        g["observations"].append(
            {
                "period": r.period,
                "value": r.value,
                "dimensions": r.dimensions,
            }
        )
    return list(groups.values())


def build_response(
    *,
    cd: CuratedDataset,
    df: pd.DataFrame,
    filters: dict[str, Any],
    measures: str | list[str] | None,
    start_period: str | None,
    end_period: str | None,
    fmt: str,
    user_query: dict[str, Any],
    last_n: int | None = None,
    include_full_authorisation: bool = False,
) -> DataResponse:
    """The single entrypoint shaping uses to build a DataResponse from a parsed df.

    Steps:
    1. Rename source columns → curated aliases.
    2. Coerce dtypes per curated hints.
    3. Apply user filters on dimensions.
    4. Resolve `measures` to a list of measure keys.
    5. Shape per layout (wide vs transposed).
    6. Optionally trim to last_n records per measure.
    7. Apply per-dataset bloat-trim rules (e.g. ASIC_AFS_LICENSEE's
       `authorisation` field — 2-3 KB of license boilerplate per record —
       truncated to ~200 chars unless `include_full_authorisation=True`).
    8. Emit in the requested format (records / series / csv).
    """
    renamed = _apply_aliases(df, cd)
    coerced = _coerce_dtypes(renamed, cd)
    filtered = _apply_filters(coerced, cd, filters)

    measure_keys = resolve_measure_keys(cd, measures)

    if cd.layout == "wide":
        records = shape_wide(filtered, cd, measure_keys)
    else:
        records = shape_transposed(filtered, cd, measure_keys, start_period, end_period)

    if last_n is not None and last_n > 0 and records:
        # last_n per measure — keep the MOST RECENT N observations per measure.
        # Sort by normalised period ascending first, so `tail` always selects
        # the freshest values regardless of source-file row order (the SMSF
        # overview lists years descending; the GST monthly table lists them
        # ascending — both need to land on "newest" when last_n=1).
        #
        # SKIP the trim entirely if every record has a null period — that's
        # the wide-layout case (single-year tables like IND_POSTCODE_MEDIAN).
        # Trimming there would arbitrarily pick one row per measure based on
        # iteration order, which is never what the caller wants. `latest()`
        # on a wide dataset == get_data() (same filter, same shape).
        if all(r.period is None for r in records):
            pass  # no trim
        else:
            per_measure: dict[str, list[Observation]] = {}
            for r in records:
                per_measure.setdefault(r.measure or "", []).append(r)
            records = []
            for k, group in per_measure.items():
                group_sorted = sorted(
                    group,
                    key=lambda r: _normalize_period(r.period or "") or "",
                )
                records.extend(group_sorted[-last_n:])

    # Per-dataset bloat-trim: truncate verbose free-text dimension values
    # (e.g. ASIC_AFS_LICENSEE's `authorisation`) unless the caller opted in
    # to the full text. Done in-place on `records` so the same trimming
    # applies regardless of output format (records / series / csv).
    if not include_full_authorisation:
        active_rules = {
            dim_key: max_chars
            for (ds_id, dim_key), max_chars in _TRUNCATE_RULES.items()
            if ds_id == cd.id
        }
        if active_rules:
            for rec in records:
                _truncate_record_text(rec, active_rules)

    response_unit: str | None = None
    if records:
        units = {r.unit for r in records if r.unit}
        if len(units) == 1:
            response_unit = next(iter(units))

    period_start = start_period
    period_end = end_period
    if (period_start is None or period_end is None) and records:
        periods = sorted({r.period for r in records if r.period})
        if periods:
            period_start = period_start or periods[0]
            period_end = period_end or periods[-1]

    if fmt == "csv":
        out_records: list[Observation] | list[dict[str, Any]] = []
        csv_text: str | None = records_to_csv(records)
    elif fmt == "series":
        out_records = records_to_series(records)
        csv_text = None
    else:  # records
        out_records = records
        csv_text = None

    return DataResponse(
        dataset_id=cd.id,
        dataset_name=cd.name,
        query=user_query,
        period={"start": period_start, "end": period_end},
        unit=response_unit,
        row_count=len(records),
        records=out_records,
        csv=csv_text,
        retrieved_at=datetime.now(UTC),
        source_url=cd.source_url,
    )
