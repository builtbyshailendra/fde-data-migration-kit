# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-08

### Added

- `migkit profile`: type inference, null and placeholder counts, uniqueness,
  value patterns, date format mix and findings for CSV and Excel exports, as
  text, Markdown or JSON.
- YAML mapping validated with pydantic: renames, splits, joins, constants,
  string transforms, value maps, lookups, defaults, casts to string, integer,
  decimal, boolean, date and datetime, foreign key references and validation
  rules.
- Source reading with encoding detection, delimiter sniffing and named Excel
  sheets; every value read as text.
- Validation with pandera (default) or pydantic from the same rules, plus
  cross-field row checks.
- Fuzzy deduplication with multi-pass blocking, weighted rapidfuzz scoring,
  union-find clustering, survivor rules and child record repointing.
- `migkit validate` with rejects and merges CSV output and `--strict`.
- `migkit plan`: dry-run diff against the target database.
- `migkit load`: transactional, batched, idempotent upserts into SQLite or
  PostgreSQL with a `migkit_runs` log carrying the mapping SHA-256.
- `migkit reconcile`: row counts, field-by-field comparison, sums per group and
  mapping provenance, as text, Markdown and JSON.
- Sample legacy CRM exports (accounts, contacts, invoices workbook) with a
  deterministic workbook generator.
- Customer migration runbook, mapping reference, architecture notes and ADRs.
- Dockerfile, Makefile and GitHub Actions CI including a PostgreSQL job.

### Fixed (before release)

- Reconciliation reported a mismatch when `merge_fields` filled a survivor's
  empty amount from a duplicate, although the load was correct: that amount is
  now counted as loaded, not merged away. Merges record the fields they filled
  (`filled_fields` in the merges CSV), and dedupe never copies an amount across
  `reconcile_by` groups.
- Exact duplicate column headers were renamed `name.1` by pandas instead of
  being rejected.
- Source row numbers drifted after multi-line quoted CSV fields and in Excel
  sheets with blank rows above the header. CSV records are now numbered by the
  line they start on and Excel records by worksheet row. Unterminated quotes
  are an error instead of swallowing the rest of the file.

[0.1.0]: https://github.com/builtbyshailendra/fde-data-migration-kit/releases/tag/v0.1.0
