"""Read music: artist, title, album and year from the file itself.

VindTerug should also be able to find music back — "dat nummer van Mr. Probz" —
without having to install a package such as mutagen for it. This file reads the
data with the standard library instead:

* **ID3v2** (mp3): the frames TIT2, TPE1, TPE2, TALB, TCON, TDRC/TYER, TRCK and
  COMM. We take the three ways ID3v2 encodes text into account (latin-1,
  utf-16 with BOM, utf-8 with BOM), plus extension headers and
  unsynchronised tags.
* **ID3v1** (the last 128 bytes), if there is no readable ID3v2 tag.
* **FLAC**: the VORBIS_COMMENT blocks.
* If nothing works, the **file name** does the job: "10. Mr. Probz - Gold
  Days (ft Action Bronson)" becomes artist "Mr. Probz" and title "Gold Days".

The reader never raises an exception: a damaged file simply yields fewer
fields, so that indexing carries on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "MUSIC_EXTENSIONS",
    "MusicData",
    "is_music",
    "read_music",
    "title_from_filename",
    "text_of_music",
    "ID3V1_GENRES",
]

MUSIC_EXTENSIONS: set[str] = {
    ".mp3", ".wav", ".flac", ".m4a", ".ogg", ".wma", ".aac", ".aiff", ".aif",
    ".opus",
}

# The ID3v1 genre table. Only needed for the fallback to the last 128 bytes;
# ID3v2 carries the genre as ordinary text.
ID3V1_GENRES: tuple[str, ...] = (
    "Blues", "Classic Rock", "Country", "Dance", "Disco", "Funk", "Grunge",
    "Hip-Hop", "Jazz", "Metal", "New Age", "Oldies", "Other", "Pop", "R&B",
    "Rap", "Reggae", "Rock", "Techno", "Industrial", "Alternative", "Ska",
    "Death Metal", "Pranks", "Soundtrack", "Euro-Techno", "Ambient",
    "Trip-Hop", "Vocal", "Jazz+Funk", "Fusion", "Trance", "Classical",
    "Instrumental", "Acid", "House", "Game", "Sound Clip", "Gospel", "Noise",
    "Alternative Rock", "Bass", "Soul", "Punk", "Space", "Meditative",
    "Instrumental Pop", "Instrumental Rock", "Ethnic", "Gothic", "Darkwave",
    "Techno-Industrial", "Electronic", "Pop-Folk", "Eurodance", "Dream",
    "Southern Rock", "Comedy", "Cult", "Gangsta", "Top 40", "Christian Rap",
    "Pop/Funk", "Jungle", "Native American", "Cabaret", "New Wave",
    "Psychedelic", "Rave", "Showtunes", "Trailer", "Lo-Fi", "Tribal",
    "Acid Punk", "Acid Jazz", "Polka", "Retro", "Musical", "Rock & Roll",
    "Hard Rock", "Folk", "Folk-Rock", "National Folk", "Swing", "Fast Fusion",
    "Bebob", "Latin", "Revival", "Celtic", "Bluegrass", "Avantgarde",
    "Gothic Rock", "Progressive Rock", "Psychedelic Rock", "Symphonic Rock",
    "Slow Rock", "Big Band", "Chorus", "Easy Listening", "Acoustic", "Humour",
    "Speech", "Chanson", "Opera", "Chamber Music", "Sonata", "Symphony",
    "Booty Bass", "Primus", "Porn Groove", "Satire", "Slow Jam", "Club",
    "Tango", "Samba", "Folklore", "Ballad", "Power Ballad", "Rhythmic Soul",
    "Freestyle", "Duet", "Punk Rock", "Drum Solo", "A capella", "Euro-House",
    "Dance Hall", "Goa", "Drum & Bass", "Club-House", "Hardcore", "Terror",
    "Indie", "BritPop", "Negerpunk", "Polsk Punk", "Beat", "Christian Gangsta",
    "Heavy Metal", "Black Metal", "Crossover", "Contemporary Christian",
    "Christian Rock", "Merengue", "Salsa", "Thrash Metal", "Anime", "Jpop",
    "Synthpop",
)

_MAX_VALUE = 400


@dataclass
class MusicData:
    """What could be pulled out of a music file."""

    artist: str = ""
    title: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    year: str = ""
    track: str = ""
    comment: str = ""
    source: str = ""         # id3v2, id3v1, vorbis, bestandsnaam
    extra: dict = field(default_factory=dict)

    @property
    def has_anything(self) -> bool:
        return bool(self.artist or self.title or self.album or self.genre
                    or self.year)

    def fields(self) -> list[str]:
        """The filled fields, in fixed order (without duplicates)."""
        out: list[str] = []
        for value in (self.artist, self.title, self.album, self.album_artist,
                      self.genre, self.year, self.track, self.comment):
            if value and value not in out:
                out.append(value)
        return out


def is_music(path: str | Path) -> bool:
    """Is this file about music?"""
    return Path(path).suffix.lower() in MUSIC_EXTENSIONS


def _clean(value: str) -> str:
    """Take whitespace and control characters out of a tag field."""
    text = (value or "").replace("\x00", " ")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()[:_MAX_VALUE]


# --------------------------------------------------------------------------
# ID3v2
# --------------------------------------------------------------------------

def _id3_text(raw: bytes, encoding: int) -> str:
    """Decode one ID3v2 text field, with the encoding from the first byte.

    0 = latin-1, 1 = utf-16 with BOM, 2 = utf-16 without BOM (big-endian),
    3 = utf-8. Unknown encodings fall back to utf-8 with replacement, so that
    something readable always remains instead of an exception.
    """
    if encoding == 0:
        return _clean(raw.decode("latin-1", "replace"))
    if encoding == 1:
        if raw.startswith(b"\xff\xfe"):
            return _clean(raw[2:].decode("utf-16-le", "replace"))
        if raw.startswith(b"\xfe\xff"):
            return _clean(raw[2:].decode("utf-16-be", "replace"))
        return _clean(raw.decode("utf-16-le", "replace"))
    if encoding == 2:
        return _clean(raw.decode("utf-16-be", "replace"))
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return _clean(raw.decode("utf-8", "replace"))


def _id3_year(raw: bytes, encoding: int) -> str:
    """Pull the year out of TDRC ("2021-05-01") or TYER ("2021")."""
    value = _id3_text(raw, encoding)
    match = re.search(r"(1[89]\d{2}|20\d{2})", value)
    return match.group(1) if match else value


def _desynchronise(raw: bytes) -> bytes:
    """Undo the unsynchronised tag: 0xFF 0x00 -> 0xFF.

    ID3v2.2/2.3 can store a tag unsynchronised to avoid a frame end ending up in
    the data by accident; every 0xFF is then followed by a 0x00. Without this
    step we read frames in the wrong place.
    """
    out = bytearray()
    i = 0
    while i < len(raw):
        out.append(raw[i])
        if raw[i] == 0xFF and i + 1 < len(raw) and raw[i + 1] == 0x00:
            i += 2
            continue
        i += 1
    return bytes(out)


def _synchsafe(raw: bytes) -> int:
    """Convert a synchsafe number (7 bits per byte)."""
    value = 0
    for byte in raw[:4]:
        value = (value << 7) | (byte & 0x7F)
    return value


def _id3v2_frames(data: bytes) -> tuple[dict[str, str], dict[str, str]]:
    """Read the frames of an ID3v2 tag; returns (text fields, extra info)."""
    fields: dict[str, str] = {}
    extra: dict[str, str] = {}
    if data[:3] != b"ID3" or len(data) < 10:
        return fields, extra

    version = data[3]
    flags = data[5]
    size = _synchsafe(data[6:10])
    body = data[10:10 + size] if size else data[10:]
    if flags & 0x80:                         # whole tag unsynchronised
        body = _desynchronise(body)

    # ID3v2.2 uses 3-letter frame names and 3-byte sizes; 2.3 and 2.4 use
    # 4-letter­names and 4-byte sizes. We read both.
    i = 0
    while i + 6 <= len(body):
        if version >= 3:
            name_raw = body[i:i + 4]
            if name_raw[:1] == b"\x00":
                break
            size_raw = body[i + 4:i + 8]
            if version >= 4:
                size = _synchsafe(size_raw)
            else:
                size = int.from_bytes(size_raw, "big")
            head = i + 10
            frame_flags = body[i + 8:i + 10]
        else:
            name_raw = body[i:i + 3]
            if name_raw[:1] == b"\x00":
                break
            size = int.from_bytes(body[i + 3:i + 6], "big")
            head = i + 6
            frame_flags = b""
        try:
            name = name_raw.decode("ascii")
        except UnicodeDecodeError:
            break
        if size <= 0 or i + (head - i) + size > len(body):
            break
        content = body[head:head + size]
        i = head + size

        # Unsynchronisation per frame (ID3v2.4).
        if version >= 4 and frame_flags and frame_flags[0] & 0x02:
            content = _desynchronise(content)
        # We skip compression and encryption: we cannot read those.
        if version >= 3 and frame_flags:
            flag2 = frame_flags[1] if len(frame_flags) > 1 else 0
            if flag2 & 0x0C:
                continue
        if not content:
            continue

        if name in ("TIT2", "TT2", "TPE1", "TP1", "TPE2", "TP2", "TALB",
                    "TAL", "TCON", "TCO", "TRCK", "TRK", "TDRC", "TYER",
                    "TYE", "TENC", "TSSE"):
            encoding = content[0]
            value = _id3_text(content[1:], encoding)
            key = {
                "TT2": "TIT2", "TP1": "TPE1", "TP2": "TPE2", "TAL": "TALB",
                "TCO": "TCON", "TRK": "TRCK", "TYE": "TYER",
            }.get(name, name)
            if value and key not in fields:
                fields[key] = value
        elif name in ("COMM", "COM") and "COMM" not in fields:
            # COMM: encoding, language (3 bytes), short description, then the text.
            encoding = content[0]
            rest = content[4:]
            separator = _text_end(rest, encoding)
            text = rest[separator:]
            value = _id3_text(text, encoding)
            if value:
                fields["COMM"] = value
        elif name in ("TRCK", "TRK") and "TRCK" not in fields:
            pass

    extra["id3_versie"] = f"2.{version}"
    return fields, extra


def _text_end(raw: bytes, encoding: int) -> int:
    """Where does a short description end? (null bytes, one or two)."""
    if encoding in (1, 2):
        for i in range(0, len(raw) - 1, 2):
            if raw[i] == 0 and raw[i + 1] == 0:
                return i + 2
        return 0
    for i, byte in enumerate(raw):
        if byte == 0:
            return i + 1
    return 0


def _id3v1(data: bytes) -> dict[str, str]:
    """Read the last 128 bytes (TAG...)."""
    if len(data) < 128 or data[-128:-125] != b"TAG":
        return {}
    tail = data[-125:]

    def field(start: int, length: int) -> str:
        return _clean(tail[start:start + length].decode("latin-1", "replace"))

    genre_byte = tail[122]
    genre = (ID3V1_GENRES[genre_byte]
             if genre_byte < len(ID3V1_GENRES) else "")
    return {
        "TIT2": field(0, 30),
        "TPE1": field(30, 30),
        "TALB": field(60, 30),
        "TYER": field(90, 4),
        "COMM": field(94, 28),
        "TCON": genre,
        "TRCK": "",
    }


def _vorbis_comment(data: bytes) -> dict[str, str]:
    """Read the VORBIS_COMMENT blocks of a FLAC file."""
    out: dict[str, str] = {}
    counter: dict[str, str] = {}
    if not data.startswith(b"fLaC"):
        return out
    i = 4
    while i + 4 <= len(data):
        block_head = data[i]
        last = block_head & 0x80
        kind = block_head & 0x7F
        length = int.from_bytes(data[i + 1:i + 4], "big")
        block = data[i + 4:i + 4 + length]
        if kind == 4:
            _read_vorbis_block(block, out, counter)
        i += 4 + length
        if last:
            break
    return out


def _read_vorbis_block(block: bytes, out: dict[str, str],
                       counter: dict[str, str]) -> None:
    """Read the key=value lines of one VORBIS_COMMENT block."""
    if len(block) < 8:
        return
    count = int.from_bytes(block[4:8], "little")
    pointer = 8
    for _ in range(count):
        if pointer + 4 > len(block):
            return
        length = int.from_bytes(block[pointer:pointer + 4], "little")
        pointer += 4
        line = block[pointer:pointer + length]
        pointer += length
        if b"=" not in line:
            continue
        key, _, value = line.partition(b"=")
        name = key.decode("ascii", "replace").upper()
        content = _clean(value.decode("utf-8", "replace"))
        if not content:
            continue
        if name in ("ARTIST", "TITLE", "ALBUM", "ALBUMARTIST", "GENRE",
                    "DATE", "YEAR", "TRACKNUMBER", "COMMENT"):
            previous = counter.get(name)
            if previous:
                # Several artists belong together, not hidden in one field.
                out[previous] = f"{out[previous]} & {content}"
            else:
                keys = {"ARTIST": "kunstenaar", "TITLE": "titel",
                        "ALBUM": "album", "ALBUMARTIST": "albumartiest",
                        "GENRE": "genre", "DATE": "datum",
                        "YEAR": "jaar", "TRACKNUMBER": "track",
                        "COMMENT": "opmerking"}
                key_name = f"vorbis_{keys[name]}"
                out[key_name] = content
                counter[name] = key_name


# --------------------------------------------------------------------------
# File name as a last resort
# --------------------------------------------------------------------------

_LEADING_NUMBER = re.compile(r"^\s*\d{1,3}\s*[-.)\]]\s*")


def title_from_filename(filename: str) -> tuple[str, str]:
    """Pull (artist, title) out of a file name.

    "10. Mr. Probz - Gold Days (ft Action Bronson)" becomes ("Mr. Probz",
    "Gold Days"). Without a dash the whole name is the title.
    """
    name = Path(filename).stem
    name = _LEADING_NUMBER.sub("", name)
    name = name.replace("_", " ").strip()
    name = re.sub(r"\s{2,}", " ", name)

    artist = ""
    title = name
    for dash in (" - ", " – ", " — ", "-"):
        if dash in name:
            left, _, right = name.partition(dash)
            left, right = left.strip(), right.strip()
            if left and right:
                artist, title = left, right
            break

    title = re.sub(r"\s*[\(\[]\s*(ft|feat|featuring)\.?[^)\]]*[\)\]]",
                   "", title, flags=re.IGNORECASE).strip()
    title = re.sub(r"\s*[\(\[]\s*(official|lyrics?|audio|video|hd|hq|mv|"
                   r"remaster(ed)?)[^)\]]*[\)\]]", "", title,
                   flags=re.IGNORECASE).strip()
    return artist, title or name


# --------------------------------------------------------------------------
# What the outside world uses
# --------------------------------------------------------------------------

def read_music(path: str | Path) -> MusicData:
    """Read the data of one music file. Never fails hard."""
    p = Path(path)
    out = MusicData()
    head = b""
    try:
        with open(p, "rb") as handle:
            head = handle.read(1024 * 1024)
    except OSError:
        head = b""

    ext = p.suffix.lower()
    if head[:3] == b"ID3":
        fields, extra = _id3v2_frames(head)
        if fields:
            out.artist = fields.get("TPE1", "")
            out.title = fields.get("TIT2", "")
            out.album = fields.get("TALB", "")
            out.album_artist = fields.get("TPE2", "")
            out.genre = fields.get("TCON", "")
            out.year = _short_year(fields.get("TDRC") or fields.get("TYER", ""))
            out.track = fields.get("TRCK", "")
            out.comment = fields.get("COMM", "")
            out.source = "id3v2"
            out.extra.update(extra)
    if not out.has_anything and ext == ".flac":
        fields = _vorbis_comment(head)
        if fields:
            out.artist = fields.get("vorbis_kunstenaar", "")
            out.title = fields.get("vorbis_titel", "")
            out.album = fields.get("vorbis_album", "")
            out.album_artist = fields.get("vorbis_albumartiest", "")
            out.genre = fields.get("vorbis_genre", "")
            out.year = _short_year(fields.get("vorbis_jaar")
                                   or fields.get("vorbis_datum", ""))
            out.track = fields.get("vorbis_track", "")
            out.comment = fields.get("vorbis_opmerking", "")
            out.source = "vorbis"
    if not out.has_anything:
        try:
            with open(p, "rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 128))
                tail = handle.read(128)
        except OSError:
            tail = b""
        fields = _id3v1(tail)
        if fields:
            out.artist = fields.get("TPE1", "")
            out.title = fields.get("TIT2", "")
            out.album = fields.get("TALB", "")
            out.genre = fields.get("TCON", "")
            out.year = _short_year(fields.get("TYER", ""))
            out.comment = fields.get("COMM", "")
            out.source = "id3v1"

    # Whatever is still missing, we fill in from the file name: a track
    # without tags should still be findable on its name.
    name_artist, name_title = title_from_filename(p.name)
    if not out.title:
        out.title = name_title
    if not out.artist:
        out.artist = name_artist
    if not out.source:
        out.source = "bestandsnaam"
    return out


def _short_year(value: str) -> str:
    """Pull only the year out of a date such as "2021-05-01"."""
    match = re.search(r"(1[89]\d{2}|20\d{2})", value or "")
    return match.group(1) if match else (value or "")[:10]


def text_of_music(path: str | Path, data: MusicData | None = None) -> str:
    """The searchable text of a music file.

    Besides the tag fields, the file name goes in as well (so that searching
    for "Gold Days" finds the file, even if it holds no title) and the folder it
    sits in (people organise music into folders with a meaningful name).
    """
    p = Path(path)
    data = data or read_music(p)
    # The full file name (with extension) and the name without extension both
    # go in: that way both "Gold Days" and "nummer.mp3" find the file back.
    parts = [p.name, p.stem]
    try:
        if p.parent and p.parent.name:
            parts.append(p.parent.name)
    except OSError:
        pass
    parts.extend(data.fields())
    return "\n".join(part for part in parts if part)
