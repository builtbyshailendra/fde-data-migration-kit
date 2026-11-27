# ADR 0001: Diff, then upsert, in one transaction

- Status: accepted
- Date: 2026-11-08

## Context

Customer migrations are never run once. The first load happens, the customer
reviews the rejects, fixes twenty rows in their old system, sends a new
export, and the load runs again. Sometimes the cutover itself is a re-run of a
load that was rehearsed the week before. Each re-run must:

1. not create duplicates,
2. pick up corrections to rows that already loaded,
3. leave the target untouched when nothing changed,
4. never leave the target half-loaded if it fails, and
5. report honestly what it did ("inserted 3, updated 2"), because those numbers
   go into the customer's sign-off.

Options considered:

- **Truncate and reload.** Simple and idempotent, but it destroys rows the
  target application created or edited since the last load, breaks foreign keys
  from other tables, and cannot report what changed.
- **Plain upsert (`INSERT ... ON CONFLICT DO UPDATE`) of every row.**
  Idempotent in outcome, but every run rewrites every row (triggers, audit
  columns and replication all see a change), and the database cannot tell us
  whether a conflicting row actually differed, so the counts are meaningless.
- **Diff in the application, then upsert only what differs.**

## Decision

`load` runs inside a single database transaction. For each table, in mapping
order (parents before children), it reads the existing rows for the planned
keys in batches, classifies each planned row as insert, update or unchanged
with a type-tolerant comparison, and upserts only inserts and updates with
`INSERT ... ON CONFLICT (key) DO UPDATE`, in batches of `--batch-size` rows.
The upsert, not a plain insert or update, is still used for the writes so that a
row inserted concurrently between the read and the write cannot fail the load.
At the end it appends one row per table to `migkit_runs` with the counts and the
SHA-256 of the mapping file. Any error rolls back everything, including the run
log.

The dry-run `plan` command uses the same diff code without writing, so what
`plan` shows is exactly what `load` will do.

Rows present only in the target are counted and never touched.

## Consequences

- Re-running is cheap and safe; the second run of an unchanged load writes zero
  rows, which the test suite asserts.
- Insert/update counts are real and appear in the reconciliation report.
- The whole load must fit in one transaction. For the sizes this kit targets
  (up to low millions of rows) that is fine on SQLite and PostgreSQL; a much
  larger migration would need per-table or per-batch commits with resumable
  checkpoints.
- The extra key lookups cost one `SELECT ... WHERE key IN (...)` per 500 planned
  rows, which is small next to the writes they avoid.
- Upserts depend on `ON CONFLICT`, so only SQLite 3.24+ and PostgreSQL 9.5+ are
  supported. Adding another database means adding its upsert form.
- Deletions are out of scope by design; removing data the source no longer has
  must be an explicit, separately reviewed step.
