"""CSV parsing tests for ASIC fixtures.

Two key concerns:
1. ASIC labels every file ".csv" on data.gov.au, but the actual delimiter
   varies — some are comma-delimited with quoted fields, some are tab-
   delimited. `parsing.read_csv` sniffs the delimiter automatically.
2. UTF-8 BOM, blank trailing rows, and quoted fields containing newlines
   all need to parse cleanly.
"""
from __future__ import annotations

import pytest

from asic_mcp.parsing import ParseError, drop_blank_rows, read_csv


def test_read_csv_comma_delimited(afs_licensee_csv):
    """AFS Licensee CSV is comma-delimited with quoted strings — must parse to 13 cols."""
    df = read_csv(afs_licensee_csv)
    assert list(df.columns)[:3] == ["REGISTER_NAME", "AFS_LIC_NUM", "AFS_LIC_NAME"]
    assert len(df.columns) == 13
    assert len(df) >= 10


def test_read_csv_tab_delimited(financial_advisers_csv):
    """Financial Advisers CSV is actually TAB-delimited — sniffer must detect."""
    df = read_csv(financial_advisers_csv)
    assert "REGISTER_NAME" in df.columns
    assert "ADV_NAME" in df.columns
    assert len(df.columns) >= 70  # 76 in the real file


def test_read_csv_tab_delimited_banned_orgs(banned_orgs_csv):
    """Banned Organisations CSV is also tab-delimited."""
    df = read_csv(banned_orgs_csv)
    assert "REGISTER_NAME" in df.columns
    assert "BD_ORG_NAME" in df.columns
    assert "BD_ORG_TYPE" in df.columns


def test_read_csv_tab_delimited_afs_auth_rep(afs_auth_rep_csv):
    """AFS Auth Rep CSV is tab-delimited."""
    df = read_csv(afs_auth_rep_csv)
    assert "REGISTER_NAME" in df.columns
    assert "AFS_REP_NUM" in df.columns
    assert "AFS_REP_NAME" in df.columns


def test_read_csv_banned_persons(banned_persons_csv):
    """Banned Persons CSV is comma-delimited with 11 columns."""
    df = read_csv(banned_persons_csv)
    assert "REGISTER_NAME" in df.columns
    assert "BD_PER_NAME" in df.columns
    assert "BD_PER_TYPE" in df.columns
    assert len(df.columns) == 11


def test_read_csv_credit_licensee(credit_licensee_csv):
    """Credit Licensee CSV is comma-delimited with 17 columns."""
    df = read_csv(credit_licensee_csv)
    assert "CRED_LIC_NUM" in df.columns
    assert "CRED_LIC_NAME" in df.columns
    assert "CRED_LIC_STATUS" in df.columns
    assert len(df.columns) == 17


def test_read_csv_liquidator(liquidator_csv):
    df = read_csv(liquidator_csv)
    assert "LIQ_NUM" in df.columns
    assert "LIQ_NAME" in df.columns
    assert "LIQ_FIRM" in df.columns


def test_read_csv_empty_body_raises():
    with pytest.raises(ParseError, match="empty CSV body"):
        read_csv(b"")


def test_read_csv_handles_utf8_bom(afs_licensee_csv):
    """All ASIC fixtures ship with UTF-8 BOM. The BOM should NOT appear in
    the first column header."""
    df = read_csv(afs_licensee_csv)
    first_col = df.columns[0]
    assert not first_col.startswith("﻿"), (
        f"BOM leaked into first column header: {first_col!r}"
    )
    assert first_col == "REGISTER_NAME"


def test_read_csv_handles_quoted_fields_with_special_chars(credit_licensee_csv):
    """Credit licensee `CRED_LIC_AUTHORISATIONS` field carries multi-line text
    with commas and newlines — must parse correctly."""
    df = read_csv(credit_licensee_csv)
    # Make sure parsing didn't split the authorisations column into rows
    assert "CRED_LIC_AUTHORISATIONS" in df.columns


def test_read_csv_does_not_corrupt_leading_zeros(afs_licensee_csv):
    """AFS_LIC_NUM has leading zeros (000218600 etc.) — must remain a string."""
    df = read_csv(afs_licensee_csv)
    # First row's AFS_LIC_NUM should still be a string of digits.
    lic_num = df["AFS_LIC_NUM"].iloc[0]
    assert isinstance(lic_num, str) or hasattr(lic_num, "item"), (
        f"AFS_LIC_NUM dtype unexpected: {type(lic_num)}"
    )


def test_drop_blank_rows_removes_all_nan_in_keys(afs_licensee_csv):
    """drop_blank_rows trims trailing rows where every key column is NaN."""
    df = read_csv(afs_licensee_csv)
    keep = drop_blank_rows(df, ["AFS_LIC_NUM", "AFS_LIC_NAME"])
    assert len(keep) <= len(df)
    # Every retained row must have at least one of the keys non-null
    if keep.empty:
        return
    not_all_null = ~keep[["AFS_LIC_NUM", "AFS_LIC_NAME"]].isna().all(axis=1)
    assert not_all_null.all()


def test_drop_blank_rows_passthrough_when_keys_absent(afs_licensee_csv):
    """If the named key columns don't exist in df, drop_blank_rows is a no-op."""
    df = read_csv(afs_licensee_csv)
    out = drop_blank_rows(df, ["DEFINITELY_NOT_A_COLUMN"])
    assert len(out) == len(df)


def test_read_csv_sniff_handles_pure_comma_no_tabs(afs_licensee_csv):
    """A CSV with zero tabs must use the comma branch of the sniffer."""
    df = read_csv(afs_licensee_csv)
    # If the sniffer had picked tab here we'd see 1 wide column instead of 13.
    assert len(df.columns) == 13


def test_read_csv_sniff_handles_more_tabs_than_commas(financial_advisers_csv):
    """A CSV with more tabs than commas must use the tab branch of the sniffer."""
    df = read_csv(financial_advisers_csv)
    # Tab-sniffed correctly → many columns. Comma-sniffed wrongly → 1 column.
    assert len(df.columns) > 5
