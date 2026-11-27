"""Dry-run diff: what a load would change in the target database.

For every planned row the target is checked by key. A row is an insert when
the key is absent, an update when any column differs, and unchanged
otherwise. Rows that exist only in the target are counted but never touched;
a migration that deletes customer data has to be a deliberate, separate step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, MetaData
from sqlalchemy.engine import Connection

from migkit.db import build_metadata, count_rows, fetch_rows, table_exists
from migkit.pipeline import MigrationPlan, TablePlan


@dataclass(frozen=True)
class RowChange:
    key: Any
    changes: dict[str, tuple[Any, Any]]  # column -> (current, planned)


@dataclass
class TableDiff:
    table: str
    target_exists: bool
    inserts: list[dict[str, Any]] = field(default_factory=list)
    updates: list[RowChange] = field(default_factory=list)
    unchanged: int = 0
    target_rows: int = 0
    target_only: int = 0
    planned_rows: dict[Any, dict[str, Any]] = field(default_factory=dict, repr=False)

    @property
    def writes(self) -> list[dict[str, Any]]:
        """Rows a load has to send: every insert plus the full row of every update."""
        return [*self.inserts, *(self.planned_rows[c.key] for c in self.updates)]

    def to_dict(self, sample: int = 5) -> dict[str, Any]:
        return {
            "table": self.table,
            "target_exists": self.target_exists,
            "inserts": len(self.inserts),
            "updates": len(self.updates),
            "unchanged": self.unchanged,
            "target_rows": self.target_rows,
            "target_only": self.target_only,
            "sample_updates": [
                {
                    "key": plain(c.key),
                    "changes": {
                        col: {"current": plain(old), "planned": plain(new)}
                        for col, (old, new) in c.changes.items()
                    },
                }
                for c in self.updates[:sample]
            ],
        }


def plain(value: Any) -> Any:
    """A JSON-friendly version of a database value."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def same_value(current: Any, planned: Any) -> bool:
    """Compare a stored value with a planned one, tolerating driver representations."""
    if current is None or planned is None:
        return current is None and planned is None
    if isinstance(planned, Decimal) or isinstance(current, Decimal):
        try:
            return Decimal(str(current)) == Decimal(str(planned))
        except ArithmeticError:
            return False
    if isinstance(planned, datetime) and isinstance(current, datetime):
        return planned.replace(tzinfo=None) == current.replace(tzinfo=None)
    if isinstance(planned, date) and isinstance(current, datetime):
        return current.date() == planned
    return bool(current == planned)


def diff_table(conn: Connection, metadata: MetaData, plan: TablePlan) -> TableDiff:
    records = plan.records()
    planned = {r[plan.key]: r for r in records}
    if not table_exists(conn, plan.table):
        return TableDiff(
            table=plan.table, target_exists=False, inserts=records, planned_rows=planned
        )
    table = metadata.tables[plan.table]
    existing = fetch_rows(conn, table, plan.key, planned)
    result = TableDiff(table=plan.table, target_exists=True, planned_rows=planned)
    for key, row in planned.items():
        current = existing.get(key)
        if current is None:
            result.inserts.append(row)
            continue
        changes = {
            col: (current.get(col), value)
            for col, value in row.items()
            if not same_value(current.get(col), value)
        }
        if changes:
            result.updates.append(RowChange(key=key, changes=changes))
        else:
            result.unchanged += 1
    result.target_rows = count_rows(conn, table)
    result.target_only = result.target_rows - len(existing)
    return result


def diff_plan_on(conn: Connection, plan: MigrationPlan) -> list[TableDiff]:
    metadata = build_metadata(plan.spec)
    return [diff_table(conn, metadata, table_plan) for table_plan in plan.tables]


def diff_plan(plan: MigrationPlan, engine: Engine) -> list[TableDiff]:
    """Diff every planned table against the target without writing anything."""
    with engine.connect() as conn:
        return diff_plan_on(conn, plan)


def render_diff(diffs: list[TableDiff], sample: int = 3) -> str:
    header = f"{'table':<16} {'insert':>7} {'update':>7} {'unchanged':>9} {'target-only':>11}"
    lines = [header, "-" * len(header)]
    for d in diffs:
        lines.append(
            f"{d.table:<16} {len(d.inserts):>7} {len(d.updates):>7} {d.unchanged:>9} "
            f"{d.target_only:>11}" + ("" if d.target_exists else "  (table will be created)")
        )
    for d in diffs:
        for change in d.updates[:sample]:
            detail = ", ".join(
                f"{col}: {plain(old)!r} -> {plain(new)!r}"
                for col, (old, new) in change.changes.items()
            )
            lines.append(f"  ~ {d.table} {change.key}: {detail}")
        if len(d.updates) > sample:
            lines.append(f"  ~ {d.table}: {len(d.updates) - sample} more update(s)")
    return "\n".join(lines)
