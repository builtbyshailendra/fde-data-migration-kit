import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import mini_mapping_data
from migkit.mapping import MappingError, MappingSpec, load_mapping, parse_mapping


def _with(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    data = copy.deepcopy(mini_mapping_data())
    change(data)
    return data


def test_sample_mapping_loads(sample_spec: MappingSpec) -> None:
    assert [t.table for t in sample_spec.targets] == ["customers", "contacts", "invoices"]
    assert sample_spec.target("invoices").reconcile_by == "currency"
    assert len(sample_spec.checksum) == 64
    assert sample_spec.resolve("accounts.csv").name == "accounts.csv"
    assert sample_spec.resolve("accounts.csv").parent == sample_spec.base_dir


def test_field_uses_from_alias(mini_spec: MappingSpec) -> None:
    field = mini_spec.target("customers").field("id")
    assert field.source == "Cust No"
    assert field.source_columns == ["Cust No"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["targets"][0].update(source="nope"), "unknown source 'nope'"),
        (lambda d: d["targets"][0].update(key="missing"), "key 'missing' is not one of"),
        (
            lambda d: d["targets"][0]["fields"][1].update(constant="x"),
            "exactly one of 'from', 'split', 'join' or 'constant'",
        ),
        (
            lambda d: d["targets"][0]["fields"][2]["lookup"].update(table="states"),
            "unknown lookup 'states'",
        ),
        (
            lambda d: d["targets"].reverse(),
            "must be defined earlier in 'targets'",
        ),
        (
            lambda d: d["targets"][1]["fields"][1].update(references="customers.name"),
            "can only reference the key",
        ),
        (lambda d: d["targets"][1].update(reconcile_by="region"), "reconcile_by 'region'"),
        (
            lambda d: d["targets"][0]["fields"][1].update(reconcile_sum=True),
            "reconcile_sum needs an integer or decimal",
        ),
        (
            lambda d: d["targets"][0]["dedupe"].update(survivor="random"),
            "survivor must be",
        ),
        (
            lambda d: d["targets"][0]["dedupe"].update(block_on=["postcode"]),
            "dedupe refers to unknown fields ['postcode']",
        ),
        (lambda d: d["targets"][0].update(colour="blue"), "Extra inputs are not permitted"),
        (
            lambda d: d["targets"][0]["fields"].append({"name": "id", "from": "Cust No"}),
            "duplicate field names ['id']",
        ),
    ],
)
def test_invalid_mappings_fail_with_a_precise_message(
    change: Callable[[dict[str, Any]], None], message: str
) -> None:
    with pytest.raises(MappingError) as excinfo:
        parse_mapping(_with(change))
    assert message in str(excinfo.value)


def test_lookup_needs_exactly_one_source() -> None:
    data = _with(lambda d: d["lookups"]["countries"].update(path="countries.csv"))
    with pytest.raises(MappingError, match="exactly one of 'path' or 'values'"):
        parse_mapping(data)


def test_load_mapping_reports_bad_files(tmp_path: Path) -> None:
    with pytest.raises(MappingError, match="cannot read mapping"):
        load_mapping(tmp_path / "absent.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("targets: [unclosed", encoding="utf-8")
    with pytest.raises(MappingError, match="not valid YAML"):
        load_mapping(broken)
    listing = tmp_path / "list.yaml"
    listing.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(MappingError, match="mapping at the top level"):
        load_mapping(listing)


def test_checksum_changes_with_the_file(write_files: Callable[..., Path]) -> None:
    first = load_mapping(write_files()).checksum
    second = load_mapping(write_files(_with(lambda d: d.update(name="other")))).checksum
    assert first != second
