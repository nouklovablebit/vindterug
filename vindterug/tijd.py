"""Recognise time expressions in ordinary Dutch sentences.

A search question such as "dat mailtje over die aanbesteding in maart" consists of
two kinds of information: *what it is about* and *when it was*. This file pulls
out the second: it returns a text without the time words plus a
date range (:class:`Period`) that the index can be filtered on.

All functions are pure: the same input plus the same ``nu`` always gives the
same answer, which makes the tests predictable.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from .tekst import normalise


__all__ = [
    "MONTHS",
    "Period",
    "FoundTime",
    "parse_time",
    "month_name",
    "describe_period",
    "within",
    "MONTH_NAME",
]


def _month_index() -> dict[str, int]:
    """Month names (Dutch, English and the usual abbreviations)."""
    nl = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
          "augustus", "september", "oktober", "november", "december"]
    en = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
    # The abbreviations are listed separately: "mrt" is the Dutch one, "mar" the
    # English one.
    afk = ["jan", "feb", "mrt", "mar", "apr", "mei", "may", "jun", "jul",
           "aug", "sep", "sept", "okt", "oct", "nov", "dec"]
    out: dict[str, int] = {}
    for series in (nl, en):
        for number, name in enumerate(series, start=1):
            out.setdefault(name, number)
    for number, name in enumerate(nl, start=1):
        out.setdefault(name[:3], number)
    for name in afk:
        if name in out:
            continue
        for number, real_name in enumerate(en, start=1):
            if real_name.startswith(name):
                out[name] = number
                break
    return out


MONTHS: dict[str, int] = _month_index()

_WEEKDAYS = {
    "maandag": 0, "dinsdag": 1, "woensdag": 2, "donderdag": 3,
    "vrijdag": 4, "zaterdag": 5, "zondag": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_MONTH_NL = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
             "augustus", "september", "oktober", "november", "december"]

MONTH_NAME = "|".join(sorted(MONTHS, key=len, reverse=True))


@dataclass(frozen=True)
class Period:
    """A time range a document has to fall within.

    ``start`` and ``end`` are inclusive and always sit on whole days, so that
    filtering on ``mtime >= start.timestamp()`` has no edge cases.
    """

    start: dt.datetime
    end: dt.datetime
    label: str = ""

    def contains(self, moment: dt.datetime | None) -> bool:
        if moment is None:
            return False
        return self.start <= moment <= self.end

    def describe(self) -> str:
        return self.label or describe_period(self)


@dataclass(frozen=True)
class FoundTime:
    """The result of reading one search question."""

    period: Period | None
    rest: str                      # the search question without the time words
    recognition: str               # the piece that indicated the time
    precision: str = "day"         # "day", "month", "year" or "unknown"


def month_name(number: int) -> str:
    """Dutch month name for month number 1..12."""
    if 1 <= number <= 12:
        return _MONTH_NL[number - 1]
    return ""


def describe_period(period: Period | None) -> str:
    """Short, readable description of a range."""
    if period is None:
        return "altijd"
    s, e = period.start, period.end
    if s.year == e.year and s.month == e.month and s.day == e.day:
        return s.strftime("%d-%m-%Y")
    if s.year == e.year and s.month == e.month and s.day == 1:
        return f"{month_name(s.month)} {s.year}"
    if s.year == e.year and (s.month, s.day) == (1, 1):
        return str(s.year)
    return f"{s.strftime('%d-%m-%Y')} t/m {e.strftime('%d-%m-%Y')}"


def _day(moment: dt.datetime) -> dt.datetime:
    return dt.datetime(moment.year, moment.month, moment.day)


def _month_start(year: int, month: int) -> dt.datetime:
    return dt.datetime(year, month, 1)


def _month_end(year: int, month: int) -> dt.datetime:
    """The last moment of a month: the last day at 23:59:59.

    Without the time, "in maart" would not include a document from 31 March
    14:00: the range has to cover the last day completely.
    """
    if month == 12:
        following = dt.datetime(year + 1, 1, 1)
    else:
        following = dt.datetime(year, month + 1, 1)
    return following - dt.timedelta(seconds=1)


def _year_period(year: int) -> Period:
    return Period(dt.datetime(year, 1, 1), dt.datetime(year, 12, 31, 23, 59, 59),
                  str(year))


def _month_period(year: int, month: int) -> Period:
    return Period(_month_start(year, month), _month_end(year, month),
                  f"{month_name(month)} {year}")


def _day_period(day: dt.datetime, label: str) -> Period:
    return Period(_day(day), _day(day) + dt.timedelta(days=1) - dt.timedelta(seconds=1),
                  label)


def _weeks_back(nu: dt.datetime, weeks: int) -> Period:
    """The past N whole weeks, counted from today."""
    today = _day(nu)
    start = today - dt.timedelta(days=7 * weeks - 1)
    plural = "weken" if weeks != 1 else "week"
    return Period(start, today + dt.timedelta(days=1) - dt.timedelta(seconds=1),
                  f"afgelopen {weeks} {plural}")


def _days_back(nu: dt.datetime, days: int) -> Period:
    today = _day(nu)
    start = today - dt.timedelta(days=days - 1)
    plural = "dagen" if days != 1 else "dag"
    return Period(start, today + dt.timedelta(days=1) - dt.timedelta(seconds=1),
                  f"afgelopen {days} {plural}")


# Order matters: longer and more specific patterns come first, so that
# "vorige week donderdag" is not read as "vorige week".
_PATTERNS: list[tuple[str, str]] = [
    ("dagdeel", r"\b(?P<deel>vanochtend|vanmorgen|vanmiddag|vanavond|vannacht|gisteravond|net)\b"),
    ("gisteren", r"\bgisteren\b"),
    ("eergisteren", r"\beergisteren\b"),
    ("vandaag", r"\b(vandaag|vandaag de dag)\b"),
    ("morgen", r"\bmorgen\b(?!\s+\d)"),
    ("weekdag", r"\b(afgelopen|vorige|verleden|deze)\s+(?P<dag>"
                + "|".join(_WEEKDAYS) + r")\b"),
    ("weekdag_kaal", r"\b(?:op\s+)?(?P<dag>" + "|".join(_WEEKDAYS) + r")\b"),
    ("week", r"\b(?P<n>een|één|1|twee|2|drie|3|vier|4)\s+weken\s+"
             r"(?:geleden|terug)\b"),
    ("week", r"\b(afgelopen|vorige|verleden|deze)\s+week\b"),
    ("maand_terug", r"\b(?P<n>een|één|1|twee|2|drie|3|vier|4|vijf|5|zes|6)\s+maanden\s+"
                    r"(?:geleden|terug)\b"),
    ("maand_terug", r"\b(afgelopen|vorige|verleden|deze)\s+maand\b"),
    ("jaar_terug", r"\b(?P<n>een|één|1|twee|2|drie|3|vier|5)\s+jaar\s+"
                   r"(?:geleden|terug)\b"),
    ("jaar_terug", r"\b(afgelopen|vorige|verleden|dit|deze)\s+jaar\b"),
    ("dagen_terug", r"\b(?:afgelopen\s+|vorige\s+|laatste\s+)?(?P<n>een|1|twee|2|drie|3|vier|4|"
                    r"vijf|5|zes|6|zeven|7|tien|10|14|30|dertig)\s+dagen\s+"
                    r"(?:geleden|terug)?\b"),
    ("week_kaal", r"\b(?:afgelopen|vorige)\s+week\b"),
    ("maand_met_jaar", r"\b(?P<maand>" + MONTH_NAME + r")\s+(?P<jaar>(?:19|20)\d\d)\b"),
    ("maand_met_dag", r"\b(?P<dag>\d{1,2})(?:e|ste)?\s+(?P<maand>" + MONTH_NAME + r")\b"
                       r"(?:\s+(?P<jaar>(?:19|20)\d\d))?"),
    ("maand_alleen", r"\b(?P<maand>" + MONTH_NAME + r")\b"),
    ("datum_nl", r"\b(?P<dag>\d{1,2})[-/](?P<maand>\d{1,2})[-/](?P<jaar>(?:19|20)\d\d)\b"),
    ("datum_iso", r"\b(?P<jaar>(?:19|20)\d\d)-(?P<maand>\d{1,2})-(?P<dag>\d{1,2})\b"),
    ("jaar_alleen", r"\b(?P<jaar>(?:19|20)\d\d)\b"),
]

_COMPILED = [(name, re.compile(pat)) for name, pat in _PATTERNS]

_WORD_NUMBER = {
    "een": 1, "één": 1, "twee": 2, "drie": 3, "vier": 4, "vijf": 5,
    "zes": 6, "zeven": 7, "acht": 8, "negen": 9, "tien": 10,
    "dertig": 30,
}


def _number(text: str | None, default: int = 1) -> int:
    if not text:
        return default
    text = normalise(text)
    if text.isdigit():
        return int(text)
    return _WORD_NUMBER.get(text, default)


def valid_year(year: int) -> bool:
    """Is this a year a document could come from?"""
    return 1900 <= year <= 2999


def _month_number(name: str) -> int:
    return MONTHS.get(normalise(name), 0)


def _round_month(nu: dt.datetime, month: int, year: int | None) -> Period:
    """Pick the most logical year when the user does not mention one.

    "in maart" almost always means the most recently passed March; but if that
    coming March is very close (within a month), the user almost certainly means
    that upcoming month.
    """
    if year is None:
        year = nu.year
        if month > nu.month:
            year -= 1
        elif month == nu.month and nu.day < 15:
            year = nu.year
        if month == nu.month + 1 and nu.day >= 15:
            year = nu.year + 1
    return _month_period(year, month)


def parse_time(question: str, nu: dt.datetime | None = None) -> FoundTime:
    """Pull the time expression out of a search question.

    Returns the question without the time words, plus the range found.
    Found nothing? Then the question stays unchanged and there is no range.
    """
    nu = nu or dt.datetime.now()
    raw = normalise(question or "")
    if not raw.strip():
        return FoundTime(None, question or "", "")

    candidates: list[tuple[str, tuple[int, int], Period, str]] = []
    for name, pattern in _COMPILED:
        for match in pattern.finditer(raw):
            group = match.group(0)
            # "mei" and "maart" are also ordinary words ("in mei", "de maart-
            # vergadering"): a bare month name only counts when a time word
            # precedes it (in/van/uit/rond/omstreeks/tijdens).
            if name in ("maand_alleen", "jaar_alleen"):
                before = raw[max(0, match.start() - 12):match.start()]
                if not re.search(r"\b(?:in|van|uit|rond|omstreeks|tijdens|gedurende)\s+$", before):
                    continue
            period, precision = _build(name, match, nu)
            if period is not None:
                # First the longest (so most specific) hit, then the one that
                # sits furthest back in the question: that one usually belongs
                # to the subject ("dat mailtje over de vergadering in maart").
                candidates.append((group, match.span(), period, precision))

    if not candidates:
        return FoundTime(None, raw, "")

    _, span, period, precision = min(
        candidates, key=lambda k: (-len(k[0]), k[1][0]))
    return FoundTime(period, _strip(raw, span), raw[span[0]:span[1]].strip(),
                     precision)


def _strip(text: str, span: tuple[int, int]) -> str:
    """Remove the recognised piece (plus a loose preposition before it)."""
    start, end = span
    before = text[:start]
    after = text[end:]
    before = re.sub(r"\b(?:in|op|van|uit|rond|omstreeks|gedurende|tijdens|"
                    r"over|betreffende|aangaande|omtrent)\s+$", "", before)
    return re.sub(r"\s{2,}", " ", (before + " " + after)).strip()


def _build(name: str, match: re.Match, nu: dt.datetime) -> tuple[Period | None, str]:
    """Turn one recognition into a range."""
    g = match.groupdict()

    if name == "dagdeel":
        part = normalise(g.get("deel") or "")
        if part == "gisteravond":
            return _day_period(_day(nu) - dt.timedelta(days=1), "gisteren"), "dag"
        if part == "net":
            return _day_period(_day(nu) - dt.timedelta(days=1), "gisteren"), "dag"
        return _day_period(_day(nu), "vandaag"), "dag"
    if name == "gisteren":
        return _day_period(_day(nu) - dt.timedelta(days=1), "gisteren"), "dag"
    if name == "eergisteren":
        return _day_period(_day(nu) - dt.timedelta(days=2), "eergisteren"), "dag"
    if name == "vandaag":
        return _day_period(_day(nu), "vandaag"), "dag"
    if name == "morgen":
        return _day_period(_day(nu) + dt.timedelta(days=1), "morgen"), "dag"
    if name == "weekdag":
        day = _WEEKDAYS.get(normalise(g.get("dag") or ""), 0)
        start_of_week = _day(nu) - dt.timedelta(days=(nu.weekday() - day) % 7)
        if start_of_week > _day(nu):
            start_of_week -= dt.timedelta(days=7)
        return _day_period(start_of_week, "afgelopen " + (g.get("dag") or "")), "dag"
    if name == "weekdag_kaal":
        day = _WEEKDAYS.get(normalise(g.get("dag") or ""), 0)
        last = _day(nu) - dt.timedelta(days=(nu.weekday() - day) % 7)
        if last > _day(nu):
            last -= dt.timedelta(days=7)
        return _day_period(last, "afgelopen " + (g.get("dag") or "")), "dag"
    if name == "week":
        n = _number(g.get("n"), 1)
        if "weken" in match.group(0):
            return _weeks_back(nu, n), "dag"
        return _weeks_back(nu, 1), "dag"
    if name == "dagen_terug":
        n = _number(g.get("n"), 1)
        # "14 dagen" without "geleden" can also be a term or a due date; it is
        # only really a look back with a time word before it or "geleden"/"terug"
        # after it.
        text = match.group(0)
        if "geleden" not in text and "terug" not in text and not re.match(
                r"\s*(?:afgelopen|vorige|laatste)\b", text):
            return None, ""
        return _days_back(nu, n), "dag"
    if name == "maand_terug":
        n = _number(g.get("n"), 1)
        month = nu.month - n
        year = nu.year
        while month < 1:
            month += 12
            year -= 1
        return _month_period(year, month), "maand"
    if name == "jaar_terug":
        n = _number(g.get("n"), 1)
        return _year_period(nu.year - n), "jaar"
    if name == "maand_met_jaar":
        month = _month_number(g.get("maand") or "")
        year = _number(g.get("jaar"), 0)
        if not month or not valid_year(year):
            return None, ""
        return _month_period(year, month), "maand"
    if name == "maand_met_dag":
        month = _month_number(g.get("maand") or "")
        day = _number(g.get("dag"), 1)
        if not month or not 1 <= day <= 31:
            return None, ""
        year = _number(g.get("jaar"), 0)
        if valid_year(year):
            try:
                return _day_period(dt.datetime(year, month, day), "dag"), "dag"
            except ValueError:
                return _month_period(year, month), "maand"
        year = nu.year
        # A day that falls later in this year than today probably lies in the
        # past of last year; "11 maart" in September is last year.
        if month > nu.month:
            year -= 1
        try:
            return _day_period(dt.datetime(year, month, day), "dag"), "dag"
        except ValueError:
            return _month_period(year, month), "maand"
    if name == "maand_alleen":
        month = _month_number(g.get("maand") or "")
        if not month:
            return None, ""
        return _round_month(nu, month, None), "maand"
    if name == "datum_nl":
        day, month, year = _number(g.get("dag"), 1), _number(g.get("maand"), 1), _number(g.get("jaar"), 0)
        try:
            return _day_period(dt.datetime(year, month, day), "dag"), "dag"
        except ValueError:
            return None, ""
    if name == "datum_iso":
        year, month, day = _number(g.get("jaar"), 0), _number(g.get("maand"), 1), _number(g.get("dag"), 1)
        try:
            return _day_period(dt.datetime(year, month, day), "dag"), "dag"
        except ValueError:
            return None, ""
    if name == "jaar_alleen":
        year = _number(g.get("jaar"), 0)
        if valid_year(year):
            return _year_period(year), "jaar"
    return None, ""


def within(moment: dt.datetime | None, period: Period | None) -> bool:
    """Does this moment fall within the range? Without a range anything goes."""
    if period is None:
        return True
    return period.contains(moment)
