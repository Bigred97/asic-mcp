# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] — 2026-05-13

Bugfix release — closes the only customer-facing UX footgun surfaced by a
portfolio-wide live smoke test.

### Fixed — `latest()` on large registers no longer bombs agent context

Calling `latest("ASIC_AFS_AUTH_REP")` (or any large register) used to dump
**all ~360,000 rows** in a single response. ASIC registers are flat snapshots
of every authorised entity, not time series — so the existing `last_n=1`
indirection was meaningless and the full register flowed through unfiltered.
Any agent calling this without filters would blow its context window.

**Fix**: `latest()` gains a new `limit: int = 50` parameter (Pydantic-bounded
to `[1, 10000]`). The response is sliced to `limit` records; if truncation
happened, the original row count is preserved in `DataResponse.truncated_at`
so an agent can detect it and either widen the limit or add more filters.

For precise lookups (e.g. `filters={"adviser_number": "1234567"}`) the
result already fits well under the cap and no truncation occurs —
`truncated_at` stays None.

### Added

- `DataResponse.truncated_at: int | None` — set to the original row count
  when `latest()` capped the response. None otherwise.
- 2 regression tests in `test_server_validation.py`:
  - 1,000-row fake response capped to 50 with `truncated_at=1000`
  - 10-row response passes through unchanged with `truncated_at=None`

231 unit tests now (was 229).

## [0.1.0] — 2026-05-13

Initial release. ASIC registers via data.gov.au, plain-English access.

### Added

- **Seven curated ASIC register datasets**, all served from CKAN-discovered
  resource URLs on data.gov.au under CC BY 3.0 AU:
  - `ASIC_FINANCIAL_ADVISERS` — Financial Advisers Register (~21,000 records, weekly).
  - `ASIC_AFS_LICENSEE` — Australian Financial Services Licensees (~6,500 entities, weekly).
  - `ASIC_AFS_AUTH_REP` — AFS Authorised Representatives (~50 MB CSV, weekly).
  - `ASIC_CREDIT_LICENSEE` — Australian Credit Licensees (NCCP-regulated lenders/brokers, weekly).
  - `ASIC_BANNED_PERSONS` — Banned and Disqualified Persons (weekly).
  - `ASIC_BANNED_ORGS` — Banned and Disqualified Organisations (weekly).
  - `ASIC_LIQUIDATOR` — Registered & Official Liquidators (~700 practitioners, weekly).
- **Five MCP tools** matching the abs-mcp / rba-mcp / ato-mcp / apra-mcp envelope:
  `search_datasets`, `describe_dataset`, `get_data`, `latest`, `list_curated`.
- **Trust contract on every `DataResponse`**: `source`, `source_url`,
  `attribution`, `retrieved_at`, `server_version`, `stale`. Attribution is
  the exact CC BY 3.0 AU statement from data.gov.au, naming ASIC as the
  source and pointing at the canonical CC licence URL.
- **CKAN auto-discovery** — each YAML declares a `discovery:` block
  (`package_id` + `resource_name`); the server resolves the freshest
  resource URL at fetch time. Hard-coded YAML URLs are the safe fallback.
- **CSV delimiter sniffing** — ASIC labels every file `.csv` on data.gov.au,
  but the actual delimiter is tab for some datasets (Financial Advisers, AFS
  Authorised Representative, Banned Orgs) and comma for others (AFS
  Licensee, Credit Licensee, Banned Persons, Liquidator). `read_csv`
  detects from the first line.
- **Dimension-only register shaping** — register data has no measure
  columns, so `shape_wide` emits one `Observation` per row carrying every
  dimension on `Observation.dimensions` with `value`/`measure` left `None`.
- **SQLite byte cache** with per-kind TTLs (24h for register data, 1h for
  CKAN catalogue) and mid-session corruption recovery.
- **Parsed-DataFrame in-process LRU cache** keyed by (URL, parse-spec,
  body content hash) — warm hits avoid pandas re-parse.
- **In-flight dedup** — a burst of identical `latest()` calls fans into
  one HTTP request.
- **State alias maps** on every register that exposes `state` — pass
  `"nsw"`, `"NSW"`, or canonical `"NSW"`; all resolve identically.
- **Status alias maps** on Credit Licensee, Liquidator, and Financial
  Adviser registers — pass `"approved"` and asic-mcp resolves to ASIC's
  `"APPR"` code.
- **Polite User-Agent** on every outbound request. data.gov.au's CDN
  blocks the default httpx UA (returns 302 to HTML); asic-mcp identifies
  itself with `asic-mcp/0.1 (+https://github.com/Bigred97/asic-mcp)`.

### Tests

- **229 unit tests + 9 live tests = 238 total**, zero flake across the
  10× gauntlet.
- Live tests assert a known-stable AFSL number (234945 — Commonwealth
  Bank of Australia) is present in the live snapshot.
- Adversarial / fuzz inputs: ~80 parametrised cases probing Unicode, path
  traversal, URL injection, type confusion, huge strings, and emoji on
  every tool parameter.
- Discovery tests cover happy paths, off-host URL rejection (defense-in-
  depth), no-match raises `DiscoveryError`, malformed CKAN responses,
  and the fallback-to-YAML invariant when the network is down.

### Known limitations (deferred to v0.2+)

- `ASIC_COMPANIES` (373 MB CSV) and `ASIC_BUSINESS_NAMES` (234 MB CSV)
  are excluded from v0.1 — they need a streaming lookup-by-ACN/ABN tool
  rather than bulk table reads.
- Enforcement / insolvency statistics (Series 3.1 / 3.2 / 3.3 XLSX on
  asic.gov.au) are not in v0.1 — they live outside data.gov.au and need
  apra-style landing-page scraping.
- Short-position daily CSVs are not in v0.1 — they have a different
  cadence and shape (one CSV per reporting day).
- The dimension-only register model gives `Observation.value = None` for
  every row. Agents should read register data from `Observation.dimensions`.
