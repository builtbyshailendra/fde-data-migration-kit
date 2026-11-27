"""Profile legacy exports before writing a mapping.

The profile answers the questions a forward-deployed engineer asks in the first
hour with a new customer: what type is each column really, how many values are
missing (including the placeholders people type instead of leaving a cell
empty), which column could be a key, and which formats are mixed together.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field

import pandas as pd

from migkit.casting import (
    CastError,
    date_format_of,
    is_email,
    is_phone,
    parse_boolean,
    parse_decimal,
    parse_integer,
)
from migkit.mapping import DEFAULT_DATE_FORMATS, DEFAULT_NULL_VALUES
from migkit.sources import SOURCE_ROW

# Share of non-null values that must parse for a type to be inferred. Legacy
# exports always carry a few bad values, so this is deliberately below 100%:
# the profile reports the type the column is meant to have and counts the rest.
TYPE_CONFIDENCE = 0.90
PROFILE_DATE_FORMATS: tuple[str, ...] = (
    *DEFAULT_DATE_FORMATS,
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d.%m.%Y",
    "%d/%m/%Y %H:%M",
    "%m/%d/%Y %H:%M",
    "%d.%m.%Y %H:%M",
)
# Types worth warning about when a column is almost, but not quite, unique.
IDENTIFIER_TYPES = frozenset({"string", "integer", "email"})


@dataclass
class ColumnProfile:
    name: str
    rows: int
    nulls: int
    placeholder_nulls: int
    distinct: int
    inferred_type: str
    type_confidence: float
    min_length: int
    max_length: int
    top_values: list[tuple[str, int]] = field(default_factory=list)
    patterns: list[tuple[str, int]] = field(default_factory=list)
    date_formats: list[tuple[str, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def null_ratio(self) -> float:
        return self.nulls / self.rows if self.rows else 0.0

    @property
    def uniqueness(self) -> float:
        non_null = self.rows - self.nulls
        return self.distinct / non_null if non_null else 0.0

    @property
    def candidate_key(self) -> bool:
        return self.rows > 0 and self.nulls == 0 and self.distinct == self.rows

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data.update(
            null_ratio=round(self.null_ratio, 4),
            uniqueness=round(self.uniqueness, 4),
            candidate_key=self.candidate_key,
        )
        return data


@dataclass
class TableProfile:
    source: str
    rows: int
    columns: list[ColumnProfile]

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "rows": self.rows,
            "columns": [c.to_dict() for c in self.columns],
        }


def shape_of(value: str) -> str:
    """Reduce a value to its shape: digits become 9, letter runs become A+/a+.

    '(555) 010-2233' -> '(999) 999-9999'; 'ada@example.com' -> 'a+@a+.a+'.
    Digits are kept one-for-one because their count is usually the signal
    (zip codes, phone numbers, dates); letter runs are collapsed because word
    length rarely is.
    """
    out: list[str] = []
    for ch in value:
        if ch.isdigit():
            token = "9"
        elif ch.isalpha():
            token = "A" if ch.isupper() else "a"
        elif ch.isspace():
            token = " "
        else:
            token = ch
        out.append(token)
    shape = "".join(out)
    shape = re.sub(r"(A{2,})", "A+", shape)
    shape = re.sub(r"(a{2,})", "a+", shape)
    return re.sub(r" {2,}", " ", shape)


def _share(values: Iterable[str], predicate: Callable[[str], bool]) -> float:
    items = list(values)
    if not items:
        return 0.0
    return sum(1 for v in items if predicate(v)) / len(items)


def _parses(parser: Callable[[str], object]) -> Callable[[str], bool]:
    def check(value: str) -> bool:
        try:
            parser(value)
        except CastError:
            return False
        return True

    return check


def infer_type(values: list[str]) -> tuple[str, float]:
    """Infer the most specific type that at least 95% of values satisfy."""
    if not values:
        return "empty", 1.0
    lowered = {v.strip().lower() for v in values}
    if lowered <= {"true", "false", "yes", "no", "y", "n", "t", "f"}:
        return "boolean", 1.0
    candidates: list[tuple[str, Callable[[str], bool]]] = [
        ("integer", _parses(parse_integer)),
        ("decimal", _parses(lambda v: parse_decimal(v, scale=None))),
        ("date", lambda v: date_format_of(v, PROFILE_DATE_FORMATS) is not None),
        ("email", is_email),
        ("phone", is_phone),
        ("boolean", _parses(parse_boolean)),
    ]
    for name, predicate in candidates:
        share = _share(values, predicate)
        if share >= TYPE_CONFIDENCE:
            inferred = "datetime" if name == "date" and _has_time(values) else name
            return inferred, round(share, 4)
    return "string", 1.0


def _has_time(values: list[str]) -> bool:
    """True when most values parse with a format that carries a time of day.

    Excel date cells read as text always come with ' 00:00:00', so a time that
    is midnight in every row does not make a column a datetime.
    """
    timed = 0
    for value in values:
        fmt = date_format_of(value, PROFILE_DATE_FORMATS)
        if fmt and "%H" in fmt and not value.strip().endswith("00:00:00"):
            timed += 1
    return timed >= len(values) * TYPE_CONFIDENCE


def profile_column(
    name: str,
    series: pd.Series,
    null_values: Iterable[str] = DEFAULT_NULL_VALUES,
    top: int = 5,
) -> ColumnProfile:
    texts = [str(v) for v in series.tolist()]
    null_set = set(null_values)
    stripped = [t.strip() for t in texts]
    is_null = [s in null_set for s in stripped]
    placeholders = sum(1 for s, n in zip(stripped, is_null, strict=True) if n and s != "")
    values = [t for t, n in zip(texts, is_null, strict=True) if not n]
    clean = [v.strip() for v in values]

    inferred, confidence = infer_type(clean)
    lengths = [len(v) for v in values] or [0]
    counts = Counter(clean)
    patterns = Counter(shape_of(v) for v in clean)
    formats: Counter[str] = Counter()
    if inferred in ("date", "datetime"):
        for v in clean:
            fmt = date_format_of(v, PROFILE_DATE_FORMATS)
            if fmt:
                formats[fmt] += 1

    profile = ColumnProfile(
        name=name,
        rows=len(texts),
        nulls=sum(is_null),
        placeholder_nulls=placeholders,
        distinct=len(counts),
        inferred_type=inferred,
        type_confidence=confidence,
        min_length=min(lengths),
        max_length=max(lengths),
        top_values=counts.most_common(top),
        patterns=patterns.most_common(top),
        date_formats=formats.most_common(),
    )
    profile.warnings = _warnings(profile, values, clean)
    return profile


def _warnings(profile: ColumnProfile, values: list[str], clean: list[str]) -> list[str]:
    notes: list[str] = []
    padded = sum(1 for raw, s in zip(values, clean, strict=True) if raw != s)
    if padded:
        notes.append(f"{padded} value(s) have leading or trailing whitespace")
    if profile.placeholder_nulls:
        notes.append(f"{profile.placeholder_nulls} placeholder null(s) such as 'N/A' or 'NULL'")
    if len(profile.date_formats) > 1:
        mix = ", ".join(f"{fmt} ({n})" for fmt, n in profile.date_formats)
        notes.append(f"mixed date formats: {mix}")
    if profile.type_confidence < 1.0:
        bad = round((1 - profile.type_confidence) * (profile.rows - profile.nulls))
        notes.append(f"{bad} value(s) do not parse as {profile.inferred_type}")
    case_groups = Counter(v.lower() for v in set(clean))
    variants = sum(1 for n in case_groups.values() if n > 1)
    if variants:
        notes.append(f"{variants} value(s) appear with different capitalisation")
    non_null = profile.rows - profile.nulls
    if non_null and profile.inferred_type in IDENTIFIER_TYPES and 0.9 <= profile.uniqueness < 1.0:
        dupes = non_null - profile.distinct
        notes.append(f"nearly unique: {dupes} duplicate value(s), check before using as a key")
    if profile.rows and profile.nulls == profile.rows:
        notes.append("column is entirely empty")
    return notes


def profile_frame(
    frame: pd.DataFrame,
    source: str,
    null_values: Iterable[str] = DEFAULT_NULL_VALUES,
    top: int = 5,
) -> TableProfile:
    """Profile every column of a frame read by ``sources.read_table``."""
    columns = [c for c in frame.columns if c != SOURCE_ROW]
    return TableProfile(
        source=source,
        rows=len(frame),
        columns=[profile_column(c, frame[c], null_values, top) for c in columns],
    )


def render_text(profile: TableProfile) -> str:
    """Render a profile as a fixed-width table followed by per-column findings."""
    header = (
        f"{'column':<22} {'type':<8} {'conf':>5} {'nulls':>6} {'distinct':>8} "
        f"{'unique':>6}  top pattern"
    )
    lines = [f"{profile.source}: {profile.rows} rows, {len(profile.columns)} columns", header]
    lines.append("-" * len(header))
    for col in profile.columns:
        pattern = col.patterns[0][0] if col.patterns else ""
        key = "  [key]" if col.candidate_key else ""
        lines.append(
            f"{col.name[:22]:<22} {col.inferred_type:<8} {col.type_confidence:>5.2f} "
            f"{col.nulls:>6} {col.distinct:>8} {col.uniqueness:>6.2f}  {pattern[:28]}{key}"
        )
    findings = [(c.name, w) for c in profile.columns for w in c.warnings]
    if findings:
        lines.append("")
        lines.append("findings:")
        lines.extend(f"  {name}: {warning}" for name, warning in findings)
    return "\n".join(lines)


def render_markdown(profile: TableProfile) -> str:
    lines = [
        f"## {profile.source}",
        "",
        f"{profile.rows} rows, {len(profile.columns)} columns.",
        "",
        "| column | type | nulls | distinct | uniqueness | top patterns | findings |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for col in profile.columns:
        patterns = ", ".join(f"`{p}` ({n})" for p, n in col.patterns[:3])
        findings = "; ".join(col.warnings) or ""
        key = " (candidate key)" if col.candidate_key else ""
        lines.append(
            f"| {col.name}{key} | {col.inferred_type} | {col.nulls} | {col.distinct} | "
            f"{col.uniqueness:.2f} | {patterns} | {findings} |"
        )
    return "\n".join(lines) + "\n"
