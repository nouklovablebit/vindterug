"""VindTerug — meaning-based search in your own files, mail and history.

VindTerug builds one local index over documents, e-mail, screenshots and
browser history, and searches it by meaning instead of by file name.
You type "that mail about that tender in March" and get the document
back, without having to know what it is called or where it is.

Everything stays on this pc: no network, no account, no subscription. The index
lives by default in ``%LOCALAPPDATA%/VindTerug/index.db``.

This module is the public face of the package: what is here is what another
module or a test needs.

Usage::

    from vindterug import Indexer, VindTerugIndex, Searcher

    with VindTerugIndex() as index:
        Indexer(index).index({"documenten": ["C:/Documenten"]})
        results, explanation = Searcher(index).search("that mail about the tender")
"""

from __future__ import annotations

from . import muziek, ocr, query, sources, tekst, theme, tijd
from .core import (
    BROWSER_SOURCES,
    Chunk,
    Document,
    Indexer,
    IndexFout,
    IndexProgress,
    IndexState,
    SearchResult,
    SourceDocument,
    Searcher,
    SearchExplanation,
    VindTerugIndex,
    chrome_time_to_datetime,
    document_from_file,
    document_from_visit,
    split_into_chunks,
    default_index_path,
)
from .query import Query, QueryKind, parse_query

__version__ = "1.0"

# The name as it appears in the window and in the status bar.
APP_NAME = "VindTerug"

__all__ = [
    "APP_NAME",
    "__version__",
    "BROWSER_SOURCES",
    "Chunk",
    "Document",
    "Indexer",
    "IndexFout",
    "IndexProgress",
    "IndexState",
    "Query",
    "QueryKind",
    "SearchResult",
    "SearchExplanation",
    "SourceDocument",
    "Searcher",
    "VindTerugIndex",
    "chrome_time_to_datetime",
    "default_index_path",
    "document_from_file",
    "document_from_visit",
    "index_text_dir",
    "parse_query",
    "split_into_chunks",
    # submodules die samen het publieke oppervlak vormen
    "muziek",
    "ocr",
    "query",
    "sources",
    "tekst",
    "theme",
    "tijd",
]
