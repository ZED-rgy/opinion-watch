import asyncio
import json
from pathlib import Path

from opinion_watch.credentials import CredentialStore
from opinion_watch.models import CollectedContent, Platform
from opinion_watch.report import build_daily_report
from opinion_watch.storage import Storage
from opinion_watch.wecom import WeComClient, send_daily_report_if_due


def make_storage(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    return storage


def test_daily_report_contains_scan_summary(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.add_brand("速探长")
    storage.upsert_contents(
        [
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="report-content",
                url="https://www.douyin.com/video/report-content",
                title="速探长物流投诉",
                source_keyword="速探长",
            )
        ]
    )
    report = build_daily_report(storage)

    assert "品牌舆情巡检日报" in report
    assert "当日去重内容：1 条" in report
    assert "聚合事件：0 个" in report


def test_wecom_client_authenticates_and_sends_markdown() -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.responses: list[str] = []

        async def send(self, value: str) -> None:
            frame = json.loads(value)
            self.sent.append(frame)
            self.responses.append(
                json.dumps({"headers": frame["headers"], "errcode": 0, "errmsg": "ok"})
            )

        async def recv(self) -> str:
            return self.responses.pop(0)

    class FakeConnection:
        def __init__(self, websocket: FakeWebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, *_args: object) -> None:
            return None

    websocket = FakeWebSocket()

    def fake_connect(*_args: object, **_kwargs: object) -> FakeConnection:
        return FakeConnection(websocket)

    import opinion_watch.wecom as wecom

    original_connect = wecom.websockets.connect
    wecom.websockets.connect = fake_connect  # type: ignore[assignment]
    try:
        asyncio.run(WeComClient(bot_id="bot", secret="secret").send_markdown("chat", "## 测试日报"))
    finally:
        wecom.websockets.connect = original_connect  # type: ignore[assignment]

    assert websocket.sent[0]["cmd"] == "aibot_subscribe"
    assert websocket.sent[0]["body"] == {"bot_id": "bot", "secret": "secret"}
    assert websocket.sent[1]["cmd"] == "aibot_send_msg"
    assert websocket.sent[1]["body"]["chatid"] == "chat"  # type: ignore[index]


def test_wecom_client_discovers_group_chat_id() -> None:
    class DiscoveryWebSocket:
        def __init__(self) -> None:
            self.responses: list[str] = []

        async def send(self, value: str) -> None:
            frame = json.loads(value)
            self.responses.append(
                json.dumps({"headers": frame["headers"], "errcode": 0, "errmsg": "ok"})
            )
            if frame["cmd"] == "aibot_subscribe":
                self.responses.append(
                    json.dumps(
                        {
                            "cmd": "aibot_msg_callback",
                            "headers": {"req_id": "callback"},
                            "body": {"chattype": "group", "chatid": "wr_group_123"},
                        }
                    )
                )

        async def recv(self) -> str:
            return self.responses.pop(0)

    class FakeConnection:
        def __init__(self, websocket: DiscoveryWebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self) -> DiscoveryWebSocket:
            return self.websocket

        async def __aexit__(self, *_args: object) -> None:
            return None

    import opinion_watch.wecom as wecom

    websocket = DiscoveryWebSocket()
    original_connect = wecom.websockets.connect
    wecom.websockets.connect = lambda *_args, **_kwargs: FakeConnection(websocket)  # type: ignore[assignment]
    try:
        chat_id = asyncio.run(
            WeComClient(bot_id="bot", secret="secret").discover_group_chat_id(timeout=1)
        )
    finally:
        wecom.websockets.connect = original_connect  # type: ignore[assignment]

    assert chat_id == "wr_group_123"


def test_daily_report_is_sent_once_per_day(tmp_path: Path, monkeypatch: object) -> None:
    storage = make_storage(tmp_path)
    storage.save_wecom_config(enabled=True, bot_id="bot", chat_id="chat")
    run_id = storage.create_scan_run(
        trigger="watch", platforms=["douyin"], brands=["速探长"], options={}
    )
    calls: list[str] = []

    async def fake_send(self: WeComClient, chat_id: str, content: str) -> None:
        calls.append(f"{chat_id}:{content[:4]}")

    monkeypatch.setattr(CredentialStore, "get_wecom_secret", classmethod(lambda cls: "secret"))
    monkeypatch.setattr(WeComClient, "send_markdown", fake_send)

    assert asyncio.run(send_daily_report_if_due(storage, scan_run_id=run_id))
    assert not asyncio.run(send_daily_report_if_due(storage, scan_run_id=run_id))
    assert len(calls) == 1


def test_manual_scan_can_send_daily_report(tmp_path: Path, monkeypatch: object) -> None:
    storage = make_storage(tmp_path)
    storage.save_wecom_config(enabled=True, bot_id="bot", chat_id="chat")
    run_id = storage.create_scan_run(
        trigger="manual", platforms=["douyin"], brands=["速探长"], options={}
    )
    calls: list[str] = []

    async def fake_send(self: WeComClient, chat_id: str, content: str) -> None:
        calls.append(chat_id)

    monkeypatch.setattr(CredentialStore, "get_wecom_secret", classmethod(lambda cls: "secret"))
    monkeypatch.setattr(WeComClient, "send_markdown", fake_send)

    assert asyncio.run(send_daily_report_if_due(storage, scan_run_id=run_id))
    assert calls == ["chat"]


def test_daily_report_claim_prevents_duplicate_senders(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run_id = storage.create_scan_run(
        trigger="watch", platforms=["douyin"], brands=["示例品牌"], options={}
    )

    assert storage.claim_daily_report(
        report_date="2026-08-21",
        scan_run_id=run_id,
        content="日报内容",
    )
    assert not storage.claim_daily_report(
        report_date="2026-08-21",
        scan_run_id=run_id,
        content="重复日报",
    )
    assert storage.mark_daily_report_sent("2026-08-21")
    assert not storage.mark_daily_report_sent("2026-08-21")
