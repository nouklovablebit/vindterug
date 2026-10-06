"""Core logic of VindTerug: building the index and searching in it.

This file contains no GUI code and does not talk to the user. It manages one
SQLite file holding:

* ``documents`` — one row per source (file, mail, visit, screenshot) with
  where it is, what it is called, what kind it is and from when;
* ``chunks`` — the text of that document, cut into pieces so the result can
  show a usable fragment;
* ``chunks_fts`` — an FTS5 table over ``chunks.tekst`` for searching;
* ``meta`` — bookkeeping (when the index was last updated).

Re-indexing is cheap: a file whose size and modification time have stayed the
same is skipped.

The search function is the heart of the program. It combines three signals:

1. FTS5 with BM25 ranking, holding the words the user typed and their
   synonyms (those synonyms weigh less);
2. a fuzzy supplement with :func:`difflib.get_close_matches` on the words that
   occur in the index, so a typo does not immediately yield nothing;
3. optionally embeddings through a local Ollama, as an extra signal.

That gives one ranking, with an explanation per result ("hit on: aanbesteding
(title), maart 2025 (date)").
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from . import ocr
from . import query as query_module
from . import sources
from . import tekst as text_module

__all__ = [
    "Document",
    "Chunk",
    "SearchResult",
    "IndexState",
    "IndexProgress",
    "Indexer",
    "SearchExplanation",
    "VindTerugIndex",
    "SourceDocument",
    "IndexFout",
    "default_index_path",
    "index_dir",
    "text_dir",
    "split_into_chunks",
    "document_from_file",
    "document_from_visit",
    "chrome_time_to_datetime",
    "firefox_time_to_datetime",
    "BROWSER_SOURCES",
]

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

MIN_CHUNK = 400          # lower bound: smaller pieces we glue together
MAX_CHUNK = 900          # upper bound: bigger we cut off (FTS and readability)
MAX_TEXT = 400_000       # per document we store no more than this
APP_DIR = "VindTerug"
INDEX_FILE = "index.db"

# How much does a hit in which field weigh?
WEIGHT_CHUNK = 1.0      # the text itself
WEIGHT_TITLE = 3.0      # title or file name: a hit here counts heavier
WEIGHT_EXTRA = 1.5      # sender, subject, path, url
TITLE_BOOST = 0.35       # extra bonus per title word you searched for
RECENT_BOOST = 0.15      # slight preference for more recent documents
FUZZY_BOOST = 0.25       # contribution of a spelling suggestion
EMBED_BOOST = 0.5        # maximum contribution of embeddings
URL_BOOST = 0.4          # average similarity of the url with the search words

# Words that cause a syntax error as an FTS5 search query (AND, OR, NOT, NEAR).
FTS_SPECIAL = {"and", "or", "not", "near"}

# Columns of the FTS table. The weights per column are in ``_search_fts``.
FTS_COLUMNS = ("tekst", "titel", "extra", "woorden")

# Which browsers we can read, with the label that goes into the interface.
BROWSER_SOURCES: dict[str, str] = {
    "browser:chrome": "Browser history of Chrome",
    "browser:edge": "Browser history of Edge",
    "browser:firefox": "Browser history of Firefox",
    "browser:alles": "Browser history (all browsers)",
}


def text_dir(indexpath: str | Path | None = None) -> Path:
    """The folder with the full text of each document (for the detail panel)."""
    path = Path(indexpath) if indexpath else default_index_path()
    folder_path = path.parent / "tekst"
    folder_path.mkdir(parents=True, exist_ok=True)
    return folder_path


def index_dir(indexpath: str | Path | None = None) -> Path:
    """The folder holding the index file (created if needed)."""
    path = Path(indexpath) if indexpath else default_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.parent


def chrome_time_to_datetime(value: int | float | None) -> dt.datetime | None:
    """Chrome/Edge time (microseconds since 1601-01-01 **UTC**) to a moment.

    The result is deliberately a naive UTC time, not local time: the stored
    value is epoch time and so timezone-independent. That way the answer does
    not depend on the timezone of the pc the program happens to run on, and the
    same data is the same on every machine. Only the display (the date column
    in the results list) goes to local time, through :func:`date_to_local`.

    The conversion itself lives in :mod:`vindterug.sources`; here we only pass
    it on, so there is just one implementation.
    """
    return sources.chrome_time_to_datetime(value)


def firefox_time_to_datetime(value: int | float | None) -> dt.datetime | None:
    """Firefox time (microseconds since 1970 **UTC**) to a naive UTC time.

    Same choice as in :func:`chrome_time_to_datetime`; see there.
    """
    return sources.firefox_time_to_datetime(value)


def _now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _iso(moment: dt.datetime | None) -> str | None:
    return moment.replace(microsecond=0).isoformat(sep=" ") if moment else None


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _shorten(path: str, width: int = 70) -> str:
    """Shorten a path for the explanation, without losing the file name."""
    if len(path) <= width:
        return path
    return "…" + path[-(width - 1):]


def _unique(row: Sequence[str]) -> list[str]:
    """De-duplicate words in order, lower case."""
    seen: set[str] = set()
    result: list[str] = []
    for word in row:
        low = (word or "").lower().strip()
        if low and low not in seen:
            seen.add(low)
            result.append(low)
    return result


def _safe_search_term(term: str) -> str:
    """Prevent FTS5 syntax errors (AND, OR, NOT, NEAR are reserved).

    FTS5 does not read ``"and"`` as an ordinary word: it is an operator. A term
    that collides with an operator therefore takes precedence over the full
    text above; here we escape with an explicit column hint.
    """
    if term.lower() in FTS_SPECIAL:
        return f'"{term}"'
    return term


# --------------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------------

@dataclass
class Document:
    """One source in the index: a file, mail, screenshot or website visit."""

    source: str
    path: str
    title: str = ""
    kind: str = ""
    mtime: dt.datetime | None = None
    size: int = 0
    extra: dict = field(default_factory=dict)
    id: int = 0
    n_chunks: int = 0

    @property
    def name(self) -> str:
        """Display name: file name, or the last piece of a url."""
        if self.path and "://" in self.path:
            return self.path.rstrip("/").rsplit("/", 1)[-1] or self.path
        return Path(self.path).name if self.path else self.title

    @property
    def is_web(self) -> bool:
        return bool(self.path and "://" in self.path)

    @property
    def is_mail(self) -> bool:
        return self.kind == "mail"

    @property
    def folder(self) -> str:
        """The folder of this document (or the site for a visit)."""
        if self.is_web:
            return self.path.split("://", 1)[0] + "://" + self.path.split("/")[2] if self.path.count("/") >= 2 else self.path
        return str(Path(self.path).parent) if self.path else ""

    def display_title(self) -> str:
        return self.title or self.name

    @property
    def name(self) -> str:
        return self.doc.name

    @property
    def title(self) -> str:
        return self.doc.display_title()

    @property
    def kind(self) -> str:
        return self.doc.kind

    @property
    def path(self) -> str:
        return self.doc.path

    @property
    def date(self) -> dt.datetime | None:
        return self.doc.mtime

    def date_text(self) -> str:
        moment = self.date
        return moment.strftime("%d-%m-%Y") if moment else "unknown"


def date_to_local(moment: dt.datetime | None) -> dt.datetime | None:
    """Convert a stored moment to local time, for display.

    Browser history is stored in UTC (see :func:`chrome_time_to_datetime`); on
    screen you want to see the time it was here and now. Moments that already
    carry a timezone are left alone.
    """
    if moment is None:
        return None
    if moment.tzinfo is not None:
        return moment.astimezone()
    return moment.replace(tzinfo=dt.timezone.utc).astimezone()


# --------------------------------------------------------------------------
# Where is the index?
# --------------------------------------------------------------------------

def default_index_path() -> Path:
    """The default path of the index: %LOCALAPPDATA%/VindTerug/index.db.

    Without LocalAppData (in a test or on another machine, for example) it
    falls back to the home folder, so the program always works.
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / APP_DIR / INDEX_FILE
    return Path.home() / f".{APP_DIR}" / INDEX_FILE


class IndexFout(RuntimeError):
    """The index could not be opened or updated."""


# --------------------------------------------------------------------------
# Indexing: what do the sources yield?
# --------------------------------------------------------------------------

