from datetime import date, datetime
from decimal import Decimal

import pytest

from migkit.casting import (
    CastError,
    cast_value,
    is_email,
    is_phone,
    parse_boolean,
    parse_date,
    parse_decimal,
    parse_integer,
    to_e164,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$12,450,000.00", Decimal("12450000.00")),
        ("£3,200,000", Decimal("3200000.00")),
        ("1.234.500,00 €", Decimal("1234500.00")),
        ("1.234,50", Decimal("1234.50")),
        ("(1,500.00)", Decimal("-1500.00")),
        ("250-", Decimal("-250.00")),
        ("₹8,40,00,000", Decimal("84000000.00")),
        ("1\u00a0999,00", Decimal("1999.00")),
        ("845.6", Decimal("845.60")),
        ("0.005", Decimal("0.01")),
        (".5", Decimal("0.50")),
    ],
)
def test_parse_decimal_handles_legacy_formats(text: str, expected: Decimal) -> None:
    assert parse_decimal(text) == expected


@pytest.mark.parametrize("text", ["TBD", "see email", "AUD 9,900", "1.2.3", ""])
def test_parse_decimal_rejects_non_numbers(text: str) -> None:
    with pytest.raises(CastError, match="is not a number"):
        parse_decimal(text)


def test_parse_decimal_without_scale_keeps_precision() -> None:
    assert parse_decimal("3.14159", scale=None) == Decimal("3.14159")


@pytest.mark.parametrize(
    ("text", "expected"), [("007", 7), ("1,204", 1204), ("12.0", 12), (" -3 ", -3)]
)
def test_parse_integer(text: str, expected: int) -> None:
    assert parse_integer(text) == expected


@pytest.mark.parametrize("text", ["12.5", "abc", "1e3"])
def test_parse_integer_rejects(text: str) -> None:
    with pytest.raises(CastError):
        parse_integer(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Yes", True), ("y", True), ("TRUE", True), ("1", True), ("no", False), ("F", False)],
)
def test_parse_boolean(text: str, expected: bool) -> None:
    assert parse_boolean(text) is expected


def test_parse_boolean_rejects_unknown_words() -> None:
    with pytest.raises(CastError, match="not a boolean"):
        parse_boolean("maybe")


@pytest.mark.parametrize(
    "text", ["2019-02-03", "03-Feb-2019", "03 Feb 2019", "Feb 03 2019", "2019/02/03"]
)
def test_parse_date_accepts_every_default_format(text: str) -> None:
    assert parse_date(text) == date(2019, 2, 3)


def test_parse_date_with_custom_formats_and_errors() -> None:
    assert parse_date("03/02/2019", ["%d/%m/%Y"]) == date(2019, 2, 3)
    with pytest.raises(CastError, match="does not match any date format"):
        parse_date("not recorded")


def test_cast_value_dispatches_on_type() -> None:
    assert cast_value("2024-05-01 10:30:00", "datetime") == datetime(2024, 5, 1, 10, 30)
    assert cast_value("42", "integer") == 42
    assert cast_value("x", "string") == "x"
    with pytest.raises(CastError, match="unknown type"):
        cast_value("x", "uuid")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(415) 555-0132", "+14155550132"),
        ("415.555.0199", "+14155550199"),
        ("(415) 555-0132 x101", "+14155550132"),
        ("+44 20 7946 0958", "+442079460958"),
        ("0044 20 7946 0958", "+442079460958"),
        ("1-212-555-0188", "+12125550188"),
    ],
)
def test_to_e164(text: str, expected: str) -> None:
    assert to_e164(text) == expected


def test_to_e164_uses_default_country_and_rejects_short_numbers() -> None:
    assert to_e164("020 7946 0958", "44") == "+442079460958"
    with pytest.raises(CastError, match="not a valid phone number"):
        to_e164("12")


def test_email_and_phone_detection() -> None:
    assert is_email("jane.doe@corvane-corp.com")
    assert not is_email("neil.ashdown[at]velquist.com")
    assert is_phone("(212) 555-0147")
    assert not is_phone("2125550147")  # bare digits look like an ID, not a phone
