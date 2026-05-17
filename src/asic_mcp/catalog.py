"""Fuzzy search and listing across the curated dataset registry.

Unlike abs-mcp (which calls SDMX dataflow listings) or rba-mcp (which has a
static F-table registry), asic-mcp ships with N curated datasets hand-picked
for sellable value. The catalog surface is intentionally small in v0.1 — we
expose only the 7 curated ASIC registers. Future versions can grow this to
discover all 12 ASIC datasets on data.gov.au via CKAN.
"""
from __future__ import annotations

from rapidfuzz import fuzz

from . import curated as curated_mod
from .models import DatasetSummary


def list_summaries() -> list[DatasetSummary]:
    """All curated datasets as DatasetSummary objects."""
    out: list[DatasetSummary] = []
    for cd in curated_mod.list_all():
        out.append(
            DatasetSummary(
                id=cd.id,
                name=cd.name,
                description=cd.description,
                update_frequency=cd.update_frequency,
                is_curated=True,
            )
        )
    return out


def search(query: str, limit: int = 10) -> list[DatasetSummary]:
    """Fuzzy-search curated datasets — two-pool ranker.

    High-signal pool (id + name + curated.search_keywords) scored with
    token_set_ratio. Description pool capped via WRatio + DESCRIPTION_CAP.
    Prevents the WRatio-collapse-to-57 problem where unrelated datasets
    score identically because every description shares boilerplate.
    """
    if not query.strip():
        raise ValueError(
            "query is required. Try 'financial adviser', 'banned', 'afs licence', "
            "'credit', 'liquidator', or any other ASIC register topic."
        )
    summaries = list_summaries()
    if not summaries:
        return []
    DESCRIPTION_CAP = 30
    KEYWORD_WEIGHT = 0.4  # keywords broaden recall but must NOT outvote name
    PHRASE_BONUS = 15  # query as contiguous substring of id+name
    keyword_lookup = {cd.id: " ".join(cd.search_keywords) for cd in curated_mod.list_all()}
    query_lc = query.lower()
    # Three-pool design: id+name is the PRIMARY discriminator (a curated
    # dataset's own name is the strongest semantic match). Keywords
    # broaden recall but at reduced weight — otherwise an unrelated
    # dataset whose keyword bag happens to contain the query's tokens
    # (e.g. ASIC_BANNED_ORGS having 'banned business name' as a
    # keyword) ties with the actually-named dataset (ASIC_BUSINESS_NAMES)
    # for queries like 'business name register'. Description capped.
    candidates: list[tuple[float, float, int]] = []
    for i, s in enumerate(summaries):
        name_str = f"{s.id} {s.name}".lower()
        kw_str = f"{name_str} {keyword_lookup.get(s.id, '')}".lower()
        desc_str = (s.description or "").lower()
        name_high = fuzz.token_set_ratio(query_lc, name_str)
        kw_high = fuzz.token_set_ratio(query_lc, kw_str)
        desc_raw = fuzz.WRatio(query_lc, desc_str) if desc_str else 0
        desc = min(desc_raw, DESCRIPTION_CAP)
        phrase = PHRASE_BONUS if query_lc and query_lc in kw_str else 0
        raw_adjusted = name_high + kw_high * KEYWORD_WEIGHT + desc * 0.3 + phrase
        candidates.append((raw_adjusted, name_high, i))
    candidates.sort(key=lambda t: (-t[0], -t[1]))
    top_pool = candidates[:limit]
    out: list[DatasetSummary] = []
    if top_pool:
        leader_adj = top_pool[0][0]
        # Proportional scaling against leader's raw so the second-best
        # candidate doesn't pile up at the same clamp ceiling.
        scale_ref = max(leader_adj, 100.0)
        for raw, _name_high, idx in top_pool:
            rel = round(max(0.0, (raw / scale_ref) * 100.0), 1)
            out.append(summaries[idx].model_copy(update={"relevance": rel}))
    return out
