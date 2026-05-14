# asic-mcp

[![PyPI](https://img.shields.io/pypi/v/asic-mcp.svg)](https://pypi.org/project/asic-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/asic-mcp.svg)](https://pypi.org/project/asic-mcp/)
[![License](https://img.shields.io/pypi/l/asic-mcp.svg)](https://github.com/Bigred97/asic-mcp/blob/main/LICENSE)
[![Tests](https://github.com/Bigred97/asic-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/Bigred97/asic-mcp/actions/workflows/test.yml)
[![CodeQL](https://github.com/Bigred97/asic-mcp/actions/workflows/codeql.yml/badge.svg)](https://github.com/Bigred97/asic-mcp/actions/workflows/codeql.yml)
[![Glama MCP server quality](https://glama.ai/mcp/servers/Bigred97/asic-mcp/badges/score.svg)](https://glama.ai/mcp/servers/Bigred97/asic-mcp)

**MCP server for Australian Securities and Investments Commission registers.** Plain-English access to the Financial Advisers Register, AFS Licensees, AFS Authorised Representatives, Credit Licensees, Banned & Disqualified Persons and Organisations, and Registered Liquidators — every one updated weekly by ASIC and served via data.gov.au.

```text
"Is Jane Smith a registered financial adviser?"
"Find every AFS licensee in NSW with 'Westpac' in the name."
"List all banned persons added since 2024-01-01."
"Who's the registered liquidator for KordaMentha?"
"Which credit licensees were suspended last year?"
```

Sister to [abs-mcp](https://github.com/Bigred97/abs-mcp) (Australian Bureau of Statistics), [rba-mcp](https://github.com/Bigred97/rba-mcp) (Reserve Bank of Australia), [ato-mcp](https://github.com/Bigred97/ato-mcp) (Australian Taxation Office), [apra-mcp](https://github.com/Bigred97/apra-mcp) (Australian Prudential Regulation Authority), [aihw-mcp](https://github.com/Bigred97/aihw-mcp) (Australian Institute of Health and Welfare), and [au-weather-mcp](https://github.com/Bigred97/au-weather-mcp) (Australian weather). All seven together cover the macro / regulator / health / tax / climate layer of Australian official data.

---

## Install

```bash
# Run on demand via uvx (recommended)
uvx --upgrade asic-mcp

# Or install permanently
pip install asic-mcp
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "asic": { "command": "uvx", "args": ["--upgrade", "asic-mcp"] }
  }
}
```

> **Why `--upgrade`?** `uvx asic-mcp` (without the flag) uses whatever wheel is cached and never adopts new PyPI releases on its own. `--upgrade` makes uvx check PyPI on each launch and pull a newer release if one exists. To verify which version is currently serving you, look at the `server_version` field on any `DataResponse`.

### Claude Code / Cursor

```bash
claude mcp add asic --command uvx --args -- --upgrade asic-mcp
```

## Auto-updating data

Beyond the wheel-level `--upgrade`, the server has a second auto-update path **inside** the data layer: every register snapshot on data.gov.au is reissued each week (some monthly) at a stable resource UUID. asic-mcp resolves that UUID via [data.gov.au's CKAN API](https://data.gov.au/data/api/3/action/package_show) at fetch time and uses the freshest match. Hard-coded YAML URLs are the safe fallback if CKAN discovery fails. You do **not** need to wait for a new wheel release to get new weekly data — the 24-hour cache TTL means a fresh snapshot lands within a day. Force-refresh by deleting `~/.asic-mcp/cache.db`.

---

## What it exposes

Five tools, all plain-English in, structured out:

| Tool                | Purpose                                                              |
|---------------------|----------------------------------------------------------------------|
| `search_datasets`   | Fuzzy-search the curated catalogue by keyword                         |
| `describe_dataset`  | List a dataset's filterable dimensions and source URL                 |
| `get_data`          | Query with `filters`, period range, output format                     |
| `latest`            | Most recent observation(s) for a dataset (shortcut)                   |
| `list_curated`      | Enumerate the curated dataset IDs                                     |

Every response is the same shape — `dataset_id`, `dataset_name`, `query`, `period`, `unit`, `row_count`, `records`, `source_url`, `attribution`, `server_version`, `stale` — across every curated dataset.

---

## Curated datasets (7)

| ID                       | What it is                                                                                 | Cadence | Source slug                                |
|--------------------------|--------------------------------------------------------------------------------------------|---------|--------------------------------------------|
| `ASIC_FINANCIAL_ADVISERS`| Every individual on the Financial Advisers Register (~21,000 records, 76 columns)          | Weekly  | `asic-financial-adviser`                   |
| `ASIC_AFS_LICENSEE`      | Every Australian Financial Services Licensee (~6,500 entities)                              | Weekly  | `asic-afs-licensee`                        |
| `ASIC_AFS_AUTH_REP`      | Every AFS Authorised Representative — appointees under each AFSL                            | Weekly  | `asic-afs-authorised-representative`       |
| `ASIC_CREDIT_LICENSEE`   | Every Australian Credit Licensee (NCCP-regulated lenders, brokers, BNPL providers)         | Weekly  | `asic-credit-licensee`                     |
| `ASIC_BANNED_PERSONS`    | Persons banned/disqualified from financial services, credit, or managing corporations       | Weekly  | `asic-banned-disqualified-per`             |
| `ASIC_BANNED_ORGS`       | Organisations banned/disqualified — companion register to BANNED_PERSONS                     | Weekly  | `asic-banned-disqualified-org`             |
| `ASIC_LIQUIDATOR`        | Every Registered Liquidator and Official Liquidator (~700 insolvency practitioners)         | Weekly  | `asic-liquidator`                          |

Adding a new dataset is a single YAML drop into `src/asic_mcp/data/curated/` — see [CONTRIBUTING.md](CONTRIBUTING.md). v0.2 roadmap below adds enforcement statistics, short-position reports, and a streaming-lookup tool for the 373 MB Company Register.

---

## Example queries (paste into Claude)

> **Cross-source compatibility.** The `state` filter on every register
> accepts canonical state codes (`"NSW"`), full names (`"New South Wales"`),
> case-insensitive variants (`"nsw"`), ISO 3166-2 (`"AU-NSW"`), and 4-digit
> postcodes (`"2000"` → NSW). Powered by
> [`aus-identity`](https://pypi.org/project/aus-identity/) — the same input
> format works across abs-mcp, ato-mcp, apra-mcp, aihw-mcp, and asic-mcp.

**Compliance check**: *"Confirm whether AFSL 234945 is a current AFS licensee, who the legal name is, and what financial services it's authorised to deal in."*

**Consumer protection**: *"Has anyone with the surname 'Smith' been banned by ASIC in the last 5 years? Show the ban type, dates, and the public comment."*

**Insolvency**: *"List every Registered Liquidator practising under KordaMentha in NSW and Victoria."*

**Adviser lookup**: *"Find financial adviser number 000221137 — current/ceased, licensee, and qualifications."*

**Credit broker due diligence**: *"For Australian Credit Licence 219612, give me the licensee name, status, EDR scheme membership, and a summary of the authorisations text."*

Each prompt resolves to one `get_data` (or `latest`) call. The response includes the source URL and CC-BY 3.0 AU attribution so the agent can cite it back.

---

## Architecture

Same shape as the sister packages — `client → cache → parsing → shaping → server`:

- **`client.py`** wraps `httpx` with a SQLite-backed disk cache and an in-flight dedup so a burst of `latest()` calls fans into one HTTP request. Sends a polite User-Agent — data.gov.au's CDN blocks the default httpx UA.
- **`parsing.py`** reads CSV via pandas, with delimiter auto-sniffing because ASIC labels every file ".csv" but ships some as tab-separated (Financial Advisers, AFS Auth Rep, Banned Orgs) and some as comma-separated (AFS Licensee, Credit Licensee, Banned Persons, Liquidator).
- **`curated.py`** loads dataset specs from `data/curated/*.yaml` — each one declares its dimensions, dimension-value enums, source/download URLs, and CKAN discovery hints.
- **`discovery.py`** resolves the freshest CKAN resource URL at query time; falls back to the YAML default on any failure.
- **`shaping.py`** transforms the parsed DataFrame into `DataResponse` (records / series / csv). For register data (no measure columns) it emits one Observation per row with all dimensions populated.
- **`server.py`** is the FastMCP entrypoint — five tools, full input validation with helpful "Try X" hints on error.

Cache lives under `~/.asic-mcp/cache.db`. ASIC registers refresh weekly, so the byte cache TTL is 24 hours and the CKAN catalogue cache is 1 hour.

---

## Attribution

Source: **Australian Securities and Investments Commission**, licensed under [Creative Commons Attribution 3.0 Australia (CC BY 3.0 AU)](https://creativecommons.org/licenses/by/3.0/au/). Data accessed via [data.gov.au](https://data.gov.au/). © Commonwealth of Australia. The MCP server is MIT-licensed; the underlying register data carries the CC-BY 3.0 AU licence, which is echoed verbatim in every response's `attribution` field.

---

## Sister MCPs (Australian Public Data portfolio)

- [abs-mcp](https://pypi.org/project/abs-mcp/) — Australian Bureau of Statistics (CPI, unemployment, ERP, building approvals)
- [rba-mcp](https://pypi.org/project/rba-mcp/) — Reserve Bank of Australia (cash rate, lending stats, exchange rates)
- [ato-mcp](https://pypi.org/project/ato-mcp/) — Australian Taxation Office (tax stats, ACNC charities)
- [apra-mcp](https://pypi.org/project/apra-mcp/) — Australian Prudential Regulation Authority (banking, insurance, super)
- [aihw-mcp](https://pypi.org/project/aihw-mcp/) — Australian Institute of Health and Welfare
- **asic-mcp** — this one. Financial advisers, AFS / credit licensees, banned persons & orgs, liquidators.
- [aemo-mcp](https://pypi.org/project/aemo-mcp/) — Australian Energy Market Operator (NEM dispatch, spot prices, generation)
- [au-weather-mcp](https://pypi.org/project/au-weather-mcp/) — Open-Meteo (Bureau of Meteorology aggregator)
- [wgea-mcp](https://pypi.org/project/wgea-mcp/) — Workplace Gender Equality Agency
- [aus-identity](https://pypi.org/project/aus-identity/) — Postcode / state / ABN normalisation helper used by all sisters

The portfolio is designed to compose: an agent can ask for "current financial adviser in postcode 2000, paired with the AFS licensee's parent firm and the regulator's banning history" and one shot fans out across two or three MCPs.

---

## Roadmap

- **v0.2**: `ASIC_REGISTERED_AUDITOR`, `ASIC_CREDIT_REPRESENTATIVE`, `ASIC_SMSF_AUDITOR`. Streaming-lookup tool for the 373 MB Company Register (look up by ACN/ABN instead of bulk-loading).
- **v0.3**: Insolvency Series 3.x XLSX from asic.gov.au (apra-mcp-style landing-page scrape). Short-position reports (daily CSV).
- **v0.4**: Hosted version with [x402](https://x402.org/) per-call paywall; programmatic SEO pages; MCPay + Apify listings.

[CHANGELOG](CHANGELOG.md) tracks every release.

---

## Development

```bash
git clone https://github.com/Bigred97/asic-mcp.git
cd asic-mcp
uv sync --extra dev
uv run pytest                  # ~229 unit tests
uv run pytest -m live          # ~9 live tests against data.gov.au
```

Issues, ideas, and contributions welcome: [github.com/Bigred97/asic-mcp/issues](https://github.com/Bigred97/asic-mcp/issues).
