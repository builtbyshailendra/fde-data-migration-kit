# ADR 0002: Read every source column as text and cast in one place

- Status: accepted
- Date: 2026-11-08

## Context

pandas and openpyxl infer types while reading. On legacy exports that inference
is wrong often enough to matter, and wrong silently:

- IDs and postcodes lose leading zeros (`"007"` becomes `7`).
- `"N/A"`, `"NA"` and `"null"` become NaN before anyone can count them, and a
  column with one of them turns from integers into floats.
- A column of amounts with one `"TBD"` stays `object`, while a clean one becomes
  `float64`, so `1204.5` and `"$1,204.50"` take different code paths.
- Excel cells hold real dates in some rows and typed text in others.

A migration has to report every value it could not use, with the original text,
or the customer cannot fix it.

## Decision

`sources.read_table` reads every CSV and Excel column with `dtype=str` and
`keep_default_na=False`: the frame holds exactly the text the customer exported,
and empty cells are empty strings. Null placeholders are a mapping setting
(`null_values`), applied explicitly by the transformer.

All type conversion lives in `casting.py`. Each parser either returns a typed
value (`int`, `Decimal`, `date`, `datetime`, `bool`) or raises `CastError` with a
reason that quotes the original text. The transformer turns those errors into
reject reasons, and the profiler uses the same parsers to infer column types, so
"the profiler says decimal" means "the mapping can cast it as decimal".

Money is `Decimal`, quantised to the field's `scale` with half-up rounding,
never float.

## Consequences

- Every value that cannot be cast appears in the rejects file as
  `amount: 'see email' is not a number`, with its source row.
- Behaviour does not depend on which reader or pandas version guessed what.
- Casting is per value in Python rather than vectorised. It is measurably slower
  than pandas' native parsing, but it is not the bottleneck for exports of the
  sizes this kit is meant for, and the error messages are the point.
- Sums reconcile to the cent, because no amount ever passes through a float.
