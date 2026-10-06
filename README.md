# VindTerug

**Find it back.** A local search tool for Windows that remembers things by what
they *mean*, not by what they are called.

You type something like *"that mail about the tender in March"* and VindTerug
understands three things at once: you are looking for **mail**, it is about a
**tender**, and it should be from **March**. Dates, kinds and synonyms are taken
out of the question; the rest is used as search text, with spell checking.

Everything stays on your own computer. There is no account, no cloud, nothing is
uploaded, and no internet connection is needed.

---

## Features

* **Documents** — plain text, Markdown, CSV, JSON, HTML, source code, Word,
  Excel, PowerPoint and PDF (including the awkward ones whose text is hidden in
  embedded font streams).
* **Mail** — `.eml` and `.mbox`, read from the file, not from a mail client.
* **Screenshots and photos** — through OCR, but only if you tick that box,
  because reading one image costs about a second.
* **Music** — artist, title, album, genre and year from the file's tags, or from
  the file name when there are no tags.
* **Browser history** — Chrome, Edge and Firefox: address, title and time of the
  visit. Locked databases are read from a temporary copy.
* **The whole computer** — one button adds every fixed drive. System folders,
  caches and build rubbish are skipped before they are even opened.

## Running it

Two ways, both without installing anything:

```
python -m vindterug
```

or double-click `vindterug.cmd`, or build a single `.exe` with `build_exe.bat`
(see below). Python 3.10 or newer; only the standard library is used.

Other command-line options:

```
python -m vindterug --demo       show on the console what the program does
python -m vindterug --selftest   build the window, pump one round, close
python -m vindterug --version    print the version
```

## Building the index

1. Choose your sources:
   * **Add my default folders** — Desktop, Documents, Downloads, Music,
     Pictures and Videos that exist, plus the browser profiles it finds.
   * **Add whole computer** — every fixed drive. See below for what that means.
   * **Add folder…** — pick your own. You can add several.
2. Tick what should be indexed: documents, mail, screenshots and/or browser
   history.
3. Press **Update index** (or **F5**). Indexing runs in the background; the
   window stays usable and shows progress. **Cancel** stops it neatly.
4. Type your question in the search box and press **Enter**.

