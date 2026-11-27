"""Run every offline stage of a migration and collect the result as a plan.

The plan is everything the migration would do, computed without touching the
target database: the rows each table would receive, every rejected source row
with its reasons, and every duplicate that was folded into a survivor. The
``plan``, ``validate``, ``load`` and ``reconcile`` commands all start here, so
they always agree on what the migration contains.

Stages per target table, in mapping order (parents before children):

1. transform   cast and reshape source rows (``transform.transform_target``)
2. reference   repoint foreign keys at deduplicated parents, reject orphans
3. validate    pandera or pydantic rules plus cross-field row checks
4. dedupe      fuzzy-merge duplicates when the target has a ``dedupe`` block
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pandas as pd

from migkit.casting import CastError
from migkit.dedupe import deduplicate
from migkit.mapping import MappingSpec, TargetSpec
from migkit.results import Merge, Reject
from migkit.sources import SOURCE_ROW, read_lookup, read_source
from migkit.transform import FieldPlan, apply_references, transform_target
from migkit.validate import validate_frame

STAGES: tuple[str, ...] = ("transform", "reference", "validate")


UNPARSED_GROUP = "(invalid)"
NULL_GROUP = "(none)"


@dataclass(frozen=True)
class SourceSums:
    """Values of a ``reconcile_sum`` field read straight from the export.

    ``values`` holds every source row whose value could be parsed (nulls
    excluded), keyed by source row, so the reconciliation can say how much of
    the source total was loaded, rejected or merged away.
    """

    field: str
    values: dict[int, Decimal]
    unparsed: frozenset[int]

    @property
    def total(self) -> Decimal:
        return sum(self.values.values(), Decimal(0))


@dataclass
class TablePlan:
    """What one target table would receive."""

    target: TargetSpec
    source_rows: int
    frame: pd.DataFrame
    rejects: list[Reject] = field(default_factory=list)
    merges: list[Merge] = field(default_factory=list)
    source_sums: list[SourceSums] = field(default_factory=list)
    # source row -> value of ``reconcile_by``; empty when the target has none.
    source_groups: dict[int, str] = field(default_factory=dict)

    @property
    def table(self) -> str:
        return self.target.table

    @property
    def key(self) -> str:
        return self.target.key

    @property
    def keys(self) -> set[Any]:
        return set(self.frame[self.key])

    @property
    def remap(self) -> dict[Any, Any]:
        return {m.merged_key: m.survivor_key for m in self.merges}

    def rejected_by_stage(self) -> dict[str, int]:
        counts = dict.fromkeys(STAGES, 0)
        for reject in self.rejects:
            counts[reject.stage] = counts.get(reject.stage, 0) + 1
        return counts

    def records(self) -> list[dict[str, Any]]:
        """Rows to load, without the bookkeeping source-row column."""
        return self.frame.drop(columns=[SOURCE_ROW]).to_dict(orient="records")

    @property
    def balanced(self) -> bool:
        """Every source row is accounted for: loaded, rejected or merged."""
        return self.source_rows == len(self.frame) + len(self.rejects) + len(self.merges)


@dataclass
class MigrationPlan:
    spec: MappingSpec
    engine: str
    tables: list[TablePlan]

    def table(self, name: str) -> TablePlan:
        for plan in self.tables:
            if plan.table == name:
                return plan
        raise KeyError(name)

    @property
    def rejects(self) -> list[Reject]:
        return [r for t in self.tables for r in t.rejects]

    @property
    def merges(self) -> list[Merge]:
        return [m for t in self.tables for m in t.merges]


def load_lookups(spec: MappingSpec) -> dict[str, dict[str, str]]:
    return {name: read_lookup(spec, name) for name in spec.lookups}


def source_sums(
    spec: MappingSpec,
    target: TargetSpec,
    source: pd.DataFrame,
    lookups: Mapping[str, dict[str, str]],
) -> list[SourceSums]:
    """Read ``reconcile_sum`` fields from the raw export, before anything is rejected.

    Finance teams reconcile against the total in their old system, so this is
    the number the loaded total has to be explained against.
    """
    sums: list[SourceSums] = []
    null_values = set(spec.null_values)
    records = source.to_dict(orient="records")
    for field_spec in target.fields:
        if not field_spec.reconcile_sum:
            continue
        plan = FieldPlan(field_spec, null_values, spec.date_formats, lookups)
        values: dict[int, Decimal] = {}
        unparsed: set[int] = set()
        for record in records:
            row = int(record[SOURCE_ROW])
            try:
                value = plan.apply(record)
            except CastError:
                unparsed.add(row)
                continue
            if value is not None:
                values[row] = Decimal(value)
        sums.append(SourceSums(field=field_spec.name, values=values, unparsed=frozenset(unparsed)))
    return sums


def source_groups(
    spec: MappingSpec,
    target: TargetSpec,
    source: pd.DataFrame,
    lookups: Mapping[str, dict[str, str]],
) -> dict[int, str]:
    """The ``reconcile_by`` value of every source row, as it would be loaded."""
    if target.reconcile_by is None:
        return {}
    plan = FieldPlan(
        target.field(target.reconcile_by), set(spec.null_values), spec.date_formats, lookups
    )
    groups: dict[int, str] = {}
    for record in source.to_dict(orient="records"):
        try:
            value = plan.apply(record)
        except CastError:
            groups[int(record[SOURCE_ROW])] = UNPARSED_GROUP
            continue
        groups[int(record[SOURCE_ROW])] = NULL_GROUP if value is None else str(value)
    return groups


def plan_table(
    spec: MappingSpec,
    target: TargetSpec,
    source: pd.DataFrame,
    lookups: Mapping[str, dict[str, str]],
    parents: Mapping[str, TablePlan],
    *,
    engine: str = "pandera",
) -> TablePlan:
    """Run the offline stages for one target table."""
    transformed = transform_target(spec, target, source, lookups)
    parent_index = {name: (plan.keys, plan.remap) for name, plan in parents.items()}
    referenced = apply_references(target, transformed.frame, parent_index)
    validated = validate_frame(target, referenced.frame, engine)
    frame, merges = validated.frame, []
    if target.dedupe is not None:
        deduped = deduplicate(target, frame, target.dedupe)
        frame, merges = deduped.frame, deduped.merges
    rejects = sorted(
        [*transformed.rejects, *referenced.rejects, *validated.rejects],
        key=lambda r: r.source_row,
    )
    return TablePlan(
        target=target,
        source_rows=len(source),
        frame=frame,
        rejects=rejects,
        merges=merges,
        source_sums=source_sums(spec, target, source, lookups),
        source_groups=source_groups(spec, target, source, lookups),
    )


def build_plan(spec: MappingSpec, engine: str = "pandera") -> MigrationPlan:
    """Read every source and plan every target table in mapping order."""
    lookups = load_lookups(spec)
    sources: dict[str, pd.DataFrame] = {}
    planned: dict[str, TablePlan] = {}
    for target in spec.targets:
        if target.source not in sources:
            sources[target.source] = read_source(spec, target.source)
        planned[target.table] = plan_table(
            spec, target, sources[target.source], lookups, planned, engine=engine
        )
    return MigrationPlan(spec=spec, engine=engine, tables=list(planned.values()))


def render_summary(plan: MigrationPlan) -> str:
    """Per-table counts: what would load, what was rejected at which stage, what merged."""
    header = (
        f"{'table':<12} {'source':>6} {'load':>6} {'transform':>9} {'reference':>9} "
        f"{'validate':>8} {'merged':>6}  balanced"
    )
    lines = [header, "-" * len(header)]
    for t in plan.tables:
        stages = t.rejected_by_stage()
        lines.append(
            f"{t.table:<12} {t.source_rows:>6} {len(t.frame):>6} {stages['transform']:>9} "
            f"{stages['reference']:>9} {stages['validate']:>8} {len(t.merges):>6}  "
            f"{'yes' if t.balanced else 'NO'}"
        )
    return "\n".join(lines)


def render_rejects(plan: MigrationPlan, limit: int = 50) -> str:
    """One line per rejected source row, with every reason."""
    rejects = plan.rejects
    if not rejects:
        return "no rejected rows"
    lines = [f"rejected rows ({len(rejects)}):"]
    for r in rejects[:limit]:
        lines.append(
            f"  {r.table} row {r.source_row} [{r.key or '-'}] {r.stage}: {'; '.join(r.reasons)}"
        )
    if len(rejects) > limit:
        lines.append(f"  ... {len(rejects) - limit} more (use --rejects to write them all)")
    return "\n".join(lines)


def render_merges(plan: MigrationPlan) -> str:
    merges = plan.merges
    if not merges:
        return "no duplicates merged"
    lines = [f"merged duplicates ({len(merges)}):"]
    lines.extend(
        f"  {m.table} row {m.source_row}: {m.merged_key} -> {m.survivor_key} (score {m.score:.1f})"
        for m in merges
    )
    return "\n".join(lines)
