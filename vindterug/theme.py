"""Shared visual theme for the PC-Toolbox programs (Tkinter/ttk).

This file is the canonical source. It is copied unchanged to
``<package>/theme.py`` in every program, so that each program stays
standalone to build and to move around.

Standard library only: tkinter + ttk. No external dependencies, so
PyInstaller can build a small .exe without extra hooks.

Design principles
-----------------
* Calm, cool grey-blue palette with one accent colour; no gradient heroes,
  no pastel/beige background, no emoji as iconography.
* A real type scale (7 sizes) instead of two random sizes.
* Contrast: all text colours meet WCAG AA on their background.
* Colour is never the only carrier of information; there is always text or a
  symbol next to it (see ``Badge``).
"""

from __future__ import annotations

import os
import sys
import traceback

import tkinter as tk
from tkinter import ttk, messagebox

__all__ = [
    "PALETTE",
    "FONTS",
    "APP_TITLE_SUFFIX",
    "apply_theme",
    "Card",
    "SectionTitle",
    "Badge",
    "StatusBar",
    "FormRow",
    "toast",
    "show_info",
    "show_error",
    "show_confirm",
    "show_text_dialog",
    "copy_to_clipboard",
    "open_in_explorer",
    "run_app",
    "selftest",
]

APP_TITLE_SUFFIX = "PC-Toolbox"

# --------------------------------------------------------------------------
# Colours — cool neutral grey with one accent (steel blue).
# --------------------------------------------------------------------------
PALETTE: dict[str, str] = {
    "canvas": "#EDF0F3",       # window background
    "surface": "#FFFFFF",      # cards, input fields
    "surface_alt": "#F6F8FA",  # alternating rows, secondary areas
    "surface_sunken": "#E4E8ED",
    "border": "#D6DCE3",
    "border_strong": "#AEB8C4",
    "ink": "#111820",          # main text
    "ink_muted": "#4E5A6B",    # secondary text (AA on white and on surface_alt)
    "ink_faint": "#6E7A8A",    # tertiary, only for non-essential text
    "accent": "#1F5F8B",
    "accent_hover": "#164A6E",
    "accent_soft": "#E1EDF5",
    "accent_ink": "#FFFFFF",
    "ok": "#1D6F45",
    "ok_soft": "#E1F0E7",
    "warn": "#8A5600",
    "warn_soft": "#FBEFDA",
    "danger": "#9E2B2B",
    "danger_soft": "#F7E4E4",
    "focus": "#2C7CB0",
}

FONTS: dict[str, tuple] = {
    "display": ("Segoe UI Semibold", 19),
    "title": ("Segoe UI Semibold", 14),
    "subtitle": ("Segoe UI Semibold", 11),
    "body": ("Segoe UI", 10),
    "body_strong": ("Segoe UI Semibold", 10),
    "small": ("Segoe UI", 9),
    "caption": ("Segoe UI", 8),
    "mono": ("Consolas", 10),
    "mono_small": ("Consolas", 9),
}

_RADIUS_PAD = 8


