"""Parsers that turn messy legacy text into typed Python values.

Each parser raises CastError with a human-readable reason. The same parsers are
used by the profiler (to infer types) and by the transformer (to cast), so a
column the profiler calls "decimal" is one the mapping can actually cast.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from migkit.mapping import DEFAULT_DATE_FORMATS

TRUE_WORDS = frozenset({"true", "t", "yes", "y", "1"})
FALSE_WORDS = frozenset({"false", "f", "no", "n", "0"})
NBSP = chr(0xA0)  # thousands separator in French-locale exports
CURRENCY_SYMBOLS = "$€£₹¥"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
INTEGER_RE = re.compile(r"^[+-]?\d+$")
DECIMAL_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
PHONE_CHARS_RE = re.compile(r"^[+()\d\s.-]{7,}$")


class CastError(ValueError):
    """A value could not be converted to the requested type."""


def parse_integer(text: str) -> int:
    cleaned = text.strip().replace(",", "")
    if cleaned.endswith(".0"):  # Excel writes whole numbers as floats
        cleaned = cleaned[:-2]
    if not INTEGER_RE.match(cleaned):
        raise CastError(f"'{text}' is not an integer")
    return int(cleaned)


def parse_decimal(text: str, scale: int | None = 2) -> Decimal:
    """Parse amounts such as '$1,204.50', '(12.00)', '1.234,50' or '1 999,00 €'.

    When both separators appear, the last one is the decimal separator. A lone
    comma followed by exactly two digits at the end is a decimal comma
    (European exports); every other comma is a thousands separator.
    """
    cleaned = text.strip()
    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative, cleaned = True, cleaned[1:-1]
    cleaned = cleaned.strip(CURRENCY_SYMBOLS + " ").replace(" ", "").replace(NBSP, "")
    if cleaned.endswith("-"):
        negative, cleaned = True, cleaned[:-1]
    if "," in cleaned and "." in cleaned and cleaned.rfind(",") > cleaned.rfind("."):
        # '1.234,50': dots group thousands, the comma is the decimal separator.
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif re.search(r",\d{2}$", cleaned) and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    cleaned = cleaned.strip(CURRENCY_SYMBOLS)
    if not DECIMAL_RE.match(cleaned):
        raise CastError(f"'{text}' is not a number")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards this
        raise CastError(f"'{text}' is not a number") from exc
    if negative:
        value = -value
    if scale is not None:
        value = value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
    return value


def parse_boolean(text: str) -> bool:
    word = text.strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise CastError(f"'{text}' is not a boolean")


def _strptime_any(text: str, formats: Iterable[str]) -> tuple[datetime, str]:
    cleaned = " ".join(text.strip().split())
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt), fmt
        except ValueError:
            continue
    raise CastError(f"'{text}' does not match any date format")


def parse_date(text: str, formats: Iterable[str] = DEFAULT_DATE_FORMATS) -> date:
    return _strptime_any(text, formats)[0].date()


def parse_datetime(text: str, formats: Iterable[str] = DEFAULT_DATE_FORMATS) -> datetime:
    return _strptime_any(text, formats)[0]


def date_format_of(text: str, formats: Iterable[str]) -> str | None:
    """Return the first format that parses ``text``, or None."""
    try:
        return _strptime_any(text, formats)[1]
    except CastError:
        return None


def is_email(text: str) -> bool:
    return bool(EMAIL_RE.match(text.strip()))


def is_phone(text: str) -> bool:
    stripped = text.strip()
    digits = sum(ch.isdigit() for ch in stripped)
    return bool(PHONE_CHARS_RE.match(stripped)) and 7 <= digits <= 15 and not stripped.isdigit()


def to_e164(text: str, default_country_code: str = "1") -> str:
    """Normalise a phone number to E.164, assuming a national number when no '+' is given."""
    stripped = text.strip()
    stripped = re.split(r"\s*(?:x|ext\.?)\s*\d+$", stripped, flags=re.IGNORECASE)[0]
    digits = re.sub(r"\D", "", stripped)
    if stripped.startswith("+"):
        result = digits
    elif stripped.startswith("00"):
        result = digits[2:]
    elif len(digits) == 11 and digits.startswith(default_country_code):
        result = digits
    else:
        result = default_country_code + digits.lstrip("0")
    if not 8 <= len(result) <= 15:
        raise CastError(f"'{text}' is not a valid phone number")
    return "+" + result


def cast_value(
    text: str,
    field_type: str,
    *,
    formats: Iterable[str] = DEFAULT_DATE_FORMATS,
    scale: int = 2,
) -> object:
    """Cast one non-null text value to a field type."""
    if field_type == "string":
        return text
    if field_type == "integer":
        return parse_integer(text)
    if field_type == "decimal":
        return parse_decimal(text, scale)
    if field_type == "boolean":
        return parse_boolean(text)
    if field_type == "date":
        return parse_date(text, formats)
    if field_type == "datetime":
        return parse_datetime(text, formats)
    raise CastError(f"unknown type '{field_type}'")