The index lives in `%LOCALAPPDATA%\VindTerug\index.db`. The full text of every
indexed document is kept next to it in `%LOCALAPPDATA%\VindTerug\tekst\`, so the
detail panel can show it.

**Updating is cheap.** On a later round a file is skipped when its size *and*
its change time are the same. Only new or changed files are read again, and
files that disappeared are removed from the index.

**Interim saving and resuming.** Every 200 documents (or every 5 seconds) the
state really goes to disk, and the folders that still had to be done stay in a
queue. Stopping, closing the program or a power cut therefore never costs the
work of a whole disk: the next round continues where it stopped instead of
starting over. The status line says "continue with the previous round: N items
to go".

**Careful:** if the index itself sits in a folder you index, VindTerug skips its
own files (the database and the stored texts). Otherwise the index would start
indexing itself.

## Indexing the whole computer — and how fast that is

**Add whole computer** puts every fixed drive in the list (usually `C:\`). It
asks for confirmation first, showing the drives and warning that the first round
takes a while.

What stays out: `Windows`, `Program Files`, `ProgramData`, `$Recycle.Bin`,
`System Volume Information`, `PerfLogs`, `Windows.old`, the reparse points
`Documents and Settings` and `All Users`, `AppData`, all caches, `node_modules`,
`.git`, `__pycache__`, virtual environments and temporary folders. A folder on
that list is **not even opened** — that is the biggest speed-up in the program.
Files above 25 MB are skipped too: a film contains no useful text and would make
a round hours longer.

Photos and screenshots are only read when you tick *Screenshots (via OCR)*. That
follows from the measurement below: reading one image costs over 800 ms, against
less than a millisecond for a text file. In a trial round over `C:\`, 40 images
took 32.6 of the 32.8 seconds.

Huge spreadsheets, Word files and text files are only read at the front: more
text than fits in the index cannot be stored anyway. That cut reading on a trial
of 1 500 files from 60 to 24 seconds.

### What was measured

`benchmark_indexering.py` (standard library only, no network) compares the ways
of walking and reading on real folders of this machine. Files per second, higher
is better:

| tree | `rglob` (the old way) | `os.walk` | **`os.scandir` + pruning** |
|---|---|---|---|
| `C:\Windows\System32\drivers` | 2 600 | 16 800 | **22 300** |
| `…\AppData\Local\Programs\Python` | 1 400 | 6 700 | **9 900** |
| `C:\Windows\System32` | 2 200 | 18 000 | **17 000** |
| `C:\Users\Anouk` | not measurable — far too slow | 4 300 | **11 900** |

The old way collected **all** paths and sorted them before anything happened; on
a home folder with many files there was no measurement left to make. The new way
walks with `os.scandir`, skips folders before opening them, and starts indexing
straight away. Size and time of a file come from that same directory read, so no
extra call per file is needed (measured: no difference).

Also measured:

* **Tracking what is already indexed:** looking up 5 000 files in the database
  took 0.41 s, in memory 0.046 s — **9 times faster**. So each round reads the
  known state in one go.
* **Reading files:** one at a time 811 per second, with eight helper threads
  1 697 per second — **more than twice as fast**. Reading therefore happens in a
  small group of threads while writing to the index stays in one thread. Never
  more than a handful of files are in memory, and the order is preserved.
* **Writing to the index:** 149 documents per second without batching against
  138 with batching — batching is **not** faster. It is there for something else:
  without batching a long round only reaches the disk at the end, and a crash
  loses everything. With batching you lose seconds of work at most and you can
  resume. WAL (which lets searching continue during indexing) is there for the
  same reason.
* **On a real disk:** walking and finding 1 500 usable files took 0.3 s (5 379
  per second); reading the text out of them 24.3 s (62 per second); writing them
  into the index 10.8 s (139 per second). A trial round of 2 000 documents from
  `C:\` finished in 84 seconds. The slowest files are PDFs and spreadsheets: 0.7
  to 1.7 seconds each, while the other 1 492 files together took under 16
  seconds. Measured separately, walking speeds up by a factor of 3 to 7 and the
  lookup of known files by a factor of 9; those gains are in that number too, but
  it is not a measured comparison against the older version as a whole.

## What is and is not indexed

**Indexed** (from the folders you added):

* text files: `.txt .md .csv .log .json .html .py` and so on
* Word, Excel and PowerPoint (`.docx .xlsx .pptx`)
* PDFs with a text layer; for scans VindTerug tries OCR on the embedded JPEGs
* music (`.mp3 .wav .flac .m4a .ogg .wma .aac .aiff .opus`): artist, title,
  album, genre and year from the tag, or from the file name
* mail: `.eml` and `.mbox` (subject, sender, date, body)
* screenshots and photos — only the text in them, through OCR, and only when you
  tick that box
* browser history: address, title and time of your visits

**Not indexed:**

* system folders and hidden rubbish (Windows, AppData, cache, node_modules,
  build, and so on)
* file types that are not in the list above
* PDFs without a text layer (a scan without OCR)
* the contents of mail attachments

What is stored: per document the title, path, date and text, in
`%LOCALAPPDATA%\VindTerug\index.db`, plus the full text of each document in the
`tekst` folder beside it.

## How a search question is read

VindTerug does not need a language model. It splits your question into four
parts and weighs them:

* **kind** — "mail", "screenshot", "pdf", "website" (in Dutch and English words)
* **time** — "in March", "last week", "yesterday", "since 2024", "2 months ago",
  "11 March 2026"
* **subject** — the words that are left, with synonyms (tender ↔ offerte ↔
  aanbesteding), stemming and spelling correction
* **where** — a folder name or a website in the question narrows the list down

Results are ranked by where the words were found (title weighs heavier than
body), how well the date matches and how recent the document is. Every result
says *why* it is there.

If a local [Ollama](https://ollama.com) is running you can switch on an extra
similarity signal with the environment variable `VINDTERUG_OLLAMA`
(for example `http://127.0.0.1:11434`). It is off by default and nothing is ever
sent anywhere without that setting.

