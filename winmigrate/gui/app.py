"""The desktop window: scan, choose, capture.

Deliberately a thin shell. Every decision about what a bundle contains lives in
:mod:`winmigrate.scan`, :mod:`winmigrate.gui.selection` and
:mod:`winmigrate.capture`, and this module only shows them and collects clicks.
A second implementation of "what goes in the bundle" is the one thing a GUI
must not become, because it would be the one nobody tests.

tkinter, because it ships with Python: no wheel to find for a frozen build, no
extra licence, and an .exe measured in tens of megabytes rather than hundreds.

Two rules the window keeps that a console does not have to think about:

* **The work happens off the UI thread.** A scan of a real profile takes
  minutes and a capture takes an hour; doing either on the main thread gives
  Windows a frozen, "not responding" window, and the whole point of this tool is
  that it shows what it is doing. Progress comes back through a queue that the
  main thread drains on a timer.
* **The passphrase is never anywhere but the widget.** Not in a variable that
  outlives the capture, not in the log, not in the window title. It is read at
  the moment it is needed and the field is cleared afterwards.
"""

from __future__ import annotations

import queue
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import capture as capture_mod
from ..capture import CaptureOptions
from ..compression import CHOICES as COMPRESSION_CHOICES
from ..config import ScanConfig
from ..manifest import detect_source_machine
from ..models import ScanResult
from ..platform_win import Environment
from ..scan import run_scan
from ..util import humanize
from . import defaults, selection

TICKED = "☑"      # ☑
UNTICKED = "☐"    # ☐
BLOCKED = "–"     # –


def run() -> int:
    """Open the window. Returns a process exit code."""
    try:
        import tkinter as tk  # noqa: PLC0415
    except ImportError:
        print(
            "The graphical interface needs tkinter, which is missing from this "
            "Python installation. Use the command line instead: winmigrate --help"
        )
        return 2
    root = tk.Tk()
    WinMigrateApp(root)
    root.mainloop()
    return 0


