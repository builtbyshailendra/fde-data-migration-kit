import json
from pathlib import Path

import pandas as pd
import pytest

from conftest import ROOT, SAMPLE_MAPPING
from migkit import __version__
from migkit.cli import DB_ENV, main

CONTACTS = str(ROOT / "examples" / "legacy_crm" / "contacts.csv")


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_profile_file_as_json(tmp_path: Path) -> None:
    out = tmp_path / "profile.json"
    assert main(["profile", CONTACTS, "--format", "json", "-o", str(out)]) == 0
    (profile,) = json.loads(out.read_text(encoding="utf-8"))
    assert profile["rows"] == 30
    assert profile["columns"][0]["candidate_key"] is True


def test_profile_every_source_of_a_mapping(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["profile", "--mapping", str(SAMPLE_MAPPING), "--format", "markdown"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Source profile")
    assert "## invoices (invoices.xlsx#Invoices)" in out


def test_profile_without_input_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["profile"]) == 2
    assert "give one or more files" in capsys.readouterr().err


def test_validate_writes_rejects_and_merges(
    mini_files: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rejects, merges = tmp_path / "out" / "rejects.csv", tmp_path / "out" / "merges.csv"
    code = main(
        ["validate", "-m", str(mini_files), "--rejects", str(rejects), "--merges", str(merges)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "rejected rows (4):" in out
    assert "customers row 4: C-3 -> C-1" in out
    frame = pd.read_csv(rejects, encoding="utf-8-sig")
    assert list(frame.columns) == ["table", "stage", "source_row", "key", "reasons"]
    assert len(frame) == 4
    assert len(pd.read_csv(merges, encoding="utf-8-sig")) == 1


@pytest.mark.parametrize("engine", ["pandera", "pydantic"])
def test_validate_strict_fails_on_rejects(mini_files: Path, engine: str) -> None:
    assert main(["validate", "-m", str(mini_files), "--engine", engine, "--strict"]) == 1


def test_plan_load_reconcile_round_trip(
    mini_files: Path, db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_json = tmp_path / "plan.json"
    assert main(["plan", "-m", str(mini_files), "--db", db_url, "--json", str(plan_json)]) == 0
    assert "dry run against the target (nothing written)" in capsys.readouterr().out
    assert json.loads(plan_json.read_text(encoding="utf-8"))["diff"][0]["inserts"] == 2

    assert main(["reconcile", "-m", str(mini_files), "--db", db_url]) == 1
    capsys.readouterr()

    assert main(["load", "-m", str(mini_files), "--db", db_url, "--batch-size", "2"]) == 0
    assert "5 row(s) written, 4 rejected" in capsys.readouterr().out
    assert main(["load", "-m", str(mini_files), "--db", db_url]) == 0
    assert "0 row(s) written" in capsys.readouterr().out

    report, as_json = tmp_path / "rec.md", tmp_path / "rec.json"
    code = main(
        [
            *("reconcile", "-m", str(mini_files), "--db", db_url),
            *("--report", str(report), "--json", str(as_json)),
        ]
    )
    assert code == 0
    assert "result: RECONCILED" in capsys.readouterr().out
    assert "**Reconciled**" in report.read_text(encoding="utf-8")
    assert json.loads(as_json.read_text(encoding="utf-8"))["ok"] is True


def test_database_url_from_environment(
    mini_files: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DB_ENV, db_url)
    assert main(["load", "-m", str(mini_files)]) == 0


def test_missing_database_url(
    mini_files: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(DB_ENV, raising=False)
    assert main(["plan", "-m", str(mini_files)]) == 2
    assert f"pass --db or set {DB_ENV}" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["validate", "-m", "does-not-exist.yaml"], "cannot read mapping"),
        (["plan", "-m", str(SAMPLE_MAPPING), "--db", "not a url"], "Could not parse"),
    ],
)
def test_bad_input_exits_2(
    args: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(args) == 2
    assert message in capsys.readouterr().err


@pytest.fixture
def mini_files(write_files) -> Path:
    return write_files()
