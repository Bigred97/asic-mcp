"""XLSX and CSV parsers for ASIC resources on data.gov.au.

Three readers, each appropriate to a different size band:

  - `read_xlsx` — XLSX sheets (small-to-medium files, ~1-3s cold).
  - `read_csv`  — full-pandas CSV load. Fine up to ~50 MB / 360k rows
    (AFS Auth Rep is the worst case in the public registers). Above
    that pandas peaks at >1.5 GB resident which OOMs a 512 MB worker.
  - `stream_csv_to_parquet` — true streaming pyarrow CSV → Parquet,
    used for ASIC_COMPANIES (600+ MB, 3.5 M rows). Peak resident
    memory during conversion is bounded by `block_size_bytes` (~50 MB
    default) regardless of source size. Subsequent reads of the
    cached Parquet via `pd.read_parquet(path, columns=[...])` stay
    under ~80 MB.

Higher-level coercion (rename to aliases, melt transposed time series,
type-convert columns) happens in `shaping.py` guided by the curated
table spec.
"""
from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import pandas as pd


class ParseError(Exception):
    """Raised when an ASIC resource can't be parsed."""


def read_xlsx(
    body: bytes,
    *,
    sheet: str,
    header_row: int,
    data_start_row: int | None = None,
    max_rows: int | None = None,
) -> pd.DataFrame:
    """Read one sheet from an XLSX as a DataFrame.

    Args:
        body: raw bytes of the .xlsx file.
        sheet: sheet name (must exist).
        header_row: 1-indexed row containing column headers (matches Excel's
            row numbering and the convention used in curated YAMLs).
        data_start_row: 1-indexed first row of data. Defaults to header_row + 1.
            Set this when there are blank/spacer rows between header and data.
        max_rows: cap on data rows returned (None = no limit). Useful when
            tables have trailing footnote rows.

    Returns:
        DataFrame indexed 0..N-1. Column names are the raw header strings
        (renaming to plain-English aliases happens in shaping.py).
    """
    if not body:
        raise ParseError("empty XLSX body")
    if header_row < 1:
        raise ParseError(f"header_row must be 1-indexed (>=1), got {header_row}")

    # pandas header= is 0-indexed; user-facing header_row is 1-indexed.
    pandas_header = header_row - 1

    try:
        df = pd.read_excel(
            BytesIO(body),
            sheet_name=sheet,
            header=pandas_header,
            engine="openpyxl",
        )
    except ValueError as e:
        # pandas raises ValueError("Worksheet named '...' not found")
        raise ParseError(f"sheet {sheet!r} not found in workbook: {e}") from e
    except (KeyError, OSError, zipfile.BadZipFile) as e:
        # openpyxl/zipfile raises BadZipFile on non-zip bodies, KeyError for
        # missing zip entries when truncated, and OSError on IO problems.
        # Wrap so callers see a uniform ParseError instead of arbitrary internals.
        raise ParseError(f"could not parse XLSX (corrupt or truncated body): {e}") from e

    # If data_start_row > header_row + 1 there's a spacer row to drop.
    if data_start_row is not None:
        if data_start_row < header_row + 1:
            raise ParseError(
                f"data_start_row ({data_start_row}) must be > header_row ({header_row})"
            )
        skip_after_header = data_start_row - header_row - 1
        if skip_after_header > 0:
            df = df.iloc[skip_after_header:].reset_index(drop=True)

    if max_rows is not None and len(df) > max_rows:
        df = df.iloc[:max_rows].reset_index(drop=True)

    df.columns = [_normalize_header(c) for c in df.columns]
    return df


def _normalize_header(c):
    """Normalize a CSV/XLSX column header.

    ASIC headers usually fit on one line, but some carry embedded newlines.
    We keep the newlines (they're semantically meaningful) but strip padding
    whitespace around them so curated YAMLs only ever need to spell one
    canonical form per column.
    """
    if not isinstance(c, str):
        return c
    parts = c.split("\n")
    parts = [p.strip() for p in parts]
    return "\n".join(parts)


