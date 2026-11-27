from pathlib import Path

import pandas as pd
import pytest

from conftest import ROOT
from migkit.profiling import (
    infer_type,
    profile_column,
    profile_frame,
    render_markdown,
    render_text,
    shape_of,
)
from migkit.sources import SOURCE_ROW, SourceError, detect_encoding, read_table

SAMPLE = ROOT / "examples" / "legacy_crm"


def test_reads_cp1252_semicolon_export_as_text() -> None:
    frame = read_table(SAMPLE / "contacts.csv")
    assert detect_encoding(SAMPLE / "contacts.csv") == "cp1252"
    assert "Full Name" in frame.columns
    assert "José Müller" in set(frame["Full Name"])
    # IDs stay text and empty cells stay empty strings, never NaN.
    assert frame.loc[0, "Contact ID"] == "1001"
    assert frame.loc[9, "Opt In"] == ""
    assert list(frame[SOURCE_ROW][:2]) == [2, 3]


def test_reads_named_excel_sheet() -> None:
    frame = read_table(SAMPLE / "invoices.xlsx", sheet="Invoices")
    assert frame.columns[0] == "Invoice #"
    assert len(frame) == 36


def test_read_errors(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="not found"):
        read_table(tmp_path / "missing.csv")
    dupes = tmp_path / "dupes.csv"
    dupes.write_text("id, id \n1,2\n", encoding="utf-8")
    with pytest.raises(SourceError, match="duplicate column headers"):
        read_table(dupes)


@pytest.mark.parametrize(
    ("value", "shape"),
    [
        ("(415) 555-0132", "(999) 999-9999"),
        ("ada@example.com", "a+@a+.a+"),
        ("ACC-0001", "A+-9999"),
        ("Ab", "Aa"),
    ],
)
def test_shape_of(value: str, shape: str) -> None:
    assert shape_of(value) == shape


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["1", "2", "3"], "integer"),
        (["$1.50", "2,000", "3"], "decimal"),
        (["2024-01-01", "03-Feb-2019"], "date"),
        (["12/01/2024 09:15", "13/01/2024 17:30"], "datetime"),
        (["a@b.com", "c@d.org"], "email"),
        (["Yes", "no", "Y"], "boolean"),
        (["hello", "world"], "string"),
        ([], "empty"),
    ],
)
def test_infer_type(values: list[str], expected: str) -> None:
    assert infer_type(values)[0] == expected


def test_infer_type_tolerates_a_few_bad_values() -> None:
    values = [str(i) for i in range(19)] + ["TBD"]
    inferred, confidence = infer_type(values)
    assert inferred == "integer"
    assert confidence == 0.95


def test_profile_column_counts_placeholders_and_warns() -> None:
    series = pd.Series(["ACC-1", "ACC-2", "N/A", " ACC-3", "ACC-3", "acc-4", "Acc-4", ""] * 2)
    profile = profile_column("Account", series)
    assert profile.nulls == 4
    assert profile.placeholder_nulls == 2
    assert profile.inferred_type == "string"
    assert any("whitespace" in w for w in profile.warnings)
    assert any("placeholder" in w for w in profile.warnings)
    assert any("capitalisation" in w for w in profile.warnings)
    assert not profile.candidate_key


def test_profile_frame_finds_keys_and_mixed_dates() -> None:
    frame = read_table(SAMPLE / "accounts.csv")
    profile = profile_frame(frame, "accounts.csv")
    by_name = {c.name: c for c in profile.columns}
    assert profile.rows == 30
    assert by_name["Annual Revenue"].inferred_type == "decimal"
    assert by_name["Created Date"].inferred_type == "date"
    assert len(by_name["Created Date"].date_formats) > 1
    assert any("duplicate value" in w for w in by_name["Account ID"].warnings)
    assert SOURCE_ROW not in by_name
    text = render_text(profile)
    assert "accounts.csv: 30 rows, 10 columns" in text
    assert "mixed date formats" in text
    markdown = render_markdown(profile)
    assert markdown.startswith("## accounts.csv")
    assert "| Annual Revenue | decimal |" in markdown
    assert profile.to_dict()["columns"][0]["name"] == "Account ID"
