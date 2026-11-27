import copy
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, text

from conftest import mini_mapping_data
from migkit.load import load_plan
from migkit.mapping import MappingSpec, load_mapping
from migkit.pipeline import build_plan
from migkit.reconcile import reconcile, render_markdown, render_text


def test_before_any_load_nothing_reconciles(mini_spec: MappingSpec, engine: Engine) -> None:
    rec = reconcile(build_plan(mini_spec), engine)
    assert not rec.ok
    assert rec.last_run is None
    assert rec.mapping_matches_last_run is None
    customers = rec.table("customers")
    assert not customers.target_exists
    assert customers.missing == ["C-1", "C-2"]
    assert customers.balanced
    assert "no load recorded" in render_text(rec)


def test_after_a_load_counts_and_sums_reconcile(mini_spec: MappingSpec, engine: Engine) -> None:
    plan = build_plan(mini_spec)
    load_plan(plan, engine)
    rec = reconcile(plan, engine)
    assert rec.ok, render_text(rec)
    customers = rec.table("customers")
    assert (customers.source_rows, customers.planned, customers.rejected, customers.merged) == (
        5,
        2,
        2,
        1,
    )
    (revenue,) = customers.sums
    assert revenue.group is None
    # 2,530 in the export: 30 rejected (C-4, C-5) and 1,000 merged away (C-3 into C-1).
    assert (revenue.source, revenue.rejected, revenue.merged) == (
        Decimal("2530.00"),
        Decimal("30.00"),
        Decimal("1000.00"),
    )
    assert revenue.expected == revenue.planned == revenue.target == Decimal("1500.00")
    assert "result: RECONCILED" in render_text(rec)


def test_sums_are_broken_down_by_group(mini_spec: MappingSpec, engine: Engine) -> None:
    plan = build_plan(mini_spec)
    load_plan(plan, engine)
    lines = {line.group: line for line in reconcile(plan, engine).table("orders").sums}
    assert set(lines) == {"GBP", "USD"}
    assert lines["GBP"].target == Decimal("7.25")
    assert lines["GBP"].unparsed == 1  # O-5 'oops'
    assert lines["USD"].rejected == Decimal("5.00")  # O-4, orphan
    assert lines["USD"].target == Decimal("120.50")


def test_edited_and_deleted_target_rows_are_reported(
    mini_spec: MappingSpec, engine: Engine
) -> None:
    plan = build_plan(mini_spec)
    load_plan(plan, engine)
    with engine.begin() as conn:
        conn.execute(text("update orders set amount = 99 where order_id = 'O-1'"))
        conn.execute(text("delete from orders where order_id = 'O-3'"))
    rec = reconcile(plan, engine)
    orders = rec.table("orders")
    assert not rec.ok
    assert orders.missing == ["O-3"]
    assert [c.key for c in orders.mismatched] == ["O-1"]
    usd = next(line for line in orders.sums if line.group == "USD")
    assert usd.target == Decimal("119.00")
    assert not usd.ok
    report = render_markdown(rec)
    assert "**Not reconciled**" in report
    assert "1 planned row(s) missing: O-3" in report
    assert "amount is '99" in report


def test_a_different_mapping_fails_provenance(
    write_files: Callable[..., Path], engine: Engine
) -> None:
    load_plan(build_plan(load_mapping(write_files())), engine)
    data = copy.deepcopy(mini_mapping_data())
    data["name"] = "mini-v2"
    rec = reconcile(build_plan(load_mapping(write_files(data))), engine)
    assert all(t.ok for t in rec.tables)
    assert rec.mapping_matches_last_run is False
    assert not rec.ok
    assert "DIFFERENT mapping" in render_text(rec)


def test_report_formats(mini_spec: MappingSpec, engine: Engine) -> None:
    plan = build_plan(mini_spec)
    load_plan(plan, engine)
    rec = reconcile(plan, engine)
    payload = json.loads(rec.to_json())
    assert payload["ok"] is True
    assert payload["mapping"]["name"] == "mini"
    assert payload["tables"][1]["sums"][0]["group"] == "GBP"
    assert len(payload["rejects"]) == 4
    assert payload["merges"][0]["survivor_key"] == "C-1"
    report = render_markdown(rec)
    assert report.startswith("# Reconciliation report: mini")
    assert "| customers | 5 | 2 | 2 | 1 | yes | 2 | 0 | 0 | 0 | OK |" in report
    assert "## Rejected rows (4)" in report
    assert "| customers | 4 | C-3 | C-1 | 100.0 |" in report
