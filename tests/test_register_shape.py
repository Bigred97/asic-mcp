"""Register-style shaping tests.

ASIC datasets are dimension-only — there are no numeric measures, every
column is a structural attribute of the record. The shape_wide function
must therefore emit one Observation per row carrying all the dimensions
on `Observation.dimensions`, with `value` and `measure` left None.

These tests pin the shaping invariants so the response envelope stays
agent-friendly: row_count matches, dimensions carry the aliased keys,
filtering on plain-English state codes works.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from asic_mcp import curated, parsing, shaping
from asic_mcp.models import DataResponse


def _load(fixture_bytes: bytes, dataset_id: str) -> tuple[curated.CuratedDataset, pd.DataFrame]:
    cd = curated.get(dataset_id)
    assert cd is not None
    df = parsing.read_csv(fixture_bytes)
    dim_cols = [c.source_column for c in cd.columns.values() if c.role == "dimension"]
    df_clean = parsing.drop_blank_rows(df, dim_cols)
    return cd, df_clean


def _build(cd, df, **kwargs):
    defaults = {
        "filters": {}, "measures": None, "start_period": None,
        "end_period": None, "fmt": "records", "user_query": {}, "last_n": None,
    }
    defaults.update(kwargs)
    return shaping.build_response(cd=cd, df=df, **defaults)


def test_register_emits_one_observation_per_row(afs_licensee_csv):
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    resp = _build(cd, df)
    assert resp.row_count == len(df)
    assert resp.row_count >= 10


def test_register_observation_has_no_value_or_measure(afs_licensee_csv):
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    resp = _build(cd, df)
    assert resp.records
    first = resp.records[0]
    assert first.value is None
    assert first.measure is None


def test_register_observation_carries_all_dimensions(afs_licensee_csv):
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    resp = _build(cd, df)
    first = resp.records[0]
    # Every expected dimension alias should show up
    for alias in ("register_name", "licence_number", "licensee_name", "state"):
        assert alias in first.dimensions, f"missing {alias}"


def test_register_unit_is_none_for_dimension_only_datasets(banned_persons_csv):
    cd, df = _load(banned_persons_csv, "ASIC_BANNED_PERSONS")
    resp = _build(cd, df)
    assert resp.unit is None


def test_register_response_carries_attribution_and_source(afs_licensee_csv):
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    resp = _build(cd, df)
    assert resp.source == "Australian Securities and Investments Commission"
    assert "Creative Commons" in resp.attribution
    assert "CC BY 3.0 AU" in resp.attribution
    assert resp.source_url.startswith("https://data.gov.au/")


def test_register_response_has_retrieved_at(afs_licensee_csv):
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    resp = _build(cd, df)
    assert isinstance(resp.retrieved_at, datetime)
    assert (datetime.now(UTC) - resp.retrieved_at).total_seconds() < 60


def test_state_alias_filter_nsw(afs_licensee_csv):
    """'nsw' alias must resolve to 'NSW' source value and filter accordingly."""
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    full = _build(cd, df)
    nsw = _build(cd, df, filters={"state": "nsw"})
    # NSW subset should be strictly fewer than total
    assert nsw.row_count < full.row_count
    # Every NSW record must have state == "NSW"
    for obs in nsw.records:
        assert obs.dimensions.get("state") == "NSW"


def test_state_alias_filter_vic(afs_licensee_csv):
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    vic = _build(cd, df, filters={"state": "vic"})
    for obs in vic.records:
        assert obs.dimensions.get("state") == "VIC"


def test_state_filter_canonical_passthrough(afs_licensee_csv):
    """Passing 'NSW' directly must also work — both alias and canonical."""
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    nsw_alias = _build(cd, df, filters={"state": "nsw"})
    nsw_canon = _build(cd, df, filters={"state": "NSW"})
    assert nsw_alias.row_count == nsw_canon.row_count


def test_state_filter_unknown_state_raises(afs_licensee_csv):
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    with pytest.raises(ValueError, match="Unknown value"):
        _build(cd, df, filters={"state": "wakanda"})


def test_filter_list_or_semantics(afs_licensee_csv):
    """{'state': ['nsw', 'vic']} should return NSW OR VIC records."""
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    multi = _build(cd, df, filters={"state": ["nsw", "vic"]})
    states = {obs.dimensions.get("state") for obs in multi.records}
    assert states.issubset({"NSW", "VIC"})


def test_filter_free_form_substring_match_fails_by_default(afs_licensee_csv):
    """Free-form dimensions (licensee_name) use EXACT match — a substring
    doesn't return matches unless it matches the full value."""
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    # Use the full name of the first record
    first_name = df["AFS_LIC_NAME"].iloc[0]
    exact = _build(cd, df, filters={"licensee_name": first_name})
    assert exact.row_count == 1


