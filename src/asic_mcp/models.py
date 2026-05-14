"""Pydantic v2 response models for asic-mcp.

Mirrors the response shape used by abs-mcp, rba-mcp, apra-mcp, and ato-mcp so
a downstream agent that calls multiple Australian government MCPs gets a
uniform envelope. ASIC-specific differences:
- attribution names ASIC and the data.gov.au Creative Commons licence
- DataResponse.source defaults to "Australian Securities and Investments Commission"
- DataResponse.source_url points back at the data.gov.au dataset page
- Observation.dimensions is open-ended (name, status, state, licence number, etc.)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

_ASIC_ATTRIBUTION = (
    "Source: Australian Securities and Investments Commission, licensed under "
    "Creative Commons Attribution 3.0 Australia (CC BY 3.0 AU) — "
    "https://creativecommons.org/licenses/by/3.0/au/. "
    "Data accessed via data.gov.au. © Commonwealth of Australia."
)


class DatasetSummary(BaseModel):
    """Search-result shape: one row per known ASIC dataset."""
    id: str                                  # asic-mcp curated ID, e.g. "ASIC_FINANCIAL_ADVISERS"
    name: str                                # human name
    description: str | None = None
    update_frequency: str | None = None      # "weekly" / "monthly" / "daily"
    is_curated: bool = False


class ColumnDetail(BaseModel):
    """One queryable column in a curated table."""
    key: str                                 # plain-English alias (e.g. "current_status")
    source_column: str                       # the actual CSV/XLSX header text
    description: str | None = None
    unit: str | None = None                  # "AUD", "Persons", "Count", "Per cent"
    role: str = "measure"                    # "dimension" | "measure" | "id"


class DatasetDetail(BaseModel):
    """describe_dataset shape."""
    id: str
    name: str
    description: str
    is_curated: bool
    update_frequency: str | None = None
    period_coverage: str | None = None       # e.g. "current snapshot (weekly)"
    dimensions: list[ColumnDetail] = Field(default_factory=list)
    measures: list[ColumnDetail] = Field(default_factory=list)
    source_url: str                          # data.gov.au dataset page
    download_url: str | None = None          # the actual XLSX/CSV resource URL


class Observation(BaseModel):
    """One row of returned data."""
    period: str | None = None                # ISO snapshot date ("2026-05-13")
    value: float | None = None               # the measure value (typically counts)
    measure: str | None = None               # which measure this value is for
    dimensions: dict[str, Any] = Field(default_factory=dict)  # name, status, state, licence_no, etc.
    unit: str | None = None


class DataResponse(BaseModel):
    """get_data / latest shape — uniform across all curated datasets.

    `records` carries either:
      - list of `Observation` (default "records" format), or
      - list of dicts shaped {measure, unit, observations: [{period, value, dimensions}, ...]}
        (the "series" format — one group per measure).
    We use `Any` here instead of a union so Pydantic does not silently coerce
    the series dicts into Observations (every Observation field is optional,
    so the dicts would otherwise match and `observations` would be dropped).
    """
    dataset_id: str
    dataset_name: str
    query: dict[str, Any] = Field(default_factory=dict)
    period: dict[str, str | None] = Field(default_factory=lambda: {"start": None, "end": None})
    unit: str | None = None
    row_count: int = 0
    records: list[Any] = Field(default_factory=list)
    csv: str | None = None
    source: str = "Australian Securities and Investments Commission"
    attribution: str = _ASIC_ATTRIBUTION
    retrieved_at: datetime
    source_url: str
    # Echoed in every response so testers can verify which wheel served the call;
    # uvx caches per-version and stale caches cause real "is this fixed?" confusion.
    server_version: str = Field(default_factory=lambda: _get_server_version())
    # Set when the data backing this response is older than the dataset's
    # expected refresh cadence (weekly registers > 14d, monthly > 45d). Agents
    # should surface this to end-users when stale=True.
    stale: bool = False
    stale_reason: str | None = None
    # Set when `latest()` truncated a large register response to the `limit`
    # default. Original row count goes here; the truncation prevents
    # 100k+ rows from blowing an agent's context window. Agents should
    # surface this so users know to add filters or pass a larger `limit`.
    truncated_at: int | None = None


def _get_server_version() -> str:
    try:
        from importlib.metadata import version
        return version("asic-mcp")
    except Exception:
        return "0.0.0+unknown"
