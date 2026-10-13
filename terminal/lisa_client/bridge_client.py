"""Bridge WebSocket 客户端

连接 PC 端的丽萨 bridge 服务器，实现：
  - 自动重连（指数退避）
  - 心跳保活
  - 音频发送（PCM → base64）
  - 消息接收（display / led / audio / error）
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class BridgeMessage:
    """从 bridge 收到的消息"""
    type: str = ""
    status: str = ""
    text: str = ""
    r: int = 0
    g: int = 0
    b: int = 0
    mode: str = "solid"
    audio_data: bytes = b""
    raw: dict[str, Any] = field(default_factory=dict)


class BridgeClient:
    """WebSocket 客户端，连接丽萨 bridge 服务器"""

    def __init__(
        self,
        ws_url: str,
        device_id: str = "lisa-opi5-01",
        firmware_version: str = "1.0.0",
        reconnect_interval: float = 5.0,
        heartbeat_interval: float = 15.0,
    ):
        self.ws_url = ws_url
        self.device_id = device_id
        self.firmware_version = firmware_version
        self.reconnect_interval = reconnect_interval
        self.heartbeat_interval = heartbeat_interval

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._connected = False
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None

        # 回调
        self._on_message: Callable[[BridgeMessage], Awaitable[None]] | None = None
        self._on_connected: Callable[[], Awaitable[None]] | None = None
        self._on_disconnected: Callable[[], None] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def on_message(self, callback: Callable[[BridgeMessage], Awaitable[None]]) -> None:
        self._on_message = callback

    def on_connected(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._on_connected = callback

    def on_disconnected(self, callback: Callable[[], None]) -> None:
        self._on_disconnected = callback

    async def connect(self) -> None:
        """建立 WebSocket 连接（带自动重连）"""
        self._running = True
        backoff = self.reconnect_interval

        while self._running:
            try:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                logger.info("Connecting to bridge: %s", self.ws_url.split("?")[0])
                self._ws = await self._session.ws_connect(
                    self.ws_url,
                    heartbeat=20,
                )
                self._connected = True
                backoff = self.reconnect_interval  # 重置退避
                logger.info("Connected to bridge!")

                # 发送 hello
                await self._send_json({
                    "type": "hello",
                    "device_id": self.device_id,
                    "firmware_version": self.firmware_version,
                })

                # 启动心跳
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                # 通知上层
                if self._on_connected:
                    await self._on_connected()

                # 接收循环
                await self._receive_loop()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Connection failed: %s (retry in %.0fs)", exc, backoff)
                self._connected = False
                if self._on_disconnected:
                    self._on_disconnected()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60)  # 最大 60 秒
            finally:
                self._connected = False
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                if self._session:
                    await self._session.close()
                    self._session = None

    async def _receive_loop(self) -> None:
        """接收消息循环"""
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await self._handle_text_message(data)

            elif msg.type == aiohttp.WSMsgType.BINARY:
                # 二进制帧 = WAV 音频数据
                bm = BridgeMessage(type="audio", audio_data=msg.data)
                if self._on_message:
                    await self._on_message(bm)

            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                logger.warning("WebSocket closed: %s", msg.type)
                break

    async def _handle_text_message(self, data: dict[str, Any]) -> None:
        """解析文本消息"""
        msg_type = data.get("type", "")
        bm = BridgeMessage(type=msg_type, raw=data)

        if msg_type == "welcome":
            logger.info("Bridge welcome: session=%s", data.get("session_id"))

        elif msg_type == "display":
            bm.status = data.get("status", "")
            bm.text = data.get("text", "")

        elif msg_type == "led":
            bm.r = data.get("r", 0)
            bm.g = data.get("g", 0)
            bm.b = data.get("b", 0)
            bm.mode = data.get("mode", "solid")

        elif msg_type == "error":
            bm.text = data.get("message", "Unknown error")
            logger.error("Bridge error: %s", bm.text)

        elif msg_type == "pong":
            return  # 心跳响应，不转发

        if self._on_message:
            await self._on_message(bm)

    async def _heartbeat_loop(self) -> None:
        """定时发送心跳"""
        try:
            while self._connected:
                await asyncio.sleep(self.heartbeat_interval)
                await self._send_json({
                    "type": "ping",
                    "state": 0,
                    "rssi": -50,
                })
        except asyncio.CancelledError:
            pass

    async def send_button_pressed(self) -> None:
        """通知 bridge：按钮按下（开始录音）"""
        await self._send_json({"type": "button", "state": "pressed"})

    async def send_button_released(self) -> None:
        """通知 bridge：按钮松开（录音结束）"""
        await self._send_json({"type": "button", "state": "released"})

    async def send_audio(self, pcm_data: bytes) -> None:
        """发送录音数据（PCM → base64）"""
        b64 = base64.b64encode(pcm_data).decode("ascii")
        await self._send_json({
            "type": "audio",
            "sample_rate": 16000,
            "format": "pcm16",
            "data": b64,
        })

    async def _send_json(self, data: dict[str, Any]) -> None:
        """发送 JSON 消息"""
        if self._ws and not self._ws.closed:
            try:
                await self._ws.send_json(data)
            except Exception as exc:
                logger.warning("Send failed: %s", exc)

    async def close(self) -> None:
        """关闭连接"""
        self._running = False
        self._connected = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()
