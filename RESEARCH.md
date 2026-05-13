# asic-mcp — Phase 1 Research

**Status:** Feasibility verdict — **GREEN. Build.**
**Date:** 2026-05-13
**Author:** Harry Vass

---

## TL;DR

ASIC publishes **12 register datasets** on data.gov.au, all under
**Creative Commons Attribution 3.0 Australia**, contact
`Access.Request@asic.gov.au`. The CKAN API at
`https://data.gov.au/data/api/3/action/*` works fine **provided you send a
realistic `User-Agent` header** (default httpx UA gets bot-blocked at the
CloudFront edge → 302 to HTML). The architectural template is **ato-mcp**
verbatim: CKAN `package_show` + `resource_name` discovery → cached CSV
fetch → wide-table shaping. No prior art on PyPI or GitHub. Build estimate
**5 days** for a 7-dataset v0.1.0 release.

---

## 1a. Backend inventory

### data.gov.au CKAN — works with UA header

```bash
curl -A "Mozilla/5.0 ..." \
  "https://data.gov.au/data/api/3/action/organization_show\
?id=australian-securities-and-investments-commission-asic\
&include_datasets=true"
```

Returns full org metadata. ASIC's org slug on data.gov.au is
`australian-securities-and-investments-commission-asic`. **`package_count = 12`**.

`organization_show` paginates `packages` to 10; use
`package_search?fq=organization:<slug>&rows=20` to get the full 12.

### All 12 ASIC datasets on data.gov.au

| Slug | Title | Formats | CSV (MB) | Resources | metadata_modified | Cadence |
|---|---|---|---:|---:|---|---|
| `asic-companies` | ASIC – Company Dataset | CSV, ZIP, PDF | 373.5 | 3 | 2026-05-11 | **weekly** |
| `asic-business-names` | ASIC – Business Names Dataset | CSV, ZIP, PDF | 234.3 | 3 | 2026-05-12 | **monthly** |
| `asic-financial-adviser` | ASIC – Financial Advisers Dataset | CSV, XLSX, PDF | 51.3 | 3 | 2026-05-06 | **weekly** |
| `asic-afs-authorised-representative` | ASIC – AFS Authorised Representative Dataset | CSV, XLSX, PDF | 49.9 | 3 | 2026-05-06 | **weekly** |
| `asic-smsf` | ASIC – SMSF Auditor Dataset | CSV, XLSX, PDF | 11.2 | 3 | 2026-05-06 | **monthly** |
| `asic-afs-licensee` | ASIC – AFS Licensee Dataset | CSV, TSV, XLSX, PDF | 10.0 | 5 | 2026-05-07 | **weekly** |
| `asic-credit-representative` | ASIC – Credit Representative Dataset | CSV, TSV, XLSX, PDF | 6.8 | 4 | 2026-05-07 | **weekly** |
| `asic-credit-licensee` | ASIC – Credit Licensee Dataset | CSV, TSV, XLSX, PDF | 6.8 | 5 | 2026-05-07 | **weekly** |
| `asic-banned-disqualified-per` | ASIC – Banned and Disqualified Persons Dataset | CSV, TSV, XLSX, PDF | 1.2 | 4 | 2026-05-11 | **weekly** |
| `asic-registered-auditor` | ASIC – Registered Auditor Dataset | CSV, TSV, XLSX, PDF | 0.4 | 4 | 2026-05-08 | **weekly** |
| `asic-liquidator` | ASIC – Liquidator Dataset | CSV, TSV, XLSX, PDF | 0.1 | 4 | 2026-05-08 | **weekly** |
| `asic-banned-disqualified-org` | ASIC – Banned and Disqualified Organisations Dataset | CSV, TSV, XLSX, PDF | <0.1 | 4 | 2026-05-11 | **weekly** |

- Every dataset has the same author / contact / licence triplet.
- `metadata_modified` reflects last successful upload — proven fresh as of
  research date (last 7 days for all 12).
