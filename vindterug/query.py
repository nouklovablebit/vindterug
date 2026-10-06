"""Understanding search queries: kind, time and meaning.

An ordinary search query like "dat mailtje over die aanbesteding in maart" contains
three kinds of information:

* **kind** — mail, image, document or website (``QueryKind.MAIL`` …);
* **time** — a range within which the file must fall;
* **subject** — the words it is really about, expanded with
  synonyms so that "rekening" also finds a document with "factuur".

This module is pure: ``parse_query()`` does no database and no network,
so the tests can call it without any setup.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum

from . import tekst
from . import tijd

__all__ = ["QueryKind", "Query", "parse_query", "KIND_WORDS", "describe_explanation"]


class QueryKind(str, Enum):
    """The kind of result the user is asking for."""

    ALL = "alles"
    MAIL = "mail"
    IMAGE = "image"
    FILE = "file"
    BROWSER = "browser"

    @property
    def label(self) -> str:
        """Dutch word for this kind."""
        return {
            QueryKind.ALL: "alles",
            QueryKind.MAIL: "e-mail",
            QueryKind.IMAGE: "afbeelding",
            QueryKind.FILE: "bestand",
            QueryKind.BROWSER: "website",
        }[self]


# Words that give the kind away. The order within a list does not
# matter; the order of the lists does: a question may get only one kind
# and the first group that finds something wins.
KIND_WORDS: tuple[tuple[QueryKind, tuple[str, ...]], ...] = (
    (QueryKind.MAIL, (
        "mailtje", "mail", "mailtjes", "e-mail", "email", "e-mails", "emails",
        "mailbericht", "bericht", "berichten", "inbox", "postvak", "outlook",
        "thunderbird", "verzonden", "ontvangen", "e-mailtje", "reply", "forward",
    )),
    (QueryKind.IMAGE, (
        "screenshot", "screenshots", "schermafdruk", "schermopname", "printscreen",
        "foto", "foto's", "fotos", "afbeelding", "afbeeldingen", "plaatje",
        "plaatjes", "image", "images", "picture", "pictures", "png", "jpg",
        "jpeg", "gif", "bmp", "webp", "scan", "ingescand",
    )),
    (QueryKind.BROWSER, (
        "website", "websites", "site", "sites", "webpagina", "webpage", "link",
        "links", "browser", "browsen", "bezocht", "bezochte", "url", "urls",
        "bookmark", "favoriet", "favorieten", "chrome", "edge", "firefox",
        "internet", "online", "gedownload", "download",
    )),
    (QueryKind.FILE, (
        "pdf", "document", "documenten", "bestand", "bestanden", "file", "files",
        "word", "docx", "excel", "xlsx", "powerpoint", "pptx", "presentatie",
        "spreadsheet", "tekstdocument", "notitie", "notities", "rapport",
        "verslag", "brief", "map", "mapje", "folder",
    )),
)

# What the user typed -> the matching kind word, for the explanation.
_KIND_EXPLANATION = {
    QueryKind.MAIL: "je vroeg om een mailtje",
    QueryKind.IMAGE: "je vroeg om een afbeelding of schermafdruk",
    QueryKind.BROWSER: "je vroeg om iets dat je online hebt bekeken",
    QueryKind.FILE: "je vroeg om een document of bestand",
    QueryKind.ALL: "je vroeg niet om een bepaalde soort",
}


@dataclass
class Query:
    """A fully understood search query."""

    original: str = ""
    kind: QueryKind = QueryKind.ALL
    period: tijd.Period | None = None
    time_recognition: str = ""
    precision: str = "unknown"
    terms: list[str] = field(default_factory=list)           # what the user typed, filtered down to the useful words
    search_words: list[str] = field(default_factory=list)    # terms + synonyms, what FTS searches on
    rest: str = ""                                            # the question without kind and time words

    @property
    def has_search_words(self) -> bool:
        return bool(self.search_words)

    @property
    def explanation(self) -> str:
        """Short explanation of what was taken from the query (for the GUI)."""
        return describe_explanation(self)

    def as_dict(self) -> dict:
        """Everything the GUI and the tests want to know, in one dictionary."""
        return {
            "original": self.original,
            "kind": self.kind.value,
            "kind_label": self.kind.label,
            "period": (
                {
                    "start": self.period.start.isoformat(),
                    "end": self.period.end.isoformat(),
                    "label": self.period.describe(),
                }
                if self.period
                else None
            ),
            "time_recognition": self.time_recognition,
            "precision": self.precision,
            "terms": list(self.terms),
            "search_words": list(self.search_words),
            "rest": self.rest,
            "explanation": self.explanation,
        }


def _all_kind_words() -> dict[str, QueryKind]:
    """Reverse map from kind word to kind, with stem forms added."""
    out: dict[str, QueryKind] = {}
    for kind, words in KIND_WORDS:
        for word in words:
            out.setdefault(word, kind)
            out.setdefault(tekst.stem(word), kind)
    return out


_KIND_INDEX: dict[str, QueryKind] = _all_kind_words()


def _find_kind(terms: list[str]) -> tuple[QueryKind, str]:
    """Find the kind in the words of the query.

    Also returns the word that gave the kind away, so the explanation stays
    concrete ("hit: mailtje").
    """
    for term in terms:
        for form in (term, tekst.stem(term)):
            kind = _KIND_INDEX.get(form)
            if kind is not None:
                return kind, term
    return QueryKind.ALL, ""


def parse_query(question: str, now: dt.datetime | None = None) -> Query:
    """Read a search query and pull kind, time and meaning out of it."""
    raw = (question or "").strip()
    q = Query(original=raw, rest=tekst.normalise(raw))
    if not raw:
        return q

    # 1. Take the time out: that changes the text that is left.
    found = tijd.parse_time(raw, now)
    q.period = found.period
    q.time_recognition = found.recognition
    q.precision = found.precision
    q.rest = found.rest

    # 2. Take the kind out: "mailtje" is a kind, not a subject.
    raw_terms = tekst.tokenise(q.rest)
    q.kind, _kind_word = _find_kind(raw_terms)

    # 3. Subject: take stopwords and kind words off, then stem + synonyms.
    content = [w for w in raw_terms if tekst.stem(w) not in _KIND_INDEX
               and w not in tekst.STOPWORDS]
    q.terms = tekst.remove_stopwords(content)
    q.search_words = tekst.keywords(q.terms)
    return q


def describe_explanation(query: Query) -> str:
    """Sentence that explains what VindTerug took from the query."""
    parts: list[str] = []
    if query.terms:
        parts.append("ik zoek op: " + ", ".join(query.terms))
    else:
        parts.append("ik zoek op de hele vraag")
    if query.kind is not QueryKind.ALL:
        parts.append(_KIND_EXPLANATION[query.kind])
    if query.period is not None:
        where = f" — {query.time_recognition}" if query.time_recognition else ""
        parts.append(f"en het moet uit {query.period.describe()} komen{where}")
    return "; ".join(parts)
