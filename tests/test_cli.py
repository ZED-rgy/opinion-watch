from opinion_watch.cli import _configure_utf8_output


class ReconfigurableStream:
    def __init__(self) -> None:
        self.options: dict[str, str] = {}

    def reconfigure(self, **options: str) -> None:
        self.options = options


def test_cli_configures_utf8_output_for_collected_unicode(monkeypatch: object) -> None:
    stdout = ReconfigurableStream()
    stderr = ReconfigurableStream()
    monkeypatch.setattr("opinion_watch.cli.sys.stdout", stdout)
    monkeypatch.setattr("opinion_watch.cli.sys.stderr", stderr)

    _configure_utf8_output()

    expected = {"encoding": "utf-8", "errors": "backslashreplace"}
    assert stdout.options == expected
    assert stderr.options == expected
