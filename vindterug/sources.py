"""Get text out of all kinds of files, without external packages.

VindTerug has to be able to search through documents, mail, screenshots and
browser history. This file handles the first part: from a file to text.

Everything happens with the standard library. Office files are zip files
with XML in them, PDF is a bit of a puzzle, and browser history is SQLite. If
the extraction does not succeed, there is always an honest message back in
``Extraction.error`` — never silence, so the user knows why something is not
found.
"""

from __future__ import annotations

import ctypes
import email
import email.policy
import email.utils
import glob
import html
import json
import os
import re
import shutil
import sqlite3
import string
import tempfile
import zlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import muziek
from . import tekst
from . import tijd

__all__ = [
    "Extraction",
    "TEXT_EXTENSIONS",
    "OFFICE_EXTENSIONS",
    "MAIL_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "MUSIC_EXTENSIONS",
    "SUPPORTED_EXTENSIONS",
    "kind_of_file",
    "is_supported",
    "extract_file",
    "extract_text_file",
    "extract_music",
    "strip_html",
    "extract_docx",
    "extract_xlsx",
    "extract_pptx",
    "extract_pdf",
    "extract_eml",
    "extract_mbox",
    "extract_mail_file",
    "chrome_time_to_datetime",
    "firefox_time_to_datetime",
    "find_chrome_profiles",
    "find_firefox_profiles",
    "read_chrome_history",
    "read_firefox_history",
    "BrowserVisit",
    "MAX_TEXT",
    "MAX_FILE_SIZE",
    "drives",
    "computer_folders",
    "is_fixed_drive",
]

# How much text we keep per document. More is only ballast for searching; the
# full text stays in the original file.
MAX_TEXT = 400_000

# Files bigger than this we skip. A film, a database or an installation package
# yields no usable text, but would make a round over the whole computer take
# hours longer.
MAX_FILE_SIZE = 25 * 1024 * 1024

# How many bytes we read from an unpacked document. More text than ``MAX_TEXT``
# characters cannot get into the index anyway; for a huge spreadsheet or log file
# reading the rest only costs time.
MAX_READ_BYTES = 4 * MAX_TEXT

TEXT_EXTENSIONS: set[str] = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".xml",
    ".py", ".ini", ".cfg", ".conf", ".htm", ".html", ".xhtml", ".css",
    ".js", ".yml", ".yaml", ".rst", ".srt", ".vtt", ".sql",
}

OFFICE_EXTENSIONS: set[str] = {".docx", ".xlsx", ".pptx"}
MAIL_EXTENSIONS: set[str] = {".eml", ".mbox", ".mbx"}
IMAGE_EXTENSIONS: set[str] = {
    ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp",
}

PDF_EXTENSIONS: set[str] = {".pdf"}
MUSIC_EXTENSIONS: set[str] = set(muziek.MUSIC_EXTENSIONS)

SUPPORTED_EXTENSIONS: set[str] = (
    TEXT_EXTENSIONS | OFFICE_EXTENSIONS | MAIL_EXTENSIONS | IMAGE_EXTENSIONS
    | PDF_EXTENSIONS | MUSIC_EXTENSIONS
)

# Files you never want to index, even if they are in a chosen folder.
SKIPPED_NAMES = {
    "desktop.ini", "thumbs.db", "ehthumbs.db", ".ds_store", "iconcache.db",
}

# Folders we skip. This is the main speed-up of the whole program: a folder
# that is in here is not even opened. That keeps a round over the whole drive
# out of Windows, Program Files and all caches.
SKIPPED_FOLDERS = {
    # Junk from programs and development folders: nothing of yours is ever in here.
    "__pycache__", ".git", ".svn", "node_modules", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".gradle", ".m2", ".nuget",
    ".npm", ".cache", ".thumbnails", ".idea", ".vscode", "venv", ".venv",
    "site-packages", "dist", "build", "obj", "target",
    # Caches, temporary files and logs.
    "temp", "tmp", "cache", "caches", "cache2", "crashpad", "logs",
    "onedrivetemp", "softwaredistribution", "packages",
    # Windows itself.
    "windows", "winsxs", "system32", "syswow64", "servicing", "assembly",
    "driverstore", "catroot", "prefetch", "winsat", "installer",
    "$windows.~bt", "$windows.~ws", "windows.old", "config.msi", "$sysreset",
    "recovery", "perflogs", "msocache", "program files", "program files (x86)",
    "programdata", "$recycle.bin", "system volume information", "appdata",
    # Junction points of Windows itself. Without these lines a round through
    # "C:\" would end up at the same folders again via "Documents and Settings".
    "documents and settings", "all users", "default user",
}


@dataclass
class Extraction:
    """The result of reading one file.

    ``text`` is always usable (empty if there was nothing to read). ``error``
    is filled as soon as something failed; the GUI shows that with the
    document, so you know why a scan or an empty file yields nothing.
    """

    text: str = ""
    title: str = ""
    extra: dict = field(default_factory=dict)
    error: str = ""
    kind: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


# --------------------------------------------------------------------------
# What kind of file is this?
# --------------------------------------------------------------------------

def kind_of_file(path: str | Path) -> str:
    """Determine the kind of source: text, office, pdf, mail, image or unknown."""
    ext = Path(path).suffix.lower()
    if ext in TEXT_EXTENSIONS:
        return "tekst"
    if ext in OFFICE_EXTENSIONS:
        return "office"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in MAIL_EXTENSIONS:
        return "mail"
    if ext in MUSIC_EXTENSIONS:
        return "muziek"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return "onbekend"


def is_supported(path: str | Path) -> bool:
    """Can we get text out of this?"""
    ext = Path(path).suffix.lower()
    name = Path(path).name.lower()
    if name in SKIPPED_NAMES:
        return False
    if name == "history" or name == "places.sqlite":
        return True     # browser history is added as a source, not as a file
    return ext in SUPPORTED_EXTENSIONS


def _shorten(text_value: str) -> str:
    """Limit the amount of text we keep."""
    if len(text_value) <= MAX_TEXT:
        return text_value
    return text_value[:MAX_TEXT]


# --------------------------------------------------------------------------
# Plain text, HTML and XML
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_ENTITEIT_RE = re.compile(r"&[a-zA-Z#0-9]+;")


def strip_html(raw: str) -> str:
    """Take HTML tags, scripts and styles out and read the readable text that is left.

    Block tags become a line break, so paragraphs do not stick together.
    Entities like ``&eacute;`` and ``&#233;`` become ordinary letters, and the
    content of ``<title>`` comes on the first line at the top.
    """
    if not raw:
        return ""
    title = ""
    match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
    if match:
        title = html.unescape(_TAG_RE.sub(" ", match.group(1))).strip()

    clean = _SCRIPT_RE.sub(" ", raw)
    clean = re.sub(r"<br\s*/?>", "\n", clean, flags=re.IGNORECASE)
    clean = re.sub(r"</(p|div|li|tr|h[1-6]|section|article|header|footer)>",
                   "\n", clean, flags=re.IGNORECASE)
    clean = _TAG_RE.sub(" ", clean)
    clean = html.unescape(clean)
    clean = re.sub(r"[ \t\f\v]+", " ", clean)
    clean = re.sub(r"\n\s*\n+", "\n", clean)

    lines = [line.strip() for line in clean.splitlines()]
    junk = {"", "window.", "document."}
    lines = [line for line in lines if line not in junk]
    body = "\n".join(lines).strip()
    if title and title.lower() not in body.lower():
        return f"{title}\n{body}".strip()
    return body or title


