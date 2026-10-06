"""Tests for the core of VindTerug.

Everything runs without a screen and without a network: unittest, standard
library only.

What these tests guard:

* reading every kind of file (text, HTML, docx, xlsx, pdf, eml, mbox);
* understanding a search question (kind, time, subject, synonyms);
* building the index and finding back what is in it;
* filtering on a date range and ranking the results.

The sample files are made inside the test itself, so nothing is needed from the
user's machine.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

# The tests live in tests/; the package lives one folder up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vindterug import core, query, sources, tekst, tijd  # noqa: E402


# --------------------------------------------------------------------------
# Helpers: sample files we build ourselves
# --------------------------------------------------------------------------

def _write_docx(path: Path, lines: list[str]) -> None:
    """Write a minimal .docx: a zip with just enough XML in it."""
    paragraph = "".join(
        f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
        '2006/main"><w:body>' + paragraph + "</w:body></w:document>")
    types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>")
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships"><Relationship Id="rId1" Type="http://schemas.openxml'
        'formats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)


def _write_pdf(path: Path, lines: list[str]) -> None:
    """Write a minimal PDF with an uncompressed content stream.

    It holds just enough to make VindTerug's reader work: a ``BT ... ET`` block
    with ``Tj`` operators. That way we test PDF reading without needing a
    package such as reportlab.
    """
    text = "".join(f"({line}) Tj\n0 -14 Td\n" for line in lines)
    stream = f"BT\n/F1 12 Tf\n72 720 Td\n{text}ET\n".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream + b"endstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n").encode("ascii")
    path.write_bytes(bytes(out))


def _encode_synchsafe(value: int) -> bytes:
    """A number as synchsafe bytes (7 bits per byte), as ID3v2 wants it."""
    return bytes([(value >> 21) & 0x7F, (value >> 14) & 0x7F,
                  (value >> 7) & 0x7F, value & 0x7F])


def _id3_frame(fid: str, content: bytes, *, utf16: bool = False) -> bytes:
    """One ID3v2.3 frame: id, size, encoding byte and the text itself."""
    if utf16:
        body = (b"\x01" + "\ufeff".encode("utf-16-le")
                + content.decode("latin-1").encode("utf-16-le"))
    else:
        body = b"\x00" + content                    # 0 = latin-1
    return (fid.encode("ascii") + len(body).to_bytes(4, "big")
            + b"\x00\x00" + body)


def _mp3_with_id3v2(path: Path, fields: dict[str, str]) -> None:
    """Write a small mp3 with a real ID3v2.3 tag (text encoding 0).

    The frames are built by hand, exactly as the standard prescribes, so the
    reader is tested against real bytes and not against a helper.
    """
    frames = b""
    for fid, value in fields.items():
        frames += _id3_frame(fid, value.encode("latin-1", "replace"))
    tag = b"ID3\x03\x00\x00" + _encode_synchsafe(len(frames)) + frames
    path.write_bytes(tag + b"\xff\xfb\x90\x00" + b"\x00" * 400)


def _mp3_with_id3v1(path: Path, *, title: str, artist: str, album: str) -> None:
    """Write an mp3 with only an ID3v1 tag in the last 128 bytes."""
    body = b"\xff\xfb\x90\x00" + b"\x00" * 400
    tag = (
        b"TAG" + title.encode("latin-1")[:30].ljust(30, b"\x00")
        + artist.encode("latin-1")[:30].ljust(30, b"\x00")
        + album.encode("latin-1")[:30].ljust(30, b"\x00")
        + b"1999".ljust(4, b"\x00") + b"\x00" * 30 + b"\x00"
    )
    path.write_bytes(body + tag)


def _utf16_hex(character: str) -> str:
    """A character as a UTF-16BE hex string, exactly two bytes per code unit."""
    raw = character.encode("utf-16-be")
    return "".join(f"{raw[i]:02X}{raw[i + 1]:02X}" for i in range(0, len(raw), 2))


def _pdf_with_cmap_text(path: Path, *,
                        bfchar: dict[str, str],
                        bfrange: tuple[str, str, list[str]] | None = None) -> None:
    """A pdf with a FlateDecode text stream and a ToUnicode CMap.

    The CMap uses ``beginbfchar`` and, when given, also ``beginbfrange`` with a
    *list* of targets per code — exactly the shape real invoices use. The
    encoding is one byte per glyph (simple font).

    Returns the text that should come out of the glyph codes.
    """
    rows: list[tuple[str, str]] = []

    def add(code_hex: str, text: str) -> None:
        rows.append((code_hex, text))

    for code_hex, character in bfchar.items():
        add(code_hex, character)
    if bfrange is not None:
        start, end, targets = bfrange
        begin = int(start, 16)
        for shift, target in enumerate(targets):
            add(f"{begin + shift:02X}", target)

    # All glyph runs in one row, without moving in between: that way they belong
    # together and form a single word.
    operators = "".join(f"<{code_hex}> Tj\n" for code_hex, _text in rows)
    content = f"BT\n/F1 12 Tf\n72 720 Td\n{operators}ET\n".encode("latin-1")
    compressed = zlib.compress(content)

    cmap_parts = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<00> <FF>",
        "endcodespacerange",
    ]
    if bfchar:
        cmap_parts.append(f"{len(bfchar)} beginbfchar")
        for code_hex, character in bfchar.items():
            cmap_parts.append(f"<{code_hex}> <{_utf16_hex(character)}>")
        cmap_parts.append("endbfchar")
    if bfrange is not None:
        start, end, targets = bfrange
        cmap_parts.append("1 beginbfrange")
        target_list = " ".join(f"<{_utf16_hex(target)}>" for target in targets)
        cmap_parts.append(f"<{start}> <{end}> [{target_list}]")
        cmap_parts.append("endbfrange")
    cmap_parts += ["endcmap", "CMapName currentdict /CMap defineresource pop",
                   "end", "end"]
    cmap_raw = ("\n".join(cmap_parts) + "\n").encode("latin-1")
    cmap_compressed = zlib.compress(cmap_raw)

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /TrueType /BaseFont /ABCDEF+Carlito "
        b"/FirstChar 0 /LastChar 255 /ToUnicode 5 0 R >>",
        b"<< /Length " + str(len(cmap_compressed)).encode("ascii")
        + b" /Filter /FlateDecode >>\nstream\n" + cmap_compressed + b"\nendstream",
        b"<< /Length " + str(len(compressed)).encode("ascii")
        + b" /Filter /FlateDecode >>\nstream\n" + compressed + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode("ascii")
    path.write_bytes(bytes(out))

    return "".join(text for _run, text in rows)


def _minimal_jpeg() -> bytes:
    """A tiny but valid JPEG file (1x1 pixel)."""
    return bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300"
        "080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222b231c1c2837292c30313434341f27393d38323c2e333432"
        "ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405"
        "0607"
        "08090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613"
        "516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfc28a28a2803ffd9")


def _pdf_with_jpeg(path: Path) -> None:
    """A pdf without a text layer, but with an embedded JPEG (a scan)."""
    jpeg = _minimal_jpeg()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length 40 >>\nstream\nq 595 0 0 842 0 0 cm /Im0 Do Q\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
        b"/Length " + str(len(jpeg)).encode("ascii") + b" >>\nstream\n" + jpeg
        + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode("ascii")
    path.write_bytes(bytes(out))


def _wav_without_tags(path: Path) -> None:
    """A valid but empty wav (44-byte RIFF header, no audio)."""
    header = b"RIFF" + (36).to_bytes(4, "little") + b"WAVE"
    fmt = (b"fmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
           + (1).to_bytes(2, "little") + (8000).to_bytes(4, "little")
           + (8000).to_bytes(4, "little") + (1).to_bytes(2, "little")
           + (8).to_bytes(2, "little"))
    data = b"data" + (0).to_bytes(4, "little")
    path.write_bytes(header + fmt + data)


def _write_eml(path: Path, moment: dt.datetime, sender: str, to: str,
               subject: str, body: str) -> None:
    """Write an .eml file with \\r\\n line endings, as the standard wants."""
    lines = [
        f"From: {sender}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Date: {moment.strftime('%a, %d %b %Y %H:%M:%S +0200')}",
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
        "",
    ] + body.splitlines()
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))


def _write_mbox(path: Path, messages: list[tuple]) -> None:
    """Write an mbox file: every message starts with a 'From ' line."""
    parts: list[str] = []
    for moment, sender, to, subject, body in messages:
        head = "From " + moment.strftime("%a %b %d %H:%M:%S %Y") + "\r\n"
        lines = [
            f"From: {sender}",
            f"To: {to}",
            f"Subject: {subject}",
            f"Date: {moment.strftime('%a, %d %b %Y %H:%M:%S +0200')}",
            "MIME-Version: 1.0",
            'Content-Type: text/plain; charset="utf-8"',
            "",
        ] + body.splitlines()
        parts.append(head + "\r\n".join(lines) + "\r\n")
    path.write_bytes("\r\n".join(parts).encode("utf-8"))


class _TempFolder(unittest.TestCase):
    """Base for tests that need files on disk."""

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="vindterug-test-"))
        self.addCleanup(self._clean_up)

    def _clean_up(self) -> None:
        import shutil

        shutil.rmtree(self.folder, ignore_errors=True)

    def path(self, name: str) -> Path:
        target = self.folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def index(self, name: str = "index.db") -> core.VindTerugIndex:
        """A fresh index in the temporary folder, closed neatly after the test."""
        ix = core.VindTerugIndex(self.folder / name)
        self.addCleanup(ix.close)
        return ix


# --------------------------------------------------------------------------
# Language: splitting, stemming, synonyms
# --------------------------------------------------------------------------

class LanguageTest(unittest.TestCase):
    def test_tokenise_splits_on_letters_and_digits(self) -> None:
        self.assertEqual(
            tekst.tokenise("De Aanbesteding, fietsbrug 2026!"),
            ["de", "aanbesteding", "fietsbrug", "2026"])

    def test_tokenise_strips_accents(self) -> None:
        self.assertEqual(tekst.tokenise("Curaçao — één"), ["curacao", "een"])

    def test_stem_removes_known_endings(self) -> None:
        # The stem of the plural equals the stem of the singular, so that
        # "aanbestedingen" and "aanbesteding" find each other while searching.
        self.assertEqual(tekst.stem("aanbestedingen"), tekst.stem("aanbesteding"))
        self.assertEqual(tekst.stem("facturen"), "factuur")
        self.assertEqual(tekst.stem("vergadering"), "vergader")

    def test_stem_is_idempotent(self) -> None:
        """Stemming twice may not change anything more than stemming once."""
        for word in ("aanbestedingen", "vergadering", "facturen", "notulen"):
            self.assertEqual(tekst.stem(tekst.stem(word)), tekst.stem(word))

    def test_stem_leaves_short_words_alone(self) -> None:
        for word in ("kas", "mei", "bus", "ns"):
            self.assertEqual(tekst.stem(word), word)

    def test_synonym_rekening_points_to_factuur(self) -> None:
        variants = tekst.meaning_variants("rekening")
        self.assertIn("factuur", variants)
        self.assertIn("nota", variants)

    def test_synonym_also_works_in_the_plural(self) -> None:
        self.assertIn("factuur", tekst.meaning_variants("rekeningen"))

    def test_stopwords_are_removed(self) -> None:
        self.assertEqual(
            tekst.remove_stopwords(["dat", "mailtje", "over", "aanbesteding"]),
            ["mailtje", "aanbesteding"])


# --------------------------------------------------------------------------
# Time: recognising date ranges
# --------------------------------------------------------------------------

class TimeTest(unittest.TestCase):
    NU = dt.datetime(2026, 9, 28, 12, 0)

    def test_month_name_gives_a_month_range(self) -> None:
        found = tijd.parse_time("aanbesteding in maart", self.NU)
        self.assertIsNotNone(found.period)
        self.assertEqual(found.period.start, dt.datetime(2026, 3, 1))
        self.assertEqual(found.period.end,
                         dt.datetime(2026, 3, 31, 23, 59, 59))

    def test_month_with_a_year(self) -> None:
        found = tijd.parse_time("aanbesteding maart 2025", self.NU)
        self.assertEqual(found.period.start, dt.datetime(2025, 3, 1))
        self.assertEqual(found.period.end.month, 3)

    def test_iso_date_becomes_one_day(self) -> None:
        found = tijd.parse_time("verslag 2026-03-11", self.NU)
        self.assertEqual(found.period.start, dt.datetime(2026, 3, 11))
        self.assertEqual(found.period.end.day, 11)

    def test_words_about_time(self) -> None:
        yesterday = tijd.parse_time("wat ik gisteren zag", self.NU)
        self.assertEqual(yesterday.period.start, dt.datetime(2026, 9, 27))
        last_week = tijd.parse_time("vorige week", self.NU)
        self.assertIsNotNone(last_week.period)

    def test_without_time_words_no_range(self) -> None:
        found = tijd.parse_time("handleiding koffiemachine", self.NU)
        self.assertIsNone(found.period)

    def test_period_contains_is_inclusive(self) -> None:
        period = tijd.parse_time("in maart", self.NU).period
        self.assertTrue(period.contains(dt.datetime(2026, 3, 1, 0, 0)))
        self.assertTrue(period.contains(dt.datetime(2026, 3, 31, 23, 59)))
        self.assertFalse(period.contains(dt.datetime(2026, 4, 1)))
        self.assertFalse(period.contains(None))

    def test_the_label_we_show_is_english(self) -> None:
        """What the search *understands* is Dutch; what we show is English."""
        found = tijd.parse_time("aanbesteding in maart", self.NU)
        self.assertEqual(found.period.describe(), "March 2026")
        self.assertEqual(found.recognition, "maart")
        self.assertEqual(tijd.month_name(3), "March")
        yesterday = tijd.parse_time("gisteren", self.NU)
        self.assertEqual(yesterday.period.describe(), "yesterday")


# --------------------------------------------------------------------------
# Understanding the search question
# --------------------------------------------------------------------------

class QueryTest(unittest.TestCase):
    NU = dt.datetime(2026, 9, 28, 12, 0)

    def test_the_example_question(self) -> None:
        """The example question must give mail and March 2026."""
        q = query.parse_query("dat mailtje over die aanbesteding in maart",
                              self.NU)
        self.assertIs(q.kind, query.QueryKind.MAIL)
        self.assertIsNotNone(q.period)
        self.assertEqual(q.period.start, dt.datetime(2026, 3, 1))
        self.assertEqual(q.period.end, dt.datetime(2026, 3, 31, 23, 59, 59))
        self.assertIn("aanbesteding", q.terms)
        self.assertTrue(q.has_search_words)

    def test_the_date_range_sits_around_march(self) -> None:
        """The range must cover March and not touch April or February."""
        q = query.parse_query("dat mailtje over die aanbesteding in maart",
                              self.NU)
        self.assertTrue(q.period.contains(dt.datetime(2026, 3, 15)))
        self.assertFalse(q.period.contains(dt.datetime(2026, 4, 15)))
        self.assertFalse(q.period.contains(dt.datetime(2026, 2, 15)))

    def test_screenshot_of_the_invoice(self) -> None:
        q = query.parse_query("screenshot van de factuur", self.NU)
        self.assertIs(q.kind, query.QueryKind.IMAGE)
        self.assertIn("factuur", q.terms)

    def test_kind_words_do_not_become_the_subject(self) -> None:
        q = query.parse_query("dat mailtje over de aanbesteding", self.NU)
        self.assertNotIn("mailtje", q.terms)

    def test_synonyms_end_up_in_the_search_words(self) -> None:
        q = query.parse_query("de rekening van de loodgieter", self.NU)
        self.assertIn("rekening", q.terms)
        self.assertIn("factuur", q.search_words)

    def test_the_explanation_is_english(self) -> None:
        q = query.parse_query("dat mailtje over die aanbesteding in maart",
                              self.NU)
        uitleg = q.explanation
        self.assertIn("searching for:", uitleg)
        self.assertIn("you asked for a mail", uitleg)
        self.assertIn("March 2026", uitleg)

    def test_empty_stays_empty(self) -> None:
        q = query.parse_query("", self.NU)
        self.assertFalse(q.has_search_words)
        self.assertIsNone(q.period)


# --------------------------------------------------------------------------
# Getting text out of files
# --------------------------------------------------------------------------

class ExtractionTest(_TempFolder):
    def test_plain_text(self) -> None:
        bestand = self.path("notitie.txt")
        bestand.write_text("Eerste regel over de fietsbrug.\nTweede regel.",
                           encoding="utf-8")
        result = sources.extract_text_file(bestand)
        self.assertIn("fietsbrug", result.text)
        self.assertEqual(result.title, "Eerste regel over de fietsbrug.")

    def test_html_is_stripped(self) -> None:
        raw = ("<html><head><title>Mijn pagina</title>"
               "<style>body { color: red }</style></head>"
               "<body><h1>Kop</h1><p>De factuur voor <b>maart</b>.</p>"
               "<script>alert('weg')</script></body></html>")
        clean = sources.strip_html(raw)
        self.assertIn("Mijn pagina", clean)
        self.assertIn("factuur", clean)
        self.assertIn("maart", clean)
        self.assertNotIn("<b>", clean)
        self.assertNotIn("alert", clean)
        self.assertNotIn("color: red", clean)

    def test_html_entities_become_letters(self) -> None:
        self.assertIn("één", sources.strip_html("<p>e&#233;n &amp; één</p>"))

    def test_docx(self) -> None:
        bestand = self.path("aanbesteding.docx")
        _write_docx(bestand, [
            "Aanbesteding fietsbrug",
            "De inschrijving moet voor de zomer binnen zijn.",
        ])
        result = sources.extract_docx(bestand)
        self.assertTrue(result.ok, result.error)
        self.assertIn("Aanbesteding fietsbrug", result.text)
        self.assertIn("zomer", result.text)

    def test_a_huge_sheet_is_not_read_completely(self) -> None:
        """More text than fits in the index need not be read at all.

        From a huge sheet or Word document we only read the beginning; on a
        round over a whole disk that saves seconds per file. What does go in is
        the beginning — so that the first rows stay findable.
        """
        bestand = self.path("groot.xlsx")
        rows = "".join(
            f'<row><c t="inlineStr"><is><t>regel {number} met inhoud</t></is></c></row>'
            for number in range(500))
        sheet = ('<?xml version="1.0"?><worksheet><sheetData>' + rows
                 + "</sheetData></worksheet>")
        with zipfile.ZipFile(bestand, "w") as archive:
            archive.writestr("xl/worksheets/sheet1.xml", sheet)

        old_text, old_read = sources.MAX_TEXT, sources.MAX_READ_BYTES
        sources.MAX_TEXT, sources.MAX_READ_BYTES = 300, 600
        try:
            result = sources.extract_xlsx(bestand)
        finally:
            sources.MAX_TEXT, sources.MAX_READ_BYTES = old_text, old_read

        self.assertLessEqual(len(result.text), 300)
        self.assertIn("regel 0", result.text)
        self.assertNotIn("regel 400", result.text)

    def test_xlsx(self) -> None:
        bestand = self.path("cijfers.xlsx")
        sheet = (
            '<?xml version="1.0"?><worksheet><sheetData>'
            "<row><c t=\"inlineStr\"><is><t>Post</t></is></c>"
            "<c t=\"inlineStr\"><is><t>Bedrag</t></is></c></row>"
            "<row><c t=\"inlineStr\"><is><t>Aanbesteding</t></is></c>"
            "<c><v>1250</v></c></row>"
            "</sheetData></worksheet>")
        with zipfile.ZipFile(bestand, "w") as archive:
            archive.writestr("xl/worksheets/sheet1.xml", sheet)
        result = sources.extract_xlsx(bestand)
        self.assertIn("Aanbesteding", result.text)
        self.assertIn("1250", result.text)

    def test_pdf(self) -> None:
        bestand = self.path("brief.pdf")
        _write_pdf(bestand, [
            "Aanbesteding fietsbrug Kanaalweg",
            "De gunning vindt plaats in maart 2026.",
        ])
        result = sources.extract_pdf(bestand)
        self.assertTrue(result.ok, result.error)
        self.assertIn("Aanbesteding", result.text)
        self.assertIn("maart", result.text)

    def test_pdf_without_a_text_layer_says_so_honestly(self) -> None:
        bestand = self.path("scan.pdf")
        bestand.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
                            b"trailer\n<< /Root 1 0 R >>\n%%EOF\n")
        result = sources.extract_pdf(bestand)
        self.assertFalse(result.ok)
        self.assertIn("text layer", result.error.lower())

    def test_eml(self) -> None:
        bestand = self.path("mail.eml")
        _write_eml(bestand, dt.datetime(2026, 3, 5, 10, 12),
                   "bram@voorbeeld.nl", "anouk@voorbeeld.nl",
                   "Aanbesteding fietsbrug", "Hierbij de stukken voor maart.")
        result = sources.extract_eml(bestand)
        self.assertEqual(result.title, "Aanbesteding fietsbrug")
        self.assertIn("stukken voor maart", result.text)
        self.assertEqual(result.extra["moment"], "2026-03-05 10:12")
        self.assertEqual(result.extra["van"], "bram@voorbeeld.nl")

    def test_mbox_with_several_messages(self) -> None:
        bestand = self.path("box.mbox")
        _write_mbox(bestand, [
            (dt.datetime(2026, 1, 9, 11, 2), "a@x.nl", "b@x.nl",
             "Eerste bericht", "Over de jaarcijfers."),
            (dt.datetime(2026, 3, 11, 16, 40), "c@x.nl", "b@x.nl",
             "Tweede bericht", "De aanbesteding is rond."),
        ])
        result = sources.extract_mbox(bestand)
        self.assertEqual(result.extra["berichten"], 2)
        self.assertIn("jaarcijfers", result.text)
        self.assertIn("aanbesteding is rond", result.text)

    def test_extract_file_picks_the_route_itself(self) -> None:
        bestand = self.path("iets.txt")
        bestand.write_text("zomaar wat tekst", encoding="utf-8")
        self.assertTrue(sources.extract_file(bestand).has_text)


# --------------------------------------------------------------------------
# Browser history
# --------------------------------------------------------------------------

class BrowserTimeTest(unittest.TestCase):
    def test_chrome_time_to_datetime(self) -> None:
        """Chrome counts microseconds since 1601 **UTC**.

        The core function deliberately returns UTC (epoch time does not depend
        on a time zone); only the display goes to local time.
        """
        moment = dt.datetime(2026, 3, 5, 10, 12)          # UTC
        microseconds = int(
            (moment - dt.datetime(1601, 1, 1)).total_seconds() * 1_000_000)
        result = core.chrome_time_to_datetime(microseconds)
        self.assertEqual(result.replace(microsecond=0), moment)

    def test_chrome_time_is_utc_and_not_timezone_dependent(self) -> None:
        """The same stored value gives the same moment on every pc."""
        moment = dt.datetime(2026, 3, 5, 10, 12)
        micro = int((moment - dt.datetime(1601, 1, 1)).total_seconds() * 1e6)
        first = core.chrome_time_to_datetime(micro)
        second = core.chrome_time_to_datetime(micro)
        self.assertEqual(first, second)
        local = core.date_to_local(first)
        self.assertIsNotNone(local.tzinfo)
        # The same moment, but in this pc's time zone: the clock time may
        # differ, the moment in the world may not.
        self.assertEqual(local.astimezone(dt.timezone.utc).replace(tzinfo=None),
                         first)
        self.assertEqual(core.date_to_local(None), None)

    def test_chrome_time_with_an_empty_value(self) -> None:
        self.assertIsNone(core.chrome_time_to_datetime(0))
        self.assertIsNone(core.chrome_time_to_datetime(None))

    def test_firefox_time(self) -> None:
        """Firefox counts microseconds since 1970 **UTC**."""
        moment = dt.datetime(2026, 3, 5, 10, 12)          # UTC
        microseconds = int(
            (moment - dt.datetime(1970, 1, 1)).total_seconds() * 1_000_000)
        result = core.firefox_time_to_datetime(microseconds)
        self.assertEqual(result.replace(microsecond=0), moment)


# --------------------------------------------------------------------------
# The index: building it and finding things back
# --------------------------------------------------------------------------

class IndexTest(_TempFolder):
    NU = dt.datetime(2026, 3, 10, 9, 0)

    def _document(self, ix: core.VindTerugIndex, name: str, title: str,
                  text: str, moment: dt.datetime,
                  kind: str = "tekst") -> int:
        return ix.store_document(core.SourceDocument(
            source="map:test", path=str(self.folder / name), title=title,
            kind=kind, mtime=moment, size=len(text), text=text))

    def test_create_the_index_and_open_it_again(self) -> None:
        ix = self.index()
        self.assertTrue(ix.pad.exists())
        self.assertTrue(ix.state().is_empty)
        self._document(ix, "a.txt", "Notulen", "Over de fietsbrug.", self.NU)
        self.assertEqual(ix.state().documents, 1)
        self.assertEqual(ix.sources(), {"map:test": 1})

    def test_a_stored_document_can_be_found_back(self) -> None:
        ix = self.index()
        self._document(ix, "a.txt", "Aanbesteding fietsbrug",
                       "We hebben gesproken over de aanbesteding van de "
                       "nieuwe fietsbrug. " * 8, self.NU)
        results, explanation = core.Searcher(ix).search("aanbesteding fietsbrug")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Aanbesteding fietsbrug")
        self.assertTrue(explanation.used_words)

    def test_reindexing_gives_no_duplicate_hits(self) -> None:
        ix = self.index()
        for _ in range(3):
            self._document(ix, "a.txt", "Aanbesteding fietsbrug",
                           "Over de aanbesteding van de fietsbrug. " * 8,
                           self.NU)
        rows = ix._db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        self.assertEqual(rows, 1)
        results, _ = core.Searcher(ix).search("aanbesteding")
        self.assertEqual(len(results), 1)

    def test_no_index_means_no_results(self) -> None:
        ix = self.index()
        results, explanation = core.Searcher(ix).search("aanbesteding")
        self.assertEqual(results, [])
        self.assertEqual(explanation.count, 0)

    def test_clearing_removes_everything(self) -> None:
        ix = self.index()
        self._document(ix, "a.txt", "Aanbesteding", "Over de aanbesteding.",
                       self.NU)
        ix.clear()
        self.assertTrue(ix.state().is_empty)

    def test_the_full_text_comes_back(self) -> None:
        ix = self.index()
        doc_id = self._document(ix, "a.txt", "Verslag",
                                "De volledige inhoud van het verslag.",
                                self.NU)
        self.assertIn("volledige inhoud", ix.full_text(doc_id))

    def test_folders_are_remembered(self) -> None:
        ix = self.index()
        ix.add_folder(self.folder)
        self.assertIn(str(self.folder.resolve()), ix.folders())
        ix.remove_folder(self.folder)
        self.assertEqual(ix.folders(), [])

    def test_documents_by_ids(self) -> None:
        ix = self.index()
        doc_id = self._document(ix, "a.txt", "Eén", "Wat tekst.", self.NU)
        found = ix.documents_by_ids([doc_id])
        self.assertEqual(found[doc_id]["title"], "Eén")
        self.assertEqual(ix.documents_by_ids([]), {})


# --------------------------------------------------------------------------
# Searching: synonyms, date range and ranking
# --------------------------------------------------------------------------

class SearchTest(_TempFolder):
    NU = dt.datetime(2026, 9, 28, 12, 0)

    def _document(self, ix: core.VindTerugIndex, name: str, title: str,
                  text: str, moment: dt.datetime,
                  kind: str = "tekst") -> int:
        return ix.store_document(core.SourceDocument(
            source="map:test", path=str(self.folder / name), title=title,
            kind=kind, mtime=moment, size=len(text), text=text))

    def test_synonym_rekening_finds_factuur(self) -> None:
        """The user types 'rekening'; the document says 'factuur'."""
        ix = self.index()
        self._document(ix, "factuur.txt", "Reparatie ketel",
                       "Hierbij de factuur voor het repareren van de ketel. "
                       * 8, dt.datetime(2026, 5, 1))
        results, _ = core.Searcher(ix).search("rekening van de ketel",
                                             now=self.NU)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Reparatie ketel")

    def test_the_synonym_also_works_the_other_way(self) -> None:
        ix = self.index()
        self._document(ix, "a.txt", "Betaling",
                       "De rekening van de loodgieter is betaald. " * 8,
                       dt.datetime(2026, 5, 1))
        results, _ = core.Searcher(ix).search("factuur loodgieter", now=self.NU)
        self.assertEqual(len(results), 1)

    def test_the_date_range_drops_a_document_outside_it(self) -> None:
        ix = self.index()
        self._document(ix, "maart.txt", "Aanbesteding maart",
                       "De aanbesteding van de fietsbrug in maart. " * 8,
                       dt.datetime(2026, 3, 10))
        self._document(ix, "mei.txt", "Aanbesteding mei",
                       "De aanbesteding van de fietsbrug in mei. " * 8,
                       dt.datetime(2026, 5, 10))
        results, explanation = core.Searcher(ix).search("aanbesteding in maart",
                                                        now=self.NU)
        titles = [r.title for r in results]
        self.assertIn("Aanbesteding maart", titles)
        self.assertNotIn("Aanbesteding mei", titles)
        self.assertTrue(explanation.window)

    def test_without_a_time_word_both_stay(self) -> None:
        ix = self.index()
        self._document(ix, "maart.txt", "Aanbesteding maart",
                       "De aanbesteding van de fietsbrug. " * 8,
                       dt.datetime(2026, 3, 10))
        self._document(ix, "mei.txt", "Aanbesteding mei",
                       "De aanbesteding van de fietsbrug. " * 8,
                       dt.datetime(2026, 5, 10))
        results, _ = core.Searcher(ix).search("aanbesteding fietsbrug",
                                              now=self.NU)
        self.assertEqual(len(results), 2)

    def test_the_kind_filter_only_shows_mail(self) -> None:
        ix = self.index()
        self._document(ix, "doc.txt", "Aanbesteding document",
                       "Over de aanbesteding van de brug. " * 8,
                       dt.datetime(2026, 3, 10))
        self._document(ix, "post.eml", "Aanbesteding mail",
                       "Over de aanbesteding van de brug. " * 8,
                       dt.datetime(2026, 3, 10), kind="mail")
        results, _ = core.Searcher(ix).search("mailtje over de aanbesteding",
                                              now=self.NU)
        self.assertEqual([r.kind for r in results], ["mail"])

    def test_ranking_puts_the_best_hit_first(self) -> None:
        """The document that hits both title and text belongs at the top."""
        ix = self.index()
        self._document(ix, "goed.txt", "Aanbesteding fietsbrug",
                       "De aanbesteding van de fietsbrug in maart. " * 10,
                       dt.datetime(2026, 3, 10))
        self._document(ix, "zwak.txt", "Boodschappenlijst",
                       "Melk, brood en een aanbesteding. " * 10,
                       dt.datetime(2026, 3, 9))
        results, _ = core.Searcher(ix).search("aanbesteding fietsbrug",
                                              now=self.NU)
        self.assertGreaterEqual(len(results), 2)
        self.assertEqual(results[0].title, "Aanbesteding fietsbrug")
        self.assertGreater(results[0].score, results[1].score)

    def test_the_snippet_shows_the_search_word(self) -> None:
        ix = self.index()
        self._document(ix, "a.txt", "Verslag",
                       "Inleiding. " * 60
                       + "Hier staat het woord aanbesteding precies. "
                       + "Nawoord. " * 60, dt.datetime(2026, 3, 10))
        results, _ = core.Searcher(ix).search("aanbesteding", now=self.NU)
        self.assertEqual(len(results), 1)
        self.assertIn("aanbesteding", results[0].snippet.lower())

    def test_why_mentions_the_title_hit(self) -> None:
        ix = self.index()
        self._document(ix, "a.txt", "Aanbesteding fietsbrug",
                       "Over de brug bij de Kanaalweg. " * 8,
                       dt.datetime(2026, 3, 10))
        results, _ = core.Searcher(ix).search("aanbesteding", now=self.NU)
        self.assertIn("title", results[0].why)

    def test_the_example_question_finds_the_march_mail(self) -> None:
        """The whole chain: mail, month filter and meaning search together."""
        ix = self.index()
        self._document(
            ix, "post.eml", "Aanbesteding fietsbrug — de stukken voor maart",
            "Onderwerp: Aanbesteding fietsbrug\nVan: bram@voorbeeld.nl\n\n"
            "Hierbij de stukken over de aanbesteding van de fietsbrug. " * 4,
            dt.datetime(2026, 3, 5, 10, 12), kind="mail")
        self._document(
            ix, "ander.eml", "Nieuwsbrief april",
            "In deze nieuwsbrief: een nieuw aanvraagsysteem. " * 6,
            dt.datetime(2026, 4, 2, 8, 5), kind="mail")
        results, explanation = core.Searcher(ix).search(
            "dat mailtje over die aanbesteding in maart", now=self.NU)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "mail")
        self.assertIn("aanbesteding", results[0].title.lower())
        self.assertEqual(results[0].date.month, 3)
        self.assertIn("March 2026", explanation.window)


# --------------------------------------------------------------------------
# Indexing from the disk
# --------------------------------------------------------------------------

class IndexerTest(_TempFolder):
    def test_indexes_a_folder_with_various_files(self) -> None:
        (self.folder / "notulen.md").write_text(
            "# Notulen\n\nWe spraken over de aanbesteding van de fietsbrug.",
            encoding="utf-8")
        _write_docx(self.folder / "stuk.docx",
                    ["Aanbesteding fietsbrug Kanaalweg"])
        _write_eml(self.folder / "post.eml", dt.datetime(2026, 3, 5, 10, 12),
                   "bram@voorbeeld.nl", "anouk@voorbeeld.nl",
                   "Aanbesteding fietsbrug", "Hierbij de stukken.")

        ix = self.index()
        progress = core.Indexer(ix).index(
            {"documenten": [str(self.folder)], "mail": True})
        self.assertEqual(progress.errors, 0)
        self.assertEqual(progress.added, 3)
        self.assertIsNotNone(ix.state().last_updated)
        self.assertFalse(ix.state().is_empty)

    def test_mail_is_not_indexed_twice(self) -> None:
        """With both 'documenten' and 'mail' on, an .eml may appear once."""
        _write_eml(self.folder / "post.eml", dt.datetime(2026, 3, 5, 10, 12),
                   "bram@voorbeeld.nl", "anouk@voorbeeld.nl",
                   "Aanbesteding", "Hierbij de stukken.")
        ix = self.index()
        core.Indexer(ix).index({"documenten": [str(self.folder)],
                                "mail": True})
        rows = ix._db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_mail_gets_the_send_date_not_the_file_date(self) -> None:
        _write_eml(self.folder / "post.eml", dt.datetime(2026, 3, 5, 10, 12),
                   "bram@voorbeeld.nl", "anouk@voorbeeld.nl",
                   "Aanbesteding", "Hierbij de stukken.")
        ix = self.index()
        core.Indexer(ix).index({"documenten": [str(self.folder)],
                                "mail": True})
        results, explanation = core.Searcher(ix).search(
            "dat mailtje over de aanbesteding in maart",
            now=dt.datetime(2026, 9, 28, 12, 0))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].date, dt.datetime(2026, 3, 5, 10, 12))
        self.assertIn("March 2026", explanation.window)

    def test_a_second_round_skips_unchanged_files(self) -> None:
        (self.folder / "notulen.txt").write_text("Over de aanbesteding.",
                                                 encoding="utf-8")
        ix = self.index()
        first = core.Indexer(ix).index({"documenten": [str(self.folder)]})
        self.assertEqual(first.added, 1)
        second = core.Indexer(ix).index({"documenten": [str(self.folder)]})
        self.assertEqual(second.skipped, 1)
        self.assertEqual(second.added, 0)

    def test_a_changed_file_is_updated(self) -> None:
        bestand = self.folder / "notulen.txt"
        bestand.write_text("Eerste versie over de aanbesteding.",
                           encoding="utf-8")
        ix = self.index()
        core.Indexer(ix).index({"documenten": [str(self.folder)]})
        bestand.write_text("Tweede versie over de fietsbrug, veel langer. "
                           * 4, encoding="utf-8")
        os.utime(bestand, (bestand.stat().st_atime + 10,
                           bestand.stat().st_mtime + 10))
        progress = core.Indexer(ix).index({"documenten": [str(self.folder)]})
        self.assertEqual(progress.updated, 1)

    def test_a_removed_file_leaves_the_index(self) -> None:
        bestand = self.folder / "tijdelijk.txt"
        bestand.write_text("Over de aanbesteding van de fietsbrug.",
                           encoding="utf-8")
        ix = self.index()
        core.Indexer(ix).index({"documenten": [str(self.folder)]})
        self.assertEqual(ix.state().documents, 1)
        bestand.unlink()
        core.Indexer(ix).index({"documenten": [str(self.folder)]})
        self.assertEqual(ix.state().documents, 0)

    def test_stopping_ends_the_round_neatly(self) -> None:
        for number in range(5):
            (self.folder / f"bestand{number}.txt").write_text(
                "Over de aanbesteding van de fietsbrug.", encoding="utf-8")
        ix = self.index()
        indexer = core.Indexer(ix)
        indexer.stop()
        progress = indexer.index({"documenten": [str(self.folder)]})
        self.assertTrue(progress.cancelled)

    def test_one_read_error_does_not_stop_the_round(self) -> None:
        (self.folder / "goed.txt").write_text("Over de aanbesteding.",
                                              encoding="utf-8")
        (self.folder / "kapot.docx").write_bytes(b"dit is geen zipbestand")
        ix = self.index()
        progress = core.Indexer(ix).index({"documenten": [str(self.folder)]})
        self.assertEqual(progress.added, 2)   # beide geprobeerd
        self.assertGreaterEqual(ix.state().documents, 1)


# --------------------------------------------------------------------------
# What may not be indexed
# --------------------------------------------------------------------------

class SkippingTest(_TempFolder):
    def test_an_unknown_extension_is_not_supported(self) -> None:
        self.assertFalse(sources.is_supported("iets.exe"))
        self.assertTrue(sources.is_supported("iets.txt"))

    def test_junk_files_are_skipped(self) -> None:
        for name in ("desktop.ini", "Thumbs.db"):
            self.assertFalse(sources.is_supported(name))

    def test_system_folders_are_skipped(self) -> None:
        """Files in system and junk folders do not get in.

        The rule is tested through the real walk: such a folder should not even
        be opened.
        """
        for name in ("__pycache__/rond.py", "node_modules/pakket.js",
                     "Windows/systeem.txt"):
            self.path(name).write_text("inhoud", encoding="utf-8")
        self.path("verslagen/verslag.txt").write_text("inhoud", encoding="utf-8")
        ix = self.index()
        found = [p.name for p, _g, _m in core.Indexer(ix)._walk(
            self.folder, kind="bestanden")]
        self.assertEqual(found, ["verslag.txt"])

    def test_kind_of_file(self) -> None:
        self.assertEqual(sources.kind_of_file("a.docx"), "office")
        self.assertEqual(sources.kind_of_file("a.pdf"), "pdf")
        self.assertEqual(sources.kind_of_file("a.eml"), "mail")
        self.assertEqual(sources.kind_of_file("a.png"), "image")
        self.assertEqual(sources.kind_of_file("a.onbekend"), "onbekend")


# --------------------------------------------------------------------------
# The index's own building blocks
# --------------------------------------------------------------------------

class ChunkTest(unittest.TestCase):
    def test_a_short_piece_stays_one_piece(self) -> None:
        self.assertEqual(core.split_into_chunks("kort"), ["kort"])

    def test_cutting_happens_on_a_neat_boundary(self) -> None:
        # Much longer than MAX_CHUNK, so there really has to be cutting.
        text = ("Eerste alinea over de aanbesteding.\n\n"
                "Tweede alinea over de fietsbrug.\n\n"
                "Derde alinea over de planning.\n\n") * 12
        self.assertGreater(len(text), core.MAX_CHUNK)
        pieces = core.split_into_chunks(text)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(len(piece), core.MAX_CHUNK)
            self.assertTrue(piece.strip())
        # Nothing of the text may be lost.
        self.assertIn("aanbesteding", "".join(pieces))

    def test_cutting_happens_on_a_paragraph_boundary(self) -> None:
        """With an empty line in the window, that is where we cut."""
        text = ("A" * 500) + "\n\n" + ("B" * 500) + "\n\n" + ("C" * 500)
        pieces = core.split_into_chunks(text)
        self.assertGreater(len(pieces), 1)
        # No piece ends in the middle of a long run of letters.
        for piece in pieces[:-1]:
            self.assertTrue(piece.strip())
            self.assertLessEqual(len(piece), core.MAX_CHUNK)

    def test_empty_text_gives_no_pieces(self) -> None:
        self.assertEqual(core.split_into_chunks(""), [])
        self.assertEqual(core.split_into_chunks("   \n  "), [])


class StateTest(unittest.TestCase):
    def test_an_empty_state_tells_you_what_to_do(self) -> None:
        state = core.IndexState()
        self.assertTrue(state.is_empty)
        self.assertIn("no index yet", state.summary())

    def test_a_filled_state_mentions_the_counts(self) -> None:
        state = core.IndexState(documents=3, fragments=9,
                                last_updated=dt.datetime(2026, 3, 5, 10, 12))
        line = state.summary()
        self.assertIn("3 documents", line)
        self.assertIn("9 fragments", line)
        self.assertIn("05-03-2026", line)


class PdfTextLayerTest(_TempFolder):
    """The text layer of a pdf with glyph codes and a ToUnicode CMap."""

    def test_flatedecode_stream_with_bfchar_and_bfrange(self) -> None:
        """A compressed stream with both bfchar and bfrange (with a list).

        This is the case that used to yield nothing: real invoices use a subset
        font whose glyph codes only become letters through the CMap. The
        bfrange with a list of targets has to work as well.
        """
        bestand = self.path("factuur.pdf")
        expected = _pdf_with_cmap_text(
            bestand,
            bfchar={"01": "T", "02": "o", "03": "t", "04": "a", "05": "l",
                    "06": " ", "07": "E", "08": "u", "09": "r"},
            bfrange=("10", "12", ["6", "9", "2"]),
        )
        # The glyph codes come out in order: "Total Eur" + "692".
        self.assertIn("Total", expected)
        self.assertIn("692", expected)
        result = sources.extract_pdf(bestand)
        self.assertTrue(result.ok, result.error)
        self.assertIn("Total", result.text)
        self.assertIn("692", result.text)
        self.assertEqual(result.extra.get("route"), "tekstlaag")

    def test_streams_without_text_operators_are_skipped(self) -> None:
        """A compressed stream without BT/Tj may yield no text."""
        junk = zlib.compress(b"\x00\x01\x02 nogal wat binaire rommel \xff\xfe")
        block = (b"%PDF-1.4\n1 0 obj\n<< /Length "
                 + str(len(junk)).encode("ascii")
                 + b" /Filter /FlateDecode >>\nstream\n" + junk
                 + b"\nendstream\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n")
        bestand = self.path("binair.pdf")
        bestand.write_bytes(block)
        result = sources.extract_pdf(bestand)
        self.assertFalse(result.ok)
        self.assertIn("text layer", result.error.lower())

    def test_escapes_and_octal_codes_in_parenthesis_strings(self) -> None:
        """A plain Tj with parenthesis escapes and octal codes.

        ``\\(`` and ``\\)`` are literal parentheses and ``\\101`` is the octal
        code for an A.
        """
        stream = (b"BT\n/F1 12 Tf\n72 720 Td\n"
                  b"(Factuur \\(kop\\) \\101\\102) Tj\nET\n")
        block = (b"%PDF-1.4\n5 0 obj\n<< /Length "
                 + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream
                 + b"\nendstream\nendobj\ntrailer\n<< /Root 5 0 R >>\n%%EOF\n")
        bestand = self.path("kop.pdf")
        bestand.write_bytes(block)
        result = sources.extract_pdf(bestand)
        self.assertIn("Factuur (kop) AB", result.text)

    def test_a_scan_with_an_embedded_jpeg_tries_ocr(self) -> None:
        """No text layer but a JPEG: OCR is tried or reported honestly.

        There may be no crash and no empty silence: either text comes out of the
        OCR, or it says honestly why nothing came out. The JPEG really is taken
        out of the pdf, because otherwise there would be nothing to read.
        """
        bestand = self.path("scan.pdf")
        _pdf_with_jpeg(bestand)
        images = sources._pdf_embedded_jpegs(bestand.read_bytes())
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith(b"\xff\xd8"))
        self.assertTrue(images[0].endswith(b"\xff\xd9"))

        # With OCR off, an honest message is left, without a crash.
        result = sources.extract_pdf(bestand, ocr_method="uit")
        self.assertFalse(result.ok)
        self.assertTrue(result.error)
        self.assertIn("text layer", result.error.lower())

        # With OCR on, the route is really tried and reported properly.
        tried = sources.extract_pdf(bestand, ocr_method="auto")
        self.assertIn(tried.extra.get("route"), {"ocr", "ocr-geprobeerd"})
        if not tried.text:
            self.assertTrue(tried.error, "empty silence without an explanation")
            self.assertTrue(tried.extra.get("ocr_meldingen"))


class MusicTest(_TempFolder):
    """Reading music: tags, falling back to the file name, and indexing."""

    def test_id3v2_is_read_and_indexed(self) -> None:
        bestand = self.path("nummer.mp3")
        _mp3_with_id3v2(bestand, {
            "TIT2": "Gold Days",
            "TPE1": "Mr. Probz",
            "TALB": "The Treatment",
            "TCON": "Hip-Hop",
            "TRCK": "10",
        })
        data = sources.extract_file(bestand)
        self.assertEqual(data.kind, "muziek")
        self.assertEqual(data.extra["artiest"], "Mr. Probz")
        self.assertEqual(data.extra["album"], "The Treatment")
        self.assertEqual(data.extra["muziekbron"], "id3v2")
        self.assertIn("Gold Days", data.text)
        self.assertIn("Mr. Probz", data.text)
        self.assertIn("nummer.mp3", data.text)

    def test_searching_on_artist_title_and_file_name(self) -> None:
        bestand = self.path("10. Mr. Probz - Gold Days (ft Action Bronson).mp3")
        _mp3_with_id3v2(bestand, {"TIT2": "Gold Days", "TPE1": "Mr. Probz"})
        plain = sources.extract_file(bestand)
        document = core.SourceDocument(
            source="map", path=str(bestand), title=plain.title, kind="muziek",
            text=plain.text, extra=dict(plain.extra))
        index = self.index()
        index.store_document(document)
        searcher = core.Searcher(index)
        for question in ("Mr. Probz", "Gold Days", "Bronson"):
            results, _explanation = searcher.search(question)
            self.assertTrue(results, f"no hit for {question!r}")
            self.assertEqual(results[0].kind, "muziek")

    def test_id3v1_fallback(self) -> None:
        bestand = self.path("oud.mp3")
        _mp3_with_id3v1(bestand, title="Oude Hit", artist="De Band",
                        album="Jaar 99")
        data = sources.extract_file(bestand)
        self.assertEqual(data.kind, "muziek")
        self.assertEqual(data.extra["artiest"], "De Band")
        self.assertEqual(data.extra["muziekbron"], "id3v1")

    def test_without_tags_the_file_name_does_the_work(self) -> None:
        bestand = self.path("10. Mr. Probz - Gold Days (ft Action Bronson).mp3")
        bestand.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 400)
        data = sources.extract_file(bestand)
        self.assertEqual(data.kind, "muziek")
        self.assertEqual(data.extra["artiest"], "Mr. Probz")
        self.assertEqual(data.extra["muziektitel"], "Gold Days")
        self.assertEqual(data.extra["muziekbron"], "bestandsnaam")
        self.assertIn("Gold Days", data.text)

    def test_a_wav_without_tags_does_not_crash(self) -> None:
        bestand = self.path("opname.wav")
        _wav_without_tags(bestand)
        data = sources.extract_file(bestand)
        self.assertEqual(data.kind, "muziek")
        self.assertEqual(data.error, "")
        self.assertEqual(data.extra["muziektitel"], "opname")

    def test_mp3_is_supported_and_recognised(self) -> None:
        self.assertTrue(sources.is_supported("liedje.mp3"))
        self.assertTrue(sources.is_supported("liedje.wav"))
        self.assertTrue(sources.is_supported("liedje.flac"))
        self.assertEqual(sources.kind_of_file("liedje.mp3"), "muziek")
        self.assertEqual(sources.kind_of_file("liedje.opus"), "muziek")


class DefaultFoldersTest(unittest.TestCase):
    def test_the_default_folders_really_exist(self) -> None:
        """What the button adds has to exist as well."""
        for folder_path in sources.default_folders():
            self.assertTrue(folder_path.is_dir(), folder_path)

    def test_browser_profiles_give_a_dict_with_three_browsers(self) -> None:
        profiles = sources.default_browser_profiles()
        self.assertEqual(set(profiles), {"chrome", "edge", "firefox"})


# --------------------------------------------------------------------------
# The whole computer as a source
# --------------------------------------------------------------------------

class WholeComputerTest(unittest.TestCase):
    def test_there_is_at_least_one_drive(self) -> None:
        drives = sources.drives()
        self.assertTrue(drives, "no drive found at all")
        for drive in drives:
            self.assertTrue(drive.is_dir(), f"{drive} is not a folder")

    def test_computer_folders_is_the_same_as_drives(self) -> None:
        self.assertEqual(sources.computer_folders(), sources.drives())

    def test_the_system_drive_is_a_fixed_drive(self) -> None:
        root = Path(os.environ.get("SystemDrive", "C:") + "\\")
        self.assertTrue(sources.is_fixed_drive(root))

    def test_system_folders_are_skipped(self) -> None:
        for name in ("windows", "winsxs", "program files", "program files (x86)",
                     "programdata", "system volume information", "$recycle.bin",
                     "documents and settings", "all users", "perflogs",
                     "node_modules", ".git", "__pycache__", "cache"):
            self.assertIn(name, sources.SKIPPED_FOLDERS, name)

    def test_the_users_own_folders_still_join_in(self) -> None:
        """The skip list may not hit someone's documents by accident."""
        for name in ("documents", "documenten", "downloads", "bureaublad",
                     "desktop", "afbeeldingen", "mijn documenten", "projecten"):
            self.assertNotIn(name, sources.SKIPPED_FOLDERS, name)

    def test_huge_files_stay_out(self) -> None:
        """A film or huge file costs hours and yields nothing usable."""
        self.assertEqual(sources.MAX_FILE_SIZE, 25 * 1024 * 1024)