- Resource URLs follow the stable CKAN shape
  `https://data.gov.au/data/dataset/{pkg-uuid}/resource/{res-uuid}/download/{filename}`.
  The filename portion embeds a YYYYMM token (e.g. `company_202605.csv`) but
  the **UUIDs are stable across snapshots** — same UUID, new bytes. So
  `package_show` resolves the current URL at query time and the URL itself
  doesn't need scraping like APRA.

### asic.gov.au bulk downloads NOT on data.gov.au

| Source | Format | Cadence | Notes |
|---|---|---|---|
| **Short position reports table** (`/regulatory-resources/markets/short-selling/short-position-reports-table/`) | CSV per day | **daily** (T+4) | One CSV per reporting day; aggregated by ASIC. NOT on data.gov.au. CC-BY 4.0 per ASIC copyright page (short selling data is explicitly listed). |
| **Insolvency statistics — Series 3.1 / 3.2 / 3.3** (`/regulatory-resources/find-a-document/statistics/insolvency-statistics/`) | XLSX | quarterly | External administrators' reports time series 2004-present, 876 KB Series 3.3 XLSX. Report-style, not row-style. |
| **Enforcement outcomes** (statistical reports) | PDF + occasional XLSX | annual / 6-monthly | Mostly narrative reports; XLSX appendices are rare. |
| **ASIC fee statistics / industry funding levies** | PDF | annual | Cost recovery implementation statement (CRIS) — PDF only. Not machine-readable. |
| **Managed investment schemes register** | (only via ASIC Connect search UI — no bulk) | n/a | No bulk feed; per-record only via the paid InfoConnect EDGE feed. |

### ASIC Connect register search — is there an API?

No free programmatic API. ASIC Connect (`connectonline.asic.gov.au`) is a
form-based portal. Bulk programmatic register access is sold via:

- **InfoConnect** (`https://infoconnect.asic.gov.au`) — pay-per-search.
- **EDGE feed** (`http://edge.asic.gov.au`) — bulk daily/weekly XML
  subscriptions. Commercial only, requires customer agreement + fees.

**Implication:** the only free path for register data is the 12 data.gov.au
datasets above. That's fine — the registers we care about (Financial
Advisers, AFS, Banned Persons, Credit, Auditor, Liquidator) are all there.

### Prior art — PyPI + GitHub

| Name | Status |
|---|---|
| `asic-mcp` | **404 — clean lane** |
| `asic-py` | 404 |
| `pyasic` | 0.79.0 — Bitcoin mining ASICs (irrelevant) |
| `asic` | 0.4.4 — Antarctic Sea Ice (irrelevant) |
| `asic-api`, `asicregister`, `asicdata`, `asic-australia` | all 404 |

GitHub: no MCP server for ASIC. Sibling lane covered by `mcp-server-abs`
and a couple of ASX-trading MCPs (different domain). **Zero overlap.**

---

## 1b. Curated dataset shortlist for v0.1.0

7 datasets, all CC-BY 3.0 AU, all weekly except where noted. Picked for
(a) commercial monetization (advisers register is the highest-value
ASIC register data per data.gov.au telemetry), (b) tractable file size
(<60 MB), and (c) consumer relevance (banned persons / orgs is
universally requested).

1. **`ASIC_FINANCIAL_ADVISERS`** — Financial Advisers Register
   - Source: `https://data.gov.au/data/dataset/asic-financial-adviser`
   - CSV 51 MB, ~21,000 rows × ~25 cols, weekly
   - Flagship dataset. "Is X a registered financial adviser?" is the
     single most common ASIC consumer query.

2. **`ASIC_AFS_LICENSEE`** — Australian Financial Services Licensees
   - Source: `https://data.gov.au/data/dataset/asic-afs-licensee`
   - CSV 10 MB, ~6,500 rows, weekly
   - "Which firms hold an AFS licence?" — paired with FA register.

