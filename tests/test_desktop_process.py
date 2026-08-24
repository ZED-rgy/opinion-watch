import sys
from pathlib import Path

from opinion_watch.desktop.process import cli_command


def test_cli_command_uses_python_module_in_source_checkout() -> None:
    program, arguments = cli_command("wecom", "test")

    assert program == sys.executable
    assert arguments == ["-m", "opinion_watch", "wecom", "test"]


def test_cli_command_uses_sibling_executable_when_frozen(monkeypatch, tmp_path: Path) -> None:
    gui = tmp_path / "OpinionWatch.exe"
    cli = tmp_path / "OpinionWatchCli.exe"
    gui.write_bytes(b"gui")
    cli.write_bytes(b"cli")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(gui))

    program, arguments = cli_command("scan", "--mode", "quick")

    assert program == str(cli)
    assert arguments == ["scan", "--mode", "quick"]
