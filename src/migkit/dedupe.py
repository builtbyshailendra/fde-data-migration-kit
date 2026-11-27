"""Fuzzy deduplication of customers, contacts and similar records.

Comparing every record with every other is quadratic, so candidate pairs come
from blocking: two records are compared only if they share a value in at least
one ``block_on`` field (multi-pass blocking, so a typo in the postcode does not
hide a duplicate that shares an email address). Each pair gets a weighted
similarity score from rapidfuzz; pairs at or above the threshold are joined
with union-find, and each cluster collapses into one surviving record.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import pandas as pd
from rapidfuzz import fuzz

from migkit.mapping import DedupeField, DedupeSpec, TargetSpec
from migkit.results import Merge
from migkit.sources import SOURCE_ROW

COMPANY_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "co",
        "corp",
        "corporation",
        "company",
        "plc",
        "gmbh",
        "pvt",
        "the",
    }
)

SCORERS = {
    "ratio": fuzz.ratio,
    "token_sort": fuzz.token_sort_ratio,
    "token_set": fuzz.token_set_ratio,
}


def normalise(value: Any, strip_suffixes: bool = False) -> str:
    """Lower-case, drop punctuation, collapse whitespace, optionally drop 'Inc', 'LLC'..."""
    text = re.sub(r"[^\w\s]", " ", str(value).lower())
    tokens = text.split()
    if strip_suffixes:
        tokens = [t for t in tokens if t not in COMPANY_SUFFIXES] or tokens
    return " ".join(tokens)


def field_score(a: Any, b: Any, spec: DedupeField) -> float | None:
    """Similarity 0-100 of two values, or None when either side is missing."""
    if a is None or b is None:
        return None
    left, right = normalise(a, spec.strip_suffixes), normalise(b, spec.strip_suffixes)
    if not left or not right:
        return None
    if spec.method == "exact":
        return 100.0 if left == right else 0.0
    return float(SCORERS[spec.method](left, right))


def record_score(a: dict[str, Any], b: dict[str, Any], fields: list[DedupeField]) -> float:
    """Weighted mean of field scores, ignoring fields missing on either side."""
    total = weight = 0.0
    for spec in fields:
        score = field_score(a.get(spec.name), b.get(spec.name), spec)
        if score is None:
            continue
        total += score * spec.weight
        weight += spec.weight
    return total / weight if weight else 0.0


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[max(root_a, root_b)] = min(root_a, root_b)


def candidate_pairs(records: list[dict[str, Any]], block_on: list[str]) -> set[tuple[int, int]]:
    """Index pairs that share at least one blocking value."""
    pairs: set[tuple[int, int]] = set()
    for column in block_on:
        blocks: dict[str, list[int]] = defaultdict(list)
        for idx, record in enumerate(records):
            value = record.get(column)
            if value is None:
                continue
            key = normalise(value)
            if key:
                blocks[key].append(idx)
        for members in blocks.values():
            pairs.update(combinations(members, 2))
    return pairs


def _completeness(record: dict[str, Any], names: list[str]) -> int:
    return sum(1 for n in names if record.get(n) is not None)


def _pick_survivor(cluster: list[int], records: list[dict[str, Any]], rule: str, names: list[str]):
    if rule == "first":
        return min(cluster)
    if rule.startswith("latest:"):
        column = rule.split(":", 1)[1]
        dated = [i for i in cluster if records[i].get(column) is not None]
        if dated:
            # Latest value wins; ties go to the earliest row for determinism.
            return max(dated, key=lambda i: (records[i][column], -i))
        return min(cluster)
    return max(cluster, key=lambda i: (_completeness(records[i], names), -i))


def merge_into(
    survivor: dict[str, Any], duplicate: dict[str, Any], target: TargetSpec
) -> tuple[str, ...]:
    """Fill the survivor's empty fields from a duplicate; return the fields filled.

    Money never changes group: with ``reconcile_by`` (e.g. currency), a
    ``reconcile_sum`` amount is copied only from a duplicate in the same group,
    and the group itself is copied only onto a survivor that has no amounts
    yet. Otherwise a merge could book EUR 50 as USD 50 and the reconciliation
    by currency would be meaningless.
    """
    group = target.reconcile_by
    money = {f.name for f in target.fields if f.reconcile_sum}
    names = target.field_names
    if group is not None:
        # Settle the group before deciding which amounts may follow it.
        names = [group, *(n for n in names if n != group)]
    filled: list[str] = []
    for name in names:
        if survivor.get(name) is not None or duplicate.get(name) is None:
            continue
        if group is not None:
            if name == group and any(survivor.get(m) is not None for m in money):
                continue
            if name in money and survivor.get(group) != duplicate.get(group):
                continue
        survivor[name] = duplicate[name]
        filled.append(name)
    return tuple(filled)


@dataclass
class DedupeResult:
    frame: pd.DataFrame
    merges: list[Merge] = field(default_factory=list)

    @property
    def remap(self) -> dict[Any, Any]:
        """Merged key -> surviving key, used to repoint child records."""
        return {m.merged_key: m.survivor_key for m in self.merges}


def deduplicate(target: TargetSpec, frame: pd.DataFrame, spec: DedupeSpec) -> DedupeResult:
    """Collapse fuzzy duplicates in ``frame`` according to ``spec``."""
    if frame.empty:
        return DedupeResult(frame=frame)
    records = frame.to_dict(orient="records")
    names = target.field_names
    uf = _UnionFind(len(records))
    scores: dict[tuple[int, int], float] = {}
    for a, b in sorted(candidate_pairs(records, spec.block_on)):
        score = record_score(records[a], records[b], spec.fields)
        if score >= spec.threshold:
            uf.union(a, b)
            scores[(a, b)] = score

    clusters: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(records)):
        clusters[uf.find(idx)].append(idx)

    keep: list[dict[str, Any]] = []
    merges: list[Merge] = []
    for members in sorted(clusters.values(), key=min):
        survivor_idx = _pick_survivor(members, records, spec.survivor, names)
        survivor = dict(records[survivor_idx])
        losers = sorted(i for i in members if i != survivor_idx)
        for loser in losers:
            filled = merge_into(survivor, records[loser], target) if spec.merge_fields else ()
            pair = (min(loser, survivor_idx), max(loser, survivor_idx))
            score = scores.get(pair) or record_score(records[loser], survivor, spec.fields)
            merges.append(
                Merge(
                    table=target.table,
                    survivor_key=survivor[target.key],
                    merged_key=records[loser][target.key],
                    score=round(score, 1),
                    source_row=int(records[loser][SOURCE_ROW]),
                    filled=filled,
                )
            )
        keep.append(survivor)

    keep.sort(key=lambda r: r[SOURCE_ROW])
    result = pd.DataFrame(keep, columns=frame.columns).astype(object)
    result = result.where(result.notna(), None)
    return DedupeResult(frame=result, merges=merges)
