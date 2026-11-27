from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import DBAPIError

from conftest import CUSTOMERS_CSV, ORDERS_CSV
from migkit.db import RUNS_TABLE, DatabaseError, batched, build_metadata, upsert_statement
from migkit.diff import diff_plan, render_diff, same_value
from migkit.load import load_plan, render_load
from migkit.mapping import MappingSpec, load_mapping
from migkit.pipeline import build_plan


def test_metadata_follows_the_mapping(mini_spec: MappingSpec) -> None:
    metadata = build_metadata(mini_spec)
    customers = metadata.tables["customers"]
    orders = metadata.tables["orders"]
    assert [c.name for c in customers.primary_key] == ["id"]
    assert not customers.c.name.nullable
    assert customers.c.revenue.type.scale == 2
    assert {fk.target_fullname for fk in orders.c.customer_id.foreign_keys} == {"customers.id"}
    assert RUNS_TABLE in metadata.tables


@pytest.mark.parametrize(
    ("dialect", "name"), [(sqlite.dialect(), "sqlite"), (postgresql.dialect(), "postgresql")]
)
def test_upsert_compiles_to_on_conflict_update(mini_spec: MappingSpec, dialect, name) -> None:
    table = build_metadata(mini_spec).tables["customers"]
    stmt = upsert_statement(table, mini_spec.target("customers"), name)
    sql = str(stmt.compile(dialect=dialect))
    assert "ON CONFLICT (id) DO UPDATE SET" in sql
    assert "name = excluded.name" in sql
    assert "id = excluded.id" not in sql


def test_batched_splits_and_validates() -> None:
    assert [list(b) for b in batched([1, 2, 3, 4, 5], 2)] == [[1, 2], [3, 4], [5]]
    with pytest.raises(ValueError, match="at least 1"):
        list(batched([1], 0))


@pytest.mark.parametrize(
    ("current", "planned", "same"),
    [
        (Decimal("10.0"), Decimal("10.00"), True),
        (10.5, Decimal("10.50"), True),
        (None, None, True),
        (None, "x", False),
        ("a", "b", False),
    ],
)
def test_same_value_tolerates_driver_representations(current, planned, same: bool) -> None:
    assert same_value(current, planned) is same


def test_first_load_inserts_and_second_load_writes_nothing(
    mini_spec: MappingSpec, engine: Engine
) -> None:
    plan = build_plan(mini_spec)
    first = load_plan(plan, engine, batch_size=1)
    assert [(t.table, t.inserted, t.batches) for t in first.tables] == [
        ("customers", 2, 2),
        ("orders", 3, 3),
    ]
    second = load_plan(build_plan(mini_spec), engine)
    assert second.written == 0
    assert [t.unchanged for t in second.tables] == [2, 3]
    with engine.connect() as conn:
        runs = conn.execute(text(f"select run_id, inserted from {RUNS_TABLE}")).all()
    assert len(runs) == 4
    assert len({r.run_id for r in runs}) == 2
    assert "run " + first.run_id in render_load(first)


def test_changed_source_rows_become_updates(
    write_files: Callable[..., Path], engine: Engine
) -> None:
    mapping = write_files()
    load_plan(build_plan(load_mapping(mapping)), engine)
    write_files(
        customers=CUSTOMERS_CSV.replace("Wrenfield,UK,500", "Wrenfield,UK,750"),
        orders=ORDERS_CSV + "O-6,C-1,1,USD,2024-02-01\n",
    )
    plan = build_plan(load_mapping(mapping))
    diffs = {d.table: d for d in diff_plan(plan, engine)}
    assert len(diffs["customers"].updates) == 1
    change = diffs["customers"].updates[0]
    assert change.key == "C-2"
    assert change.changes["revenue"][1] == Decimal("750.00")
    assert len(diffs["orders"].inserts) == 1
    assert "C-2: revenue: '500.00' -> '750.00'" in render_diff(list(diffs.values()))
    result = load_plan(plan, engine)
    assert [(t.inserted, t.updated) for t in result.tables] == [(0, 1), (1, 0)]
    with engine.connect() as conn:
        revenue = conn.execute(text("select revenue from customers where id = 'C-2'")).scalar()
    assert Decimal(str(revenue)) == Decimal("750")


def test_rows_only_in_the_target_are_never_touched(mini_spec: MappingSpec, engine: Engine) -> None:
    load_plan(build_plan(mini_spec), engine)
    with engine.begin() as conn:
        conn.execute(text("insert into customers (id, name) values ('X-1', 'Manual entry')"))
    diffs = diff_plan(build_plan(mini_spec), engine)
    assert diffs[0].target_only == 1
    load_plan(build_plan(mini_spec), engine)
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from customers")).scalar() == 3


def test_dry_run_against_empty_database_creates_nothing(
    mini_spec: MappingSpec, engine: Engine
) -> None:
    diffs = diff_plan(build_plan(mini_spec), engine)
    assert [len(d.inserts) for d in diffs] == [2, 3]
    assert not diffs[0].target_exists
    assert "(table will be created)" in render_diff(diffs)
    assert inspect(engine).get_table_names() == []


def test_a_failing_load_rolls_back_every_table(mini_spec: MappingSpec, engine: Engine) -> None:
    metadata = build_metadata(mini_spec)
    metadata.create_all(engine)
    with engine.begin() as conn:
        # A trigger stands in for any database-side failure halfway through the load.
        conn.execute(
            text(
                "create trigger no_gbp before insert on orders when new.currency = 'GBP' "
                "begin select raise(abort, 'GBP orders are frozen'); end"
            )
        )
    with pytest.raises(DBAPIError, match="GBP orders are frozen"):
        load_plan(build_plan(mini_spec), engine)
    with engine.connect() as conn:
        assert conn.execute(select(metadata.tables["customers"])).all() == []
        assert conn.execute(select(metadata.tables[RUNS_TABLE])).all() == []


def test_no_create_tables_refuses_a_missing_schema(mini_spec: MappingSpec, engine: Engine) -> None:
    with pytest.raises(DatabaseError, match=r"\['customers', 'migkit_runs', 'orders'\]"):
        load_plan(build_plan(mini_spec), engine, create_tables=False)
    build_metadata(mini_spec).create_all(engine)
    result = load_plan(build_plan(mini_spec), engine, create_tables=False)
    assert result.written == 5


def test_batch_size_must_be_positive(mini_spec: MappingSpec, engine: Engine) -> None:
    with pytest.raises(ValueError, match="batch size"):
        load_plan(build_plan(mini_spec), engine, batch_size=0)
