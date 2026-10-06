"""Dutch language helpers: cutting, stemming, synonyms and stopwords.

This file is the linguistic underlayer of VindTerug and contains no
database or GUI code, so everything here can be tested on its own.

The approach is deliberately modest: no big dictionary and no language model,
but just enough to understand "dat mailtje over die aanbesteding in maart".
Everything is pure Python (no packages outside the standard library).
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "STOPWORDS",
    "SYNONYMS",
    "normalise",
    "tokenise",
    "stem",
    "unfold",
    "expand",
    "remove_stopwords",
    "keywords",
    "meaning_variants",
]

# --------------------------------------------------------------------------
# Stopwords
# --------------------------------------------------------------------------

# Words that add nothing to a search query. Deliberately wide: for a query
# like "dat mailtje over die aanbesteding in maart" you want to keep only "mailtje" and
# "aanbesteding". The kind and time words ("mailtje", "maart")
# are recognised before this filter, so they are deliberately not in here.
STOPWORDS: set[str] = {
    # Dutch
    "de", "het", "een", "en", "of", "maar", "want", "dus", "als", "dan",
    "dat", "die", "dit", "deze", "zulke", "zo", "er", "daar", "hier",
    "ik", "je", "jij", "u", "we", "wij", "ze", "zij", "hij", "hem", "haar",
    "me", "mij", "mijn", "ons", "onze", "jouw", "uw", "hun",
    "is", "was", "waren", "ben", "bent", "zijn", "wordt", "worden", "werd",
    "heb", "hebt", "heeft", "had", "hadden", "kan", "kon", "kunnen", "zou",
    "zal", "zullen", "moet", "moeten", "mag", "mocht", "wil", "wilde",
    "in", "op", "aan", "bij", "met", "van", "voor", "naar", "over", "onder",
    "tussen", "tot", "uit", "door", "om", "te", "ook", "nog", "al", "eens",
    "even", "weer", "niet", "geen", "wel", "toch", "maar", "gewoon",
    "iets", "iemand", "ergens", "niets", "alle", "elke", "ieder", "sommige",
    "wat", "wie", "waar", "wanneer", "welke", "hoe", "welk",
    "the", "a", "an", "and", "or", "but", "of", "to", "for", "with", "about",
    "from", "into", "at", "on", "in", "by", "as", "that", "this", "these",
    "those", "it", "its", "is", "was", "were", "be", "been", "am", "are",
    "do", "does", "did", "have", "has", "had", "my", "your", "his", "her",
    "our", "their", "me", "you", "we", "they", "he", "she", "some", "any",
    "please", "find", "search", "show",
}

# --------------------------------------------------------------------------
# Synonyms
# --------------------------------------------------------------------------

# Small Dutch synonym list with English variants. The key is the
# word as the user types it, the value are related words that
# mean the same thing. That way "rekening" also finds documents with "factuur".
SYNONYMS: dict[str, tuple[str, ...]] = {
    # money and administration
    "aanbesteding": ("tender", "tendering", "offerteaanvraag", "inschrijving",
                     "gunning", "bestek", "tenderprocedure"),
    "tender": ("aanbesteding", "offerteaanvraag", "inschrijving", "gunning"),
    "offerte": ("quotation", "quote", "prijsopgave", "aanbieding", "tender"),
    "factuur": ("rekening", "nota", "invoice", "billing", "facturatie"),
    "rekening": ("factuur", "nota", "invoice", "bankafschrift"),
    "nota": ("factuur", "rekening", "invoice"),
    "betaling": ("payment", "incasso", "overschrijving", "betaalopdracht",
                 "afschrijving", "transactie"),
    "incasso": ("betaling", "afschrijving", "automatische incasso"),
    "overschrijving": ("betaling", "overboeking", "transactie", "transfer"),
    "bankafschrift": ("rekening", "transactie", "afschrift", "betaling"),
    "belasting": ("tax", "aangifte", "btw", "voorlopige aanslag"),
    "aangifte": ("belasting", "belastingaangifte", "btw", "tax"),
    "verzekering": ("insurance", "polis", "premie", "dekking"),
    "polis": ("verzekering", "premie", "insurance"),
    "salaris": ("loon", "loonstrook", "salary", "uitbetaling"),
    "loonstrook": ("salaris", "loon", "payslip", "uitbetaling"),
    # werk en overleg
    "vergadering": ("overleg", "meeting", "bijeenkomst", "bespreking",
                    "vergaderstukken", "notulen"),
    "overleg": ("vergadering", "meeting", "bespreking", "afstemming"),
    "meeting": ("vergadering", "overleg", "bespreking"),
    "notulen": ("verslag", "minutes", "vergadering", "bespreking"),
    "verslag": ("rapport", "report", "notulen", "samenvatting", "verantwoording"),
    "rapport": ("verslag", "report", "onderzoek", "eindrapport"),
    "afspraak": ("appointment", "agenda", "kalender", "agendapunt", "afspreken"),
    "agenda": ("afspraak", "kalender", "schedule", "planning", "agenderen"),
    "planning": ("agenda", "plan", "schedule", "rooster", "tijdschema"),
    "project": ("projectplan", "traject", "programma", "opdracht"),
    "opdracht": ("assignment", "klus", "project", "taak", "briefing"),
    "klant": ("client", "opdrachtgever", "relatie", "klanten"),
    "sollicitatie": ("solliciteren", "vacature", "application", "cv",
                     "sollicitatiebrief"),
    "vacature": ("sollicitatie", "functie", "job", "opening", "werving"),
    "contract": ("overeenkomst", "agreement", "getekend", "verbintenis"),
    "overeenkomst": ("contract", "agreement", "afspraak"),
    # documenten
    "document": ("bestand", "file", "stuk", "papier", "documentatie"),
    "brief": ("letter", "mail", "schrijven", "begeleidend schrijven"),
    "handleiding": ("manual", "instructie", "gebruiksaanwijzing", "gids"),
    "instructie": ("handleiding", "uitleg", "procedure", "instructies"),
    "formulier": ("form", "formulieren", "aanvraagformulier", "invulformulier"),
    "aanvraag": ("request", "verzoek", "application", "formulier", "indienen"),
    "verzoek": ("aanvraag", "request", "vraag", "oproep"),
    "beslissing": ("besluit", "decision", "goedkeuring", "akkoord"),
    "goedkeuring": ("akkoord", "approval", "toestemming", "beslissing"),
    # techniek
    "storing": ("error", "fout", "probleem", "incident", "defect"),
    "fout": ("error", "storing", "probleem", "bug", "failure"),
    "wachtwoord": ("password", "inloggen", "login", "credentials", "aanmelden"),
    "inloggen": ("login", "wachtwoord", "aanmelden", "signin", "account"),
    "update": ("wijziging", "release", "nieuwe versie", "patch", "vernieuwing"),
    "backup": ("reservekopie", "kopie", "archief", "herstelpunt"),
    "computer": ("pc", "laptop", "werkstation", "systeem", "machine"),
    # vervoer en reizen
    "reis": ("trip", "vakantie", "booking", "reisbescheiden", "itinerary"),
    "vlucht": ("flight", "boarding", "vliegticket", "luchtvaart"),
    "trein": ("ns", "rail", "spoor", "ov", "treinticket"),
    "hotel": ("overnachting", "booking", "accommodatie", "verblijf"),
    "woning": ("huis", "pand", "koopwoning", "woningmarkt", "adres"),
    "adres": ("address", "locatie", "straat", "postcode", "woonplaats"),
}


def _key(word: str) -> str:
    """Make a synonym-list key: lower case, without accents."""
    return normalise(word).strip()


# --------------------------------------------------------------------------
# Normalising and cutting
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+(?:['’][a-z]+)?")


def normalise(text: str) -> str:
    """Turn text into lower case without accents.

    "Aanbesteding Curaçao" becomes "aanbesteding curacao", so you can also search
    without diaereses.
    """
    if not text:
        return ""
    parsed = unicodedata.normalize("NFKD", str(text).lower())
    flat = "".join(ch for ch in parsed if not unicodedata.combining(ch))
    return flat.replace("’", "'")


def tokenise(text: str) -> list[str]:
    """Cut text into words of letters and digits.

    Amounts and dates stay usable: "€ 12.500,00" gives
    ``["12", "500", "00"]`` and ``2025-03-11`` gives ``["2025", "03", "11"]``.
    """
    return _WORD_RE.findall(normalise(text))


_IRREGULAR: dict[str, str] = {
    # words where the plural shortens the vowel, so that stem(singular) and
    # stem(plural) come out the same. Small, explicit list: guessing at
    # spelling would wrongly turn "notulen" into "notuul".
    "factuur": "factuur", "facturen": "factuur",
    "jaar": "jaar", "jaren": "jaar",
    "uur": "uur", "uren": "uur",
    "boom": "boom", "bomen": "boom",
    "maan": "maan", "manen": "maan",
    "schaar": "schaar", "scharen": "schaar",
}


def stem(word: str) -> str:
    """Very simple stemming, only for Dutch and English.

    Deliberately conservative: only known endings come off and a stem is
    never shorter than four characters, so that "kas", "mei" and "bus" stay whole.

    More important than linguistic beauty is that the singular and the
    plural come out at exactly the same stem, and that the function is idempotent:
    ``stem(stem(w)) == stem(w)``. That is why the plural comes off first and
    then the ending rule runs once more over what is left. That way
    ``aanbesteding`` and ``aanbestedingen`` both give ``aanbested``, and
    ``facturen`` and ``factuur`` both give ``factuur``. Where the plural shortens
    the vowel, the pair is in :data:`_IRREGULAR`.
    """
    w = normalise(word).strip()
    if len(w) <= 3 or not w.isalpha():
        return w

    if w in _IRREGULAR:
        return _IRREGULAR[w]

    # First take the plural off. "mogelijkheden" is the plural of
    # "mogelijkheid", not of "mogeli".
    if w.endswith("heden") and len(w) - 5 >= 3:
        w = w[: -5] + "heid"
    elif w.endswith("en") and len(w) - 2 >= 3:
        w = w[:-2]                              # aanbestedingen -> aanbesteding
    elif w.endswith("s") and len(w) - 1 >= 4 and not w.endswith(("ss", "us", "is")):
        w = w[:-1]                              # invoices -> invoice, mails -> mail

    # Then the ending rule on what is left. Because this is the same rule
    # that a bare singular also goes through, singular and plural come out
    # the same, and a second round of stemming keeps giving the same result.
    if w.endswith("ing") and len(w) - 3 >= 3:
        return w[:-3]                           # vergadering -> vergader
    if w.endswith("ende") and len(w) - 4 >= 4:
        return w[:-4]                           # lopende -> lopend?
    return w


def _index() -> dict[str, tuple[str, ...]]:
    """Build the synonym index with stemmed keys added.

    By also taking the stem form as a key, "rekeningen" and
    "facturen" find the same family as "rekening" and "factuur".
    """
    out: dict[str, tuple[str, ...]] = {}
    for word, relatives in SYNONYMS.items():
        key = _key(word)
        relative = tuple(dict.fromkeys(_key(w) for w in relatives if _key(w)))
        out.setdefault(key, relative)
        out.setdefault(stem(key), relative)
    return out


_SYNONYM_INDEX: dict[str, tuple[str, ...]] = _index()


def unfold(word: str) -> str:
    """Bring a word back to its simple form (for indexing).

    ``vergoedingen`` → ``vergoeding``, ``aanbestedingen`` → ``aanbesteding``,
    ``meetings`` → ``meeting``, ``betaald`` → ``betaal``.
    """
    return stem(word)


def expand(word: str) -> set[str]:
    """All forms that could look like this word (for searching)."""
    return {stem(word)}


def remove_stopwords(terms: list[str]) -> list[str]:
    """Take stopwords out, but never the last word."""
    if not terms:
        return []
    kept = [w for w in terms if w not in STOPWORDS and len(w) > 1]
    if not kept:
        return [w for w in terms if len(w) > 1] or terms[:1]
    return kept


def meaning_variants(word: str) -> list[str]:
    """Synonyms of a word, including stem forms of those synonyms.

    ``meaning_variants("rekening")`` contains among others ``factuur``, ``nota``
    and ``invoice``. That way the searcher also finds documents that use the
    synonym, even when the given word appears nowhere literally.
    """
    key = normalise(word).strip()
    if not key:
        return []
    relatives = _SYNONYM_INDEX.get(key) or _SYNONYM_INDEX.get(stem(key)) or ()
    out: list[str] = []
    for w in relatives:
        for form in (w, stem(w)):
            if form and form != key and form not in out:
                out.append(form)
    return out


def keywords(terms: list[str], *, with_synonyms: bool = True) -> list[str]:
    """The words we really search on, including synonyms.

    The first words are the words the user typed themselves; they get
    priority in weighing. After that come the synonyms.
    """
    out: list[str] = []
    for word in terms:
        # Only the stem, not also the raw word: otherwise a document that
        # contains the plural would get no title boost for the singular, and one
        # term would count as two hits.
        for form in (stem(word),):
            if form and form not in out and form not in STOPWORDS:
                out.append(form)
        if with_synonyms:
            for variant in meaning_variants(word):
                if variant not in out:
                    out.append(variant)
    return out


def without_stopwords(text: str) -> str:
    """All words from a text that are not a stopword (for snippets)."""
    return " ".join(w for w in tokenise(text) if w not in STOPWORDS)