def read_csv(body: bytes, *, encoding: str = "utf-8-sig") -> pd.DataFrame:
    """Read a CSV/TSV body as a DataFrame.

    ASIC labels every register-snapshot file ".csv" on data.gov.au, but the
    actual delimiter varies — some are comma-delimited with quoted fields
    (AFS Licensee, Credit Licensee, Banned Persons, Liquidator), and some
    are tab-delimited (Financial Advisers, AFS Authorised Representative,
    Banned Organisations). We sniff the first line: if it contains tabs and
    has more tabs than commas, use tab; otherwise use comma. Falling back
    to pandas' built-in C engine keeps parsing fast on the 50 MB advisers
    file (~1.5s vs ~6s for the Python engine with sep=None).

    Files mostly ship as UTF-8 with BOM, but ASIC has been known to ship
    Windows-1252 / latin-1 bytes inside a `.csv` file (e.g. a stray smart
    quote or copyright symbol). We try the caller's preferred encoding
    first, then fall back through windows-1252 and iso-8859-1 — the latter
    decodes any byte sequence without raising, so the fallback chain
    always terminates with a successful decode attempt. `low_memory=False`
    prevents mixed-dtype columns (e.g. licence numbers with leading zeros)
    from being silently coerced partway through.
    """
    if not body:
        raise ParseError("empty CSV body")

    # Try the caller's preferred encoding first; fall back to common
    # latin-family encodings when ASIC ships a non-UTF-8 file. iso-8859-1
    # decodes any byte sequence, so the chain always terminates.
    encodings_to_try = [encoding, "windows-1252", "iso-8859-1"]
    # De-duplicate while preserving order in case the caller passed one of
    # the fallback encodings explicitly.
    seen: set[str] = set()
    encodings_to_try = [
        e for e in encodings_to_try if not (e in seen or seen.add(e))
    ]

    last_decode_error: UnicodeDecodeError | None = None
    for enc in encodings_to_try:
        # Sniff delimiter from the first non-empty header line.
        sep = ","
        try:
            head = body[:4096].decode(enc, errors="replace")
            first_line = next((ln for ln in head.splitlines() if ln.strip()), "")
            if first_line.count("\t") > first_line.count(","):
                sep = "\t"
        except Exception:
            sep = ","

        try:
            df = pd.read_csv(
                BytesIO(body),
                encoding=enc,
                sep=sep,
                low_memory=False,
            )
        except UnicodeDecodeError as e:
            last_decode_error = e
            continue
        except pd.errors.ParserError as e:
            raise ParseError(f"CSV parse failed: {e}") from e

        df.columns = [_normalize_header(c) for c in df.columns]
        return df

    raise ParseError(
        f"CSV decode failed with all attempted encodings "
        f"({encodings_to_try!r}): {last_decode_error}"
    ) from last_decode_error


def _sniff_delimiter_from_file(path: Path, *, encoding: str = "utf-8-sig") -> str:
    """Sniff the delimiter from the first non-empty line of a CSV file on disk.

    Mirrors the logic in `read_csv`: if the first line has more tabs
    than commas, return tab; else comma. Used by the streaming path
    where the CSV lives on disk (not in memory) so we can't pre-decode
    bytes from a slice.
    """
    with open(path, "rb") as f:
        head_bytes = f.read(8192)
    try:
        head = head_bytes.decode(encoding, errors="replace")
    except Exception:
        return ","
    first_line = next((ln for ln in head.splitlines() if ln.strip()), "")
    if first_line.count("\t") > first_line.count(","):
        return "\t"
    return ","


