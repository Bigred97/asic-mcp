"""Shared pytest fixtures.

Test fixtures load small head-only samples of real ASIC register CSVs from
`tests/fixtures/`. Each is ~50 data rows — enough to exercise the parser,
shaper, and filter pipeline without needing network access. Live tests (the
`live` pytest marker) hit data.gov.au directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from asic_mcp import curated

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def reset_curated_registry():
    """Force a fresh load of curated YAMLs before each test."""
    curated.reset_registry()
    yield
    curated.reset_registry()


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture
def financial_advisers_csv() -> bytes:
    """ASIC Financial Advisers register — TAB-delimited CSV, head-only sample."""
    return (FIXTURE_DIR / "asic_financial_advisers.csv").read_bytes()


@pytest.fixture
def afs_licensee_csv() -> bytes:
    """ASIC AFS Licensee register — comma-delimited, head-only sample."""
    return (FIXTURE_DIR / "asic_afs_licensee.csv").read_bytes()


@pytest.fixture
def afs_auth_rep_csv() -> bytes:
    """ASIC AFS Authorised Representative register — TAB-delimited, head-only."""
    return (FIXTURE_DIR / "asic_afs_auth_rep.csv").read_bytes()


@pytest.fixture
def credit_licensee_csv() -> bytes:
    """ASIC Credit Licensee register — comma-delimited, head-only sample."""
    return (FIXTURE_DIR / "asic_credit_licensee.csv").read_bytes()


@pytest.fixture
def banned_persons_csv() -> bytes:
    """ASIC Banned and Disqualified Persons — comma-delimited, head-only sample."""
    return (FIXTURE_DIR / "asic_banned_persons.csv").read_bytes()


@pytest.fixture
def banned_orgs_csv() -> bytes:
    """ASIC Banned and Disqualified Organisations — TAB-delimited, head-only."""
    return (FIXTURE_DIR / "asic_banned_orgs.csv").read_bytes()


@pytest.fixture
def liquidator_csv() -> bytes:
    """ASIC Registered Liquidator — comma-delimited, head-only sample."""
    return (FIXTURE_DIR / "asic_liquidator.csv").read_bytes()
