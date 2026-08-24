"""Build the desktop application with PyInstaller.

The application deliberately keeps runtime data outside the executable. Users
still need Google Chrome for the Playwright-based platform collectors.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from PyInstaller.__main__ import run

ROOT = Path(__file__).resolve().parents[1]


def _common_arguments(*, name: str, dist_dir: Path, build_dir: Path) -> list[str]:
    assets_dir = ROOT / "src" / "opinion_watch" / "assets"
    return [
        "--noconfirm",
        "--clean",
        "--name",
        name,
        "--paths",
        str(ROOT / "src"),
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(build_dir),
        "--collect-all",
        "playwright",
        "--add-data",
        f"{assets_dir}{os.pathsep}opinion_watch/assets",
    ]


def _copy_cli_next_to_gui(*, dist_dir: Path, cli_dist_dir: Path) -> None:
    cli_name = "OpinionWatchCli.exe" if os.name == "nt" else "OpinionWatchCli"
    source = cli_dist_dir / cli_name
    if not source.exists():
        raise FileNotFoundError(f"PyInstaller 未生成 CLI 子程序：{source}")

    if sys.platform == "darwin":
        app_binary_dir = dist_dir / "OpinionWatch.app" / "Contents" / "MacOS"
        target = (
            app_binary_dir / cli_name
            if app_binary_dir.exists()
            else dist_dir / "OpinionWatch" / cli_name
        )
    else:
        target = dist_dir / "OpinionWatch" / cli_name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def main() -> None:
    dist_dir = ROOT / "dist"
    build_root = ROOT / "build"
    gui_build_dir = build_root / "pyinstaller-gui"
    cli_build_dir = build_root / "pyinstaller-cli"
    cli_dist_dir = build_root / "cli-dist"

    gui_arguments = _common_arguments(
        name="OpinionWatch", dist_dir=dist_dir, build_dir=gui_build_dir
    )
    gui_arguments.extend(
        [
            "--onedir",
            "--windowed",
            "--collect-all",
            "qtawesome",
            "--collect-submodules",
            "opinion_watch.desktop",
            str(ROOT / "src" / "opinion_watch" / "desktop" / "__main__.py"),
        ]
    )
    run(gui_arguments)

    cli_arguments = _common_arguments(
        name="OpinionWatchCli", dist_dir=cli_dist_dir, build_dir=cli_build_dir
    )
    cli_arguments.extend(
        [
            "--onefile",
            "--console",
            str(ROOT / "src" / "opinion_watch" / "__main__.py"),
        ]
    )
    run(cli_arguments)
    _copy_cli_next_to_gui(dist_dir=dist_dir, cli_dist_dir=cli_dist_dir)


if __name__ == "__main__":
    main()
