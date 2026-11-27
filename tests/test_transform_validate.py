from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from migkit.mapping import FieldSpec, MappingSpec, TargetSpec, parse_mapping
from migkit.sources import SOURCE_ROW
from migkit.transform import FieldPlan, TransformError, apply_references, transform_target
from migkit.validate import ENGINES, pydantic_model, validate_frame

NULLS = {"", "N/A", "NULL"}


def plan_for(lookups: dict[str, dict[str, str]] | None = None, **spec: Any) -> FieldPlan:
    return FieldPlan(FieldSpec(name="f", **spec), NULLS, ["%Y-%m-%d"], lookups or {})


def test_split_join_constant_and_default() -> None:
    row = {"Full": "Mary Ann  Smith", "City": "Leeds", "Zip": "N/A"}
    last = plan_for(split={"column": "Full", "maxsplit": 1, "from_right": True, "index": 1})
    first = plan_for(split={"column": "Full", "maxsplit": 1, "from_right": True, "index": 0})
    assert last.apply(row) == "Smith"
    assert first.apply(row) == "Mary Ann"
    assert plan_for(split={"column": "Full", "index": 5}).apply(row) is None
    assert plan_for(join={"columns": ["City", "Zip"], "sep": ", "}).apply(row) == "Leeds"
    assert plan_for(constant="legacy-crm").apply(row) == "legacy-crm"
    assert plan_for(**{"from": "Zip", "default": "00000"}).apply(row) == "00000"


@pytest.mark.parametrize(
    ("transforms", "raw", "expected"),
    [
        (["lower"], " Jane.Doe@X.COM ", "jane.doe@x.com"),
        (["collapse_whitespace", "title"], "rafael   nunes", "Rafael Nunes"),
        (["digits_only", {"zfill": 5}], "Zip 501", "00501"),
        ([{"replace": ["-", ""]}], "AB-12", "AB12"),
        ([{"regex_replace": ["^(https?://)?(www[.])?", ""]}], "https://www.qelto.com", "qelto.com"),
        ([{"phone_e164": "44"}], "020 7946 0958", "+442079460958"),
        (["alnum_only", "upper"], "ab-12/c", "AB12C"),
    ],
)
def test_string_transforms(transforms: list[Any], raw: str, expected: str) -> None:
    assert plan_for(**{"from": "c", "transforms": transforms}).apply({"c": raw}) == expected


def test_unknown_or_malformed_transform_is_a_mapping_error() -> None:
    with pytest.raises(TransformError, match="unknown transform 'reverse'"):
        plan_for(**{"from": "c", "transforms": ["reverse"]})
    with pytest.raises(TransformError, match="two-item list"):
        plan_for(**{"from": "c", "transforms": [{"replace": "x"}]})


def test_value_map_is_case_insensitive_and_runs_before_lookup() -> None:
    lookups = {"codes": {"Paid": "P"}}
    plan = plan_for(
        lookups,
        **{"from": "s", "value_map": {"PD": "Paid"}, "lookup": {"table": "codes"}},
    )
    assert plan.apply({"s": "pd"}) == "P"


@pytest.mark.parametrize(("on_missing", "expected"), [("null", None), ("keep", "Brevonia")])
def test_lookup_on_missing(on_missing: str, expected: str | None) -> None:
    lookups = {"countries": {"usa": "US"}}
    plan = plan_for(
        lookups, **{"from": "c", "lookup": {"table": "countries", "on_missing": on_missing}}
    )
    assert plan.apply({"c": "USA"}) == "US"
    assert plan.apply({"c": "Brevonia"}) == expected


def test_transform_target_collects_every_reason(mini_spec: MappingSpec) -> None:
    target = mini_spec.target("customers")
    source = pd.DataFrame(
        {
            "Cust No": ["C-1", "C-2"],
            "Company": ["Halcyra", "Bad"],
            "Country": ["USA", "Mars"],
            "Revenue": ["10", "lots"],
            "Since": ["2020-01-01", "yesterday"],
            "Active": ["Y", "Y"],
            SOURCE_ROW: [2, 3],
        }
    )
    result = transform_target(mini_spec, target, source, {"countries": {"usa": "US"}})
    assert list(result.frame["id"]) == ["C-1"]
    assert result.frame.loc[0, "revenue"] == Decimal("10.00")
    assert result.frame.loc[0, "since"] == date(2020, 1, 1)
    (reject,) = result.rejects
    assert reject.source_row == 3
    assert reject.key == "C-2"
    assert len(reject.reasons) == 3
    assert reject.reasons[0].startswith("country: 'Mars' not found")


