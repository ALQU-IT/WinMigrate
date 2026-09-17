"""Enough of tkinter to drive the real window without a display.

The window's logic lives outside the widgets on purpose, but "outside the
widgets" stops at the point where a callback reads one. Everything past that --
whether a page's widgets exist by the time something asks for them, whether the
rail says what the mode says, whether a passphrase is still in the field when
the user comes back to retry -- can only be found by running it, and tkinter
needs a display that CI has not got.

So the widgets are stubbed and the wizard is real. The stubs record what they
were configured with, which is what makes it possible to ask what the window is
showing rather than only whether it crashed. Two bugs turned up on the first
attempt that no amount of reading had found.
"""

from __future__ import annotations

import queue
import sys
import types


class Var:
    """StringVar/BooleanVar: a value with a get and a set."""

    def __init__(self, value=None, master=None, **kwargs):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class Widget:
    def __init__(self, *args, **kwargs):
        self._kw = dict(kwargs)
        self._children: list = []
        self._packed = True
        self._text = kwargs.get("text", "")
        self._inserted: list[str] = []

    # -- geometry
    def pack(self, **kwargs):
        self._packed = True

    def pack_forget(self):
        self._packed = False

    def pack_propagate(self, value):
        pass

    def grid(self, **kwargs):
        pass

    def columnconfigure(self, *args, **kwargs):
        pass

    # -- state
    def configure(self, **kwargs):
        self._kw.update(kwargs)
        if "text" in kwargs:
            self._text = kwargs["text"]

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        return self._kw.get(key, "")

    @property
    def state(self):
        return self._kw.get("state")

    # -- everything the window calls on something
    def bind(self, *args, **kwargs):
        pass

    def insert(self, *args, **kwargs):
        # Text widgets are written into rather than configured, and what the
        # restore page puts in the follow-up box -- "import these, then delete
        # the file" -- is the actual output of a restore. Recorded so a test can
        # read it.
        self._inserted.extend(str(arg) for arg in args if isinstance(arg, str))

    def delete(self, *args, **kwargs):
        pass

    def get(self, *args):
        return ""

    def set(self, *args):
        pass

    def start(self, *args):
        pass

    def stop(self):
        pass

    def yview(self, *args):
        pass

    def see(self, *args):
        """Scrolling to the newest line. Nothing to scroll here."""

    def focus(self):
        return ""

    def identify_region(self, x, y):
        return "cell"

    def identify_row(self, y):
        return ""

    def tag_configure(self, *args, **kwargs):
        pass

    def get_children(self):
        return list(self._children)

    def heading(self, *args, **kwargs):
        pass

    def column(self, *args, **kwargs):
        pass

    def title(self, *args):
        pass

    def geometry(self, *args):
        pass

    def minsize(self, *args):
        pass

    def destroy(self):
        self.destroyed = True

    # -- the clipboard, which the passwords page puts an address on
    def clipboard_clear(self):
        self.clipboard = ""

    def clipboard_append(self, text):
        self.clipboard = getattr(self, "clipboard", "") + str(text)

    def after(self, milliseconds, callback=None, *args):
        # Deliberately does not run the callback: the event loop is driven by
        # the test, one event at a time, so a scan finishing cannot interleave
        # with the assertion looking at what it produced.
        return "after#1"


