"""On-disk Parquet cache for parsed DataFrames.

Mirrors `wgea-mcp` / `aihw-mcp` / `ato-mcp` / `apra-mcp`'s parquet_cache
module. Two roles here:

1. Warm-cache for parsed DataFrames produced by the small-file `read_csv` /
   `read_xlsx` path — same role as the sisters, ~50ms warm reads after
   the first cold parse.

2. **Cold-path destination** for the large streaming-CSV path
   (ASIC_COMPANIES specifically — 600MB+ CSV / 3.5M rows). The streaming
   converter writes directly to a Parquet file in this cache during the
   first fetch; subsequent reads `pd.read_parquet(path, columns=[...])`
   with column projection so peak resident memory stays under ~80MB
   regardless of source size.

Location: defaults to `~/.asic-mcp/parquet-cache/`, overridable via
`ASIC_MCP_PARQUET_CACHE_DIR`.

TTL: 14 days. ASIC registers refresh weekly; 14d gives a slack window
for transient upstream failure without serving stale snapshots
indefinitely.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd

DEFAULT_TTL_SECONDS = 14 * 24 * 60 * 60

_ENV_VAR = "ASIC_MCP_PARQUET_CACHE_DIR"
_DEFAULT_DIR = Path.home() / ".asic-mcp" / "parquet-cache"


def cache_dir() -> Path:
    override = os.environ.get(_ENV_VAR)
    path = Path(override) if override else _DEFAULT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key_to_filename(key: tuple[Any, ...]) -> str:
    payload = repr(key).encode("utf-8")
    return hashlib.sha256(payload).hexdigest() + ".parquet"


def path_for(key: tuple[Any, ...]) -> Path:
    """Return the on-disk Parquet path for a cache key.

    Used by the streaming-CSV path to write directly to the final cache
    location (skipping the in-memory DataFrame round-trip that `write()`
    does for small files).
    """
    return cache_dir() / _key_to_filename(key)


def read_if_fresh(
    key: tuple[Any, ...],
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    columns: list[str] | None = None,
) -> pd.DataFrame | None:
    """Return cached DataFrame if present and within TTL, else None.

    `columns` is forwarded to `pd.read_parquet` as `columns=` — Parquet's
    column-projected read means we only materialise the columns we'll
    actually use, which on the ASIC_COMPANIES register is ~80MB instead
    of ~600MB.
    """
    path = cache_dir() / _key_to_filename(key)
    if not path.is_file():
        return None
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > ttl_seconds:
        return None
    try:
        return pd.read_parquet(path, columns=columns) if columns else pd.read_parquet(path)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        return None


def read_stale(
    key: tuple[Any, ...],
    *,
    columns: list[str] | None = None,
) -> tuple[pd.DataFrame, float] | None:
    """Read the cached parquet regardless of TTL.

    Returns (df, cached_at_epoch) or None if no cache file exists.
    Used by the streaming path's graceful-degradation fallback when
    data.gov.au is unreachable but a previous successful warm exists.
    """
    path = cache_dir() / _key_to_filename(key)
    if not path.is_file():
        return None
    try:
        cached_at = path.stat().st_mtime
    except OSError:
        return None
    try:
        df = pd.read_parquet(path, columns=columns) if columns else pd.read_parquet(path)
    except Exception:
        return None
    return df, cached_at


def write(key: tuple[Any, ...], df: pd.DataFrame) -> None:
    """Write a DataFrame to the cache atomically.

    Used by the small-file path that already has a fully-parsed
    DataFrame in memory. The streaming-CSV path writes via a
    `pyarrow.parquet.ParquetWriter` directly to `path_for(key)` and
    does not call this function.
    """
    target = cache_dir() / _key_to_filename(key)
    tmp = target.with_suffix(".parquet.tmp")
    try:
        df.to_parquet(tmp, engine="pyarrow", compression="snappy", index=False)
        tmp.replace(target)
    except Exception:
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass


def reset_for_tests() -> None:
    d = cache_dir()
    for f in d.glob("*.parquet"):
        try:
            f.unlink()
        except OSError:
            pass
    for f in d.glob("*.parquet.tmp"):
        try:
            f.unlink()
        except OSError:
            pass
