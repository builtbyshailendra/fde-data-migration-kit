# Data migration runbook

This runbook is written for you, the customer. It describes how your data moves
from your current system into the new platform, what we need from you at each
step, what you will receive back, and how you decide the migration is complete.
Your forward-deployed engineer (FDE) runs the tooling; you own the data and the
sign-off.

## At a glance

| Phase | What happens | Your part | You receive | Typical time |
|---|---|---|---|---|
| 1. Discovery | We profile your exports | Provide exports and a contact who knows the data | Data profile | 1-2 days |
| 2. Mapping | We agree how each field moves | Review and approve the mapping | Mapping document (YAML + walkthrough) | 2-3 days |
| 3. Trial runs | We validate and rehearse the load in a test environment | Fix rejected records at the source | Rejects file, duplicates file, reconciliation report per run | 1-2 weeks |
| 4. Cutover | Final export, load into production | Freeze edits, provide final export, be available | Load summary | 1 day |
| 5. Sign-off | We reconcile production against your export | Check the report, sign off | Final reconciliation report | 1-2 days |

## Roles

- **Data owner** (you): decides what is correct, approves the mapping and the
  final reconciliation.
- **Source system contact** (you): produces exports and fixes records in the
  current system.
- **FDE** (us): profiles, writes the mapping, runs every step, explains every
  rejected record.
- **Platform admin** (us or you): provides access to the target database for
  the trial and production loads.

## Phase 1: Discovery

**We need from you:** a full export of each object you want migrated
(for example accounts, contacts, invoices) in CSV or Excel, exactly as your
system produces it. Please do not clean it up first: we need to see the data as
it really is, and the final export at cutover will not be cleaned either.

**We do:** run `migkit profile` on each file. The profile shows, per column, the
real data type, how many values are missing (including placeholders such as
"N/A", "TBD" or "-"), how many values are unique, the common formats (for
example `(999) 999-9999` for phone numbers) and anything unusual, such as dates
in several formats or IDs that are almost but not quite unique.

**You receive:** the profile as a Markdown document, with questions where the data is
ambiguous, for example "Created Date uses three formats; is `03/04/2021` the 3rd
of April or the 4th of March?".

## Phase 2: Mapping

**We do:** write a mapping that describes, field by field, how your data
becomes data on the new platform: renamed columns, conversions (text to dates,
amounts with currency symbols to numbers), code translations (for example
"Tech" and "IT" both become `technology`), country names to ISO codes, full
names split into first and last name, and the rules every record must meet.
We also agree how duplicates are detected, for example "same email address and
a similar name" for contacts.

**We need from you:** a review of the mapping with us, and answers to:

- Which records are duplicates, and when they are, which copy should be kept
  (the most complete, the oldest, the most recently updated)?
- Which fields are mandatory on the new platform, and what should happen if
  they are empty: reject the record, or use a default?
- Which totals does your finance team check (for example invoice amounts by
  currency)? These become the sums in the reconciliation report.

**Sign-off:** you approve the mapping. Every later report records the
fingerprint (SHA-256) of the approved mapping file, so you can always tell which
version produced which data.

## Phase 3: Trial runs

Each trial run uses a fresh export and a test environment. Nothing in your
current system or in production changes.

1. **Validate.** `migkit validate` applies the mapping and lists every record
   that cannot be migrated as-is, with all of its problems at once, for example:

   ```
   customers row 26 [ACC-0025] transform: country_code: 'Brevonia' not found in lookup 'countries'
   invoices row 29 [INV-24028] validate: due_on: '2024-06-01' fails rule due date is before the invoice date
   ```

   "Row 26" is the row number you see when you open the file in Excel. For a
   CSV file it is the line the record starts on in a text editor, which is the
   same number unless a field (typically a note) contains line breaks.
2. **Dry run.** `migkit plan` shows how many records would be inserted or
   updated in the target, without writing anything.
