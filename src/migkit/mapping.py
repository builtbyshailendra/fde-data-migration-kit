"""The YAML mapping specification.

A mapping file is the contract between the customer's legacy exports and the
target schema. It is parsed into pydantic models so that a typo in a mapping
fails fast with a precise message instead of halfway through a load.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

FieldType = Literal["string", "integer", "decimal", "boolean", "date", "datetime"]
SimilarityMethod = Literal["exact", "ratio", "token_sort", "token_set"]

DEFAULT_NULL_VALUES: tuple[str, ...] = (
    "",
    "NULL",
    "null",
    "N/A",
    "n/a",
    "NA",
    "na",
    "-",
    "--",
    "none",
    "None",
    "?",
)

DEFAULT_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d",
    "%d-%b-%Y",
    "%d %b %Y",
    "%b %d %Y",
    "%b %d, %Y",
)


class MappingError(ValueError):
    """Raised when a mapping file cannot be loaded or is inconsistent."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SourceSpec(_Strict):
    """A legacy export: a CSV file or one sheet of an Excel workbook."""

    path: str
    sheet: str | None = None
    delimiter: str | None = None
    encoding: str | None = None


class LookupSpec(_Strict):
    """A reference table used to translate codes, e.g. country name to ISO code."""

    path: str | None = None
    key: str = "key"
    value: str = "value"
    values: dict[str, str] | None = None

    @model_validator(mode="after")
    def _one_source(self) -> LookupSpec:
        if (self.path is None) == (self.values is None):
            raise ValueError("a lookup needs exactly one of 'path' or 'values'")
        return self


class SplitSpec(_Strict):
    """Take one part of a delimited column, e.g. the first name of 'Ada Lovelace'."""

    column: str
    sep: str = " "
    index: int = 0
    maxsplit: int = -1
    from_right: bool = False


class JoinSpec(_Strict):
    """Concatenate several columns, skipping empty ones."""

    columns: list[str] = Field(min_length=1)
    sep: str = " "


class LookupRef(_Strict):
    """Translate a value through a named lookup table."""

    table: str
    case_insensitive: bool = True
    on_missing: Literal["reject", "null", "keep"] = "reject"


class FieldSpec(_Strict):
    """One column of a target table and how to derive it from the source row."""

    name: str
    source: str | None = Field(default=None, alias="from")
    split: SplitSpec | None = None
    join: JoinSpec | None = None
    constant: Any = None
    type: FieldType = "string"
    transforms: list[str | dict[str, Any]] = Field(default_factory=list)
    value_map: dict[str, Any] | None = None
    lookup: LookupRef | None = None
    default: Any = None
    formats: list[str] | None = None
    scale: int = Field(default=2, ge=0, le=10)
    references: str | None = None
    # Validation rules, enforced by pandera or pydantic.
    required: bool = False
    unique: bool = False
    pattern: str | None = None
    allowed: list[Any] | None = None
    min: float | None = None
    max: float | None = None
    max_length: int | None = Field(default=None, gt=0)
    reconcile_sum: bool = False

    @model_validator(mode="after")
    def _one_origin(self) -> FieldSpec:
        origins = [
            self.source is not None,
            self.split is not None,
            self.join is not None,
            self.constant is not None,
        ]
        if sum(origins) != 1:
            raise ValueError(
                f"field '{self.name}' needs exactly one of 'from', 'split', 'join' or 'constant'"
            )
        if self.references is not None and "." not in self.references:
            raise ValueError(
                f"field '{self.name}': references must look like 'table.column', "
                f"got '{self.references}'"
            )
        if self.reconcile_sum and self.type not in ("integer", "decimal"):
            raise ValueError(f"field '{self.name}': reconcile_sum needs an integer or decimal")
        return self

    @property
    def source_columns(self) -> list[str]:
        """Source columns this field reads, used to check the export has them."""
        if self.source is not None:
            return [self.source]
        if self.split is not None:
            return [self.split.column]
        if self.join is not None:
            return list(self.join.columns)
        return []


class DedupeField(_Strict):
    name: str
    weight: float = Field(default=1.0, gt=0)
    method: SimilarityMethod = "token_sort"
    strip_suffixes: bool = False


class DedupeSpec(_Strict):
    """Fuzzy duplicate detection for one target table."""

    block_on: list[str] = Field(min_length=1)
    fields: list[DedupeField] = Field(min_length=1)
    threshold: float = Field(default=90.0, ge=0, le=100)
    survivor: str = "most_complete"
    merge_fields: bool = True

    @field_validator("survivor")
    @classmethod
    def _survivor_rule(cls, value: str) -> str:
        if value in ("most_complete", "first") or value.startswith("latest:"):
            return value
        raise ValueError("survivor must be 'most_complete', 'first' or 'latest:<field>'")


CompareOp = Literal[">=", ">", "<=", "<", "==", "!="]


class RowCheck(_Strict):
    """A rule comparing two fields of the same row, e.g. due_date >= invoice_date.

    The check is skipped when either side is null; use ``required`` for that.
    """

    left: str
    op: CompareOp
    right: str
    message: str | None = None

    @property
    def description(self) -> str:
        return self.message or f"{self.left} {self.op} {self.right}"


