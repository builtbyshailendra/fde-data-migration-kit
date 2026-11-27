# Mapping reference

A mapping is one YAML file. It is validated in full before any data is read:
unknown keys, missing references and inconsistent options fail with the exact
location, for example `targets.0.fields.3: Value error, field 'phone' needs
exactly one of 'from', 'split', 'join' or 'constant'`. Paths in the file are
relative to the file itself.

The [sample mapping](../examples/legacy_crm/mapping.yaml) uses every feature
below.

## Top level

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | int | `1` | Mapping format version |
| `name` | string | `migration` | Shown in reports and stored in `migkit_runs` |
| `null_values` | list of strings | `"", NULL, null, N/A, n/a, NA, na, -, --, none, None, ?` | Cell values (after trimming) that mean "no value" |
| `date_formats` | list of strptime formats | ISO, `%Y/%m/%d`, `%d-%b-%Y`, `%d %b %Y`, `%b %d %Y`, `%b %d, %Y` | Tried in order for `date` and `datetime` fields |
| `sources` | map | required | Named exports, see below |
| `lookups` | map | `{}` | Named reference tables, see below |
| `targets` | list | required | Target tables, parents before children |

## `sources.<name>`

| Key | Default | Meaning |
|---|---|---|
| `path` | required | `.csv`, `.xlsx` or `.xlsm` file |
| `sheet` | first sheet | Excel sheet name |
| `delimiter` | sniffed from `, ; tab \|` | CSV delimiter |
| `encoding` | detected: `utf-8-sig`, then `cp1252`, then `latin-1` | CSV encoding |

## `lookups.<name>`

Exactly one of:

- `path` (+ `key`, `value` column names, default `key` / `value`): a CSV or
  Excel table;
- `values`: an inline map.

## `targets[]`

| Key | Meaning |
|---|---|
| `table` | Target table name |
| `source` | Name of a source |
| `key` | Field that identifies a row; primary key, upsert conflict target, unique and required |
| `fields` | List of fields, see below |
| `row_checks` | Cross-field rules: `{left, op, right, message}` with `op` one of `>= > <= < == !=`; skipped when either side is null |
| `dedupe` | Fuzzy duplicate detection, see below |
| `reconcile_by` | Field to break reconciliation sums down by, e.g. `currency` |

## `fields[]`

Each field takes its value from exactly one origin:

| Origin | Example |
|---|---|
| `from: <column>` | `from: Account Name` |
| `split: {column, sep=" ", index=0, maxsplit=-1, from_right=false}` | last name: `split: {column: Full Name, maxsplit: 1, from_right: true, index: 1}` |
| `join: {columns: [...], sep=" "}` | `join: {columns: [Street, City], sep: ", "}`; empty parts are skipped |
| `constant: <value>` | `constant: legacy-crm` |

Then, in this order:

1. surrounding whitespace is trimmed and `null_values` become null;
2. `transforms` run on non-null strings;
3. `value_map` replaces values (keys compared case-insensitively, after trimming);
4. `lookup: {table, case_insensitive=true, on_missing=reject|null|keep}` translates
   through a lookup table;
5. `default` fills a value that is still null;
6. the value is cast to `type`.

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Target column |
| `type` | `string` | `string`, `integer`, `decimal`, `boolean`, `date`, `datetime` |
| `formats` | top-level `date_formats` | Date formats for this field only |
| `scale` | `2` | Decimal places for `decimal` (rounded half-up) |
| `transforms` | `[]` | See below |
| `value_map` | none | Map of source value to target value |
| `lookup` | none | Lookup reference |
| `default` | none | Value when null |
| `references` | none | `table.key` of an earlier target; values are repointed to dedupe survivors and orphans are rejected |
| `required` | `false` | Null is a validation failure |
| `unique` | `false` | Later duplicates are rejected; the first is kept |
| `pattern` | none | Regular expression the whole value must match |
| `allowed` | none | List of permitted values |
| `min`, `max` | none | Numeric bounds |
| `max_length` | none | Maximum string length; also sizes the `VARCHAR` |
| `reconcile_sum` | `false` | Include this `integer`/`decimal` field in sum reconciliation |

### Transforms

Written as a plain name or a one-key map with an argument.

| Transform | Effect |
|---|---|
| `strip`, `lower`, `upper`, `title` | String methods |
| `collapse_whitespace` | Runs of whitespace become one space |
| `digits_only`, `alnum_only` | Remove everything but digits / letters and digits |
| `{zfill: 5}` | Left-pad with zeros |
| `{replace: [old, new]}` | Literal replacement |
| `{regex_replace: [pattern, replacement]}` | Regular expression replacement |
| `{phone_e164: "44"}` | Normalise a phone number to E.164, using the given country code when the number has no `+` or `00` prefix; extensions (`x101`, `ext. 4`) are dropped |

### Casting rules

- `integer`: `1,204`, `007`, `12.0` (Excel) are accepted.
- `decimal`: currency symbols `$ € £ ₹ ¥`, thousands separators, European
  `1.234,50`, Indian `12,40,000`, accounting negatives `(1,500.00)` and
  trailing minus `250-`.
- `boolean`: `true/false`, `t/f`, `yes/no`, `y/n`, `1/0`, any case.

## `dedupe`

| Key | Default | Meaning |
|---|---|---|
| `block_on` | required | Fields; only records sharing a (normalised) value in at least one of them are compared |
| `fields` | required | `{name, weight=1, method=token_sort, strip_suffixes=false}`; methods `exact`, `ratio`, `token_sort`, `token_set` |
| `threshold` | `90` | Weighted score (0-100) at or above which two records are duplicates |
| `survivor` | `most_complete` | `most_complete`, `first` or `latest:<field>` |
| `merge_fields` | `true` | Fill the survivor's empty fields from its duplicates; with `reconcile_by`, amounts are copied only within the same group |

Values are compared lower-cased with punctuation removed; `strip_suffixes`
also drops company suffixes such as Inc, LLC, Ltd, Corp, GmbH. Fields missing on
either side are left out of the weighted score. Duplicates are clustered
transitively.
