"""Starting point of VindTerug.

    python -m vindterug             start the interface
    python -m vindterug --demo      show on the console what the program does
    python -m vindterug --selftest  build the interface, pump one round and close

The demo makes its own example files in a temporary folder, builds an index
over them and does one search query. That way you can see the program straight
away, without having to index your own documents first.
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
import tempfile
import textwrap
import zipfile
from pathlib import Path

from . import __version__
from . import core
from . import sources
from . import theme

APP_NAME = "VindTerug"


# --------------------------------------------------------------------------
# Example material for the demo
# --------------------------------------------------------------------------

DOCX_DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{paragraph}</w:body>
</w:document>
"""


def _docx_paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def make_docx(path: Path, lines: list[str]) -> None:
    """Write a minimal but valid .docx (a zip with XML in it)."""
    document = DOCX_DOCUMENT.format(
        paragraph="".join(_docx_paragraph(line) for line in lines))
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)


def _eml(when: dt.datetime, sender: str, to: str, subject: str,
         body: str) -> str:
    """Build an .eml message as plain text.

    The line endings are \\r\\n with an empty line between the header and the
    text: that is what the e-mail standard prescribes, and it is also what an
    mbox file needs to keep the messages apart.
    """
    date = when.strftime("%a, %d %b %Y %H:%M:%S +0200")
    lines = [
        f"From: {sender}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Date: {date}",
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
        "",
    ] + body.splitlines()
    return "\r\n".join(lines) + "\r\n"


def make_demo_folder() -> tuple[Path, dt.datetime]:
    """Make a temporary folder with example documents, mail and history.

    Gives back the folder plus the moment the demo counts with, so that the
    search query always gives the same answer.
    """
    base = Path(tempfile.mkdtemp(prefix="vindterug-demo-"))
    documents = base / "documenten"
    mail = base / "mail"
    documents.mkdir()
    mail.mkdir()

    (documents / "notulen-vergadering.md").write_text(
        textwrap.dedent("""\
            # Notulen projectvergadering

            Aanwezig: Anouk, Bram, Chantal.

            We hebben gesproken over de aanbesteding van de nieuwe fietsbrug.
            De inschrijving moet voor de zomer binnen zijn. Bram stuurt de
            stukken naar de projectgroep.
            """),
        encoding="utf-8")

    (documents / "handleiding.txt").write_text(
        textwrap.dedent("""\
            Handleiding van de koffiemachine

            Stap 1: vul het waterreservoir met schoon water.
            Stap 2: doe een filterzakje in het filter.
            Stap 3: druk op de knop om het zetten te starten.
            """),
        encoding="utf-8")

    make_docx(documents / "aanbestedingsdocument.docx", [
        "Aanbesteding fietsbrug Kanaalweg",
        "De gemeente nodigt u uit om in te schrijven op de aanbesteding.",
        "De gunning vindt plaats in maart. Vragen kunt u stellen tot de "
        "sluitingsdatum.",
    ])

    # The mail the example question looks for: sent in March 2026. That send
    # date is also the date on which VindTerug finds it back.
    (mail / "aanbesteding-maart.eml").write_text(
        _eml(dt.datetime(2026, 3, 5, 10, 12),
             "bram@voorbeeld.nl", "anouk@voorbeeld.nl",
             "Aanbesteding fietsbrug — de stukken voor maart",
             "Hoi Anouk,\n\nHierbij de stukken over de aanbesteding van de "
             "fietsbrug. De inschrijving moet voor de zomer binnen zijn.\n\n"
             "Groeten,\nBram"),
        encoding="utf-8", newline="")

    # An mbox starts each message with a line beginning with 'From ', followed
    # by the date. Without that line the reader sees one long message.
    mbox_parts = []
    for when, sender, to, subject, body in ((
            dt.datetime(2026, 1, 9, 11, 2), "charlotte@voorbeeld.nl",
            "anouk@voorbeeld.nl", "Vraag over de jaarcijfers",
            "Anouk, kun je de cijfers van het eerste kwartaal aanleveren?"),
            (dt.datetime(2026, 3, 11, 16, 40), "charlotte@voorbeeld.nl",
             "anouk@voorbeeld.nl", "Vraag over de jaarcijfers",
             "Anouk, kun je de cijfers van het eerste kwartaal aanleveren?"),
            (dt.datetime(2026, 4, 2, 8, 5), "nieuwsbrief@voorbeeld.nl",
             "anouk@voorbeeld.nl", "Nieuwsbrief april",
             "In deze nieuwsbrief: een nieuw aanvraagsysteem en meer.")):
        header = "From " + when.strftime("%a %b %d %H:%M:%S %Y") + "\r\n"
        mbox_parts.append(header + _eml(when, sender, to, subject, body))
    (mail / "mailbox.mbox").write_text("\r\n".join(mbox_parts),
                                       encoding="utf-8", newline="")

    return base, dt.datetime(2026, 9, 28, 12, 0)


