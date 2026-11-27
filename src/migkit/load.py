"""Idempotent load of a migration plan into the target database.

The whole load runs in one transaction: either every table is written or
none is. Inside it, each table is diffed against what is already there and
only new or changed rows are upserted, in batches. Running the same load twice
therefore writes nothing the second time, and re-running after the customer
fixes a handful of rejected rows only sends those rows.

Every load appends one row per table to ``migkit_runs`` with the counts and the
SHA-256 of the mapping file, so "which mapping produced this data?" always has
an answer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Engine

from migkit.db import RUNS_TABLE, DatabaseError, build_metadata, table_exists, upsert_rows
from migkit.diff import TableDiff, diff_plan_on
from migkit.pipeline import MigrationPlan

DEFAULT_BATCH_SIZE = 500


@dataclass(frozen=True)
class TableLoad:
    table: str
    inserted: int
    updated: int
    unchanged: int
    rejected: int
    merged: int
    batches: int


@dataclass
class LoadResult:
    run_id: str
    tables: list[TableLoad] = field(default_factory=list)

    @property
    def written(self) -> int:
        return sum(t.inserted + t.updated for t in self.tables)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def load_plan(
    plan: MigrationPlan,
    engine: Engine,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    create_tables: bool = True,
) -> LoadResult:
    """Write the plan to the target. Parents are loaded before children."""
    if batch_size < 1:
        raise ValueError("batch size must be at least 1")
    metadata = build_metadata(plan.spec)
    run_id = uuid.uuid4().hex
    started = _utcnow()
    result = LoadResult(run_id=run_id)
    with engine.begin() as conn:
        if create_tables:
            metadata.create_all(conn, checkfirst=True)
        else:
            missing = sorted(t for t in metadata.tables if not table_exists(conn, t))
            if missing:
                raise DatabaseError(
                    f"target table(s) {missing} do not exist; create them or drop "
                    "--no-create-tables"
                )
        diffs: list[TableDiff] = diff_plan_on(conn, plan)
        for table_plan, table_diff in zip(plan.tables, diffs, strict=True):
            batches = upsert_rows(
                conn,
                metadata.tables[table_plan.table],
                table_plan.target,
                table_diff.writes,
                batch_size,
            )
            result.tables.append(
                TableLoad(
                    table=table_plan.table,
                    inserted=len(table_diff.inserts),
                    updated=len(table_diff.updates),
                    unchanged=table_diff.unchanged,
                    rejected=len(table_plan.rejects),
                    merged=len(table_plan.merges),
                    batches=batches,
                )
            )
        finished = _utcnow()
        conn.execute(
            metadata.tables[RUNS_TABLE].insert(),
            [
                {
                    "run_id": run_id,
                    "mapping_name": plan.spec.name,
                    "mapping_checksum": plan.spec.checksum or "",
                    "engine": plan.engine,
                    "table_name": t.table,
                    "inserted": t.inserted,
                    "updated": t.updated,
                    "unchanged": t.unchanged,
                    "rejected": t.rejected,
                    "merged": t.merged,
                    "started_at": started,
                    "finished_at": finished,
                }
                for t in result.tables
            ],
        )
    return result


def render_load(result: LoadResult) -> str:
    header = (
        f"{'table':<16} {'inserted':>8} {'updated':>8} {'unchanged':>9} "
        f"{'rejected':>8} {'merged':>6} {'batches':>7}"
    )
    lines = [f"run {result.run_id}", header, "-" * len(header)]
    for t in result.tables:
        lines.append(
            f"{t.table:<16} {t.inserted:>8} {t.updated:>8} {t.unchanged:>9} "
            f"{t.rejected:>8} {t.merged:>6} {t.batches:>7}"
        )
    return "\n".join(lines)
