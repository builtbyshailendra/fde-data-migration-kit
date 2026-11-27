"""Deduplication with ``merge_fields`` must not break the money reconciliation."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine

from migkit.dedupe import merge_into
from migkit.load import load_plan
from migkit.mapping import TargetSpec, load_mapping, parse_mapping
from migkit.pipeline import build_plan
from migkit.reconcile import reconcile, render_markdown, render_text
from migkit.results import merges_frame


def mapping(reconcile_by: str | None = None, **dedupe: Any) -> dict[str, Any]:
    target: dict[str, Any] = {
        "table": "people",
        "source": "people",
        "key": "id",
        "fields": [
            {"name": "id", "from": "id", "type": "integer"},
            {"name": "email", "from": "email"},
            {"name": "amount", "from": "amount", "type": "decimal", "reconcile_sum": True},
            {"name": "currency", "from": "currency"},
        ],
        "dedupe": {
            "block_on": ["email"],
            "fields": [{"name": "email", "method": "exact"}],
            "survivor": "first",
            **dedupe,
        },
    }
    if reconcile_by:
        target["reconcile_by"] = reconcile_by
    return {"name": "merge", "sources": {"people": {"path": "people.csv"}}, "targets": [target]}


def write(tmp_path: Path, csv_text: str, data: dict[str, Any]) -> Path:
    (tmp_path / "people.csv").write_text(csv_text, encoding="utf-8")
    path = tmp_path / "mapping.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def target_spec(reconcile_by: str | None) -> TargetSpec:
    return parse_mapping(mapping(reconcile_by)).targets[0]


def test_amount_filled_into_the_survivor_reconciles(tmp_path: Path, engine: Engine) -> None:
    # Row 2 (id 1) survives with an empty amount and takes 100 from its duplicate.
    path = write(
        tmp_path,
        "id,email,amount,currency\n1,a@x.com,,\n2,a@x.com,100,\n3,b@x.com,50,\n",
        mapping(),
    )
    plan = build_plan(load_mapping(path))
    (merge,) = plan.merges
    assert (merge.merged_key, merge.survivor_key, merge.filled) == (2, 1, ("amount",))
    assert merges_frame(plan.merges).loc[0, "filled_fields"] == "amount"
    load_plan(plan, engine)
    rec = reconcile(plan, engine)
    (line,) = rec.table("people").sums
    assert (line.source, line.merged, line.expected, line.planned, line.target) == (
        Decimal("150"),
        Decimal("0"),
        Decimal("150"),
        Decimal("150"),
        Decimal("150"),
    )
    assert rec.ok, render_text(rec)
    assert "result: RECONCILED" in render_text(rec)


def test_amount_not_filled_is_still_merged_away(tmp_path: Path, engine: Engine) -> None:
    path = write(
        tmp_path,
        "id,email,amount,currency\n1,a@x.com,,\n2,a@x.com,100,\n3,b@x.com,50,\n",
        mapping(merge_fields=False),
    )
    plan = build_plan(load_mapping(path))
    assert plan.merges[0].filled == ()
    load_plan(plan, engine)
    rec = reconcile(plan, engine)
    (line,) = rec.table("people").sums
    assert (line.merged, line.target) == (Decimal("100"), Decimal("50"))
    assert rec.ok, render_text(rec)


def test_survivor_amount_is_kept_and_duplicate_amount_merged(
    tmp_path: Path, engine: Engine
) -> None:
    path = write(
        tmp_path,
        "id,email,amount,currency\n1,a@x.com,30,\n2,a@x.com,100,\n",
        mapping(),
    )
    plan = build_plan(load_mapping(path))
    assert plan.merges[0].filled == ()
    load_plan(plan, engine)
    (line,) = reconcile(plan, engine).table("people").sums
    assert (line.source, line.merged, line.target) == (
        Decimal("130"),
        Decimal("100"),
        Decimal("30"),
    )
    assert line.ok


def test_merging_by_currency_never_moves_money_between_groups(
    tmp_path: Path, engine: Engine
) -> None:
    csv_text = (
        "id,email,amount,currency\n"
        "1,a@x.com,,USD\n"  # survivor in USD: the EUR amount must not be copied
        "2,a@x.com,50,EUR\n"
        "3,b@x.com,,\n"  # no amount, no currency: takes GBP and then the GBP amount
        "4,b@x.com,70,GBP\n"
        "5,c@x.com,10,\n"  # has an amount: its group must not change to INR
        "6,c@x.com,20,INR\n"
    )
    plan = build_plan(load_mapping(write(tmp_path, csv_text, mapping("currency"))))
    assert [m.filled for m in plan.merges] == [(), ("currency", "amount"), ()]
    rows = {r["id"]: r for r in plan.table("people").records()}
    assert rows[1]["amount"] is None and rows[1]["currency"] == "USD"
    assert (rows[3]["amount"], rows[3]["currency"]) == (Decimal("70"), "GBP")
    assert (rows[5]["amount"], rows[5]["currency"]) == (Decimal("10"), None)
    load_plan(plan, engine)
    rec = reconcile(plan, engine)
    lines = {line.group: line for line in rec.table("people").sums}
    assert lines["EUR"].merged == Decimal("50") and lines["EUR"].target == 0
    assert lines["GBP"].merged == 0 and lines["GBP"].target == Decimal("70")
    assert lines["INR"].merged == Decimal("20")
    assert lines["(none)"].target == Decimal("10")
    assert rec.ok, render_text(rec)
    assert "filled into its survivor" in render_markdown(rec)


@pytest.mark.parametrize(
    ("survivor", "duplicate", "expected", "filled"),
    [
        ({"amount": None, "currency": "USD"}, {"amount": 5, "currency": "USD"}, 5, ("amount",)),
        ({"amount": None, "currency": "USD"}, {"amount": 5, "currency": "EUR"}, None, ()),
        (
            {"amount": None, "currency": None},
            {"amount": 5, "currency": "EUR"},
            5,
            ("currency", "amount"),
        ),
        ({"amount": 1, "currency": None}, {"amount": 5, "currency": "EUR"}, 1, ()),
    ],
)
def test_merge_into_respects_reconcile_groups(
    survivor: dict[str, Any], duplicate: dict[str, Any], expected: Any, filled: tuple[str, ...]
) -> None:
    record = {"id": 1, "email": "a@x.com", **survivor}
    assert merge_into(record, {"id": 2, "email": None, **duplicate}, target_spec("currency")) == (
        filled
    )
    assert record["amount"] == expected


def test_merge_into_without_groups_fills_every_empty_field() -> None:
    record = {"id": 1, "email": None, "amount": None, "currency": "USD"}
    duplicate = {"id": 2, "email": "a@x.com", "amount": 5, "currency": "EUR"}
    assert merge_into(record, duplicate, target_spec(None)) == ("email", "amount")
    assert record == {"id": 1, "email": "a@x.com", "amount": 5, "currency": "USD"}