def stream_csv_to_parquet(
    csv_path: Path,
    parquet_path: Path,
    *,
    columns: list[str] | None = None,
    block_size_bytes: int = 8 * 1024 * 1024,
    delimiter: str | None = None,
    encoding: str = "utf-8",
    compression: str = "snappy",
) -> int:
    """Stream a CSV file through pyarrow into a Parquet file on disk.

    Peak resident memory is bounded by `block_size_bytes` plus parquet
    writer state (~10-20 MB), regardless of how large the input CSV is.
    Built for ASIC_COMPANIES (600 MB / 3.5 M rows) where `pd.read_csv`
    OOMs a 512 MB Fly worker.

    Args:
        csv_path: local path to the source CSV. Must already be on disk
            (the streaming HTTP fetch in `client.fetch_resource_to_file`
            writes the body here before this function runs).
        parquet_path: destination path. Will be created/replaced atomically
            via a `.parquet.tmp` sidecar so a crashed run never leaves a
            half-written file in the cache.
        columns: subset of source-column headers to keep. If None, all
            columns are passed through. Projection happens at the pyarrow
            read stage so unused columns are skipped without ever being
            materialised into Arrow buffers.
        block_size_bytes: chunk size for the pyarrow CSV reader. Default
            8 MB. Larger blocks = fewer batches but higher peak memory.
        delimiter: '\\t' or ','. If None, sniffs from the first line of
            the file (same heuristic as `read_csv`).
        encoding: text encoding for the CSV. ASIC files are UTF-8 (with
            or without BOM); pyarrow handles BOM transparently if you
            pass utf-8 as the encoding.
        compression: parquet compression. "snappy" is the standard
            balance of size vs CPU.

    Returns:
        Number of rows written to the Parquet file.

    Raises:
        ParseError: pyarrow can't open the CSV (corrupt body, wrong
            encoding, etc.) or the projection matched zero columns.
    """
    import pyarrow as pa
    import pyarrow.csv as pa_csv
    import pyarrow.parquet as pa_parquet

    if not csv_path.is_file():
        raise ParseError(f"streaming CSV source not found: {csv_path}")

    if delimiter is None:
        delimiter = _sniff_delimiter_from_file(csv_path, encoding=encoding)

    read_options = pa_csv.ReadOptions(
        block_size=block_size_bytes,
        encoding=encoding,
    )
    parse_options = pa_csv.ParseOptions(delimiter=delimiter)
    convert_options = pa_csv.ConvertOptions(
        include_columns=columns if columns else None,
        # ASIC mixes blanks and "NULL" sentinels; treat both as missing
        # so downstream pandas reads see NaN/<NA> consistently.
        strings_can_be_null=True,
        null_values=["", "NULL", "null"],
        # Don't try to coerce to numeric/date — keep everything as utf8
        # so shaping.py can apply curated dtype hints on the warm path.
        # ASIC's mixed-format dates (DD/MM/YYYY) and ACN/ABN leading-zero
        # quirks break naive pyarrow type inference anyway.
        column_types=None,
    )

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = parquet_path.with_suffix(parquet_path.suffix + ".tmp")

    rows_written = 0
    writer: pa_parquet.ParquetWriter | None = None
    reader = None
    try:
        try:
            reader = pa_csv.open_csv(
                str(csv_path),
                read_options=read_options,
                parse_options=parse_options,
                convert_options=convert_options,
            )
        except pa.ArrowInvalid as e:
            raise ParseError(
                f"streaming CSV open failed for {csv_path.name}: {e}"
            ) from e

        # Force every column to utf8 in the parquet schema so the warm
        # path reads strings (shaping.py coerces from there per the YAML
        # dtype hints). Inferring numerics here would break ACN/ABN
        # leading-zero preservation.
        first_batch: pa.RecordBatch | None = None
        try:
            first_batch = reader.read_next_batch()
        except StopIteration:
            raise ParseError(
                f"streaming CSV read 0 rows from {csv_path.name}; the file "
                "may have only a header line or be truncated."
            ) from None
        except pa.ArrowInvalid as e:
            raise ParseError(
                f"streaming CSV read failed for {csv_path.name}: {e}"
            ) from e

        all_utf8_schema = pa.schema(
            [pa.field(f.name, pa.string()) for f in first_batch.schema]
        )
        writer = pa_parquet.ParquetWriter(
            str(tmp_path), all_utf8_schema, compression=compression
        )

        def _coerce_to_utf8(batch: pa.RecordBatch) -> pa.RecordBatch:
            cols = [
                batch.column(i).cast(pa.string(), safe=False)
                for i in range(batch.num_columns)
            ]
            return pa.RecordBatch.from_arrays(cols, names=all_utf8_schema.names)

        first_utf8 = _coerce_to_utf8(first_batch)
        writer.write_table(pa.Table.from_batches([first_utf8]))
        rows_written += first_utf8.num_rows

        while True:
            try:
                batch = reader.read_next_batch()
            except StopIteration:
                break
            except pa.ArrowInvalid as e:
                raise ParseError(
                    f"streaming CSV read failed for {csv_path.name}: {e}"
                ) from e
            coerced = _coerce_to_utf8(batch)
            writer.write_table(pa.Table.from_batches([coerced]))
            rows_written += coerced.num_rows

        writer.close()
        writer = None
        tmp_path.replace(parquet_path)
        return rows_written
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass
        if tmp_path.is_file():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def drop_blank_rows(df: pd.DataFrame, key_columns: list[str]) -> pd.DataFrame:
    """Drop rows where every column in `key_columns` is NaN.

    Use this to trim trailing footnote / blank rows. We require ALL key columns
    to be NaN before discarding — a single non-null in any key column means
    the row is real.
    """
    present = [c for c in key_columns if c in df.columns]
    if not present:
        return df
    keep_mask = ~df[present].isna().all(axis=1)
    return df.loc[keep_mask].reset_index(drop=True)
