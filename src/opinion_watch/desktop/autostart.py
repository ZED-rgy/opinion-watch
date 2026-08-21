"""Windows 开机自启的注册表读写。"""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from pathlib import Path

AUTOSTART_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def windows_autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REGISTRY_PATH) as key:
            winreg.QueryValueEx(key, "OpinionWatch")
    except (FileNotFoundError, OSError):
        return False
    return True


def set_windows_autostart(enabled: bool, runtime_dir: Path | None = None) -> None:
    if os.name != "nt":
        if enabled:
            raise RuntimeError("开机启动仅支持 Windows。")
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REGISTRY_PATH) as key:
        if enabled:
            launcher = Path(sys.argv[0]).resolve()
            if launcher.suffix.lower() == ".py" or launcher.name.lower().startswith("python"):
                command = f'"{sys.executable}" -m opinion_watch.desktop'
            else:
                command = f'"{launcher}"'
            # 开机启动没有可控的工作目录，默认 runtime 目录相对 CWD 解析，
            # 自启进程会连到另一个空数据库。把当前实际使用的目录固定下来。
            if runtime_dir is not None:
                command += f' --runtime-dir "{runtime_dir.resolve()}"'
            winreg.SetValueEx(key, "OpinionWatch", 0, winreg.REG_SZ, command)
        else:
            with suppress(FileNotFoundError):
                winreg.DeleteValue(key, "OpinionWatch")