def apply_theme(root: tk.Misc) -> ttk.Style:
    """Apply the theme to a Tk root or Toplevel. Idempotent."""
    root.option_add("*Font", FONTS["body"])
    root.configure(background=PALETTE["canvas"])

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:  # pragma: no cover - platform fallback
        pass

    p = PALETTE
    style.configure(".", background=p["canvas"], foreground=p["ink"],
                    font=FONTS["body"], borderwidth=0, focuscolor=p["focus"])
    style.configure("TFrame", background=p["canvas"])
    style.configure("Surface.TFrame", background=p["surface"])
    style.configure("Alt.TFrame", background=p["surface_alt"])
    style.configure("Sunken.TFrame", background=p["surface_sunken"])
    style.configure("TLabel", background=p["canvas"], foreground=p["ink"])
    style.configure("Surface.TLabel", background=p["surface"], foreground=p["ink"])
    style.configure("Alt.TLabel", background=p["surface_alt"], foreground=p["ink"])
    style.configure("Muted.TLabel", background=p["canvas"], foreground=p["ink_muted"],
                    font=FONTS["small"])
    style.configure("MutedSurface.TLabel", background=p["surface"],
                    foreground=p["ink_muted"], font=FONTS["small"])
    style.configure("Caption.TLabel", background=p["canvas"],
                    foreground=p["ink_faint"], font=FONTS["caption"])
    style.configure("Display.TLabel", background=p["canvas"], foreground=p["ink"],
                    font=FONTS["display"])
    style.configure("Title.TLabel", background=p["canvas"], foreground=p["ink"],
                    font=FONTS["title"])
    style.configure("Subtitle.TLabel", background=p["canvas"], foreground=p["ink"],
                    font=FONTS["subtitle"])
    style.configure("Mono.TLabel", background=p["surface"], foreground=p["ink"],
                    font=FONTS["mono"])

    # Buttons ------------------------------------------------------------
    def _btn(name: str, bg: str, fg: str, hover: str, *,
             border: str | None = None, font=None):
        style.configure(name, background=bg, foreground=fg, font=font or FONTS["body"],
                        borderwidth=1 if border else 0, relief="flat",
                        padding=(14, 7), anchor="center")
        style.map(name,
                  background=[("pressed", hover), ("active", hover),
                              ("disabled", p["surface_sunken"])],
                  foreground=[("disabled", p["ink_faint"])],
                  bordercolor=[("!disabled", border or bg)])
        if border:
            style.configure(name, bordercolor=border, lightcolor=bg, darkcolor=bg,
                            relief="solid")

    _btn("TButton", p["surface"], p["ink"], p["surface_alt"], border=p["border_strong"])
    _btn("Accent.TButton", p["accent"], p["accent_ink"], p["accent_hover"],
         font=FONTS["body_strong"])
    _btn("Ghost.TButton", p["canvas"], p["accent"], p["accent_soft"])
    _btn("Danger.TButton", p["danger_soft"], p["danger"], "#F0D2D2", border=p["danger"])
    _btn("Tiny.TButton", p["surface"], p["ink_muted"], p["surface_alt"],
         border=p["border"], font=FONTS["small"])
    style.configure("Tiny.TButton", padding=(8, 4))

    # Input -------------------------------------------------------------
    style.configure("TEntry", fieldbackground=p["surface"], foreground=p["ink"],
                    bordercolor=p["border_strong"], lightcolor=p["surface"],
                    darkcolor=p["surface"], insertcolor=p["ink"], padding=6,
                    relief="flat")
    style.map("TEntry", bordercolor=[("focus", p["focus"])])
    style.configure("TCombobox", fieldbackground=p["surface"], background=p["surface"],
                    foreground=p["ink"], arrowcolor=p["ink_muted"], padding=5,
                    bordercolor=p["border_strong"], relief="flat")
    style.map("TCombobox", fieldbackground=[("readonly", p["surface"])],
              bordercolor=[("focus", p["focus"])])
    style.configure("TCheckbutton", background=p["canvas"], foreground=p["ink"],
                    focuscolor=p["canvas"], padding=2)
    style.map("TCheckbutton", background=[("active", p["canvas"])],
              foreground=[("disabled", p["ink_faint"])])
    style.configure("Surface.TCheckbutton", background=p["surface"], focuscolor=p["surface"])
    style.map("Surface.TCheckbutton", background=[("active", p["surface"])])
    style.configure("TRadiobutton", background=p["canvas"], foreground=p["ink"],
                    focuscolor=p["canvas"])
    style.map("TRadiobutton", background=[("active", p["canvas"])])
    style.configure("Surface.TRadiobutton", background=p["surface"],
                    focuscolor=p["surface"])
    style.map("Surface.TRadiobutton", background=[("active", p["surface"])])
    style.configure("TScale", background=p["canvas"], troughcolor=p["surface_sunken"])
    style.configure("TProgressbar", background=p["accent"],
                    troughcolor=p["surface_sunken"], bordercolor=p["border"],
                    lightcolor=p["accent"], darkcolor=p["accent"], thickness=8)
    style.configure("TScrollbar", background=p["surface_sunken"],
                    troughcolor=p["canvas"], bordercolor=p["canvas"],
                    arrowcolor=p["ink_muted"], relief="flat")
    style.map("TScrollbar", background=[("active", p["border_strong"])])
    style.configure("TSeparator", background=p["border"])

    # Notebook -----------------------------------------------------------
    style.configure("TNotebook", background=p["canvas"], borderwidth=0,
                    tabmargins=(0, 4, 0, 0))
    style.configure("TNotebook.Tab", background=p["canvas"], foreground=p["ink_muted"],
                    padding=(16, 8), font=FONTS["body"], borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", p["surface"])],
              foreground=[("selected", p["ink"])],
              font=[("selected", FONTS["body_strong"])])

    # Tables -----------------------------------------------------------
    style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"],
                    foreground=p["ink"], rowheight=26, borderwidth=0,
                    font=FONTS["body"])
    style.map("Treeview",
              background=[("selected", p["accent_soft"])],
              foreground=[("selected", p["ink"])])
    style.configure("Treeview.Heading", background=p["surface_alt"],
                    foreground=p["ink_muted"], font=FONTS["body_strong"],
                    relief="flat", padding=(8, 7), borderwidth=0)
    style.map("Treeview.Heading", background=[("active", p["surface_sunken"])])

    # Card / dialog ----------------------------------------------------
    style.configure("TLabelframe", background=p["canvas"], bordercolor=p["border"],
                    borderwidth=1, relief="solid")
    style.configure("TLabelframe.Label", background=p["canvas"],
                    foreground=p["ink_muted"], font=FONTS["subtitle"])
    return style