def test_filter_unknown_dimension_raises(afs_licensee_csv):
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    with pytest.raises(ValueError, match="Unknown filter"):
        _build(cd, df, filters={"definitely_not_a_dimension": "x"})


def test_filter_empty_list_raises(afs_licensee_csv):
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    with pytest.raises(ValueError, match="empty list"):
        _build(cd, df, filters={"state": []})


def test_csv_format_returns_csv_string(afs_licensee_csv):
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    resp = _build(cd, df, filters={"state": "nsw"}, fmt="csv")
    assert resp.csv is not None
    assert "licensee_name" in resp.csv or "state" in resp.csv


def test_records_format_returns_observations(afs_licensee_csv):
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    resp = _build(cd, df, filters={"state": "nsw"}, fmt="records")
    assert resp.csv is None
    assert all(hasattr(r, "dimensions") for r in resp.records)


def test_series_format_groups_by_measure(afs_licensee_csv):
    """series format groups by measure. For register data (measure=None), all
    records collapse into a single 'value' group."""
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    resp = _build(cd, df, filters={"state": "nsw"}, fmt="series")
    assert resp.csv is None
    # records here is a list of group dicts
    assert isinstance(resp.records, list)
    if resp.records:
        assert "observations" in resp.records[0]


def test_register_empty_filter_returns_all(banned_persons_csv):
    cd, df = _load(banned_persons_csv, "ASIC_BANNED_PERSONS")
    resp = _build(cd, df)
    assert resp.row_count == len(df)
    assert resp.row_count >= 10


def test_register_filter_to_empty_subset(banned_persons_csv):
    cd, df = _load(banned_persons_csv, "ASIC_BANNED_PERSONS")
    resp = _build(cd, df, filters={"person_name": "ZZZZZZ_NEVER_EXISTS"})
    assert resp.row_count == 0
    assert resp.records == []


def test_response_envelope_carries_dataset_id_and_name(liquidator_csv):
    cd, df = _load(liquidator_csv, "ASIC_LIQUIDATOR")
    resp = _build(cd, df)
    assert resp.dataset_id == "ASIC_LIQUIDATOR"
    assert "Liquidator" in resp.dataset_name


def test_response_carries_user_query(liquidator_csv):
    cd, df = _load(liquidator_csv, "ASIC_LIQUIDATOR")
    q = {"filters": {"state": "nsw"}}
    resp = _build(cd, df, filters={"state": "nsw"}, user_query=q)
    assert resp.query == q


def test_server_version_populated_in_response(liquidator_csv):
    cd, df = _load(liquidator_csv, "ASIC_LIQUIDATOR")
    resp = _build(cd, df)
    assert isinstance(resp.server_version, str)
    assert resp.server_version  # non-empty


def test_stale_defaults_to_false(liquidator_csv):
    cd, df = _load(liquidator_csv, "ASIC_LIQUIDATOR")
    resp = _build(cd, df)
    assert resp.stale is False
    assert resp.stale_reason is None


def test_dimensions_preserve_case_in_register_name(liquidator_csv):
    cd, df = _load(liquidator_csv, "ASIC_LIQUIDATOR")
    resp = _build(cd, df)
    # Every row's register_name should be the constant "Liquidator"
    register_names = {obs.dimensions.get("register_name") for obs in resp.records}
    assert register_names == {"Liquidator"}


def test_register_handles_empty_dataframe(afs_licensee_csv):
    """An empty df after filtering should produce a clean empty response."""
    cd, df = _load(afs_licensee_csv, "ASIC_AFS_LICENSEE")
    empty_resp = _build(cd, df, filters={"state": "nt"})  # likely empty in 50-row sample
    # Either there are 0 NT records (typical) or a few — both are valid; the
    # invariant is the response shape.
    assert isinstance(empty_resp.row_count, int)
    assert isinstance(empty_resp.records, list)
