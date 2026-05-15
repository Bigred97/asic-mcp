"""Curated YAML loader contract tests for asic-mcp.

These hit the actual YAMLs shipped with the package — if anyone breaks one,
this suite catches it before the wheel ships. ASIC registers are dimension-
only (no measure columns), so the contract is: every dataset must declare at
least one dimension column, every dimension_values entry must reference a
real column key, every column key must be unique within a dataset.
"""
from __future__ import annotations

import pytest

from asic_mcp import curated


def test_at_least_seven_curated_datasets_load():
    ids = curated.list_ids()
    assert len(ids) >= 7, f"expected at least 7 curated datasets, got {ids}"


def test_every_curated_dataset_has_required_fields():
    for cd in curated.list_all():
        assert cd.id, f"missing id in {cd}"
        assert cd.name, f"missing name on {cd.id}"
        assert cd.description, f"missing description on {cd.id}"
        assert cd.source_url.startswith("https://"), f"bad source_url on {cd.id}: {cd.source_url}"
        assert cd.download_url.startswith("https://"), f"bad download_url on {cd.id}: {cd.download_url}"
        assert cd.format in ("xlsx", "csv"), f"bad format on {cd.id}: {cd.format}"
        if cd.format == "xlsx":
            assert cd.sheet, f"xlsx dataset {cd.id} missing sheet name"
        assert cd.header_row >= 1, f"bad header_row on {cd.id}"
        assert cd.layout in ("wide", "transposed"), f"bad layout on {cd.id}"
        # ASIC register datasets are dimension-only. The contract is just
        # "has at least one dimension column".
        roles = {c.role for c in cd.columns.values()}
        assert "dimension" in roles or "id" in roles, (
            f"{cd.id} declares no dimensions or ids"
        )


def test_every_curated_dataset_has_asic_prefix():
    """v0.1 ID convention — every curated dataset starts with ASIC_."""
    for cd in curated.list_all():
        assert cd.id.startswith("ASIC_"), f"non-conforming id: {cd.id!r}"


def test_every_curated_dataset_is_weekly_or_monthly():
    """ASIC register datasets refresh weekly or monthly — no other cadences in v0.1."""
    for cd in curated.list_all():
        assert cd.update_frequency in ("weekly", "monthly"), (
            f"{cd.id} unexpected update_frequency {cd.update_frequency!r}"
        )


def test_every_curated_dataset_has_discovery_block():
    """All v0.1 datasets use CKAN discovery so URL refresh is automatic."""
    for cd in curated.list_all():
        assert cd.discovery is not None, f"{cd.id} has no discovery block"
        assert "package_id" in cd.discovery, f"{cd.id} discovery missing package_id"


def test_every_curated_dataset_cache_kind_is_register():
    """Register data uses the 24h `register` cache TTL, not 7-day `data`."""
    for cd in curated.list_all():
        assert cd.cache_kind == "register", (
            f"{cd.id} cache_kind {cd.cache_kind!r} — registers should be 'register'"
        )


def test_no_duplicate_curated_ids():
    ids = curated.list_ids()
    assert len(ids) == len(set(ids)), f"duplicate IDs in curated registry: {ids}"


def test_column_keys_are_unique_within_dataset():
    for cd in curated.list_all():
        keys = [c.key for c in cd.columns.values()]
        assert len(keys) == len(set(keys)), f"duplicate column keys in {cd.id}: {keys}"


def test_source_columns_are_unique_within_dataset():
    """Two YAML columns can't map to the same source CSV column."""
    for cd in curated.list_all():
        sources = [c.source_column for c in cd.columns.values()]
        assert len(sources) == len(set(sources)), (
            f"duplicate source_columns in {cd.id}: {sources}"
        )


def test_dimension_values_reference_real_columns():
    """Every dimension_values entry must reference a dimension column key."""
    for cd in curated.list_all():
        col_keys = {c.key for c in cd.columns.values()}
        for dim_key in cd.dimension_values:
            assert dim_key in col_keys, (
                f"{cd.id}: dimension_values entry {dim_key!r} doesn't match any column"
            )


def test_state_dimension_consistent_across_datasets():
    """Every register that exposes 'state' must alias the 8 Australian codes."""
    canonical_states = {"NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"}
    for cd in curated.list_all():
        if "state" in cd.dimension_values:
            dv = cd.dimension_values["state"]
            assert dv.values is not None
            mapped = set(dv.values.values())
            missing = canonical_states - mapped
            assert not missing, (
                f"{cd.id}: state mapping missing {sorted(missing)}"
            )


def test_register_name_is_constant_column_in_every_dataset():
    """Most ASIC register CSVs have a REGISTER_NAME column we expose as register_name.
    Exception: ASIC_COMPANIES uses a different CSV schema without REGISTER_NAME."""
    no_register_name = {"ASIC_COMPANIES"}
    for cd in curated.list_all():
        if cd.id in no_register_name:
            continue
        assert "register_name" in cd.columns, (
            f"{cd.id} missing register_name column"
        )


