"""The reader keeps headers honest and row numbers matching what the customer sees."""

from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from migkit.sources import SOURCE_ROW, SourceError, read_table


def test_exact_duplicate_csv_headers_are_rejected(tmp_path: Path) -> None:
    # pandas alone would rename the second header to 'name.1' and carry on.
    path = tmp_path / "dupes.csv"
    path.write_text("id,name,name\n1,Ada,Lovelace\n", encoding="utf-8")
    with pytest.raises(SourceError, match=r"duplicate column headers \['name'\]"):
        read_table(path)


def test_exact_duplicate_excel_headers_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dupes.xlsx"
    workbook = Workbook()
    workbook.active.append(["id", "name", "name"])
    workbook.active.append([1, "Ada", "Lovelace"])
    workbook.save(path)
    with pytest.raises(SourceError, match="duplicate column headers"):
        read_table(path)


def test_a_file_without_a_header_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(SourceError, match="no header row"):
        read_table(path)


def test_csv_rows_are_numbered_by_the_line_they_start_on(tmp_path: Path) -> None:
    path = tmp_path / "notes.csv"
    path.write_bytes(
        b'id,note\r\n1,"first line\r\nsecond line"\r\n\r\n2,plain\r\n3,"a\nb\nc"\r\n4,last\r\n'
    )
    frame = read_table(path)
    assert list(frame["id"]) == ["1", "2", "3", "4"]
    # Record 1 spans lines 2-3, line 4 is blank, record 3 spans lines 6-8.
    assert list(frame[SOURCE_ROW]) == [2, 5, 6, 9]
    assert frame.loc[0, "note"] == "first line\r\nsecond line"


def test_blank_lines_before_the_header_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "late.csv"
    path.write_text("\n\nid,name\n1,Ada\n", encoding="utf-8")
    frame = read_table(path)
    assert list(frame.columns) == ["id", "name", SOURCE_ROW]
    assert list(frame[SOURCE_ROW]) == [4]


def test_short_rows_are_padded_and_long_rows_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ragged.csv"
    path.write_text("a,b,c\n1,2\n4,5,6,\n", encoding="utf-8")
    frame = read_table(path)
    assert frame.loc[0, "c"] == ""
    assert frame.loc[1, "c"] == "6"  # a trailing empty field is tolerated
    path.write_text("a,b\n1,2\n3,4,5\n", encoding="utf-8")
    with pytest.raises(SourceError, match="row 3 has 3 fields, the header has 2"):
        read_table(path)


def test_blank_header_cells_get_pandas_style_names(tmp_path: Path) -> None:
    path = tmp_path / "blank.csv"
    path.write_text("id,,name\n1,x,Ada\n", encoding="utf-8")
    assert list(read_table(path).columns) == ["id", "Unnamed: 1", "name", SOURCE_ROW]


def test_excel_rows_are_numbered_as_the_sheet_shows_them(tmp_path: Path) -> None:
    path = tmp_path / "offset.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    # Rows 1-2 are blank, the header is on row 3 and row 5 is blank.
    sheet["A3"], sheet["B3"], sheet["C3"] = "id", "amount", "issued"
    sheet["A4"], sheet["B4"], sheet["C4"] = "007", 12.5, datetime(2024, 1, 5)
    sheet["A6"], sheet["B6"] = 2, 100.0
    workbook.save(path)
    frame = read_table(path, sheet="Data")
    assert list(frame.columns) == ["id", "amount", "issued", SOURCE_ROW]
    assert list(frame[SOURCE_ROW]) == [4, 6]
    assert frame.loc[0].tolist()[:3] == ["007", "12.5", "2024-01-05 00:00:00"]
    assert (frame.loc[1, "amount"], frame.loc[1, "issued"]) == ("100", "")


def test_an_unterminated_quote_is_a_source_error(tmp_path: Path) -> None:
    # A lenient reader would swallow every following line into one field.
    path = tmp_path / "broken.csv"
    path.write_text('id,name\n1,"Ada\n2,Grace\n3,Edsger\n', encoding="utf-8")
    with pytest.raises(SourceError, match="cannot read"):
        read_table(path)


def test_long_free_text_fields_are_read(tmp_path: Path) -> None:
    path = tmp_path / "notes.csv"
    note = "x" * 300_000
    path.write_text(f'id,note\n1,"{note}"\n', encoding="utf-8")
    assert read_table(path).loc[0, "note"] == note
