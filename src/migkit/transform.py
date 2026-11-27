"""Apply a target's field mapping to source rows.

For every field the value goes through the same steps, in this order:

1. extract: ``from`` a column, ``split`` one column, ``join`` several, or a ``constant``
2. trim surrounding whitespace and treat configured placeholders ('N/A', 'NULL') as null
3. string ``transforms`` (lower, title, digits_only, phone_e164, ...)
4. ``value_map`` (case-insensitive) and then ``lookup`` through a reference table
5. ``default`` when the value is still null
6. cast to the field ``type``

Any failure rejects the whole row with every reason collected, so the customer
gets one complete list of problems per record instead of one at a time.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd

from migkit.casting import CastError, cast_value, to_e164
from migkit.mapping import FieldSpec, MappingSpec, TargetSpec
from migkit.results import Reject, StageResult
from migkit.sources import SOURCE_ROW


class TransformError(ValueError):
    """Raised for mapping problems that affect every row, e.g. a missing column."""


def _transform_fn(item: str | dict[str, Any]) -> Callable[[str], str]:
    if isinstance(item, str):
        name, arg = item, None
    else:
        if len(item) != 1:
            raise TransformError(f"transform {item!r} must have exactly one key")
        name, arg = next(iter(item.items()))
    simple: dict[str, Callable[[str], str]] = {
        "strip": str.strip,
        "lower": str.lower,
        "upper": str.upper,
        "title": str.title,
        "collapse_whitespace": lambda v: " ".join(v.split()),
        "digits_only": lambda v: re.sub(r"\D", "", v),
        "alnum_only": lambda v: re.sub(r"[^0-9A-Za-z]", "", v),
    }
    if name in simple:
        return simple[name]
    if name == "phone_e164":
        country = str(arg) if arg is not None else "1"
        return lambda v: to_e164(v, country)
    if name == "zfill":
        width = int(arg) if arg is not None else 0
        return lambda v: v.zfill(width)
    if name == "replace":
        if not isinstance(arg, list) or len(arg) != 2:
            raise TransformError("replace needs a two-item list: [old, new]")
        old, new = str(arg[0]), str(arg[1])
        return lambda v: v.replace(old, new)
    if name == "regex_replace":
        if not isinstance(arg, list) or len(arg) != 2:
            raise TransformError("regex_replace needs a two-item list: [pattern, replacement]")
        pattern, repl = re.compile(str(arg[0])), str(arg[1])
        return lambda v: pattern.sub(repl, v)
    raise TransformError(f"unknown transform '{name}'")


class FieldPlan:
    """A compiled field mapping: everything resolved once, applied per row."""

    def __init__(
        self,
        spec: FieldSpec,
        null_values: set[str],
        date_formats: list[str],
        lookups: Mapping[str, dict[str, str]],
    ) -> None:
        self.spec = spec
        self.null_values = null_values
        self.formats = spec.formats or date_formats
        self.transforms = [_transform_fn(t) for t in spec.transforms]
        self.value_map = (
            {str(k).strip().lower(): v for k, v in spec.value_map.items()}
            if spec.value_map
            else None
        )
        self.lookup: dict[str, str] | None = None
        if spec.lookup is not None:
            table = lookups[spec.lookup.table]
            self.lookup = (
                {k.lower(): v for k, v in table.items()} if spec.lookup.case_insensitive else table
            )

    def _extract(self, row: Mapping[str, str]) -> Any:
        spec = self.spec
        if spec.constant is not None:
            return spec.constant
        if spec.source is not None:
            return row[spec.source]
        if spec.split is not None:
            raw = str(row[spec.split.column]).strip()
            if raw in self.null_values:
                return None
            split = spec.split
            parts = (
                raw.rsplit(split.sep, split.maxsplit)
                if split.from_right
                else raw.split(split.sep, split.maxsplit)
            )
            parts = [p for p in parts if p.strip()]
            try:
                return parts[split.index]
            except IndexError:
                return None
        assert spec.join is not None
        pieces = []
        for column in spec.join.columns:
            piece = str(row[column]).strip()
            if piece and piece not in self.null_values:
                pieces.append(piece)
        return spec.join.sep.join(pieces) if pieces else None

    def apply(self, row: Mapping[str, str]) -> Any:
        """Return the typed value, or raise CastError with the reason."""
        spec = self.spec
        value = self._extract(row)
        if isinstance(value, str):
            value = value.strip()
            if value in self.null_values:
                value = None
        if isinstance(value, str):
            for fn in self.transforms:
                value = fn(value)
            if value == "":
                value = None
        if value is not None and self.value_map is not None:
            mapped_key = str(value).strip().lower()
            if mapped_key in self.value_map:
                value = self.value_map[mapped_key]
        if value is not None and self.lookup is not None:
            assert spec.lookup is not None
            probe = str(value).lower() if spec.lookup.case_insensitive else str(value)
            if probe in self.lookup:
                value = self.lookup[probe]
            elif spec.lookup.on_missing == "reject":
                raise CastError(f"'{value}' not found in lookup '{spec.lookup.table}'")
            elif spec.lookup.on_missing == "null":
                value = None
        if value is None:
            value = spec.default
        if value is None:
            return None
        return cast_value(str(value), spec.type, formats=self.formats, scale=spec.scale)


def check_columns(target: TargetSpec, frame: pd.DataFrame) -> None:
    """Fail fast if the export is missing columns the mapping reads."""
    wanted = {c for f in target.fields for c in f.source_columns}
    missing = sorted(wanted - set(frame.columns))
    if missing:
        raise TransformError(
            f"table '{target.table}': source '{target.source}' has no column(s) {missing}; "
            f"available: {sorted(c for c in frame.columns if c != SOURCE_ROW)}"
        )


def transform_target(
    spec: MappingSpec,
    target: TargetSpec,
    source: pd.DataFrame,
    lookups: Mapping[str, dict[str, str]],
) -> StageResult:
    """Map source rows to target rows; rows with any cast failure are rejected."""
    check_columns(target, source)
    null_values = set(spec.null_values)
    plans = [FieldPlan(f, null_values, spec.date_formats, lookups) for f in target.fields]
    key_plan = next(p for p in plans if p.spec.name == target.key)

    rows: list[dict[str, Any]] = []
    rejects: list[Reject] = []
    for record in source.to_dict(orient="records"):
        out: dict[str, Any] = {SOURCE_ROW: int(record[SOURCE_ROW])}
        reasons: list[str] = []
        for plan in plans:
            try:
                out[plan.spec.name] = plan.apply(record)
            except CastError as exc:
                reasons.append(f"{plan.spec.name}: {exc}")
        if reasons:
            raw_key = key_plan._extract(record)
            rejects.append(
                Reject(
                    table=target.table,
                    stage="transform",
                    source_row=out[SOURCE_ROW],
                    key=None if raw_key is None else str(raw_key).strip(),
                    reasons=tuple(reasons),
                )
            )
            continue
        rows.append(out)

    columns = [SOURCE_ROW, *target.field_names]
    frame = pd.DataFrame(rows, columns=columns).astype(object)
    frame = frame.where(frame.notna(), None)
    return StageResult(frame=frame, rejects=rejects)


def apply_references(
    target: TargetSpec,
    frame: pd.DataFrame,
    parents: Mapping[str, tuple[set[Any], dict[Any, Any]]],
) -> StageResult:
    """Point foreign keys at deduplicated survivors and reject orphans.

    ``parents`` maps a parent table name to (loaded keys, merged-key -> survivor-key).
    A null foreign key is allowed here; ``required`` is a validation rule.
    """
    ref_fields = [f for f in target.fields if f.references is not None]
    if not ref_fields or frame.empty:
        return StageResult(frame=frame)
    frame = frame.copy()
    bad: dict[int, list[str]] = {}
    for spec in ref_fields:
        assert spec.references is not None
        parent_table = spec.references.split(".", 1)[0]
        keys, remap = parents[parent_table]
        values = []
        for source_row, raw in zip(frame[SOURCE_ROW], frame[spec.name], strict=True):
            if raw is None:
                values.append(None)
                continue
            value = remap.get(raw, raw)
            if value not in keys:
                bad.setdefault(int(source_row), []).append(
                    f"{spec.name}: no {parent_table} row with key '{value}'"
                )
            values.append(value)
        frame[spec.name] = values
    rejects = [
        Reject(
            table=target.table,
            stage="reference",
            source_row=row,
            key=_key_of(frame, target.key, row),
            reasons=tuple(reasons),
        )
        for row, reasons in bad.items()
    ]
    kept = frame[~frame[SOURCE_ROW].isin(bad.keys())].reset_index(drop=True)
    return StageResult(frame=kept, rejects=rejects)


def _key_of(frame: pd.DataFrame, key: str, source_row: int) -> str | None:
    match = frame.loc[frame[SOURCE_ROW] == source_row, key]
    if match.empty or match.iloc[0] is None:
        return None
    return str(match.iloc[0])
