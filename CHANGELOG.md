# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-05-16

### Added — ASIC_SMSF_AUDITORS

- **`ASIC_SMSF_AUDITORS` curated dataset.** Every SMSF auditor
  registered with ASIC under section 128A of the SIS Act 1993 —
  ~5,000 unique auditors, ~30,000 rows (one row per
  auditor × condition/attribute). Updated weekly via data.gov.au.
- Columns: smsf_auditor_number, auditor_name, current_status,
  abn, registration_date, suspension dates, locality, postcode,
  state, attribute_type, capacity (Director/Partner/Sole
  Practitioner/Employee), capacity_firm_name, condition + detail.
- Closes a real gap for SMSF service providers, accounting firms
  with SMSF audit practices, trustees searching for an auditor,
  compliance teams, lawyers handling SMSF disputes.
- Uses existing TSV-detection register parser (the file has a
  `.csv` extension but is tab-delimited; the parser auto-detects).
  Plus CKAN auto-discovery for weekly URL refresh.

### Customer-value validation (live ASIC fetch, 2026-05-16)

- SMSF service provider lookup: `latest('ASIC_SMSF_AUDITORS',
  filters={'smsf_auditor_number':'100263447'})` returns 8 rows
  (one per registration condition) for Benjamin Jenkins, NSW,
  Registered.
- NSW registered SMSF auditors: 1,618 unique auditors (of 13,072
  total attribute rows).
- Suspended SMSF auditors nationally: 1 unique auditor — useful
  for compliance teams.
- Search routing: "smsf auditor", "self managed super fund
  auditor", "smsf compliance", "smsf audit" all hit
  ASIC_SMSF_AUDITORS at #1.

### Tests

- 249 unit tests passing (was 249). 10× zero-flake gauntlet.
- `test_list_curated_returns_sorted_ids` updated from 12 to 13.

## [0.5.1] - 2026-05-16

### Fixed

- Ruff lint failure in `_resolve_dated_url` — `last_err` variable was
  assigned in the `except` branch but never read (F841). Removed.
  No behaviour change.

## [0.5.0] - 2026-05-16

### Added — ASIC_SHORT_POSITIONS daily market data feed (Wave 3)

- **`ASIC_SHORT_POSITIONS` curated dataset.** Daily reported short
  positions for every ASX-listed equity (~2,000 securities), with T+4
  business-day publication lag. Each row carries reported short positions,
  total product on issue, and the resulting short-interest percentage.
  This is the regulator's official short-interest measure for the
  Australian market — the dataset hedge funds, equity researchers, and
  financial media use daily.
- **Date-templated URL infrastructure (new).** Introduces the
  `url_template: "...{date:YYYYMMDD}..."` YAML field plus
  `url_template_lookback_days` (default 10). The server probes the last
  N calendar days with HEAD requests to find the most-recent published
  file. Handles weekends and public holidays automatically — no business-
  day calendar required.
- New `ASICClient.head_ok()` method on the HTTP client — cheap probe used
  by the date resolver. ~5 LOC.
- New `_resolve_dated_url()` helper in server.py — ~25 LOC.

### Customer-value validation (live ASIC fetch, 2026-05-16)

- Most-shorted query: `latest('ASIC_SHORT_POSITIONS', limit=10)` returns
  the top 10 of 2,193 securities (truncated_at=2193).
- Ticker lookup: `get_data('ASIC_SHORT_POSITIONS', filters={'product_code':'BHP'})`
  returns 3 records (reported shorts 63.35M, total issued 5.08B, short
  percent 1.25%).
- Search routing: "short positions", "short interest", "most shorted",
  "short selling" all surface ASIC_SHORT_POSITIONS at #1.

### Known limitation

- `top_n` is not available on asic-mcp (intentionally absent for
  registers per the v0.1 design); clients wanting "top 10 most-shorted"
  currently fetch all rows via `latest(... limit=10000)` and sort
  client-side. Adding `top_n` to asic-mcp for this market-data dataset
  is a follow-up.

### Tests

- 249 unit tests now (was 246). 10× zero-flake gauntlet.
- Test predicates that asserted "every dataset is a weekly/monthly
  register with CKAN discovery" updated to except ASIC_SHORT_POSITIONS
  (daily-cadence market data with date-templated URL).

## [0.4.1] - 2026-05-16

### Fixed

- `test_live_list_curated_round_trip` updated to expect 11 datasets (was 7).
- CLAUDE.md curated dataset list updated to all 11 ASIC registers.

## [0.4.0] - 2026-05-16

### Added