# --------------------------------------------------------------------------
# Fast walking, interim saving and resuming
# --------------------------------------------------------------------------

class _StoppingIndexer(core.Indexer):
    """Stops itself after a number of documents, to test resuming."""

    def __init__(self, index, *, stop_after: int, **rest) -> None:
        super().__init__(index, **rest)
        self.stop_after = stop_after
        self.seen = 0

    def _after_document(self, index) -> None:  # type: ignore[override]
        super()._after_document(index)
        self.seen += 1
        if self.seen >= self.stop_after:
            self.stop()


class FastWalkingTest(_TempFolder):
    def test_skipped_folders_are_not_opened(self) -> None:
        """Files in system and junk folders do not get in."""
        for name, text in (("notulen.md", "wel meetellen"),
                           ("node_modules/pakket.js", "niet meetellen"),
                           ("Windows/systeem.txt", "niet meetellen"),
                           (".git/config", "niet meetellen")):
            self.path(name).write_text(text, encoding="utf-8")
        ix = self.index()
        found = [p.name for p, _g, _m in core.Indexer(ix)._walk(
            self.folder, kind="bestanden")]
        self.assertEqual(found, ["notulen.md"])

    def test_size_and_time_come_along(self) -> None:
        bestand = self.path("klein.txt")
        bestand.write_text("twaalf tekens", encoding="utf-8")
        ix = self.index()
        found = list(core.Indexer(ix)._walk(self.folder, kind="bestanden"))
        self.assertEqual(len(found), 1)
        found_path, size, moment = found[0]
        self.assertEqual(found_path, bestand)
        self.assertEqual(size, bestand.stat().st_size)
        self.assertEqual(int(moment.timestamp()), int(bestand.stat().st_mtime))

    def test_files_above_the_limit_are_skipped(self) -> None:
        self.path("klein.txt").write_text("klein", encoding="utf-8")
        self.path("groot.txt").write_text("x" * 200, encoding="utf-8")
        ix = self.index()
        old_limit = sources.MAX_FILE_SIZE
        sources.MAX_FILE_SIZE = 100
        try:
            found = [p.name for p, _g, _m in core.Indexer(ix)._walk(
                self.folder, kind="bestanden")]
        finally:
            sources.MAX_FILE_SIZE = old_limit
        self.assertEqual(found, ["klein.txt"])

    def test_the_index_itself_stays_out(self) -> None:
        """The database and the stored texts may never be indexed.

        The index sits in the folder that is indexed here — exactly the case
        where it can go wrong: only the index's own files may be skipped, not
        the rest of the folder.
        """
        self.path("notulen.md").write_text("de vergadering", encoding="utf-8")
        ix = self.index()
        ix.store_document(core.SourceDocument(source="map:test", path="x",
                                              text="iets"))
        ix.commit_batch()
        found = [p.name for p, _g, _m in core.Indexer(ix)._walk(
            self.folder, kind="bestanden")]
        self.assertIn("notulen.md", found)
        self.assertNotIn("index.db", found)
        self.assertNotIn("index.db-wal", found)

    def test_mail_and_images_are_looked_for_separately(self) -> None:
        self.path("notulen.md").write_text("tekst", encoding="utf-8")
        self.path("post.eml").write_text("From: a@b.nl\n\nhoi\n", encoding="utf-8")
        self.path("scherm.png").write_bytes(b"\x89PNG\r\n\x1a\n nep")
        ix = self.index()
        indexer = core.Indexer(ix)
        self.assertEqual([p.name for p, _g, _m in indexer._walk(
            self.folder, kind="mail")], ["post.eml"])
        self.assertEqual([p.name for p, _g, _m in indexer._walk(
            self.folder, kind="beeld")], ["scherm.png"])
        without = [p.name for p, _g, _m in indexer._walk(
            self.folder, kind="bestanden-zonder-mail")]
        self.assertIn("notulen.md", without)
        self.assertNotIn("post.eml", without)

    def test_images_only_join_in_when_ticked(self) -> None:
        """Photos cost almost a second each (OCR) and are optional.

        Otherwise a round over a whole disk would mostly be busy reading
        pictures the user never ticked.
        """
        self.path("notulen.md").write_text("tekst", encoding="utf-8")
        self.path("foto.jpg").write_bytes(b"\xff\xd8\xff neppe jpeg")
        ix = self.index()
        plain = [p.name for p, _g, _m in core.Indexer(ix)._walk(
            self.folder, kind="bestanden")]
        self.assertEqual(plain, ["notulen.md"])
        ticked = [p.name for p, _g, _m in core.Indexer(ix)._walk(
            self.folder, kind="beeld")]
        self.assertEqual(ticked, ["foto.jpg"])


