"""The shipped legacy CRM sample, end to end.

These numbers are quoted in the README and the runbook, so a change to the
sample data or the mapping has to update the documentation as well.
"""

from decimal import Decimal

import pytest
from sqlalchemy import Engine, text

from migkit.load import load_plan
from migkit.mapping import MappingSpec
from migkit.pipeline import build_plan
from migkit.reconcile import reconcile


@pytest.mark.parametrize("engine_name", ["pandera", "pydantic"])
def test_sample_plan_counts(sample_spec: MappingSpec, engine_name: str) -> None:
    plan = build_plan(sample_spec, engine_name)
    counts = {
        t.table: (t.source_rows, len(t.frame), len(t.rejects), len(t.merges)) for t in plan.tables
    }
    assert counts == {
        "customers": (30, 23, 5, 2),
        "contacts": (30, 22, 5, 3),
        "invoices": (36, 27, 9, 0),
    }
    assert all(t.balanced for t in plan.tables)


def test_sample_rejects_explain_themselves(sample_spec: MappingSpec) -> None:
    plan = build_plan(sample_spec)
    reasons = {(r.table, r.key): r.reasons[0] for r in plan.rejects}
    assert (
        reasons[("customers", "ACC-0025")]
        == "country_code: 'Brevonia' not found in lookup 'countries'"
    )
    assert reasons[("contacts", "1013")] == "customer_id: no customers row with key 'ACC-0999'"
    assert reasons[("invoices", "INV-24007")] == "invoice_number: duplicate value 'INV-24007'"
    assert reasons[("invoices", "INV-24033")].startswith("status: 'refunded' is not one of")


def test_sample_duplicates_and_repointed_children(sample_spec: MappingSpec) -> None:
    plan = build_plan(sample_spec)
    assert plan.table("customers").remap == {"ACC-0019": "ACC-0001", "ACC-0028": "ACC-0005"}
    invoices = plan.table("invoices").frame.set_index("invoice_number")
    # INV-24003 and INV-24008 were billed to the duplicate accounts.
    assert invoices.loc["INV-24003", "customer_id"] == "ACC-0001"
    assert invoices.loc["INV-24008", "customer_id"] == "ACC-0005"
    contacts = plan.table("contacts").frame.set_index("contact_id")
    assert contacts.loc[1003, "first_name"] == "Jane"
    assert contacts.loc[1007, "first_name"] == "José"
    assert contacts.loc[1001, "phone"] == "+14155550132"


def test_sample_loads_and_reconciles(sample_spec: MappingSpec, engine: Engine) -> None:
    plan = build_plan(sample_spec)
    load_plan(plan, engine, batch_size=7)
    rec = reconcile(plan, engine)
    assert rec.ok
    eur = next(s for s in rec.table("invoices").sums if s.group == "EUR")
    assert eur.target == Decimal("1234.50")
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from contacts")).scalar() == 22
