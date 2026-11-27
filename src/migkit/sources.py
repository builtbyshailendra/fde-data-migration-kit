"""Read legacy CSV and Excel exports as text.

Everything is read as strings. Legacy exports lie about their types (zip codes
with leading zeros, IDs like "007", amounts with currency symbols), so type
inference and casting happen later, where every failure can be recorded with a
reason instead of being silently coerced by the reader.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from migkit.mapping import LookupSpec, MappingError, MappingSpec, SourceSpec

# Tried in order. cp1252 is the usual culprit for exports from old Windows CRMs.
ENCODINGS: tuple[str, ...] = ("utf-8-sig", "cp1252", "latin-1")
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}

# Column holding the 1-based line or row number of each record in its file.
SOURCE_ROW = "__source_row__"

# Free-text notes columns in CRM exports exceed the csv module's 128 KiB default.
csv.field_size_limit(2**31 - 1)


class SourceError(RuntimeError):
    """Raised when a source file cannot be read."""


def detect_encoding(path: Path) -> str:
    """Return the first candidate encoding that decodes the whole file."""
    data = path.read_bytes()
    for encoding in ENCODINGS:
        try:
            data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return encoding
    return "latin-1"  # pragma: no cover - latin-1 decodes any byte sequence


def sniff_delimiter(path: Path, encoding: str) -> str:
    """Guess the delimiter from the first few kilobytes of a CSV file."""
    with path.open(encoding=encoding, newline="") as handle:
        sample = handle.read(16_384)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _header_names(raw: list[object], path: Path) -> list[str]:
    """Stripped header names; blank headers become 'Unnamed: <n>' as in pandas.

    Duplicates are checked here, on the header row as written, because pandas
    would otherwise rename a repeated 'name' to 'name.1' and the mapping would
    quietly read whichever column came first.
    """
    names = [str(v).strip() if v is not None else "" for v in raw]
    names = [name or f"Unnamed: {i}" for i, name in enumerate(names)]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        raise SourceError(f"{path}: duplicate column headers {duplicated}")
    return names


def _is_blank(values: list[str]) -> bool:
    return all(not v.strip() for v in values)


def _read_csv_rows(path: Path, delimiter: str, encoding: str) -> list[tuple[int, list[str]]]:
    """Every non-blank record with the physical line it starts on (1-based).

    A quoted field may span several lines, so the line a record starts on is
    tracked from the reader rather than computed from the record's position.
    """
    rows: list[tuple[int, list[str]]] = []
    with path.open(encoding=encoding, newline="") as handle:
        # strict: an unterminated quote is an error, not the rest of the file
        # swallowed into one field.
        reader = csv.reader(handle, delimiter=delimiter, strict=True)
        next_line = 1
        try:
            for values in reader:
                if not _is_blank(values):
                    rows.append((next_line, values))
                next_line = reader.line_num + 1
        except csv.Error as exc:
            raise SourceError(f"cannot read {path}: line {reader.line_num}: {exc}") from exc
    return rows


def _read_excel_rows(path: Path, sheet: str | None) -> list[tuple[int, list[str]]]:
    """Every non-blank sheet row with its worksheet row number (1-based).

    ``header=None`` keeps leading and interior blank rows, so position + 1 is
    the row number Excel shows, wherever the header sits.
    """
    frame = pd.read_excel(
        path,
        sheet_name=sheet if sheet is not None else 0,
        header=None,
        dtype=str,
        keep_default_na=False,
        engine="openpyxl",
    )
    frame = frame.fillna("").astype(str)
    rows = []
    for position, values in enumerate(frame.itertuples(index=False, name=None), start=1):
        cells = list(values)
        if not _is_blank(cells):
            rows.append((position, cells))
    return rows


def read_table(
    path: str | Path,
    *,
    sheet: str | None = None,
    delimiter: str | None = None,
    encoding: str | None = None,
) -> pd.DataFrame:
    """Read a CSV or Excel file into a DataFrame of stripped-header string columns.

    The first non-blank row is the header; blank rows are skipped. Empty cells
    become empty strings, never NaN, so later stages see exactly what the
    customer exported. A ``__source_row__`` column records where each record
    is in the file: the line it starts on for CSV (as a text editor numbers
    it), the worksheet row for Excel.
    """
    path = Path(path)
    if not path.is_file():
        raise SourceError(f"source file not found: {path}")
    try:
        if path.suffix.lower() in EXCEL_SUFFIXES:
            rows = _read_excel_rows(path, sheet)
        else:
            enc = encoding or detect_encoding(path)
            rows = _read_csv_rows(path, delimiter or sniff_delimiter(path, enc), enc)
    except (ValueError, OSError) as exc:
        raise SourceError(f"cannot read {path}: {exc}") from exc
    if not rows:
        raise SourceError(f"{path}: no header row, the file is empty")

    _, header = rows[0]
    columns = _header_names(header, path)
    width = len(columns)
    data: list[list[str]] = []
    line_numbers: list[int] = []
    for line, values in rows[1:]:
        extra = values[width:]
        if any(v.strip() for v in extra):
            raise SourceError(
                f"{path}: row {line} has {len(values)} fields, the header has {width}"
            )
        data.append(values[:width] + [""] * (width - len(values)))
        line_numbers.append(line)
    frame = pd.DataFrame(data, columns=columns, dtype=object).astype(str)
    frame[SOURCE_ROW] = line_numbers
    return frame


def read_source(spec: MappingSpec, name: str) -> pd.DataFrame:
    """Read a named source from the mapping."""
    source: SourceSpec = spec.sources[name]
    return read_table(
        spec.resolve(source.path),
        sheet=source.sheet,
        delimiter=source.delimiter,
        encoding=source.encoding,
    )


def read_lookup(spec: MappingSpec, name: str) -> dict[str, str]:
    """Load a lookup table into a plain dict."""
    lookup: LookupSpec = spec.lookups[name]
    if lookup.values is not None:
        return dict(lookup.values)
    assert lookup.path is not None
    frame = read_table(spec.resolve(lookup.path))
    missing = {lookup.key, lookup.value} - set(frame.columns)
    if missing:
        raise MappingError(f"lookup '{name}' file has no column(s) {sorted(missing)}")
    return {
        str(k).strip(): str(v).strip()
        for k, v in zip(frame[lookup.key], frame[lookup.value], strict=True)
    }
