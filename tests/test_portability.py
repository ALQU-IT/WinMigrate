"""Guards against the bugs that only appear on the platform this tool runs on.

The suite runs on Linux during development and on Windows in CI, and this
project has now been bitten three times by the difference: a console that could
not encode the JSON output, a shadow-copy path built for the wrong prefix
convention, and a batch of tests that read UTF-8 files with the locale's codec.
Each was invisible on the host where the code was written.

These are cheap source-level checks. They cannot replace running on Windows,
but they catch the classes that keep recurring at the moment they are written
rather than twenty minutes into a CI run.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted((ROOT / "winmigrate").rglob("*.py")) + sorted((ROOT / "tests").glob("*.py"))


def calls_in(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def keyword_names(node: ast.Call) -> set[str]:
    return {kw.arg for kw in node.keywords if kw.arg}


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_text_files_are_read_and_written_with_an_explicit_encoding(path: Path):
    r"""Windows defaults to the locale codec, which is cp1252 on most machines.

    ``Path.read_text()`` with no encoding therefore decodes UTF-8 content as
    cp1252 and raises UnicodeDecodeError on the first character above 0x7F --
    an em dash in a browser profile title, a box-drawing character in the GUI.
    It passes on Linux, where the default is UTF-8, so the failure only ever
    appears on the machine the tool is actually for.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in calls_in(tree):
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("read_text", "write_text"):
            continue
        if "encoding" not in keyword_names(node):
            offenders.append(node.lineno)
    assert not offenders, (
        f"{path.relative_to(ROOT)} lines {offenders}: read_text/write_text without "
        f'encoding="utf-8" decodes as cp1252 on Windows'
    )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_open_in_text_mode_declares_an_encoding(path: Path):
    """Same reasoning, for the builtin. Binary modes are exempt: they have no
    encoding to declare."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in calls_in(tree):
        if not (isinstance(node.func, ast.Name) and node.func.id == "open"):
            continue
        mode = ""
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = str(kw.value.value)
        if "b" in mode:
            continue
        if "encoding" not in keyword_names(node):
            offenders.append(node.lineno)
    assert not offenders, (
        f"{path.relative_to(ROOT)} lines {offenders}: open() in text mode without "
        f"an encoding uses the locale's codec"
    )


def test_the_dry_run_assertion_survives_a_narrow_console():
    """The report's wording is a property of the report; where rich breaks the
    line is a property of the terminal. Asserting on the second is how a passing
    test turns red because a temp path got longer."""
    from rich.console import Console

    from winmigrate.models import Note, Severity
    from winmigrate.report import render_restore_report
    from winmigrate.restore import RestoreReport

    report = RestoreReport(
        bundle=Path("b.dat"),
        destination=Path("C:/Users/runneradmin/AppData/Local/Temp/pytest-of-runneradmin/x/nowhere"),
        dry_run=True,
    )
    report.notes.append(Note(Severity.INFO, "preview"))
    for width in (60, 80, 120, 200):
        console = Console(record=True, width=width)
        render_restore_report(report, console, dry_run=True)
        collapsed = " ".join(console.export_text().split())
        assert "was not created" in collapsed, width