3. **`ASIC_AFS_AUTH_REP`** — AFS Authorised Representatives
   - Source: `https://data.gov.au/data/dataset/asic-afs-authorised-representative`
   - CSV 50 MB, weekly
   - Larger — every authorised rep under an AFSL.

4. **`ASIC_CREDIT_LICENSEE`** — Australian Credit Licensees
   - Source: `https://data.gov.au/data/dataset/asic-credit-licensee`
   - CSV 7 MB, weekly
   - Credit-side counterpart to AFS licensee. NCCP-regulated entities.

5. **`ASIC_BANNED_PERSONS`** — Banned and Disqualified Persons
   - Source: `https://data.gov.au/data/dataset/asic-banned-disqualified-per`
   - CSV 1 MB, weekly
   - High-trust consumer-protection register.

6. **`ASIC_BANNED_ORGS`** — Banned and Disqualified Organisations
   - Source: `https://data.gov.au/data/dataset/asic-banned-disqualified-org`
   - CSV <0.1 MB, weekly
   - Companion to banned persons; tiny, easy win.

7. **`ASIC_LIQUIDATOR`** — Registered Liquidators
   - Source: `https://data.gov.au/data/dataset/asic-liquidator`
   - CSV 0.1 MB, weekly
   - Insolvency-practitioner directory — natural pair with later
     v0.2 insolvency-stats integration.

### v0.2.0 stretch (NOT in initial release)

- **`ASIC_REGISTERED_AUDITOR`** (weekly, 0.4 MB) — easy add but
  audience is narrower; deferred to keep v0.1.0 focused on the
  high-value 7.
- **`ASIC_CREDIT_REPRESENTATIVE`** (weekly, 7 MB) — credit-side
  authorised reps; functional duplicate of AFS_AUTH_REP structure.
- **`ASIC_SMSF_AUDITOR`** (monthly, 11 MB) — SMSF auditor register.
- **`ASIC_COMPANIES`** (weekly, **373 MB CSV / 77 MB ZIP**) — needs
  a different tool shape: streaming lookup by ACN/ABN rather than
  bulk table read. Worth a `lookup_company(acn=...)` 6th tool — but
  the user's spec forbids a 6th tool, so this stays in v0.2 as a
  shaped subset.
- **`ASIC_BUSINESS_NAMES`** (monthly, 234 MB CSV) — same size issue.
- **Insolvency Series 3.x XLSX** from asic.gov.au — quarterly, scrape
  the landing page (APRA pattern).
- **Short position reports** from asic.gov.au — daily CSV-per-day,
  needs date-range concatenation.

---

## 1c. Licence + attribution string

### What CKAN says (authoritative for distribution)

Every one of the 12 ASIC packages on data.gov.au carries:

```
license_id    : cc-by
license_title : Creative Commons Attribution 3.0 Australia
license_url   : http://creativecommons.org/licenses/by/3.0/au/
author        : Australian Securities and Investments Commission (ASIC)
contact_point : Access.Request@asic.gov.au
```

This is the binding licence at point of distribution. CC-BY 3.0 AU
(SPDX `CC-BY-3.0-AU`) — identical to APRA's licence (apra-mcp).

### What asic.gov.au says (more conservative)

ASIC's copyright page
(`/about-asic/dealing-with-asic/copyright-and-linking-to-our-websites/`)
states ten document classes (regulatory guides, reports, info sheets,
short selling position data, etc.) are reproducible under
**CC-BY 4.0 International** with the attribution:

> "© Australian Securities & Investments Commission: Reproduced with
> permission."

The register data on data.gov.au is **not** in that list of ten classes
on ASIC's copyright page — but it's published with an explicit CC-BY
3.0 AU licence at point of distribution, which is the controlling
statement. No carve-out on data.gov.au for commercial resupply (unlike
CASA).

### Required attribution string (bake into every `DataResponse.attribution`)