@dataclass
class SourceDocument:
    """One document that is ready to go into the index."""

    source: str = "map"
    path: str = ""
    title: str = ""
    kind: str = "tekst"
    mtime: dt.datetime | None = None
    size: int = 0
    text: str = ""
    extra: dict = field(default_factory=dict)
    error: str = ""
    # The time of the file on the disk itself. For most documents that is the
    # same as ``mtime``, but not for mail: there ``mtime`` is the sent date
    # (which we want to be able to search back) while the file can have a very
    # different time. To decide whether anything changed, it is the file time
    # we need — otherwise every mail is read again every round, which saves
    # hours on a full disk.
    file_mtime: dt.datetime | None = None

    @property
    def date(self) -> dt.datetime | None:
        """Best date for this source: mtime for files, visit for the rest."""
        if self.mtime is not None:
            return self.mtime
        raw = (self.extra or {}).get("datum")
        if isinstance(raw, str) and raw:
            try:
                return dt.datetime.fromisoformat(raw)
            except ValueError:
                return None
        return None

    def full_text(self) -> str:
        """The text that is searched: title, address details and content.

        Besides the text itself, the fields a searcher types or filters on go
        along too: the url of a visit, the sender and subject of a mail, and
        the names of the attachments. Otherwise you cannot find a mail back by
        the name of the sender or by an attachment.
        """
        parts = [self.title, self.text]
        for key in ("url", "host", "afzender", "van", "aan", "cc",
                    "onderwerp", "browser"):
            value = (self.extra or {}).get(key)
            if value:
                parts.append(str(value))
        attachments = (self.extra or {}).get("bijlagen")
        if isinstance(attachments, (list, tuple)):
            parts.append(" ".join(str(b) for b in attachments))
        elif attachments:
            parts.append(str(attachments))
        return "\n".join(p for p in parts if p)


def document_from_visit(visit, *, source: str) -> SourceDocument:
    """Turn one browser visit into an indexable document."""
    title = visit.title or visit.url or "(untitled)"
    extra = {
        "url": visit.url,
        "host": visit.host,
        "browser": visit.browser,
        "datum": visit.moment.isoformat(sep=" ") if visit.moment else "",
        "bezoeken": str(visit.visits),
    }
    return SourceDocument(
        source=source,
        path=visit.url,
        title=title,
        kind="browser",
        mtime=visit.moment,
        size=0,
        text=sources.text_of_visit(visit),
        extra=extra,
    )


def document_from_file(path: str | Path, *, source: str = "map",
                       ocr_method: str = "auto") -> SourceDocument:
    """Read one file and turn it into an indexable document.

    Never breaks on a single file: if reading fails, the reason ends up in
    ``error`` and the text stays empty.
    """
    p = Path(path)
    doc = SourceDocument(source=source, path=str(p), title=p.name, kind="tekst")
    try:
        stat = p.stat()
    except OSError as exc:
        doc.error = f"could not read the file: {exc}"
        return doc

    doc.mtime = dt.datetime.fromtimestamp(stat.st_mtime)
    doc.file_mtime = doc.mtime
    doc.size = stat.st_size
    ext = p.suffix.lower()

    try:
        if ext in sources.IMAGE_EXTENSIONS:
            doc.kind = "image"
            outcome = ocr.ocr_image(p, method=ocr_method)
            doc.text = outcome.text
            doc.extra["ocr_methode"] = outcome.method
            if outcome.error:
                doc.error = outcome.error
        elif ext in sources.PDF_EXTENSIONS:
            doc.kind = "pdf"
            extraction = sources.extract_pdf(p)
            doc.text, doc.error = extraction.text, extraction.error
            doc.extra.update(extraction.extra or {})
        elif ext in sources.OFFICE_EXTENSIONS:
            doc.kind = "office"
            extraction = sources.extract_file(p)
            doc.text, doc.error = extraction.text, extraction.error
        elif ext in sources.MAIL_EXTENSIONS:
            doc.kind = "mail"
            extraction = sources.extract_mail_file(p)
            doc.text, doc.error = extraction.text, extraction.error
            doc.extra.update(extraction.extra or {})
            if extraction.title:
                doc.title = extraction.title
            # A mail has two dates: that of the file on disk and that on which
            # it was sent. For searching by time the second is the right one;
            # it goes along as ``datum`` and wins when weighing.
            moment = (extraction.extra or {}).get("moment")
            if isinstance(moment, str) and moment:
                doc.extra["datum"] = moment
                try:
                    doc.mtime = dt.datetime.fromisoformat(moment)
                except ValueError:
                    pass
        elif ext in sources.MUSIC_EXTENSIONS:
            doc.kind = "muziek"
            extraction = sources.extract_music(p)
            doc.text, doc.error = extraction.text, extraction.error
            doc.extra.update(extraction.extra or {})
            if extraction.title:
                doc.title = extraction.title
        else:
            extraction = sources.extract_text_file(p)
            doc.text, doc.error = extraction.text, extraction.error
            doc.title = extraction.title or doc.title
    except Exception as exc:  # noqa: BLE001 - one broken file must never stop everything
        doc.error = f"could not read this file: {exc}"
    return doc


# --------------------------------------------------------------------------
# Bookkeeping and progress
# --------------------------------------------------------------------------

@dataclass
class IndexState:
    """How full and how fresh is the index?"""

    documents: int = 0
    fragments: int = 0
    last_updated: dt.datetime | None = None
    sources: dict[str, int] = field(default_factory=dict)
    skipped: int = 0
    errors: int = 0

    @property
    def is_empty(self) -> bool:
        return self.documents == 0

    def summary(self) -> str:
        """The line at the bottom of the window: "X documents, Y fragments, ..."."""
        if self.is_empty:
            return "no index yet — add a folder and click 'Update index'."
        when = (self.last_updated.strftime("%d-%m-%Y %H:%M")
                if self.last_updated else "unknown")
        return (f"{self.documents} documents, {self.fragments} fragments, "
                f"last updated {when}")


@dataclass
class IndexProgress:
    """Progress of one indexing round (the answer of ``Indexer``)."""

    done: bool = False
    total: int = 0
    completed: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    errors: int = 0
    current: str = ""
    message: str = ""
    source: str = ""
    cancelled: bool = False
    # Counters for a round over a whole disk or disk part:
    # ``documents`` counts the files that were looked at in this round,
    # ``saved`` how many of those are really in the index already (the
    # interim saving), ``pending`` how many folders are still to go, and
    # ``resumed`` whether this round continued an earlier round.
    documents: int = 0
    saved: int = 0
    pending: int = 0
    resumed: bool = False

    @property
    def fraction(self) -> float:
        """Progress between 0 and 1 (measured in items, not in files)."""
        if not self.total:
            return 0.0
        return max(0.0, min(1.0, self.completed / self.total))

    def status_line(self) -> str:
        """Short status line while indexing, for the status bar."""
        pieces: list[str] = []
        if self.total > 1:
            pieces.append(f"{self.completed} of {self.total} items")
        if self.documents:
            pieces.append(f"{self.documents} documents")
        for count, word in ((self.added, "new"),
                            (self.updated, "updated"),
                            (self.skipped, "unchanged"),
                            (self.removed, "removed"),
                            (self.errors, "skipped")):
            if count:
                pieces.append(f"{count} {word}")
        if self.saved:
            pieces.append(f"{self.saved} saved in between")
        if self.pending:
            pieces.append(f"{self.pending} items to go")
        return " · ".join(pieces)


class StopIndexing(Exception):
    """Internal exception to break off an indexing run neatly."""


# --------------------------------------------------------------------------
# The pieces of text a document consists of
# --------------------------------------------------------------------------

@dataclass
class Chunk:
    """A piece of text of a document."""

    ord: int = 0
    text: str = ""


def split_into_chunks(text: str, *, min_length: int = MIN_CHUNK,
                      max_length: int = MAX_CHUNK) -> list[str]:
    """Cut text into readable pieces of roughly ``min_length`` characters.

    We prefer to cut on an empty line (paragraph), then on a line and only as
    a last resort in the middle of a line. Pieces stay under ``max_length`` so
    the fragment in the results list stays readable.
    """
    flat = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not flat:
        return []
    if len(flat) <= max_length:
        return [flat]

    pieces: list[str] = []
    rest = flat
    while rest:
        if len(rest) <= max_length:
            pieces.append(rest)
            break
        bound = min(len(rest), min_length)
        window = rest[:max_length]
        cut = max(window.rfind("\n\n"), window.rfind("\n"),
                  window.rfind(". "), window.rfind("; "))
        if cut < bound:
            # No neat boundary: cut on the last space in the window.
            cut = window.rfind(" ")
        if cut < bound:
            cut = max_length
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    return [p for p in pieces if p]


# --------------------------------------------------------------------------
# The index file itself
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id       INTEGER PRIMARY KEY,
    source   TEXT NOT NULL,
    path     TEXT NOT NULL,
    title    TEXT,
    kind     TEXT,
    mtime    REAL,
    size     INTEGER,
    extra    TEXT,
    woorden  TEXT DEFAULT '',
    fout     TEXT DEFAULT '',
    ronde    INTEGER NOT NULL DEFAULT 0,
    bron_mtime REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_source_path
    ON documents(source, path);
CREATE INDEX IF NOT EXISTS idx_documents_mtime ON documents(mtime);

CREATE TABLE IF NOT EXISTS chunks (
    doc_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord      INTEGER NOT NULL,
    text     TEXT NOT NULL,
    PRIMARY KEY (doc_id, ord)
);