def test_translate_filter_value_for_known_alias():
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    out = curated.translate_filter_value(cd, "state", "nsw")
    assert out == "NSW"


def test_translate_filter_value_passthrough_canonical():
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    out = curated.translate_filter_value(cd, "state", "NSW")
    assert out == "NSW"


def test_translate_filter_value_unknown_raises():
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    with pytest.raises(ValueError, match="Unknown value"):
        curated.translate_filter_value(cd, "state", "wakanda")


# ---- aus-identity cross-source normalisation on state filter ----


def test_state_filter_accepts_full_name():
    """`state='New South Wales'` resolves to canonical 'NSW'."""
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    assert curated.translate_filter_value(cd, "state", "New South Wales") == "NSW"


def test_state_filter_accepts_lowercase_full_name():
    """`state='queensland'` (lowercase) resolves to 'QLD'."""
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    assert curated.translate_filter_value(cd, "state", "queensland") == "QLD"


def test_state_filter_accepts_iso_3166_form():
    """`state='AU-VIC'` resolves to 'VIC'."""
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    assert curated.translate_filter_value(cd, "state", "AU-VIC") == "VIC"


def test_state_filter_accepts_common_alias():
    """`state='Tassie'` resolves to 'TAS'."""
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    assert curated.translate_filter_value(cd, "state", "Tassie") == "TAS"


def test_state_filter_accepts_postcode_routing():
    """`state='2000'` (Sydney CBD) routes to 'NSW'."""
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    assert curated.translate_filter_value(cd, "state", "2000") == "NSW"


def test_state_filter_postcode_in_act_routes_correctly():
    """`state='2600'` (Parliament House) resolves to 'ACT', not 'NSW'."""
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    assert curated.translate_filter_value(cd, "state", "2600") == "ACT"


def test_state_filter_credit_licensee_accepts_full_name():
    """Verify aus_identity works across multiple ASIC registers."""
    cd = curated.get("ASIC_CREDIT_LICENSEE")
    assert cd is not None
    assert curated.translate_filter_value(cd, "state", "Victoria") == "VIC"


def test_translate_filter_value_freeform_dimension_passes_through():
    """Dimensions without dimension_values map (e.g. licensee_name) pass through."""
    cd = curated.get("ASIC_AFS_LICENSEE")
    assert cd is not None
    out = curated.translate_filter_value(cd, "licensee_name", "Westpac Banking Corporation")
    assert out == "Westpac Banking Corporation"


def test_get_dataset_case_insensitive():
    cd_upper = curated.get("ASIC_FINANCIAL_ADVISERS")
    cd_lower = curated.get("asic_financial_advisers")
    assert cd_upper is not None
    assert cd_lower is not None
    assert cd_upper.id == cd_lower.id


def test_get_unknown_dataset_returns_none():
    assert curated.get("DEFINITELY_NOT_A_DATASET") is None


def test_resolve_measure_keys_register_data_returns_empty():
    """ASIC register YAMLs have no measure columns. measures=None → []."""
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    out = curated.resolve_measure_keys(cd, None)
    assert out == []


def test_resolve_measure_keys_empty_list_raises():
    cd = curated.get("ASIC_BANNED_PERSONS")
    assert cd is not None
    with pytest.raises(ValueError, match="empty list"):
        curated.resolve_measure_keys(cd, [])


def test_resolve_measure_keys_unknown_raises():
    cd = curated.get("ASIC_LIQUIDATOR")
    assert cd is not None
    with pytest.raises(ValueError, match="Unknown measure"):
        curated.resolve_measure_keys(cd, "alien_metric")


def test_list_ids_returns_sorted():
    ids = curated.list_ids()
    assert ids == sorted(ids), "curated.list_ids() must return sorted output"


def test_list_all_returns_sorted_by_id():
    cds = curated.list_all()
    ids = [cd.id for cd in cds]
    assert ids == sorted(ids), "curated.list_all() must return sorted output"


def test_dimension_columns_helper():
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    dims = curated.dimension_columns(cd)
    assert dims, "financial advisers should have dimension columns"
    assert all(c.role == "dimension" for c in dims)


def test_id_columns_helper():
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    ids = curated.id_columns(cd)
    assert ids, "financial advisers should have an id column (adviser_number)"
    assert all(c.role == "id" for c in ids)


def test_measure_columns_helper_empty_for_registers():
    cd = curated.get("ASIC_FINANCIAL_ADVISERS")
    assert cd is not None
    measures = curated.measure_columns(cd)
    assert measures == [], "register datasets have no measure columns"


def test_each_dataset_has_search_keywords():
    """Search would otherwise rank by name/description only."""
    for cd in curated.list_all():
        assert cd.search_keywords, f"{cd.id} has no search_keywords"