```
Source: Australian Securities and Investments Commission, licensed under
Creative Commons Attribution 3.0 Australia (CC BY 3.0 AU) —
https://creativecommons.org/licenses/by/3.0/au/. Data accessed via
data.gov.au. © Commonwealth of Australia.
```

This satisfies:
- CC-BY 3.0 AU's attribution requirement (author + licence + link).
- ASIC's preference for "Australian Securities and Investments Commission".
- The data.gov.au provenance.

---

## 1d. Cadence / staleness

| Dataset | Cadence | `stale` flag trigger |
|---|---|---|
| `ASIC_FINANCIAL_ADVISERS` | weekly | metadata_modified > 14d ago |
| `ASIC_AFS_LICENSEE` | weekly | > 14d |
| `ASIC_AFS_AUTH_REP` | weekly | > 14d |
| `ASIC_CREDIT_LICENSEE` | weekly | > 14d |
| `ASIC_BANNED_PERSONS` | weekly | > 14d |
| `ASIC_BANNED_ORGS` | weekly | > 14d |
| `ASIC_LIQUIDATOR` | weekly | > 14d |

Rule of thumb: **weekly = mark stale at >14 days; monthly = >45 days**.
`latest()` should return the snapshot date (from CKAN
`metadata_modified` or the YYYYMM token in the filename) as the
`period_value`, and set `stale = True` when current date exceeds the
trigger.

### Cache TTLs

- **Register data fetch:** 24 hours (datasets refresh weekly, so a
  daily cache is the right balance — we keep the chat session warm
  and pick up new snapshots within 24h of upload).
- **CKAN `package_show` discovery:** 1 hour (same as ato-mcp — UUIDs
  rarely change, but new resources can appear when ASIC introduces
  new register fields).

---

## 1e. Architecture decision

### Backend choice

**data.gov.au CKAN** (`https://data.gov.au/data/api/3/action/*`) with
a real `User-Agent` header. Identical to ato-mcp — no new client
infrastructure needed.

### Why not the ASIC Connect API or EDGE feed?

- ASIC Connect has no free programmatic API.
- EDGE is commercial-only, gated by paid subscription + customer
  agreement. Cannot ship in an open-source MCP.

### Why not scrape asic.gov.au directly?

- data.gov.au mirrors the same register snapshots that ASIC's own
  bulk-download page links to. The data.gov.au CKAN catalogue is
  the canonical machine-readable index — fewer HTML brittleness
  problems than scraping asic.gov.au.
- Future stretch (insolvency Series 3.x XLSX, short position CSVs)
  will need apra-mcp's landing-page scrape pattern; those land in
  v0.2.

### Architectural template

`ato-mcp` byte-for-byte — same `discovery.py` pattern (`package_id` +
`resource_name`), same `client.py` skeleton (data.gov.au User-Agent),
same `cache.py`, same wide-table `shaping.py`. The 7 curated YAMLs
mirror `ato-mcp/src/ato_mcp/data/curated/ACNC_REGISTER.yaml`'s shape.

### Tool surface (5 tools — non-negotiable, copy abs-mcp signature)

1. `search_datasets(query, limit=10)`
2. `describe_dataset(dataset_id)`
3. `get_data(dataset_id, filters, start_period, end_period, format)`
4. `latest(dataset_id, filters)`
5. `list_curated()`

Every param: `Annotated[Type, Field(description=…, examples=[…])]` per
the abs-mcp Grade A pattern.

### Trust contract

Every `DataResponse` carries:
- `source = "Australian Securities and Investments Commission"`
- `source_url = "https://data.gov.au/data/dataset/{slug}"`
- `attribution = <the string in §1c>`
- `retrieved_at = <UTC ISO>`
- `server_version = "0.1.0"`
- `stale = bool` + `stale_reason: str | None`

---

## Blockers / known risks