# --------------------------------------------------------------------------
# The demo on the console
# --------------------------------------------------------------------------

def demo() -> int:
    """Build an example index and do one search query. Gives an exit code."""
    print(f"VindTerug {__version__} — demo")
    print("=" * 66)

    base, now = make_demo_folder()
    index_path = base / "index" / "index.db"
    try:
        print()
        print("Example material in a temporary folder:")
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.parent.name in ("documenten", "mail"):
                size = path.stat().st_size
                print(f"  {path.relative_to(base)!s:42} {size:>7} bytes")

        print()
        print("Get text from a .docx (without Word):")
        docx = base / "documenten" / "aanbestedingsdocument.docx"
        extraction = sources.extract_docx(docx)
        first = extraction.text.splitlines()[0] if extraction.text else "(empty)"
        print(f"  title : {extraction.title}")
        print(f"  first line: {first}")
        print(f"  {len(extraction.text)} characters read")

        print()
        print("Building the index:")
        with core.VindTerugIndex(index_path) as index:
            # Add the folder above it, so that both the documents and the mail
            # are found in one go.
            index.add_folder(base)
            indexer = core.Indexer(index)
            progress = indexer.index({
                "documenten": [str(base)],
                "mail": True,
                "screenshots": False,
                "browser": False,
            })
            state = index.state()
            print(f"  {progress.added} documents added, "
                  f"{progress.skipped} skipped, "
                  f"{progress.errors} failed")
            print(f"  index: {state.summary()}")
            print(f"  file: {index_path}")

            query_text = "dat mailtje over de aanbesteding in maart"
            print()
            print(f'Search query: "{query_text}"')
            searcher = core.Searcher(index)
            results, explanation = searcher.search(query_text, now=now)
            for line in explanation.lines():
                print(f"  · {line}")
            print()
            if not results:
                print("  (no results)")
                return 1
            print(f"  {len(results)} result(s), best first:")
            for number, result in enumerate(results[:5], start=1):
                date = (result.date.strftime("%d-%m-%Y")
                        if result.date else "unknown")
                print(f"   {number}. {result.title}")
                print(f"      kind  : {result.kind or 'unknown'}")
                print(f"      date  : {date}")
                print(f"      where : {result.what()}")
                print(f"      why   : {result.why}")
                print(f"      snippet: {result.snippet[:150]}…")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print()
    print("=" * 66)
    print("Done. Start the interface with:  python -m vindterug")
    return 0


# --------------------------------------------------------------------------
# Starting
# --------------------------------------------------------------------------

def _start_gui() -> int:
    from . import gui

    return theme.run_app(APP_NAME, gui.build_ui, geometry="1200x780",
                         min_size=(1020, 640))


def main(argv: list[str] | None = None) -> int:
    """Choose between the interface, the demo and the smoke test."""
    parser = argparse.ArgumentParser(
        prog="vindterug",
        description="Search by meaning in your own documents, mail, "
                    "screenshots and browser history.")
    parser.add_argument("--demo", action="store_true",
                        help="run a demonstration on the console with "
                             "example files in a temporary folder")
    parser.add_argument("--selftest", action="store_true",
                        help="build the interface, do one round and close again")
    parser.add_argument("--version", action="version",
                        version=f"VindTerug {__version__}")
    arguments = parser.parse_args(argv)

    if arguments.demo:
        return demo()
    # ``--selftest`` should run through the shared start routine: that sets the
    # flag in ``sys.argv`` and then builds the same interface as usual.
    if arguments.selftest and "--selftest" not in sys.argv:
        sys.argv.append("--selftest")
    return _start_gui()


if __name__ == "__main__":
    sys.exit(main())