def test_missing_source_column_fails_fast(mini_spec: MappingSpec) -> None:
    source = pd.DataFrame({"Cust No": ["C-1"], SOURCE_ROW: [2]})
    with pytest.raises(TransformError, match=r"has no column\(s\) \['Active', 'Company'"):
        transform_target(mini_spec, mini_spec.target("customers"), source, {})


def test_apply_references_remaps_merged_parents_and_rejects_orphans(
    mini_spec: MappingSpec,
) -> None:
    target = mini_spec.target("orders")
    frame = pd.DataFrame(
        {
            SOURCE_ROW: [2, 3, 4],
            "order_id": ["O-1", "O-2", "O-3"],
            "customer_id": ["C-1", "C-3", "C-9"],
        }
    ).astype(object)
    result = apply_references(target, frame, {"customers": ({"C-1"}, {"C-3": "C-1"})})
    assert list(result.frame["customer_id"]) == ["C-1", "C-1"]
    (reject,) = result.rejects
    assert reject.stage == "reference"
    assert reject.key == "O-3"


# --- validation: both engines must agree ----------------------------------------------------


def rules_target() -> TargetSpec:
    spec = parse_mapping(
        {
            "sources": {"s": {"path": "s.csv"}},
            "targets": [
                {
                    "table": "invoices",
                    "source": "s",
                    "key": "number",
                    "fields": [
                        {"name": "number", "from": "n", "pattern": r"INV-\d+"},
                        {"name": "status", "from": "s", "allowed": ["paid", "open"]},
                        {"name": "amount", "from": "a", "type": "decimal", "min": 0, "max": 1000},
                        {"name": "note", "from": "t", "max_length": 5},
                        {"name": "issued", "from": "i", "type": "date", "required": True},
                        {"name": "due", "from": "d", "type": "date"},
                    ],
                    "row_checks": [{"left": "due", "op": ">=", "right": "issued"}],
                }
            ],
        }
    )
    return spec.targets[0]


def frame_of(rows: list[dict[str, Any]]) -> pd.DataFrame:
    base = {"status": "paid", "amount": Decimal(1), "note": None, "issued": date(2024, 1, 1)}
    records = [{SOURCE_ROW: i + 2, **base, "due": None, **row} for i, row in enumerate(rows)]
    return pd.DataFrame(records).astype(object).where(lambda f: f.notna(), None)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ({"number": "X-1"}, "number: 'X-1' does not match pattern"),
        ({"number": "INV-2", "status": "lost"}, "status: 'lost' is not one of"),
        ({"number": "INV-3", "amount": Decimal(-5)}, "amount: '-5' is below the minimum 0"),
        ({"number": "INV-4", "amount": Decimal(5000)}, "amount: '5000' is above the maximum"),
        ({"number": "INV-5", "note": "too long"}, "note: 'too long' is longer than 5 chars"),
        ({"number": "INV-6", "issued": None}, "issued: required value is missing"),
        (
            {"number": "INV-7", "due": date(2023, 12, 1)},
            "due: '2023-12-01' fails rule due >= issued",
        ),
    ],
)
def test_rules_reject_with_a_reason(engine: str, row: dict[str, Any], reason: str) -> None:
    frame = frame_of([{"number": "INV-1"}, row])
    result = validate_frame(rules_target(), frame, engine)
    assert list(result.frame["number"]) == ["INV-1"]
    (reject,) = result.rejects
    assert reject.source_row == 3
    assert any(r.startswith(reason) for r in reject.reasons), reject.reasons


@pytest.mark.parametrize("engine", ENGINES)
def test_duplicate_keys_keep_the_first_row(engine: str) -> None:
    frame = frame_of([{"number": "INV-1"}, {"number": "INV-2"}, {"number": "INV-1"}])
    result = validate_frame(rules_target(), frame, engine)
    assert list(result.frame["number"]) == ["INV-1", "INV-2"]
    assert result.rejects[0].reasons == ("number: duplicate value 'INV-1'",)


def test_unknown_engine_and_generated_model() -> None:
    with pytest.raises(ValueError, match="unknown validation engine"):
        validate_frame(rules_target(), frame_of([{"number": "INV-1"}]), "great_expectations")
    model = pydantic_model(rules_target())
    assert model.__name__ == "InvoicesRow"
    assert model.model_fields["issued"].is_required()
    assert not model.model_fields["due"].is_required()