## Language

**The interface, the documentation and the code are English. The search
understands Dutch.** Dutch stopwords, Dutch synonyms, Dutch month names and Dutch
time expressions ("maart", "vorige week", "gisteren") are part of how the search
works, and they are kept on purpose: they are data, not text to translate. An
English question still finds English text (the words are what you typed), but the
smart date reading and the synonym table are Dutch for now. Adding an English
word list is a logical next step.

## Limitations, honestly

* **No Outlook archives.** `.pst` and `.msg` are not read, nor are the native
  databases of Outlook or Thunderbird. Export what you want to search to `.eml`
  or `.mbox`.
* **PDFs without a text layer.** A scan or an image-only PDF yields no text.
  VindTerug says so honestly instead of pretending the document is empty. Such
  PDFs are not found by their content.
* **OCR is limited.** Screenshots are only read when you tick that box, and the
  quality depends on the method. By default VindTerug tries the text recognition
  built into Windows (slow, but always present) and uses Tesseract when it is
  installed. Small letters, poor contrast and handwriting yield little.
* **Locked browser profiles.** Chrome, Edge and Firefox keep their history file
  open while running. VindTerug makes a temporary copy; if that fails (rights, an
  unknown layout), those visits are skipped. New visits only appear in the next
  round.
* **A whole-disk round costs time.** The first round takes half an hour to a few
  hours, depending on what is on the disk. Later rounds are fast: unchanged files
  are skipped (measured: 151× faster on an unchanged folder). You can always stop
  a round; what is finished stays and the next round continues where it stopped.
* **One user, one index.** The index belongs to this Windows user. No syncing
  between machines, no shared index.
* **Windows only.** The interface and opening files target Windows
  (`%LOCALAPPDATA%`, Explorer, the OCR of Windows). The core — reading, indexing
  and searching — is plain Python, but the GUI has not been tested on macOS or
  Linux.

## Tests

```
python -m unittest discover -s tests
```

The tests run without a screen and without a network. They create their own
sample files (including a minimal `.docx`, a PDF with an uncompressed text
stream, an `.eml` and an `.mbox`) and check the reading, the reading of search
questions, building and searching the index, filtering by date and ranking the
results. They also cover the whole-computer round: finding the fixed drives, the
skip list for system folders (and that ordinary folders such as *Documents* and
*Downloads* are **not** on it), pruning while walking, the files of the index
itself staying out, and interim saving and resuming after a stop.

Some of the test data is Dutch on purpose, because the program is tested against
the Dutch language handling described above.

**Status of this release:** the test suite is the last file still being
translated from Dutch to English. It is not part of this first commit; it is
added in the next one. Everything else here — the program, the documentation and
the scripts — is English.

## Building the .exe (optional)

`build_exe.bat` builds a single executable `dist\VindTerug.exe` with PyInstaller.
PyInstaller is not part of the standard library; the script installs it when it
is missing. This is a convenience only: the program also runs without it with
`python -m vindterug`.

The built `.exe` is **not signed**. Windows may therefore warn the first time you
run it, and some security software blocks unsigned executables. Use
`vindterug.cmd` instead if that happens, or sign the executable with a
certificate of your own.

## Layout

```
vindterug/                 the program
  core.py                  index, indexing round, search
  sources.py               getting text out of files, drives, browser history
  gui.py, theme.py         the window
  tekst.py, tijd.py, query.py   language: stemming, dates, reading the question
  muziek.py, ocr.py        music tags, text recognition
tests/test_core.py         the test suite
benchmark_indexering.py    the measuring tool behind the speed claims
run_vindterug.py           entry point
vindterug.cmd              start script for Windows
build_exe.bat              builds dist\VindTerug.exe
```