# --------------------------------------------------------------------------
# Small building blocks
# --------------------------------------------------------------------------
class Card(ttk.Frame):
    """Card with a border, used as a visual group. Do not nest without reason."""

    def __init__(self, master, *, padding=_RADIUS_PAD, style_name="Surface.TFrame", **kw):
        super().__init__(master, style=style_name, padding=padding, **kw)
        self._edge = tk.Frame(self, background=PALETTE["border"], height=1)
        self._edge.pack_forget()


class SectionTitle(ttk.Frame):
    """Heading with optional secondary explanation and room on the right for actions."""

    def __init__(self, master, text: str, subtitle: str | None = None, **kw):
        super().__init__(master, **kw)
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text=text, style="Subtitle.TLabel").grid(
            row=0, column=0, sticky="w")
        self.actions = ttk.Frame(self)
        self.actions.grid(row=0, column=1, sticky="e")
        if subtitle:
            ttk.Label(self, text=subtitle, style="Muted.TLabel",
                      wraplength=760, justify="left").grid(
                row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))


class Badge(tk.Label):
    """Small status label. Always carries text, never colour alone."""

    TONES = ("neutral", "ok", "warn", "danger", "accent")

    def __init__(self, master, text: str, tone: str = "neutral", **kw):
        fg, bg = self._colors(tone)
        super().__init__(master, text=text, foreground=fg, background=bg,
                         font=FONTS["caption"], padx=7, pady=2, **kw)

    @staticmethod
    def _colors(tone: str) -> tuple[str, str]:
        p = PALETTE
        return {
            "ok": (p["ok"], p["ok_soft"]),
            "warn": (p["warn"], p["warn_soft"]),
            "danger": (p["danger"], p["danger_soft"]),
            "accent": (p["accent"], p["accent_soft"]),
            "neutral": (p["ink_muted"], p["surface_sunken"]),
        }.get(tone, (p["ink_muted"], p["surface_sunken"]))


class FormRow:
    """Helper to line up a label + input field neatly in a grid."""

    def __init__(self, parent, label: str, row: int, *, hint: str | None = None,
                 start_column: int = 0, label_width: int = 22):
        self.label = ttk.Label(parent, text=label, style="Surface.TLabel")
        self.label.grid(row=row, column=start_column, sticky="w", pady=(0, 1))
        self.slot = ttk.Frame(parent, style="Surface.TFrame")
        self.slot.columnconfigure(0, weight=1)
        self.slot.grid(row=row, column=start_column + 1, sticky="ew", pady=(0, 8))
        if hint:
            ttk.Label(parent, text=hint, style="MutedSurface.TLabel",
                      wraplength=560, justify="left").grid(
                row=row, column=start_column + 2, sticky="w", padx=(10, 0))


class StatusBar(ttk.Frame):
    """Bottom bar with status on the left and an optional progress bar."""

    def __init__(self, master, **kw):
        super().__init__(master, style="Sunken.TFrame", padding=(12, 6), **kw)
        self.columnconfigure(0, weight=1)
        self._left = tk.Label(self, text="", background=PALETTE["surface_sunken"],
                              foreground=PALETTE["ink_muted"], font=FONTS["small"],
                              anchor="w")
        self._left.grid(row=0, column=0, sticky="ew")
        self._right = tk.Label(self, text="", background=PALETTE["surface_sunken"],
                               foreground=PALETTE["ink_muted"], font=FONTS["small"],
                               anchor="e")
        self._right.grid(row=0, column=1, sticky="e")
        self._bar = ttk.Progressbar(self, mode="determinate", length=180)
        self._bar.grid(row=0, column=2, sticky="e", padx=(10, 0))
        self._bar.grid_remove()

    def set(self, text: str, right: str = "") -> None:
        self._left.configure(text=text)
        self._right.configure(text=right)
        self.update_idletasks()

    def progress(self, value: float | None) -> None:
        if value is None:
            self._bar.grid_remove()
            return
        self._bar.grid()
        self._bar.configure(value=max(0.0, min(100.0, value)))