- **4 new curated ASIC registers** — expands from 7 to 11 datasets:
  - `ASIC_COMPANIES`: full Australian company register (~3.5M records), weekly
  - `ASIC_BUSINESS_NAMES`: registered trading names linked to ABN, weekly
  - `ASIC_CREDIT_REPRESENTATIVE`: mortgage/finance brokers authorised under
    ACL holders (CRED_REP_NUM, CRED_LIC_NUM, authorisations, state), weekly
  - `ASIC_AUDITORS`: registered company auditors with status and address, weekly

## [0.3.1] - 2026-05-15

### Fixed

- `ASIC_BANNED_ORGS` UTF-8 decode error on a CKAN-resolved CSV. The package
  publishes three resources with the same name "Banned and Disqualified
  Organisations - Current" (CSV + TSV + XLSX); discovery was returning the
  XLSX URL, whose ZIP-archive bytes then tripped the CSV decoder at byte 0xac.
  Discovery now filters resources by the curated dataset's declared format,
  so a `format: csv` dataset gets the CSV resource even when same-named
  XLSX/TSV siblings are present. Added an encoding fallback chain
  (utf-8 → windows-1252 → iso-8859-1) in `read_csv` as defence-in-depth
  for any ASIC register that ever ships non-UTF-8 bytes inside a `.csv`.

## [0.3.0] — 2026-05-15

### Added — Wave 1 portfolio interoperability fix (int-year coercion)

Cross-sister consistency pass on input handling identified in the portfolio
interoperability audit.

- **Int-year coercion in period validation.** `start_period=2024` (a bare
  JSON int) now coerces to `"2024"` instead of raising a TypeError-style
  message. LLM clients routinely send JSON ints; this removes a confusing
  failure mode that surfaced as `must be a string, got int`. Out-of-range
  ints (e.g. `12345`, `1800`) still raise — with a hint pointing at the
  canonical `'YYYY'` / `'YYYY-MM'` / `'YYYY-MM-DD'` forms. `bool` is
  explicitly rejected (it's a subclass of int) to avoid silent coercion.
- **Type signature broadened** on `get_data`'s `start_period` /
  `end_period` to `str | int | None` so the tool's published schema
  reflects the new coercion behaviour.

3 new unit tests in `tests/test_server_validation.py` cover the coercion
boundary, the out-of-range hint, and the bool-subclass-of-int guard.

### Backward compatibility

No breaking changes. Inputs that previously raised a type error on bare
int years now succeed; every other input still validates as before.

## [0.2.0] — 2026-05-15

### Added — aus-identity integration

The cross-source compatibility moat for the AU public-data MCP stack.
The `state` filter on every ASIC register (ASIC_FINANCIAL_ADVISERS,
ASIC_AFS_LICENSEE, ASIC_AFS_AUTH_REP, ASIC_CREDIT_LICENSEE,
ASIC_BANNED_PERSONS, ASIC_LIQUIDATOR) now accepts the full canonical menu:

- Canonical short codes (`NSW`, `VIC`, `QLD`, `SA`, `WA`, `TAS`, `NT`, `ACT`)
- Case-insensitive variants (`nsw`, `Nsw`)
- Full names (`New South Wales`, `Queensland`, `Tasmania`)
- ISO 3166-2 (`AU-NSW`, `AU-VIC`)
- Common aliases (`Tassie`)
- 4-digit postcodes (`2000` → NSW, `2600` → ACT, `3000` → VIC, `0800` → NT)

Powered by [`aus-identity`](https://pypi.org/project/aus-identity/). An LLM
agent that's already fetched a postcode from another sister MCP (ato-mcp,
abs-mcp) can pass it straight to asic-mcp without manual conversion.

- **`aus-identity>=0.1.0`** added as a new top-level dependency.
- **`curated.translate_filter_value`** runs state-shaped dim values through
  `aus_identity.normalize_state` + `aus_identity.postcode_to_state` before
  falling back to the existing alias / canonical lookup. Existing aliases
  (`nsw` → `NSW`) and canonical values (`NSW` → `NSW`) still resolve
  unchanged.
- **7 new unit tests** in `tests/test_curated.py` covering full name,
  lowercase full name, ISO 3166-2, common alias, postcode routing,
  ACT-postcode boundary, and a second register (ASIC_CREDIT_LICENSEE).

### Backward compatibility

No breaking changes — every input that worked in 0.1.3 still works.

## [0.1.3] — 2026-05-15

Error-message sweep — closes CLAUDE.md quality dimension #5 (Deterministic
Error Handling). Rejection messages now suggest the correction rather than
just describing the failure.

