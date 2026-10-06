"""Entry point for local use and for building the .exe.

    python run_vindterug.py

This thin layer exists because PyInstaller wants a loose script as its entry
point, while the program itself is neatly put together as a package.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vindterug.gui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
