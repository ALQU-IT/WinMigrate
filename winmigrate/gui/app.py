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
from ..models import ScanResult
from ..platform_win import Environment
from ..scan import run_scan
from ..util import humanize
from . import defaults, elevate, selection, theme
from .wizard import Step, WizardData

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
    scrub_tcl_environment()
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
    WinMigrateWizard(root, options or {})
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
        self.step = Step.WELCOME
        self.scan_result: ScanResult | None = None
        self.capture_report: Any = None
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.elevation_attempted = bool(options.get("elevation_attempted"))
        self._capture_total = 1
        self._capture_done = 0

        root.title("WinMigrate")
        root.geometry(f"{theme.WINDOW_WIDTH}x{theme.WINDOW_HEIGHT}")
        root.minsize(820, 560)

        self.family = theme.font_family()
        self.style = ttk.Style(root)
        theme.apply(self.style, self.family)
        root.configure(background=theme.PAGE)

        self._build_chrome()
        self._build_pages()
        self._show(Step.WELCOME)
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

    # --- pages -------------------------------------------------------------
    def _build_pages(self) -> None:
        self.pages: dict[Step, Any] = {}
        for step, builder in (
            (Step.WELCOME, self._page_welcome),
            (Step.SCANNING, self._page_scanning),
            (Step.SELECT, self._page_select),
            (Step.DESTINATION, self._page_destination),
            (Step.CONFIRM, self._page_confirm),
            (Step.WORKING, self._page_working),
            (Step.DONE, self._page_done),
        ):
            frame = self.ttk.Frame(self.page_area, style="Page.TFrame")
            builder(frame)
            self.pages[step] = frame

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
        self.tree.tag_configure("secret", foreground=theme.SECRET)
        self.tree.tag_configure("blocked", foreground=theme.INK_FAINT)
        self.tree.bind("<Button-1>", self._on_tree_click)

        row = ttk.Frame(page, style="Page.TFrame")
        row.pack(fill="x", pady=(10, 4))
        ttk.Button(row, text="Select all", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(row, text="Select none", command=lambda: self._set_all(False)).pack(
            side="left", padx=8
        )
        self.total_label = ttk.Label(row, text="", style="Body.TLabel")
        self.total_label.pack(side="right")

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

        for frame in self.pages.values():
            frame.pack_forget()
        self.step = step
        heading, subtitle = wizard.title(step)
        self.title_label.configure(text=heading)
        self.subtitle_label.configure(text=subtitle)
        self.pages[step].pack(fill="both", expand=True)

        current = wizard.rail_index(step)
        for index, label in enumerate(self.rail_labels):
            marker = theme.rail_marker(index, current)
            text = label.cget("text").strip()
            for glyph in ("✓", "●", "○"):
                text = text.replace(glyph, "").strip()
            label.configure(text=f"{marker}  {text}", style=theme.rail_style(index, current))

        self._on_enter(step)
        self._refresh_buttons()

    def _on_enter(self, step: Step) -> None:
        if step is Step.WELCOME:
            self._update_elevation_note()
        elif step is Step.SCANNING:
            self.scan_bar.start(14)
            self._start_scan()
        elif step is Step.SELECT:
            self._render_rows()
        elif step is Step.DESTINATION:
            if not self.output_var.get():
                self.output_var.set(str(self._proposed_output()))
            self._update_output_hint()
        elif step is Step.CONFIRM:
            self.confirm_text.configure(text=self._confirm_summary())
        elif step is Step.WORKING:
            self._start_capture()
        elif step is Step.DONE:
            self.done_text.configure(text=self._done_summary())

    def _collect(self) -> None:
        """Pull the widgets' values into the data the rules are checked against."""
        self.data.profile_root = self.profile_var.get()
        self.data.output_path = self.output_var.get()
        self.data.passphrase = self.passphrase.get()
        self.data.passphrase_confirm = self.passphrase2.get()

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
            state="normal" if wizard.can_go_back(self.step) else "disabled"
        )
        self.cancel_button.configure(text="Close" if self.step is Step.DONE else "Cancel")
        self.hint.configure(text=verdict.message if not verdict.ok else "")
        if self.step is Step.WELCOME:
            self._update_elevation_note()

    def _go_next(self) -> None:
        from . import wizard

        self._collect()
        if not wizard.check(self.step, self.data).ok:
            return
        if self.step is Step.DONE:
            self.root.destroy()
            return
        if self.step is Step.WELCOME and self._maybe_elevate():
            return
        following = wizard.next_step(self.step)
        if following is not None:
            self._show(following)

    def _go_back(self) -> None:
        from . import wizard

        earlier = wizard.previous_step(self.step)
        if earlier is not None:
            self._show(earlier)

    def _cancel(self) -> None:
        from tkinter import messagebox  # noqa: PLC0415

        if self.step is Step.DONE:
            self.root.destroy()
            return
        if self.step is Step.WORKING:
            if messagebox.askyesno(
                "WinMigrate",
                "Stop the backup?\n\nThe part written so far will not be a usable "
                "backup and should be deleted.",
            ):
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
        if elevate.relaunch_as_admin(arguments):
            self.root.destroy()
            return True
        # Declined, or no UAC to ask. Carrying on without a shadow copy is what
        # the command line does, so it is what this does -- with the note on the
        # page updated to say so rather than a dialog nobody reads.
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
        if output and defaults.same_drive(output, self.profile_var.get()):
            self.output_hint.configure(
                text="This is the same drive the profile is on, so it needs as much "
                "free space again as the backup will take. An external drive is safer."
            )
        else:
            self.output_hint.configure(
                text=f"Defaults to {drive}, the drive WinMigrate is running from."
            )

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
        lines = [
            f"From:         {self.profile_var.get()}",
            f"To:           {self.output_var.get()}",
            "",
            f"Items:        {len(self.data.selected)} selected",
            f"Size:         {humanize.bytes_(total_bytes)} in {total_files:,} files",
            f"Encrypted-only items: {secret}",
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
        if report.failures:
            lines += ["", f"{len(report.failures)} file(s) could not be read. See the log."]
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
        import subprocess  # noqa: PLC0415

        target = Path(self.output_var.get()).parent
        try:
            if elevate.is_windows():
                subprocess.Popen(["explorer", str(target)])  # noqa: S603, S607
        except OSError as exc:
            log.warning("could not open %s: %s", target, exc)

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
        return (
            Environment.fixture(config.profile_root)
            if config.profile_root is not None
            else Environment.live()
        )

    def _start_scan(self) -> None:
        config = self._config()
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
        options = CaptureOptions(
            output=Path(self.output_var.get()),
            passphrase=self.passphrase.get(),
            use_vss=self.use_vss.get(),
            compression=self.options.get("compression", "auto"),
        )
        # Cleared the moment the worker has them: the passphrase lives in the
        # options object and nowhere the window can leak it.
        self.passphrase.delete(0, "end")
        self.passphrase2.delete(0, "end")
        threading.Thread(
            target=self._capture_worker,
            args=(options, self._config(), set(self.data.selected)),
            daemon=True,
        ).start()

    def _capture_worker(
        self, options: CaptureOptions, config: ScanConfig, chosen: set[str]
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
            self.events.put(("captured", report))
        except Exception as exc:  # noqa: BLE001 -- surfaced in the window
            self.events.put(("error", (str(exc), traceback.format_exc())))

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
        elif kind == "scanned":
            self.scan_bar.stop()
            self.scan_result = payload
            self.data.rows = selection.rows_for(payload)
            self.data.selected = {r.item_id for r in self.data.rows if r.selected}
            self.data.scan_done = True
            self._show(Step.SELECT)
        elif kind == "captured":
            self.capture_bar.configure(value=1000)
            self.capture_report = payload
            self.data.capture_done = True
            self._show(Step.DONE)
        elif kind == "error":
            message, detail = payload
            self.scan_bar.stop()
            log.error("%s", detail)
            messagebox.showerror("WinMigrate", message)
            self._show(Step.WELCOME if not self.data.scan_done else Step.SELECT)
