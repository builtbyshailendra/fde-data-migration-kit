"""Target database access: schema from the mapping, batched reads and upserts.

Only SQLAlchemy Core is used. The schema is derived from the mapping so the
same file that describes the migration also describes the tables it writes;
an existing target schema works too, as long as the table and column names
match and the key column has a primary key or unique constraint.

Upserts use ``INSERT ... ON CONFLICT (key) DO UPDATE``, which SQLite (3.24+)
and PostgreSQL (9.5+) both support with the same semantics.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection
from sqlalchemy.sql.dml import Insert
from sqlalchemy.types import TypeEngine

from migkit.mapping import FieldSpec, MappingSpec, TargetSpec

RUNS_TABLE = "migkit_runs"
SUPPORTED_DIALECTS: tuple[str, ...] = ("sqlite", "postgresql")
# Keeps IN (...) lists and multi-row VALUES well under every driver's parameter limit.
READ_BATCH = 500


class DatabaseError(RuntimeError):
    """Raised for target database problems the operator has to fix."""


def connect(url: str) -> Engine:
    """Create an engine; SQLite gets foreign keys enforced like PostgreSQL does."""
    engine = create_engine(url, future=True)
    if engine.dialect.name not in SUPPORTED_DIALECTS:
        raise DatabaseError(
            f"unsupported database '{engine.dialect.name}', expected one of {SUPPORTED_DIALECTS}"
        )
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def column_type(spec: FieldSpec) -> TypeEngine[Any]:
    if spec.type == "string":
        return String(spec.max_length) if spec.max_length else Text()
    if spec.type == "integer":
        # SQLite only aliases ROWID for INTEGER, so keep keys INTEGER there.
        return BigInteger().with_variant(Integer(), "sqlite")
    if spec.type == "decimal":
        return Numeric(18, spec.scale, asdecimal=True)
    if spec.type == "boolean":
        return Boolean()
    if spec.type == "date":
        return Date()
    return DateTime()


def build_metadata(spec: MappingSpec) -> MetaData:
    """Tables for every target plus the run log, foreign keys included."""
    metadata = MetaData()
    for target in spec.targets:
        columns = []
        for field_spec in target.fields:
            args: list[Any] = [field_spec.name, column_type(field_spec)]
            if field_spec.references is not None:
                args.append(ForeignKey(field_spec.references))
            columns.append(
                Column(
                    *args,
                    primary_key=field_spec.name == target.key,
                    nullable=field_spec.name != target.key and not field_spec.required,
                    autoincrement=False,
                )
            )
        Table(target.table, metadata, *columns)
    Table(
        RUNS_TABLE,
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("run_id", String(32), nullable=False, index=True),
        Column("mapping_name", String(200), nullable=False),
        Column("mapping_checksum", String(64), nullable=False),
        Column("engine", String(20), nullable=False),
        Column("table_name", String(200), nullable=False),
        Column("inserted", Integer, nullable=False),
        Column("updated", Integer, nullable=False),
        Column("unchanged", Integer, nullable=False),
        Column("rejected", Integer, nullable=False),
        Column("merged", Integer, nullable=False),
        Column("started_at", DateTime, nullable=False),
        Column("finished_at", DateTime, nullable=False),
    )
    return metadata


def table_exists(conn: Connection, name: str) -> bool:
    return inspect(conn).has_table(name)


def batched(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    if size < 1:
        raise ValueError("batch size must be at least 1")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def fetch_rows(conn: Connection, table: Table, key: str, keys: Iterable[Any]) -> dict[Any, dict]:
    """Existing rows for the given keys, read in batches."""
    wanted = sorted(k for k in set(keys) if k is not None)
    found: dict[Any, dict[str, Any]] = {}
    key_column = table.c[key]
    for chunk in batched(wanted, READ_BATCH):
        for row in conn.execute(select(table).where(key_column.in_(chunk))).mappings():
            found[row[key]] = dict(row)
    return found


def count_rows(conn: Connection, table: Table) -> int:
    return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def upsert_statement(table: Table, target: TargetSpec, dialect: str) -> Insert:
    """``INSERT ... ON CONFLICT (key) DO UPDATE SET <every other column>``."""
    if dialect == "sqlite":
        stmt = sqlite.insert(table)
    elif dialect == "postgresql":
        stmt = postgresql.insert(table)
    else:
        raise DatabaseError(f"upsert is not supported on '{dialect}'")
    updates = {c.name: stmt.excluded[c.name] for c in table.columns if c.name != target.key}
    if not updates:
        return stmt.on_conflict_do_nothing(index_elements=[target.key])
    return stmt.on_conflict_do_update(index_elements=[target.key], set_=updates)


def upsert_rows(
    conn: Connection,
    table: Table,
    target: TargetSpec,
    rows: Sequence[dict[str, Any]],
    batch_size: int,
) -> int:
    """Upsert rows in batches; returns the number of batches executed."""
    if not rows:
        return 0
    stmt = upsert_statement(table, target, conn.dialect.name)
    batches = 0
    for chunk in batched(rows, batch_size):
        conn.execute(stmt, list(chunk))
        batches += 1
    return batches