CREATE VIRTUAL TABLE IF NOT EXISTS zoek_fts USING fts5(
    tekst,
    titel,
    extra,
    woorden,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS scan_wachtrij (
    id           INTEGER PRIMARY KEY,
    bron         TEXT NOT NULL,
    omschrijving TEXT NOT NULL DEFAULT '',
    soort        TEXT NOT NULL DEFAULT 'map',
    pad          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scan_wachtrij_bron ON scan_wachtrij(bron);
"""


def _document_words(text: str) -> list[str]:
    """All words of a document: normalised, stemmed, unique.

    This list is stored in ``documents.woorden`` and is the basis for the
    spelling suggestions (fuzzy search) and for the title boost.
    """
    result: list[str] = []
    for word in _unique(text_module.tokenise(text)):
        if len(word) < 2:
            continue
        result.append(text_module.unfold(word))
    return _unique(result)


def _title_words(title: str) -> list[str]:
    """The stemmed words of a title or file name."""
    return _document_words(title)


def _summary_field(text: str, *, limit: int = 16_000) -> str:
    """Title/url/sender for the FTS index: keep it short, do not let it grow.

    A title or url is never thousands of characters; the bound exists only so
    a weird outlier does not blow up the index.
    """
    return (text or "")[:limit]


class VindTerugIndex:
    """The index: documents, fragments and searching in them.

    The class is usable without a GUI and without a network. Everything slow
    (reading files, OCR) happens in :class:`Indexer`, which calls this class;
    that keeps the class itself predictable and easy to test.
    """

    def __init__(self, index_path: str | Path | None = None, *,
                 ocr_method: str = "auto") -> None:
        self.pad = Path(index_path) if index_path else default_index_path()
        self.ocr_methode = ocr_method
        self._owner = threading.get_ident()
        self.pad.parent.mkdir(parents=True, exist_ok=True)
        self.text_dir = self.pad.parent / "tekst"
        self.text_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._db = sqlite3.connect(str(self.pad))
        except sqlite3.Error as exc:
            raise IndexFout(f"the index could not be opened: {exc}") from exc
        self._db.row_factory = sqlite3.Row
        self._closed = False
        self._configure()
        try:
            self._db.executescript(SCHEMA)
        except sqlite3.Error as exc:  # pragma: no cover - only with broken SQLite
            raise IndexFout(f"the index could not be created: {exc}") from exc
        self._migrate()
        self._db.commit()

    def _configure(self) -> None:
        """Put SQLite in the setting in which it writes fastest.

        WAL lets reading and writing go side by side — you can keep searching
        while the indexing runs — and with synchronous=NORMAL a power cut costs
        at most the last bit of work. Without these lines SQLite waits for the
        disk after every write, and with tens of thousands of files that is the
        biggest brake of all.
        """
        for command in ("PRAGMA journal_mode=WAL",
                        "PRAGMA synchronous=NORMAL",
                        "PRAGMA temp_store=MEMORY",
                        "PRAGMA cache_size=-40000",
                        "PRAGMA busy_timeout=5000"):
            try:
                self._db.execute(command)
            except sqlite3.Error:   # a network folder cannot do WAL, for example
                continue

    def _migrate(self) -> None:
        """Make sure an index from an older version gets the new columns."""
        try:
            columns = {row["name"] for row in
                       self._db.execute("PRAGMA table_info(documents)")}
        except sqlite3.Error:
            return
        if "ronde" not in columns:
            try:
                self._db.execute(
                    "ALTER TABLE documents ADD COLUMN ronde INTEGER NOT NULL DEFAULT 0")
            except sqlite3.Error:  # pragma: no cover - only with a broken index
                pass
        if "bron_mtime" not in columns:
            try:
                self._db.execute("ALTER TABLE documents ADD COLUMN bron_mtime REAL")
            except sqlite3.Error:  # pragma: no cover - only with a broken index
                pass

    # -- shutting down neatly ----------------------------------------------

    def close(self) -> None:
        """Close the index neatly; calling it twice must do no harm.

        The interface closes the index when the window closes, and a second
        connection (from a worker thread) closes itself too. Without this check
        the second time would give an error on a connection that is already
        closed.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._db.commit()
        finally:
            self._db.close()

    def __enter__(self) -> "VindTerugIndex":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- state --------------------------------------------------------------

    def state(self) -> IndexState:
        """How many documents, how many fragments and when updated?"""
        state = IndexState()
        try:
            state.documents = int(self._db.execute(
                "SELECT COUNT(*) FROM documents").fetchone()[0])
            state.fragments = int(self._db.execute(
                "SELECT COUNT(*) FROM chunks").fetchone()[0])
            for row in self._db.execute(
                    "SELECT source, COUNT(*) AS n FROM documents GROUP BY source"):
                state.sources[str(row["source"])] = int(row["n"])
        except sqlite3.Error:
            return state
        raw = self.meta("laatst_bijgewerkt")
        state.last_updated = _parse_iso(raw)
        state.skipped = int(self.meta("overgeslagen") or 0)
        state.errors = int(self.meta("fouten") or 0)
        return state

    def meta(self, key: str, default: str = "") -> str:
        """Read a bookkeeping value (the last indexing run, for example)."""
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: object) -> None:
        self._db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))
        self._db.commit()

    # -- sources ------------------------------------------------------------

    def sources(self) -> dict[str, int]:
        """All sources that occur in the index, with their document counts."""
        result: dict[str, int] = {}
        for row in self._db.execute(
                "SELECT source, COUNT(*) AS n FROM documents GROUP BY source ORDER BY source"):
            result[str(row["source"])] = int(row["n"])
        return result

    def folders(self) -> list[str]:
        """The folders added as a source (from the bookkeeping)."""
        raw = self.meta("mappen", "")
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [str(p) for p in value] if isinstance(value, list) else []

    def add_folder(self, folder_path: str | Path) -> list[str]:
        """Remember a folder as a source. Returns the full list."""
        p = Path(folder_path).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        folders = self.folders()
        if str(p) not in folders:
            folders.append(str(p))
            self.set_meta("mappen", json.dumps(folders, ensure_ascii=False))
        return folders

    def remove_folder(self, folder_path: str | Path) -> list[str]:
        """Remove a folder as a source; the documents in it disappear too."""
        p = Path(folder_path)
        folders = [m for m in self.folders() if m != str(p)]
        self.set_meta("mappen", json.dumps(folders, ensure_ascii=False))
        self.remove_source(f"map:{p}")
        return folders

    def remove_source(self, source: str) -> int:
        """Delete all documents of one source. Returns the count."""
        rows = [int(r["id"]) for r in self._db.execute(
            "SELECT id FROM documents WHERE source = ?", (source,))]
        for doc_id in rows:
            self._delete_document(doc_id)
        self._db.commit()
        return len(rows)

    def clear(self, *, folders_too: bool = False) -> None:
        """Empty the whole index (the folder of the index itself stays)."""
        self._db.executescript(
            "DELETE FROM documents; DELETE FROM chunks; DELETE FROM zoek_fts;"
            "DELETE FROM meta; DELETE FROM scan_wachtrij;")
        self._db.commit()
        for file in self.text_dir.glob("*.txt"):
            try:
                file.unlink()
            except OSError:
                continue
        if folders_too:
            self.set_meta("mappen", "[]")

    # -- adding and removing documents --------------------------------------

    def document_id(self, source: str, path: str) -> int | None:
        """The id of a document, or ``None`` if it is not in the index yet."""
        row = self._db.execute(
            "SELECT id FROM documents WHERE source = ? AND path = ?",
            (source, path)).fetchone()
        return int(row["id"]) if row else None

    def document_mtime(self, source: str, path: str) -> tuple[float, int] | None:
        """The stored time and size of a document (for the skip check)."""
        row = self._db.execute(
            "SELECT mtime, size FROM documents WHERE source = ? AND path = ?",
            (source, path)).fetchone()
        if not row:
            return None
        return float(row["mtime"] or 0), int(row["size"] or 0)

    # -- several connections, interim state and resuming ---------------------

    def for_this_thread(self) -> "VindTerugIndex":
        """A connection that belongs to this thread.

        SQLite does not allow one connection to be used in two threads. The
        interface opens the index in the main thread and has the reading of
        files happen in a worker thread; that one gets its own connection to
        the same file here. Thanks to WAL you can keep searching in the
        meantime.
        """
        if threading.get_ident() == self._owner:
            return self
        return VindTerugIndex(self.pad, ocr_method=self.ocr_methode)

    def known_documents(self, source: str) -> dict[str, tuple[int, float, int]]:
        """What we already have from this source: path -> (id, file time, size).

        One query instead of two queries per file. On a disk with a hundred
        thousand files that saves a hundred thousand trips to the database —
        measured 9 times faster than asking per file.

        The time is the time of the file on disk (so not the sent date for
        mail): that shows whether anything changed. An index from an older
        version does not have that time yet; then it falls back to the date it
        does have.
        """
        result: dict[str, tuple[int, float, int]] = {}
        for row in self._db.execute(
                "SELECT id, path, COALESCE(bron_mtime, mtime) AS tijd, size"
                " FROM documents WHERE source = ?",
                (source,)):
            result[str(row["path"])] = (int(row["id"]), float(row["tijd"] or 0.0),
                                         int(row["size"] or 0))
        return result

    def mark_seen(self, doc_id: int, round_number: int) -> None:
        """Remember that this document was seen in this round."""
        self._db.execute("UPDATE documents SET ronde = ? WHERE id = ?",
                         (int(round_number), int(doc_id)))

    def new_round(self) -> int:
        """Start a new round and return the round number."""
        round_number = self.round_id() + 1
        self._db.execute(
            "INSERT INTO meta(key, value) VALUES('scan_ronde', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(round_number),))
        return round_number

    def round_id(self) -> int:
        """The number of the round running now (0 if there is none)."""
        try:
            return int(self.meta("scan_ronde", "0") or 0)
        except ValueError:
            return 0

    def commit_batch(self, *, interim: int | None = None) -> None:
        """Record what has been done so far: the interim saving.

        After this line the work of the past documents is really in the index
        — also if the program is closed afterwards or the power fails.
        ``interim`` is the number of documents stored now; that number goes
        into the bookkeeping so the interface can show it.
        """
        try:
            self._db.commit()
        except sqlite3.Error:
            return
        if interim is None:
            return
        try:
            self.set_meta("scan_opgeslagen", int(interim))
            self.set_meta("scan_bijgewerkt", _now_iso())
        except sqlite3.Error:
            pass

    # -- the queue: this is where an interrupted round carries on ------------

    def fill_queue(self, items: Sequence[tuple[str, str, str, str]],
                   *, round_number: int | None = None) -> int:
        """Set up the work for a round: (source, description, kind, path)."""
        self._db.execute("DELETE FROM scan_wachtrij")
        self._db.executemany(
            "INSERT INTO scan_wachtrij(bron, omschrijving, soort, pad)"
            " VALUES(?, ?, ?, ?)", items)
        self._db.commit()
        if round_number is None:
            round_number = self.new_round()
        try:
            self.set_meta("scan_status", "bezig")
            self.set_meta("scan_gestart", _now_iso())
            self.set_meta("scan_opgeslagen", 0)
        except sqlite3.Error:
            pass
        return len(items)

    def next_queue_item(self) -> dict | None:
        """The next item, or ``None`` when everything is done.

        The item stays in the queue until it is finished; that way nothing is
        lost when a round is interrupted.
        """
        row = self._db.execute(
            "SELECT id, bron, omschrijving, soort, pad FROM scan_wachtrij"
            " ORDER BY id LIMIT 1").fetchone()
        return dict(row) if row else None

    def queue_item_done(self, number: int) -> None:
        """This item is done; take it out of the queue."""
        self._db.execute("DELETE FROM scan_wachtrij WHERE id = ?", (int(number),))

    def queue_count(self, source: str = "") -> int:
        """How many items are still to go (of one source or of everything)."""
        if source:
            row = self._db.execute(
                "SELECT COUNT(*) FROM scan_wachtrij WHERE bron = ?",
                (source,)).fetchone()
        else:
            row = self._db.execute(
                "SELECT COUNT(*) FROM scan_wachtrij").fetchone()
        return int(row[0]) if row else 0

    def clear_queue(self) -> None:
        """Throw away the remaining work: the next round starts afresh."""
        self._db.execute("DELETE FROM scan_wachtrij")
        self._db.commit()
        try:
            self.set_meta("scan_status", "")
        except sqlite3.Error:
            pass

    def clean_up_source(self, source: str, round_number: int) -> int:
        """Remove documents that were not seen again in this round.

        Only call this when *every* item of that source is done: on a half-run
        source it would make documents disappear that are still there.
        """
        rows = [int(r["id"]) for r in self._db.execute(
            "SELECT id FROM documents WHERE source = ? AND ronde != ?",
            (source, int(round_number)))]
        for doc_id in rows:
            self._delete_document(doc_id)
        self._db.commit()
        return len(rows)

    def documents_by_ids(self, ids: Sequence[int]) -> dict[int, dict]:
        """The data of a series of documents, by id, fetched in one go.

        The searcher uses this to read the documents belonging to an FTS hit;
        one query instead of one per hit keeps searching fast.
        """
        numbers = [int(i) for i in ids]
        if not numbers:
            return {}
        place = ",".join("?" for _ in numbers)
        result: dict[int, dict] = {}
        for row in self._db.execute(
                "SELECT id, source, path, title, kind, mtime, extra, woorden, fout"
                f" FROM documents WHERE id IN ({place})", numbers):
            result[int(row["id"])] = {
                "id": int(row["id"]),
                "source": str(row["source"] or ""),
                "path": str(row["path"] or ""),
                "title": str(row["title"] or ""),
                "kind": str(row["kind"] or ""),
                "mtime": row["mtime"],
                "extra": row["extra"],
                "woorden": str(row["woorden"] or ""),
                "fout": str(row["fout"] or ""),
            }
        return result

    def is_current(self, doc: SourceDocument) -> bool:
        """May we skip this document because it has not changed?"""
        stored = self.document_mtime(doc.source, doc.path)
        if stored is None:
            return False
        mtime, size = stored
        new_time = doc.mtime.timestamp() if doc.mtime else 0.0
        if size != doc.size:
            return False
        return abs(mtime - new_time) < 1.0

    def store_document(self, doc: SourceDocument, *, round_number: int = 0) -> int:
        """Write one document (again) into the index. Returns the id.

        For an existing document all old data disappears first, so duplicate
        hits never arise. The FTS table is *not* an 'external content' table:
        that gives the fewest surprises when refilling it.
        """
        mtime = doc.mtime.timestamp() if doc.mtime else None
        file_mtime = (doc.file_mtime or doc.mtime)
        file_mtime = file_mtime.timestamp() if file_mtime else None
        title = doc.title or Path(doc.path).name or doc.path
        extra_text = " ".join(
            str(doc.extra.get(key, ""))
            for key in ("url", "afzender", "onderwerp", "aan", "host", "browser")
        ).strip()
        content = (doc.full_text() or "").strip()
        if len(content) > MAX_TEXT:
            content = content[:MAX_TEXT]
        words = " ".join(_document_words(f"{title} {extra_text} {content}"))

        cur = self._db.execute(
            "INSERT INTO documents(source, path, title, kind, mtime, size, extra,"
            " woorden, fout, ronde, bron_mtime)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source, path) DO UPDATE SET"
            " title = excluded.title, kind = excluded.kind, mtime = excluded.mtime,"
            " size = excluded.size, extra = excluded.extra,"
            " woorden = excluded.woorden, fout = excluded.fout,"
            " ronde = excluded.ronde, bron_mtime = excluded.bron_mtime",
            (doc.source, doc.path, title, doc.kind, mtime, doc.size,
             json.dumps(doc.extra or {}, ensure_ascii=False), words, doc.error,
             int(round_number), file_mtime))
        doc_id = int(cur.lastrowid) if cur.lastrowid else 0
        if not doc_id:
            doc_id = int(self.document_id(doc.source, doc.path) or 0)

        self._delete_chunks(doc_id)
        # One FTS row per document, with the cut-up text stuck behind it.
        # The text column starts empty: the first piece goes in straight away,
        # the rest follows after it.
        chunks = split_into_chunks(content)
        self._db.execute(
            "INSERT INTO zoek_fts(rowid, tekst, titel, extra, woorden)"
            " VALUES(?, ?, ?, ?, ?)",
            (doc_id, " ".join(chunks), _summary_field(title),
             _summary_field(extra_text), _summary_field(words)))
        for number, chunk in enumerate(chunks):
            self._db.execute(
                "INSERT INTO chunks(doc_id, ord, text) VALUES(?, ?, ?)",
                (doc_id, number, chunk))
        if content:
            self._store_text(doc_id, content)
        return doc_id

    def _store_text(self, doc_id: int, content: str) -> None:
        """Store the full text so the detail panel can show it."""
        try:
            (self.text_dir / f"{doc_id}.txt").write_text(content, encoding="utf-8")
        except OSError:
            pass

    def full_text(self, doc_id: int, *, limit: int = 60_000) -> str:
        """The stored text of a document (or what is in the index)."""
        file = self.text_dir / f"{doc_id}.txt"
        if file.exists():
            try:
                content = file.read_text(encoding="utf-8")
                return content if len(content) <= limit else content[:limit] + "\n…"
            except OSError:
                pass
        chunks = [str(r["text"]) for r in self._db.execute(
            "SELECT text FROM chunks WHERE doc_id = ? ORDER BY ord", (doc_id,))]
        content = "\n\n".join(chunks)
        return content if len(content) <= limit else content[:limit] + "\n…"

    def _delete_chunks(self, doc_id: int) -> None:
        self._db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        self._db.execute("DELETE FROM zoek_fts WHERE rowid = ?", (doc_id,))

    def _delete_document(self, doc_id: int) -> None:
        self._delete_chunks(doc_id)
        self._db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        try:
            (self.text_dir / f"{doc_id}.txt").unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Indexing: from folders and sources to rows in the index
# --------------------------------------------------------------------------

# How many files we walk past per step before we pass on the progress.
PROGRESS_EVERY = 25

# Interim saving: after so many documents (or so many seconds) the state really
# goes to disk. On a stop or a power cut at most a handful of work is then lost,
# and a round over a whole disk can continue from there afterwards instead of
# starting over.
BATCH_DOCUMENTS = 200
BATCH_SECONDS = 5.0

# The sources the user can tick.
DEFAULT_SOURCES: tuple[str, ...] = ("documenten", "mail", "screenshots", "browser")

SOURCE_LABELS: dict[str, str] = {
    "documenten": "Documents",
    "mail": "E-mail",
    "screenshots": "Screenshots",
    "browser": "Browser history",
}


def dir_to_source(source: str, folder_path: str | Path) -> str:
    """The source code under which one folder goes into the index."""
    return f"{source}:{Path(folder_path)}"


class Indexer:
    """Walks folders and sources and puts them into the index.

    This is the slow part of VindTerug: reading files, unpacking mail, OCR.
    That is why the class is kept apart from the interface: the GUI starts it
    in a background thread, reads the :class:`IndexProgress` in between and can
    break it off neatly with :meth:`stop`.

    A round is resumable. The work sits in a queue in the index itself, and the
    state goes to disk every few hundred documents. Stopping, closing or a power
    cut therefore never costs the work of a whole disk: the next round picks up
    the queue where it left off.

    The class never throws an exception outwards for one broken file: that ends
    up in ``progress.errors`` and the round carries on.
    """

    def __init__(self, index: VindTerugIndex, *, ocr_method: str = "auto",
                 batch_documents: int = BATCH_DOCUMENTS,
                 batch_seconds: float = BATCH_SECONDS) -> None:
        self.ix = index
        self.ocr_methode = ocr_method
        self.batch_documenten = max(1, int(batch_documents))
        self.batch_seconden = max(0.0, float(batch_seconds))
        self.voortgang = IndexProgress()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sinds_batch = 0
        self._laatste_batch = 0.0
        # The index's own files (database, WAL, stored texts) must never end up
        # in the index itself. We recognise only those files, without asking for
        # the path of every file: the database name and the folder with stored
        # texts.
        self._eigen_db = os.path.normcase(index.pad.name)
        self._eigen_tekst = os.path.normcase(str(index.text_dir))

    # -- stopping -----------------------------------------------------------

    def stop(self) -> None:
        """Ask for a stop; the running round breaks off at the next file."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- the round itself ---------------------------------------------------

    def index(self, sources_choice: dict[str, object] | None = None, *,
              again: bool = False) -> IndexProgress:
        """Update the index according to the chosen sources.

        ``sources_choice`` is what the interface ticked, for example::

            {"documenten": ["C:/Documenten"], "mail": True,
             "screenshots": False, "browser": ["chrome", "firefox"]}

        Without ``sources_choice`` only the list of folders from the bookkeeping is
        walked again. If there is still work from a previous round (because you
        stopped or closed the program), this round continues there; with
        ``again=True`` it starts with an empty queue instead.

        If someone calls :meth:`stop` while this round is already running, it
        breaks off as soon as the current file is done. What has been done by
        then is already in the index; the rest stays in the queue.
        """
        # The index is opened in the thread the interface has; the real work
        # happens here, so this thread gets its own connection on the same file
        # (SQLite does not allow two threads on one connection).
        index = self.ix.for_this_thread()
        own_connection = index is not self.ix
        self._readers = self._reader_count()
        self._pool = ThreadPoolExecutor(max_workers=self._readers,
                                        thread_name_prefix="vindterug-lezen")
        try:
            if self._stop.is_set():
                return self._finish(index)
            self._start_round(index, sources_choice, again=again)
            while not self._stop.is_set():
                item = index.next_queue_item()
                if item is None:
                    break
                self._process_item(index, item)
            return self._finish(index)
        finally:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
            index.commit_batch()
            if own_connection:
                index.close()

    def _start_round(self, index: VindTerugIndex, sources_choice: dict[str, object] | None,
                     *, again: bool) -> None:
        """Prepare the work: continue with what was open, or start over."""
        pending = 0 if again else index.queue_count()
        if pending:
            resumed = True
        else:
            planning = self._plan(index, sources_choice)
            pending = index.fill_queue(planning)
            resumed = False
        with self._lock:
            self.voortgang = IndexProgress(total=pending, completed=0,
                                           pending=pending, resumed=resumed)
            self.voortgang.source = "continue with the previous round" if resumed else "preparing"
        self._sinds_batch = 0
        self._laatste_batch = time.monotonic()

    def _plan(self, index: VindTerugIndex,
              sources_choice: dict[str, object] | None) -> list[tuple[str, str, str, str]]:
        """Decide what has to be done: (source, description, kind, path).

        Each item is one folder that has to be walked. They go into the index's
        queue, so a broken-off round continues at the same point later instead
        of starting over.

        ``kind`` says what is looked for in that folder: ``bestanden``,
        ``bestanden-zonder-mail`` (if mail is indexed separately, so a mail is
        not counted twice), ``mail``, ``beeld`` or ``browser``.
        """
        sources_choice = sources_choice or {}
        folders = [str(p) for p in (sources_choice.get("documenten") or [])] or index.folders()
        result: list[tuple[str, str, str, str]] = []
        mail_on = bool(sources_choice.get("mail"))
        folder_kind = "bestanden-zonder-mail" if mail_on else "bestanden"
        for folder_path in folders:
            path = Path(folder_path)
            if not path.is_dir():
                continue
            result.append((dir_to_source("map", path), str(path), folder_kind, str(path)))
        if mail_on:
            for folder_path in folders:
                path = Path(folder_path)
                if path.is_dir():
                    result.append(("mail:bestanden", f"mail in {path.name or path}",
                                   "mail", str(path)))
        if sources_choice.get("screenshots"):
            for folder_path in folders:
                path = Path(folder_path)
                if path.is_dir():
                    result.append(("screenshots:bestanden",
                                   f"screenshots in {path.name or path}",
                                   "beeld", str(path)))
        choice = sources_choice.get("browser")
        if choice:
            browsers = ([str(k) for k in choice] if isinstance(choice, (list, tuple, set))
                        else ["chrome", "edge", "firefox"])
            for browser in browsers:
                result.append((f"browser:{browser}", f"browser history ({browser})",
                               "browser", browser))
        return result

    # -- sources from the file system ---------------------------------------

    def _documents_in_folder(self, source: str, root: Path,
                             kind: str) -> Iterable[SourceDocument]:
        """All usable files under one folder, one by one.

        Nothing is put into a list beforehand: the indexing starts straight
        away, and memory stays the same size whether the folder holds ten or a
        million files.
        """
        for path, size, moment in self._walk(root, kind=kind):
            yield SourceDocument(source=source, path=str(path), title=path.name,
                                 kind=sources.kind_of_file(path),
                                 size=size, mtime=moment, file_mtime=moment)

    def _visits_of(self, browser: str) -> list[SourceDocument]:
        """The browser history of one browser as indexable documents."""
        result: list[SourceDocument] = []
        profiles: list[Path] = []
        if browser in ("chrome", "edge"):
            profiles = sources.find_chrome_profiles(browser)
        elif browser == "firefox":
            profiles = sources.find_firefox_profiles()
        for profile in profiles:
            try:
                visits = (sources.read_firefox_history(profile) if browser == "firefox"
                          else sources.read_chrome_history(profile))
            except Exception:  # noqa: BLE001 - one broken profile must not stop everything
                continue
            for visit in visits:
                result.append(document_from_visit(visit, source=f"browser:{browser}"))
        return result

    def _walk(self, root: Path, *,
              kind: str) -> Iterable[tuple[Path, int, dt.datetime]]:
        """Walk a folder and return every usable file.

        Iterative, with ``os.scandir``, and with pruning: a folder that is
        skipped is not even opened. That is the biggest speed-up of the whole
        program — measured with :mod:`benchmark_indexering` on this pc: 6,900
        to 22,000 files per second, against 1,400 to 3,200 with the old way,
        which first asked for and sorted *all* paths before anything happened.
        On the home folder that old way even ran so long that there was no
        measurement left to make.

        The size and time of every file come from the folder read itself; an
        extra lookup per file is not needed (measured: no difference). Photos
        and screenshots do not come past here: they cost almost a second each
        (OCR) and belong to the 'screenshots' tick.
        """
        stack = [root]
        seen: set[tuple[int, int]] = set()
        while stack:
            if self._stop.is_set():
                return
            folder_path = stack.pop()
            try:
                content = os.scandir(folder_path)
            except OSError:
                continue
            with content:
                for item in content:
                    if self._stop.is_set():
                        return
                    try:
                        is_folder = item.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    name = item.name
                    if is_folder:
                        if name.lower() in sources.SKIPPED_FOLDERS:
                            continue
                        if self._is_index_dir(item.path):
                            continue
                        try:
                            info = item.stat(follow_symlinks=False)
                        except OSError:
                            stack.append(Path(item.path))
                            continue
                        # A junction point can point to a folder we have already
                        # been to (C:\Documents and Settings -> C:\Users).
                        # Without this check a round can get stuck in a loop.
                        key = (int(getattr(info, "st_dev", 0) or 0),
                               int(getattr(info, "st_ino", 0) or 0))
                        if key != (0, 0):
                            if key in seen:
                                continue
                            seen.add(key)
                        stack.append(Path(item.path))
                        continue
                    if not self._include_file(name, kind):
                        continue
                    if self._is_index_file(item.path):
                        continue
                    try:
                        info = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if info.st_size > sources.MAX_FILE_SIZE:
                        continue
                    yield (Path(item.path), int(info.st_size),
                           dt.datetime.fromtimestamp(info.st_mtime))

    @staticmethod
    def _include_file(name: str, kind: str) -> bool:
        """May this file come along, given what we are looking for in this folder?

        Photos and screenshots only take part with the source ``beeld``. In a
        round over a whole disk that saves an enormous amount: reading such a
        file costs almost a second (OCR) against less than a millisecond for a
        text file. Measured on this pc: 40 images were together 32.6 of the
        32.8 seconds. That is how the tick "Screenshots (via OCR)" is also
        really a choice instead of a surprise.
        """
        if name.lower() in sources.SKIPPED_NAMES:
            return False
        ext = Path(name).suffix.lower()
        if kind == "mail":
            return ext in sources.MAIL_EXTENSIONS
        if kind == "beeld":
            return ext in sources.IMAGE_EXTENSIONS
        if kind == "bestanden-zonder-mail" and ext in sources.MAIL_EXTENSIONS:
            return False
        if ext in sources.IMAGE_EXTENSIONS:
            return False    # only through the 'screenshots' tick
        return sources.is_supported(name)

    def _is_index_file(self, path: str) -> bool:
        """Is this a file of the index itself?

        That must never be indexed along: the index would then grow with its
        own content and yield different files every round. If the index sits in
        a folder that is indexed, it is only about the database (with its
        helper files) and about the stored texts — not about the folder around
        it, because that holds the user's documents.
        """
        try:
            name = os.path.normcase(os.path.basename(path))
            parent = os.path.normcase(os.path.dirname(path))
        except (OSError, ValueError):  # pragma: no cover - only with weird paths
            return False
        if name.startswith(self._eigen_db):
            return True         # index.db, index.db-wal, index.db-shm
        return parent == self._eigen_tekst

    def _is_index_dir(self, path: str) -> bool:
        """Is this folder the folder with the index's own stored texts?"""
        try:
            return os.path.normcase(path) == self._eigen_tekst
        except (OSError, ValueError):  # pragma: no cover - only with weird paths
            return False

    @staticmethod
    def _reader_count() -> int:
        """How many files we read at the same time.

        Measured on this pc: with eight readers reading went twice as fast as
        one by one. More than eight yields nothing extra, and on a smaller
        machine this formula quietly takes it easier.
        """
        return max(1, min(8, os.cpu_count() or 2))

    # -- processing one source ----------------------------------------------

    def _process_item(self, index: VindTerugIndex, item: dict) -> None:
        """Do one queue item: a folder, mail folder or browser history."""
        source = str(item["bron"])
        description = str(item["omschrijving"] or source)
        kind = str(item["soort"] or "bestanden")
        round_number = index.round_id()
        with self._lock:
            self.voortgang.source = description
        try:
            if kind == "browser":
                self._handle_documents(index, source, round_number,
                                       self._visits_of(str(item["pad"])),
                                       needs_reading=False)
            else:
                root = Path(str(item["pad"]))
                self._handle_documents(index, source, round_number,
                                       self._documents_in_folder(source, root, kind))
        except Exception:  # noqa: BLE001 - one item must not stop the round
            with self._lock:
                self.voortgang.errors += 1
                self.voortgang.message = f"{description} could not be read"
        if self._stop.is_set():
            # This item stays in the queue; the next round continues here
            # instead of starting over.
            return
        index.queue_item_done(int(item["id"]))
        index.commit_batch()
        gone = 0
        if round_number and index.queue_count(source) == 0:
            # All items of this source are done; now we may tidy up what did
            # not come past any more in this round.
            gone = index.clean_up_source(source, round_number)
        with self._lock:
            self.voortgang.completed += 1
            self.voortgang.pending = max(0, self.voortgang.pending - 1)
            if gone:
                self.voortgang.removed += gone

    def _handle_documents(self, index: VindTerugIndex, source: str, round_number: int,
                          documents: Iterable[SourceDocument], *,
                          needs_reading: bool = True) -> None:
        """Put a series of documents into the index.

        Walking the folders and writing to the index happen in this thread;
        reading and unpacking the files happens in a small group of helper
        threads. On this pc that was measured to be twice as fast as doing
        everything neatly one after another, while never more than a handful of
        files sits in memory at the same time and the order stays the same.
        """
        known = index.known_documents(source)
        waiting: deque = deque()
        number = 0
        for prepared in documents:
            if self._stop.is_set():
                break
            number += 1
            stored = known.get(prepared.path)
            if stored is not None and self._unchanged(stored, prepared):
                index.mark_seen(stored[0], round_number)
                with self._lock:
                    self.voortgang.skipped += 1
                    self.voortgang.documents += 1
                    if number % PROGRESS_EVERY == 0:
                        self.voortgang.current = prepared.title or prepared.path
                self._after_document(index)
                continue
            future = (self._pool.submit(self._read, prepared)
                      if needs_reading else None)
            waiting.append((prepared, future))
            if len(waiting) >= self._readers:
                self._write_one(index, known, source, round_number, waiting.popleft())
        while waiting:
            if self._stop.is_set():
                break
            self._write_one(index, known, source, round_number, waiting.popleft())
        index.commit_batch()

    def _write_one(self, index: VindTerugIndex, known: dict, source: str,
                   round_number: int, pair: tuple[SourceDocument, object]) -> None:
        """Write one read document into the index."""
        prepared, future = pair
        existed = prepared.path in known
        try:
            document = future.result() if future is not None else prepared
        except Exception:  # noqa: BLE001 - one file must never stop everything
            document = SourceDocument(
                source=source, path=prepared.path, title=prepared.title,
                kind=prepared.kind, size=prepared.size, mtime=prepared.mtime,
                file_mtime=prepared.file_mtime,
                error="could not read this file")
        if document.file_mtime is None:
            document.file_mtime = prepared.file_mtime
        try:
            doc_id = index.store_document(document, round_number=round_number)
        except Exception:  # noqa: BLE001 - one document must not stop the round
            with self._lock:
                self.voortgang.errors += 1
            return
        key = document.file_mtime or document.mtime
        known[document.path] = (doc_id,
                                key.timestamp() if key else 0.0,
                                document.size)
        with self._lock:
            self.voortgang.documents += 1
            self.voortgang.current = document.title or document.path
            if existed:
                self.voortgang.updated += 1
            else:
                self.voortgang.added += 1
        self._after_document(index)

    @staticmethod
    def _unchanged(stored: tuple[int, float, int],
                   prepared: SourceDocument) -> bool:
        """Has this document not changed since the previous round?

        We compare the size and the time of the file on the disk — not the date
        in the index, because for mail that is the sent date and not the time
        of the file.
        """
        _doc_id, when, size = stored
        if size and prepared.size != size:
            return False
        moment = prepared.file_mtime or prepared.mtime
        if moment is None or not when:
            return False
        return abs(when - moment.timestamp()) < 1.0

    def _after_document(self, index: VindTerugIndex) -> None:
        """Commit the work if the batch is full or if the time is up.

        This is the interim saving: after this line everything done so far is
        really in the index. On a stop or a power cut at most a handful of
        documents is then lost, and the next round continues where this one
        stopped instead of starting over.
        """
        with self._lock:
            self._sinds_batch += 1
            full = self._sinds_batch >= self.batch_documenten
            processed = self.voortgang.documents
        now = time.monotonic()
        time_up = (bool(self.batch_seconden)
                   and (now - self._laatste_batch) >= self.batch_seconden)
        if not (full or time_up):
            return
        index.commit_batch(interim=processed)
        self._sinds_batch = 0
        self._laatste_batch = now
        with self._lock:
            self.voortgang.saved = processed

    def _read(self, prepared: SourceDocument) -> SourceDocument:
        """Fetch the text of one document."""
        if prepared.kind == "browser":
            return prepared
        read = document_from_file(prepared.path, source=prepared.source,
                                  ocr_method=self.ocr_methode)
        if read.size == 0 and prepared.size:
            read.size = prepared.size
        return read

    def _finish(self, index: VindTerugIndex | None = None) -> IndexProgress:
        """Write the bookkeeping away and return the final state.

        Only a round that has been completed in full may update the 'last
        updated' date and the counters: after a broken-off round the status line
        would otherwise promise more than the index holds. What *has* been done
        is already on disk — that is the interim saving.
        """
        index = index or self.ix
        with self._lock:
            self.voortgang.done = True
            self.voortgang.cancelled = self._stop.is_set()
            self.voortgang.message = ("stopped" if self.voortgang.cancelled
                                      else self.voortgang.current)
            progress = self.voortgang
        try:
            index.commit_batch()
            if progress.cancelled:
                index.set_meta("scan_status", "onderbroken")
            else:
                index.set_meta("scan_status", "")
                index.set_meta("laatst_bijgewerkt", _now_iso())
                index.set_meta("overgeslagen", progress.skipped)
                index.set_meta("fouten", progress.errors)
                index.set_meta("ronde_documenten", progress.documents)
            progress.pending = index.queue_count()
        except sqlite3.Error:
            pass
        return progress


# --------------------------------------------------------------------------
# Searching: from question to ranked results
# --------------------------------------------------------------------------

@dataclass
class SearchResult:
    """One search result, with everything the screen needs."""

    doc_id: int = 0
    source: str = ""
    path: str = ""
    title: str = ""
    kind: str = ""
    date: dt.datetime | None = None
    snippet: str = ""
    why: str = ""              # short reason for the list: "tender (title), March 2025 (date)"
    score: float = 0.0
    error: str = ""
    url: str = ""
    extra: dict = field(default_factory=dict)
    fragments: list[Chunk] = field(default_factory=list)

    def what(self) -> str:
        """What goes in the list under 'where is it': path or website."""
        return self.url or self.path


@dataclass
class SearchExplanation:
    """What the searcher did, in plain language (for under the search box)."""

    query: query_module.Query | None = None
    used_words: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)
    fuzzy: list[str] = field(default_factory=list)
    embeddings: str = "not used"
    window: str = ""
    count: int = 0
    duration_ms: int = 0

    def lines(self) -> list[str]:
        """Short explanation as a list of lines, for under the search box."""
        result: list[str] = []
        if self.query is not None:
            result.append(self.query.explanation)
        if self.synonyms:
            result.append("also searched for: " + ", ".join(self.synonyms[:8]))
        if self.fuzzy:
            result.append("spelling taken into account: " + ", ".join(self.fuzzy[:5]))
        if self.embeddings != "not used":
            result.append("embeddings: " + self.embeddings)
        if self.window:
            result.append(self.window)
        return result