class TargetSpec(_Strict):
    """A table in the target database."""

    table: str
    source: str
    key: str
    fields: list[FieldSpec] = Field(min_length=1)
    row_checks: list[RowCheck] = Field(default_factory=list)
    dedupe: DedupeSpec | None = None
    # Break reconciliation sums down by this field, e.g. currency.
    reconcile_by: str | None = None

    @model_validator(mode="after")
    def _key_and_names(self) -> TargetSpec:
        names = [f.name for f in self.fields]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"table '{self.table}': duplicate field names {duplicates}")
        if self.key not in names:
            raise ValueError(f"table '{self.table}': key '{self.key}' is not one of its fields")
        for check in self.row_checks:
            unknown_sides = sorted({check.left, check.right} - set(names))
            if unknown_sides:
                raise ValueError(
                    f"table '{self.table}': row check refers to unknown fields {unknown_sides}"
                )
        if self.reconcile_by is not None and self.reconcile_by not in names:
            raise ValueError(
                f"table '{self.table}': reconcile_by '{self.reconcile_by}' is not one of its fields"
            )
        if self.dedupe is not None:
            wanted = [f.name for f in self.dedupe.fields] + list(self.dedupe.block_on)
            if self.dedupe.survivor.startswith("latest:"):
                wanted.append(self.dedupe.survivor.split(":", 1)[1])
            unknown = sorted(set(wanted) - set(names))
            if unknown:
                raise ValueError(f"table '{self.table}': dedupe refers to unknown fields {unknown}")
        return self

    def field(self, name: str) -> FieldSpec:
        for spec in self.fields:
            if spec.name == name:
                return spec
        raise KeyError(name)

    @property
    def key_field(self) -> FieldSpec:
        return self.field(self.key)

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]


class MappingSpec(_Strict):
    """The whole migration: where the data comes from and where it goes."""

    version: int = 1
    name: str = "migration"
    null_values: list[str] = Field(default_factory=lambda: list(DEFAULT_NULL_VALUES))
    date_formats: list[str] = Field(default_factory=lambda: list(DEFAULT_DATE_FORMATS))
    sources: dict[str, SourceSpec]
    lookups: dict[str, LookupSpec] = Field(default_factory=dict)
    targets: list[TargetSpec] = Field(min_length=1)

    _base_dir: Path = PrivateAttr(default=Path("."))
    _checksum: str = PrivateAttr(default="")

    @model_validator(mode="after")
    def _cross_references(self) -> MappingSpec:
        seen: dict[str, TargetSpec] = {}
        for target in self.targets:
            if target.source not in self.sources:
                raise ValueError(f"table '{target.table}': unknown source '{target.source}'")
            if target.table in seen:
                raise ValueError(f"table '{target.table}' is defined twice")
            for spec in target.fields:
                if spec.lookup is not None and spec.lookup.table not in self.lookups:
                    raise ValueError(
                        f"{target.table}.{spec.name}: unknown lookup '{spec.lookup.table}'"
                    )
                if spec.references is not None:
                    ref_table, ref_column = spec.references.split(".", 1)
                    parent = seen.get(ref_table)
                    if parent is None:
                        raise ValueError(
                            f"{target.table}.{spec.name}: references '{ref_table}', which must "
                            "be defined earlier in 'targets'"
                        )
                    if ref_column != parent.key:
                        raise ValueError(
                            f"{target.table}.{spec.name}: can only reference the key of "
                            f"'{ref_table}' ('{parent.key}')"
                        )
            seen[target.table] = target
        return self

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def checksum(self) -> str:
        """SHA-256 of the mapping file, recorded with every load for traceability."""
        return self._checksum

    def target(self, table: str) -> TargetSpec:
        for target in self.targets:
            if target.table == table:
                return target
        raise KeyError(table)

    def resolve(self, relative: str) -> Path:
        """Resolve a path from the mapping relative to the mapping file."""
        path = Path(relative)
        return path if path.is_absolute() else self._base_dir / path


def parse_mapping(
    data: dict[str, Any], base_dir: Path | None = None, checksum: str = ""
) -> MappingSpec:
    """Build a MappingSpec from already-parsed YAML, turning errors into MappingError."""
    try:
        spec = MappingSpec.model_validate(data)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            where = ".".join(str(p) for p in err["loc"]) or "<root>"
            lines.append(f"  {where}: {err['msg']}")
        raise MappingError("invalid mapping:\n" + "\n".join(lines)) from exc
    if base_dir is not None:
        spec._base_dir = base_dir
    spec._checksum = checksum
    return spec


def load_mapping(path: str | Path) -> MappingSpec:
    """Load and validate a mapping YAML file."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MappingError(f"cannot read mapping {path}: {exc.strerror}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise MappingError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise MappingError(f"{path} must contain a mapping at the top level")
    return parse_mapping(
        data, base_dir=path.resolve().parent, checksum=hashlib.sha256(raw).hexdigest()
    )
