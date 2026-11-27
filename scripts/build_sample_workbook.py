"""Regenerate examples/legacy_crm/invoices.xlsx.

The workbook imitates a finance team's invoice export: real Excel dates next to
dates typed as text, amounts stored as numbers next to amounts typed with
currency symbols, credit notes in brackets, a cover sheet before the data and a
handful of rows that should not survive the migration. The output is
deterministic so the committed file only changes when this script does.

Usage: python scripts/build_sample_workbook.py [output.xlsx]
"""

from __future__ import annotations

import io
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

HEADER = [
    "Invoice #",
    "Customer",
    "Invoice Date",
    "Due Date",
    "Amount",
    "Currency",
    "Status",
    "Notes",
]

D = datetime  # Excel stores these as real date cells

# fmt: off
ROWS: list[list[object]] = [
    ["INV-24001", "ACC-0001", D(2024, 1, 15), D(2024, 2, 14), 12500.00, "USD", "Paid", ""],
    ["INV-24002", "ACC-0001", D(2024, 2, 15), D(2024, 3, 16), "$8,750.50", "USD", "PAID", ""],
    ["INV-24003", "ACC-0019", D(2024, 3, 1), D(2024, 3, 31), 4200, "USD", "Open", "legacy dup acct"],
    ["INV-24004", "ACC-0002", "2024-01-20", "2024-02-19", "£2,300.00", "gbp", "Paid", ""],
    ["INV-24005", "ACC-0003", D(2024, 1, 31), D(2024, 3, 1), 980.25, "USD", "Outstanding", ""],
    ["INV-24006", "ACC-0004", "05 Feb 2024", "06 Mar 2024", "1.234,50", "EUR", "Paid", ""],
    ["INV-24007", "ACC-0005", D(2024, 2, 10), D(2024, 3, 11), 15600, "USD", "pd", ""],
    ["INV-24008", "ACC-0028", D(2024, 4, 2), D(2024, 5, 2), 3300, "USD", "Open", ""],
    ["INV-24009", "ACC-0007", D(2024, 2, 28), D(2024, 3, 29), 640.00, "USD", "Void", "duplicate bill"],
    ["INV-24010", "ACC-0008", D(2024, 3, 3), D(2024, 4, 2), 250000, "USD", "Paid", ""],
    ["INV-24011", "ACC-0009", D(2024, 3, 9), D(2024, 4, 8), "1,050.00", "USD", "Paid", ""],
    ["INV-24012", "ACC-0010", D(2024, 3, 12), D(2024, 4, 11), 7200, "USD", "Open", ""],
    ["INV-24013", "ACC-0011", D(2024, 3, 20), D(2024, 4, 19), 48000, "USD", "Paid", ""],
    ["INV-24014", "ACC-0012", D(2024, 4, 1), D(2024, 5, 1), "AUD 9,900", "AUD", "Open", ""],
    ["INV-24015", "ACC-0011", D(2024, 4, 5), D(2024, 5, 5), "(1,500.00)", "USD", "Paid", "credit note"],
    ["INV-24016", "ACC-0015", D(2024, 4, 15), D(2024, 5, 15), 3999.99, "USD", "Open", ""],
    ["INV-24017", "ACC-0016", D(2024, 4, 18), D(2024, 5, 18), 22000, "USD", "Cancelled", ""],
    ["INV-24018", "ACC-0017", D(2024, 4, 30), D(2024, 5, 30), 1250, "USD", "Paid", ""],
    ["INV-24019", "ACC-0018", D(2024, 5, 2), D(2024, 6, 1), "₹4,50,000.00", "INR", "Paid", ""],
    ["INV-24020", "ACC-0020", D(2024, 5, 6), D(2024, 6, 5), 845.6, "USD", "Paid", "paper order"],
    ["INV-24021", "ACC-0021", D(2024, 5, 9), D(2024, 6, 8), 125000, "GBP", "Open", ""],
    ["INV-24022", "ACC-0022", D(2024, 5, 14), D(2024, 6, 13), "£6,480.00", "GBP", "Paid", ""],
    ["INV-24023", "ACC-0023", D(2024, 5, 20), D(2024, 6, 19), 31000, "USD", "Paid", ""],
    ["INV-24024", "ACC-0024", D(2024, 5, 24), D(2024, 6, 23), 87500, "USD", "Open", ""],
    ["INV-24025", "ACC-0025", D(2024, 6, 1), D(2024, 7, 1), 2100, "USD", "Paid", "account rejected"],
    ["INV-24026", "ACC-0026", D(2024, 6, 3), D(2024, 7, 3), 455.75, "USD", "Paid", ""],
    ["INV-24027", "ACC-0029", D(2024, 6, 10), D(2024, 7, 10), "12,40,000", "INR", "Open", ""],
    ["INV-24028", "ACC-0030", D(2024, 6, 12), D(2024, 6, 1), 18000, "USD", "Paid", "due before issue"],
    ["INV-24029", "ACC-0001", "N/A", D(2024, 7, 20), 5000, "USD", "Open", "no invoice date"],
    ["INV-24030", "ACC-0002", D(2024, 6, 25), D(2024, 7, 25), "see email", "GBP", "Open", ""],
    ["INV-24031", "ACC-0003", D(2024, 7, 1), D(2024, 7, 31), 1960.5, "USD", "Paid", ""],
    ["INV-24032", "ACC-0005", D(2024, 7, 3), D(2024, 8, 2), 8800, "USD", "Paid", ""],
    ["INV-24007", "ACC-0005", D(2024, 2, 10), D(2024, 3, 11), 15600, "USD", "pd", "re-exported row"],
    ["INV-24033", "ACC-0008", D(2024, 7, 8), D(2024, 8, 7), 175000, "USD", "Refunded", ""],
    ["INV-24034", "ACC-0013", D(2024, 7, 9), D(2024, 8, 8), 900, "USD", "Paid", ""],
    ["INV-24035", "acc-0015", D(2024, 7, 11), D(2024, 8, 10), 2750, "usd", "paid", ""],
]
# fmt: on


