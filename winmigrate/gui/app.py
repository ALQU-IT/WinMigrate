"""The window: a setup wizard over the same pipeline the command line drives.

One page at a time, in the order every Windows installer has trained people to
expect. The rules about which page follows which, and when the forward button
works, live in :mod:`winmigrate.gui.wizard`; the palette lives in
:mod:`winmigrate.gui.theme`; what a bundle contains lives in the scan and
capture modules. What is left here is widgets and wiring, which is the only part
that genuinely needs a display.

Three things this window does that a console does not have to think about:

* **The scan starts by itself.** Someone who opened a backup tool wants to know
  what is on the machine, and making them press a button to find out is a step
  that exists only because it was easy to write.
* **Elevation is asked for once, at the start.** A shadow copy needs
  administrator rights, and elevation restarts the process -- so the prompt has
  to come before the scan, not after, or minutes of work are thrown away.
* **Slow work runs off the UI thread.** A scan takes minutes and a capture an
  hour. On the main thread Windows paints a frozen "not responding" window over
  a tool whose entire premise is showing what it is doing.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from .. import capture as capture_mod
from ..capture import CaptureOptions
from ..config import ScanConfig
from ..manifest import detect_source_machine
from ..models import ScanResult, Severity
from ..platform_win import Environment
from ..scan import run_scan
from ..util import humanize, paths as pathutil
from . import defaults, elevate, runlog, selection, theme
from .wizard import Mode, PasswordExport, Step, WizardData

log = logging.getLogger(__name__)

TICKED = "☑"
UNTICKED = "☐"
BLOCKED = "–"


def fatal(message: str) -> None:
    """Say something the user will actually see.

    A ``--windowed`` build has no console, so ``print`` goes nowhere and a
    traceback disappears entirely. MessageBoxW is Win32, needs no Tcl, and
    therefore still works when the reason for the message is that Tcl does not.
    """
    log.error("%s", message)
    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415

            # MB_ICONERROR | MB_OK
            ctypes.windll.user32.MessageBoxW(None, message, "WinMigrate", 0x10)
            return
        except Exception:  # noqa: BLE001 -- fall through to stdout
            pass
    print(message)


def scrub_tcl_environment() -> None:
    """Drop TCL_LIBRARY and TK_LIBRARY when they point outside this program.

    Another Python on the machine can leave these set system-wide, and Tcl
    believes them over anything the frozen build says -- so a perfectly good
    build looks for init.tcl in some other installation's folder and reports
    that "Tcl wasn't installed properly". Only inherited values are removed;
    the ones PyInstaller sets point inside the application and are what makes
    it work.
    """
    if not getattr(sys, "frozen", False):
        return
    here = Path(sys.executable).resolve().parent
    bundled = Path(getattr(sys, "_MEIPASS", here)).resolve()
    for name in ("TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH"):
        value = os.environ.get(name)
        if not value:
            continue
        try:
            resolved = Path(value).resolve()
        except (OSError, ValueError):
            os.environ.pop(name, None)
            continue
        if not (resolved.is_relative_to(bundled) or resolved.is_relative_to(here)):
            log.info("ignoring inherited %s=%s", name, value)
            os.environ.pop(name, None)


TCL_ADVICE = (
    "WinMigrate could not start its window because Tcl/Tk, the toolkit it draws "
    "with, could not be loaded.\n\n"
    "If you are running WinMigrate.exe from inside a .zip, extract the whole "
    "folder to a real location first and run it from there. The program needs "
    "the files that sit next to it.\n\n"
    "The command-line version does not use Tcl/Tk and will work either way:\n"
    "    winmigrate-cli.exe scan\n\n"
    "Details: {error}"
)


def run(options: dict | None = None) -> int:
    """Open the window. ``options`` carries the first page's choices across an
    elevation restart. Returns a process exit code."""
    options = dict(options or {})
    scrub_tcl_environment()
    # Before the window, before tkinter: the failures worth having a log for
    # include the one where no window appears at all.
    if options.get("log_file"):
        # winmigrate gui --log-file: the command line configured it already.
        log_path = Path(options["log_file"])
    else:
        log_path = runlog.begin(
            "window",
            {"elevation attempted": "yes" if options.get("elevation_attempted") else "no"},
        )
    if log_path is not None:
        options["log_path"] = str(log_path)
    try:
        import tkinter as tk  # noqa: PLC0415
    except ImportError as exc:
        fatal(
            "The graphical interface needs tkinter, which is missing from this "
            "Python installation. Use the command line instead: winmigrate --help"
            f"\n\nDetails: {exc}"
        )
        return 2
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        # The failure this build is shaped to avoid, reported in words rather
        # than as a PyInstaller crash dialog quoting search paths.
        fatal(TCL_ADVICE.format(error=exc))
        return 2
    WinMigrateWizard(root, options)
    root.mainloop()
    return 0


class WinMigrateWizard:
    """The window, its pages, and the two background workers."""

    def __init__(self, root: Any, options: dict) -> None:
        import tkinter as tk  # noqa: PLC0415
        from tkinter import ttk  # noqa: PLC0415

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.options = options

        self.data = WizardData(profile_root=options.get("profile_root") or str(Path.home()))
        self.step = Step.CHOOSE
        self.manifest: dict | None = None
        self.restore_report: Any = None
        self.scan_result: ScanResult | None = None
        self.capture_report: Any = None
        self.verify_report: Any = None
        self.verify_failure: str = ""
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.elevation_attempted = bool(options.get("elevation_attempted"))
        self.log_path = Path(options["log_path"]) if options.get("log_path") else None
        self._capture_total = 1
        self._capture_done = 0

        root.title("WinMigrate")
        root.geometry(f"{theme.WINDOW_WIDTH}x{theme.WINDOW_HEIGHT}")
        root.minsize(820, 560)

        self.family = theme.font_family()
        # Windows keeps the light/dark choice in the registry; matching it is
        # the difference between a tool that belongs on the desktop and one that
        # flashes white at someone working in the dark.
        self.dark = theme.detect_dark_mode()
        self.palette = theme.palette_for(self.dark)
        self.style = ttk.Style(root)
        theme.apply(self.style, self.family, self.palette)
        root.configure(background=self.palette.page)
        log.info("theme: %s", "dark" if self.dark else "light")

        self._build_chrome()
        self._build_pages()
        self._show(Step.CHOOSE)
        self.root.after(80, self._drain_events)

    # --- the frame around every page ---------------------------------------
    def _build_chrome(self) -> None:
        ttk = self.ttk

        body = ttk.Frame(self.root, style="Page.TFrame")
        body.pack(fill="both", expand=True)

        self.rail = ttk.Frame(body, style="Rail.TFrame", width=theme.RAIL_WIDTH)
        self.rail.pack(side="left", fill="y")
        self.rail.pack_propagate(False)
        ttk.Label(self.rail, text="WinMigrate", style="RailTitle.TLabel").pack(
            anchor="w", padx=18, pady=(22, 2)
        )
        ttk.Label(self.rail, text="profile backup", style="RailOff.TLabel").pack(
            anchor="w", padx=18, pady=(0, 18)
        )
        self.rail_labels: list[Any] = []
        from .wizard import RAIL_LABELS, progress_steps

        for entry in progress_steps():
            label = ttk.Label(
                self.rail, text=f"  {RAIL_LABELS[entry]}", style=theme.RAIL_OFF
            )
            label.pack(anchor="w", padx=16, pady=4)
            self.rail_labels.append(label)

        # Pinned to the bottom of the rail rather than mentioned once at the
        # end: the moment someone wants the log is the moment something looks
        # wrong, which is in the middle, not after.
        self.log_label = ttk.Label(
            self.rail,
            text=self._log_note(),
            style="RailOff.TLabel",
            wraplength=theme.RAIL_WIDTH - 32,
            justify="left",
        )
        self.log_label.pack(side="bottom", anchor="w", padx=16, pady=(0, 16))

        right = ttk.Frame(body, style="Page.TFrame")
        right.pack(side="left", fill="both", expand=True)

        head = ttk.Frame(right, style="Page.TFrame")
        head.pack(fill="x", padx=theme.PAD, pady=(24, 0))
        self.title_label = ttk.Label(head, text="", style="Title.TLabel")
        self.title_label.pack(anchor="w")
        self.subtitle_label = ttk.Label(
            head, text="", style="Subtitle.TLabel", wraplength=640, justify="left"
        )
        self.subtitle_label.pack(anchor="w", pady=(6, 0))
        ttk.Separator(right, orient="horizontal").pack(fill="x", padx=theme.PAD, pady=16)

        self.page_area = ttk.Frame(right, style="Page.TFrame")
        self.page_area.pack(fill="both", expand=True, padx=theme.PAD)

        footer = ttk.Frame(self.root, style="Band.TFrame")
        footer.pack(fill="x", side="bottom")
        ttk.Separator(footer, orient="horizontal").pack(fill="x")
        inner = ttk.Frame(footer, style="Band.TFrame")
        inner.pack(fill="x", padx=theme.PAD, pady=12)
        self.hint = ttk.Label(inner, text="", style="BandHint.TLabel")
        self.hint.pack(side="left")
        self.next_button = self.ttk.Button(
            inner, text="Next", style="Wizard.TButton", command=self._go_next
        )
        self.next_button.pack(side="right")
        self.back_button = self.ttk.Button(
            inner, text="Back", style="Wizard.TButton", command=self._go_back
        )
        self.back_button.pack(side="right", padx=(0, 8))
        self.cancel_button = self.ttk.Button(
            inner, text="Cancel", style="Wizard.TButton", command=self._cancel
        )
        self.cancel_button.pack(side="right", padx=(0, 8))

    def _log_note(self) -> str:
        """One line for the rail: the log's name, not its whole path.

        The rail is narrow and the folder is one the user is already standing
        in -- it is beside the program. The full path goes in the log itself and
        on the last page, where there is room for it.
        """
        if self.log_path is None:
            return ""
        return f"log\n{self.log_path.name}"

    # --- pages -------------------------------------------------------------
    def _build_pages(self) -> None:
        self.pages: dict[Step, Any] = {}
        for step, builder in (
            (Step.CHOOSE, self._page_choose),
            (Step.SOURCE, self._page_source),
            (Step.OPENING, self._page_opening),
            (Step.RESTORE_SELECT, self._page_restore_select),
            (Step.RESTORE_CONFIRM, self._page_restore_confirm),
            (Step.RESTORING, self._page_restoring),
            (Step.RESTORE_DONE, self._page_restore_done),
            (Step.WELCOME, self._page_welcome),
            (Step.SCANNING, self._page_scanning),
            (Step.SELECT, self._page_select),
            (Step.PASSWORDS, self._page_passwords),
            (Step.DESTINATION, self._page_destination),
            (Step.CONFIRM, self._page_confirm),
            (Step.WORKING, self._page_working),
            (Step.DONE, self._page_done),
        ):
            frame = self.ttk.Frame(self.page_area, style="Page.TFrame")
            builder(frame)
            self.pages[step] = frame

    def _page_choose(self, page: Any) -> None:
        tk, ttk = self.tk, self.ttk
        # A backup sitting in the same folder as the program is a strong signal
        # about why someone is here: they have carried a USB drive to the new
        # machine and plugged it in. Defaulting to backup in that situation is
        # the wrong guess, and the wrong guess is the one that writes files.
        self.found_bundles = defaults.bundles_beside_program()
        opening = Mode.RESTORE.value if self.found_bundles else Mode.BACKUP.value
        self.mode_var = tk.StringVar(value=opening)
        for value, heading, blurb in (
            (
                Mode.BACKUP.value,
                "Back up this machine",
                "Look through this profile and copy what you choose into one "
                "encrypted file. Nothing on this machine is changed.",
            ),
            (
                Mode.RESTORE.value,
                "Restore a backup onto this machine",
                "Open a backup made elsewhere and put its files here. Files "
                "already here are kept unless you say otherwise.",
            ),
        ):
            ttk.Radiobutton(
                page,
                text=heading,
                value=value,
                variable=self.mode_var,
                style="Wizard.TRadiobutton",
                command=self._refresh_buttons,
            ).pack(anchor="w", pady=(14, 0))
            ttk.Label(page, text="     " + blurb, style="Hint.TLabel", wraplength=600,
                      justify="left").pack(anchor="w")
            if value == Mode.RESTORE.value and self.found_bundles:
                found = defaults.describe_bundle_file(self.found_bundles[0])
                extra = (
                    f" (and {len(self.found_bundles) - 1} more)"
                    if len(self.found_bundles) > 1 else ""
                )
                ttk.Label(
                    page,
                    text=f"     Found here: {found}{extra}",
                    style="Good.TLabel",
                    wraplength=600,
                    justify="left",
                ).pack(anchor="w")

    def _page_source(self, page: Any) -> None:
        tk, ttk = self.tk, self.ttk
        ttk.Label(page, text="Backup file", style="Body.TLabel").pack(anchor="w")
        row = ttk.Frame(page, style="Page.TFrame")
        row.pack(fill="x", pady=(4, 2))
        self.bundle_var = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.bundle_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self._pick_bundle).pack(side="left", padx=(8, 0))
        self.bundle_summary = ttk.Label(
            page, text="", style="Hint.TLabel", wraplength=620, justify="left"
        )
        self.bundle_summary.pack(anchor="w", pady=(6, 18))

        ttk.Label(page, text="Passphrase", style="Body.TLabel").pack(anchor="w")
        self.bundle_passphrase = ttk.Entry(page, show="•")
        self.bundle_passphrase.pack(fill="x", pady=(4, 6))
        self.bundle_passphrase.bind("<KeyRelease>", lambda _e: self._refresh_buttons())
        ttk.Label(
            page,
            text="Used only to open the backup and read what is in it. Nothing is "
            "written to this machine until you have chosen what to put back.",
            style="Hint.TLabel",
            wraplength=620,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

    def _page_opening(self, page: Any) -> None:
        ttk = self.ttk
        self.open_bar = ttk.Progressbar(page, mode="indeterminate")
        self.open_bar.pack(fill="x", pady=(30, 14))
        self.open_status = ttk.Label(page, text="Checking the file…", style="Body.TLabel")
        self.open_status.pack(anchor="w")

    def _page_restore_select(self, page: Any) -> None:
        ttk = self.ttk
        holder = ttk.Frame(page, style="Page.TFrame")
        holder.pack(fill="both", expand=True)
        columns = ("pick", "title", "kind", "size", "files", "note")
        self.restore_tree = ttk.Treeview(
            holder, columns=columns, show="headings", selectmode="none",
            style="Wizard.Treeview",
        )
        for name, heading, width, anchor, stretch in (
            ("pick", "", 36, "center", False),
            ("title", "Item", 300, "w", True),
            ("kind", "Kind", 120, "w", False),
            ("size", "Size", 90, "e", False),
            ("files", "Files", 80, "e", False),
            ("note", "", 200, "w", True),
        ):
            self.restore_tree.heading(name, text=heading)
            self.restore_tree.column(name, width=width, anchor=anchor, stretch=stretch)
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.restore_tree.yview)
        self.restore_tree.configure(yscrollcommand=bar.set)
        self.restore_tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.restore_tree.tag_configure("secret", foreground=self.palette.secret)
        self.restore_tree.bind("<Button-1>", self._on_restore_tree_click)

        row = ttk.Frame(page, style="Page.TFrame")
        row.pack(fill="x", pady=(10, 4))
        ttk.Button(row, text="Select all", command=lambda: self._set_all_restore(True)).pack(
            side="left"
        )
        ttk.Button(row, text="Select none", command=lambda: self._set_all_restore(False)).pack(
            side="left", padx=8
        )
        self.restore_total = self.ttk.Label(row, text="", style="Body.TLabel")
        self.restore_total.pack(side="right")

    def _page_restore_confirm(self, page: Any) -> None:
        tk, ttk = self.tk, self.ttk
        ttk.Label(page, text="Put the files into", style="Body.TLabel").pack(anchor="w")
        row = ttk.Frame(page, style="Page.TFrame")
        row.pack(fill="x", pady=(4, 2))
        self.destination_var = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.destination_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(row, text="Change…", command=self._pick_destination).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(
            page,
            text="Defaults to this machine's own profile, which is where a migration "
            "goes. Point it somewhere else to unpack a backup without touching your "
            "own files.",
            style="Hint.TLabel",
            wraplength=620,
            justify="left",
        ).pack(anchor="w", pady=(0, 16))

        self.dry_run_var = tk.BooleanVar(value=False)
        self.overwrite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            page, text="Practice run — report what would happen, write nothing",
            variable=self.dry_run_var, style="Wizard.TCheckbutton",
            command=self._refresh_restore_summary,
        ).pack(anchor="w")
        ttk.Checkbutton(
            page, text="Replace files that are already here and differ",
            variable=self.overwrite_var, style="Wizard.TCheckbutton",
            command=self._refresh_restore_summary,
        ).pack(anchor="w", pady=(6, 0))
        # A restore changes more than files, and the window was not saying so.
        # The command line has always had --no-apply-settings; this is the same
        # choice, made where the person can see what it covers.
        self.apply_settings_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            page,
            text="Also put back settings that need no sign-in",
            variable=self.apply_settings_var, style="Wizard.TCheckbutton",
            command=self._refresh_restore_summary,
        ).pack(anchor="w", pady=(6, 0))
        ttk.Label(
            page,
            text="     Wi-Fi networks, printers, mapped drives and your own "
            "environment variables. Nothing that needs a password or a licence: "
            "those stay on the list at the end for you to do.",
            style="Hint.TLabel",
            wraplength=620,
            justify="left",
        ).pack(anchor="w")
        ttk.Label(
            page,
            text="     Off, your own version of a file is kept and the backup's copy "
            "is left out. Identical files are skipped either way.",
            style="Hint.TLabel",
            wraplength=620,
            justify="left",
        ).pack(anchor="w")

        self.restore_summary = ttk.Label(
            page, text="", style="Body.TLabel", justify="left", wraplength=640
        )
        self.restore_summary.pack(anchor="w", pady=(18, 0))

    def _page_restoring(self, page: Any) -> None:
        ttk = self.ttk
        self.restore_bar = ttk.Progressbar(page, mode="determinate", maximum=1000)
        self.restore_bar.pack(fill="x", pady=(30, 14))
        self.restore_status = ttk.Label(page, text="Starting…", style="Body.TLabel")
        self.restore_status.pack(anchor="w")
        self.restore_detail = ttk.Label(page, text="", style="Hint.TLabel", wraplength=640)
        self.restore_detail.pack(anchor="w", pady=(6, 0))

    def _page_restore_done(self, page: Any) -> None:
        ttk = self.ttk
        self.restore_done_text = ttk.Label(
            page, text="", style="Body.TLabel", justify="left", wraplength=640
        )
        self.restore_done_text.pack(anchor="w", pady=(6, 8))
        self.reinstall_button = ttk.Button(
            page, text="Open the reinstall folder…", command=self._open_reinstall_folder
        )
        holder = ttk.Frame(page, style="Page.TFrame")
        holder.pack(fill="both", expand=True)
        self.followup_holder = holder
        self.followup_box = self.tk.Text(
            holder, height=10, wrap="word", relief="flat", background=self.palette.page,
            foreground=self.palette.ink, borderwidth=0, highlightthickness=1,
            highlightbackground=self.palette.rule,
        )
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.followup_box.yview)
        self.followup_box.configure(yscrollcommand=bar.set)
        self.followup_box.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

    def _page_welcome(self, page: Any) -> None:
        tk, ttk = self.tk, self.ttk
        ttk.Label(page, text="Profile to back up", style="Body.TLabel").pack(anchor="w")
        row = ttk.Frame(page, style="Page.TFrame")
        row.pack(fill="x", pady=(4, 2))
        self.profile_var = tk.StringVar(value=self.data.profile_root)
        ttk.Entry(row, textvariable=self.profile_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(row, text="Change…", command=self._pick_profile).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(
            page,
            text="Detected automatically. Change it only to back up a different account.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(0, 18))

        ttk.Label(page, text="Options", style="Body.TLabel").pack(anchor="w")
        options = ttk.Frame(page, style="Page.TFrame")
        options.pack(fill="x", pady=(6, 0))

        self.use_vss = tk.BooleanVar(value=self.options.get("use_vss", True))
        self.files_only = tk.BooleanVar(value=self.options.get("files_only", False))
        self.include_wifi = tk.BooleanVar(value=self.options.get("include_wifi", False))
        self.include_software = tk.BooleanVar(value=self.options.get("include_software", True))
        self.include_notepad = tk.BooleanVar(value=self.options.get("include_notepad", True))

        for text, var, hint in (
            (
                "Copy files that programs are using (recommended)",
                self.use_vss,
                "Uses a shadow copy, which needs administrator rights. Windows will ask.",
            ),
            (
                "Include the software list",
                self.include_software,
                "What is installed, so it can be reinstalled on the new machine.",
            ),
            (
                "Include Wi-Fi networks",
                self.include_wifi,
                "Their saved passwords travel with them, encrypted.",
            ),
            (
                "Files only — leave all credentials behind",
                self.files_only,
                "No browser profiles, no keys, no saved passwords of any kind.",
            ),
        ):
            ttk.Checkbutton(
                page, text=text, variable=var, style="Wizard.TCheckbutton",
                command=self._refresh_buttons,
            ).pack(anchor="w", pady=(8, 0))
            ttk.Label(page, text="     " + hint, style="Hint.TLabel").pack(anchor="w")

        self.elevation_note = ttk.Label(page, text="", style="Warn.TLabel", wraplength=620)
        self.elevation_note.pack(anchor="w", pady=(16, 0))

    def _page_scanning(self, page: Any) -> None:
        ttk = self.ttk
        self.scan_bar = ttk.Progressbar(page, mode="indeterminate")
        self.scan_bar.pack(fill="x", pady=(30, 14))
        self.scan_status = ttk.Label(page, text="Starting…", style="Body.TLabel")
        self.scan_status.pack(anchor="w")
        ttk.Label(
            page,
            text="Large folders take a moment. Nothing is written during this step.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(6, 0))

    def _page_select(self, page: Any) -> None:
        ttk = self.ttk
        holder = ttk.Frame(page, style="Page.TFrame")
        holder.pack(fill="both", expand=True)
        columns = ("pick", "title", "kind", "size", "files", "note")
        self.tree = ttk.Treeview(
            holder, columns=columns, show="headings", selectmode="none",
            style="Wizard.Treeview",
        )
        for name, heading, width, anchor, stretch in (
            ("pick", "", 36, "center", False),
            ("title", "Item", 300, "w", True),
            ("kind", "Kind", 120, "w", False),
            ("size", "Size", 90, "e", False),
            ("files", "Files", 80, "e", False),
            ("note", "", 200, "w", True),
        ):
            self.tree.heading(name, text=heading)
            self.tree.column(name, width=width, anchor=anchor, stretch=stretch)
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.tag_configure("secret", foreground=self.palette.secret)
        self.tree.tag_configure("blocked", foreground=self.palette.ink_faint)
        self.tree.bind("<Button-1>", self._on_tree_click)

        row = ttk.Frame(page, style="Page.TFrame")
        row.pack(fill="x", pady=(10, 4))
        ttk.Button(row, text="Select all", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(row, text="Select none", command=lambda: self._set_all(False)).pack(
            side="left", padx=8
        )
        self.total_label = ttk.Label(row, text="", style="Body.TLabel")
        self.total_label.pack(side="right")

    def _page_passwords(self, page: Any) -> None:
        """The browser password handoff, which only the command line had.

        The rule this page exists to keep: WinMigrate never reads a password
        store and never touches DPAPI. The browser does its own export, behind
        its own Windows Hello prompt, and all this does is open the right page,
        take the file the user saved, and put it in the encrypted bundle. The
        tool never sees the OS credential and never decrypts anything.
        """
        tk, ttk = self.tk, self.ttk
        self.passwords_intro = ttk.Label(
            page, text="", style="Body.TLabel", justify="left", wraplength=640
        )
        self.passwords_intro.pack(anchor="w", pady=(0, 12))

        self.passwords_area = ttk.Frame(page, style="Page.TFrame")
        self.passwords_area.pack(fill="x")

        self.passwords_cloud = ttk.Label(
            page, text="", style="Hint.TLabel", justify="left", wraplength=640
        )
        self.passwords_cloud.pack(anchor="w", pady=(12, 0))

        self.shred_after = tk.BooleanVar(value=True)
        self.shred_check = ttk.Checkbutton(
            page,
            text="Delete the exported file(s) from this machine once the backup is written",
            variable=self.shred_after,
            style="Wizard.TCheckbutton",
        )
        self.shred_hint = ttk.Label(
            page,
            text="     Recommended: what the browser wrote is plaintext. The copy "
            "inside the backup is encrypted.",
            style="Hint.TLabel",
        )
        # Both are packed only once there is a file to delete.
        self.password_rows: dict[str, Any] = {}
        self.password_status: dict[str, Any] = {}
        self.password_targets: list = []
        self.password_cloud_accounts: list = []
        self.password_targets_by_key: dict[str, Any] = {}
        #: Looked up once. Coming back to this page must not re-read every
        #: browser's preferences, and must not lose what has already been added.
        self.password_state_known = False

    def _render_passwords(self) -> None:
        """Say something true about every browser found, not only the ones
        with work to do.

        Three states, and the page shows whichever applies: passwords saved
        locally (export them), passwords already synced (nothing to do, sign in
        on the new machine), and files-only mode (no credential material travels
        at all). A page that only ever listed the first would look broken on the
        machines where there is nothing to list.
        """
        ttk = self.ttk
        for frame in self.password_rows.values():
            frame.destroy()
        self.password_rows.clear()
        self.password_status.clear()

        if self.files_only.get():
            self.passwords_intro.configure(
                text="Files-only mode: no credential material of any kind travels in "
                "this backup, so there is nothing to export here."
            )
            self.passwords_cloud.configure(text="")
            return

        if not self.password_state_known:
            self._find_password_targets()

        cloud = self.password_cloud_accounts
        if self.password_targets:
            self.passwords_intro.configure(
                text="These browsers have passwords saved on this machine rather "
                "than in an account. Export them from the browser itself — it will "
                "ask for Windows Hello — and hand the file to WinMigrate, which "
                "encrypts it into the backup. This is optional; you can skip it."
            )
        elif cloud:
            self.passwords_intro.configure(
                text="Nothing to do here: every browser found keeps its passwords "
                "in an account, so they come back when you sign in on the new machine."
            )
        else:
            self.passwords_intro.configure(
                text="No browser with passwords saved on this machine was found, so "
                "there is nothing to export."
            )

        for target in self.password_targets:
            frame = ttk.Frame(self.passwords_area, style="Page.TFrame")
            frame.pack(fill="x", pady=(0, 14))
            ttk.Label(
                frame, text=f"{target.label} — saved on this machine", style="Body.TLabel"
            ).pack(anchor="w")
            ttk.Label(
                frame,
                text=f"     Export page: {target.export_page or 'the browser password manager'}",
                style="Hint.TLabel",
            ).pack(anchor="w")
            buttons = ttk.Frame(frame, style="Page.TFrame")
            buttons.pack(anchor="w", pady=(6, 0))
            key = target.key
            ttk.Button(
                buttons,
                text=f"Open {target.label}",
                command=lambda k=key: self._open_export_page(k),
            ).pack(side="left")
            ttk.Button(
                buttons,
                text="Choose the exported file…",
                command=lambda k=key: self._pick_password_csv(k),
            ).pack(side="left", padx=(8, 0))
            status = ttk.Label(frame, text="", style="Hint.TLabel", wraplength=620)
            status.pack(anchor="w", pady=(6, 0))
            self.password_rows[key] = frame
            self.password_status[key] = status
            added = self.data.passwords_added.get(key)
            if added is not None:
                status.configure(
                    text=f"Added — {added.label} passwords travel encrypted only "
                    f"(from {added.csv_path.name})."
                )

        self.passwords_cloud.configure(
            text=(
                "Already in the cloud: "
                + "; ".join(
                    f"{account.label}"
                    + (f" (sign in as {account.account_email})" if account.account_email else "")
                    for account in cloud
                )
                + "."
                if cloud
                else ""
            )
        )
        self._refresh_shred_box()

    def _refresh_shred_box(self) -> None:
        if self.data.passwords_added:
            self.shred_check.pack(anchor="w", pady=(16, 0))
            self.shred_hint.pack(anchor="w")
        else:
            self.shred_check.pack_forget()
            self.shred_hint.pack_forget()

    def _find_password_targets(self) -> None:
        """Which browsers have local passwords, and which are already synced.

        Read-only, and only configuration: the sign-in state comes out of the
        browser's own preferences files. No password store is opened.
        """
        from .. import passwords as passwords_mod  # noqa: PLC0415

        try:
            env = self._environment(self._config())
            self.password_targets = passwords_mod.export_targets(env)
            self.password_cloud_accounts = passwords_mod.synced_browsers(env)
        except Exception as exc:  # noqa: BLE001 -- a page, not the backup
            log.warning("could not work out the browser password state: %s", exc)
            self.password_targets = []
            self.password_cloud_accounts = []
        self.password_targets_by_key = {t.key: t for t in self.password_targets}
        self.password_state_known = True
        log.info(
            "browser passwords: %s local, %s already synced",
            len(self.password_targets),
            len(self.password_cloud_accounts),
        )

    def _open_export_page(self, key: str) -> None:
        """Open the browser on its own export page.

        An internal browser URL means nothing to the Windows shell, so the
        browser's own executable is launched with the address as an argument.
        When it cannot be found the address is shown instead -- a wrong dialog
        is worse than none.
        """
        from .. import passwords as passwords_mod  # noqa: PLC0415

        target = self.password_targets_by_key.get(key)
        status = self.password_status.get(key)
        if target is None or status is None:
            return
        opened = passwords_mod.open_export_page(target, self._environment(self._config()))
        direct = passwords_mod.opens_directly(target)
        copied = self._copy_address(target.export_page)
        log.info(
            "export page for %s: %s (%s)",
            key,
            "opened" if opened else "could not open",
            "on the page" if direct else f"at {passwords_mod.landing_page(target)}",
        )
        # The address goes on the clipboard in all three cases. A browser that
        # was already open can come to the front on the page it was last
        # showing, and a Chromium ignores an internal address handed to it from
        # outside — so being told to type "brave://password-manager/settings" by
        # hand is exactly the fiddling this button exists to remove.
        paste = " The address is on your clipboard." if copied else ""
        if not opened:
            where = (
                f"Could not start {target.label}. Open it yourself and go to "
                f"{target.export_page}."
            )
        elif direct:
            where = f"{target.label} should now be showing {target.export_page}."
        else:
            # Saying the browser is on a page it is not sends the user hunting
            # for a tab that was never opened. It is on its settings, because
            # that is the only one of its own pages Chromium will accept from
            # another program, and the last hop is theirs.
            where = (
                f"{target.label} is open at its settings — a browser will not let "
                f"another program open its password page. Paste {target.export_page} "
                "into the address bar, or find Passwords in settings."
            )
        status.configure(
            text=where
            + paste
            + " Use 'Export passwords' there — it will ask for Windows Hello — "
            "then choose the file you saved."
        )

    def _copy_address(self, address: str) -> bool:
        """Put the export page's address on the clipboard. True when it went.

        Nothing secret: it is a fixed page inside the browser, the same string
        already printed on screen.
        """
        if not address:
            return False
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(address)
            return True
        except Exception as exc:  # noqa: BLE001 -- a convenience, never a failure
            log.info("could not use the clipboard: %s", exc)
            return False

    def _pick_password_csv(self, key: str) -> None:
        from tkinter import filedialog  # noqa: PLC0415

        target = self.password_targets_by_key.get(key)
        if target is None:
            return
        chosen = filedialog.askopenfilename(
            title=f"The file {target.label} exported",
            filetypes=[("Password export (CSV)", "*.csv"), ("All files", "*.*")],
        )
        if not chosen:
            return
        self._ingest_password_csv(key, Path(chosen))

    def _ingest_password_csv(self, key: str, csv_path: Path) -> None:
        """Stage the user's export as encrypted-only material.

        The same :func:`winmigrate.passwords.ingest_csv` the command line uses,
        so there is one answer to what a password export is and where it lands.
        The item is SECRET: it exists only inside the encrypted payload and the
        plaintext sidecar carries a redacted stub.
        """
        from .. import passwords as passwords_mod  # noqa: PLC0415

        target = self.password_targets_by_key.get(key)
        status = self.password_status.get(key)
        if target is None or self.scan_result is None:
            return
        outcome = passwords_mod.ingest_csv(target, csv_path, self.scan_result)
        if status is not None:
            status.configure(text=outcome.message)
        # The browser and the outcome, never the path: it points at a plaintext
        # password file, and the log is written to the drive the backup is on.
        log.info("password export for %s: %s", key, "accepted" if outcome.ok else "refused")
        if not outcome.ok:
            return
        # Keyed by profile, so choosing a second file for the same one replaces
        # the first. The old record going away is the point: the file it names
        # is no longer in the bundle, and nothing may offer to delete it.
        self.data.passwords_added[key] = PasswordExport(
            key=key,
            label=target.label,
            item_id=outcome.item.id if outcome.item is not None else "",
            csv_path=csv_path,
        )
        # The item arrived after the choosing page was built. Without this it
        # would be marked "deselected" at capture time and silently left out --
        # the one item the user went furthest out of their way to include.
        self.data.rows = selection.rows_for(self.scan_result)
        if outcome.item is not None:
            self.data.selected.add(outcome.item.id)
        self._refresh_shred_box()
        self._refresh_buttons()

    def _shred_exported_csvs(self) -> list[str]:
        """Delete the plaintext exports, once the bundle really holds them.

        Deleting a file that did not reach the backup is the worst thing this
        page could do: what the browser wrote is the only copy, and the user
        exported it precisely because it is not in the cloud. So the box being
        ticked is not enough on its own. Three ways a staged export can fail to
        be in the bundle, all of which happened:

        * the user went back and scanned again, which builds a new plan the
          export was never added to (the records are dropped then, so this
          sees nothing);
        * they unticked the row on the choosing page, leaving the item in the
          plan but marked as skipped;
        * the capture could not read the file.

        Each is checked against what the capture actually did, not against what
        was asked for.
        """
        from ..models import Action  # noqa: PLC0415
        from .. import passwords as passwords_mod  # noqa: PLC0415

        if not self.data.passwords_added or not self.shred_after.get():
            return []
        captured = {
            item.id
            for item in (self.scan_result.items if self.scan_result else [])
            if item.action is Action.CAPTURE
        }
        failed = {
            pathutil.normalize_key(Path(path)) for path, _reason in
            (self.capture_report.failures if self.capture_report else [])
        }
        done: list[str] = []
        seen: set[str] = set()
        for export in self.data.passwords_added.values():
            path = export.csv_path
            key = pathutil.normalize_key(path)
            if key in seen:
                continue
            seen.add(key)
            if export.item_id not in captured or key in failed:
                log.info("not shredding %s: it is not in the backup", path.name)
                done.append(f"{path.name} is not in the backup, so it was left alone")
                continue
            ok = passwords_mod.shred(path)
            log.info("shred %s: %s", path.name, "done" if ok else "failed")
            done.append(f"{path.name} deleted" if ok else f"{path.name} could not be deleted")
        return done

    def _page_destination(self, page: Any) -> None:
        tk, ttk = self.tk, self.ttk
        ttk.Label(page, text="Save the backup as", style="Body.TLabel").pack(anchor="w")
        row = ttk.Frame(page, style="Page.TFrame")
        row.pack(fill="x", pady=(4, 2))
        self.output_var = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.output_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Change…", command=self._pick_output).pack(
            side="left", padx=(8, 0)
        )
        self.output_hint = ttk.Label(page, text="", style="Hint.TLabel", wraplength=620)
        self.output_hint.pack(anchor="w", pady=(0, 20))
        # Shown only when a file of that name is already there. A backup is the
        # one kind of file whose whole purpose is being there when something
        # else is not, so replacing one is a decision, not a default.
        self.replace_var = tk.BooleanVar(value=False)
        self.replace_check = ttk.Checkbutton(
            page,
            text="Replace the backup that is already there",
            variable=self.replace_var,
            style="Wizard.TCheckbutton",
            command=self._refresh_buttons,
        )

        ttk.Label(page, text="Passphrase", style="Body.TLabel").pack(anchor="w")
        self.passphrase = ttk.Entry(page, show="•")
        self.passphrase.pack(fill="x", pady=(4, 10))
        self.passphrase.bind("<KeyRelease>", lambda _e: self._refresh_buttons())
        ttk.Label(page, text="Type it again", style="Body.TLabel").pack(anchor="w")
        self.passphrase2 = ttk.Entry(page, show="•")
        self.passphrase2.pack(fill="x", pady=(4, 6))
        self.passphrase2.bind("<KeyRelease>", lambda _e: self._refresh_buttons())
        ttk.Label(
            page,
            text="This passphrase is the only way back into the backup. Nobody can "
            "recover it for you — not us, not Microsoft. Write it down somewhere "
            "that is not the machine you are replacing.",
            style="Warn.TLabel",
            wraplength=620,
            justify="left",
        ).pack(anchor="w", pady=(10, 0))

        # The command line has --verify and the window had nothing: after an
        # hour-long backup there was no way to find out whether it opens,
        # short of running the console tool. The question it answers is the
        # one asked just before a machine is wiped.
        self.verify_after = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            page,
            text="Check the backup afterwards",
            variable=self.verify_after,
            style="Wizard.TCheckbutton",
        ).pack(anchor="w", pady=(16, 0))
        ttk.Label(
            page,
            text="     Reads the whole backup back and compares every file against "
            "what was recorded. It takes about as long again as writing it, and it "
            "is what earns the right to wipe this machine.",
            style="Hint.TLabel",
            wraplength=620,
            justify="left",
        ).pack(anchor="w")

    def _page_confirm(self, page: Any) -> None:
        self.confirm_text = self.ttk.Label(
            page, text="", style="Body.TLabel", justify="left", wraplength=640
        )
        self.confirm_text.pack(anchor="w", pady=(6, 0))

    def _page_working(self, page: Any) -> None:
        ttk = self.ttk
        self.capture_bar = ttk.Progressbar(page, mode="determinate", maximum=1000)
        self.capture_bar.pack(fill="x", pady=(30, 14))
        self.capture_status = ttk.Label(page, text="Starting…", style="Body.TLabel")
        self.capture_status.pack(anchor="w")
        self.capture_detail = ttk.Label(page, text="", style="Hint.TLabel", wraplength=640)
        self.capture_detail.pack(anchor="w", pady=(6, 0))

    def _page_done(self, page: Any) -> None:
        self.done_text = self.ttk.Label(
            page, text="", style="Body.TLabel", justify="left", wraplength=640
        )
        self.done_text.pack(anchor="w", pady=(6, 0))
        self.open_button = self.ttk.Button(
            page, text="Open the folder", command=self._open_output_folder
        )
        self.open_button.pack(anchor="w", pady=(18, 0))

    # --- navigation --------------------------------------------------------
    def _show(self, step: Step) -> None:
        from . import wizard

        # The log's spine: every later line is read against the page that was
        # on screen when it was written.
        log.info("page: %s", getattr(step, "value", step))

        # The mode lives in a widget until it is collected, and the rail, the
        # buttons and the validation all depend on it. _refresh_buttons collects
        # and repaints at the end of this method, which is what actually keeps
        # the rail honest; doing it here as well means nothing in between reads
        # a stale mode.
        self._collect()
        for frame in self.pages.values():
            frame.pack_forget()
        self.step = step
        heading, subtitle = wizard.title(step)
        self.title_label.configure(text=heading)
        self.subtitle_label.configure(text=subtitle)
        self.pages[step].pack(fill="both", expand=True)

        self._paint_rail()
        self._on_enter(step)
        self._refresh_buttons()

    def _paint_rail(self) -> None:
        """Draw the rail for the job being done and the page showing.

        Label and marker together, in one pass. They were two passes reading the
        mode at different moments, which is how the rail ended up describing one
        job while the window was doing the other.

        Backing up and restoring have a different number of stages, so the
        entries are rewritten rather than one rail being made to describe both
        badly, and the spare entry is hidden rather than left showing a step
        that will never arrive.
        """
        from .wizard import RAIL_LABELS, progress_steps, rail_index

        entries = progress_steps(self.data.mode)
        current = rail_index(self.step, self.data.mode)
        for index, label in enumerate(self.rail_labels):
            if index >= len(entries):
                label.pack_forget()
                continue
            marker = theme.rail_marker(index, current)
            label.configure(
                text=f"{marker}  {RAIL_LABELS[entries[index]]}",
                style=theme.rail_style(index, current),
            )
            label.pack(anchor="w", padx=16, pady=4)

    def _on_enter(self, step: Step) -> None:
        if step is Step.SOURCE:
            if not self.bundle_var.get() and getattr(self, "found_bundles", None):
                self.bundle_var.set(str(self.found_bundles[0]))
            self._describe_bundle()
        elif step is Step.OPENING:
            self.open_bar.start(14)
            self._start_open()
        elif step is Step.RESTORE_SELECT:
            self._render_restore_rows()
        elif step is Step.RESTORE_CONFIRM:
            if not self.destination_var.get():
                self.destination_var.set(str(Path.home()))
            self._refresh_restore_summary()
        elif step is Step.RESTORING:
            self._start_restore()
        elif step is Step.RESTORE_DONE:
            self._render_restore_done()
            self._forget_passphrases()
        elif step is Step.WELCOME:
            self._update_elevation_note()
        elif step is Step.SCANNING:
            self.scan_bar.start(14)
            self._start_scan()
        elif step is Step.SELECT:
            self._render_rows()
        elif step is Step.PASSWORDS:
            self._render_passwords()
        elif step is Step.DESTINATION:
            if not self.output_var.get():
                self.output_var.set(str(self._proposed_output()))
            self._update_output_hint()
        elif step is Step.CONFIRM:
            self.confirm_text.configure(text=self._confirm_summary())
        elif step is Step.WORKING:
            self._start_capture()
        elif step is Step.DONE:
            # Before the summary, so it can say what happened to them: the
            # plaintext exports have served their purpose once the bundle holds
            # an encrypted copy.
            self.shred_results = self._shred_exported_csvs()
            self.done_text.configure(text=self._done_summary())
            self._forget_passphrases()

    def _forget_passphrases(self) -> None:
        """Empty the passphrase fields, once the job they were for is over.

        Not when the work starts, which is what this used to do. A capture that
        fails at ninety per cent, or a restore that cannot write one file, sends
        the user back to try again -- and they would find the field empty, the
        button still live, and the retry failing with a message about the
        passphrase being wrong. Held until the job is genuinely finished, and
        then dropped.
        """
        for field in (self.passphrase, self.passphrase2, self.bundle_passphrase):
            field.delete(0, "end")
        self.data.passphrase = ""
        self.data.passphrase_confirm = ""
        self.data.bundle_passphrase = ""

    def _collect(self) -> None:
        """Pull the widgets' values into the data the rules are checked against."""
        self.data.mode = Mode(self.mode_var.get())
        self.data.profile_root = self.profile_var.get()
        self.data.output_path = self.output_var.get()
        self.data.replace_output = self.replace_var.get() if self._output_exists() else None
        self.data.passphrase = self.passphrase.get()
        self.data.passphrase_confirm = self.passphrase2.get()
        self.data.bundle_path = self.bundle_var.get()
        self.data.bundle_passphrase = self.bundle_passphrase.get()
        self.data.destination = self.destination_var.get()
        self.data.dry_run = self.dry_run_var.get()
        self.data.overwrite = self.overwrite_var.get()

    def _refresh_buttons(self) -> None:
        from . import wizard

        self._collect()
        verdict = wizard.check(self.step, self.data)
        automatic = self.step in wizard.AUTOMATIC
        self.next_button.configure(
            text=wizard.next_label(self.step),
            state="disabled" if (automatic or not verdict.ok) else "normal",
        )
        self.back_button.configure(
            state="normal" if wizard.can_go_back(self.step, self.data.mode) else "disabled"
        )
        finished = self.step in wizard.TERMINAL
        self.cancel_button.configure(text="Close" if finished else "Cancel")
        self.hint.configure(text=verdict.message if not verdict.ok else "")
        # Choosing the job on the first page changes what the rail says, and
        # waiting for the next page to redraw it means the user picks Restore
        # and watches a rail that still describes a backup.
        self._paint_rail()
        if self.step is Step.WELCOME:
            self._update_elevation_note()

    def _go_next(self) -> None:
        from . import wizard

        self._collect()
        if not wizard.check(self.step, self.data).ok:
            return
        if self.step in wizard.TERMINAL:
            self.root.destroy()
            return
        if self.step is Step.WELCOME and self._maybe_elevate():
            return
        following = wizard.next_step(self.step, self.data.mode)
        if following is not None:
            self._show(following)

    def _go_back(self) -> None:
        from . import wizard

        earlier = wizard.previous_step(self.step, self.data.mode)
        if earlier is not None:
            self._show(earlier)

    def _cancel(self) -> None:
        from tkinter import messagebox  # noqa: PLC0415

        from . import wizard

        if self.step in wizard.TERMINAL:
            self.root.destroy()
            return
        if self.step in (Step.WORKING, Step.RESTORING):
            # The two jobs leave very different things half-done, and telling
            # someone stopping a restore that their "backup" is unusable is
            # alarming and untrue.
            question = (
                "Stop the restore?\n\nThe files put back so far stay where they are. "
                "Running the restore again finishes the rest; nothing is written twice."
                if self.step is Step.RESTORING
                else "Stop the backup?\n\nThe part written so far will not be a usable "
                "backup and should be deleted."
            )
            if messagebox.askyesno("WinMigrate", question):
                self.root.destroy()
            return
        self.root.destroy()

    # --- elevation ---------------------------------------------------------
    def _update_elevation_note(self) -> None:
        if not elevate.should_offer(self.use_vss.get(), self.elevation_attempted):
            if self.use_vss.get() and elevate.is_windows() and elevate.is_elevated():
                self.elevation_note.configure(
                    text="Running as administrator — files that programs are using "
                    "will be copied cleanly."
                )
            elif self.use_vss.get() and self.elevation_attempted:
                self.elevation_note.configure(
                    text="Continuing without administrator rights. Files held open by "
                    "running programs may be skipped; they are listed at the end."
                )
            else:
                self.elevation_note.configure(text="")
            return
        self.elevation_note.configure(
            text="Windows will ask for administrator rights when you continue. "
            "That is what allows a shadow copy, which is the only way to copy "
            "files your browser and Outlook are holding open."
        )

    def _maybe_elevate(self) -> bool:
        """Restart elevated if that is what the options need. True means this
        process is going away and should do nothing further."""
        if not elevate.should_offer(self.use_vss.get(), self.elevation_attempted):
            return False
        arguments = elevate.forward_arguments(
            profile_root=self.profile_var.get(),
            files_only=self.files_only.get(),
            include_wifi=self.include_wifi.get(),
            include_software=self.include_software.get(),
            include_notepad=self.include_notepad.get(),
        )
        log.info("asking Windows for administrator rights (for a shadow copy)")
        if elevate.relaunch_as_admin(arguments):
            # This process is about to disappear; the elevated one opens its own
            # log, and its banner says it was started by an elevation request.
            log.info("elevated copy started; this one is closing")
            self.root.destroy()
            return True
        # Declined, or no UAC to ask. Carrying on without a shadow copy is what
        # the command line does, so it is what this does -- with the note on the
        # page updated to say so rather than a dialog nobody reads.
        log.info("elevation declined or unavailable; continuing without a shadow copy")
        self.elevation_attempted = True
        self._update_elevation_note()
        return False

    # --- the list ----------------------------------------------------------
    def _render_rows(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in self.data.rows:
            if row.selectable:
                mark = TICKED if row.item_id in self.data.selected else UNTICKED
                note = "encrypted only" if row.secret else ""
                tags = ("secret",) if row.secret else ()
            else:
                mark, note, tags = BLOCKED, row.reason, ("blocked",)
            self.tree.insert(
                "",
                "end",
                iid=row.item_id,
                values=(
                    mark,
                    row.title,
                    row.category,
                    humanize.bytes_(row.size_bytes) if row.size_bytes else "",
                    f"{row.file_count:,}" if row.file_count else "",
                    note,
                ),
                tags=tags,
            )
        total_bytes, total_files = selection.selected_totals(
            self.data.rows, self.data.selected
        )
        available = sum(r.size_bytes for r in self.data.rows if r.selectable)
        self.total_label.configure(
            text=f"{humanize.bytes_(total_bytes)} in {total_files:,} files, "
            f"of {humanize.bytes_(available)}"
        )
        self._refresh_buttons()

    def _on_tree_click(self, event: Any) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        item_id = self.tree.identify_row(event.y)
        row = next((r for r in self.data.rows if r.item_id == item_id), None)
        if row is None or not row.selectable:
            return
        if item_id in self.data.selected:
            self.data.selected.discard(item_id)
        else:
            self.data.selected.add(item_id)
        self._render_rows()

    def _set_all(self, on: bool) -> None:
        self.data.selected = (
            {r.item_id for r in self.data.rows if r.selectable} if on else set()
        )
        self._render_rows()

    # --- pickers -----------------------------------------------------------
    def _pick_profile(self) -> None:
        from tkinter import filedialog  # noqa: PLC0415

        chosen = filedialog.askdirectory(
            title="Profile folder", initialdir=self.profile_var.get()
        )
        if chosen:
            self.profile_var.set(chosen)
            self.output_var.set("")
            self._refresh_buttons()

    def _pick_output(self) -> None:
        from tkinter import filedialog  # noqa: PLC0415

        current = Path(self.output_var.get() or self._proposed_output())
        chosen = filedialog.asksaveasfilename(
            title="Save the backup as",
            initialdir=str(current.parent),
            initialfile=current.name,
            defaultextension=".dat",
            filetypes=[("WinMigrate backup", "*.dat")],
        )
        if chosen:
            self.output_var.set(chosen)
            self._update_output_hint()
            self._refresh_buttons()

    def _proposed_output(self) -> Path:
        try:
            source = detect_source_machine(self.profile_var.get())
            return defaults.default_bundle_path(source.hostname, source.username)
        except Exception:  # noqa: BLE001 -- a default must always appear
            return defaults.default_bundle_path()

    def _update_output_hint(self) -> None:
        output = self.output_var.get()
        drive = defaults.program_drive()
        if output and self._output_exists():
            self.output_hint.configure(
                text=f"There is already a backup called {Path(output).name} there. "
                "Writing over it destroys it, and nothing afterwards says what it "
                "held. Choose another name, or tick the box to replace it."
            )
            self.replace_check.pack(anchor="w", pady=(0, 16))
            return
        self.replace_var.set(False)
        self.replace_check.pack_forget()
        if output and defaults.same_drive(output, self.profile_var.get()):
            self.output_hint.configure(
                text="This is the same drive the profile is on, so it needs as much "
                "free space again as the backup will take. An external drive is safer."
            )
        else:
            self.output_hint.configure(
                text=f"Defaults to {drive}, the drive WinMigrate is running from."
            )

    def _output_exists(self) -> bool:
        """Is there already a backup at the chosen path?

        The sidecar counts as well as the bundle: half of a pair left behind is
        still someone's backup, and overwriting one of the two leaves a bundle
        and a manifest that describe different things.
        """
        raw = self.output_var.get().strip()
        if not raw:
            return False
        output = Path(raw)
        if output.suffix.lower() != ".dat":
            output = output.with_suffix(".dat")
        try:
            return output.exists() or output.with_suffix(".manifest.json").exists()
        except OSError:
            return False

    # --- summaries ---------------------------------------------------------
    def _confirm_summary(self) -> str:
        total_bytes, total_files = selection.selected_totals(
            self.data.rows, self.data.selected
        )
        secret = sum(
            1 for r in self.data.rows if r.secret and r.item_id in self.data.selected
        )
        elevated = elevate.is_windows() and elevate.is_elevated()
        shadow = "yes" if (self.use_vss.get() and elevated) else "no"
        # Built in order rather than inserted at counted positions: two
        # optional lines addressed by index is a summary that reorders itself
        # the first time both of them appear.
        lines = [
            f"From:         {self.profile_var.get()}",
            f"To:           {self.output_var.get()}",
        ]
        if self.replace_var.get():
            lines.append("Replaces:     the backup already at that name")
        lines += [
            "",
            f"Items:        {len(self.data.selected)} selected",
            f"Size:         {humanize.bytes_(total_bytes)} in {total_files:,} files",
            f"Encrypted-only items: {secret}",
        ]
        if self.data.passwords_added:
            lines.append(
                "Passwords:    exported from "
                + ", ".join(sorted(e.label for e in self.data.passwords_added.values()))
                + " (encrypted only)"
            )
        lines += [
            f"Shadow copy:  {shadow}",
            "",
            "The backup is encrypted with the passphrase you typed. Nothing is",
            "uploaded anywhere; the file stays where you put it.",
        ]
        if self.use_vss.get() and not elevated:
            lines.append("")
            lines.append(
                "Without administrator rights, files that programs are holding open"
            )
            lines.append("may be skipped. They are listed at the end.")
        return "\n".join(lines)

    def _done_summary(self) -> str:
        report = self.capture_report
        if report is None:
            return "Nothing was written."
        lines = [
            f"Backup:       {report.bundle_path}",
            f"Contents:     {report.captured_files:,} files, "
            f"{humanize.bytes_(report.captured_bytes)}",
            f"File size:    {humanize.bytes_(report.bundle_bytes)}",
            f"Shadow copy:  {'yes' if report.used_shadow_copy else 'no'}",
            "",
            f"Keep {report.manifest_path.name} beside the backup. It lets the backup "
            "be checked without the passphrase.",
        ]
        # The capture's own warnings, which only the log and the console version
        # were showing: a shadow copy that could not be read means files held
        # open by a program were copied live, and that is worth knowing before
        # the old machine is wiped.
        for note in report.notes:
            if note.severity is Severity.WARNING:
                lines += ["", f"⚠ {note.message}"]
        if report.failures:
            lines += ["", f"{len(report.failures)} file(s) could not be read. See the log."]
        if self.data.passwords_added:
            lines += [
                "",
                "Exported passwords travelled encrypted only, from "
                + ", ".join(sorted(e.label for e in self.data.passwords_added.values()))
                + ". Import them on the new machine and delete the file afterwards.",
            ]
        for line in getattr(self, "shred_results", []):
            lines += ["", line]
        if self.log_path is not None:
            lines += ["", f"Log:          {self.log_path}"]
        if self.verify_failure:
            lines += [
                "",
                "⚠ The backup was written, but the check could not be completed: "
                f"{self.verify_failure}",
                "  Run 'winmigrate verify' on it before relying on it.",
            ]
        checked = self.verify_report
        if checked is not None:
            if checked.ok:
                lines += [
                    "",
                    f"Checked:      every file read back and matched "
                    f"({checked.files_checked:,} files, "
                    f"{humanize.bytes_(checked.bytes_checked)}).",
                ]
            else:
                lines += [
                    "",
                    f"⚠ The check found problems: {len(checked.mismatches)} item(s) did "
                    f"not match and {len(checked.missing)} were missing. Do not rely on "
                    "this backup — make another one before wiping anything.",
                ]
        if report.changed_while_reading:
            lines += [
                "",
                f"{len(report.changed_while_reading)} file(s) were being written while "
                "they were copied, so their contents may be mid-change. Closing your "
                "browser and Outlook first, or letting the shadow copy run, avoids it.",
            ]
        if report.vanished:
            lines += [
                "",
                f"{len(report.vanished)} temporary file(s) disappeared while the backup "
                "ran. Nothing is missing.",
            ]
        followups = len(self.scan_result.followups) if self.scan_result else 0
        if followups:
            lines += [
                "",
                f"{followups} thing(s) still need you on the new machine — signing "
                "in to accounts, reinstalling software. Run 'winmigrate restore' there "
                "and it will list them.",
            ]
        return "\n".join(lines)

    def _open_output_folder(self) -> None:
        self._open_folder(Path(self.output_var.get()).parent)

    def _open_folder(self, target: Path) -> None:
        import subprocess  # noqa: PLC0415

        try:
            if elevate.is_windows():
                subprocess.Popen(["explorer", str(target)])  # noqa: S603, S607
        except OSError as exc:
            log.warning("could not open %s: %s", target, exc)

    # --- restoring ---------------------------------------------------------
    def _pick_bundle(self) -> None:
        from tkinter import filedialog  # noqa: PLC0415

        chosen = filedialog.askopenfilename(
            title="Choose the backup",
            filetypes=[("WinMigrate backup", "*.dat"), ("All files", "*.*")],
        )
        if chosen:
            self.bundle_var.set(chosen)
            self._describe_bundle()
            self._refresh_buttons()

    def _describe_bundle(self) -> None:
        """What can be said about the file without the passphrase.

        The header and the sidecar are plaintext by design, so the machine it
        came from and when it was made can be shown before anyone commits to
        typing anything -- and the transfer check can run, which catches a
        truncated copy without asking for a secret.
        """
        path = self.bundle_var.get().strip()
        if not path or not Path(path).is_file():
            self.bundle_summary.configure(text="")
            return
        from .. import restore as restore_mod  # noqa: PLC0415

        try:
            header = restore_mod.inspect(path)
        except Exception as exc:  # noqa: BLE001 -- a bad file is a message, not a crash
            self.bundle_summary.configure(text=f"This does not look like a backup: {exc}")
            return
        parts = [f"Made {header.get('created_utc', 'at an unknown time')}"]
        sidecar = restore_mod.load_sidecar(Path(path))
        if isinstance(sidecar, dict):
            source = sidecar.get("source") or {}
            if isinstance(source, dict) and source.get("hostname"):
                parts.append(f"on {source['hostname']}")
                if source.get("username"):
                    parts[-1] += f" ({source['username']})"
            items = sidecar.get("items")
            if isinstance(items, list):
                parts.append(f"{len(items)} item(s)")
        checked, error = restore_mod.verify_sidecar(Path(path))
        if error:
            parts.append(f"⚠ {error} — do not trust this file")
        elif checked:
            parts.append("arrived intact")
        self.bundle_summary.configure(text=" · ".join(parts))

    def _start_open(self) -> None:
        threading.Thread(
            target=self._open_worker,
            args=(Path(self.bundle_var.get()), self.bundle_passphrase.get()),
            daemon=True,
        ).start()

    def _open_worker(self, bundle: Path, passphrase: str) -> None:
        from .. import restore as restore_mod  # noqa: PLC0415

        try:
            self.events.put(("status", "Checking the file arrived intact…"))
            manifest = restore_mod.read_manifest(bundle, passphrase)
            self.events.put(("opened", manifest))
        except Exception as exc:  # noqa: BLE001 -- surfaced on the page
            self.events.put(("open-failed", str(exc)))

    def _render_restore_rows(self) -> None:
        self.restore_tree.delete(*self.restore_tree.get_children())
        for row in self.data.restore_rows:
            mark = TICKED if row.item_id in self.data.restore_selected else UNTICKED
            self.restore_tree.insert(
                "",
                "end",
                iid=row.item_id,
                values=(
                    mark,
                    row.title,
                    row.category,
                    humanize.bytes_(row.size_bytes) if row.size_bytes else "",
                    f"{row.file_count:,}" if row.file_count else "",
                    "encrypted only" if row.secret else "",
                ),
                tags=("secret",) if row.secret else (),
            )
        total_bytes, total_files = selection.selected_totals(
            self.data.restore_rows, self.data.restore_selected
        )
        self.restore_total.configure(
            text=f"{humanize.bytes_(total_bytes)} in {total_files:,} files"
        )
        self._refresh_buttons()

    def _on_restore_tree_click(self, event: Any) -> None:
        if self.restore_tree.identify_region(event.x, event.y) != "cell":
            return
        item_id = self.restore_tree.identify_row(event.y)
        if not any(r.item_id == item_id for r in self.data.restore_rows):
            return
        if item_id in self.data.restore_selected:
            self.data.restore_selected.discard(item_id)
        else:
            self.data.restore_selected.add(item_id)
        self._render_restore_rows()

    def _set_all_restore(self, on: bool) -> None:
        self.data.restore_selected = (
            {r.item_id for r in self.data.restore_rows} if on else set()
        )
        self._render_restore_rows()

    def _pick_destination(self) -> None:
        from tkinter import filedialog  # noqa: PLC0415

        chosen = filedialog.askdirectory(
            title="Restore into", initialdir=self.destination_var.get() or str(Path.home())
        )
        if chosen:
            self.destination_var.set(chosen)
            self._refresh_restore_summary()
            self._refresh_buttons()

    def _refresh_restore_summary(self) -> None:
        total_bytes, total_files = selection.selected_totals(
            self.data.restore_rows, self.data.restore_selected
        )
        lines = [
            f"From:   {self.bundle_var.get()}",
            f"Into:   {self.destination_var.get()}",
            "",
            f"{len(self.data.restore_selected)} item(s), "
            f"{humanize.bytes_(total_bytes)} in {total_files:,} files",
        ]
        from ..restore import programs_to_close  # noqa: PLC0415

        close_these = programs_to_close(
            self.manifest or {}, tuple(sorted(self.data.restore_selected))
        )
        if close_these and not self.dry_run_var.get():
            lines += [
                "",
                "⚠ Close " + ", ".join(close_these) + " before starting. The files "
                "going back are the ones they keep open, and a program writing to "
                "them at the same time can damage its own data.",
            ]
        short = self._room_shortfall(total_bytes)
        if short:
            lines += ["", short]
        if self.apply_settings_var.get() and not self.dry_run_var.get():
            lines += [
                "",
                "Wi-Fi networks, printers, mapped drives and environment variables "
                "will be put back too.",
            ]
        if self.dry_run_var.get():
            lines += ["", "Practice run: nothing will be written."]
        elif self.overwrite_var.get():
            lines += ["", "Files already here that differ will be replaced."]
        else:
            lines += ["", "Files already here that differ will be kept, not replaced."]
        self.restore_summary.configure(text="\n".join(lines))
        self._refresh_buttons()

    def _record_ids(self) -> set[str]:
        """The manifest's record items -- printers, drives, the software list.

        They carry no files, so they are not in the choosing list, and a restore
        that names only file items would leave them out along with everything
        else the user did untick.
        """
        manifest = self.manifest or {}
        return {
            str(item.get("id"))
            for item in manifest.get("items", [])
            if isinstance(item, dict)
            and item.get("kind") not in ("tree", "file")
            and item.get("id")
        }

    def _room_shortfall(self, needed: int) -> str:
        """Say on the page, not in an error afterwards, when there is no room.

        The restore refuses either way; being told before pressing Start is the
        difference between choosing fewer items and finding out at the end of a
        page you have already left.
        """
        import shutil  # noqa: PLC0415

        raw = self.destination_var.get().strip()
        if not raw or self.dry_run_var.get() or needed <= 0:
            return ""
        target = Path(raw)
        while not target.exists() and target != target.parent:
            target = target.parent
        try:
            free = shutil.disk_usage(target).free
        except OSError:
            return ""
        from ..restore import FREE_SPACE_MARGIN  # noqa: PLC0415

        if free >= needed + FREE_SPACE_MARGIN:
            return ""
        return (
            f"⚠ Not enough room: this needs about {humanize.bytes_(needed)} and "
            f"{humanize.bytes_(free)} is free. Untick some items, or restore to "
            "another drive."
        )

    def _start_restore(self) -> None:
        from ..restore import RestoreOptions  # noqa: PLC0415

        total_bytes, _files = selection.selected_totals(
            self.data.restore_rows, self.data.restore_selected
        )
        self._restore_total = max(total_bytes, 1)
        self._restore_done_bytes = 0
        self.restore_bar.configure(value=0)
        options = RestoreOptions(
            bundle=Path(self.bundle_var.get()),
            passphrase=self.bundle_passphrase.get(),
            destination=Path(self.destination_var.get()),
            dry_run=self.dry_run_var.get(),
            overwrite=self.overwrite_var.get(),
            # The records travel with the selection. They are not tick boxes --
            # the printers, the software list and the follow-ups are how a
            # restore explains itself -- but they have to be named, because
            # naming items is what tells a restore to leave the rest out.
            items=tuple(sorted(self.data.restore_selected | self._record_ids())),
            apply_settings=self.apply_settings_var.get(),
        )
        log.info(
            "restore started: %s items, %s, from %s into %s%s",
            len(self.data.restore_selected),
            humanize.bytes_(total_bytes),
            options.bundle,
            options.destination,
            " (practice run)" if options.dry_run else "",
        )
        threading.Thread(target=self._restore_worker, args=(options,), daemon=True).start()

    def _restore_worker(self, options: Any) -> None:
        from .. import restore as restore_mod  # noqa: PLC0415

        try:
            report = restore_mod.restore(
                options,
                lambda title, size: self.events.put(("restore-bytes", (title, size))),
            )
            self.events.put(("restored", report))
        except Exception as exc:  # noqa: BLE001 -- surfaced in the window
            self.events.put(("error", (str(exc), traceback.format_exc())))

    def _applied_lines(self, report: Any) -> list[str]:
        """The settings a restore put back, grouped by what happened to them."""
        from ..apply import Outcome  # noqa: PLC0415

        results = list(getattr(report, "applied", []) or [])
        if not results:
            return []
        applied = [r for r in results if r.outcome is Outcome.APPLIED]
        failed = [r for r in results if r.outcome is Outcome.FAILED]
        lines = ["", "Settings put back:"]
        if applied:
            lines.append("  " + ", ".join(sorted(f"{r.kind} {r.name}" for r in applied)))
        else:
            lines.append("  none — nothing in this backup needed re-applying")
        for result in failed:
            lines.append(f"  ⚠ {result.kind} {result.name}: {result.detail or 'failed'}")
        return lines

    def _reinstall_lines(self, report: Any) -> list[str]:
        """The software a restore prepared and deliberately did not install.

        Putting files back is what was asked for; installing software changes
        the machine in ways that are slow to undo, can want elevation, and may
        fetch different versions than were on the old one. So it is a separate
        step that shows its work and asks first -- and the command line has
        always said so at the end of a restore.

        The window said nothing at all. It wrote a winget import for 97
        applications, an Office configuration and a by-hand list into a folder
        it never named, and then offered a follow-up list whose only mention of
        software was the 167 it could *not* install. "No apps were installed"
        is not a misreading of that screen; it is the only reading available.
        """
        artifacts = getattr(report, "artifacts", None)
        if artifacts is None:
            return []
        directory = str(getattr(artifacts, "directory", ""))
        counts = []
        if getattr(artifacts, "winget_import", None):
            counts.append(f"{artifacts.reinstallable_count} can be reinstalled for you")
        if getattr(artifacts, "manual_count", 0):
            counts.append(f"{artifacts.manual_count} need installing by hand")
        lines = ["", "Software — nothing has been installed yet:"]
        if counts:
            lines.append("  " + ", ".join(counts))
        if getattr(artifacts, "component_count", 0):
            lines.append(
                f"  {artifacts.component_count} runtime(s) and driver(s) are listed for "
                "completeness; they arrive with whatever needs them"
            )
        if getattr(artifacts, "office_configuration", None):
            lines.append("  an Office configuration matching the old install was written")
        lines.append(f"  Files: {directory}")
        # Both commands, because they are not the same one: the bare command
        # prints the plan and installs nothing, which is the whole point of it.
        program = self._program_name()
        lines.append(f'  To see the plan:  {program} reinstall "{directory}"')
        lines.append(f'  To install them:  {program} reinstall "{directory}" --apps')
        return lines

    def _program_name(self) -> str:
        """What to call this program in an instruction the user has to type.

        A frozen build is an .exe with whatever name it was given; telling
        somebody running WinMigrate.exe to type "winmigrate" is telling them to
        type something their machine does not have.
        """
        import sys  # noqa: PLC0415

        if getattr(sys, "frozen", False):
            return Path(sys.executable).name
        return "winmigrate"

    def _open_reinstall_folder(self) -> None:
        artifacts = getattr(self.restore_report, "artifacts", None)
        if artifacts is None:
            return
        self._open_folder(Path(str(artifacts.directory)))

    def _render_restore_done(self) -> None:
        report = self.restore_report
        if report is None:
            self.restore_done_text.configure(text="Nothing was restored.")
            return
        verb = "Would have restored" if report.dry_run else "Restored"
        lines = [
            f"{verb} {report.restored_files:,} file(s), "
            f"{humanize.bytes_(report.restored_bytes)}",
            f"Into: {report.destination}",
        ]
        if report.skipped_existing:
            lines.append(f"Already there and identical: {report.skipped_existing:,}")
        if report.kept_existing:
            lines.append(f"Your own version kept: {report.kept_existing:,}")
        if report.digest_mismatches:
            lines.append(
                f"⚠ {len(report.digest_mismatches)} item(s) did not match the backup."
            )
        if report.failures:
            lines.append(f"⚠ {len(report.failures)} file(s) could not be written.")
        # What a restore changed beyond files, in the same place it reports the
        # files. It re-applied Wi-Fi, printers and the rest and then said
        # nothing about it, which is the one thing this tool must not do.
        lines += self._applied_lines(report)
        lines += self._reinstall_lines(report)
        for note in report.notes:
            if note.severity is Severity.WARNING:
                lines += ["", f"⚠ {note.message}"]
        if report.dry_run:
            lines += ["", "This was a practice run. Nothing was written."]
        if self.log_path is not None:
            lines += ["", f"Log: {self.log_path}"]
        self.restore_done_text.configure(text="\n".join(lines))

        if getattr(report, "artifacts", None) is not None:
            self.reinstall_button.pack(
                anchor="w", pady=(0, 10), before=self.followup_holder
            )
        else:
            self.reinstall_button.pack_forget()

        self.followup_box.configure(state="normal")
        self.followup_box.delete("1.0", "end")
        followups = report.followups or []
        if followups:
            self.followup_box.insert(
                "end",
                "These need you rather than the tool — signing in to accounts, "
                "licences, anything the operating system deliberately puts a person "
                "in front of:\n\n",
            )
            for number, followup in enumerate(followups, start=1):
                self.followup_box.insert("end", f"{number}. {followup.title}\n")
                if followup.why:
                    self.followup_box.insert("end", f"   {followup.why}\n")
                for line in followup.steps:
                    self.followup_box.insert("end", f"     • {line}\n")
                self.followup_box.insert("end", "\n")
        else:
            self.followup_box.insert("end", "Nothing else needs doing.")
        self.followup_box.configure(state="disabled")

    # --- workers -----------------------------------------------------------
    def _config(self) -> ScanConfig:
        root = self.profile_var.get().strip()
        config = ScanConfig(profile_root=Path(root) if root else None)
        config.files_only = self.files_only.get()
        config.include_wifi = self.include_wifi.get()
        config.include_software = self.include_software.get()
        config.include_notepad = self.include_notepad.get()
        return config

    def _environment(self, config: ScanConfig) -> Environment:
        """The machine the window is actually running on.

        This used to hand back ``Environment.fixture()`` whenever the profile
        field held a path -- which it always does, because the window fills it
        in with the signed-in profile. A fixture has an empty registry and
        declares itself not to be Windows: that is what makes developing this
        off Windows possible, and on a real machine it silently switched off
        every registry-backed part of a backup. The installed-software
        inventory, Office detection, OneDrive's account, the display layouts,
        where a browser is installed -- all read nothing and reported nothing
        wrong. The command line's --profile-root really is a development
        switch, documented as one; the window's "Profile to back up" field is
        not, and sharing one helper conflated them.
        """
        root = config.profile_root
        if root is None:
            return Environment.live()
        live = Environment.live()
        if not live.is_windows or os.environ.get("WINMIGRATE_ALLOW_NON_WINDOWS") == "1":
            # A development run against a fake profile tree.
            return Environment.fixture(root)
        if pathutil.normalize_key(root) == pathutil.normalize_key(live.profile_root):
            return live
        # A real machine, a profile that is not the signed-in one.
        return Environment.rooted(root)

    def _start_scan(self) -> None:
        config = self._config()
        log.info(
            "scan started: profile=%s files_only=%s wifi=%s software=%s notepad=%s",
            config.profile_root or "the signed-in profile",
            config.files_only,
            config.include_wifi,
            config.include_software,
            config.include_notepad,
        )
        threading.Thread(target=self._scan_worker, args=(config,), daemon=True).start()

    def _scan_worker(self, config: ScanConfig) -> None:
        try:
            env = self._environment(config)
            result = run_scan(config, env, progress=lambda text: self.events.put(("status", text)))
            self.events.put(("scanned", result))
        except Exception as exc:  # noqa: BLE001 -- surfaced in the window
            self.events.put(("error", (str(exc), traceback.format_exc())))

    def _start_capture(self) -> None:
        total_bytes, _files = selection.selected_totals(self.data.rows, self.data.selected)
        self._capture_total = max(total_bytes, 1)
        self._capture_done = 0
        self.capture_bar.configure(value=0)
        self.capture_detail.configure(text="Preparing…")
        options = CaptureOptions(
            output=Path(self.output_var.get()),
            passphrase=self.passphrase.get(),
            use_vss=self.use_vss.get(),
            compression=self.options.get("compression", "auto"),
            overwrite=self.replace_var.get(),
        )
        log.info(
            "capture started: %s items, %s, shadow copy=%s, rights=%s, to %s",
            len(self.data.selected),
            humanize.bytes_(total_bytes),
            "requested" if options.use_vss else "no",
            "administrator" if elevate.is_elevated() else "standard user",
            options.output,
        )
        threading.Thread(
            target=self._capture_worker,
            args=(
                options,
                self._config(),
                set(self.data.selected),
                self.verify_after.get(),
            ),
            daemon=True,
        ).start()

    def _capture_worker(
        self,
        options: CaptureOptions,
        config: ScanConfig,
        chosen: set[str],
        verify_after: bool = False,
    ) -> None:
        try:
            env = self._environment(config)
            plan = selection.apply(self.scan_result, chosen)
            report = capture_mod.capture(
                plan,
                options,
                config,
                env,
                lambda title, size: self.events.put(("bytes", (title, size))),
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced in the window
            self.events.put(("error", (str(exc), traceback.format_exc())))
            return

        if verify_after:
            # Past this point the backup exists. A check that fails, or cannot
            # run at all, is news about the backup -- not a reason to throw the
            # window back to the choosing page as though nothing had been
            # written, which is what a shared try block did.
            from .. import verify as verify_mod  # noqa: PLC0415

            self.events.put(("verifying", str(report.bundle_path)))
            try:
                # Its own event rather than an attribute on the capture report,
                # which is a slotted dataclass and rightly refuses to grow one.
                self.events.put(
                    (
                        "checked",
                        verify_mod.verify(
                            report.bundle_path,
                            options.passphrase,
                            lambda name, _size: self.events.put(("checking", name)),
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 -- reported on the last page
                log.error("the check could not be completed", exc_info=True)
                self.events.put(("check-failed", str(exc)))
        self.events.put(("captured", report))

    def _recovery_step(self) -> Step:
        """Where to land after an error: the last page the user could act on.

        Sending them to a page from the other branch, or to one whose data was
        never gathered, turns one failure into a second one.
        """
        from .wizard import as_mode  # noqa: PLC0415

        if as_mode(self.data.mode) is Mode.RESTORE:
            if self.data.bundle_open:
                return Step.RESTORE_CONFIRM
            return Step.SOURCE
        if self.data.scan_done:
            return Step.SELECT
        return Step.WELCOME

    # --- events ------------------------------------------------------------
    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def _handle(self, kind: str, payload: Any) -> None:
        from tkinter import messagebox  # noqa: PLC0415

        if kind == "status":
            if self.step is Step.SCANNING:
                self.scan_status.configure(text=str(payload))
            elif self.step is Step.OPENING:
                self.open_status.configure(text=str(payload))
        elif kind == "opened":
            self.open_bar.stop()
            self.manifest = payload
            self.data.restore_rows = selection.rows_from_manifest(payload)
            self.data.restore_selected = {r.item_id for r in self.data.restore_rows}
            self.data.bundle_open = True
            self._show(Step.RESTORE_SELECT)
        elif kind == "open-failed":
            self.open_bar.stop()
            # Almost always a mistyped passphrase, so it goes back to the field
            # rather than to a dialog that has to be dismissed first.
            messagebox.showerror(
                "WinMigrate",
                f"The backup could not be opened.\n\n{payload}\n\n"
                "The most likely reason is the passphrase.",
            )
            self._show(Step.SOURCE)
        elif kind == "restore-bytes":
            title, size = payload
            self._restore_done_bytes += size
            self.restore_bar.configure(
                value=min(1000, int(self._restore_done_bytes / self._restore_total * 1000))
            )
            self.restore_status.configure(
                text=f"{humanize.bytes_(self._restore_done_bytes)} of "
                f"{humanize.bytes_(self._restore_total)}"
            )
            self.restore_detail.configure(text=str(title))
        elif kind == "restored":
            log.info(
                "restore finished: %s files (%s), %s kept, %s failed, in %s",
                payload.restored_files,
                humanize.bytes_(payload.restored_bytes),
                payload.kept_existing,
                len(payload.failures),
                humanize.duration(payload.duration_seconds),
            )
            self.restore_bar.configure(value=1000)
            self.restore_report = payload
            self.data.restore_done = True
            self._show(Step.RESTORE_DONE)
        elif kind == "bytes":
            title, size = payload
            self._capture_done += size
            self.capture_bar.configure(
                value=min(1000, int(self._capture_done / self._capture_total * 1000))
            )
            self.capture_status.configure(
                text=f"{humanize.bytes_(self._capture_done)} of "
                f"{humanize.bytes_(self._capture_total)}"
            )
            self.capture_detail.configure(text=str(title))
        elif kind == "verifying":
            self.capture_bar.configure(value=1000)
            self.capture_status.configure(text="Checking the backup")
            self.capture_detail.configure(
                text="Reading it back and comparing every file against what was recorded."
            )
        elif kind == "checking":
            self.capture_detail.configure(text=f"Checking {Path(str(payload)).name}")
        elif kind == "check-failed":
            self.verify_failure = str(payload)
        elif kind == "checked":
            self.verify_report = payload
            log.info(
                "check finished: %s file(s), %s mismatch(es), %s missing",
                payload.files_checked,
                len(payload.mismatches),
                len(payload.missing),
            )
        elif kind == "scanned":
            # A new scan is a new plan, and nothing staged against the old one
            # is in it. Chief among them the password exports: keeping those
            # records would have the last page claim they travelled and offer
            # to delete the only plaintext copy of passwords that are not in
            # the bundle at all.
            # The browsers found belong to the profile that was scanned. Looking
            # them up again costs a few file reads and is the difference between
            # a page describing this profile and one describing the last.
            self.password_state_known = False
            self.password_targets = []
            self.password_targets_by_key = {}
            self.password_cloud_accounts = []
            if self.data.passwords_added:
                log.info(
                    "dropping %s staged password export(s): the profile was scanned again",
                    len(self.data.passwords_added),
                )
                self.data.passwords_added.clear()
            totals = payload.totals()
            log.info(
                "scan finished: %s items, %s to capture",
                len(payload.items),
                humanize.bytes_(totals.capture_bytes),
            )
            self.scan_bar.stop()
            self.scan_result = payload
            self.data.rows = selection.rows_for(payload)
            self.data.selected = {r.item_id for r in self.data.rows if r.selected}
            self.data.scan_done = True
            self._show(Step.SELECT)
        elif kind == "captured":
            log.info(
                "capture finished: %s (%s) in %s",
                payload.bundle_path,
                humanize.bytes_(payload.bundle_bytes),
                humanize.duration(payload.duration_seconds),
            )
            self.capture_bar.configure(value=1000)
            self.capture_report = payload
            self.data.capture_done = True
            self._show(Step.DONE)
        elif kind == "error":
            message, detail = payload
            self.scan_bar.stop()
            self.open_bar.stop()
            log.error("%s", detail)
            messagebox.showerror("WinMigrate", message)
            self._show(self._recovery_step())
