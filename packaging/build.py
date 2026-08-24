"""Build the desktop application with PyInstaller.

The application deliberately keeps runtime data outside the executable. Users
still need Google Chrome for the Playwright-based platform collectors.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.__main__ import run

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    dist_dir = ROOT / "dist"
    build_dir = ROOT / "build" / "pyinstaller"
    assets_dir = ROOT / "src" / "opinion_watch" / "assets"

    arguments = [
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "OpinionWatch",
        "--paths",
        str(ROOT / "src"),
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(build_dir),
        "--add-data",
        f"{assets_dir}{os.pathsep}opinion_watch/assets",
        "--collect-all",
        "qtawesome",
        "--collect-all",
        "playwright",
        "--collect-submodules",
        "opinion_watch",
        str(ROOT / "src" / "opinion_watch" / "desktop" / "__main__.py"),
    ]
    run(arguments)


if __name__ == "__main__":
    main()
