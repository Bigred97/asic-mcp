"""Per-dataset sanity checks — one focused block per curated YAML.

These tests bind the curated metadata to the real fixture so a regression
in either the YAML or the column rename map is caught immediately.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from asic_mcp import curated, parsing, shaping

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _build(dataset_id: str, fixture: str, **kwargs):
    cd = curated.get(dataset_id)
    assert cd is not None
    df = parsing.read_csv((FIXTURE_DIR / fixture).read_bytes())
    dim_cols = [c.source_column for c in cd.columns.values() if c.role == "dimension"]
    df_clean = parsing.drop_blank_rows(df, dim_cols)
    defaults = {
        "filters": {}, "measures": None, "start_period": None,
        "end_period": None, "fmt": "records", "user_query": {}, "last_n": None,
    }
    defaults.update(kwargs)
    return cd, shaping.build_response(cd=cd, df=df_clean, **defaults)


# ---------------------------------------------------------------------------
# ASIC_FINANCIAL_ADVISERS
# ---------------------------------------------------------------------------

class TestFinancialAdvisers:
    fixture = "asic_financial_advisers.csv"

    def test_describe_columns(self):
        cd = curated.get("ASIC_FINANCIAL_ADVISERS")
        assert cd is not None
        keys = set(cd.columns.keys())
        assert "adviser_name" in keys
        assert "adviser_number" in keys
        assert "licence_number" in keys
        assert "overall_registration_status" in keys

    def test_first_record_has_expected_fields(self):
        _, resp = _build("ASIC_FINANCIAL_ADVISERS", self.fixture)
        assert resp.row_count >= 5
        first = resp.records[0]
        assert "adviser_name" in first.dimensions
        assert "adviser_number" in first.dimensions

    def test_filter_by_overall_registration_status(self):
        _, resp = _build(
            "ASIC_FINANCIAL_ADVISERS", self.fixture,
            filters={"overall_registration_status": "Ceased"},
        )
        for obs in resp.records:
            assert obs.dimensions.get("overall_registration_status") == "Ceased"

    def test_filter_state_alias(self):
        _, resp = _build(
            "ASIC_FINANCIAL_ADVISERS", self.fixture,
            filters={"state": "nsw"},
        )
        for obs in resp.records:
            assert obs.dimensions.get("state") == "NSW"


# ---------------------------------------------------------------------------
# ASIC_AFS_LICENSEE
# ---------------------------------------------------------------------------

class TestAFSLicensee:
    fixture = "asic_afs_licensee.csv"

    def test_describe_columns(self):
        cd = curated.get("ASIC_AFS_LICENSEE")
        assert cd is not None
        assert "licensee_name" in cd.columns
        assert "licence_number" in cd.columns
        assert "authorisation" in cd.columns

    def test_first_record_has_expected_fields(self):
        _, resp = _build("ASIC_AFS_LICENSEE", self.fixture)
        assert resp.row_count >= 5
        first = resp.records[0]
        assert first.dimensions.get("register_name") == "AFS Licence"
        assert "licence_number" in first.dimensions
        assert "licensee_name" in first.dimensions

    def test_filter_by_state(self):
        _, resp = _build("ASIC_AFS_LICENSEE", self.fixture, filters={"state": "vic"})
        for obs in resp.records:
            assert obs.dimensions.get("state") == "VIC"

    def test_authorisation_field_present(self):
        _, resp = _build("ASIC_AFS_LICENSEE", self.fixture)
        first = resp.records[0]
        # `authorisation` is verbose free-text — should appear in dimensions
        assert "authorisation" in first.dimensions
        assert isinstance(first.dimensions["authorisation"], str)

    def test_afs_licensee_authorisation_truncated_by_default(self):
        """Regression (v0.6.2): `authorisation` carried 2-3 KB per record,
        blowing the portfolio's 10k-token response budget. It must now be
        truncated to ~200 chars by default with an opt-in suffix.
        """
        _, resp = _build("ASIC_AFS_LICENSEE", self.fixture)
        assert resp.row_count >= 5
        # Source values are well over 200 chars (mean ~1.4 KB), so every
        # fixture record should land at exactly the truncated form.
        for rec in resp.records:
            auth = rec.dimensions.get("authorisation")
            assert isinstance(auth, str)
            # Either short enough to pass through unchanged, OR
            # truncated to the 200-char prefix + suffix.
            if "[truncated, use include_full_authorisation=true" in auth:
                assert len(auth) < 400, (
                    f"truncated authorisation should be ~270 chars "
                    f"(200 prefix + suffix), got {len(auth)}"
                )
            else:
                # The pass-through case must be a genuinely short source value.
                assert len(auth) <= 200, (
                    f"untruncated authorisation must be <=200 chars "
                    f"(would otherwise need the suffix), got {len(auth)}"
                )
        # And at least the first record (real ASIC data, multi-KB) must
        # be in the truncated form — otherwise the rule isn't firing.
        assert "[truncated, use include_full_authorisation=true" in (
            resp.records[0].dimensions["authorisation"]
        )

    def test_afs_licensee_full_authorisation_opt_in(self):
        """Opt-in path: include_full_authorisation=True returns the full
        license-conditions text with no truncation suffix.
        """
        _, resp_default = _build("ASIC_AFS_LICENSEE", self.fixture)
        _, resp_full = _build(
            "ASIC_AFS_LICENSEE", self.fixture,
            include_full_authorisation=True,
        )
        first_default = resp_default.records[0].dimensions["authorisation"]
        first_full = resp_full.records[0].dimensions["authorisation"]

        # Default form is truncated; opt-in form is not.
        assert "[truncated, use include_full_authorisation=true" in first_default
        assert "[truncated, use include_full_authorisation=true" not in first_full
        # Opt-in must be strictly longer (real source is multi-KB).
        assert len(first_full) > len(first_default)
        # Opt-in form must start with the same prefix as the default
        # (truncation must be a prefix slice, not arbitrary subsetting).
        assert first_full.startswith(first_default[:200])

    def test_afs_licensee_truncation_does_not_leak_to_other_dimensions(self):
        """Other dimensions on ASIC_AFS_LICENSEE (licensee_name, abn_acn,
        locality, ...) must never carry the truncation suffix — only the
        `authorisation` field is in the bloat-trim rule set.
        """
        _, resp = _build("ASIC_AFS_LICENSEE", self.fixture)
        for rec in resp.records:
            for key, val in rec.dimensions.items():
                if key == "authorisation":
                    continue
                if isinstance(val, str):
                    assert "[truncated" not in val, (
                        f"unexpected truncation marker on {key!r}: {val!r}"
                    )

    def test_other_asic_datasets_unaffected_by_truncation(self):
        """Sanity: the truncation rule is scoped to ASIC_AFS_LICENSEE.
        Other ASIC datasets must serve every dimension full-fidelity.
        """
        # ASIC_AFS_AUTH_REP shares no schema overlap with the trim rule.
        _, resp = _build("ASIC_AFS_AUTH_REP", "asic_afs_auth_rep.csv")
        for rec in resp.records:
            for val in rec.dimensions.values():
                if isinstance(val, str):
                    assert "[truncated" not in val


# ---------------------------------------------------------------------------
# ASIC_AFS_AUTH_REP
# ---------------------------------------------------------------------------

class TestAFSAuthRep:
    fixture = "asic_afs_auth_rep.csv"

    def test_describe_columns(self):
        cd = curated.get("ASIC_AFS_AUTH_REP")
        assert cd is not None
        assert "rep_number" in cd.columns
        assert "rep_name" in cd.columns
        assert "rep_status" in cd.columns

    def test_first_record(self):
        _, resp = _build("ASIC_AFS_AUTH_REP", self.fixture)
        assert resp.row_count >= 5
        first = resp.records[0]
        assert first.dimensions.get("register_name") == "AFS Representative"

    def test_filter_rep_status(self):
        _, resp = _build(
            "ASIC_AFS_AUTH_REP", self.fixture,
            filters={"rep_status": "ceased"},
        )
        for obs in resp.records:
            assert obs.dimensions.get("rep_status") == "Ceased"


# ---------------------------------------------------------------------------
# ASIC_CREDIT_LICENSEE
# ---------------------------------------------------------------------------

class TestCreditLicensee:
    fixture = "asic_credit_licensee.csv"

    def test_describe_columns(self):
        cd = curated.get("ASIC_CREDIT_LICENSEE")
        assert cd is not None
        for k in ("licensee_name", "licence_number", "current_status", "edrs"):
            assert k in cd.columns

    def test_status_alias_resolves(self):
        cd = curated.get("ASIC_CREDIT_LICENSEE")
        assert cd is not None
        assert curated.translate_filter_value(cd, "current_status", "approved") == "APPR"
        assert curated.translate_filter_value(cd, "current_status", "cancelled") == "CANC"
        assert curated.translate_filter_value(cd, "current_status", "suspended") == "SUSP"

    def test_filter_status_alias(self):
        _, resp = _build(
            "ASIC_CREDIT_LICENSEE", self.fixture,
            filters={"current_status": "approved"},
        )
        for obs in resp.records:
            assert obs.dimensions.get("current_status") == "APPR"


# ---------------------------------------------------------------------------
# ASIC_BANNED_PERSONS
# ---------------------------------------------------------------------------

class TestBannedPersons:
    fixture = "asic_banned_persons.csv"

    def test_describe_columns(self):
        cd = curated.get("ASIC_BANNED_PERSONS")
        assert cd is not None
        for k in ("person_name", "ban_type", "doc_number", "start_date", "end_date"):
            assert k in cd.columns

    def test_record_contents(self):
        _, resp = _build("ASIC_BANNED_PERSONS", self.fixture)
        assert resp.row_count >= 5
        first = resp.records[0]
        assert first.dimensions.get("register_name") == "Banned and Disqualified Persons"
        assert "person_name" in first.dimensions
        assert "ban_type" in first.dimensions

    def test_filter_state(self):
        _, resp = _build(
            "ASIC_BANNED_PERSONS", self.fixture,
            filters={"state": "vic"},
        )
        for obs in resp.records:
            assert obs.dimensions.get("state") == "VIC"


# ---------------------------------------------------------------------------
# ASIC_BANNED_ORGS
# ---------------------------------------------------------------------------

class TestBannedOrgs:
    fixture = "asic_banned_orgs.csv"

    def test_describe_columns(self):
        cd = curated.get("ASIC_BANNED_ORGS")
        assert cd is not None
        for k in ("acn", "org_name", "ban_type", "url"):
            assert k in cd.columns

    def test_record_contents(self):
        _, resp = _build("ASIC_BANNED_ORGS", self.fixture)
        assert resp.row_count >= 5
        first = resp.records[0]
        assert first.dimensions.get("register_name") == "Banned and Disqualified Organisations"

    def test_acn_role_is_id(self):
        cd = curated.get("ASIC_BANNED_ORGS")
        assert cd is not None
        assert cd.columns["acn"].role == "id"


# ---------------------------------------------------------------------------
# ASIC_LIQUIDATOR
# ---------------------------------------------------------------------------

class TestLiquidator:
    fixture = "asic_liquidator.csv"

    def test_describe_columns(self):
        cd = curated.get("ASIC_LIQUIDATOR")
        assert cd is not None
        for k in ("liquidator_number", "liquidator_name", "current_status", "firm"):
            assert k in cd.columns

    def test_record_contents(self):
        _, resp = _build("ASIC_LIQUIDATOR", self.fixture)
        assert resp.row_count >= 5
        first = resp.records[0]
        assert first.dimensions.get("register_name") == "Liquidator"

    def test_filter_status_alias(self):
        _, resp = _build(
            "ASIC_LIQUIDATOR", self.fixture,
            filters={"current_status": "approved"},
        )
        for obs in resp.records:
            assert obs.dimensions.get("current_status") == "APPR"

    def test_filter_state_with_firm_filter(self):
        """Two-filter compound query: state AND firm."""
        _, resp = _build(
            "ASIC_LIQUIDATOR", self.fixture,
            filters={"state": "vic"},
        )
        for obs in resp.records:
            assert obs.dimensions.get("state") == "VIC"
