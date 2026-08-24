"""Build commands for CLI workers started by the desktop application."""

from __future__ import annotations

import sys
from pathlib import Path


def cli_command(*arguments: str) -> tuple[str, list[str]]:
    """Return an executable and arguments for the packaged or source app.

    A PyInstaller GUI executable cannot interpret ``-m opinion_watch``. The
    package therefore ships a small console CLI executable beside the GUI
    executable; source checkouts continue to use the Python module entrypoint.
    """
    if getattr(sys, "frozen", False):
        cli_name = "OpinionWatchCli.exe" if sys.platform == "win32" else "OpinionWatchCli"
        executable = Path(sys.executable).with_name(cli_name)
        if not executable.exists():
            raise FileNotFoundError(f"未找到桌面 CLI 子程序：{executable}")
        return str(executable), list(arguments)
    return sys.executable, ["-m", "opinion_watch", *arguments]
