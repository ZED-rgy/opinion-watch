from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from opinion_watch.models import Platform

DEFAULT_BRANDS = ("速探长", "优速卖", "配达人")


@dataclass(frozen=True, slots=True)
class Settings:
    runtime_dir: Path
    database_path: Path
    artifact_dir: Path
    browser_channel: str = "chrome"

    @classmethod
    def from_environment(cls) -> Settings:
        runtime_dir = Path(os.getenv("OPINION_WATCH_RUNTIME_DIR", "runtime")).resolve()
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
