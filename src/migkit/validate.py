"""Validate transformed rows against the rules in the mapping.

Two engines read the same rules:

* ``pandera`` (default) validates the whole frame at once with lazy error
  collection, which is fast and reports every failure in one pass.
* ``pydantic`` validates row by row against a generated model. It is slower but
  produces the familiar pydantic messages, which is useful when the target
  application already defines its records as pydantic models.

Both return the same thing: the rows that passed and a Reject per failed row
with every reason. Uniqueness keeps the first occurrence and rejects the rest.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import pandas as pd
import pandera.pandas as pa
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, create_model
from pydantic import ValidationError as PydanticValidationError

from migkit.mapping import FieldSpec, RowCheck, TargetSpec
from migkit.results import Reject, StageResult, drop_rows
from migkit.sources import SOURCE_ROW

Engine = Literal["pandera", "pydantic"]
ENGINES: tuple[str, ...] = ("pandera", "pydantic")

PY_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "decimal": Decimal,
    "boolean": bool,
    "date": date,
    "datetime": datetime,
}


def _fmt(value: Any) -> str:
    return f"'{value}'"


def rule_checks(spec: FieldSpec) -> list[tuple[str, Callable[[Any], bool], str]]:
    """The value-level rules of a field as (name, predicate, message) triples."""
    rules: list[tuple[str, Callable[[Any], bool], str]] = []
    if spec.pattern is not None:
        regex = re.compile(spec.pattern)
        rules.append(
            (
                "pattern",
                lambda v, rx=regex: bool(rx.fullmatch(str(v))),
                f"does not match pattern {spec.pattern}",
            )
        )
    if spec.allowed is not None:
        allowed = list(spec.allowed)
        rules.append(("allowed", lambda v, a=allowed: v in a, f"is not one of {allowed}"))
    if spec.min is not None:
        low = spec.min
        rules.append(("min", lambda v, lo=low: v >= lo, f"is below the minimum {low:g}"))
    if spec.max is not None:
        high = spec.max
        rules.append(("max", lambda v, hi=high: v <= hi, f"is above the maximum {high:g}"))
    if spec.max_length is not None:
        limit = spec.max_length
        rules.append(
            ("max_length", lambda v, n=limit: len(str(v)) <= n, f"is longer than {limit} chars")
        )
    return rules


# --- pandera ---------------------------------------------------------------------------------


def pandera_schema(target: TargetSpec) -> pa.DataFrameSchema:
    """Build a pandera schema from a target's field rules."""
    columns: dict[str, pa.Column] = {}
    for spec in target.fields:
        checks = [
            pa.Check(predicate, element_wise=True, name=f"{spec.name}:{name}")
            for name, predicate, _ in rule_checks(spec)
        ]
        columns[spec.name] = pa.Column(
            dtype=None,
            checks=checks,
            nullable=not (spec.required or spec.name == target.key),
            unique=spec.unique or spec.name == target.key,
            report_duplicates="exclude_first",
            coerce=False,
        )
    return pa.DataFrameSchema(columns, strict=False, name=target.table)


def _pandera_reason(target: TargetSpec, column: str, check: str, failure: Any) -> str:
    if check == "not_nullable":
        return f"{column}: required value is missing"
    if check == "field_uniqueness":
        return f"{column}: duplicate value {_fmt(failure)}"
    if ":" in check:
        field_name, rule = check.split(":", 1)
        for name, _, message in rule_checks(target.field(field_name)):
            if name == rule:
                return f"{field_name}: {_fmt(failure)} {message}"
    return f"{column}: failed {check} with {_fmt(failure)}"


def validate_pandera(target: TargetSpec, frame: pd.DataFrame) -> StageResult:
    if frame.empty:
        return StageResult(frame=frame)
    schema = pandera_schema(target)
    try:
        schema.validate(frame, lazy=True)
    except pa.errors.SchemaErrors as exc:
        cases = exc.failure_cases
    else:
        return StageResult(frame=frame)

    reasons: dict[int, list[str]] = {}
    for case in cases.to_dict(orient="records"):
        index = case.get("index")
        if index is None or pd.isna(index):
            # A schema-level failure (e.g. a missing column) affects every row.
            raise ValueError(f"table '{target.table}': {case.get('check')}")
        source_row = int(frame.at[int(index), SOURCE_ROW])
        reason = _pandera_reason(
            target, str(case["column"]), str(case["check"]), case["failure_case"]
        )
        reasons.setdefault(source_row, []).append(reason)
    return _split(target, frame, reasons)


# --- pydantic --------------------------------------------------------------------------------