def _decode(raw: bytes) -> tuple[str, str]:
    """Get text out of bytes; returns (text, encoding used)."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace"), "utf-16"
    # XML and HTML often say themselves what they are.
    head = raw[:400].decode("latin-1", "ignore").lower()
    match = re.search(r'encoding=["\']([\w\-]+)["\']', head)
    if match:
        name = match.group(1)
        try:
            return raw.decode(name, "replace"), name
        except LookupError:
            pass
    for encoder in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoder), encoder
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8"


def extract_text_file(path: str | Path) -> Extraction:
    """Read an ordinary text file, and strip HTML if it is a web page."""
    p = Path(path)
    raw = p.read_bytes()[: 4 * MAX_TEXT]
    content, encoding = _decode(raw)

    ext = p.suffix.lower()
    if ext in (".htm", ".html", ".xhtml"):
        content = strip_html(content)
    if ext == ".json":
        # Making JSON readable is not necessary, but it is nice for fragments.
        try:
            content = json.dumps(json.loads(content), ensure_ascii=False, indent=1)
        except (ValueError, TypeError):
            pass

    title = _title_from_content(p, content)
    return Extraction(text=_shorten(content), title=title, kind="tekst",
                      extra={"codering": encoding})


def _title_from_content(path: Path, content: str) -> str:
    """Pick a title: the first line, or otherwise the file name."""
    for line in content.splitlines():
        line = line.strip().lstrip("#").strip()
        if len(line) >= 4 and not line.startswith(("{", "<", "[")):
            return line[:120]
    return path.stem


# --------------------------------------------------------------------------
# Office: docx, xlsx and pptx are zip files with XML
# --------------------------------------------------------------------------

_XML_TEKST_RE = re.compile(r"<[^>]+>")


def _xml_text(fragment: str, *, new_line: bool = True) -> str:
    """All text between XML tags, with ``<w:p>``/``<a:p>`` as a line break."""
    if not fragment:
        return ""
    if new_line:
        fragment = re.sub(r"</(w:p|a:p|w:tr)>", "\n", fragment)
    fragment = re.sub(r"<w:tab\b[^>]*/>", "\t", fragment)
    fragment = re.sub(r"<w:br\b[^>]*/>", "\n", fragment)
    text_value = _XML_TEKST_RE.sub("", fragment)
    return html.unescape(text_value)


def _read_zip(path: Path, name: str, *, max_bytes: int | None = None) -> str:
    """The content of one file from a zip, without reading all of it.

    A .docx or .pptx is a zip with XML in it. Of a huge document we read only
    the start: the rest cannot get into the index anyway.
    """
    if max_bytes is None:
        max_bytes = MAX_READ_BYTES
    with zipfile.ZipFile(path) as archive:
        with archive.open(name) as source:
            raw = source.read(max_bytes)
    return raw.decode("utf-8", "replace")


def _zip_names(path: Path, filter_string: str) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return sorted(n for n in archive.namelist()
                      if n.startswith(filter_string) and n.endswith(".xml"))


def extract_docx(path: str | Path) -> Extraction:
    """Text from a Word document (.docx)."""
    p = Path(path)
    content = _read_zip(p, "word/document.xml")
    body = _xml_text(content)

    # Headers and footers count too; those are often forgotten.
    for name in _zip_names(p, "word/header") + _zip_names(p, "word/footer"):
        try:
            extra = _xml_text(_read_zip(p, name)).strip()
        except (KeyError, zipfile.BadZipFile):
            continue
        if extra:
            body += "\n" + extra

    title = _title_from_content(p, body)
    return Extraction(text=_shorten(body.strip()), title=title, kind="office")


def extract_xlsx(path: str | Path) -> Extraction:
    """Text from an Excel workbook (.xlsx), including shared strings."""
    p = Path(path)
    with_archive = zipfile.ZipFile(p)
    with with_archive as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            raw = archive.read("xl/sharedStrings.xml")[:MAX_READ_BYTES].decode("utf-8", "replace")
            for piece in re.findall(r"<si>(.*?)</si>", raw, re.DOTALL):
                shared.append(_xml_text(piece, new_line=False))

        sheets = sorted(n for n in archive.namelist()
                        if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        parts: list[str] = []
        for sheet in sheets:
            # Of a huge sheet we read only the start: the text is cut off at
            # MAX_TEXT anyway, and this saves seconds per file on big
            # workbooks.
            raw = archive.read(sheet)[:MAX_READ_BYTES].decode("utf-8", "replace")
            lines: list[str] = []
            for row in re.findall(r"<row[^>]*>(.*?)</row>", raw, re.DOTALL):
                cells: list[str] = []
                for cell in re.findall(r"<c\b[^>]*?(?:/>|>.*?</c>)", row, re.DOTALL):
                    type_match = re.search(r't="(\w+)"', cell)
                    value = re.search(r"<v>(.*?)</v>", cell, re.DOTALL)
                    inline = re.search(r"<is>(.*?)</is>", cell, re.DOTALL)
                    if type_match and type_match.group(1) == "inlineStr" and inline:
                        cells.append(_xml_text(inline.group(1), new_line=False))
                    elif value:
                        raw_value = html.unescape(value.group(1))
                        if type_match and type_match.group(1) == "s":
                            try:
                                cells.append(shared[int(raw_value)])
                            except ValueError:
                                cells.append(raw_value)
                            except IndexError:
                                # Reference to a text that falls outside the read
                                # part: nothing sensible to show.
                                continue
                        elif type_match and type_match.group(1) == "str":
                            cells.append(raw_value)
                        else:
                            cells.append(raw_value)
                if any(c.strip() for c in cells):
                    lines.append(" | ".join(cells))
            if lines:
                parts.append(f"[{Path(sheet).stem}]\n" + "\n".join(lines))

    body = "\n\n".join(parts)
    title = _title_from_content(p, body)
    return Extraction(text=_shorten(body.strip()), title=title, kind="office",
                      extra={"bladen": len(parts)})


def extract_pptx(path: str | Path) -> Extraction:
    """Text from a PowerPoint presentation (.pptx), per slide."""
    p = Path(path)
    slide_names = _zip_names(p, "ppt/slides/slide")
    slide_names.sort(key=lambda n: int(re.findall(r"\d+", n)[-1]) if re.findall(r"\d+", n) else 0)
    parts: list[str] = []
    length = 0
    for number, name in enumerate(slide_names, start=1):
        if length >= MAX_TEXT:
            break       # more text than this cannot get into the index anyway
        try:
            raw = _read_zip(p, name)
        except (KeyError, zipfile.BadZipFile):
            continue
        pieces = []
        for paragraph in re.findall(r"<a:p>.*?</a:p>", raw, re.DOTALL):
            line = _xml_text(paragraph, new_line=False).strip()
            if line:
                pieces.append(line)
        if pieces:
            block = f"[slide {number}]\n" + "\n".join(pieces)
            parts.append(block)
            length += len(block)
    body = "\n\n".join(parts)
    title = ""
    if slide_names:
        first = re.search(r"<a:t>(.*?)</a:t>", _read_zip(p, slide_names[0]), re.DOTALL)
        if first:
            title = html.unescape(first.group(1)).strip()[:120]
    return Extraction(text=_shorten(body.strip()), title=title or p.stem,
                      kind="office", extra={"dia_s": len(parts)})


# --------------------------------------------------------------------------
# PDF: really read the text layer, and be honest if there is nothing in it
# --------------------------------------------------------------------------
#
# A pdf made by Word or an accounting program contains no readable letters but
# glyph codes (``<2B>``) that only become real characters through a ToUnicode
# CMap. That CMap itself is often in a FlateDecode stream. Without those two
# steps an invoice reads as "nothing", while the text is there. That is why
# this reader reads the objects, the streams, the fonts with their CMap and only
# then the text operators. If the text layer fails, we try the embedded JPEGs
# through OCR.

_PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_PDF_TEKST_RE = re.compile(rb"\((?:[^()\\]|\\.)*\)", re.DOTALL)
_PDF_HEX_RE = re.compile(rb"<([0-9A-Fa-f\s]+)>")
_PDF_TOON_RE = re.compile(rb"\((.*?)(?<!\\)\)\s*Tj", re.DOTALL)
_PDF_ARRAY_RE = re.compile(rb"\[(.*?)\]\s*TJ", re.DOTALL)
_PDF_ONTWIJK = {
    rb"\n": b"\n", rb"\r": b"\n", rb"\t": b"\t", rb"\b": b"\b",
    rb"\f": b"\f", rb"\(": b"(", rb"\)": b")", rb"\\": b"\\",
}

# A text series is only text if it really contains text operators. Without
# this check font programs and images would count as text.
_PDF_TEKSTOP_RE = re.compile(rb"(?:\bTj\b|\bTJ\b|\bT\*\b|\bTd\b|\bTD\b|\bTm\b "
                             rb"|\bBT\b|\bET\b|'|\")")
_PDF_INHOUD_RE = re.compile(
    rb"/Contents\s*(?:(\d+)\s+0\s+R|\[([^\]]*?)\])|/Filter\s*(?:\[\s*)?/FlateDecode")
_PDF_HEXSTROOM_RE = re.compile(rb"<([0-9A-Fa-f\s]+)>")


def _pdf_strip_escapes(raw: bytes) -> str:
    """Make text between brackets from a PDF readable."""
    out = bytearray()
    i = 0
    while i < len(raw):
        char = raw[i:i + 1]
        if char == b"\\" and i + 1 < len(raw):
            two = raw[i:i + 2]
            if two in _PDF_ONTWIJK:
                out += _PDF_ONTWIJK[two]
                i += 2
                continue
            octal = re.match(rb"\\([0-7]{1,3})", raw[i:])
            if octal:
                out.append(int(octal.group(1), 8) & 0xFF)
                i += 1 + len(octal.group(1))
                continue
            i += 1
            continue
        out += char
        i += 1
    return out.decode("latin-1", "replace")


def _pdf_hex_bytes(digits: bytes) -> bytes:
    """A hex series (``<0041>``) to bytes; strips whitespace and pads."""
    clean = re.sub(rb"\s+", b"", digits)
    if len(clean) % 2:
        clean += b"0"
    try:
        return bytes.fromhex(clean.decode("ascii"))
    except ValueError:
        return b""


def _pdf_decompress(raw: bytes) -> bytes:
    """Unpack a PDF stream, or give it back unchanged.

    Many streams are FlateDecode (zlib) or gzip; those we unpack. If that does
    not work, the stream is apparently not compressed and we use the bytes as
    they are. That is exactly what an uncompressed content stream needs: only a
    font program or an image do we recognise later by the absence of text
    operators.
    """
    for head in (raw, raw.lstrip(b"\r\n \t")):
        if not head:
            continue
        if head[:2] == b"\x1f\x8b":
            try:
                return zlib.decompress(head, 16 + zlib.MAX_WBITS)
            except zlib.error:
                continue
        try:
            return zlib.decompress(head)
        except zlib.error:
            continue
        try:
            return zlib.decompressobj().decompress(head)
        except zlib.error:
            continue
    return raw


def _pdf_objects(data: bytes) -> dict[int, bytes]:
    """The separate objects from a pdf, by number.

    We read the objects themselves and not just the streams: the CMap of a
    font hangs on the ``/ToUnicode`` object and the reference to the content
    stream is in the page object. Without those two we see the glyph codes but
    not which letters belong to them.
    """
    out: dict[int, bytes] = {}
    for match in re.finditer(rb"(?m)(?:^|[\s>])(\d+)\s+0\s+obj\b", data):
        number = int(match.group(1))
        end = data.find(b"endobj", match.end())
        if end == -1:
            end = len(data)
        out[number] = data[match.end():end]
    return out


def _pdf_stream_from(body: bytes) -> bytes:
    """The raw stream from an object (between ``stream`` and ``endstream``)."""
    match = re.search(rb"stream\r?\n(.*?)\r?\nendstream", body, re.DOTALL)
    return match.group(1) if match else b""


def _pdf_is_text_series(raw: bytes) -> bool:
    """Does this stream contain real text operators?

    A font program or an image also starts with bytes, so we only take a
    stream as a text series if it contains text operators. Otherwise we would
    index nonsense that is never in the document.
    """
    return b"BT" in raw and bool(_PDF_TEKSTOP_RE.search(raw))


def _pdf_text_streams(data: bytes, objects: dict[int, bytes]) -> list[bytes]:
    """All content streams of the document, unpacked.

    We look for the ``/Contents`` references of the pages; if that fails we
    fall back on all streams, so that an unusual pdf is still read. Streams
    without text operators we leave alone.
    """
    numbers: list[int] = []
    for match in re.finditer(rb"/Contents\s*(?:(\d+)\s+0\s+R|\[([^\]]*?)\])|/Filter\s*(?:\[\s*)?/FlateDecode", data):
        if match.group(1):
            numbers.append(int(match.group(1)))
        elif match.group(2) is not None:
            numbers.extend(int(n) for n in re.findall(rb"(\d+)\s+0\s+R", match.group(2)))

    out: list[bytes] = []
    seen: set[int] = set()
    for number in numbers:
        if number in seen:
            continue
        seen.add(number)
        body = objects.get(number)
        if not body:
            continue
        raw = _pdf_stream_from(body) or body
        if b"/FlateDecode" in body:
            raw = _pdf_decompress(raw)
        if raw and _pdf_is_text_series(raw):
            out.append(raw)

    if out:
        return out

    # Safety net: look in *all* streams, also without a /Contents reference.
    for match in _PDF_STREAM_RE.finditer(data):
        raw = _pdf_decompress(match.group(1))
        if raw and _pdf_is_text_series(raw):
            out.append(raw)
    return out


def _pdf_text_numbers(raw: bytes, cmap: dict[int, str], width: int) -> str:
    """Convert the glyph codes of one text series with the font's CMap.

    ``width`` is 1 for a simple font and 2 for a CID/Type0 font. Without a
    CMap the codes are latin-1 characters; that is exactly the old behaviour
    and keeps working well for simple pdfs.
    """
    if width == 2:
        codes = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)]
    else:
        codes = list(raw)
    if cmap:
        return "".join(cmap.get(code, "") for code in codes)
    if width == 2:
        return bytes(c & 0xFF for c in codes).decode("latin-1", "replace")
    return raw.decode("latin-1", "replace")


def _pdf_text_from_series(series: bytes, cmap: dict[int, str], width: int) -> str:
    """Read the text of one BT/ET block, with Tj TJ ' and ''.

    The text series are between brackets (with escapes) or as a hex series
    between angle brackets; in a TJ array those alternate with numbers that
    control the spacing. Those numbers are not letters and are skipped.
    """
    pieces: list[str] = []
    i = 0
    n = len(series)
    while i < n:
        char = series[i:i + 1]
        if char == b"(":
            depth = 1
            j = i + 1
            while j < n and depth:
                if series[j:j + 1] == b"\\":
                    j += 2
                    continue
                if series[j:j + 1] == b"(":
                    depth += 1
                elif series[j:j + 1] == b")":
                    depth -= 1
                j += 1
            inner = series[i + 1:j - 1]
            pieces.append(_pdf_text_numbers(_pdf_escapes_to_bytes(inner), cmap, width))
            i = j
            continue
        if char == b"<":
            j = series.find(b">", i)
            if j == -1:
                break
            pieces.append(_pdf_text_numbers(_pdf_hex_bytes(series[i + 1:j]), cmap, width))
            i = j + 1
            continue
        i += 1
    return "".join(pieces)


def _pdf_escapes_to_bytes(raw: bytes) -> bytes:
    r"""Turn escapes in a text series (``\(`` ``\)`` ``\\`` ``\ooo``) back."""
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i:i + 1] == b"\\" and i + 1 < len(raw):
            two = raw[i:i + 2]
            if two in _PDF_ONTWIJK:
                out += _PDF_ONTWIJK[two]
                i += 2
                continue
            octal = re.match(rb"\\([0-7]{1,3})", raw[i:])
            if octal:
                out.append(int(octal.group(1), 8) & 0xFF)
                i += 1 + len(octal.group(1))
                continue
            i += 2
            continue
        out += raw[i:i + 1]
        i += 1
    return bytes(out)


def _pdf_split_operators(series: bytes) -> list[bytes]:
    """The text operators of a stream in the right order.

    We look for them by hand and not with one big regex: only that way can we
    see per series which font is chosen at that moment, and that determines
    which CMap applies. Both bracket series ``(...) Tj`` and hex series
    ``<...> Tj``, the TJ arrays and the moves come along; the moves become a
    line break later.
    """
    pattern = rb"(" + rb"|".join((
        rb"\[[^\[\]]*\]\s*TJ",                       # [ (x) -12 (y) ] TJ
        rb"<[0-9A-Fa-f\s]*>\s*Tj",                      # <2B> Tj (glyph codes)
        rb"\([^()]*(?:\\.|\((?:[^()]|\\.)*\))*[^()]*\)\s*Tj",  # (text) Tj
        rb"<[0-9A-Fa-f\s]*>\s*'",                       # <2B> '
        rb"\([^()]*(?:\\.)*\)\s*'",                     # (text) '
        rb"\[[^\[\]]*\]\s*\"",                        # [ ... ] "
        rb"/[A-Za-z0-9#._-]+\s+[\d.]+\s+Tf",           # /F1 11 Tf
        rb"[\d.\-]+\s+[\d.\-]+\s+Td",                  # move
        rb"[\d.\-]+\s+[\d.\-]+\s+TD",
        rb"[\d.\-]+(?:\s+[\d.\-]+){5}\s+Tm",
        rb"T\*",
        rb"BT",
        rb"ET",
    )) + rb")"
    out: list[bytes] = []
    for match in re.finditer(pattern, series):
        out.append(match.group(1))
    return out


def _pdf_font_cmap(data: bytes, objects: dict[int, bytes]) -> dict[int, tuple[dict[int, str], int]]:
    """Per font name (``/F1``) the CMap and the code width (1 or 2).

    The CMap sits in the ``/ToUnicode`` object of the font. Without that CMap
    the glyph codes stay; with that CMap they become real letters.
    """
    # 1. Which objects are a font, and where is their ToUnicode?
    toon_per_font: dict[int, int] = {}
    breedte_per_font: dict[int, int] = {}
    naam_per_font: dict[int, bytes] = {}
    for number, body in objects.items():
        if b"/Type" in body and b"/Font" in body:
            match = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", body)
            if match:
                toon_per_font[number] = int(match.group(1))
            if re.search(rb"/Subtype\s*/Type0", body):
                breedte_per_font[number] = 2
            else:
                breedte_per_font[number] = 1
            name = re.search(rb"/BaseFont\s*/([#\w+.-]+)", body)
            if name:
                naam_per_font[number] = name.group(1)

    # 2. Read the CMaps.
    cmaps: dict[int, dict[int, str]] = {}
    for font, toon in toon_per_font.items():
        body = objects.get(toon, b"")
        raw = _pdf_stream_from(body) if b"stream" in body else body
        if b"/FlateDecode" in body or raw[:2] in (b"x\x9c", b"\x1f\x8b"):
            raw = _pdf_decompress(raw) or raw
        cmaps[font] = _pdf_read_cmap(raw)

    # 3. Link the resource names (/F1) to the font objects.
    koppeling: dict[bytes, tuple[dict[int, str], int]] = {}
    for number, body in objects.items():
        for match in re.finditer(rb"/([A-Za-z0-9#._-]+)\s+(\d+)\s+0\s+R", body):
            name, verwijzing = match.group(1), int(match.group(2))
            if verwijzing in breedte_per_font:
                koppeling[name] = (cmaps.get(verwijzing, {}), breedte_per_font[verwijzing])
    return koppeling


def _pdf_read_cmap(raw: bytes) -> dict[int, str]:
    """Read a ToUnicode CMap: bfchar and bfrange, with lists in bfrange.

    Besides ``<start> <end> <target>`` the standard knows ``<start> <end>
    [<target> <target> ...]`` for a range of separate targets; that last form is
    very common in practice and is exactly why an invoice sometimes yields only
    shifted letters.
    """
    out: dict[int, str] = {}
    text = raw.decode("latin-1", "replace")

    def to_codes(hex_series: str) -> bytes:
        clean = re.sub(r"\s+", "", hex_series)
        if len(clean) % 2:
            clean += "0"
        try:
            return bytes.fromhex(clean)
        except ValueError:
            return b""

    def to_unicode(code_bytes: bytes) -> str:
        if len(code_bytes) >= 2 and len(code_bytes) % 2 == 0:
            return code_bytes.decode("utf-16-be", "replace")
        return code_bytes.decode("latin-1", "replace")

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.DOTALL):
        for source, target in re.findall(r"<([0-9A-Fa-f\s]*)>\s*<([0-9A-Fa-f\s]*)>", block):
            code_bytes = to_codes(source)
            if not code_bytes:
                continue
            out[int.from_bytes(code_bytes, "big")] = to_unicode(to_codes(target))

    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.DOTALL):
        for match in re.finditer(
                r"<([0-9A-Fa-f\s]*)>\s*<([0-9A-Fa-f\s]*)>\s*"
                r"(?:<([0-9A-Fa-f\s]*)>|\[([^\]]*?)\s*\])", block, re.DOTALL):
            start, end, target, list_text = match.groups()
            low = int.from_bytes(to_codes(start), "big") if to_codes(start) else None
            high = int.from_bytes(to_codes(end), "big") if to_codes(end) else None
            if low is None or high is None or high < low:
                continue
            if list_text is not None:
                # ``[<target> <target> ...]``: one target per code from the range.
                targets = re.findall(r"<([0-9A-Fa-f\s]*)>", list_text)
                for offset, piece in enumerate(targets):
                    out[low + offset] = to_unicode(to_codes(piece))
            else:
                first = to_codes(target)
                if not first:
                    continue
                for code in range(low, high + 1):
                    number = code - low
                    new = bytearray(first)
                    # Add on the last bytes, with carry, as the standard
                    # prescribes.
                    rest = number
                    for place in range(len(new) - 1, -1, -1):
                        if not rest:
                            break
                        total = new[place] + (rest & 0xFF)
                        new[place] = total & 0xFF
                        rest = (rest >> 8) + (total >> 8)
                    out[code] = to_unicode(bytes(new))
    return out


def _pdf_all_text(data: bytes, objects: dict[int, bytes],
                  koppeling: dict[bytes, tuple[dict[int, str], int]]) -> str:
    """The text of all content streams, with the CMap of each font.

    Within one BT/ET block the pieces belong together: the operators only move
    the cursor on. That is why we join them and only get a line break at a real
    move (``Td``/``TD``/``T*``/``Tm``). That keeps "Total" one word instead of
    loose letters on loose lines.
    """
    stromen = _pdf_text_streams(data, objects)
    if not stromen:
        stromen = [data] if _pdf_is_text_series(data) else []

    lines: list[str] = []
    for stream in stromen:
        for block in re.findall(rb"BT(.*?)ET", stream, re.DOTALL):
            cmap: dict[int, str] = {}
            breedte = 1
            part = ""
            for piece in _pdf_split_operators(block):
                font = re.match(rb"/([A-Za-z0-9#._-]+)\s+[\d.]+\s+Tf", piece)
                if font:
                    cmap, breedte = koppeling.get(font.group(1), ({}, 1))
                    continue
                if piece.endswith(b"Tj") or piece.endswith(b"'"):
                    start = piece.find(b"(")
                    end = piece.rfind(b")")
                    if start != -1 and end > start:
                        part += _pdf_text_numbers(
                            _pdf_escapes_to_bytes(piece[start + 1:end]), cmap, breedte)
                        continue
                    # A text series may also be a hex series between angle
                    # brackets: <2B> Tj. That is exactly what a subset font does.
                    hex_match = re.search(rb"<([0-9A-Fa-f\s]*)>", piece)
                    if hex_match:
                        part += _pdf_text_numbers(
                            _pdf_hex_bytes(hex_match.group(1)), cmap, breedte)
                    continue
                if b"TJ" in piece or piece.rstrip().endswith(b'"'):
                    inner = piece[piece.find(b"[") + 1:piece.rfind(b"]")]
                    part += _pdf_text_from_series(inner, cmap, breedte)
                else:
                    # Td, TD, T*, Tm: the cursor jumps; this is a new line.
                    if part.strip():
                        lines.append(part)
                        part = ""
            if part.strip():
                lines.append(part)
    return _pdf_tidy("\n".join(lines))


def _pdf_tidy(text: str) -> str:
    """Tidy up the line breaks and spaces of the text layer."""
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def _pdf_unreadable(text: str) -> bool:
    """Does the result contain almost nothing but unreadable junk?

    Some pdfs use glyph codes without ToUnicode. Then "text" does come out,
    but no human can read it and searching is no use.
    """
    if len(text) < 8:
        return True
    letters = sum(1 for t in text if t.isalpha())
    if letters / max(1, len(text)) < 0.25:
        # Many amounts and numbers belong to an invoice; only if there are
        # hardly any letters is it really unreadable.
        if len(text.replace(" ", "")) >= 40:
            return True
    # Replacement characters and control codes betray a missing CMap.
    weird = sum(1 for t in text if ord(t) < 32 and t not in "\n\t")
    return weird / max(1, len(text)) > 0.1


def _pdf_embedded_jpegs(data: bytes) -> list[bytes]:
    """Get the embedded JPEGs (DCTDecode) out of a pdf, for the scan route."""
    out: list[bytes] = []
    for match in re.finditer(rb"stream\r?\n", data):
        start = match.end()
        head = data[max(0, match.start() - 600):match.start()]
        if b"DCTDecode" not in head:
            continue
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        raw = data[start:end]
        # Some writers put a line break before endstream. A JPEG starts with
        # SOI (0xFF 0xD8) and ends with EOI (0xFF 0xD9).
        start_jpeg = raw.find(b"\xff\xd8")
        stop = raw.rfind(b"\xff\xd9")
        if start_jpeg != -1 and stop > start_jpeg:
            out.append(raw[start_jpeg:stop + 2])
    return out


def _pdf_ocr(data: bytes, *, ocr_method: str = "auto") -> tuple[str, list[str]]:
    """Safety net for scans: run the embedded JPEGs through the OCR layer.

    Gives the text found and the honest messages that belong with it. No
    exception is ever passed on; if OCR fails, that is in the messages and the
    rest continues.
    """
    try:
        from . import ocr
    except Exception:                                # noqa: BLE001
        return "", ["the OCR layer could not be loaded"]

    status = ocr.detect(ocr_method)
    if status == ocr.STATUS_UNAVAILABLE:
        return "", [ocr.ocr_status_text(ocr.STATUS_UNAVAILABLE)]
    if status == ocr.STATUS_OFF:
        return "", [ocr.ocr_status_text(ocr.STATUS_OFF)]

    images = _pdf_embedded_jpegs(data)
    if not images:
        return "", ["there is no embedded image (JPEG) to read"]

    pieces: list[str] = []
    messages: list[str] = []
    for number, image in enumerate(images[:8], start=1):
        temporary = tempfile.NamedTemporaryFile(
            prefix="vindterug-pdf-", suffix=".jpg", delete=False)
        try:
            temporary.write(image)
            temporary.close()
            result = ocr.ocr_image(temporary.name, method=ocr_method)
            if result.text:
                pieces.append(result.text)
            elif result.error:
                messages.append(f"image {number}: {result.error}")
            else:
                messages.append(
                    f"image {number}: the text recognition found no text "
                    f"in this image")
        finally:
            try:
                os.unlink(temporary.name)
            except OSError:
                pass
    if not pieces and not messages:
        messages.append("the images yielded no readable text")
    return "\n".join(pieces), messages


def _pdf_title(data: bytes, path: Path) -> str:
    """The title from the document information, if it is there."""
    match = re.search(rb"/Title\s*\(((?:[^()\\]|\\.)*)\)", data)
    if match:
        title = _pdf_strip_escapes(match.group(1)).strip()
        if title:
            return title[:120]
    return path.stem


def extract_pdf(path: str | Path, *, ocr_method: str = "auto") -> Extraction:
    """Get text from a PDF: first the text layer, otherwise OCR of the images.

    The text layer of a pdf consists of glyph codes, not of letters. That is
    why we also read the fonts with their ToUnicode CMap; otherwise nothing
    readable comes out of an invoice with a subset font. If that yields almost
    nothing, we try the embedded JPEGs through OCR (scans).

    If neither works — for example a bitonal fax scan with CCITT or JBIG2, or
    a pdf without a text layer — we say so honestly, with the route and the
    number of characters, so that it is clear later why a document is found
    but empty.
    """
    p = Path(path)
    data = p.read_bytes()
    if not data.startswith(b"%PDF"):
        return Extraction(error="not a valid pdf file", kind="pdf")

    objects = _pdf_objects(data)
    mapping = _pdf_font_cmap(data, objects)
    clean = _pdf_all_text(data, objects, mapping)
    title = _pdf_title(data, p)
    extra: dict = {"bytes": len(data), "tekstlaag": len(clean)}

    unreadable = _pdf_unreadable(clean)
    if len(clean.strip()) >= 8 and not unreadable:
        extra["route"] = "tekstlaag"
        extra["tekens"] = len(clean)
        return Extraction(text=_shorten(clean), title=title, kind="pdf", extra=extra)

    # The text layer yields (almost) nothing: this is probably a scan.
    ocr_text, messages = _pdf_ocr(data, ocr_method=ocr_method)
    if ocr_text.strip():
        extra["route"] = "ocr"
        extra["tekens"] = len(ocr_text)
        if messages:
            extra["ocr_meldingen"] = messages
        return Extraction(text=_shorten(ocr_text.strip()), title=title, kind="pdf",
                          extra=extra)

    if unreadable and clean:
        extra["route"] = "tekstlaag"
        extra["tekens"] = len(clean)
        extra["onleesbaar"] = True
        if messages:
            extra["ocr_meldingen"] = messages
        return Extraction(
            text="", title=title, kind="pdf", extra=extra,
            error="the text layer of this pdf is not readable (glyph codes without "
                  "ToUnicode and no readable letters); there is nothing to index",
        )

    # Honestly say that there is nothing in it, and why.
    if messages:
        extra["ocr_meldingen"] = messages
    detail = ("; ".join(messages) if messages
              else "the text layer is empty and no readable image was found")
    extra["route"] = ("ocr-geprobeerd" if ocr_method != "uit" else "geen")
    extra["tekens"] = 0
    return Extraction(
        text="", title=title, kind="pdf", extra=extra,
        error="pdf without a readable text layer (probably a scan); "
              f"there is nothing to index from this file ({detail})",
    )


def _pdf_streams_to_text(data: bytes) -> str:
    """Compatibility helper: the text layer of raw pdf bytes.

    Older code and tests called this directly; it therefore stays, but now
    does the same as the real reader.
    """
    objects = _pdf_objects(data)
    mapping = _pdf_font_cmap(data, objects)
    return _pdf_all_text(data, objects, mapping)


def _pdf_hex_text(data: bytes) -> str:
    """Safety net for PDFs that store text as a hex series (<00410042> Tj)."""
    pieces: list[str] = []
    for match in _PDF_HEX_RE.finditer(data):
        digits = re.sub(rb"\s+", b"", match.group(1))
        if len(digits) < 4 or len(digits) % 2:
            continue
        try:
            raw = bytes.fromhex(digits.decode("ascii"))
        except ValueError:
            continue
        if len(raw) % 2 == 0 and raw[1:2] == b"\x00":
            pieces.append(raw.decode("utf-16-be", "ignore"))
    return " ".join(pieces)


# --------------------------------------------------------------------------
# Mail: .eml and .mbox through the email module
# --------------------------------------------------------------------------

def _mail_header(message) -> dict:
    """Header fields we want to keep."""
    def clean(name: str) -> str:
        value = message.get(name, "")
        try:
            return str(value).strip()
        except Exception:                       # noqa: BLE001 - odd headers
            return ""

    received = clean("Date")
    moment = None
    if received:
        try:
            moment = email.utils.parsedate_to_datetime(received)
            moment = moment.replace(tzinfo=None)
        except (TypeError, ValueError, IndexError):
            moment = None

    attachments: list[str] = []
    try:
        for part in message.walk():
            name = part.get_filename()
            if name:
                attachments.append(str(name))
    except Exception:                            # noqa: BLE001
        pass

    return {
        "van": clean("From"),
        "aan": clean("To"),
        "cc": clean("Cc"),
        "datum": clean("Date"),
        "moment": moment.strftime("%Y-%m-%d %H:%M") if moment else "",
        "onderwerp": clean("Subject"),
        "bijlagen": attachments,
        "message_id": clean("Message-ID"),
    }


def _mail_body(message) -> str:
    """Readable text from a message, with priority for text/plain."""
    pieces: list[str] = []
    try:
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            content_type = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            if content_type not in ("text/plain", "text/html"):
                continue
            try:
                raw = part.get_payload(decode=True)
            except Exception:                    # noqa: BLE001
                continue
            if raw is None:
                raw = str(part.get_payload()).encode("utf-8", "replace")
            encoding = part.get_content_charset() or "utf-8"
            try:
                decoded = raw.decode(encoding, "replace")
            except (LookupError, UnicodeDecodeError):
                decoded = raw.decode("utf-8", "replace")
            if content_type == "text/html":
                decoded = strip_html(decoded)
            pieces.append(decoded.strip())
            if content_type == "text/plain":
                break                            # plain text is enough
    except Exception:                            # noqa: BLE001
        pass
    return "\n\n".join(piece for piece in pieces if piece)


def _message_to_text(message, source: str) -> tuple[str, str, dict]:
    """Turn one message into (text, title, extra)."""
    header = _mail_header(message)
    title = header["onderwerp"] or source or "(no subject)"
    lines = [f"Subject: {title}"]
    for label, key in (("From", "van"), ("To", "aan"), ("Cc", "cc"),
                       ("Date", "datum")):
        if header[key]:
            lines.append(f"{label}: {header[key]}")
    if header["bijlagen"]:
        lines.append("Attachments: " + ", ".join(header["bijlagen"]))
    lines.append("")
    lines.append(_mail_body(message))
    return "\n".join(lines).strip(), title, header


def extract_eml(path: str | Path) -> Extraction:
    """Read one .eml file: subject, from, to, date, text and attachments."""
    p = Path(path)
    raw = p.read_bytes()
    message = email.message_from_bytes(raw, policy=email.policy.default)
    content, title, header = _message_to_text(message, p.stem)
    header["bron"] = str(p)
    return Extraction(text=_shorten(content), title=title[:160], kind="mail",
                      extra=header)


def _mbox_parts(path: Path) -> list[bytes]:
    """Split an mbox file into separate messages.

    We split ourselves instead of using ``mailbox``: that saves an extra file
    pointer and also works on a truncated or locked file.
    """
    import mailbox

    parts: list[bytes] = []
    try:
        box = mailbox.mbox(str(path), factory=None, create=False)
        try:
            for message in box:
                try:
                    parts.append(message.as_bytes())
                except Exception:            # noqa: BLE001
                    continue
        finally:
            box.close()
        if parts:
            return parts
    except Exception:                        # noqa: BLE001 - fall back on splitting ourselves
        parts = []

    raw = path.read_bytes()
    for block in re.split(rb"(?m)^From .*$", raw):
        if block.strip():
            parts.append(block)
    return parts


def extract_mbox(path: str | Path) -> Extraction:
    """Read a .mbox archive with several mails."""
    p = Path(path)
    parts = _mbox_parts(p)
    if not parts:
        return Extraction(error="no messages found in this mbox file",
                          title=p.stem, kind="mail")

    pieces: list[str] = []
    titles: list[str] = []
    moments: list[str] = []
    for part in parts:
        try:
            message = email.message_from_bytes(part, policy=email.policy.default)
        except Exception:                    # noqa: BLE001
            continue
        content, title, header = _message_to_text(message, p.stem)
        titles.append(title)
        if header.get("moment"):
            moments.append(header["moment"])
        pieces.append(content)

    body = "\n\n" + ("-" * 60) + "\n\n".join(pieces) if False else "\n\n".join(pieces)
    title = f"{p.stem} ({len(pieces)} mails)"
    if titles:
        title = f"{p.stem}: {titles[0][:80]}"
    extra = {"berichten": len(pieces), "bijlagen": []}
    if moments:
        extra["moment"] = max(moments)
    return Extraction(text=_shorten(body.strip()), title=title, kind="mail",
                      extra=extra)


def extract_mail_file(path: str | Path) -> Extraction:
    """Choose yourself whether this is an .eml or an .mbox, and read it."""
    p = Path(path)
    if p.suffix.lower() in (".mbox", ".mbx"):
        return extract_mbox(p)
    if p.suffix.lower() == ".eml":
        return extract_eml(p)
    raw = p.read_bytes(4096)
    if raw.lower().lstrip().startswith(b"from ") and b"\nfrom " in p.read_bytes(65536).lower():
        return extract_mbox(p)
    return extract_eml(p)


def extract_music(path: str | Path) -> Extraction:
    """Get artist, title and album out of a music file.

    Gives an :class:`Extraction` with kind ``muziek``, so that searching on
    artist, title or file name finds a track. Without tags the file name does
    the work; that is never an error, because there is always something to
    find. Only if the file itself is unreadable does a message come back.
    """
    p = Path(path)
    data = muziek.read_music(p)
    content = muziek.text_of_music(p, data)
    extra = {
        "artiest": data.artist,
        "muziekartiest": data.album_artist,
        "album": data.album,
        "genre": data.genre,
        "jaar": data.year,
        "track": data.track,
        "muziektitel": data.title,
        "muziekbron": data.source,
    }
    extra.update(data.extra or {})
    title = data.title or p.stem
    if data.artist and data.title and data.title != p.stem:
        title = f"{data.artist} - {data.title}"
    return Extraction(text=_shorten(content), title=title, kind="muziek", extra=extra)


def extract_file(path: str | Path) -> Extraction:
    """Read a file of whatever supported kind.

    Never raises an exception: whatever goes wrong ends up in
    ``Extraction.error`` with an understandable explanation.
    """
    p = Path(path)
    try:
        kind = kind_of_file(p)
        if kind == "tekst":
            return extract_text_file(p)
        if kind == "office":
            ext = p.suffix.lower()
            if ext == ".docx":
                return extract_docx(p)
            if ext == ".xlsx":
                return extract_xlsx(p)
            if ext == ".pptx":
                return extract_pptx(p)
        if kind == "pdf":
            return extract_pdf(p)
        if kind == "mail":
            return extract_mail_file(p)
        if kind == "muziek":
            return extract_music(p)
        if kind == "image":
            # Reading the image itself is the job of ocr.py; only the name
            # comes here, so that the file is at least findable on its title.
            return Extraction(text="", title=p.stem, kind="image")
        return Extraction(title=p.stem, kind="onbekend",
                          error=f"this file type ({p.suffix or 'without extension'}) "
                                "is not supported")
    except zipfile.BadZipFile:
        return Extraction(title=p.stem, kind=kind_of_file(p),
                          error="this Office file is damaged (not a valid zip)")
    except (OSError, PermissionError) as exc:
        return Extraction(title=p.stem, error=f"could not read the file: {exc}")
    except Exception as exc:                     # noqa: BLE001 - never let it crash
        return Extraction(title=p.stem,
                          error=f"unexpected error while reading ({type(exc).__name__}): {exc}")


# --------------------------------------------------------------------------
# Browser history
# --------------------------------------------------------------------------

# Chrome and Edge count in microseconds since 1 January 1601 (UTC).
CHROME_EPOCH = 11644473600.0


def chrome_time_to_datetime(value: int | float | None) -> "dt.datetime | None":
    """Chrome/Edge time (microseconds since 1601 **UTC**) to a moment.

    The result is a naive UTC time, not local time: the stored value is epoch
    time and therefore timezone-independent. By not using ``fromtimestamp``
    the same history gives the same answer on every pc. Only the display turns
    this moment into local time (see ``core.date_to_local``). ``core``
    uses this function directly, so that there is only one conversion.
    """
    import datetime as _dt

    if not value:
        return None
    try:
        seconds = float(value) / 1_000_000.0 - CHROME_EPOCH
    except (TypeError, ValueError):
        return None
    try:
        return _dt.datetime(1970, 1, 1) + _dt.timedelta(seconds=seconds)
    except (OverflowError, OSError, ValueError):
        return None


def firefox_time_to_datetime(value: int | float | None) -> "dt.datetime | None":
    """Firefox time (microseconds since 1970 **UTC**) to a naive UTC time.

    Same choice as with :func:`chrome_time_to_datetime`: epoch time is
    timezone-independent, so the answer is UTC.
    """
    import datetime as _dt

    if not value:
        return None
    try:
        return _dt.datetime(1970, 1, 1) + _dt.timedelta(
            seconds=float(value) / 1_000_000.0)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class BrowserVisit:
    """One row from the browser history."""

    url: str
    title: str = ""
    host: str = ""
    moment: "dt.datetime | None" = None
    browser: str = "browser"
    profile: str = ""
    visits: int = 1
    image: bytes = b""       # favicon, if there is one

    @property
    def key(self) -> str:
        """Unique key within the index: browser + profile + url."""
        return f"{self.browser}|{self.profile}|{self.url}"


def _host_of(url: object) -> str:
    """The host of a visit, for searching by site name."""
    text = str(url or "")
    if "://" not in text:
        return ""
    rest = text.split("://", 1)[1]
    return rest.split("/", 1)[0]


def _temporary_copy(path: Path) -> Path:
    """Copy a locked browser file to a temporary file.

    Chrome, Edge and Firefox keep their history file open as long as the
    browser runs. Reading directly then does not work; a copy always does. The
    ``-wal`` next to the file comes along too, so that recent rows are not
    missing.
    """
    fd, name = tempfile.mkstemp(prefix="vindterug-", suffix=path.suffix or ".db")
    os.close(fd)
    target = Path(name)
    shutil.copy2(path, target)
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            try:
                shutil.copy2(side, str(target) + suffix)
            except OSError:
                pass
    return target


def default_folders() -> list[Path]:
    """The folders that everyone has, if they exist.

    For a new user there is nothing to search; this list is what "Add my
    default folders" prepares in one click. We only take folders that really
    exist, so that nothing odd ends up in the list.
    """
    home = Path.home()
    candidates = [
        home / "Desktop", home / "Bureaublad",
        home / "Documents", home / "Documenten",
        home / "Downloads",
        home / "Music", home / "Muziek",
        home / "Pictures", home / "Afbeeldingen",
        home / "Videos", home / "Video's",
    ]
    out: list[Path] = []
    for candidate in candidates:
        if candidate.is_dir() and candidate not in out:
            out.append(candidate)
    return out


# --------------------------------------------------------------------------
# This computer: which drives are there?
# --------------------------------------------------------------------------

def _drive_letters() -> list[str]:
    """The letters of all drives this system knows (C, D, ...)."""
    try:
        return [str(path).rstrip("\\/") for path in os.listdrives()]
    except AttributeError:
        # Older than Python 3.12: ask Windows itself.
        pass
    except OSError:
        return []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return []
    return [f"{letter}:" for number, letter in enumerate(string.ascii_uppercase)
            if mask & (1 << number)]


def is_fixed_drive(root: str | Path) -> bool:
    """Is this a fixed drive (not a USB stick, dvd or network drive)?"""
    try:
        kind = ctypes.windll.kernel32.GetDriveTypeW(str(root))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return True     # outside Windows there is nothing to tell apart
    return kind == 3   # DRIVE_FIXED


def drives(*, fixed_only: bool = True) -> list[Path]:
    """The starting points of this computer: every fixed drive.

    Fixed drives only. A USB stick can come loose during indexing and a
    network drive needs another computer that is switched on; for "the whole
    computer" that is exactly right and keeps a round predictable.
    """
    out: list[Path] = []
    for letter in _drive_letters():
        root = Path(f"{letter}\\")
        try:
            if fixed_only and not is_fixed_drive(root):
                continue
            if not root.is_dir():
                continue
        except OSError:
            continue
        out.append(root)
    if not out:
        # No drive found (or no Windows): start at the drive this program
        # itself is on.
        anchor = Path.home().anchor
        if anchor:
            out.append(Path(anchor))
    return out


def computer_folders() -> list[Path]:
    """What "Add whole computer" puts in the list: all fixed drives."""
    return drives()


def default_browser_profiles() -> dict[str, list[Path]]:
    """The browser profiles of Chrome, Edge and Firefox that were found."""
    return {
        "chrome": find_chrome_profiles("chrome"),
        "edge": find_chrome_profiles("edge"),
        "firefox": find_firefox_profiles(),
    }


def find_chrome_profiles(kind: str = "chrome") -> list[Path]:
    """All profile folders of Chrome or Edge on this pc."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    root = {
        "chrome": Path(base) / "Google" / "Chrome" / "User Data",
        "edge": Path(base) / "Microsoft" / "Edge" / "User Data",
    }.get(kind)
    if root is None or not root.is_dir():
        return []
    out: list[Path] = []
    for candidate in [root] + sorted(k for k in root.iterdir() if k.is_dir()):
        if (candidate / "History").is_file():
            out.append(candidate)
    return out