def build(path: Path) -> None:
    workbook = Workbook()
    cover = workbook.active
    cover.title = "Read Me"
    cover["A1"] = "Invoice export from FinanceDesk 7.2"
    cover["A2"] = "Generated nightly. Do not edit the Invoices sheet by hand."

    sheet = workbook.create_sheet("Invoices")
    sheet.append(HEADER)
    for row in ROWS:
        sheet.append(row)
    for cell in sheet["C"][1:] + sheet["D"][1:]:
        if isinstance(cell.value, datetime):
            cell.number_format = "dd/mm/yyyy"

    fixed = datetime(2024, 7, 15, 9, 0, 0)
    workbook.properties.created = fixed
    workbook.properties.modified = fixed
    workbook.properties.creator = "FinanceDesk"
    workbook.properties.lastModifiedBy = "FinanceDesk"
    buffer = io.BytesIO()
    workbook.save(buffer)
    path.write_bytes(_normalise_zip(buffer.getvalue(), fixed))


def _normalise_zip(data: bytes, stamp: datetime) -> bytes:
    """Rewrite the .xlsx archive with fixed timestamps so rebuilds are byte-identical.

    openpyxl stamps both the zip entries and docProps/core.xml with the time of
    the save, whatever the workbook properties say.
    """
    modified = stamp.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as source,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in source.infolist():
            info = zipfile.ZipInfo(item.filename, date_time=stamp.timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            content = source.read(item.filename)
            if item.filename == "docProps/core.xml":
                content = re.sub(
                    rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)",
                    rb"\g<1>" + modified + rb"\g<2>",
                    content,
                )
            target.writestr(info, content)
    return out.getvalue()


def main(argv: list[str]) -> int:
    default = Path(__file__).resolve().parents[1] / "examples" / "legacy_crm" / "invoices.xlsx"
    target = Path(argv[1]) if len(argv) > 1 else default
    build(target)
    print(f"wrote {target} ({len(ROWS)} invoice rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