# --------------------------------------------------------------------------
# Messages and dialogs
# --------------------------------------------------------------------------
def _tone_icon(kind: str) -> str:
    return {"info": "i", "warn": "!", "error": "x"}.get(kind, "i")


def show_info(parent, message: str, title: str = "Done") -> None:
    messagebox.showinfo(title, message, parent=parent)


def show_error(parent, message: str, title: str = "Something went wrong") -> None:
    messagebox.showerror(title, message, parent=parent)


def show_confirm(parent, message: str, title: str = "Confirm") -> bool:
    return bool(messagebox.askyesno(title, message, parent=parent))


def show_text_dialog(parent, title: str, text: str, *, width: int = 760,
                     height: int = 460) -> None:
    """Show (mono) text in a dialog with a copy button."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(background=PALETTE["canvas"])
    win.transient(parent)
    win.geometry(f"{width}x{height}")
    win.rowconfigure(0, weight=1)
    win.columnconfigure(0, weight=1)

    frame = ttk.Frame(win, style="Surface.TFrame", padding=12)
    frame.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)

    box = tk.Text(frame, wrap="word", font=FONTS["mono_small"],
                  background=PALETTE["surface"], foreground=PALETTE["ink"],
                  relief="flat", padx=8, pady=8)
    box.grid(row=0, column=0, sticky="nsew")
    scroll = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
    scroll.grid(row=0, column=1, sticky="ns")
    box.configure(yscrollcommand=scroll.set)
    box.insert("1.0", text)
    box.configure(state="disabled")

    bar = ttk.Frame(win)
    bar.grid(row=1, column=0, sticky="e", padx=12, pady=(0, 12))
    ttk.Button(bar, text="Copy", command=lambda: copy_to_clipboard(win, text)).pack(
        side="left", padx=(0, 8))
    ttk.Button(bar, text="Close", style="Accent.TButton",
               command=win.destroy).pack(side="left")
    win.bind("<Escape>", lambda _e: win.destroy())
    return win


def toast(parent, message: str, *, tone: str = "neutral", ms: int = 2600,
          x: int | None = None, y: int | None = None) -> None:
    """Short message in its own little window without buttons."""
    colors = {
        "neutral": (PALETTE["ink"], PALETTE["surface"]),
        "ok": (PALETTE["ok_soft"], PALETTE["ok"]),
        "warn": (PALETTE["warn_soft"], PALETTE["warn"]),
        "danger": (PALETTE["danger_soft"], PALETTE["danger"]),
    }
    bg, fg = colors.get(tone, colors["neutral"])
    try:
        top = tk.Toplevel(parent)
    except tk.TclError:  # pragma: no cover
        return
    top.overrideredirect(True)
    top.attributes("-topmost", True)
    tk.Label(top, text=message, background=bg, foreground=fg,
             font=FONTS["body"], padx=16, pady=10, justify="left",
             wraplength=520).pack()
    top.update_idletasks()
    win = parent.winfo_toplevel()
    px = x if x is not None else win.winfo_rootx() + win.winfo_width() - top.winfo_width() - 28
    py = y if y is not None else win.winfo_rooty() + win.winfo_height() - 72
    top.geometry(f"+{max(0, px)}+{max(0, py)}")
    top.after(ms, top.destroy)


def copy_to_clipboard(parent, text: str) -> None:
    try:
        parent.clipboard_clear()
        parent.clipboard_append(text)
    except tk.TclError:  # pragma: no cover
        pass


def explorer_plan(path: str) -> tuple[str, str, str]:
    """Work out what Explorer should do: (action, target, explanation).

    Actions: ``folder`` (open this folder), ``select`` (select this file in its
    folder), ``parent`` (the item no longer exists; open the nearest existing
    folder) and ``nothing`` (there is nothing to open).

    Pure calculation, without touching Explorer, so that this is testable.
    """
    raw = str(path or "").strip().strip('"')
    if not raw:
        return "nothing", "", "There is no path to open."
    path = os.path.normpath(raw)
    try:
        if os.path.isdir(path):
            return "folder", path, f"The folder {path} is being opened."
        if os.path.isfile(path):
            return ("select", path,
                    f"Explorer opens the folder with {os.path.basename(path)} selected.")
    except OSError:
        pass
    parent_path = os.path.dirname(path)
    while parent_path and parent_path != os.path.dirname(parent_path):
        try:
            if os.path.isdir(parent_path):
                return ("parent", parent_path,
                        f"{os.path.basename(path) or path} no longer exists; "
                        f"I opened the folder around it.")
        except OSError:
            pass
        parent_path = os.path.dirname(parent_path)
    return "nothing", "", f"This path does not exist (any more): {path}"


def open_in_explorer(parent, path: str) -> None:
    """Open a folder or select a file in Explorer; never raises an error."""
    import subprocess

    action, target, explanation = explorer_plan(path)
    if action == "nothing":
        show_error(parent, explanation or f"This location no longer exists:\n{path}")
        return
    try:
        if action in ("folder", "parent"):
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["explorer", "/select,", target])
    except OSError as exc:  # pragma: no cover
        show_error(parent, f"Could not open Explorer:\n{exc}\n\n{explanation}")
        return
    if action == "parent":
        toast(parent, explanation, tone="warn")


# --------------------------------------------------------------------------
# Starting, error handling and smoke test
# --------------------------------------------------------------------------
def _log_path(app_name: str) -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "PC-Toolbox", "logs")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{app_name}.log")


def _write_crash_log(app_name: str, text: str) -> str:
    try:
        path = _log_path(app_name)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
        return path
    except OSError:  # pragma: no cover
        return ""


def available_screen(root: tk.Misc) -> tuple[int, int]:
    """How much room a window may really use on this screen.

    Some is taken off for the taskbar and the window edges: a window that is
    as tall as the screen otherwise ends up with its bottom off-screen.
    """
    width = int(root.winfo_screenwidth())
    height = int(root.winfo_screenheight())
    return max(320, width - 40), max(240, height - 80)


def limit_geometry(root: tk.Misc, geometry: str | None) -> str:
    """Make sure a requested window size fits on the screen and centre it.

    Without this, on a small screen (1280x720 for example) the bottom of the
    window falls off — and with it precisely the buttons at the bottom.
    """
    screen_width, screen_height = available_screen(root)
    requested_width, requested_height = screen_width, screen_height
    if geometry:
        part = geometry.strip().split("+")[0].split("x")
        if len(part) >= 2:
            try:
                requested_width = int(part[0])
                requested_height = int(part[1])
            except ValueError:
                pass
    width = min(requested_width, screen_width)
    height = min(requested_height, screen_height)
    x = max(0, (int(root.winfo_screenwidth()) - width) // 2)
    y = max(0, (int(root.winfo_screenheight()) - height) // 2 - 20)
    return f"{width}x{height}+{x}+{y}"


def run_app(app_name: str, build_ui, *, geometry: str | None = None,
            min_size: tuple[int, int] | None = None) -> int:
    """Start a program with tidy error handling.

    ``--selftest`` builds the whole interface, pumps one event-loop round and
    closes again. That is the automated smoke test for these GUIs.
    """
    selftest_mode = "--selftest" in sys.argv
    root = tk.Tk()
    root.title(app_name)
    if geometry:
        root.geometry(limit_geometry(root, geometry))
    if min_size:
        screen_width, screen_height = available_screen(root)
        root.minsize(min(min_size[0], screen_width),
                     min(min_size[1], screen_height))

    state: dict[str, object] = {}

    def _report(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        path = _write_crash_log(app_name, text)
        details = f"\n\nTechnical details are in:\n{path}" if path else ""
        try:
            messagebox.showerror(
                app_name,
                f"Something went wrong in {app_name}.\n\n"
                f"{exc_type.__name__}: {exc_value}{details}",
                parent=root)
        except tk.TclError:  # pragma: no cover
            print(text, file=sys.stderr)

    root.report_callback_exception = _report
    try:
        apply_theme(root)
        build_ui(root, state)
    except Exception:  # noqa: BLE001 - top-level safety net at start-up
        _report(*sys.exc_info())
        if selftest_mode:
            print("SELFTEST FAILED during build", file=sys.stderr)
            raise
        root.destroy()
        return 1

    if selftest_mode:
        try:
            root.update_idletasks()
            root.update()
        except tk.TclError as exc:  # pragma: no cover
            print(f"SELFTEST FAILED during update: {exc}", file=sys.stderr)
            raise
        finally:
            root.destroy()
        print("SELFTEST OK")
        return 0

    root.mainloop()
    return 0


def selftest(app_name: str, build_ui, *, geometry: str | None = None) -> int:
    """Programmatic smoke test (same path as ``--selftest``)."""
    for arg in ("--selftest",):
        if arg not in sys.argv:
            sys.argv.append(arg)
    return run_app(app_name, build_ui, geometry=geometry)
