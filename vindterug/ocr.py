"""OCR for screenshots and photos — optional and pluggable.

VindTerug must also be able to search screenshots, but refuses to demand an
external dependency for that: the program must work on any Windows PC with
only Python. That is why OCR here is a layer with three modes:

* **Off** — nothing is read; the app keeps working fully.
* **Windows** — via ``Windows.Media.Ocr`` (the OCR built into Windows 10 itself),
  called with PowerShell. This works on any modern Windows PC, without
  installation. It is slow though: allow for a second per image.
* **Tesseract** — if ``tesseract.exe`` is on the path, that one is used;
  it is more accurate, but you have to install it yourself.

If OCR fails, a readable message comes back and the app carries on.
An exception is never passed on to the caller.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "OcrResult",
    "STATUS_OFF",
    "STATUS_WINDOWS",
    "STATUS_TESSERACT",
    "STATUS_UNAVAILABLE",
    "detect",
    "ocr_image",
    "ocr_status_text",
]

STATUS_OFF = "uit"
STATUS_WINDOWS = "windows"
STATUS_TESSERACT = "tesseract"
STATUS_UNAVAILABLE = "niet beschikbaar"

# Upper bound: OCR is slow and a huge file still yields no usable
# text within a reasonable waiting time.
MAX_FILE_SIZE = 24 * 1024 * 1024
OCR_TIMEOUT = 90

# Windows PowerShell script that uses the built-in OCR engine. Everything sits
# in one script file so no awkward quoting through the command line is
# needed (that goes wrong on Windows with spaces and quotation marks, guaranteed).
_WINDOWS_SCRIPT = r"""
param([string]$Pad)
$ErrorActionPreference = 'Stop'
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
    [Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null

    $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

    function Wacht($bewerking, $type) {
        $taak = $asTask.MakeGenericMethod($type).Invoke($null, @($bewerking))
        $taak.Wait()
        return $taak.Result
    }

    $bestand = Wacht ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Pad)) ([Windows.Storage.StorageFile])
    $stroom = Wacht ($bestand.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Wacht ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stroom)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Wacht ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
        (New-Object Windows.Globalization.Language 'en-US')) }
    if ($null -eq $engine) { Write-Output ''; exit 0 }

    $resultaat = Wacht ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    foreach ($regel in $resultaat.Lines) { Write-Output $regel.Text }
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
"""

_TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


@dataclass
class OcrResult:
    """What OCR produced (or why it produced nothing)."""

    text: str = ""
    method: str = STATUS_OFF
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text.strip()) and not self.error

    def __bool__(self) -> bool:
        return self.ok


def _windows_ocr_available() -> bool:
    """Are we running on Windows with PowerShell? Then the built-in OCR works."""
    if os.name != "nt":
        return False
    return shutil.which("powershell") is not None


def _tesseract_path() -> str:
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in _TESSERACT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def detect(preference: str = "auto") -> str:
    """Work out which OCR method can be used on this PC."""
    if preference == STATUS_OFF:
        return STATUS_OFF
    if preference == STATUS_TESSERACT:
        return STATUS_TESSERACT if _tesseract_path() else STATUS_UNAVAILABLE
    if preference == STATUS_WINDOWS:
        return STATUS_WINDOWS if _windows_ocr_available() else STATUS_UNAVAILABLE
    # auto
    if _tesseract_path():
        return STATUS_TESSERACT
    if _windows_ocr_available():
        return STATUS_WINDOWS
    return STATUS_UNAVAILABLE


def ocr_status_text(method: str) -> str:
    """Short explanation of the OCR mode, for in the interface."""
    return {
        STATUS_OFF: "OCR is off — images are not read.",
        STATUS_WINDOWS: "OCR through the text recognition built into Windows "
                        "(slow, but always there).",
        STATUS_TESSERACT: "OCR through Tesseract (more accurate).",
        STATUS_UNAVAILABLE: "OCR is not available — images are only found by "
                            "their file name.",
    }.get(method, "OCR status unknown.")


def _ps_quote(path: str) -> str:
    """Put a path safely between single quotation marks for PowerShell."""
    return "'" + str(path).replace("'", "''") + "'"


def _clean_ps_error(raw: str) -> str:
    """Pull the essence out of a PowerShell error message."""
    text = re.sub(r"\s+", " ", (raw or "").strip())
    lines = [r.strip() for r in text.splitlines() if r.strip()]
    core = lines[-1] if lines else text
    return core[:300]


def _ocr_via_tesseract(path: Path) -> OcrResult:
    exe = _tesseract_path()
    if not exe:
        return OcrResult("", STATUS_UNAVAILABLE,
                           "Tesseract was not found on this PC.")
    base = Path(tempfile.mkdtemp(prefix="vindterug-ocr-"))
    output = base / "out"
    try:
        run = subprocess.run(
            [exe, str(path), str(output), "-l", "nld+eng", "--psm", "3"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=OCR_TIMEOUT, creationflags=_no_window(),
        )
        if run.returncode != 0:
            return OcrResult("", STATUS_TESSERACT,
                               f"Tesseract reported an error: {_clean_ps_error(run.stderr)}")
        file = output.with_suffix(".txt")
        if not file.exists():
            return OcrResult("", STATUS_TESSERACT, "Tesseract produced no text file.")
        text = file.read_text(encoding="utf-8", errors="ignore")
        return OcrResult(_clean(text), STATUS_TESSERACT, "")
    except subprocess.TimeoutExpired:
        return OcrResult("", STATUS_TESSERACT,
                           f"Tesseract took longer than {OCR_TIMEOUT} seconds.")
    except OSError as exc:
        return OcrResult("", STATUS_TESSERACT, f"Tesseract could not start: {exc}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _ocr_via_windows(path: Path) -> OcrResult:
    if not _windows_ocr_available():
        return OcrResult("", STATUS_UNAVAILABLE,
                           "The text recognition of Windows is not available here.")
    workdir = Path(tempfile.mkdtemp(prefix="vindterug-ocr-"))
    script = workdir / "ocr.ps1"
    try:
        script.write_text(_WINDOWS_SCRIPT, encoding="utf-8")
        run = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-Pad", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=OCR_TIMEOUT, creationflags=_no_window(),
        )
        if run.returncode != 0:
            return OcrResult("", STATUS_WINDOWS,
                               "Windows text recognition failed: "
                               + _clean_ps_error(run.stderr or run.stdout))
        return OcrResult(_clean(run.stdout or ""), STATUS_WINDOWS, "")
    except subprocess.TimeoutExpired:
        return OcrResult("", STATUS_WINDOWS,
                           f"The text recognition took longer than {OCR_TIMEOUT} seconds.")
    except OSError as exc:
        return OcrResult("", STATUS_WINDOWS, f"PowerShell could not start: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _no_window() -> int:
    """Stop a console window flashing up during indexing."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _clean(text: str) -> str:
    """Take empty lines and double spaces out."""
    lines = [re.sub(r"[ \t]+", " ", r).strip() for r in (text or "").splitlines()]
    return "\n".join(r for r in lines if r)


def ocr_image(path: str | Path, *, method: str = "auto") -> OcrResult:
    """Read the text of an image. Never fails hard.

    Gives an :class:`OcrResult` with the text, the method used and — if
    it did not work — a readable reason.
    """
    p = Path(path)
    if not p.exists():
        return OcrResult("", STATUS_OFF, "This file is gone.")
    try:
        if p.stat().st_size > MAX_FILE_SIZE:
            return OcrResult("", STATUS_OFF, "This image is too large to read.")
    except OSError as exc:
        return OcrResult("", STATUS_OFF, f"Could not open the image: {exc}")

    chosen = detect(method)
    if chosen == STATUS_OFF:
        return OcrResult("", STATUS_OFF, "")
    if chosen == STATUS_UNAVAILABLE:
        return OcrResult("", STATUS_UNAVAILABLE, ocr_status_text(STATUS_UNAVAILABLE))
    if chosen == STATUS_TESSERACT:
        result = _ocr_via_tesseract(p)
        if result.text or result.error:
            return result
        # Tesseract gave nothing: try Windows too if it is there.
        if _windows_ocr_available():
            return _ocr_via_windows(p)
        return result
    return _ocr_via_windows(p)