3. **Trial load and reconciliation.** `migkit load` loads the test environment;
   `migkit reconcile` checks the result and produces the reconciliation report.

**You receive after each run:**

- `rejects.csv`: every record that was not migrated, with the reason. Opens in
  Excel.
- `merges.csv`: every duplicate that was merged, which record was kept, and how
  similar they were (0-100).
- `reconciliation.md`: the reconciliation report (see Phase 5).

**Your part:** fix rejected records in your current system (not in the export
file, or the fix will be lost at cutover), and tell us if a rule is wrong
rather than the data. Some records are expected to be rejected, such as test
accounts or contacts of deleted companies; tell us which ones, and we record them
as accepted exclusions.

We repeat trial runs until the remaining rejects are all accepted exclusions.
Re-running is safe: records that already loaded and did not change are not
written again.

## Phase 4: Cutover

**Before the cutover day**

- [ ] Last trial run reconciled, remaining rejects accepted in writing
- [ ] Mapping version frozen (no changes after the last trial run)
- [ ] Production target database reachable and backed up
- [ ] Cutover time and freeze window agreed with your users
- [ ] Rollback decision owner named

**On the day**

1. You freeze edits in the current system (read-only, or an announced freeze).
2. You produce the final exports, with the same settings as the trial runs.
3. We run `migkit validate` and compare the reject list with the last trial run.
   New rejects are reviewed with you before going further.
4. We run `migkit plan` against production and review the counts with you.
5. We run `migkit load`. The load is a single transaction: it either completes
   fully or changes nothing.
6. We run `migkit reconcile` and send you the report.

**Rollback.** If the load fails, the transaction is rolled back and production
is unchanged; we fix the cause and re-run. If you decide to roll back after a
successful load, we restore the pre-cutover backup of the target database. The
load never deletes data that was already in the target, so a partial rollback by
deleting migrated records is also possible, using the keys in the report.

## Phase 5: Reconciliation and sign-off

The reconciliation report answers three questions, using what is actually in
the target database, not what the loader reports about itself.

**1. Is every record accounted for?** For every table:

```
source rows = loaded + rejected + merged
```

and every loaded record is read back from the target and compared field by
field with what was planned. "Missing" and "differs" must both be zero.

**2. Do the totals match?** For each amount your finance team checks:

```
source total - rejected amounts - merged-away amounts = total in the target
```

broken down by currency where amounts are in more than one currency. Amounts
that could not be read at all (for example "see email") are listed as
"unparsed"; their records are always in the rejects file. When a duplicate
fills in an amount its surviving record was missing, that amount is loaded
with the survivor and is not counted as merged away.

**3. Was it produced by the approved mapping?** The report shows the mapping
fingerprint of the last load and confirms it matches the approved file.

The report ends with the full list of rejected records and merged duplicates.
When the result says **Reconciled** and the rejects match the accepted
exclusions, you sign off.

## After go-live: corrections and late records

If records are corrected or added in the old system after go-live (for example
during a parallel run), we can run the same migration again. Only new and
changed records are written; records that exist only in the new platform are
never modified or deleted. Each run adds an entry to the migration log with its
counts and mapping fingerprint.

## Data handling

- Exports are processed on infrastructure agreed with you and are deleted after
  sign-off unless you ask us to keep them.
- Database credentials are passed through the `MIGKIT_DATABASE_URL` environment
  variable, never stored in files or reports.
- Reports contain record keys and the values that failed validation. Treat them
  with the same care as the exports.

## Glossary

- **Reject**: a source record that is not migrated, with the reason.
- **Merge**: a duplicate record folded into the record that is kept (the
  survivor). Records that pointed at the duplicate, such as invoices, are moved
  to the survivor.
- **Orphan**: a record that points at a parent that does not exist or was
  rejected, for example a contact whose account is missing.
- **Dry run**: a run that shows what would change without writing anything.
- **Reconciliation**: proof that the target contains what the source contained,
  minus the documented rejects and merges.