| Risk | Severity | Mitigation |
|---|---|---|
| data.gov.au bot-blocks default httpx UA at CloudFront edge | **medium** | Send a polite browser-shaped UA (already done in ato-mcp); add a smoke-test in CI that hits CKAN every build. |
| ASIC moves to data.gov.au successor (Magda) | low | All 12 datasets active and modified within the last 7 days; no migration signals in the metadata. ato-mcp's pattern is portable if it happens. |
| Register data redistribution carve-out we missed | low | Explicit CC-BY 3.0 AU on every package's `license_url`. No carve-out language anywhere in CKAN metadata or on data.gov.au. ASIC's own copyright page is more conservative (lists ten doc classes for CC-BY 4.0) but doesn't override the data.gov.au licence. **If in doubt, email `Access.Request@asic.gov.au` before publishing.** |
| Company / Business Names files are 373 MB / 234 MB | high (if shipped in v0.1) | **Defer to v0.2** with a streaming lookup tool. Don't include in v0.1 curated set. |
| PyPI 5-new-projects-per-24h rate limit | medium | Already known constraint. Sequence the asic-mcp first publish at least 24h after the last fresh project. |
| Glama dual-listing under `mcp-server-abs` confusion | low | README cross-links explicitly to abs-mcp, apra-mcp, ato-mcp, aihw-mcp siblings to differentiate. |

---

## Build estimate

| Phase | Days |
|---|---|
| Scaffold (`pyproject`, `models`, `cache`, `client`, `catalog`, `discovery`, `shaping`) — copy ato-mcp | 1.0 |
| 7 curated YAMLs (~30 min each, including search keywords + dimension_values) | 0.5 |
| Server.py — 5 tools with Annotated/Field, input guards | 0.5 |
| Unit tests (≥120) — happy paths, input-guard parity with abs-mcp, sqlite self-heal | 1.5 |
| Live tests (≥8) including the "look up a known-stable AFS licensee" assertion | 0.5 |
| README / CHANGELOG / LICENSE / CONTRIBUTING / SECURITY / glama.json / `examples/` / `llms.txt` / GH Actions matrix | 0.5 |
| 10× zero-flake pytest gauntlet + fresh-PyPI-install verification | 0.5 |
| **Total** | **~5 days** |

Comparable to apra-mcp's 5–6 day build (245 tests, 7 datasets) and
ato-mcp's longer 8 day build (it had 13 datasets + auto-discovery
edge cases we now have prior art for).

---

## Verdict — proceed?

**Yes — green-light v0.1.0.**

- ✅ Free, machine-readable, refreshed weekly
- ✅ Open licence (CC-BY 3.0 AU) — same as APRA, no commercial carve-out
- ✅ Architectural template already proven (ato-mcp)
- ✅ No prior art — `asic-mcp` is an open name
- ✅ Commercial story is clean — "find a registered adviser" is a
  high-intent consumer query that paid feeds charge for
- ⚠ Bot-blocking on default UA — known, mitigated
- ⏸ Company + Business Names large-file handling — deferred to v0.2

**Next step:** scaffold the repo at
`/Users/harry/Desktop/MCP Endpoint Creation/asic-mcp/` mirroring
`ato-mcp/`, then iterate the 7 YAMLs.

---

## Sources

- ASIC org on data.gov.au: https://data.gov.au/data/dataset?organisation=Australian+Securities+%26+Investments+Commission
- data.gov.au CKAN endpoint pattern: https://data.gov.au/data/api/3/action/
- ASIC's own data.gov.au index: https://www.asic.gov.au/online-services/search-asic-registers/data-gov-au/
- ASIC copyright page: https://www.asic.gov.au/about-asic/dealing-with-asic/copyright-and-linking-to-our-websites/
- CC-BY 3.0 AU deed: https://creativecommons.org/licenses/by/3.0/au/
- Insolvency statistics: https://www.asic.gov.au/about-asic/corporate-publications/statistics/insolvency-statistics/
- Short position reports: https://www.asic.gov.au/regulatory-resources/markets/short-selling/short-position-reports-table/
