"""Measurement: which way of indexing is the fastest?

This is the measuring tool behind the choices in :mod:`vindterug.core`. It walks
over real folders of this pc (read only, nothing is changed) and puts the
candidates side by side:

1. **Walking through folders** — the current way (``Path.rglob('*')`` plus
   sorting), ``os.walk``, and our own stack with ``os.scandir`` that skips
   folders we skip anyway right away instead of descending into them first.
2. **Tracking what is already in the index** — one SQL query per file (the
   current way) against reading everything into memory once.
3. **Writing to the index** — the SQLite defaults against WAL plus
   ``synchronous=NORMAL`` and committing every hundred documents.
4. **Reading and extracting text** — one by one against several threads.

Running: python benchmark_indexering.py [--max-items 40000] [--seconden 20]

The result is in the README; this script is there to repeat or check that
result.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vindterug import core, sources  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def choose_trees() -> list[tuple[Path, bool]]:
    """A few real trees of this pc, from small to large.

    The second field says whether the old way (``rglob``) can take part here
    fairly. On the home folder that one takes so long that there is no
    measurement left in it — and that is a result in itself, so we skip it
    there.
    """
    home = Path.home()
    candidates = [
        (Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "drivers", True),
        (home / "AppData" / "Local" / "Programs" / "Python", True),
        (Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32", True),
        (home, False),
    ]
    return [(path, fair) for path, fair in candidates if path.is_dir()]


def report(line: str) -> None:
    print(line, flush=True)


# --------------------------------------------------------------------------
# 1. Walking through folders
# --------------------------------------------------------------------------

def walk_current(root: Path, limit: int) -> tuple[int, int]:
    """As it happens now: fetch everything, sort, then filter."""
    seen = 0
    usable = 0
    try:
        candidates = sorted(root.rglob("*"))
    except OSError:
        return 0, 0
    for path in candidates:
        seen += 1
        if seen >= limit:
            break
        if not path.is_file():
            continue
        if path.name.lower() in sources.SKIPPED_NAMES:
            continue
        if any(part.lower() in sources.SKIPPED_FOLDERS
               for part in path.parts[:-1]):
            continue
        if sources.is_supported(path):
            usable += 1
    return seen, usable


def walk_oswalk(root: Path, limit: int) -> tuple[int, int]:
    """os.walk, with the folders to skip taken out of the list."""
    seen = 0
    usable = 0
    for base, folders, files in os.walk(root, onerror=lambda _f: None):
        folders[:] = [f for f in folders if f.lower() not in sources.SKIPPED_FOLDERS]
        for name in files:
            seen += 1
            if seen >= limit:
                return seen, usable
            if name.lower() in sources.SKIPPED_NAMES:
                continue
            if sources.is_supported(name):
                usable += 1
    return seen, usable


def walk_scandir(root: Path, limit: int, *, prune: bool = True,
                 with_stat: bool = False) -> tuple[int, int]:
    """Our own stack with os.scandir; skipped folders are not even opened."""
    seen = 0
    usable = 0
    stack = [root]
    while stack:
        folder_path = stack.pop()
        try:
            with os.scandir(folder_path) as content:
                for item in content:
                    seen += 1
                    if seen >= limit:
                        return seen, usable
                    name = item.name.lower()
                    try:
                        is_folder = item.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_folder:
                        if prune and name in sources.SKIPPED_FOLDERS:
                            continue
                        stack.append(item.path)
                        continue
                    try:
                        if not item.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if name in sources.SKIPPED_NAMES:
                        continue
                    if not sources.is_supported(name):
                        continue
                    if with_stat:
                        # The real indexing needs the size and date of every
                        # file; this measures what that costs extra.
                        try:
                            info = item.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if info.st_size > sources.MAX_FILE_SIZE:
                            continue
                    usable += 1
        except OSError:
            # A folder that cannot be read (permissions, vanished drive): skip it.
            continue
    return seen, usable


def measure_walking(trees: list[Path], limit: int) -> None:
    report("")
    report("1. Walking through folders  (higher is better: usable files per second)")
    report(f"{'tree':52s} {'way':10s} {'seen':>8s} {'usable':>10s} "
           f"{'seconds':>9s} {'per second':>12s}")
    for root, fair in trees:
        ways: list[tuple[str, object]] = [
            ("rglob", walk_current), ("os.walk", walk_oswalk),
            ("scandir", walk_scandir),
            ("scandir+stat", lambda p, l: walk_scandir(p, l, with_stat=True)),
        ]
        if not fair:
            ways = ways[1:]
        for name, function in ways:
            begin = time.perf_counter()
            seen, usable = function(root, limit)
            duration = time.perf_counter() - begin
            per_second = seen / duration if duration else 0
            short = str(root)
            if len(short) > 50:
                short = "…" + short[-49:]
            report(f"{short:52s} {name:10s} {seen:8d} {usable:10d} "
                   f"{duration:9.2f} {per_second:12.0f}")


# --------------------------------------------------------------------------
# 2 and 3. The index itself
# --------------------------------------------------------------------------

def make_index(names: list[str], *, fast: bool) -> core.VindTerugIndex:
    """An index in a temporary folder, with or without the fast settings."""
    folder_path = Path(tempfile.mkdtemp(prefix="vindterug-bench-"))
    index = core.VindTerugIndex(folder_path / "index.sqlite")
    if fast:
        index._db.executescript(
            "PRAGMA journal_mode=WAL;"
            "PRAGMA synchronous=NORMAL;"
            "PRAGMA temp_store=MEMORY;"
            "PRAGMA cache_size=-40000;")
    return index


def make_documents(index: core.VindTerugIndex, amount: int) -> list[core.SourceDocument]:
    """Documents that point at real files (or otherwise made-up text)."""
    text = ("this is a test document with a few words in it, "
            "enough to make a fragment out of it. ") * 40
    result: list[core.SourceDocument] = []
    for number in range(amount):
        result.append(core.SourceDocument(
            source="bench", path=f"C:/bench/doc-{number}.txt",
            title=f"document {number}", kind="tekst", size=len(text),
            mtime=None, text=text))
    return result


def measure_lookup(amount: int = 5000) -> None:
    """One SQL query per file against reading everything into memory once."""
    index = make_index([], fast=True)
    for doc in make_documents(index, amount):
        index.store_document(doc)
    index._db.commit()

    begin = time.perf_counter()
    for number in range(amount):
        path = f"C:/bench/doc-{number}.txt"
        if index.document_id("bench", path) is not None:
            index.document_mtime("bench", path)
    per_query = time.perf_counter() - begin

    begin = time.perf_counter()
    known = {str(row["path"]): (float(row["mtime"] or 0), int(row["size"] or 0))
             for row in index._db.execute(
                 "SELECT path, mtime, size FROM documents WHERE source = ?", ("bench",))}
    for number in range(amount):
        known.get(f"C:/bench/doc-{number}.txt")
    in_memory = time.perf_counter() - begin

    report("")
    report("2. Tracking what is already in the index  (lower is better)")
    report(f"   {amount} lookups through SQL   : {per_query:7.2f} s")
    report(f"   {amount} lookups in memory     : {in_memory:7.3f} s")
    if in_memory:
        report(f"   -> {per_query / max(in_memory, 1e-6):.0f} times faster")
    index.close()


def measure_writing(amount: int = 300, *, batch: int = 100) -> None:
    """Writing: default settings against WAL plus committing per batch."""
    documents = make_documents(None, amount)  # type: ignore[arg-type]

    index = make_index([], fast=False)
    begin = time.perf_counter()
    for doc in documents:
        index.store_document(doc)
    index._db.commit()
    slow = time.perf_counter() - begin
    slow_count = index.state().documents
    index.close()

    index = make_index([], fast=True)
    begin = time.perf_counter()
    for number, doc in enumerate(documents, start=1):
        index.store_document(doc)
        if number % batch == 0:
            index._db.commit()
    index._db.commit()
    fast = time.perf_counter() - begin
    fast_count = index.state().documents
    index.close()

    report("")
    report("3. Writing to the index  (higher is better; same number of documents?)")
    report(f"   default, committing in one go  : {slow:7.2f} s "
           f"({amount / slow:6.0f} documents/s, {slow_count} rows)")
    report(f"   WAL + committing per {batch}      : {fast:7.2f} s "
           f"({amount / fast:6.0f} documents/s, {fast_count} rows)")
    if slow > 0:
        report(f"   -> {slow / max(fast, 1e-6):.1f} times faster")


# --------------------------------------------------------------------------
# 4. Reading and extracting text
# --------------------------------------------------------------------------

def find_readfiles(amount: int = 150) -> list[Path]:
    """Small text files of this pc to read from."""
    result: list[Path] = []
    for root in (Path.home() / "AppData" / "Local" / "Programs",
                 Path(__file__).resolve().parent):
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if len(result) >= amount:
                return result
            try:
                if path.suffix.lower() not in sources.TEXT_EXTENSIONS:
                    continue
                if path.stat().st_size > 64 * 1024:
                    continue
            except OSError:
                continue
            if sources.is_supported(path):
                result.append(path)
    return result


def measure_reading(amount: int = 150) -> None:
    files = find_readfiles(amount)
    if len(files) < 10:
        report("")
        report("4. Reading: too few text files found to measure.")
        return

    begin = time.perf_counter()
    characters = 0
    for path in files:
        try:
            characters += len(sources.extract_file(path).text)
        except Exception:  # noqa: BLE001
            continue
    one = time.perf_counter() - begin

    begin = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2))) as pool:
        for outcome in pool.map(
                lambda p: len(sources.extract_file(p).text), files):
            characters += 0
    many = time.perf_counter() - begin

    report("")
    report(f"4. Reading {len(files)} real files  (lower is better)")
    report(f"   one by one   : {one:7.3f} s ({len(files) / one:6.0f} files/s)")
    report(f"   with threads : {many:7.3f} s ({len(files) / many:6.0f} files/s)")
    report(f"   CPU cores on this pc: {os.cpu_count()}")


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-items", type=int, default=40000,
                        help="stop a measurement after this many items (default 40000)")
    parser.add_argument("--documenten", type=int, default=300,
                        help="number of documents for the writing measurement")
    args = parser.parse_args()

    report(f"VindTerug — measurement on {os.cpu_count()} cores, "
           f"Python {sys.version.split()[0]}")
    measure_walking(choose_trees(), args.max_items)
    measure_lookup()
    measure_writing(args.documenten)
    measure_reading()
    report("")
    report("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