### Changed — `ValueError` messages now suggest the fix

Following the same `Try X` / `Did you mean X?` / `Valid options: ...`
pattern as the rest of the sister stack, weak rejection messages were
rewritten so an agent (or a human reading a traceback) gets an obvious
next step rather than a dead-end string.

Touched sites:

- Unknown dataset id on `describe_dataset` and `get_data` — now runs the
  bad id through `difflib.get_close_matches` against `curated.list_ids()`
  and prepends "Did you mean `'ASIC_FINANCIAL_ADVISERS'`?" before the full
  list of valid ids. A typo like `'ASIC_FINANCIAL_ADVISOR'` now resolves
  to a one-line correction.
- Unknown filter key in `shaping._apply_filters` — was generic "Unknown
  filter `'foo'` for dataset `'X'`. Try one of: a, b, c"; now: "Filter
  `'adviser_no'` is not a column on `ASIC_FINANCIAL_ADVISERS`. Did you
  mean `'adviser_number'`? Valid filters: ... Try `describe_dataset(...)`
  to see all filter columns."
- Unknown dimension value in `curated.translate_filter_value` — adds a
  difflib hint across both plain-English aliases and canonical source
  values, plus a "Try `describe_dataset(...)`" pointer.
- Unknown measure key in `curated.resolve_measure_keys` — adds difflib
  hint + describe pointer. Special-cases dimension-only registers (all of
  ASIC's v0.1 datasets) with a clearer "this dataset has no curated
  measures — omit `measures` to return all rows" message.
- `search_datasets(limit=...)` rejections — limit-out-of-range message
  now states the valid range (1-50) and gives concrete examples.
- `_validate_period` non-string rejection — message now includes the
  three accepted formats (YYYY, YYYY-MM, YYYY-MM-DD) and worked examples.
- `_fetch_and_parse` upstream-fetch error — explains that data.gov.au is
  the upstream, that transient 5xx / DNS errors usually clear on retry,
  that a cached fallback would have been served if available, and points
  at the dataset's source URL for verification.

No exception types changed; only the message text. No new dependencies
(`difflib` is stdlib).

### Tests

- 2 new regression tests in `test_server_validation.py`:
  - typo'd dataset id triggers `Did you mean 'ASIC_FINANCIAL_ADVISERS'`
  - typo'd filter key triggers `Did you mean 'adviser_number'` plus
    `describe_dataset` pointer
- 3 existing assertions in `test_edge_inputs.py`, `test_register_shape.py`,
  and `test_server_validation.py` updated to match the new message
  substrings (exception type unchanged).

237 unit tests now (was 235); 10× zero-flake gauntlet.

## [0.1.2] — 2026-05-15

Reliability release — closes CLAUDE.md quality dimension #4 (Reliability +
Caching) by adding graceful degradation when data.gov.au is unreachable.

### Fixed — fall back to stale cache on upstream failure

Previously, any data.gov.au 5xx / DNS failure / connection refused broke the
tool with a raised `ASICAPIError`, even if a cached payload existed in the
local SQLite cache. Agents using the tool mid-conversation would lose the
thread when ASIC's CDN had a hiccup.

Now the client falls back to the most-recent cached payload (regardless of
TTL) when upstream is unreachable, and surfaces the staleness on the
existing `DataResponse.stale` / `stale_reason` fields:

```
DataResponse.stale         = True
DataResponse.stale_reason  = "ASIC dataset fetch returned 503 for {url};
                              serving cached payload from ~17 minute(s) ago"
```

Empty-cache case still raises `ASICAPIError` — only degrade gracefully when
there's something to degrade to. This is especially important for ASIC's
large registers (the AFS Authorised Representatives CSV is ~50 MB) where a
single re-fetch would otherwise time out the conversation.

### Added

- `Cache.get_stale(key)` — returns `(payload, cached_at_epoch)` regardless
  of TTL, with the same mid-session corruption recovery as `get()`.
- `client._stale_signal` `ContextVar` + `reset_stale_signal()` /
  `get_stale_signal()` helpers — concurrent MCP tool calls each see their
  own staleness state.
- `server._get_data_impl` now resets the signal on entry and copies the
  stale flag + reason onto the `DataResponse` after `build_response`.
- 4 regression tests in new `tests/test_client.py`:
  - 5xx fallback serves cached payload and marks stale
  - `RequestError` (DNS / connection refused) fallback path
  - empty-cache + 5xx still raises (preserves original behaviour)
  - `Cache.get_stale` TTL-bypass building block

### Tests

235 unit tests now (was 231); 10× zero-flake gauntlet.

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
