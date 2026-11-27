"""Result types shared by the pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from migkit.sources import SOURCE_ROW


@dataclass(frozen=True)
class Reject:
    """A source record that will not be loaded, and why."""

    table: str
    stage: str  # transform, reference, validate
    source_row: int
    key: str | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "stage": self.stage,
            "source_row": self.source_row,
            "key": self.key,
            "reasons": "; ".join(self.reasons),
        }


@dataclass(frozen=True)
class Merge:
    """A duplicate record folded into a surviving record.

    ``filled`` names the fields whose value this duplicate contributed to the
    survivor (``merge_fields``): those values are loaded, not merged away.
    """

    table: str
    survivor_key: Any
    merged_key: Any
    score: float
    source_row: int
    filled: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "source_row": self.source_row,
            "merged_key": self.merged_key,
            "survivor_key": self.survivor_key,
            "score": self.score,
            "filled_fields": ", ".join(self.filled),
        }


@dataclass
class StageResult:
    """Rows that passed a stage plus the rows it rejected."""

    frame: pd.DataFrame
    rejects: list[Reject] = field(default_factory=list)


def rejects_frame(rejects: list[Reject]) -> pd.DataFrame:
    columns = ["table", "stage", "source_row", "key", "reasons"]
    return pd.DataFrame([r.to_dict() for r in rejects], columns=columns)


def merges_frame(merges: list[Merge]) -> pd.DataFrame:
    columns = ["table", "source_row", "merged_key", "survivor_key", "score", "filled_fields"]
    return pd.DataFrame([m.to_dict() for m in merges], columns=columns)


def write_csv(frame: pd.DataFrame, path: str | Path) -> Path:
    """Write a report CSV with a BOM so Excel opens accented names correctly."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def drop_rows(frame: pd.DataFrame, source_rows: set[int]) -> pd.DataFrame:
    """Remove rows by their source row number."""
    if not source_rows:
        return frame
    return frame[~frame[SOURCE_ROW].isin(source_rows)].reset_index(drop=True)
