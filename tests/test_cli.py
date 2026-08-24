from opinion_watch.cli import _configure_utf8_output


class ReconfigurableStream:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def reconfigure(self, **options: object) -> None:
        self.options = options


def test_cli_configures_utf8_output_for_collected_unicode(monkeypatch: object) -> None:
    stdout = ReconfigurableStream()
    stderr = ReconfigurableStream()
    monkeypatch.setattr("opinion_watch.cli.sys.stdout", stdout)
    monkeypatch.setattr("opinion_watch.cli.sys.stderr", stderr)

    _configure_utf8_output()

    # line_buffering 是桌面端进度条的前提：stdout 接管道时默认全缓冲，
    # 事件会攒到进程退出才一次性到达。
    expected = {"encoding": "utf-8", "errors": "backslashreplace", "line_buffering": True}
    assert stdout.options == expected
    assert stderr.options == expected
