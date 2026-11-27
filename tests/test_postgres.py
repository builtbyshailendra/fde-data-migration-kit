"""Round trip against a real PostgreSQL server.

Skipped unless MIGKIT_TEST_POSTGRES_URL points at a disposable database, e.g.
postgresql+psycopg://postgres:postgres@localhost:5432/migkit. CI runs it
against a PostgreSQL service container; every table it creates is dropped first.
"""

import os
from decimal import Decimal

import pytest

from migkit.db import build_metadata, connect
from migkit.load import load_plan
from migkit.mapping import MappingSpec
from migkit.pipeline import build_plan
from migkit.reconcile import reconcile

URL = os.environ.get("MIGKIT_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(not URL, reason="MIGKIT_TEST_POSTGRES_URL is not set")


def test_sample_round_trip_on_postgres(sample_spec: MappingSpec) -> None:
    assert URL is not None
    engine = connect(URL)
    try:
        build_metadata(sample_spec).drop_all(engine)
        plan = build_plan(sample_spec)
        first = load_plan(plan, engine, batch_size=10)
        assert first.written == 72
        second = load_plan(build_plan(sample_spec), engine)
        assert second.written == 0
        rec = reconcile(plan, engine)
        assert rec.ok
        usd = next(s for s in rec.table("invoices").sums if s.group == "USD")
        assert usd.target == Decimal("504082.59")
    finally:
        engine.dispose()