def find_firefox_profiles() -> list[Path]:
    """All Firefox profile folders with a places.sqlite."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    root = Path(base) / "Mozilla" / "Firefox" / "Profiles"
    out: list[Path] = []
    if root.is_dir():
        for profile in sorted(root.iterdir()):
            if (profile / "places.sqlite").is_file():
                out.append(profile)
    for extra in glob.glob(str(Path(base) / "Mozilla" / "Firefox" / "Profiles" / "*.default*")):
        candidate = Path(extra)
        if (candidate / "places.sqlite").is_file() and candidate not in out:
            out.append(candidate)
    return out


def read_chrome_history(profile: Path, *, max_rows: int = 4000,
                        with_favicon: bool = True) -> list[BrowserVisit]:
    """Read the visits from a Chrome or Edge profile.

    We read per URL: address, title and the time of the last visit.
    """
    browser = "edge" if "edge" in str(profile).lower() else "chrome"
    history = Path(profile) / "History"
    if not history.is_file():
        return []
    copy = _temporary_copy(history)
    out: list[BrowserVisit] = []
    try:
        connection = sqlite3.connect(str(copy))
        try:
            rows = connection.execute(
                "SELECT url, title, last_visit_time, visit_count "
                "FROM urls WHERE last_visit_time > 0 "
                "ORDER BY last_visit_time DESC LIMIT ?", (max_rows,)).fetchall()
        except sqlite3.DatabaseError:
            rows = []
        found = {
            url: BrowserVisit(url=url, title=(title or "").strip(),
                              host=_host_of(url),
                              moment=chrome_time_to_datetime(moment),
                              browser=browser, profile=profile.name,
                              visits=int(count or 1))
            for url, title, moment, count in rows
        }
        out = list(found.values())
        if with_favicon:
            _fill_favicons(profile, out)
        try:
            connection.close()
        except sqlite3.Error:
            pass
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(copy) + suffix).unlink(missing_ok=True)
            except OSError:
                pass
    return out


def _fill_favicons(profile: Path, visits: list[BrowserVisit]) -> None:
    """Fetch stored favicons; those make indexing screenshots worthwhile.

    Chrome and Edge keep the icons in a separate ``Favicons`` file with the
    columns ``icon_mapping.page_url`` and ``favicon_bitmaps.image_data``. If it
    fails (different file format, no rights), the rest continues.
    """
    source = Path(profile) / "Favicons"
    if not source.is_file():
        return
    copy = None
    try:
        copy = _temporary_copy(source)
        connection = sqlite3.connect(str(copy))
        rows = connection.execute(
            "SELECT m.page_url, b.image_data FROM icon_mapping m "
            "JOIN favicon_bitmaps b ON b.icon_id = m.icon_id "
            "WHERE b.image_data IS NOT NULL").fetchall()
        per_url: dict[str, bytes] = {}
        for url, data in rows:
            if data and url not in per_url:
                per_url[str(url)] = bytes(data)
        for visit in visits:
            visit.image = per_url.get(visit.url, b"")
        connection.close()
    except (sqlite3.Error, OSError):
        return
    finally:
        if copy is not None:
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(copy) + suffix).unlink(missing_ok=True)
                except OSError:
                    pass


def read_firefox_history(profile: Path, *, max_rows: int = 4000) -> list[BrowserVisit]:
    """Read the visits from a Firefox profile (places.sqlite)."""
    places = Path(profile) / "places.sqlite"
    if not places.is_file():
        return []
    copy = _temporary_copy(places)
    out: list[BrowserVisit] = []
    try:
        connection = sqlite3.connect(str(copy))
        try:
            rows = connection.execute(
                "SELECT url, title, last_visit_date, visit_count FROM moz_places "
                "WHERE last_visit_date IS NOT NULL AND hidden = 0 "
                "ORDER BY last_visit_date DESC LIMIT ?", (max_rows,)).fetchall()
        except sqlite3.DatabaseError:
            rows = []
        for url, title, moment, count in rows:
            out.append(BrowserVisit(
                url=str(url), title=(title or "").strip(),
                host=_host_of(url),
                moment=firefox_time_to_datetime(moment),
                browser="firefox", profile=Path(profile).name,
                visits=int(count or 1)))
        try:
            connection.close()
        except sqlite3.Error:
            pass
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(copy) + suffix).unlink(missing_ok=True)
            except OSError:
                pass
    return out


def browser_history_source(now: "dt.datetime | None" = None) -> list[BrowserVisit]:
    """All findable browser visits of this pc, newest first."""
    out: list[BrowserVisit] = []
    for kind in ("chrome", "edge"):
        for profile in find_chrome_profiles(kind):
            try:
                out.extend(read_chrome_history(profile))
            except Exception:                    # noqa: BLE001 - never let it crash
                continue
    for profile in find_firefox_profiles():
        try:
            out.extend(read_firefox_history(profile))
        except Exception:                        # noqa: BLE001
            continue
    out.sort(key=lambda v: v.moment or tijd._day(now or __import__("datetime").datetime.now()),
             reverse=True)
    return out


def text_of_visit(visit: BrowserVisit) -> str:
    """The searchable text of one browser visit."""
    parts = [visit.url]
    if visit.title:
        parts.append(visit.title)
    if visit.moment:
        parts.append(visit.moment.strftime("%d-%m-%Y %H:%M"))
    if visit.browser:
        parts.append(visit.browser)
    return "\n".join(part for part in parts if part)