def _after(rules: list[tuple[str, Callable[[Any], bool], str]]) -> Callable[[Any], Any]:
    def check(value: Any) -> Any:
        if value is None:
            return value
        for _, predicate, message in rules:
            if not predicate(value):
                raise ValueError(f"{_fmt(value)} {message}")
        return value

    return check


def pydantic_model(target: TargetSpec) -> type[BaseModel]:
    """Generate a strict pydantic model for a target table."""
    fields: dict[str, Any] = {}
    for spec in target.fields:
        py_type: Any = PY_TYPES[spec.type]
        required = spec.required or spec.name == target.key
        annotation: Any = py_type if required else py_type | None
        annotated = Annotated[annotation, AfterValidator(_after(rule_checks(spec)))]
        fields[spec.name] = (annotated, Field(...) if required else Field(default=None))
    model_name = "".join(part.title() for part in re.split(r"\W+", target.table)) + "Row"
    return create_model(  # type: ignore[call-overload,no-any-return]
        model_name,
        __config__=ConfigDict(strict=True, extra="ignore"),
        **fields,
    )


def validate_pydantic(target: TargetSpec, frame: pd.DataFrame) -> StageResult:
    model = pydantic_model(target)
    reasons: dict[int, list[str]] = {}
    unique_fields = [f.name for f in target.fields if f.unique or f.name == target.key]
    seen: dict[str, set[Any]] = {name: set() for name in unique_fields}
    for record in frame.to_dict(orient="records"):
        source_row = int(record[SOURCE_ROW])
        row_reasons: list[str] = []
        try:
            model.model_validate(record)
        except PydanticValidationError as exc:
            for err in exc.errors():
                where = ".".join(str(p) for p in err["loc"])
                message = err["msg"].removeprefix("Value error, ")
                if err["type"] == "missing" or (
                    err.get("input") is None and err["type"].endswith("_type")
                ):
                    message = "required value is missing"
                row_reasons.append(f"{where}: {message}")
        for name in unique_fields:
            value = record[name]
            if value is None:
                continue
            if value in seen[name]:
                row_reasons.append(f"{name}: duplicate value {_fmt(value)}")
            else:
                seen[name].add(value)
        if row_reasons:
            reasons[source_row] = row_reasons
    return _split(target, frame, reasons)


# --- shared ----------------------------------------------------------------------------------


def _split(target: TargetSpec, frame: pd.DataFrame, reasons: dict[int, list[str]]) -> StageResult:
    by_row = frame.set_index(SOURCE_ROW)[target.key].to_dict()
    rejects = [
        Reject(
            table=target.table,
            stage="validate",
            source_row=row,
            key=None if by_row.get(row) is None else str(by_row[row]),
            reasons=tuple(dict.fromkeys(items)),
        )
        for row, items in sorted(reasons.items())
    ]
    return StageResult(frame=drop_rows(frame, set(reasons)), rejects=rejects)


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}


def row_check_failures(target: TargetSpec, frame: pd.DataFrame) -> dict[int, list[str]]:
    """Evaluate cross-field rules; a rule is skipped when either side is null."""
    failures: dict[int, list[str]] = {}
    if not target.row_checks or frame.empty:
        return failures
    for record in frame.to_dict(orient="records"):
        for check in target.row_checks:
            if not _row_check_passes(check, record):
                failures.setdefault(int(record[SOURCE_ROW]), []).append(
                    f"{check.left}: {_fmt(record[check.left])} fails rule {check.description} "
                    f"({check.right} is {_fmt(record[check.right])})"
                )
    return failures


def _row_check_passes(check: RowCheck, record: dict[str, Any]) -> bool:
    left, right = record.get(check.left), record.get(check.right)
    if left is None or right is None:
        return True
    try:
        return bool(OPERATORS[check.op](left, right))
    except TypeError:
        return False


def validate_frame(target: TargetSpec, frame: pd.DataFrame, engine: str = "pandera") -> StageResult:
    """Validate a transformed frame with the chosen engine, then apply row checks."""
    if engine == "pandera":
        result = validate_pandera(target, frame)
    elif engine == "pydantic":
        result = validate_pydantic(target, frame)
    else:
        raise ValueError(f"unknown validation engine '{engine}', expected one of {ENGINES}")
    failures = row_check_failures(target, result.frame)
    if not failures:
        return result
    extra = _split(target, result.frame, failures)
    rejects = sorted([*result.rejects, *extra.rejects], key=lambda r: r.source_row)
    return StageResult(frame=extra.frame, rejects=rejects)
