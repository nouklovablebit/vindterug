"""The interface of VindTerug.

Everything lives in one window: one search bar, a list of results, a panel
with the fragment and the full text, and a panel where you decide what
gets indexed.

The design is deliberately kept calm: colour is never used without text
beside it, and the search bar tells you with a grey example question what you
can type.

Indexing is slow work (reading files, OCR, unpacking mail). That is why it
happens in a background thread; the interface keeps responding in the
meantime and collects the progress with ``root.after``.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk

from . import core
from . import sources
from . import theme

APP_NAME = "VindTerug"

# What the status bar shows when nothing has been indexed yet.
EMPTY_START = "No index yet"

EXAMPLE_QUERY = "for example: that mail about that tender in March"

# The kinds as they come into the list, each with a readable word beside it.
KIND_TEXT = {
    "mail": "e-mail",
    "image": "screenshot",
    "pdf": "pdf document",
    "office": "office file",
    "tekst": "text file",
    "muziek": "music",
    "browser": "website",
    "": "unknown",
}

# Which extensions count as a 'document', and which as a screenshot. Mail
# (.eml/.mbox) is its own source; the remaining kinds are documents.
DOCUMENT_KINDS = {"tekst", "pdf", "office", "muziek"}


class VindTerugApp:
    """The main window of VindTerug."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.index = core.VindTerugIndex()
        self.searcher = core.Searcher(self.index)
        self.results: list[core.SearchResult] = []
        self.by_item: dict[str, core.SearchResult] = {}
        self.queue: queue.Queue = queue.Queue()
        self.busy = False
        self.running_indexer: core.Indexer | None = None
        self.first_text: str = ""
        self._build()
        self._show_start_or_results()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x")
        theme.SectionTitle(
            head,
            "What are you looking for?",
            "Type what you remember about it. VindTerug searches by meaning, "
            "not by file name — so even if you no longer remember what it is called.",
        ).pack(side="left", anchor="w")

        # -- search bar ----------------------------------------------------
        search = ttk.Frame(outer)
        search.pack(fill="x", pady=(8, 4))
        self.search_query = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.search_query)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda _e: self.search())
        # Ctrl+C in the search bar copies the path of the chosen result,
        # as in the rest of the window.
        self.search_entry.bind("<Control-c>", self.copy_path)
        ttk.Button(search, text="Search", style="Accent.TButton",
                   command=self.search).pack(side="left", padx=(8, 0))
        self.example_label = ttk.Label(search, text=EXAMPLE_QUERY,
                                       style="Muted.TLabel")
        self.example_label.pack(side="left", padx=(10, 0))

        # -- explanation of the query ---------------------------------------
        self.explanation_label = ttk.Label(outer, text="", style="Muted.TLabel",
                                           wraplength=1000, justify="left")
        self.explanation_label.pack(fill="x", pady=(0, 4))

        split = ttk.Panedwindow(outer, orient="horizontal")
        split.pack(fill="both", expand=True)

        left = ttk.Frame(split)
        split.add(left, weight=3)

        self.tree = ttk.Treeview(
            left, columns=("title", "kind", "date", "why"),
            show="headings", selectmode="browse", height=4)
        for key, text, width in (
            ("title", "What it is", 300),
            ("kind", "Kind", 110),
            ("date", "Date", 100),
            ("why", "Why it matches", 320),
        ):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w",
                             stretch=(key in ("title", "why")))
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Control-c>", self.copy_path)
        self.tree.bind("<Return>", lambda _e: self.open_item())
        self.tree.bind("<Double-1>", lambda _e: self.open_item())

        right = theme.Card(split)
        split.add(right, weight=4)
        self.detail_title = ttk.Label(right, text="Nothing chosen yet",
                                      style="Title.TLabel", wraplength=520,
                                      justify="left")
        self.detail_title.pack(anchor="w")
        self.badge_row = ttk.Frame(right, style="Surface.TFrame")
        self.badge_row.pack(anchor="w", pady=(8, 2))
        self.detail_where = ttk.Label(right, text="", style="MutedSurface.TLabel",
                                      wraplength=520, justify="left")
        self.detail_where.pack(anchor="w", pady=(4, 10))

        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)

        fragment_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(fragment_tab, text="Fragment")
        self.fragment_box = self._text_widget(fragment_tab)

        text_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(text_tab, text="Full text")
        self.text_box = self._text_widget(text_tab, mono=True)

        actions = ttk.Frame(right, style="Surface.TFrame")
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Open", style="Accent.TButton",
                   command=self.open_item).pack(side="left")
        ttk.Button(actions, text="Open folder",
                   command=self.open_folder).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Copy",
                   command=self.copy_path).pack(side="left", padx=(8, 0))

        # -- sources & index -------------------------------------------------
        self.sources_card = theme.Card(outer)
        self.sources_card.pack(fill="x", pady=(12, 0))
        self._build_sources_panel(self.sources_card)

        self.status = theme.StatusBar(outer)
        self.status.pack(fill="x", pady=(8, 0))

        # Keyboard: arrows, Enter, F5 and Ctrl+C.
        self.root.bind("<F5>", lambda _e: self.start_index())
        self.root.bind("<Control-c>", self.copy_path)
        self.root.bind("<Escape>", lambda _e: self._clear_search_box())

        self._refresh_state()

    def _text_widget(self, parent: tk.Misc, *, mono: bool = False) -> tk.Text:
        """A read-only text box with a scrollbar."""
        holder = ttk.Frame(parent, style="Surface.TFrame")
        holder.pack(fill="both", expand=True)
        box = tk.Text(
            holder, wrap="word", height=2,
            font=theme.FONTS["mono_small"] if mono else theme.FONTS["body"],
            background=theme.PALETTE["surface"],
            foreground=theme.PALETTE["ink"], relief="flat", padx=8, pady=8)
        bar = ttk.Scrollbar(holder, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=bar.set)
        box.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return box

    def _build_sources_panel(self, parent: ttk.Frame) -> None:
        """The panel with folders, sources and the 'Update index' button."""
        title = theme.SectionTitle(parent, "Sources & index")
        title.pack(fill="x")

        folder_row = ttk.Frame(parent, style="Surface.TFrame")
        folder_row.pack(fill="x", pady=(8, 4))
        self.default_button = ttk.Button(
            folder_row, text="Add my default folders",
            style="Accent.TButton", command=self.add_default_folders)
        self.default_button.pack(side="left")
        self.computer_button = ttk.Button(
            folder_row, text="Add whole computer",
            command=self.add_whole_computer)
        self.computer_button.pack(side="left", padx=(8, 0))
        ttk.Button(folder_row, text="Add folder…",
                   command=self.add_folder).pack(side="left", padx=(8, 0))
        ttk.Button(folder_row, text="Remove folder",
                   command=self.remove_folder).pack(side="left", padx=(8, 0))
        self.folder_list = tk.Listbox(
            folder_row, height=1, activestyle="none",
            background=theme.PALETTE["surface_alt"],
            foreground=theme.PALETTE["ink"],
            selectbackground=theme.PALETTE["accent_soft"],
            selectforeground=theme.PALETTE["ink"],
            relief="flat", highlightthickness=1,
            highlightbackground=theme.PALETTE["border"],
            font=theme.FONTS["small"])
        self.folder_list.pack(side="left", fill="x", expand=True, padx=(10, 0))

        self.source_vars: dict[str, tk.BooleanVar] = {
            "documenten": tk.BooleanVar(value=True),
            "mail": tk.BooleanVar(value=True),
            "screenshots": tk.BooleanVar(value=False),
            "browser": tk.BooleanVar(value=False),
        }
        boxes = ttk.Frame(parent, style="Surface.TFrame")
        boxes.pack(fill="x")
        boxes.columnconfigure(0, weight=1)
        boxes.columnconfigure(1, weight=1)
        # Two columns instead of one long list: that saves height, and on
        # a 720 pixel screen every line counts.
        for number, (key, label) in enumerate((
            ("documenten", "Documents (.txt, .md, .pdf, Word, Excel)"),
            ("mail", "E-mail (.eml and .mbox)"),
            ("screenshots", "Screenshots (via OCR, can be slow)"),
            ("browser", "Browser history (Chrome, Edge, Firefox)"),
        )):
            ttk.Checkbutton(boxes, text=label, variable=self.source_vars[key],
                            style="Surface.TCheckbutton").grid(
                row=number // 2, column=number % 2, sticky="w")

        self.ocr_label = ttk.Label(parent, text="", style="MutedSurface.TLabel",
                                   wraplength=900, justify="left")
        self.ocr_label.pack(anchor="w", pady=(4, 0))

        buttons = ttk.Frame(parent, style="Surface.TFrame")
        buttons.pack(fill="x", pady=(8, 0))
        self.index_button = ttk.Button(buttons, text="Update index",
                                       style="Accent.TButton",
                                       command=self.start_index)
        self.index_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Cancel",
                                      command=self.stop_index, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Clear index",
                   command=self.clear_index).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="What gets indexed?",
                   command=self.show_help).pack(side="right")

        self.progress = ttk.Progressbar(buttons, mode="determinate", length=180)
        self.progress.pack(side="right", padx=(0, 10))
        self.progress.pack_forget()

    # ------------------------------------------------------------------ state

    def _refresh_state(self) -> None:
        """Update the folder list, the status line and the OCR text."""
        self.folder_list.delete(0, "end")
        for folder_path in self.index.folders():
            self.folder_list.insert("end", folder_path)
        state = self.index.state()
        self.status.set(state.summary())
        pending = self.index.queue_count()
        if pending:
            self.status.set(
                f"{pending} more items to go from an earlier round",
                "click 'Update index' to continue")
        self.ocr_label.configure(
            text=core.ocr.ocr_status_text(core.ocr.detect("auto")))

    def _show_start_or_results(self) -> None:
        """Without an index: explain in the window itself what to do."""
        state = self.index.state()
        if state.is_empty:
            self._show_first_steps()
            return
        self.status.set(state.summary())
        self.status.progress(None)

    def _show_first_steps(self) -> None:
        """The explanation in the window itself when there is no index yet."""
        self._fill(
            self.fragment_box,
            "\n".join([
                "No index yet — here is how it works:",
                "",
                "1. Click 'Add my default folders' below: that prepares "
                "Desktop, Documents, Downloads, Music, Pictures and "
                "Videos straight away, plus your browser history if it is there.",
                "   If you want to search everything, choose 'Add whole computer'. "
                "That takes a long time the first time, but you can interrupt it and "
                "continue later.",
                "   Or click 'Add folder…' and choose a folder yourself.",
                "2. Tick what should be indexed from it: documents, "
                "e-mail, screenshots and/or browser history.",
                "3. Click 'Update index'. That happens in the background; "
                "the window keeps working as usual.",
                "4. Then type what you are looking for at the top and press Enter.",
                "",
                "The first time it takes a while: every file is read "
                "once and then skipped as long as it does not change.",
                "",
                "Everything stays on this pc. VindTerug sends nothing and needs "
                "no internet.",
            ]))
        self._fill(self.text_box, "The full text of a result appears here.")
        self.detail_title.configure(text="First steps")
        self._show_badge("No index yet", "warn")
        self.detail_where.configure(text="")
        self.status.set(EMPTY_START)

    # ------------------------------------------------------------------ search

    def search(self) -> None:
        """Run the search query and fill the list."""
        query = self.search_query.get().strip()
        if not query:
            self._show_first_steps()
            return
        try:
            results, explanation = self.searcher.search(query)
        except core.IndexFout as exc:
            theme.show_error(self.root, f"The index could not be searched:\n{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - never let the window fall over
            theme.show_error(self.root, f"The search failed:\n{exc}")
            return

        self.results = results
        self.by_item.clear()
        self.tree.delete(*self.tree.get_children())

        if not results:
            self.explanation_label.configure(
                text="Nothing found. " + " ".join(explanation.lines()))
            self._show_no_results()
            self.status.set(self.index.state().summary())
            return

        for result in results:
            local = core.date_to_local(result.date)
            item = self.tree.insert("", "end", values=(
                result.title or result.path,
                KIND_TEXT.get(result.kind, result.kind or "unknown"),
                local.strftime("%d-%m-%Y") if local else "unknown",
                result.why,
            ))
            self.by_item[item] = result

        self.explanation_label.configure(
            text=f"{len(results)} results · " + " ".join(explanation.lines()))
        self.example_label.configure(text="")
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0])
            self.tree.focus(children[0])
        self.status.set(self.index.state().summary(),
                        f"{len(results)} found")

    def _show_no_results(self) -> None:
        self.detail_title.configure(text="No results")
        self._show_badge("nothing found", "warn")
        self.detail_where.configure(text="")
        self._fill(self.fragment_box, "\n".join([
            "Nothing was found that matches this question.",
            "",
            "What you can try:",
            "  • fewer words, or a different word for the same thing",
            "  • leaving out the time (for example 'in March')",
            "  • checking whether the folder with this document has been added "
            "and indexed",
        ]))
        self._fill(self.text_box, "")

    def _clear_search_box(self) -> None:
        self.search_query.set("")
        self.example_label.configure(text=EXAMPLE_QUERY)
        self.search_entry.focus_set()

    # ------------------------------------------------------------------ detail

    def current(self) -> core.SearchResult | None:
        """The chosen result."""
        selection = self.tree.selection()
        if not selection:
            return None
        return self.by_item.get(selection[0])

    def _on_select(self, _event=None) -> None:
        result = self.current()
        if result is None:
            return
        self.detail_title.configure(text=result.title or result.path)
        self._show_badge(KIND_TEXT.get(result.kind, result.kind),
                         "accent" if result.url else "neutral")
        self.detail_where.configure(
            text=f"{result.what()}\n{result.why}")

        lines = [result.why, ""]
        if result.error:
            lines += [f"Note: {result.error}", ""]
        lines.append(result.snippet or "No fragment available.")
        self._fill(self.fragment_box, "\n".join(lines))

        text = self.index.full_text(result.doc_id)
        if not text and result.url:
            # A website visit has no file to show; show what is
            # known about it, so the detail panel does not stay empty.
            text = "\n".join([
                "Website from the browser history:",
                result.url,
                "",
                "Visited on: " + (core.date_to_local(result.date).strftime(
                    "%d-%m-%Y %H:%M") if result.date else "unknown"),
                "Title: " + (result.title or "(untitled)"),
            ])
        self._fill(self.text_box, text or "No text from this document was saved.")

    def _show_badge(self, text: str, tone: str) -> None:
        for kind in self.badge_row.winfo_children():
            kind.destroy()
        if text:
            theme.Badge(self.badge_row, text, tone).pack(side="left")

    def _fill(self, box: tk.Text, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    # ------------------------------------------------------------------ actions

    def open_item(self) -> None:
        """Open the file in the default program, or the url in the browser."""
        result = self.current()
        if result is None:
            return
        if result.url:
            webbrowser.open(result.url)
            return
        path = result.path
        if not path or not Path(path).exists():
            theme.show_error(self.root, f"This file is no longer there:\n{path}")
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            theme.show_error(self.root, f"Could not open this file:\n{exc}")

    def open_folder(self) -> None:
        """Open the folder of the chosen result in Explorer."""
        result = self.current()
        if result is None:
            return
        if result.url:
            theme.show_info(
                self.root,
                "This is a website from the browser history; there is no folder.\n\n"
                f"{result.url}")
            return
        theme.open_in_explorer(self.root, result.path)

    def copy_path(self, _event=None) -> None:
        """Copy the path or the url of the chosen result."""
        result = self.current()
        if result is None:
            return
        theme.copy_to_clipboard(self.root, result.what())
        theme.toast(self.root, "Path copied.", tone="ok")

    # ------------------------------------------------------------------ index

    def add_default_folders(self) -> None:
        """Prepare the folders that everyone has, in one click.

        A new user has not added anything yet and therefore finds nothing by
        definition. This button adds Desktop, Documents, Downloads,
        Music, Pictures and Videos that exist, plus the browser
        profiles found for Chrome, Edge and Firefox. After that it shows what
        is going to be indexed, so that the next step is 'Update index'.
        """
        folders = sources.default_folders()
        added = 0
        for folder_path in folders:
            try:
                self.index.add_folder(str(folder_path))
                added += 1
            except Exception:  # noqa: BLE001 - one folder must not stop the rest
                continue

        profiles = sources.default_browser_profiles()
        browsers = [name for name, items in profiles.items() if items]
        if browsers:
            # There is one checkbox for all browser history; the browsers
            # we found we switch on and mention in the overview.
            self.source_vars["browser"].set(True)
        self.source_vars["documenten"].set(True)
        self.source_vars["mail"].set(True)

        self._refresh_state()
        self._show_default_overview(folders, added, browsers)

    def add_whole_computer(self) -> None:
        """Prepare the whole computer as a source: all fixed drives.

        This is the big button: everything readable on every fixed drive. The
        first round takes a long time, but it can be interrupted and afterwards it
        continues where it left off.
        """
        drives = sources.computer_folders()
        if not drives:
            theme.show_info(self.root,
                            "I could not find a fixed drive to search.")
            return
        listing = "\n".join(f"  • {drive}" for drive in drives)
        if not theme.show_confirm(
                self.root,
                "VindTerug will then search the whole computer:\n\n"
                f"{listing}\n\n"
                "System folders (Windows, Program Files, ProgramData) and junk "
                "such as caches, temporary files and node_modules are "
                "skipped.\n\n"
                "The first round takes the longest — count on half an hour to "
                "a few hours, depending on what is on the drive. "
                "After that it is much faster, because only what has changed "
                "is read again.\n\n"
                "You can click 'Stop' in between: what is done stays "
                "saved, and next time VindTerug continues where it "
                "left off.\n\nContinue?"):
            return
        added = 0
        for drive in drives:
            try:
                self.index.add_folder(str(drive))
                added += 1
            except Exception:  # noqa: BLE001 - one drive must not stop the rest
                continue
        self.source_vars["documenten"].set(True)
        self._refresh_state()
        self.status.set(f"{added} drive(s) prepared",
                        "click 'Update index' to start")
        theme.toast(self.root,
                    "The whole computer is ready. Click 'Update index'.",
                    tone="ok")

    def _show_default_overview(self, folders, added: int,
                               browsers: list[str]) -> None:
        """Show what is going to be indexed from these folders."""
        lines = [
            "These folders are now ready:",
            "",
        ]
        for folder_path in folders:
            lines.append(f"  • {folder_path}")
        if browsers:
            lines.append("")
            lines.append("Browser history that is ticked: "
                         + ", ".join(browsers) + ".")
        else:
            lines.append("")
            lines.append("No browser profiles from Chrome, Edge or Firefox "
                         "found — that source is off.")
        lines += [
            "",
            "What will be indexed in a moment:",
            "  • documents (text, Word, Excel, pdf, music)",
            "  • e-mail (.eml and .mbox)",
            "  • the browser history ticked above",
            "",
            "Now click 'Update index'. That happens in the background; "
            "the window keeps working as usual.",
            "",
            "Everything stays on this pc. VindTerug sends nothing and needs "
            "no internet.",
        ]
        self._fill(self.fragment_box, "\n".join(lines))
        self._fill(self.text_box,
                   "The full text of a result appears here.")
        self.detail_title.configure(text="First steps")
        self._show_badge(f"{added} folders ready", "ok")
        self.detail_where.configure(text="")
        self.status.set(f"{added} default folders added",
                        "click 'Update index'")
        theme.toast(self.root,
                    f"{added} folders added. Click 'Update index'.",
                    tone="ok")

    def add_folder(self) -> None:
        """Let the user choose a folder and remember it as a source."""
        chosen = filedialog.askdirectory(
            parent=self.root, title="Which folder should VindTerug search?")
        if not chosen:
            return
        self.index.add_folder(chosen)
        self._refresh_state()
        self.status.set(f"Folder added: {chosen}", "ready to index")

    def remove_folder(self) -> None:
        """Remove the chosen folder (and what was indexed from it)."""
        selection = self.folder_list.curselection()
        if not selection:
            theme.show_info(self.root, "Choose a folder in the list first.")
            return
        folder_path = self.folder_list.get(selection[0])
        if not theme.show_confirm(
                self.root,
                f"Are you sure?\n\n{folder_path}\n\n"
                "The documents from this folder disappear from the index. "
                "The files themselves stay where they are."):
            return
        self.index.remove_folder(folder_path)
        self._refresh_state()
        self.status.set(f"Folder removed from the index: {folder_path}")

    def clear_index(self) -> None:
        """Throw away the whole index."""
        if self.busy:
            theme.show_info(self.root, "Wait until indexing is done.")
            return
        if not theme.show_confirm(
                self.root,
                "The whole index will be cleared. Your files stay where they are, "
                "but everything has to be read again.\n\nContinue?"):
            return
        self.index.clear()
        self.tree.delete(*self.tree.get_children())
        self.by_item.clear()
        self.results = []
        self._refresh_state()
        self._show_first_steps()
        theme.toast(self.root, "The index has been cleared.", tone="ok")

    def show_help(self) -> None:
        """Tell exactly what is and is not indexed."""
        theme.show_text_dialog(
            self.root, "What VindTerug does and does not index",
            "\n".join([
                "IS indexed (from the folders you add yourself):",
                "  • text files: .txt .md .csv .log .json .html .py and so on",
                "  • Word, Excel and PowerPoint (.docx .xlsx .pptx)",
                "  • pdfs that contain text; for scans of embedded JPEGs "
                "VindTerug tries OCR",
                "  • music (.mp3 .wav .flac .m4a .ogg .wma .aac .aiff .opus): "
                "artist, title, album, genre and year from the tag, or otherwise "
                "from the file name",
                "  • e-mail: .eml and .mbox (subject, sender, date, content)",
                "  • screenshots and photos — only the text in them, via OCR, "
                "and only if you tick that",
                "  • browser history: address, title and time of your visits",
                "",
                "NOT indexed:",
                "  • system folders and hidden junk (Windows, AppData, "
                "cache, node_modules, build and so on)",
                "  • file types that are not in the list above",
                "  • pdfs without a text layer (a scan without OCR)",
                "  • the content of attachments to a mail",
                "",
                "What is saved:",
                "  • per document the title, the path, the date and the text, in "
                f"{core.default_index_path()}",
                "  • the full text of every document, so that the "
                "detail panel can show it",
                "",
                "Nothing goes to the internet. No account is needed and nothing "
                "is sent.",
            ]))

    # -- indexing in the background ------------------------------------------

    def _chosen_sources(self) -> dict[str, object]:
        """What the user has ticked, in the form core expects."""
        folders = self.index.folders()
        return {
            "documenten": folders if self.source_vars["documenten"].get() else [],
            "mail": bool(self.source_vars["mail"].get()),
            "screenshots": bool(self.source_vars["screenshots"].get()),
            "browser": bool(self.source_vars["browser"].get()),
        }

    def start_index(self) -> None:
        """Start an indexing run in a background thread."""
        if self.busy:
            theme.show_info(self.root, "An indexing run is already going.")
            return
        choice = self._chosen_sources()
        if not choice["documenten"] and not any(
                (choice["mail"], choice["screenshots"], choice["browser"])):
            theme.show_info(
                self.root,
                "Add a folder first and tick what should be "
                "indexed.")
            return

        self.busy = True
        self.index_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.pack(side="right", padx=(0, 10))
        self.progress.configure(value=0)
        pending = self.index.queue_count()
        if pending:
            self.status.set(
                f"Continuing the previous round: {pending} items to go",
                "what is already done stays saved")
        else:
            self.status.set("Indexing…")

        indexer = core.Indexer(self.index)
        self.running_indexer = indexer
        threading.Thread(target=self._work, args=(indexer, choice),
                         daemon=True).start()
        self.root.after(120, self._poll)

    def _work(self, indexer: core.Indexer, choice: dict) -> None:
        """The background thread: index and report the progress."""
        try:
            indexer.index(choice)
        except Exception as exc:  # noqa: BLE001 - the GUI must keep working
            self.queue.put(("error", str(exc)))
            return
        self.queue.put(("done", None))

    def _poll(self) -> None:
        """Collect the progress (called with ``root.after``)."""
        while True:
            try:
                kind, content = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind == "done":
                self._finish_indexing()
                return
            if kind == "error":
                self.busy = False
                self.running_indexer = None
                self.index_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.progress.pack_forget()
                self.status.set("Indexing stopped with an error.")
                theme.show_error(self.root, f"Indexing failed:\n{content}")
                return

        indexer = self.running_indexer
        if indexer is not None and self.busy:
            progress = indexer.voortgang
            self.progress.configure(value=progress.fraction * 100)
            self.status.set(progress.status_line() if progress.status_line() else "Working…",
                            progress.source)
        if self.busy:
            self.root.after(200, self._poll)

    def _finish_indexing(self) -> None:
        """Tidy up after indexing is done."""
        indexer = self.running_indexer
        self.busy = False
        self.running_indexer = None
        self.index_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.progress.pack_forget()
        self._refresh_state()

        if indexer is not None and indexer.voortgang.cancelled:
            pending = indexer.voortgang.pending
            self.status.set(
                "Indexing stopped.",
                (f"{pending} items saved to continue with"
                 if pending else "your results are saved"))
            theme.toast(
                self.root,
                ("Indexing stopped. What is done is saved; click 'Update "
                 "index' to continue." if pending
                 else "Indexing stopped."),
                tone="warn")
            return
        if indexer is not None:
            progress = indexer.voortgang
            self.status.set(
                f"Done: {progress.added} new, "
                f"{progress.updated} updated, "
                f"{progress.skipped} unchanged"
                + (f", {progress.errors} skipped" if progress.errors else "")
                + (f" · {progress.documents} documents looked at"
                   if progress.documents else ""),
                "last updated " + (self.index.state().last_updated.strftime("%d-%m-%Y %H:%M")
                                   if self.index.state().last_updated else "unknown"))
            theme.toast(self.root, "The index has been updated.", tone="ok")
        self._show_start_or_results()

    def stop_index(self) -> None:
        """Ask the background thread to stop."""
        if self.running_indexer is not None:
            self.running_indexer.stop()
            self.status.set("Stopping…", "the running files are still being finished")
            self.stop_button.configure(state="disabled")

    # ------------------------------------------------------------------ close

    def close(self, _event=None) -> None:
        """Close the index neatly before the window disappears."""
        if self.busy:
            if not theme.show_confirm(
                    self.root,
                    "Indexing is still running. Do you want to stop now?"):
                return
            if self.running_indexer is not None:
                self.running_indexer.stop()
        try:
            self.index.close()
        except Exception:  # noqa: BLE001 - closing must never complain
            pass
        self.root.destroy()


def build_ui(root: tk.Tk, *_args, **_kwargs) -> VindTerugApp:
    """Called by ``theme.run_app``."""
    app = VindTerugApp(root)
    root.protocol("WM_DELETE_WINDOW", app.close)
    return app


def main() -> int:
    return theme.run_app(APP_NAME, build_ui, geometry="1200x780",
                         min_size=(1020, 640))