class Entry(Widget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._value = ""

    def get(self):
        return self._value

    def insert(self, index, text):
        self._value += text

    def delete(self, first, last=None):
        self._value = ""


class Treeview(Widget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows: dict[str, tuple] = {}

    def insert(self, parent, index, iid=None, values=(), tags=()):
        self.rows[iid] = values
        self._children.append(iid)

    def delete(self, *iids):
        for iid in iids:
            self.rows.pop(iid, None)
        self._children = [child for child in self._children if child in self.rows]


class Canvas(Widget):
    """The backdrop. Records what was drawn, in order.

    Not a drawing surface -- there is no display -- but the wizard positions
    the three panels on it, so what it was told to draw and where is exactly
    the layout, and can be asserted on.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.items: list[tuple] = []
        self.windows: dict[int, dict] = {}
        self._next = 0

    def _add(self, kind, *coords, **kwargs):
        self._next += 1
        self.items.append((kind, coords, kwargs))
        return self._next

    def create_rectangle(self, *coords, **kwargs):
        return self._add("rectangle", *coords, **kwargs)

    def create_oval(self, *coords, **kwargs):
        return self._add("oval", *coords, **kwargs)

    def create_polygon(self, *coords, **kwargs):
        return self._add("polygon", *coords, **kwargs)

    def create_line(self, *coords, **kwargs):
        return self._add("line", *coords, **kwargs)

    def create_window(self, x, y, window=None, anchor="nw", **kwargs):
        self._next += 1
        self.windows[self._next] = {"x": x, "y": y, "window": window, **kwargs}
        return self._next

    def coords(self, item, *values):
        if item in self.windows and len(values) >= 2:
            self.windows[item]["x"], self.windows[item]["y"] = values[0], values[1]

    def itemconfigure(self, item, **kwargs):
        if item in self.windows:
            self.windows[item].update(kwargs)

    itemconfig = itemconfigure

    def delete(self, *tags):
        # Only tagged drawing is cleared; the embedded panels stay, which is
        # what the real canvas does and what the wizard relies on.
        if "backdrop" in tags:
            self.items = [i for i in self.items if i[2].get("tags") != "backdrop"]

    def winfo_width(self):
        return 1000

    def winfo_height(self):
        return 700


class Style:
    def __init__(self, *args):
        self.themes: list[str] = []
        self.styles: dict[str, dict] = {}
        self.layouts: dict[str, object] = {}

    def theme_use(self, name):
        self.themes.append(name)

    def configure(self, name, **kwargs):
        self.styles[name] = kwargs

    def map(self, *args, **kwargs):
        pass

    def layout(self, name, spec=None):
        self.layouts[name] = spec
        return spec


def install(monkeypatch) -> list:
    """Put the stub in place. Returns the list dialogs are recorded into."""
    shown: list[tuple[str, tuple]] = []

    def module(name):
        made = types.ModuleType(name)
        for widget in (
            "Frame", "Label", "Button", "Checkbutton", "Radiobutton", "Separator",
            "Progressbar", "Scrollbar", "Combobox", "LabelFrame", "Notebook",
        ):
            setattr(made, widget, Widget)
        made.Entry = Entry
        made.Treeview = Treeview
        made.Style = Style
        return made

    tk = module("tkinter")
    tk.Tk = Widget
    tk.Text = Widget
    tk.Canvas = Canvas
    tk.StringVar = Var
    tk.BooleanVar = Var
    tk.TkVersion = 8.6
    tk.TclError = type("TclError", (Exception,), {})
    ttk = module("tkinter.ttk")
    tk.ttk = ttk

    font = types.ModuleType("tkinter.font")
    font.families = lambda: ["Segoe UI", "DejaVu Sans"]
    tk.font = font

    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askdirectory = lambda **kwargs: ""
    filedialog.asksaveasfilename = lambda **kwargs: ""
    filedialog.askopenfilename = lambda **kwargs: ""

    messagebox = types.ModuleType("tkinter.messagebox")
    messagebox.showerror = lambda *args, **kwargs: shown.append(("error", args))
    messagebox.showinfo = lambda *args, **kwargs: shown.append(("info", args))
    messagebox.askyesno = lambda *args, **kwargs: True

    for name, made in (
        ("tkinter", tk), ("tkinter.ttk", ttk), ("tkinter.font", font),
        ("tkinter.filedialog", filedialog), ("tkinter.messagebox", messagebox),
    ):
        monkeypatch.setitem(sys.modules, name, made)
    return shown


def open_window(monkeypatch, options: dict | None = None):
    """The real wizard, on stub widgets. Returns ``(wizard, dialogs)``."""
    import importlib

    shown = install(monkeypatch)
    app = importlib.import_module("winmigrate.gui.app")
    return app.WinMigrateWizard(Widget(), options or {}), shown


def pump(wizard, until=("scanned", "captured", "opened", "open-failed", "restored", "error"),
         limit: int = 400):
    """Drain the worker queue the way the window's timer would.

    Returns the event that ended the wait, so a test can say what happened
    rather than inferring it from the page that is showing.
    """
    for _ in range(limit):
        try:
            kind, payload = wizard.events.get(timeout=20)
        except queue.Empty:
            return "nothing"
        wizard._handle(kind, payload)
        if kind in until:
            return kind
    return "too many events"


def written(widget) -> str:
    """Everything inserted into a Text widget, as one string."""
    return "".join(widget._inserted)


def rail(wizard) -> list[str]:
    """What the step rail currently reads, top to bottom."""
    return [label.cget("text").strip() for label in wizard.rail_labels if label._packed]
