from datetime import date
from typing import Any

import pandas as pd
import pytest

from migkit.dedupe import candidate_pairs, deduplicate, field_score, normalise, record_score
from migkit.mapping import DedupeField, DedupeSpec, TargetSpec, parse_mapping
from migkit.sources import SOURCE_ROW


def contacts_target(**dedupe: Any) -> TargetSpec:
    config = {
        "block_on": ["email"],
        "threshold": 85,
        "fields": [
            {"name": "email", "weight": 2, "method": "exact"},
            {"name": "name", "method": "token_sort"},
        ],
        **dedupe,
    }
    spec = parse_mapping(
        {
            "sources": {"s": {"path": "s.csv"}},
            "targets": [
                {
                    "table": "contacts",
                    "source": "s",
                    "key": "id",
                    "fields": [
                        {"name": "id", "from": "id", "type": "integer"},
                        {"name": "email", "from": "e"},
                        {"name": "name", "from": "n"},
                        {"name": "phone", "from": "p"},
                        {"name": "updated", "from": "u", "type": "date"},
                    ],
                    "dedupe": config,
                }
            ],
        }
    )
    return spec.targets[0]


def contacts(*rows: tuple[Any, ...]) -> pd.DataFrame:
    columns = ["id", "email", "name", "phone", "updated"]
    records = [
        {SOURCE_ROW: i + 2, **dict(zip(columns, r, strict=True))} for i, r in enumerate(rows)
    ]
    return pd.DataFrame(records).astype(object).where(lambda f: f.notna(), None)


def run(target: TargetSpec, frame: pd.DataFrame):
    assert target.dedupe is not None
    return deduplicate(target, frame, target.dedupe)


def test_normalise_drops_punctuation_and_company_suffixes() -> None:
    assert normalise("CORVANE Corp.", strip_suffixes=True) == "corvane"
    assert normalise("The Company", strip_suffixes=True) == "the company"  # never empty
    assert normalise("  Jane   O'Neil ") == "jane o neil"


def test_scores_ignore_missing_fields() -> None:
    spec = DedupeField(name="name", method="token_sort")
    assert field_score("Jane Doe", "doe jane", spec) == 100.0
    assert field_score(None, "x", spec) is None
    fields = [spec, DedupeField(name="phone", method="exact")]
    assert record_score({"name": "Jane Doe", "phone": None}, {"name": "Jane Doe"}, fields) == 100


def test_blocking_only_pairs_records_sharing_a_value() -> None:
    records = [
        {"email": "a@x.com", "phone": "1"},
        {"email": "A@X.com ", "phone": "2"},
        {"email": "b@x.com", "phone": "1"},
        {"email": None, "phone": None},
    ]
    assert candidate_pairs(records, ["email"]) == {(0, 1)}
    assert candidate_pairs(records, ["email", "phone"]) == {(0, 1), (0, 2)}


def test_most_complete_record_survives_and_absorbs_missing_values() -> None:
    frame = contacts(
        (1, "jane@x.com", "Jane Doe", None, None),
        (2, "jane@x.com", "Doe, Jane", "+1555", None),
        (3, "bob@x.com", "Bob", None, None),
    )
    result = run(contacts_target(), frame)
    assert list(result.frame["id"]) == [2, 3]
    (merge,) = result.merges
    assert (merge.merged_key, merge.survivor_key, merge.source_row) == (1, 2, 2)
    assert result.remap == {1: 2}


def test_latest_survivor_rule() -> None:
    frame = contacts(
        (1, "jane@x.com", "Jane Doe", "+1555", date(2024, 5, 1)),
        (2, "jane@x.com", "Jane Doe", None, date(2023, 1, 1)),
    )
    result = run(contacts_target(survivor="latest:updated"), frame)
    assert list(result.frame["id"]) == [1]


def test_first_survivor_without_field_merging() -> None:
    frame = contacts(
        (1, "jane@x.com", "Jane Doe", None, None),
        (2, "jane@x.com", "Jane Doe", "+1555", None),
    )
    result = run(contacts_target(survivor="first", merge_fields=False), frame)
    assert list(result.frame["id"]) == [1]
    assert result.frame.loc[0, "phone"] is None


@pytest.mark.parametrize(("threshold", "clusters"), [(99, 3), (70, 2)])
def test_threshold_controls_merging(threshold: float, clusters: int) -> None:
    frame = contacts(
        (1, "j@x.com", "Jane Doe", None, None),
        (2, "j@x.com", "Janet Dough", None, None),
        (3, "k@x.com", "Kim", None, None),
    )
    target = contacts_target(threshold=threshold, fields=[{"name": "name", "method": "ratio"}])
    assert len(run(target, frame).frame) == clusters


def test_transitive_duplicates_form_one_cluster() -> None:
    frame = contacts(
        (1, "a@x.com", "Jane Doe", "p1", None),
        (2, "a@x.com", "Jane Doe", "p2", None),
        (3, "b@x.com", "Jane Doe", "p2", None),
    )
    target = contacts_target(block_on=["email", "phone"], fields=[{"name": "name"}])
    result = run(target, frame)
    assert len(result.frame) == 1
    assert len(result.merges) == 2


def test_dedupe_spec_defaults() -> None:
    spec = DedupeSpec(block_on=["email"], fields=[DedupeField(name="email")])
    assert spec.threshold == 90.0
    assert spec.survivor == "most_complete"
