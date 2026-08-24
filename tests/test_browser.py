import asyncio

from playwright.async_api import Error

from opinion_watch.browser import BrowserSession, summarize_browser_error


class FakeContext:
    async def close(self) -> None:
        return None


class FakeChromium:
    def __init__(self) -> None:
        self.launches = 0

    async def launch_persistent_context(self, **_kwargs: object) -> FakeContext:
        self.launches += 1
        if self.launches == 1:
            raise Error(
                "BrowserType.launch_persistent_context: Target page, context or browser "
                "has been closed\n<process did exit: exitCode=0, signal=null>"
            )
        return FakeContext()


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeStarter:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.instances: list[FakePlaywright] = []

    async def start(self) -> FakePlaywright:
        instance = FakePlaywright(self.chromium)
        self.instances.append(instance)
        return instance


def test_browser_session_retries_clean_launch_exit(tmp_path, monkeypatch) -> None:
    chromium = FakeChromium()
    starter = FakeStarter(chromium)
    monkeypatch.setattr("opinion_watch.browser.async_playwright", lambda: starter)

    async def no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr("opinion_watch.browser.asyncio.sleep", no_delay)

    async def run() -> None:
        async with BrowserSession(tmp_path / "profile") as session:
            assert session.active_context is not None

    asyncio.run(run())

    assert chromium.launches == 2
    assert len(starter.instances) == 2
    assert all(instance.stopped for instance in starter.instances)


def test_browser_error_summary_hides_playwright_launch_logs() -> None:
    raw = (
        "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n"
        "Browser logs:\n<launching> chrome.exe --many-private-flags\n"
        "<process did exit: exitCode=0, signal=null>"
    )

    summary = summarize_browser_error(raw)

    assert "Chrome 启动后立即退出" in summary
    assert "Browser logs" not in summary
    assert "--many-private-flags" not in summary
