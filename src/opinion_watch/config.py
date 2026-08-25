from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from opinion_watch.models import Platform

DEFAULT_BRANDS = ("速探长", "优速卖", "配达人")
RUNTIME_ENV_VAR = "OPINION_WATCH_RUNTIME_DIR"


def stable_runtime_dir() -> Path:
    """Return the per-user runtime directory shared by source and packaged apps."""
    if sys.platform == "win32":
        root = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return (root / "OpinionWatch" / "runtime").resolve()
    if sys.platform == "darwin":
        return (
            Path.home() / "Library" / "Application Support" / "OpinionWatch" / "runtime"
        ).resolve()
    root = Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return (root / "opinion-watch" / "runtime").resolve()


def legacy_runtime_dirs() -> list[Path]:
    """Find old working-directory or executable-adjacent runtime locations."""
    candidates = [(Path.cwd() / "runtime").resolve()]
    if getattr(sys, "frozen", False):
        candidates.append((Path(sys.executable).resolve().parent / "runtime").resolve())
    return list(dict.fromkeys(candidates))


def resolve_runtime_dir() -> Path:
    configured = os.getenv(RUNTIME_ENV_VAR)
    if configured:
        return Path(configured).resolve()
    stable = stable_runtime_dir()
    if (stable / "opinion-watch.db").is_file():
        return stable
    for legacy in legacy_runtime_dirs():
        if (legacy / "opinion-watch.db").is_file():
            return legacy
    return stable


@dataclass(frozen=True, slots=True)
class Settings:
    runtime_dir: Path
    database_path: Path
    artifact_dir: Path
    browser_channel: str = "chrome"

    @classmethod
    def from_environment(cls) -> Settings:
        runtime_dir = resolve_runtime_dir()
        artifact_dir = Path(os.getenv("OPINION_WATCH_ARTIFACT_DIR", "output/playwright")).resolve()
        return cls(
            runtime_dir=runtime_dir,
            database_path=runtime_dir / "opinion-watch.db",
            artifact_dir=artifact_dir,
            browser_channel=os.getenv("OPINION_WATCH_BROWSER_CHANNEL", "chrome"),
        )

    def profile_dir(self, platform: Platform) -> Path:
        return self.runtime_dir / "browser-profiles" / platform.value

    def account_profile_dir(self, platform: Platform, account_id: int) -> Path:
        """Profile used by both the account login flow and automatic scans."""
        return self.runtime_dir / "browser-profiles" / platform.value / str(account_id)

    def ensure_directories(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "browser-profiles").mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
