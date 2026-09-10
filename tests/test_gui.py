"""The GUI's logic, which is deliberately not in the GUI.

Every decision about what a bundle contains stays in the scan and capture
modules; the window only shows them and collects clicks. That split is what
makes this testable at all -- tkinter needs a display, so anything that mattered
and lived in a widget callback would be untested by construction.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from winmigrate.gui import defaults, selection
from winmigrate.models import Action, Category, Item, Kind, ScanResult, Sensitivity, SkipReason


def item(item_id, *, action=Action.CAPTURE, kind=Kind.TREE, size=1000, files=10,
         secret=False, reason=None) -> Item:
    return Item(
        id=item_id,
        category=Category.USER_FILES,
        kind=kind,
        title=item_id.title(),
        action=action,
        size_bytes=size,
        file_count=files,
        sensitivity=Sensitivity.SECRET if secret else Sensitivity.NORMAL,
        skip_reason=reason,
    )


def scan_with(*items) -> ScanResult:
    result = ScanResult(source=None)
    result.items = list(items)
    return result


def test_rows_offer_a_choice_only_where_there_is_one():
    """A folder the scan already rejected is shown, greyed, with why.

    Hiding it would leave the user wondering where Downloads went; offering a
    tick box that does nothing would be worse.
    """
    scan = scan_with(
        item("files:documents"),
        item("files:downloads", action=Action.SKIP, reason=SkipReason.EXCLUDED),
        item("files:empty", action=Action.SKIP, reason=SkipReason.EMPTY),
        item("browser:chrome:default", secret=True),
    )
    rows = {row.item_id: row for row in selection.rows_for(scan)}

    assert rows["files:documents"].selectable and rows["files:documents"].selected
    assert not rows["files:downloads"].selectable
    assert rows["files:downloads"].reason == "excluded by a pattern"
    assert rows["files:empty"].reason == "empty"
    assert rows["browser:chrome:default"].secret is True


def test_records_and_reports_are_not_offered_as_tick_boxes():
    """Printers, the software inventory and the sync-root explanations are what
    make a restore legible. They are not something to untick by accident."""
    scan = scan_with(
        item("files:documents"),
        item("settings:printers", kind=Kind.RECORD),
        item("sync:onedrive:0", kind=Kind.REPORT, action=Action.SKIP, reason=SkipReason.SYNCED),
    )
    assert [row.item_id for row in selection.rows_for(scan)] == ["files:documents"]


def test_unticking_marks_the_item_deselected_and_leaves_the_rest_alone():
    scan = scan_with(item("files:documents"), item("files:pictures"), item("files:music"))
    selection.apply(scan, {"files:documents", "files:music"})

    by_id = {i.id: i for i in scan.items}
    assert by_id["files:documents"].action is Action.CAPTURE
    assert by_id["files:music"].action is Action.CAPTURE
    assert by_id["files:pictures"].action is Action.SKIP
    assert by_id["files:pictures"].skip_reason is SkipReason.DESELECTED


def test_deselecting_never_overwrites_why_the_scan_skipped_something():
    """"Excluded" and "empty" are the tool's findings; "deselected" is the
    user's decision. Six months later the manifest is the only record of which
    of the two left a folder out of a backup, so the reasons must not be
    collapsed into one."""
    scan = scan_with(
        item("files:downloads", action=Action.SKIP, reason=SkipReason.EXCLUDED),
        item("files:onedrive", action=Action.SKIP, reason=SkipReason.SYNCED),
    )
    selection.apply(scan, set())  # nothing ticked at all

    by_id = {i.id: i for i in scan.items}
    assert by_id["files:downloads"].skip_reason is SkipReason.EXCLUDED
    assert by_id["files:onedrive"].skip_reason is SkipReason.SYNCED


def test_the_running_total_counts_only_what_is_ticked_and_selectable():
    scan = scan_with(
        item("a", size=1000, files=10),
        item("b", size=2000, files=20),
        item("c", size=9999, files=99, action=Action.SKIP, reason=SkipReason.EMPTY),
    )
    rows = selection.rows_for(scan)
    assert selection.selected_totals(rows, {"a", "b"}) == (3000, 30)
    assert selection.selected_totals(rows, {"a"}) == (1000, 10)
    # A blocked row cannot contribute even if its id is somehow in the set.
    assert selection.selected_totals(rows, {"a", "c"}) == (1000, 10)
    assert selection.selected_totals(rows, set()) == (0, 0)


def test_a_selection_applied_to_a_scan_is_what_capture_already_understands(tmp_path: Path):
    """The GUI does not get its own capture path. It hands the same ScanResult
    to the same capture(), or there would be two implementations of what goes in
    a bundle and the untested one would be wrong."""
    from winmigrate import capture as capture_mod
    from winmigrate import restore as restore_mod
    from winmigrate.capture import CaptureOptions
    from winmigrate.config import ScanConfig
    from winmigrate.platform_win import Environment
    from winmigrate.restore import RestoreOptions
    from winmigrate.scan import run_scan

    profile = tmp_path / "alice"
    for folder in ("Documents", "Pictures", "Music"):
        (profile / folder).mkdir(parents=True)
        (profile / folder / "f.bin").write_bytes(folder.encode() * 100)

    env = Environment.fixture(profile, {})
    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)

    rows = selection.rows_for(scan)
    keep = {r.item_id for r in rows if r.selectable and "music" not in r.item_id}
    selection.apply(scan, keep)

    bundle = tmp_path / "b.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase="pw", use_vss=False), config, env
    )
    destination = tmp_path / "out"
    destination.mkdir()
    result = restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase="pw", destination=destination)
    )
    assert result.ok
    restored = {p.parent.name for p in destination.rglob("*.bin")}
    assert restored == {"Documents", "Pictures"}  # Music was unticked


# --- where the bundle is proposed ------------------------------------------
def test_the_bundle_is_proposed_on_the_drive_the_program_runs_from(monkeypatch, tmp_path: Path):
    """Run from a USB stick, the backup should default to the USB stick. That is
    the common case and it should cost the user no thought."""
    exe = tmp_path / "E_drive" / "WinMigrate.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(defaults.sys, "frozen", True, raising=False)
    monkeypatch.setattr(defaults.sys, "executable", str(exe))

    assert defaults.program_directory() == exe.parent


def test_the_proposed_name_carries_host_user_and_a_timestamp():
    """Bundles from several machines end up in one folder more often than not,
    and a second capture must not silently land on the first."""
    path = defaults.default_bundle_path("DESKTOP-PC", "Alessio", datetime(2026, 9, 10, 14, 30, 5))
    assert path.name == "desktop-pc-alessio-20260910-143005.dat"

    # Missing pieces must not leave stray separators.
    assert defaults.default_bundle_path("", "", datetime(2026, 1, 2, 3, 4, 5)).name == (
        "20260102-030405.dat"
    )


def test_same_drive_detects_writing_the_bundle_onto_what_it_is_reading():
    """Needs the profile's size again in free space -- worth asking about before
    the capture rather than discovering at 90%."""
    assert defaults.same_drive(r"C:\Users\a\backup.dat", r"C:\Users\a") is True
    assert defaults.same_drive(r"c:\backup.dat", r"C:\Users\a") is True  # case
    assert defaults.same_drive(r"E:\backup.dat", r"C:\Users\a") is False
    assert defaults.same_drive("backup.dat", r"C:\Users\a") is False  # no drive at all


def test_importing_the_gui_package_does_not_need_tkinter():
    """The frozen build, the tests and every other command import this package.
    Only opening the window should reach for tkinter."""
    import importlib
    import sys

    for name in [n for n in sys.modules if n.startswith("winmigrate.gui")]:
        del sys.modules[name]
    importlib.import_module("winmigrate.gui")
    assert "tkinter" not in sys.modules


# --- the window itself -----------------------------------------------------
def _module_source(name: str) -> str:
    import importlib.util
    from pathlib import Path as _Path

    spec = importlib.util.find_spec(name)
    assert spec and spec.origin
    return _Path(spec.origin).read_text(encoding="utf-8")


def _app_source() -> str:
    """The window's source, read the only way that works on both platforms.

    ``read_text()`` with no encoding uses the locale's -- cp1252 on a Windows
    runner -- and app.py contains the box-drawing characters the tick column is
    made of. Located through the module rather than the working directory,
    because pytest does not promise to run from the repository root.
    """
    import importlib.util
    from pathlib import Path as _Path

    spec = importlib.util.find_spec("winmigrate.gui.app")
    assert spec and spec.origin
    return _Path(spec.origin).read_text(encoding="utf-8")


def stub_tkinter(monkeypatch):
    """Enough of tkinter to import the window without a display.

    The widgets cannot be exercised headlessly, but the module can be imported
    and inspected -- which catches the failures that actually happen to a GUI
    nobody can run in CI: a renamed attribute on a report, an event a worker
    emits that nothing handles, a method that quietly stopped existing.
    """
    import sys
    import types

    for name in ("tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox"):
        module = types.ModuleType(name)
        module.TkVersion = 8.6
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules["tkinter"].ttk = sys.modules["tkinter.ttk"]


def test_the_window_module_imports_and_keeps_its_methods(monkeypatch):
    import importlib
    import inspect

    stub_tkinter(monkeypatch)
    app = importlib.import_module("winmigrate.gui.app")
    methods = {name for name, _ in inspect.getmembers(app.WinMigrateWizard, inspect.isfunction)}
    assert {
        "_build_chrome",
        "_build_pages",
        "_show",
        "_go_next",
        "_go_back",
        "_maybe_elevate",
        "_start_scan",
        "_scan_worker",
        "_start_capture",
        "_capture_worker",
        "_drain_events",
        "_handle",
        "_render_rows",
    } <= methods


def test_every_page_of_the_wizard_has_a_builder():
    """A Step with no page is a window that goes blank when it gets there --
    no error, nothing drawn, and the buttons still work."""
    import re
    from winmigrate.gui.wizard import Step

    registered = set(re.findall(r"\(Step\.(\w+), self\._page_", _app_source()))
    assert registered == {step.name for step in Step}


def test_every_ttk_style_the_window_uses_is_configured():
    """An unknown style name is not an error in ttk -- the widget silently falls
    back to the default and the page looks wrong in a way no test would catch
    and no traceback would mention."""
    import re

    from winmigrate.gui import theme

    used = set(re.findall(r'style="([\w.]+)"', _app_source()))
    theme_source = _module_source("winmigrate.gui.theme")
    configured = set(re.findall(r'style\.configure\(\s*"([\w.]+)"', theme_source))
    named = {theme.RAIL_ON, theme.RAIL_DONE, theme.RAIL_OFF}
    assert used - configured - named == set()


def test_the_passphrase_is_cleared_as_soon_as_the_capture_has_it():
    """It lives in the widget and the options object, and nowhere else. The
    window is open for the length of an hour-long capture with the fields
    visible on screen."""
    source = _app_source()
    start = source.index("def _start_capture")
    body = source[start : source.index("def _capture_worker")]
    assert 'self.passphrase.delete(0, "end")' in body
    assert 'self.passphrase2.delete(0, "end")' in body


def test_every_event_a_worker_emits_is_handled(monkeypatch):
    """The scan and the capture run on their own threads and report back
    through a queue. An event kind nothing handles is a silent hang: the window
    sits at "Scanning…" forever with no error, which is the worst way for this
    to fail."""
    import ast

    tree = ast.parse(_app_source())

    emitted = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "put"
            and node.args
            and isinstance(node.args[0], ast.Tuple)
            and isinstance(node.args[0].elts[0], ast.Constant)
        ):
            emitted.add(node.args[0].elts[0].value)

    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_handle"
    )
    handled = {
        node.value
        for node in ast.walk(handler)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert emitted, "no events found -- the check itself has drifted"
    assert emitted <= handled, f"unhandled: {emitted - handled}"


def test_the_final_page_reads_only_fields_the_capture_report_has(monkeypatch):
    """The summary reads a CaptureReport by attribute. A renamed field would
    raise inside a callback, where the traceback goes to a console the user
    launched from Explorer and never sees."""
    import importlib
    from pathlib import Path as _Path

    from winmigrate.capture import CaptureReport

    stub_tkinter(monkeypatch)
    app = importlib.import_module("winmigrate.gui.app")

    wizard = object.__new__(app.WinMigrateWizard)
    wizard.capture_report = CaptureReport(
        bundle_path=_Path("b.dat"), manifest_path=_Path("b.manifest.json")
    )
    wizard.scan_result = None
    summary = app.WinMigrateWizard._done_summary(wizard)
    assert "b.dat" in summary and "b.manifest.json" in summary


# --- surviving being frozen ------------------------------------------------
def test_an_inherited_tcl_library_is_ignored_but_the_bundled_one_is_kept(
    monkeypatch, tmp_path: Path
):
    """Another Python on the machine can leave TCL_LIBRARY set system-wide, and
    Tcl believes it over anything the frozen build says. A perfectly good build
    then hunts for init.tcl in some other installation and reports that "Tcl
    wasn't installed properly" -- with the real files sitting right there.

    Only inherited values are dropped. PyInstaller's own point inside the
    application and are the reason it works at all.
    """
    import sys

    from winmigrate.gui import app

    monkeypatch.setenv("TCL_LIBRARY", "/some/other/python/lib/tcl8.6")

    # Not frozen: not our business, and removing it could break a dev machine.
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    app.scrub_tcl_environment()
    assert os.environ["TCL_LIBRARY"] == "/some/other/python/lib/tcl8.6"

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "WinMigrate.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    app.scrub_tcl_environment()
    assert "TCL_LIBRARY" not in os.environ

    monkeypatch.setenv("TCL_LIBRARY", str(tmp_path / "_tcl_data"))
    app.scrub_tcl_environment()
    assert os.environ["TCL_LIBRARY"] == str(tmp_path / "_tcl_data")


def test_a_tcl_failure_is_explained_rather_than_crashing(monkeypatch):
    """--windowed has no console, so a raw TclError is either silence or
    PyInstaller's crash dialog quoting search paths. Neither tells someone that
    the fix is to extract the folder."""
    import importlib
    import sys
    import types

    said: list[str] = []

    stub = types.ModuleType("tkinter")

    class TclError(Exception):
        pass

    def explode():
        raise TclError("Can't find a usable init.tcl")

    stub.TclError = TclError
    stub.Tk = explode
    stub.TkVersion = 8.6
    monkeypatch.setitem(sys.modules, "tkinter", stub)
    for name in ("tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    stub.ttk = sys.modules["tkinter.ttk"]

    app = importlib.import_module("winmigrate.gui.app")
    monkeypatch.setattr(app, "fatal", said.append)

    assert app.run({}) == 2
    assert len(said) == 1
    message = said[0]
    assert "extract" in message.lower() and ".zip" in message.lower()
    assert "winmigrate-cli" in message.lower()  # the way out that still works
    assert "init.tcl" in message  # the actual error, not swallowed


def test_the_frozen_entry_point_reports_anything_that_escapes():
    """A windowed build that dies silently is indistinguishable from one that
    was never launched."""
    root = Path(__file__).resolve().parent.parent
    source = (root / "gui_entry.py").read_text(encoding="utf-8")
    assert "freeze_support()" in source
    assert "MessageBoxW" in source
    assert "except BaseException" in source
    assert "extract" in source.lower()