class WinMigrateApp:
    """The window and its state."""

    def __init__(self, root: Any) -> None:
        import tkinter as tk  # noqa: PLC0415
        from tkinter import ttk  # noqa: PLC0415

        self.tk = tk
        self.ttk = ttk
        self.root = root
        root.title("WinMigrate")
        root.geometry("980x680")
        root.minsize(760, 520)

        self.scan_result: ScanResult | None = None
        self.rows: list[selection.Row] = []
        self.selected: set[str] = set()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False

        self._build()
        self.root.after(100, self._drain_events)

    # --- layout ------------------------------------------------------------
    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Profile").grid(row=0, column=0, sticky="w")
        self.profile_var = tk.StringVar(value=str(Path.home()))
        ttk.Entry(top, textvariable=self.profile_var, width=60).grid(
            row=0, column=1, sticky="we", padx=(6, 6)
        )
        ttk.Button(top, text="Browse…", command=self._pick_profile).grid(row=0, column=2)
        self.scan_button = ttk.Button(top, text="Scan", command=self._start_scan)
        self.scan_button.grid(row=0, column=3, padx=(6, 0))
        top.columnconfigure(1, weight=1)

        options = ttk.LabelFrame(self.root, text="Options")
        options.pack(fill="x", **pad)
        self.files_only = tk.BooleanVar(value=False)
        self.include_wifi = tk.BooleanVar(value=False)
        self.include_software = tk.BooleanVar(value=True)
        self.use_vss = tk.BooleanVar(value=True)
        for column, (text, var) in enumerate(
            (
                ("Files only (no credentials)", self.files_only),
                ("Include Wi-Fi passwords", self.include_wifi),
                ("Inventory software", self.include_software),
                ("Shadow copy (needs admin)", self.use_vss),
            )
        ):
            ttk.Checkbutton(options, text=text, variable=var).grid(
                row=0, column=column, sticky="w", padx=8, pady=4
            )
        ttk.Label(options, text="Compression").grid(row=0, column=4, sticky="e", padx=(16, 4))
        self.compression = tk.StringVar(value="auto")
        ttk.Combobox(
            options,
            textvariable=self.compression,
            values=list(COMPRESSION_CHOICES),
            width=8,
            state="readonly",
        ).grid(row=0, column=5, sticky="w")

        middle = ttk.LabelFrame(self.root, text="What to capture")
        middle.pack(fill="both", expand=True, **pad)
        columns = ("pick", "title", "category", "size", "files", "note")
        self.tree = ttk.Treeview(middle, columns=columns, show="headings", selectmode="none")
        for name, text, width, anchor in (
            ("pick", "", 34, "center"),
            ("title", "Item", 330, "w"),
            ("category", "Kind", 130, "w"),
            ("size", "Size", 100, "e"),
            ("files", "Files", 80, "e"),
            ("note", "", 220, "w"),
        ):
            self.tree.heading(name, text=text)
            self.tree.column(name, width=width, anchor=anchor, stretch=(name in ("title", "note")))
        scroll = ttk.Scrollbar(middle, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._on_space)
        self.tree.tag_configure("secret", foreground="#8a5cf6")
        self.tree.tag_configure("blocked", foreground="#888888")

        buttons = ttk.Frame(self.root)
        buttons.pack(fill="x", padx=10)
        ttk.Button(buttons, text="Select all", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(buttons, text="Select none", command=lambda: self._set_all(False)).pack(
            side="left", padx=6
        )
        self.total_label = ttk.Label(buttons, text="Nothing scanned yet.")
        self.total_label.pack(side="right")

        bottom = ttk.LabelFrame(self.root, text="Bundle")
        bottom.pack(fill="x", **pad)
        ttk.Label(bottom, text="Save to").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.output_var = tk.StringVar(value=str(self._proposed_output()))
        ttk.Entry(bottom, textvariable=self.output_var).grid(
            row=0, column=1, sticky="we", padx=6
        )
        ttk.Button(bottom, text="Browse…", command=self._pick_output).grid(row=0, column=2, padx=6)
        ttk.Label(bottom, text="Passphrase").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.passphrase = ttk.Entry(bottom, show="•")
        self.passphrase.grid(row=1, column=1, sticky="we", padx=6)
        ttk.Label(bottom, text="Confirm").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.passphrase2 = ttk.Entry(bottom, show="•")
        self.passphrase2.grid(row=2, column=1, sticky="we", padx=6)
        self.capture_button = ttk.Button(
            bottom, text="Capture", command=self._start_capture, state="disabled"
        )
        self.capture_button.grid(row=1, column=2, rowspan=2, padx=6, sticky="ns")
        bottom.columnconfigure(1, weight=1)
        ttk.Label(
            bottom,
            text="The bundle is encrypted with this passphrase only. There is no recovery "
            "if you lose it.",
            foreground="#a06010",
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 6))

        status = ttk.Frame(self.root)
        status.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(status, mode="determinate", maximum=1000)
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(status, textvariable=self.status_var).pack(anchor="w", pady=(4, 0))

    # --- helpers -----------------------------------------------------------
    def _proposed_output(self) -> Path:
        try:
            source = detect_source_machine(self.profile_var.get())
            return defaults.default_bundle_path(source.hostname, source.username)
        except Exception:  # noqa: BLE001 -- a default must never fail to appear
            return defaults.default_bundle_path(now=datetime.now())

    def _config(self) -> ScanConfig:
        root = self.profile_var.get().strip()
        config = ScanConfig(profile_root=Path(root) if root else None)
        config.files_only = self.files_only.get()
        config.include_wifi = self.include_wifi.get()
        config.include_software = self.include_software.get()
        return config

    def _environment(self, config: ScanConfig) -> Environment:
        return (
            Environment.fixture(config.profile_root)
            if config.profile_root is not None
            else Environment.live()
        )

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.scan_button.configure(state=state)
        self.capture_button.configure(
            state="normal" if (not busy and self.scan_result is not None) else "disabled"
        )
        if status:
            self.status_var.set(status)

    # --- the list ----------------------------------------------------------
    def _render_rows(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in self.rows:
            if row.selectable:
                mark = TICKED if row.item_id in self.selected else UNTICKED
                note = "encrypted-only" if row.secret else ""
                tags = ("secret",) if row.secret else ()
            else:
                mark = BLOCKED
                note = row.reason
                tags = ("blocked",)
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
        self._update_total()

    def _update_total(self) -> None:
        total_bytes, total_files = selection.selected_totals(self.rows, self.selected)
        available = sum(r.size_bytes for r in self.rows if r.selectable)
        self.total_label.configure(
            text=f"Selected {humanize.bytes_(total_bytes)} in {total_files:,} files "
            f"of {humanize.bytes_(available)} available"
        )

    def _toggle(self, item_id: str) -> None:
        row = next((r for r in self.rows if r.item_id == item_id), None)
        if row is None or not row.selectable:
            return
        if item_id in self.selected:
            self.selected.discard(item_id)
        else:
            self.selected.add(item_id)
        self._render_rows()

    def _on_click(self, event: Any) -> None:
        if self.busy or self.tree.identify_region(event.x, event.y) != "cell":
            return
        item_id = self.tree.identify_row(event.y)
        if item_id:
            self._toggle(item_id)

    def _on_space(self, _event: Any) -> None:
        focused = self.tree.focus()
        if focused and not self.busy:
            self._toggle(focused)

    def _set_all(self, on: bool) -> None:
        if self.busy:
            return
        self.selected = {r.item_id for r in self.rows if r.selectable} if on else set()
        self._render_rows()

    # --- pickers -----------------------------------------------------------
    def _pick_profile(self) -> None:
        from tkinter import filedialog  # noqa: PLC0415

        chosen = filedialog.askdirectory(title="Profile folder", initialdir=self.profile_var.get())
        if chosen:
            self.profile_var.set(chosen)
            self.output_var.set(str(self._proposed_output()))

    def _pick_output(self) -> None:
        from tkinter import filedialog  # noqa: PLC0415

        current = Path(self.output_var.get())
        chosen = filedialog.asksaveasfilename(
            title="Save the bundle as",
            initialdir=str(current.parent),
            initialfile=current.name,
            defaultextension=".dat",
            filetypes=[("WinMigrate bundle", "*.dat")],
        )
        if chosen:
            self.output_var.set(chosen)

    # --- scanning ----------------------------------------------------------
    def _start_scan(self) -> None:
        if self.busy:
            return
        self._set_busy(True, "Scanning…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        config = self._config()
        threading.Thread(target=self._scan_worker, args=(config,), daemon=True).start()

    def _scan_worker(self, config: ScanConfig) -> None:
        try:
            env = self._environment(config)
            result = run_scan(config, env, progress=lambda text: self.events.put(("status", text)))
            self.events.put(("scanned", result))
        except Exception as exc:  # noqa: BLE001 -- surfaced in the window
            self.events.put(("error", (str(exc), traceback.format_exc())))

    # --- capturing ---------------------------------------------------------
    def _start_capture(self) -> None:
        from tkinter import messagebox  # noqa: PLC0415

        if self.busy or self.scan_result is None:
            return
        passphrase = self.passphrase.get()
        if not passphrase:
            messagebox.showerror("WinMigrate", "A passphrase is required; bundles are always encrypted.")
            return
        if passphrase != self.passphrase2.get():
            messagebox.showerror("WinMigrate", "The passphrases did not match.")
            return
        if not self.selected:
            messagebox.showerror("WinMigrate", "Nothing is selected.")
            return

        output = Path(self.output_var.get())
        profile = self.profile_var.get()
        if defaults.same_drive(output, profile):
            # Writing the bundle onto the drive being captured needs the
            # profile's size again in free space. Worth asking about now rather
            # than running out at 90%.
            if not messagebox.askyesno(
                "WinMigrate",
                f"{output} is on the same drive as the profile being captured.\n\n"
                "That needs as much free space again as the profile. Continue?",
            ):
                return

        total_bytes, _files = selection.selected_totals(self.rows, self.selected)
        self._set_busy(True, "Capturing…")
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._capture_total = max(total_bytes, 1)
        self._capture_done = 0

        options = CaptureOptions(
            output=output,
            passphrase=passphrase,
            use_vss=self.use_vss.get(),
            compression=self.compression.get(),
        )
        # Cleared immediately: the passphrase lives in the worker's argument and
        # nowhere else for longer than it takes to derive the key.
        self.passphrase.delete(0, "end")
        self.passphrase2.delete(0, "end")

        threading.Thread(
            target=self._capture_worker,
            args=(options, self._config(), set(self.selected)),
            daemon=True,
        ).start()

    def _capture_worker(self, options: CaptureOptions, config: ScanConfig, chosen: set[str]) -> None:
        try:
            env = self._environment(config)
            plan = selection.apply(self.scan_result, chosen)

            def progress(title: str, size: int) -> None:
                self.events.put(("bytes", (title, size)))

            report = capture_mod.capture(plan, options, config, env, progress)
            self.events.put(("captured", report))
        except Exception as exc:  # noqa: BLE001 -- surfaced in the window
            self.events.put(("error", (str(exc), traceback.format_exc())))

    # --- events from the workers -------------------------------------------
    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _handle(self, kind: str, payload: Any) -> None:
        from tkinter import messagebox  # noqa: PLC0415

        if kind == "status":
            self.status_var.set(str(payload))
        elif kind == "bytes":
            title, size = payload
            self._capture_done += size
            self.progress.configure(
                value=min(1000, int(self._capture_done / self._capture_total * 1000))
            )
            self.status_var.set(
                f"{title} — {humanize.bytes_(self._capture_done)} of "
                f"{humanize.bytes_(self._capture_total)}"
            )
        elif kind == "scanned":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.scan_result = payload
            self.rows = selection.rows_for(payload)
            self.selected = {r.item_id for r in self.rows if r.selected}
            self._render_rows()
            self._set_busy(False, f"Scanned in {payload.duration_seconds:.1f}s. Choose what to keep.")
        elif kind == "captured":
            self.progress.configure(value=1000)
            self._set_busy(False, f"Written to {payload.bundle_path}")
            messagebox.showinfo("WinMigrate", _summary(payload))
        elif kind == "error":
            message, detail = payload
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self._set_busy(False, "Stopped.")
            messagebox.showerror("WinMigrate", message)
            print(detail)


def _summary(report: Any) -> str:
    lines = [
        f"Bundle: {report.bundle_path}",
        f"{report.captured_files:,} files, {humanize.bytes_(report.captured_bytes)} captured",
        f"Bundle size: {humanize.bytes_(report.bundle_bytes)}",
        f"Shadow copy: {'yes' if report.used_shadow_copy else 'no'}",
    ]
    if report.failures:
        lines.append(f"{len(report.failures)} file(s) could not be read — see the log.")
    if report.vanished:
        lines.append(f"{len(report.vanished)} temporary file(s) disappeared while running.")
    lines.append("")
    lines.append(f"Keep {report.manifest_path.name} beside the bundle.")
    return "\n".join(lines)