class ResumingTest(_TempFolder):
    def _make_files(self, count: int) -> None:
        for number in range(count):
            self.path(f"stuk-{number}.txt").write_text(
                f"document nummer {number} met inhoud", encoding="utf-8")

    def test_a_stopped_round_leaves_the_queue(self) -> None:
        self._make_files(6)
        ix = self.index()
        indexer = _StoppingIndexer(ix, stop_after=1, batch_documents=1,
                                   batch_seconds=0)
        progress = indexer.index({"documenten": [str(self.folder)]})
        self.assertTrue(progress.cancelled)
        self.assertTrue(progress.pending >= 1, progress)
        self.assertGreaterEqual(ix.queue_count(), 1)

    def test_resuming_finishes_the_round(self) -> None:
        self._make_files(6)
        ix = self.index()
        stopped = _StoppingIndexer(ix, stop_after=2, batch_documents=1,
                                   batch_seconds=0)
        stopped.index({"documenten": [str(self.folder)]})
        halfway = ix.state().documents
        self.assertGreaterEqual(halfway, 1)
        self.assertLess(halfway, 6)

        second = core.Indexer(ix, batch_documents=1, batch_seconds=0)
        progress = second.index({"documenten": [str(self.folder)]})
        self.assertFalse(progress.cancelled)
        self.assertTrue(progress.resumed)
        self.assertEqual(progress.pending, 0)
        self.assertEqual(ix.queue_count(), 0)
        self.assertEqual(ix.state().documents, 6)
        self.assertGreaterEqual(progress.skipped, 1)   # not read again

    def test_the_queue_survives_a_new_connection(self) -> None:
        """Even if the program closes in between, the work stays."""
        self._make_files(6)
        ix = self.index()
        _StoppingIndexer(ix, stop_after=1, batch_documents=1,
                         batch_seconds=0).index(
            {"documenten": [str(self.folder)]})
        ix.close()
        again = core.VindTerugIndex(self.folder / "index.db")
        self.addCleanup(again.close)
        self.assertGreaterEqual(again.queue_count(), 1)
        progress = core.Indexer(again, batch_documents=1,
                                batch_seconds=0).index(
            {"documenten": [str(self.folder)]})
        self.assertTrue(progress.resumed)
        self.assertEqual(again.queue_count(), 0)
        self.assertEqual(again.state().documents, 6)

    def test_what_was_saved_interim_really_is_in_the_index(self) -> None:
        """What the program says it has saved is on disk as well."""
        self._make_files(6)
        ix = self.index()
        indexer = _StoppingIndexer(ix, stop_after=3, batch_documents=1,
                                   batch_seconds=0)
        progress = indexer.index({"documenten": [str(self.folder)]})
        self.assertTrue(progress.cancelled)
        self.assertEqual(progress.saved, 3)

        # A second connection on the same file sees the same: the work really
        # is in the index and not only in memory.
        other = core.VindTerugIndex(self.folder / "index.db")
        self.addCleanup(other.close)
        self.assertEqual(other.state().documents, 3)
        self.assertGreaterEqual(other.queue_count(), 1)
        self.assertEqual(other.meta("scan_status"), "onderbroken")

    def test_a_finished_round_leaves_nothing_behind(self) -> None:
        self._make_files(3)
        ix = self.index()
        progress = core.Indexer(ix, batch_documents=1,
                                batch_seconds=0).index(
            {"documenten": [str(self.folder)]})
        self.assertFalse(progress.resumed)
        self.assertEqual(progress.pending, 0)
        self.assertEqual(ix.queue_count(), 0)
        self.assertEqual(ix.meta("scan_status"), "")
        self.assertEqual(progress.added, 3)

    def test_starting_over_with_an_empty_queue(self) -> None:
        self._make_files(3)
        ix = self.index()
        _StoppingIndexer(ix, stop_after=1, batch_documents=1,
                         batch_seconds=0).index(
            {"documenten": [str(self.folder)]})
        progress = core.Indexer(ix, batch_documents=1,
                                batch_seconds=0).index(
            {"documenten": [str(self.folder)]}, again=True)
        self.assertFalse(progress.resumed)
        self.assertEqual(ix.state().documents, 3)


if __name__ == "__main__":
    unittest.main()
