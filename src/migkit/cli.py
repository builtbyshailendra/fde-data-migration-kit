"""Command line interface: ``migkit profile | validate | plan | load | reconcile``.

Exit codes: 0 success, 1 the run worked but found a problem the operator asked
to fail on (rejects with ``--strict``, or a reconciliation mismatch), 2 bad
input such as an invalid mapping, a missing file or an unreachable database.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from migkit import __version__
from migkit.db import DatabaseError, connect
from migkit.diff import diff_plan, render_diff
from migkit.load import DEFAULT_BATCH_SIZE, load_plan, render_load
from migkit.mapping import DEFAULT_NULL_VALUES, MappingError, MappingSpec, load_mapping
from migkit.pipeline import (
    MigrationPlan,
    build_plan,
    render_merges,
    render_rejects,
    render_summary,
)
from migkit.profiling import TableProfile, profile_frame
from migkit.profiling import render_markdown as profile_markdown
from migkit.profiling import render_text as profile_text
from migkit.reconcile import reconcile
from migkit.reconcile import render_markdown as reconcile_markdown
from migkit.reconcile import render_text as reconcile_text
from migkit.results import merges_frame, rejects_frame, write_csv
from migkit.sources import SourceError, read_source, read_table
from migkit.transform import TransformError
from migkit.validate import ENGINES

DB_ENV = "MIGKIT_DATABASE_URL"
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2

USER_ERRORS = (
    MappingError,
    SourceError,
    TransformError,
    DatabaseError,
    ArgumentError,
    SQLAlchemyError,
    ValueError,
)


class CliError(Exception):
    """A problem with the command line itself, reported without a traceback."""


def _write_text(path: str, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target}")


def _database_url(args: argparse.Namespace) -> str:
    url = args.db or os.environ.get(DB_ENV)
    if not url:
        raise CliError(f"no target database: pass --db or set {DB_ENV}")
    return str(url)


def _plan(args: argparse.Namespace) -> tuple[MappingSpec, MigrationPlan]:
    spec = load_mapping(args.mapping)
    return spec, build_plan(spec, args.engine)


def _write_reports(args: argparse.Namespace, plan: MigrationPlan) -> None:
    if getattr(args, "rejects", None):
        print(f"wrote {write_csv(rejects_frame(plan.rejects), args.rejects)}")
    if getattr(args, "merges", None):
        print(f"wrote {write_csv(merges_frame(plan.merges), args.merges)}")


# --- commands --------------------------------------------------------------------------------


def cmd_profile(args: argparse.Namespace) -> int:
    profiles: list[TableProfile] = []
    if args.mapping:
        spec = load_mapping(args.mapping)
        for name, source in spec.sources.items():
            frame = read_source(spec, name)
            label = f"{name} ({source.path}{'#' + source.sheet if source.sheet else ''})"
            profiles.append(profile_frame(frame, label, spec.null_values, args.top))
    for file in args.files:
        frame = read_table(file, sheet=args.sheet)
        profiles.append(profile_frame(frame, file, DEFAULT_NULL_VALUES, args.top))
    if not profiles:
        raise CliError("give one or more files to profile, or --mapping")

    if args.format == "json":
        text = json.dumps([p.to_dict() for p in profiles], indent=2)
    elif args.format == "markdown":
        text = "# Source profile\n\n" + "\n".join(profile_markdown(p) for p in profiles)
    else:
        text = "\n\n".join(profile_text(p) for p in profiles)
    if args.output:
        _write_text(args.output, text)
    else:
        print(text)
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    _, plan = _plan(args)
    print(render_summary(plan))
    print()
    print(render_rejects(plan, args.limit))
    print()
    print(render_merges(plan))
    _write_reports(args, plan)
    if args.strict and plan.rejects:
        print(f"\n{len(plan.rejects)} rejected row(s); failing because of --strict")
        return EXIT_FAILED
    return EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    _, plan = _plan(args)
    engine = connect(_database_url(args))
    try:
        diffs = diff_plan(plan, engine)
    finally:
        engine.dispose()
    print(render_summary(plan))
    print()
    print("dry run against the target (nothing written):")
    print(render_diff(diffs, sample=args.sample))
    if args.json:
        payload = {
            "mapping": plan.spec.name,
            "summary": [
                {
                    "table": t.table,
                    "source_rows": t.source_rows,
                    "planned": len(t.frame),
                    "rejected": t.rejected_by_stage(),
                    "merged": len(t.merges),
                }
                for t in plan.tables
            ],
            "diff": [d.to_dict(sample=args.sample) for d in diffs],
        }
        _write_text(args.json, json.dumps(payload, indent=2, default=str))
    return EXIT_OK


def cmd_load(args: argparse.Namespace) -> int:
    _, plan = _plan(args)
    engine = connect(_database_url(args))
    try:
        result = load_plan(
            plan, engine, batch_size=args.batch_size, create_tables=not args.no_create_tables
        )
    finally:
        engine.dispose()
    print(render_load(result))
    print(f"\n{result.written} row(s) written, {len(plan.rejects)} rejected")
    _write_reports(args, plan)
    return EXIT_OK


def cmd_reconcile(args: argparse.Namespace) -> int:
    _, plan = _plan(args)
    engine = connect(_database_url(args))
    try:
        rec = reconcile(plan, engine)
    finally:
        engine.dispose()
    print(reconcile_text(rec))
    if args.report:
        _write_text(args.report, reconcile_markdown(rec))
    if args.json:
        _write_text(args.json, rec.to_json())
    _write_reports(args, plan)
    return EXIT_OK if rec.ok else EXIT_FAILED


# --- parser ----------------------------------------------------------------------------------


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migkit",
        description="Profile, map, validate, deduplicate, load and reconcile legacy data.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def mapping_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("-m", "--mapping", required=True, help="mapping YAML file")
        p.add_argument(
            "--engine", choices=ENGINES, default="pandera", help="validation engine (pandera)"
        )

    def db_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--db",
            help=f"target SQLAlchemy URL, e.g. sqlite:///target.db (default: ${DB_ENV})",
        )

    def report_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--rejects", metavar="CSV", help="write every rejected row to this CSV")

    p = sub.add_parser("profile", help="profile CSV/Excel exports before writing a mapping")
    p.add_argument("files", nargs="*", help="CSV or Excel files")
    p.add_argument("-m", "--mapping", help="profile every source named in this mapping")
    p.add_argument("--sheet", help="Excel sheet name (default: first sheet)")
    p.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    p.add_argument("--top", type=_positive_int, default=5, help="top values/patterns to keep")
    p.add_argument("-o", "--output", help="write to a file instead of stdout")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("validate", help="transform and validate; report rejects and duplicates")
    mapping_args(p)
    report_args(p)
    p.add_argument("--merges", metavar="CSV", help="write merged duplicates to this CSV")
    p.add_argument("--limit", type=_positive_int, default=50, help="rejects to print (50)")
    p.add_argument("--strict", action="store_true", help="exit 1 when any row is rejected")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("plan", help="dry-run diff against the target database")
    mapping_args(p)
    db_arg(p)
    p.add_argument("--sample", type=_positive_int, default=3, help="updates to show per table")
    p.add_argument("--json", metavar="PATH", help="write the plan and diff as JSON")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("load", help="idempotent batched upsert into the target database")
    mapping_args(p)
    db_arg(p)
    report_args(p)
    p.add_argument("--batch-size", type=_positive_int, default=DEFAULT_BATCH_SIZE)
    p.add_argument(
        "--no-create-tables",
        action="store_true",
        help="fail instead of creating missing target tables",
    )
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("reconcile", help="check counts and sums in the target; write a report")
    mapping_args(p)
    db_arg(p)
    report_args(p)
    p.add_argument("--report", metavar="MD", help="write the Markdown reconciliation report")
    p.add_argument("--json", metavar="PATH", help="write the reconciliation as JSON")
    p.set_defaults(func=cmd_reconcile)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Legacy data is full of characters a cp1252 console cannot print.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CliError as exc:
        print(f"migkit: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except USER_ERRORS as exc:
        print(f"migkit: error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
