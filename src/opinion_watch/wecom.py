from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from urllib.parse import urlparse

import websockets

from opinion_watch.credentials import CredentialStore
from opinion_watch.report import build_daily_report, report_date_for_now
from opinion_watch.storage import Storage

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
AUTH_COMMAND = "aibot_subscribe"
SEND_COMMAND = "aibot_send_msg"


class WeComError(RuntimeError):
    pass


class WeComConfigurationError(WeComError):
    pass


class WeComDiscoveryTimeout(WeComError):
    pass


def _request_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class WeComClient:
    def __init__(self, *, bot_id: str, secret: str, ws_url: str = DEFAULT_WS_URL) -> None:
        self.bot_id = bot_id.strip()
        self.secret = secret.strip()
        self.ws_url = ws_url.strip() or DEFAULT_WS_URL
        if not self.bot_id or not self.secret:
            raise WeComConfigurationError("企微机器人 Bot ID 和 Secret 都不能为空")
        if self.ws_url != DEFAULT_WS_URL or urlparse(self.ws_url).scheme != "wss":
            raise WeComConfigurationError("企微 WebSocket 地址必须使用官方 WSS 地址")

    async def send_markdown(self, chat_id: str, content: str) -> None:
        clean_chat_id = chat_id.strip()
        if not clean_chat_id:
            raise WeComConfigurationError("企微日报必须配置群聊 ID")
        if not content.strip():
            raise ValueError("企微日报内容不能为空")

        timeout = 15
        async with websockets.connect(
            self.ws_url,
            open_timeout=timeout,
            close_timeout=5,
            ping_interval=None,
            max_size=2**20,
        ) as websocket:
            auth_request_id = _request_id(AUTH_COMMAND)
            await websocket.send(
                json.dumps(
                    {
                        "cmd": AUTH_COMMAND,
                        "headers": {"req_id": auth_request_id},
                        "body": {"bot_id": self.bot_id, "secret": self.secret},
                    },
                    ensure_ascii=False,
                )
            )
            await self._wait_ack(websocket, auth_request_id, timeout=timeout)

            send_request_id = _request_id(SEND_COMMAND)
            await websocket.send(
                json.dumps(
                    {
                        "cmd": SEND_COMMAND,
                        "headers": {"req_id": send_request_id},
                        "body": {
                            "chatid": clean_chat_id,
                            "msgtype": "markdown",
                            "markdown": {"content": content},
                        },
                    },
                    ensure_ascii=False,
                )
            )
            await self._wait_ack(websocket, send_request_id, timeout=timeout)

    async def discover_group_chat_id(self, *, timeout: float = 120) -> str:
        if timeout <= 0:
            raise ValueError("企微群聊 ID 监听时长必须大于 0")

        async with websockets.connect(
            self.ws_url,
            open_timeout=15,
            close_timeout=5,
            ping_interval=None,
            max_size=2**20,
        ) as websocket:
            auth_request_id = _request_id(AUTH_COMMAND)
            await websocket.send(
                json.dumps(
                    {
                        "cmd": AUTH_COMMAND,
                        "headers": {"req_id": auth_request_id},
                        "body": {"bot_id": self.bot_id, "secret": self.secret},
                    },
                    ensure_ascii=False,
                )
            )
            await self._wait_ack(websocket, auth_request_id, timeout=15)

            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise WeComDiscoveryTimeout("监听超时：请在目标群聊中 @机器人 后重试")
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=min(remaining, 20))
                except TimeoutError:
                    heartbeat_id = _request_id("ping")
                    await websocket.send(
                        json.dumps(
                            {"cmd": "ping", "headers": {"req_id": heartbeat_id}},
                            ensure_ascii=False,
                        )
                    )
                    await self._wait_ack(websocket, heartbeat_id, timeout=10)
                    continue

                frame = self._decode_frame(raw)
                if frame is None:
                    continue
                body = frame.get("body")
                if not isinstance(body, dict):
                    continue
                if body.get("chattype") == "group" and body.get("chatid"):
                    return str(body["chatid"])

    @staticmethod
    async def _wait_ack(websocket: Any, request_id: str, *, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise WeComError("企微机器人响应超时")
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            except TimeoutError as exc:
                raise WeComError("企微机器人响应超时") from exc
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            frame = WeComClient._decode_frame(raw)
            if frame is None:
                continue
            headers = frame.get("headers")
            if not isinstance(headers, dict) or headers.get("req_id") != request_id:
                continue
            errcode = int(frame.get("errcode", 0))
            if errcode != 0:
                errmsg = str(frame.get("errmsg") or "未知错误")
                raise WeComError(f"企微机器人返回错误 {errcode}：{errmsg}")
            return

    @staticmethod
    def _decode_frame(raw: object) -> dict[str, Any] | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            frame = json.loads(str(raw))
        except json.JSONDecodeError:
            return None
        return frame if isinstance(frame, dict) else None


async def send_daily_report_if_due(
    storage: Storage,
    *,
    scan_run_id: int,
    force: bool = False,
) -> bool:
    """Send scheduled reports once per day and every successful manual run."""
    config = storage.get_wecom_config()
    if not bool(config.get("enabled")):
        return False

    report_date = report_date_for_now()
    secret = CredentialStore.get_wecom_secret()
    if not secret:
        raise WeComConfigurationError("未找到企微机器人 Secret，请在设置中重新保存")
    content = build_daily_report(storage, report_date)
    if not force:
        claimed = storage.claim_daily_report(
            report_date=report_date,
            scan_run_id=scan_run_id,
            content=content,
        )
        if not claimed:
            return False
    try:
        client = WeComClient(
            bot_id=str(config.get("bot_id") or ""),
            secret=secret,
            ws_url=str(config.get("ws_url") or DEFAULT_WS_URL),
        )
        await client.send_markdown(str(config.get("chat_id") or ""), content)
    except Exception as exc:
        if not force:
            storage.mark_daily_report_failed(report_date, str(exc))
        raise
    if not force:
        storage.mark_daily_report_sent(report_date)
    return True
