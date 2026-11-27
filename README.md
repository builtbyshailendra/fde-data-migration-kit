# fde-data-migration-kit

> Personal learning project, built pairing with Claude Code. Every company, person and number here is made up.

[![ci](https://github.com/builtbyshailendra/fde-data-migration-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/builtbyshailendra/fde-data-migration-kit/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

The toolkit a forward-deployed engineer uses to onboard a customer's legacy data:
profile the exports, map them to the target schema in one YAML file, validate,
fuzzy-deduplicate customers and contacts, preview the change as a dry-run diff,
load idempotently into SQLite or PostgreSQL, and hand the customer a
reconciliation report that accounts for every row and every unit of money.

```
migkit profile | validate | plan | load | reconcile
```

**What has been run, and where.** The SQLite path was tested locally on
Python 3.12: the full test suite (181 passed, 1 skipped) and the demo below,
whose output is pasted from that run. The PostgreSQL path was not run on the
development machine; it is tested only in CI, where `tests/test_postgres.py`
and a CLI load and reconcile run against a `postgres:16` service container.
The Docker image is likewise built and smoke-tested only in CI. The kit has
not been used on a real customer's data.

## The problem

Every customer onboarding starts with the same three files: a CRM export saved
from Excel in cp1252 with semicolons, an accounts sheet where "N/A", "TBD" and
an empty cell all mean "unknown", and an invoice workbook whose dates are half
real Excel dates and half typed text. The usual answer is a one-off script per
customer, and the usual outcome is a load that silently drops rows, coerces
"007" to 7, creates "Corvane Corp." and "CORVANE Corporation" as two customers,
and cannot be re-run after the customer fixes their data without creating
duplicates.

This kit replaces the one-off script with a repeatable process:

- **Nothing is silently dropped.** Every source row ends up loaded, rejected
  with every reason in plain English, or merged into a named duplicate, and the
  report proves the three add up.
- **The mapping is the contract.** One YAML file, reviewed with the customer,
  describes renames, casts, value maps, splits and joins, lookups, validation
  rules and dedupe settings. It is checked before any data is read.
- **Loads are safe to repeat.** A load diffs against the target and upserts only
  new or changed rows, in batches, in one transaction. The second run writes
  nothing; a run after a fix writes only the fixed rows.
- **Money reconciles.** Totals are compared source vs. rejected vs. merged vs.
  what is actually in the target database, per currency if you ask. An amount
  a duplicate fills into its survivor counts as loaded, not merged away, and a
  merge never moves money from one currency to another.

## Architecture

```mermaid
flowchart LR
    subgraph Customer exports
        CSV[CSV files<br/>any encoding/delimiter]
        XLSX[Excel workbooks<br/>named sheets]
        LK[Lookup tables]
    end
    MAP[[mapping.yaml<br/>pydantic-validated]]

    CSV & XLSX --> SRC[sources<br/>read everything as text]
    SRC --> PROF[profiling<br/>types, nulls, patterns, keys]
    SRC --> TR[transform<br/>extract, clean, map, lookup, cast]
    LK --> TR
    MAP --> TR
    TR --> REF[reference<br/>repoint FKs to survivors,<br/>reject orphans]
    REF --> VAL[validate<br/>pandera or pydantic<br/>+ row checks]
    VAL --> DD[dedupe<br/>blocking + rapidfuzz<br/>+ union-find]
    DD --> PLAN[(MigrationPlan<br/>rows, rejects, merges)]

    PLAN --> DIFF[plan<br/>dry-run diff]
    PLAN --> LOAD[load<br/>diff + batched upsert<br/>one transaction]
    PLAN --> REC[reconcile<br/>counts, sums, provenance]
    DIFF & LOAD & REC <--> DB[(Target DB<br/>SQLite / PostgreSQL)]
    REC --> RPT[reconciliation.md / .json<br/>rejects.csv]
```

Every command builds the same `MigrationPlan` offline first (transform,
reference, validate, dedupe), so `validate`, `plan`, `load` and `reconcile`
always agree on what the migration contains. Only `plan`, `load` and
`reconcile` talk to the database, and only `load` writes to it. Details in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quickstart

Requires Python 3.12+. With [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/builtbyshailendra/fde-data-migration-kit.git
cd fde-data-migration-kit
uv venv --python 3.12
uv pip install -e ".[dev]"
source .venv/bin/activate          # Windows Git Bash: source .venv/Scripts/activate
```

or with plain pip: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`.
Add the `postgres` extra (`".[dev,postgres]"`) to load into PostgreSQL.

The repository ships a realistic legacy CRM in
[`examples/legacy_crm/`](examples/legacy_crm/): 30 accounts, 30 contacts
(cp1252, semicolon separated) and 36 invoices in an Excel workbook with a cover
sheet, mixed date formats, five currencies, typos, placeholders, duplicates,
orphans and a re-exported row. Run the whole migration against a throwaway
SQLite database:

```bash
M=examples/legacy_crm/mapping.yaml
migkit profile examples/legacy_crm/accounts.csv
migkit validate -m $M --rejects out/rejects.csv
migkit plan     -m $M --db sqlite:///target.db
migkit load     -m $M --db sqlite:///target.db
migkit reconcile -m $M --db sqlite:///target.db --report out/reconciliation.md
```

`make demo` runs the same sequence and also writes the profile, merges and a
JSON reconciliation to `out/`. The Makefile needs GNU make, which Git Bash on
Windows does not ship; every target is one or two plain commands you can run
directly.

## Usage

All output below is real output from the sample data.

### `profile`: understand the exports before writing a mapping

```text
$ migkit profile examples/legacy_crm/accounts.csv
examples/legacy_crm/accounts.csv: 30 rows, 10 columns
column                 type      conf  nulls distinct unique  top pattern
-------------------------------------------------------------------------
Account ID             string    1.00      0       29   0.97  A+-9999
Account Name           string    1.00      1       29   1.00  Aa+ Aa+
Industry               string    1.00      0       14   0.47  Aa+
Country                string    1.00      0       11   0.37  A+
Phone                  phone     1.00      0       30   1.00  (999) 999-9999  [key]
Website                string    1.00      1       29   1.00  a+.a+
Annual Revenue         decimal   0.97      1       28   0.97  9,999,999
Created Date           date      1.00      1       28   0.97  9999-99-99
Active                 boolean   1.00      0        8   0.27  A
Account Owner          string    1.00      2        4   0.14  A. Aa+

findings:
  Account ID: nearly unique: 1 duplicate value(s), check before using as a key
  Account Name: 1 value(s) have leading or trailing whitespace
  Annual Revenue: 1 placeholder null(s) such as 'N/A' or 'NULL'
  Annual Revenue: 1 value(s) do not parse as decimal
  Created Date: 1 placeholder null(s) such as 'N/A' or 'NULL'
  Created Date: mixed date formats: %Y-%m-%d (21), %d-%b-%Y (3), %Y/%m/%d (2), %b %d %Y (2), %d %b %Y (1)
  Active: 2 value(s) appear with different capitalisation
```

`--mapping m.yaml` profiles every source the mapping names (including Excel
sheets), and `--format markdown|json -o file` produces something to paste into
the discovery notes.

### `validate`: every rejected row, with every reason

```text
$ migkit validate -m examples/legacy_crm/mapping.yaml --rejects out/rejects.csv
table        source   load transform reference validate merged  balanced
------------------------------------------------------------------------
customers        30     23         2         0        3      2  yes
contacts         30     22         2         2        1      3  yes
invoices         36     27         2         3        4      0  yes

rejected rows (19):
  customers row 11 [ACC-0010] transform: annual_revenue: 'TBD' is not a number
  customers row 14 [ACC-0013] validate: annual_revenue: '-250000.00' is below the minimum 0
  customers row 15 [ACC-0014] validate: name: required value is missing
  customers row 26 [ACC-0025] transform: country_code: 'Brevonia' not found in lookup 'countries'
  customers row 28 [ACC-0003] validate: customer_id: duplicate value 'ACC-0003'
  contacts row 14 [1013] reference: customer_id: no customers row with key 'ACC-0999'
  contacts row 28 [1027] transform: updated_at: 'not recorded' does not match any date format
  invoices row 15 [INV-24014] transform: amount: 'AUD 9,900' is not a number
  invoices row 29 [INV-24028] validate: due_on: '2024-06-01' fails rule due date is before the invoice date (issued_on is '2024-06-12')
  invoices row 34 [INV-24007] validate: invoice_number: duplicate value 'INV-24007'
  ...

merged duplicates (5):
  customers row 20: ACC-0019 -> ACC-0001 (score 100.0)
  customers row 29: ACC-0028 -> ACC-0005 (score 100.0)
  contacts row 3: 1002 -> 1003 (score 100.0)
  contacts row 10: 1009 -> 1008 (score 93.2)
  contacts row 18: 1017 -> 1016 (score 100.0)
```

Row numbers are what the customer sees when they open the file: the worksheet
row in Excel (blank rows above the header included), and for CSV the line the
record starts on, so a note with embedded line breaks does not shift the rows
after it. Exact duplicate headers (`name,name`) are an error rather than being
renamed `name.1` behind your back.

Children of merged duplicates are repointed: invoice INV-24003, billed to the
duplicate account ACC-0019, loads against ACC-0001. `--strict` exits 1 when
anything is rejected, for use in a CI gate on the customer's export.

### `plan`: dry-run diff against the target

```text
$ migkit plan -m examples/legacy_crm/mapping.yaml --db sqlite:///target.db
...
dry run against the target (nothing written):
table             insert  update unchanged target-only
------------------------------------------------------
customers             23       0         0           0  (table will be created)
contacts              22       0         0           0  (table will be created)
invoices              27       0         0           0  (table will be created)
```

On a populated target, updates are listed field by field
(`~ customers C-2: revenue: '500.00' -> '750.00'`), and rows that exist only in
the target are counted but never touched. `--json plan.json` writes the same
information for review tooling.

### `load`: idempotent, batched, transactional

```text
$ migkit load -m examples/legacy_crm/mapping.yaml --db sqlite:///target.db
run 6e0ebe4c03de4ec0ba898b4653d61f61
table            inserted  updated unchanged rejected merged batches
--------------------------------------------------------------------
customers              23        0         0        5      2       1
contacts               22        0         0        5      3       1
invoices               27        0         0        9      0       1

72 row(s) written, 19 rejected

$ migkit load -m examples/legacy_crm/mapping.yaml --db sqlite:///target.db
...
customers               0        0        23        5      2       0
contacts                0        0        22        5      3       0
invoices                0        0        27        9      0       0

0 row(s) written, 19 rejected
```

### `reconcile`: the report the customer signs off

```text
$ migkit reconcile -m examples/legacy_crm/mapping.yaml --db sqlite:///target.db --report out/reconciliation.md
table        source loaded rejected merged in target missing differs  status
----------------------------------------------------------------------------
customers        30     23        5      2        23       0       0  OK
contacts         30     22        5      3        22       0       0  OK
invoices         36     27        9      0        27       0       0  OK

sum                                 source unparsed      rejected        merged          target  status
customers.annual_revenue    390,999,500.00        1  3,000,000.00 16,550,000.00  371,449,500.00  OK
invoices.amount [AUD]                 0.00        1          0.00          0.00            0.00  OK
invoices.amount [EUR]             1,234.50        0          0.00          0.00        1,234.50  OK
invoices.amount [GBP]           133,780.00        1          0.00          0.00      133,780.00  OK
invoices.amount [INR]         1,690,000.00        0          0.00          0.00    1,690,000.00  OK
invoices.amount [USD]           727,882.59        0    223,800.00          0.00      504,082.59  OK

last load 7111f656e6fe48719c3a8735ce3d4b06 at 2026-11-27T07:36:51 (same mapping)
result: RECONCILED
wrote out/reconciliation.md
```

Reconciliation reads the rows back from the target and compares them field by
field with the plan; it does not trust the loader's own counts. It exits 1 when
anything is missing, differs, does not sum, or when the last load recorded in
`migkit_runs` used a different mapping file (by SHA-256).

## Configuration

| Setting | Where | Default | Purpose |
|---|---|---|---|
| `--mapping`, `-m` | validate, plan, load, reconcile, profile | required (optional for profile) | Mapping YAML; paths inside it are relative to the file |
| `--db` | plan, load, reconcile | `$MIGKIT_DATABASE_URL` | SQLAlchemy URL: `sqlite:///target.db`, `postgresql+psycopg://user:pass@host/db` |
| `MIGKIT_DATABASE_URL` | environment | unset | Target URL when `--db` is not given (keeps passwords out of shell history) |
| `--engine` | validate, plan, load, reconcile | `pandera` | Validation engine: `pandera` (vectorised) or `pydantic` (row models) |
| `--batch-size` | load | `500` | Rows per upsert statement |
| `--no-create-tables` | load | off | Fail on a missing table instead of creating it from the mapping |
| `--rejects CSV` | validate, load, reconcile | off | Every rejected row with stage and reasons (UTF-8 with BOM for Excel) |
| `--merges CSV` | validate | off | Every merged duplicate with its survivor and score |
| `--strict` | validate | off | Exit 1 if any row is rejected |
| `--report MD`, `--json PATH` | reconcile (`--json` also on plan) | off | Customer-facing Markdown report, machine-readable JSON |
| `--format`, `--top`, `--sheet`, `-o` | profile | `text`, `5`, first sheet, stdout | Profile output |

The mapping file itself is documented in
[docs/MAPPING.md](docs/MAPPING.md); the annotated
[sample mapping](examples/legacy_crm/mapping.yaml) uses every feature.

Exit codes: `0` success, `1` the run worked but found what you asked it to fail
on (rejects with `--strict`, a reconciliation mismatch), `2` bad input (invalid
mapping, missing file or column, unreachable database).

## Docker

```bash
docker build -t fde-data-migration-kit .
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -e MIGKIT_DATABASE_URL \
  fde-data-migration-kit \
  reconcile -m examples/legacy_crm/mapping.yaml --report out/reconciliation.md
```

Multi-stage build on `python:3.12-slim`, runs as UID 10001, PostgreSQL driver
included. The `build` job in `.github/workflows/ci.yml` builds the image and
runs it against the sample mapping on every push; the image is built in CI
only, the development machine this was written on does not run Docker.

## Testing

```bash
pytest -q                                   # 181 passed, 1 skipped (PostgreSQL), ~10 s, no network
pytest -q --cov=migkit --cov-report=term    # 96% coverage (lines and branches)
ruff check . && ruff format --check .
```

The suite covers every parser and transform, both validation engines against
the same rules (parametrised so they cannot drift apart), blocking and survivor
rules in dedupe, upsert SQL for both dialects, idempotent re-loads, update
detection, transactional rollback (a SQLite trigger fails the load halfway),
reconciliation of tampered and deleted target rows, amounts that a merge fills
into a survivor, row numbering across multi-line CSV records and offset Excel
headers, the CLI exit codes, and the full sample migration end to end.
`tests/test_postgres.py` runs the sample round trip against a real PostgreSQL
when `MIGKIT_TEST_POSTGRES_URL` is set, and is skipped otherwise. Locally it was
skipped; the `postgres` job in CI sets the variable, runs it against a
`postgres:16` service container and fails if the test is skipped.

## Design decisions

- **Read everything as text, cast in one place.** Readers that infer types turn
  `007` into `7` and `N/A` into NaN before you can see it. Sources are read as
  strings and every cast happens in `casting.py`, where a failure becomes a
  reason on a reject. The profiler uses the same parsers, so a column it calls
  `decimal` is one the mapping can actually cast.
  ([ADR 0002](docs/adr/0002-read-sources-as-text.md))
- **Diff, then upsert, in one transaction.** Instead of trusting
  `ON CONFLICT DO UPDATE` alone, each load reads existing rows by key, sends
  only inserts and real changes, and logs the counts plus the mapping SHA-256 to
  `migkit_runs`. Re-runs are cheap and the counts mean something.
  ([ADR 0001](docs/adr/0001-diff-then-upsert-in-one-transaction.md))
- **Reject the row, keep going, report every reason.** A row with three
  problems produces one reject with three reasons, so the customer fixes it
  once. Uniqueness keeps the first occurrence and rejects the rest.
- **Blocking before fuzzy matching.** Candidate pairs share at least one
  `block_on` value (multi-pass, so a typo in one field does not hide a
  duplicate found by another); pairs above the threshold are clustered with
  union-find, so A~B and B~C merge into one record.
- **Two validation engines, one rule set.** pandera validates whole frames
  quickly; pydantic gives row models that match a target app that already uses
  pydantic. Both are generated from the same mapping and tested against the
  same cases.
- **Never delete.** Rows that exist only in the target are counted and left
  alone. Deleting customer data should be a deliberate, separate step.

## Limitations

- The whole source is held in memory (pandas). Comfortable into the low
  millions of rows on a laptop; beyond that, split the export or stream it.
- Deduplication compares records within one export run. It does not match new
  records against rows already in the target from an earlier migration.
- Upserts use `INSERT ... ON CONFLICT`, so SQLite 3.24+ and PostgreSQL 9.5+
  only. MySQL and SQL Server are not supported.
- Tables are created from the mapping when missing, but existing tables are not
  altered. Schema migrations of the target belong to the target application.
- Sums are per field and optionally per one grouping field; there is no
  currency conversion, by design.
- Only CSV and Excel (`.xlsx`, `.xlsm`) sources. No direct reads from a legacy
  database, JSON or fixed-width files. The header must be the first non-blank
  row; a sheet with a title row above the header needs that row removed.
- The Docker image and the GitHub Actions workflow run only on GitHub; they are
  not exercised by the local test suite.

## Roadmap

- Match incoming records against existing target rows during dedupe.
- Chunked reading and loading for exports that do not fit in memory.
- `migkit init` to draft a mapping from a profile.
- Fixed-width and JSON Lines sources; reading directly from a source database.
- Soft deletes for rows that disappeared from the source, behind an explicit flag.
- HTML version of the reconciliation report with charts.

## Documentation

- [Customer migration runbook](docs/RUNBOOK.md): the process to agree with the customer
- [Mapping reference](docs/MAPPING.md)
- [Architecture](docs/ARCHITECTURE.md) and [decision records](docs/adr/)
- [Changelog](CHANGELOG.md)

## License

MIT, see [LICENSE](LICENSE).
