# Demo prompts

Copy-paste any of these into Claude Desktop (or any MCP client with `asic-mcp` enabled). Each prompt forces a tool call against a real ASIC register and returns a concrete, verifiable answer.

All values below were verified live against `data.gov.au` on 2026-05-13. If your screenshot disagrees, either the tool wasn't called or ASIC shipped a new weekly snapshot (the CKAN discovery layer should resolve the freshest data automatically).

---

## 1. Compliance check — confirm a known AFS licensee

> Confirm whether AFSL 234945 is a current Australian Financial Services Licensee. Show me the legal name, the start date, the state, and a one-line summary of the authorisations.

**Expected**: Commonwealth Bank of Australia, AFSL 234945, NSW, authorised to deal in a long list of financial services. Tool: `asic:get_data("ASIC_AFS_LICENSEE", filters={"licence_number": "234945"})`.

---

## 2. Adviser lookup — is this person a registered financial adviser?

> Look up financial adviser number 000221137. Tell me their name, whether they're current or ceased, who their authorising licensee is, and when they first provided advice.

**Expected**: One record. Status will show "Ceased" with end date. Tool: `asic:get_data("ASIC_FINANCIAL_ADVISERS", filters={"adviser_number": "000221137"})`.

---

## 3. Consumer protection — banned by ASIC?

> List every person banned by ASIC who is based in Victoria. Show me their name, the type of ban, the start and end dates, and the public comment.

**Expected**: A few hundred records under "Banned and Disqualified Persons". Common ban types: "Banned Securities", "Banned Credit", "Disqualified from managing corporations". Tool: `asic:get_data("ASIC_BANNED_PERSONS", filters={"state": "vic"})`.

---

## 4. Credit broker due diligence — Australian Credit Licensees in NSW

> Find every currently approved Australian Credit Licensee in NSW with the word "mortgage" in their name. Show me the licence number, the legal name, the start date, and which external-dispute-resolution scheme they belong to.

**Expected**: A handful of AFCA-registered mortgage brokers. Tool: `asic:get_data("ASIC_CREDIT_LICENSEE", filters={"state": "nsw", "current_status": "approved"})` then narrow on `licensee_name`.

---

## 5. Insolvency lookup — who can wind up a company?

> Find every Registered Liquidator practising under KordaMentha. Show me their name, registered office state, and the date they were registered as a liquidator.

**Expected**: ~30+ practitioners (KordaMentha is a major firm). Tool: `asic:get_data("ASIC_LIQUIDATOR", filters={"firm": "KordaMentha"})`.

---

## 6. Banned organisations — is this company on ASIC's banned list?

> Show me every organisation banned or disqualified by ASIC, ordered by date. Include the ACN, organisation name, ban type, start date, and any public comments.

**Expected**: Around 60–70 records spanning ~20 years. Ban types include "Australian Financial Services banning" and "Credit banning by a State or Territory". Tool: `asic:get_data("ASIC_BANNED_ORGS")`.

---

## 7. AFS authorisation chain — who acts under this licensee?

> AFS Licence number 000237879 — show me every authorised representative currently appointed under it, with their rep number, name, and registered state.

**Expected**: Multiple reps (some "Current", some "Ceased"). Tool: `asic:get_data("ASIC_AFS_AUTH_REP", filters={"licence_number": "000237879"})`.

---

## 8. Multi-state filter — financial advisers in two states

> Find all currently authorised financial advisers in NSW or VIC whose role is "Financial Adviser". Show me the name, the licensee, and the state.

**Expected**: Thousands of records. Tool: `asic:get_data("ASIC_FINANCIAL_ADVISERS", filters={"state": ["nsw", "vic"], "overall_registration_status": "current", "adviser_role": "Financial Adviser"})`.

---

## 9. Latest snapshot date — when did ASIC last refresh?

> What date does the ASIC Financial Advisers register say it was last updated? (Use the `describe_dataset` tool to inspect the metadata.)

**Expected**: Within the last 7 days. Tool: `asic:describe_dataset("ASIC_FINANCIAL_ADVISERS")` — read the `source_url` and click through to data.gov.au to confirm.

---

## 10. CSV export

> Pull every AFS licensee in the ACT and give it to me as CSV so I can paste into a spreadsheet.

**Expected**: Multi-row CSV with columns including `licence_number`, `licensee_name`, `state`, `postcode`, etc. Tool: `asic:get_data("ASIC_AFS_LICENSEE", filters={"state": "act"}, format="csv")`.

---

## Multi-server combos

Once you also have [ato-mcp](https://github.com/Bigred97/ato-mcp) and [abs-mcp](https://github.com/Bigred97/abs-mcp) installed, you can fan out across all three:

> Look up the AFS licensee at AFSL 234945 (asic). Pull the median taxable income for its registered postcode 2000 (ato). And get the latest unemployment rate for Greater Sydney (abs). Summarise.

Claude disambiguates with `asic:`, `ato:`, `abs:` prefixes — one user message produces three parallel tool calls.

---

## Troubleshooting

- **Tool not called / vague answer**: the MCP server isn't installed or not enabled. Check Claude Desktop's tool panel for `asic`. If not present: verify your config file (see `examples/claude_desktop_config_local.json`), then **fully quit Claude Desktop (Cmd+Q)** and reopen.
- **"Could not fetch dataset … from data.gov.au"**: data.gov.au or your network had a hiccup. Retry; the cache is forgiving and warm hits don't go to the network.
- **Numbers look stale**: the register data cache TTL is 24 hours (registers refresh weekly). Delete `~/.asic-mcp/cache.db` to force a refresh.
- **Filter raised "Unknown value"**: `current_status` codes are ASIC short codes (`APPR`, `CANC`, `SUSP`, `DISQ`, `EXPI`). Use the friendly alias (`approved`, `cancelled`, `suspended`, `disqualified`, `expired`) — the resolver translates.
