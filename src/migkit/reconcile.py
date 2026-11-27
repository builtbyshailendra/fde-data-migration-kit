"""Reconciliation: prove that every source row and every unit of money is accounted for.

After a load the customer's first question is "did everything arrive?". The
answer has three parts, and each one is checked against the target database
rather than against what the loader believes it wrote:

* counts: every source row was loaded, rejected (with reasons) or merged into
  a duplicate, and every planned row is present in the target with the planned
  values;
* sums: for each ``reconcile_sum`` field, the source total minus the rejected
  and merged-away amounts equals the planned total and the total in the target,
  optionally broken down by a ``reconcile_by`` field such as currency. An
  amount that ``merge_fields`` copied from a duplicate into its survivor was
  loaded, so it is not counted as merged away;
* provenance: the last load recorded in ``migkit_runs`` used this exact
  mapping file (same SHA-256).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, MetaData, select
from sqlalchemy.engine import Connection

from migkit.db import RUNS_TABLE, build_metadata, count_rows, fetch_rows, table_exists
from migkit.diff import RowChange, plain, same_value
from migkit.pipeline import NULL_GROUP, MigrationPlan, TablePlan
from migkit.results import Merge, Reject

MISMATCH_SAMPLE = 20


@dataclass(frozen=True)
class SumLine:
    """One reconciled total: a numeric field, optionally within one group."""

    field: str
    group: str | None
    source: Decimal
    unparsed: int
    rejected: Decimal
    merged: Decimal
    planned: Decimal
    target: Decimal

    @property
    def expected(self) -> Decimal:
        """What should have arrived: the source total less rejected and merged amounts."""
        return self.source - self.rejected - self.merged

    @property
    def ok(self) -> bool:
        return self.expected == self.planned == self.target

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "group": self.group,
            "source": str(self.source),
            "unparsed_rows": self.unparsed,
            "rejected": str(self.rejected),
            "merged": str(self.merged),
            "expected": str(self.expected),
            "planned": str(self.planned),
            "target": str(self.target),
            "ok": self.ok,
        }


@dataclass
class TableReconciliation:
    table: str
    source_rows: int
    rejected_by_stage: dict[str, int]
    merged: int
    planned: int
    target_exists: bool
    found: int = 0
    missing: list[Any] = field(default_factory=list)
    mismatched: list[RowChange] = field(default_factory=list)
    target_rows: int = 0
    sums: list[SumLine] = field(default_factory=list)

    @property
    def rejected(self) -> int:
        return sum(self.rejected_by_stage.values())

    @property
    def balanced(self) -> bool:
        """Source rows = planned + rejected + merged."""
        return self.source_rows == self.planned + self.rejected + self.merged

    @property
    def target_only(self) -> int:
        """Rows in the target that this migration did not produce (left untouched)."""
        return self.target_rows - self.found

    @property
    def ok(self) -> bool:
        return (
            self.balanced
            and not self.missing
            and not self.mismatched
            and all(line.ok for line in self.sums)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "source_rows": self.source_rows,
            "rejected": self.rejected,
            "rejected_by_stage": dict(self.rejected_by_stage),
            "merged": self.merged,
            "planned": self.planned,
            "target_exists": self.target_exists,
            "found_in_target": self.found,
            "missing_in_target": [plain(k) for k in self.missing],
            "mismatched_count": len(self.mismatched),
            "mismatched_sample": [
                {
                    "key": plain(c.key),
                    "changes": {
                        col: {"target": plain(old), "planned": plain(new)}
                        for col, (old, new) in c.changes.items()
                    },
                }
                for c in self.mismatched[:MISMATCH_SAMPLE]
            ],
            "target_rows": self.target_rows,
            "target_only": self.target_only,
            "balanced": self.balanced,
            "sums": [line.to_dict() for line in self.sums],
            "ok": self.ok,
        }


@dataclass
class Reconciliation:
    mapping_name: str
    mapping_checksum: str
    engine: str
    generated_at: datetime
    tables: list[TableReconciliation]
    rejects: list[Reject]
    merges: list[Merge]
    last_run: dict[str, Any] | None = None

    @property
    def mapping_matches_last_run(self) -> bool | None:
        """None when nothing was loaded yet, otherwise whether the checksums agree."""
        if self.last_run is None:
            return None
        return bool(self.last_run["mapping_checksum"] == self.mapping_checksum)

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.tables) and self.mapping_matches_last_run is True

    def table(self, name: str) -> TableReconciliation:
        for item in self.tables:
            if item.table == name:
                return item
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping": {"name": self.mapping_name, "sha256": self.mapping_checksum},
            "engine": self.engine,
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "last_run": self.last_run,
            "mapping_matches_last_run": self.mapping_matches_last_run,
            "ok": self.ok,
            "tables": [t.to_dict() for t in self.tables],
            "rejects": [r.to_dict() for r in self.rejects],
            "merges": [m.to_dict() for m in self.merges],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)


def _group_of(value: Any) -> str:
    return NULL_GROUP if value is None else str(value)


def sum_lines(plan: TablePlan, existing: dict[Any, dict[str, Any]]) -> list[SumLine]:
    """Reconcile every ``reconcile_sum`` field of one table.

    ``existing`` holds the target rows for the planned keys, as read back from
    the database.
    """
    rejected_rows = {r.source_row for r in plan.rejects}
    group_field = plan.target.reconcile_by
    planned_records = plan.records()
    lines: list[SumLine] = []
    for sums in plan.source_sums:
        buckets: dict[str | None, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        unparsed: dict[str | None, int] = defaultdict(int)

        # A duplicate's amount is merged away unless merge_fields copied it into
        # the survivor, in which case it was loaded and stays in the expected total.
        merged_rows = {m.source_row for m in plan.merges if sums.field not in m.filled}

        def group(row: int) -> str | None:
            return plan.source_groups.get(row, NULL_GROUP) if group_field else None

        for row, value in sums.values.items():
            bucket = buckets[group(row)]
            bucket["source"] += value
            if row in rejected_rows:
                bucket["rejected"] += value
            elif row in merged_rows:
                bucket["merged"] += value
        for row in sums.unparsed:
            unparsed[group(row)] += 1
        for record in planned_records:
            value = record[sums.field]
            if value is not None:
                key = _group_of(record[group_field]) if group_field else None
                buckets[key]["planned"] += Decimal(value)
            current = existing.get(record[plan.key])
            if current is not None and current.get(sums.field) is not None:
                target_key = _group_of(current.get(group_field)) if group_field else None
                buckets[target_key]["target"] += Decimal(str(current[sums.field]))
        groups = sorted(set(buckets) | set(unparsed), key=lambda k: (k is None, k or ""))
        for key in groups:
            bucket = buckets[key]
            lines.append(
                SumLine(
                    field=sums.field,
                    group=key,
                    source=bucket["source"],
                    unparsed=unparsed[key],
                    rejected=bucket["rejected"],
                    merged=bucket["merged"],
                    planned=bucket["planned"],
                    target=bucket["target"],
                )
            )
    return lines


def reconcile_table(conn: Connection, metadata: MetaData, plan: TablePlan) -> TableReconciliation:
    """Compare one planned table with what the target database holds."""
    records = plan.records()
    planned = {r[plan.key]: r for r in records}
    result = TableReconciliation(
        table=plan.table,
        source_rows=plan.source_rows,
        rejected_by_stage=plan.rejected_by_stage(),
        merged=len(plan.merges),
        planned=len(records),
        target_exists=table_exists(conn, plan.table),
    )
    existing: dict[Any, dict[str, Any]] = {}
    if result.target_exists:
        table = metadata.tables[plan.table]
        existing = fetch_rows(conn, table, plan.key, planned)
        result.target_rows = count_rows(conn, table)
    for key, row in planned.items():
        current = existing.get(key)
        if current is None:
            result.missing.append(key)
            continue
        result.found += 1
        changes = {
            col: (current.get(col), value)
            for col, value in row.items()
            if not same_value(current.get(col), value)
        }
        if changes:
            result.mismatched.append(RowChange(key=key, changes=changes))
    result.sums = sum_lines(plan, existing)
    return result


def last_run(conn: Connection, metadata: MetaData) -> dict[str, Any] | None:
    """Summary of the most recent load recorded in ``migkit_runs``."""
    if not table_exists(conn, RUNS_TABLE):
        return None
    runs = metadata.tables[RUNS_TABLE]
    latest = conn.execute(select(runs).order_by(runs.c.id.desc()).limit(1)).mappings().first()
    if latest is None:
        return None
    return {
        "run_id": latest["run_id"],
        "mapping_name": latest["mapping_name"],
        "mapping_checksum": latest["mapping_checksum"],
        "engine": latest["engine"],
        "finished_at": latest["finished_at"].isoformat(timespec="seconds"),
    }


def reconcile(plan: MigrationPlan, engine: Engine) -> Reconciliation:
    """Reconcile a plan against the target database. Read-only."""
    metadata = build_metadata(plan.spec)
    with engine.connect() as conn:
        tables = [reconcile_table(conn, metadata, table_plan) for table_plan in plan.tables]
        run = last_run(conn, metadata)
    return Reconciliation(
        mapping_name=plan.spec.name,
        mapping_checksum=plan.spec.checksum,
        engine=plan.engine,
        generated_at=datetime.now(UTC).replace(microsecond=0),
        tables=tables,
        rejects=plan.rejects,
        merges=plan.merges,
        last_run=run,
    )


# --- rendering -------------------------------------------------------------------------------


def _mark(ok: bool) -> str:
    return "OK" if ok else "MISMATCH"


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def render_text(rec: Reconciliation) -> str:
    """A terminal summary: counts per table, sums, and the overall verdict."""
    header = (
        f"{'table':<12} {'source':>6} {'loaded':>6} {'rejected':>8} {'merged':>6} "
        f"{'in target':>9} {'missing':>7} {'differs':>7}  status"
    )
    lines = [header, "-" * len(header)]
    for t in rec.tables:
        lines.append(
            f"{t.table:<12} {t.source_rows:>6} {t.planned:>6} {t.rejected:>8} {t.merged:>6} "
            f"{t.found:>9} {len(t.missing):>7} {len(t.mismatched):>7}  {_mark(t.ok)}"
        )
    sum_rows = [(t.table, line) for t in rec.tables for line in t.sums]
    if sum_rows:
        lines.append("")
        lines.append(
            f"{'sum':<26} {'source':>15} {'unparsed':>8} {'rejected':>13} {'merged':>13} "
            f"{'target':>15}  status"
        )
        for table, line in sum_rows:
            label = f"{table}.{line.field}" + (f" [{line.group}]" if line.group else "")
            lines.append(
                f"{label:<26} {_money(line.source):>15} {line.unparsed:>8} "
                f"{_money(line.rejected):>13} {_money(line.merged):>13} "
                f"{_money(line.target):>15}  {_mark(line.ok)}"
            )
    lines.append("")
    if rec.last_run is None:
        lines.append("no load recorded in migkit_runs")
    else:
        same = "same mapping" if rec.mapping_matches_last_run else "DIFFERENT mapping"
        run = rec.last_run
        lines.append(f"last load {run['run_id']} at {run['finished_at']} ({same})")
    lines.append(f"result: {'RECONCILED' if rec.ok else 'NOT RECONCILED'}")
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(rec: Reconciliation, reject_limit: int = 200) -> str:
    """The customer-facing reconciliation report."""
    verdict = "Reconciled" if rec.ok else "Not reconciled"
    out = [
        f"# Reconciliation report: {rec.mapping_name}",
        "",
        f"- Result: **{verdict}**",
        f"- Generated: {rec.generated_at.isoformat(timespec='seconds')}",
        f"- Mapping SHA-256: `{rec.mapping_checksum or 'n/a'}`",
        f"- Validation engine: {rec.engine}",
    ]
    if rec.last_run is None:
        out.append("- Last load: none recorded")
    else:
        same = "same mapping" if rec.mapping_matches_last_run else "**different mapping**"
        out.append(
            f"- Last load: `{rec.last_run['run_id']}` finished {rec.last_run['finished_at']} "
            f"({same})"
        )
    out += [
        "",
        "## Row counts",
        "",
        "Every source row is either loaded, rejected with a reason, or merged into a "
        "duplicate. Loaded rows are read back from the target and compared field by field.",
        "",
        "| table | source rows | loaded | rejected | merged | balanced | in target "
        "| missing | differs | target-only | status |",
        "|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---|",
    ]
    for t in rec.tables:
        out.append(
            f"| {t.table} | {t.source_rows} | {t.planned} | {t.rejected} | {t.merged} | "
            f"{'yes' if t.balanced else 'no'} | {t.found} | {len(t.missing)} | "
            f"{len(t.mismatched)} | {t.target_only} | {_mark(t.ok)} |"
        )
    out += ["", "### Rejections by stage", "", "| table | transform | reference | validate |"]
    out.append("|---|---:|---:|---:|")
    for t in rec.tables:
        stages = t.rejected_by_stage
        out.append(
            f"| {t.table} | {stages.get('transform', 0)} | {stages.get('reference', 0)} | "
            f"{stages.get('validate', 0)} |"
        )
    sum_rows = [(t.table, line) for t in rec.tables for line in t.sums]
    if sum_rows:
        out += [
            "",
            "## Sums",
            "",
            "Expected = source total - rejected - merged. Values that could not be parsed "
            "are not part of the source total; their rows are counted separately and are "
            "always rejected. An amount a merged duplicate filled into its survivor's empty "
            "field was loaded, so it is not part of the merged column.",
            "",
            "| table | field | group | source | unparsed rows | rejected | merged | expected "
            "| planned | target | status |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for table, line in sum_rows:
            out.append(
                f"| {table} | {line.field} | {line.group or ''} | {_money(line.source)} | "
                f"{line.unparsed} | {_money(line.rejected)} | {_money(line.merged)} | "
                f"{_money(line.expected)} | {_money(line.planned)} | {_money(line.target)} | "
                f"{_mark(line.ok)} |"
            )
    problems = [t for t in rec.tables if t.missing or t.mismatched]
    if problems:
        out += ["", "## Differences in the target", ""]
        for t in problems:
            if t.missing:
                keys = ", ".join(str(k) for k in t.missing[:MISMATCH_SAMPLE])
                out.append(f"- `{t.table}`: {len(t.missing)} planned row(s) missing: {keys}")
            for change in t.mismatched[:MISMATCH_SAMPLE]:
                detail = "; ".join(
                    f"{col} is {plain(old)!r}, expected {plain(new)!r}"
                    for col, (old, new) in change.changes.items()
                )
                out.append(f"- `{t.table}` {change.key}: {_md_cell(detail)}")
    out += ["", f"## Rejected rows ({len(rec.rejects)})", ""]
    if rec.rejects:
        out += [
            "Fix these in the source system or the export and re-run the load; rows "
            "that already loaded are not written again.",
            "",
            "| table | source row | key | stage | reasons |",
            "|---|---:|---|---|---|",
        ]
        for reject in rec.rejects[:reject_limit]:
            out.append(
                f"| {reject.table} | {reject.source_row} | {_md_cell(reject.key or '')} | "
                f"{reject.stage} | {_md_cell('; '.join(reject.reasons))} |"
            )
        if len(rec.rejects) > reject_limit:
            out.append(f"\n{len(rec.rejects) - reject_limit} more in the rejects CSV.")
    else:
        out.append("None.")
    out += ["", f"## Merged duplicates ({len(rec.merges)})", ""]
    if rec.merges:
        out += ["| table | source row | merged key | kept key | score |", "|---|---:|---|---|---:|"]
        for merge in rec.merges:
            out.append(
                f"| {merge.table} | {merge.source_row} | {merge.merged_key} | "
                f"{merge.survivor_key} | {merge.score:.1f} |"
            )
    else:
        out.append("None.")
    return "\n".join(out) + "\n"