# --- helpers for searching ---------------------------------------------------

def _safe_fts_word(word: str) -> str:
    """Make a word suitable for an FTS5 search query."""
    clean = re.sub(r'["\'^*():]', " ", word).strip()
    if not clean or clean.lower() in FTS_SPECIAL:
        return ""
    return '"' + clean.replace('"', '') + '"'


def _all_index_words(index: VindTerugIndex, *, limit: int = 60_000) -> list[str]:
    """The words that occur in the index (for spelling suggestions)."""
    result: list[str] = []
    try:
        for row in index._db.execute("SELECT woorden FROM documents LIMIT 4000"):
            for word in str(row["woorden"] or "").split():
                result.append(word)
                if len(result) >= limit:
                    return result
    except sqlite3.Error:
        return result
    return result


def _spelling_suggestions(words: Iterable[str], *,
                          source_words: Sequence[str] | None = None) -> list[str]:
    """Words in the index that look like what the user typed.

    Uses ``difflib.get_close_matches``: pure Python, no packages.
    """
    candidates = list(source_words or [])
    if not candidates:
        return []
    unique = _unique(candidates)
    result: list[str] = []
    for word in words:
        if not word or len(word) < 4:
            continue
        for hit in difflib.get_close_matches(word, unique, n=2, cutoff=0.84):
            if hit not in result and hit != word:
                result.append(hit)
    return result


def _fragment_around(text: str, words: Sequence[str], *,
                     width: int = 240) -> str:
    """A bit of text around the first search word found.

    If there is nothing in it, the start of the text comes back, so the results
    list never looks empty on a hit on title or date.
    """
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return ""
    positions: list[int] = []
    low = flat.lower()
    for word in words:
        if not word or len(word) < 3:
            continue
        start = low.find(word.lower())
        if start >= 0:
            positions.append(start)
    if not positions:
        return flat[:width] + ("…" if len(flat) > width else "")
    middle = min(positions)
    begin = max(0, middle - width // 3)
    end = min(len(flat), begin + width)
    piece = flat[begin:end]
    return ("…" if begin > 0 else "") + piece + ("…" if end < len(flat) else "")


# --- the searching itself ----------------------------------------------------

class Searcher:
    """Runs one search query on a :class:`VindTerugIndex`.

    Kept apart from the index so the GUI has to know nothing of SQLite and the
    tests only have to call ``search()``.
    """

    def __init__(self, index: VindTerugIndex) -> None:
        self.index = index

    # -- main route ---------------------------------------------------------

    def search(self, query_text: str, *, now: dt.datetime | None = None,
               limit: int = 40, embeddings: bool = False) -> tuple[list[SearchResult], SearchExplanation]:
        """Search by meaning and return the results in order of usefulness."""
        begin = dt.datetime.now()
        q = query_module.parse_query(query_text or "", now)
        explanation = SearchExplanation(query=q)
        if not q.has_search_words:
            explanation.duration_ms = int((dt.datetime.now() - begin).total_seconds() * 1000)
            return [], explanation

        raw_scores, found_words = self._match(q)
        explanation.used_words = found_words[:12]
        explanation.synonyms = [w for w in q.search_words if w not in q.terms][:10]

        if not raw_scores:
            # Nothing found: try it with spelling suggestions.
            suggestions = _spelling_suggestions(
                [text_module.unfold(t) for t in q.terms],
                source_words=_all_index_words(self.index))
            if suggestions:
                explanation.fuzzy = suggestions[:5]
                q2 = query_module.parse_query(query_text or "", now)
                q2.search_words = suggestions
                raw_scores, _ = self._match(q2)

        results = self._weigh(q, self._rows_for(raw_scores), limit)
        if embeddings:
            explanation.embeddings = self._embedding_status()
        explanation.count = len(results)
        explanation.duration_ms = int((dt.datetime.now() - begin).total_seconds() * 1000)
        if q.period is not None:
            explanation.window = f"filtered on {q.period.describe()}"
        return results, explanation

    # -- FTS5 ---------------------------------------------------------------

    def _match(self, q: query_module.Query) -> tuple[dict[int, float], list[str]]:
        """Search with FTS5 and BM25. Returns a raw score per document."""
        terms = [text_module.unfold(w) for w in q.terms]
        all_terms = list(dict.fromkeys(terms + list(q.search_words)))
        safe = [_safe_fts_word(w) for w in all_terms]
        safe = [w for w in safe if w]
        if not safe:
            return {}, []

        expression = " OR ".join(safe)
        scores: dict[int, float] = {}
        found: list[str] = []
        try:
            rows = self.index._db.execute(
                "SELECT rowid, bm25(zoek_fts, ?, ?, ?, ?) AS score FROM zoek_fts "
                "WHERE zoek_fts MATCH ? ORDER BY score LIMIT 400",
                (WEIGHT_CHUNK, WEIGHT_TITLE, WEIGHT_EXTRA, WEIGHT_CHUNK,
                 expression)).fetchall()
        except sqlite3.Error:
            rows = []
        # The hits sit on ``rowid`` of ``zoek_fts``; that is the place where the
        # document sits in the full-text table. Which document belongs there can
        # differ per index (a document that is indexed again gets a new id).
        # That is why we read the documents in one go and attach them here, with
        # a count as a check: if the number does not fit, we drop the hit instead
        # of attaching it to the wrong document.
        rows = self._attach_documents(rows)
        for row in rows:
            # SQLite's BM25 is negative: closer to 0 is better.
            score = -float(row["score"] or 0.0)
            scores[int(row["id"])] = score
        for i, patroon in enumerate(safe):
            if i < len(terms):
                found.append(terms[i])
        return scores, [t for t in found if t]

    def _attach_documents(self, rows) -> list[dict]:
        """Attach the FTS hits to their document data.

        Every document found yields one row; the document data is fetched in
        one query. If the number does not fit, nothing comes back: a missing
        result is better than a result on the wrong document.
        """
        rowids = [int(row["rowid"]) for row in rows]
        if not rowids:
            return []
        try:
            documents = self.index.documents_by_ids(rowids)
        except sqlite3.Error:
            return []
        result: list[dict] = []
        for rowid, raw in zip(rowids, rows):
            document = documents.get(rowid)
            if document is None:
                continue
            result.append({
                "id": rowid,
                "score": raw["score"],
                "source": document["source"],
                "path": document["path"],
                "title": document["title"],
                "kind": document["kind"],
                "mtime": document["mtime"],
                "extra": document["extra"],
                "fout": document["fout"],
            })
        return result

    # -- weighing and ranking -----------------------------------------------

    def _rows_for(self, scores: dict[int, float]) -> list[dict]:
        """Look up the document data belonging to a score per document."""
        if not isinstance(scores, dict) or not scores:
            return []
        documents = self.index.documents_by_ids(list(scores)[:800])
        rows: list[dict] = []
        for doc_id, score in scores.items():
            document = documents.get(int(doc_id))
            if document is None:
                continue
            rows.append({
                "id": int(doc_id),
                "score": score,
                "source": document["source"],
                "path": document["path"],
                "title": document["title"],
                "kind": document["kind"],
                "mtime": document["mtime"],
                "extra": document["extra"],
                "fout": document["fout"],
            })
        return rows

    # -- weighing and ranking -----------------------------------------------

    def _weigh(self, q: query_module.Query, rows: list[dict],
               limit: int) -> list[SearchResult]:
        """Turn the score per document into one ranking with explanation.

        The document data has already been looked up in :meth:`_match`; here we
        only weigh, filter and sort.
        """
        if not rows:
            return []
        rows = list(rows)[:800]
        asked = [text_module.unfold(t) for t in q.terms]
        searched = list(dict.fromkeys(asked + list(q.search_words)))
        results: list[SearchResult] = []
        now = dt.datetime.now()

        for row in rows:
            doc_id = int(row["id"])
            try:
                extra = json.loads(row["extra"] or "{}")
            except json.JSONDecodeError:
                extra = {}
            title = str(row["title"] or "")
            path = str(row["path"] or "")
            source_text = f"{title} {path} " + " ".join(
                str(extra.get(key, "")) for key in ("url", "onderwerp", "afzender", "host"))
            source_words = set(_document_words(source_text))

            score = float(row["score"] or 0.0)

            # 1. Title and file name bonus.
            title_hits = [w for w in asked if w in source_words]
            score += TITLE_BOOST * len(title_hits) * WEIGHT_TITLE

            # 2. Slight preference for more recent documents.
            date = _parse_iso(extra.get("datum")) or self._timestamp(row["mtime"])
            if date is not None:
                days = abs((now - date).days)
                score += RECENT_BOOST * (1.0 / (1.0 + days / 365.0))

            # 3. Url that looks like the query (extra signal for browser history).
            url = str(extra.get("url") or "")
            if url:
                score += URL_BOOST * self._url_similarity(url, searched)

            if row["fout"]:
                score *= 0.9          # a document that could not be read stays lower

            result = SearchResult(
                doc_id=doc_id,
                source=str(row["source"] or ""),
                path=path,
                title=title or path,
                kind=str(row["kind"] or ""),
                date=date,
                score=score,
                error=str(row["fout"] or ""),
                url=url,
                extra=extra,
            )
            result.fragments = self._fragments(doc_id)
            result.snippet = _fragment_around(
                self._best_fragment_text(result), searched)
            result.why = self._why(q, result, title_hits)
            results.append(result)

        results = self._filter(q, results)
        results.sort(key=lambda r: (-r.score, r.title.lower()))
        return results[:limit]

    def _timestamp(self, mtime: object) -> dt.datetime | None:
        if not mtime:
            return None
        try:
            return dt.datetime.fromtimestamp(float(mtime))
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    def _fragments(self, doc_id: int) -> list[Chunk]:
        return [Chunk(int(r["ord"]), str(r["text"])) for r in self.index._db.execute(
            "SELECT ord, text FROM chunks WHERE doc_id = ? ORDER BY ord LIMIT 40", (doc_id,))]

    @staticmethod
    def _best_fragment_text(result: SearchResult) -> str:
        """The fragment with the most search words, otherwise the first fragment."""
        if not result.fragments:
            return ""
        low = [f for f in result.fragments if f.text]
        if not low:
            return ""
        return max(low, key=lambda f: len(f.text)).text

    @staticmethod
    def _url_similarity(url: str, words: Sequence[str]) -> float:
        """How much do the words of the url look like what was searched (0..1)?"""
        parts = [d for d in re.split(r"[^a-z0-9]+", url.lower()) if len(d) > 2]
        if not parts or not words:
            return 0.0
        hits = sum(1 for w in words if any(w[:5] in d or d[:5] in w for d in parts))
        return min(1.0, hits / max(1, len(words)))

    def _filter(self, q: query_module.Query, results: list[SearchResult]) -> list[SearchResult]:
        """Apply the kind and date range filter."""
        result_list: list[SearchResult] = []
        for r in results:
            if q.kind is not query_module.QueryKind.ALL and not _kind_matches(q.kind, r):
                continue
            if q.period is not None:
                if r.date is None:
                    # Without a date we cannot check the range: the result stays
                    # in, but sinks in the ranking.
                    r.score *= 0.7
                elif not q.period.contains(r.date):
                    continue
            result_list.append(r)
        return result_list

    def _why(self, q: query_module.Query, r: SearchResult,
             title_hits: Sequence[str]) -> str:
        """The reason for the hit, in plain language."""
        parts: list[str] = []
        text = " ".join(f.text.lower() for f in r.fragments)
        title_words = source_title_words(r)
        for word in q.terms[:3]:
            if text_module.unfold(word) in title_words:
                parts.append(f"{word} (title)")
            elif word and word.lower() in text:
                parts.append(f"{word} (in the text)")
            else:
                for variant in text_module.meaning_variants(word):
                    if variant and variant.lower() in text:
                        parts.append(f"{word} → {variant} (in the text)")
                        break
        if q.period is not None:
            if r.date is not None:
                parts.append(f"{r.date.strftime('%d-%m-%Y')} falls in {q.period.describe()}")
            else:
                parts.append(f"date unknown, cannot be checked against {q.period.describe()}")
        if r.kind == "browser" and r.url:
            parts.append("website from the browser history")
        if not parts:
            parts.append("the text looks like your question")
        return ", ".join(parts)

    # -- embeddings (optional) ----------------------------------------------

    def _embedding_status(self) -> str:
        """Ask a local Ollama for embeddings; works even if there is none."""
        base, model = ollama_settings()
        if not base:
            return "not used (no local Ollama configured)"
        return f"not used (Ollama at {base} is not called by default)"


def source_title_words(result: SearchResult) -> set[str]:
    """The stemmed words of title and file name of a result."""
    return set(_document_words(f"{result.title} {result.path}"))


def _kind_matches(kind: query_module.QueryKind, r: SearchResult) -> bool:
    """Does this result belong to the requested kind?"""
    if kind is query_module.QueryKind.MAIL:
        return r.kind == "mail"
    if kind is query_module.QueryKind.IMAGE:
        return r.kind == "image"
    if kind is query_module.QueryKind.BROWSER:
        return r.kind == "browser" or bool(r.url)
    if kind is query_module.QueryKind.FILE:
        return r.kind != "browser"
    return True


# --------------------------------------------------------------------------
# Optional: embeddings through a local Ollama
# --------------------------------------------------------------------------

OLLAMA_BASE = "http://127.0.0.1:11434"
OLLAMA_MODEL = "nomic-embed-text"
OLLAMA_TIMEOUT = 4.0        # seconds; a slow Ollama must not hold up the app


def ollama_settings() -> tuple[str, str]:
    """Base url and model of a local Ollama (empty if the user sets nothing)."""
    base = os.environ.get("VINDTERUG_OLLAMA", "").strip()
    model = os.environ.get("VINDTERUG_OLLAMA_MODEL", OLLAMA_MODEL).strip()
    return base, model


def ollama_status(*, base: str | None = None, model: str = OLLAMA_MODEL) -> str:
    """Check whether a local Ollama is running. Never fails hard."""
    import urllib.error
    import urllib.request

    base = base or OLLAMA_BASE
    request = urllib.request.Request(f"{base.rstrip('/')}/api/tags", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT) as answer:
            raw = answer.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"no local Ollama reachable ({type(exc).__name__})"
    try:
        models = [m.get("name", "") for m in json.loads(raw).get("models", [])]
    except (json.JSONDecodeError, AttributeError):
        return "Ollama answered, but not with a list of models"
    if model and not any(m.startswith(model.split(":")[0]) for m in models):
        return f"Ollama is running, but model '{model}' is not among them"
    return f"Ollama is reachable on {base}"


def ollama_embedding(text: str, *, base: str = OLLAMA_BASE,
                     model: str = OLLAMA_MODEL) -> list[float] | None:
    """Ask for one embedding. Returns ``None`` if it does not work."""
    import urllib.error
    import urllib.request

    body = json.dumps({"model": model, "prompt": text[:4000]}).encode("utf-8")
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/embeddings", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT) as answer:
            raw = json.loads(answer.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    value = raw.get("embedding")
    if isinstance(value, list) and value:
        return [float(x) for x in value]
    return None
