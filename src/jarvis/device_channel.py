"""
丽萨终端 - 设备通道 (Device Channel)

ESP32 硬件终端通过 WebSocket 连接此端点，实现：
  - 按键录音 → ASR 转写 → LLM 回复 → TTS 合成 → 音频下发
  - 屏幕文字推送
  - LED 灯环控制
  - 心跳保活

协议：
  ESP32 → Server:
    {"type": "hello", "device_id": "...", "firmware_version": "..."}
    {"type": "button", "state": "pressed"|"released"}
    {"type": "audio", "sample_rate": 16000, "format": "pcm16", "data": "<base64>"}
    {"type": "ping", "state": 0, "rssi": -50}

  Server → ESP32:
    {"type": "welcome", "device_id": "...", "session_id": "..."}
    {"type": "display", "status": "THINKING", "text": "我在听..."}
    {"type": "led", "mode": "solid", "r": 0, "g": 200, "b": 0}
    {"type": "error", "message": "..."}
    {"type": "pong"}
    <binary WAV audio data>

集成方式：
  在 bridge.py 的 create_app() 中追加两行：
    from jarvis.device_channel import register_device_channel
    register_device_channel(app, bridge)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Any, Callable

from aiohttp import WSMsgType, web

logger = logging.getLogger(__name__)

# ==================== 设备会话 ====================

class DeviceSession:
    """一个 ESP32 设备的连接会话"""

    def __init__(self, device_id: str, websocket: web.WebSocketResponse):
        self.device_id = device_id
        self.ws = websocket
        self.session_id = uuid.uuid4().hex[:12]
        self.connected_at = time.time()
        self.last_heartbeat = time.time()
        self.is_recording = False
        self.audio_buffer: list[bytes] = []  # 累积 PCM 数据
        self.total_samples = 0

    def reset_recording(self):
        self.audio_buffer.clear()
        self.total_samples = 0
        self.is_recording = False


# ==================== 全局设备注册表 ====================

_connected_devices: dict[str, DeviceSession] = {}


def get_connected_devices() -> dict[str, DeviceSession]:
    return dict(_connected_devices)


# ==================== 工具执行状态映射 ====================

_TOOL_STATUS_MAP: dict[str, str] = {
    # 桌面控制
    "computer_open_app": "正在打开 {application}",
    "computer_close_app": "正在关闭 {application}",
    "computer_focus_window": "正在切换到 {window}",
    "computer_find_and_click": "正在点击 {target}",
    "computer_keyboard_type": "正在输入文字",
    "computer_keyboard_hotkey": "正在按下快捷键",
    "computer_screenshot": "正在截屏",
    "computer_analyze_screen": "正在分析屏幕",
    # 信息搜索
    "web_search": "正在搜索 {query}",
    "web_read_page": "正在读取网页",
    "browser_open_url": "正在打开浏览器",
    "get_weather": "正在查天气",
    "search_nearby": "正在搜索附近",
    "get_current_time": "正在查询时间",
    # 文件/系统
    "list_workspace": "正在列出文件",
    "read_workspace_file": "正在读取文件",
    "run_workspace_command": "正在执行命令",
    "get_system_info": "正在查看系统信息",
    # 记忆
    "remember": "正在记住",
    "recall": "正在回忆",
    # 调度
    "schedule_reminder": "正在设置提醒",
    "execute_goal": "正在执行任务",
}


def _format_tool_status(tool_name: str, arguments: dict | None) -> str:
    """把工具名+参数格式化成人类可读的中文状态"""
    template = _TOOL_STATUS_MAP.get(tool_name)
    if template and arguments:
        try:
            return template.format(**arguments)
        except (KeyError, IndexError):
            return template
    if template:
        return template
    # 未知工具：显示工具名（去掉前缀）
    short_name = tool_name.replace("computer_", "").replace("_", " ")
    return f"正在执行 {short_name}"


# ==================== WebSocket 处理器 ====================

async def handle_device_ws(request: web.Request) -> web.WebSocketResponse:
    """
    GET /ws/device?token=xxx&device_id=lisa-esp32-01

    ESP32 终端的 WebSocket 端点。
    复用 bridge 的鉴权逻辑（X-Jarvis-Token / ?token=）。
    """
    bridge: Any = request.app["bridge"]

    # 鉴权（复用 bridge 的 token 校验）
    try:
        bridge._authorize_request(request)
    except web.HTTPUnauthorized as exc:
        # WebSocket 无法返回 HTTP 错误，记录日志后拒绝
        logger.warning("Device channel: unauthorized connection attempt from %s", request.remote)
        return web.WebSocketResponse()

    device_id = request.query.get("device_id", f"unknown-{uuid.uuid4().hex[:8]}")
    owner_id, _ = bridge._request_scope(request)

    websocket = web.WebSocketResponse(heartbeat=20)
    await websocket.prepare(request)

    session = DeviceSession(device_id, websocket)
    _connected_devices[device_id] = session

    logger.info(
        "Device connected: %s (session=%s, owner=%s)",
        device_id, session.session_id, owner_id,
    )

    # 发送欢迎消息
    await websocket.send_json({
        "type": "welcome",
        "device_id": device_id,
        "session_id": session.session_id,
    })

    # 推送待机画面
    await _push_display(websocket, "LISA", "Press BOOT to talk")
    await _push_led(websocket, 0, 0, 80, mode="solid")

    # ASR 提供者（按需创建）
    asr_provider = None
    partial_task: asyncio.Task | None = None
    asr_started_at: float | None = None

    try:
        async for message in websocket:
            session.last_heartbeat = time.time()

            if message.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except json.JSONDecodeError:
                    continue

                msg_type = payload.get("type", "")

                if msg_type == "hello":
                    # 设备上线握手
                    logger.info(
                        "Device hello: %s fw=%s",
                        device_id, payload.get("firmware_version", "?"),
                    )
                    await _push_display(websocket, "LISA", "Press BOOT to talk")

                elif msg_type == "button":
                    btn_state = payload.get("state", "")
                    if btn_state == "pressed":
                        # 开始录音
                        session.is_recording = True
                        session.reset_recording()
                        session.is_recording = True

                        # 创建 ASR 提供者
                        asr_provider = _create_asr_for_device(bridge, owner_id)
                        await asr_provider.start_session(
                            session_id=session.session_id,
                            request_id=f"device-{device_id}-{int(time.time())}",
                        )
                        asr_started_at = time.perf_counter()

                        await _push_display(websocket, "RECORDING", "Speaking...")
                        await _push_led(websocket, 150, 0, 0, mode="solid")
                        logger.info("Device %s started recording", device_id)

                    elif btn_state == "released":
                        # 停止录音，开始处理
                        session.is_recording = False
                        await _push_display(websocket, "THINKING", "Waiting for Lisa...")
                        await _push_led(websocket, 120, 80, 0, mode="solid")

                        if asr_provider and session.audio_buffer:
                            # 合并所有音频数据
                            raw_pcm = b"".join(session.audio_buffer)
                            logger.info(
                                "Device %s: %d samples recorded, processing...",
                                device_id, session.total_samples,
                            )

                            # 把 PCM 转成 WAV 给 ASR
                            wav_bytes = _pcm_to_wav(raw_pcm, session.total_samples, sample_rate=16000)

                            # 用 whisper 转写
                            transcript = await _transcribe_pcm(bridge, wav_bytes)
                            logger.info("Device %s transcript: %s", device_id, transcript)

                            if transcript.strip():
                                # 发给 LLM（带实时工具状态反馈）
                                await _push_display(websocket, "THINKING", transcript)
                                reply = await _ask_llm(bridge, transcript, session.session_id, websocket)
                                logger.info("Device %s reply: %s", device_id, reply[:80] if reply else "")

                                if reply:
                                    # TTS 合成
                                    await _push_display(websocket, "SPEAKING", reply)
                                    await _push_led(websocket, 0, 150, 0, mode="solid")

                                    audio_bytes, sample_rate = await _synthesize_tts(bridge, reply)

                                    # 发送 WAV 音频（二进制帧）
                                    if audio_bytes:
                                        await websocket.send_bytes(audio_bytes)
                                        logger.info(
                                            "Device %s: sent %d bytes audio",
                                            device_id, len(audio_bytes),
                                        )

                                    # 恢复待机
                                    await asyncio.sleep(0.5)
                                    await _push_display(websocket, "LISA", "Press BOOT to talk")
                                    await _push_led(websocket, 0, 0, 80, mode="solid")
                            else:
                                await _push_display(websocket, "LISA", "没听清，再说一次")
                                await asyncio.sleep(2)
                                await _push_display(websocket, "LISA", "Press BOOT to talk")
                                await _push_led(websocket, 0, 0, 80, mode="solid")

                        # 清理 ASR
                        if asr_provider:
                            await asr_provider.cancel()
                            asr_provider = None

                elif msg_type == "audio":
                    # 录音中的音频数据
                    if session.is_recording and asr_provider:
                        b64_data = payload.get("data", "")
                        if b64_data:
                            pcm_bytes = base64.b64decode(b64_data)
                            session.audio_buffer.append(pcm_bytes)
                            session.total_samples += len(pcm_bytes) // 2  # 16-bit = 2 bytes/sample

                elif msg_type == "ping":
                    await websocket.send_json({"type": "pong"})

            elif message.type == WSMsgType.ERROR:
                logger.error(
                    "Device %s WebSocket error: %s",
                    device_id, websocket.exception(),
                )
                break

    except Exception as exc:
        logger.error("Device %s handler error: %s", device_id, exc, exc_info=True)
    finally:
        _connected_devices.pop(device_id, None)
        if asr_provider:
            await asr_provider.cancel()
        logger.info("Device disconnected: %s", device_id)

    return websocket


# ==================== 辅助函数 ====================

def _create_asr_for_device(bridge: Any, owner_id: str) -> Any:
    """为设备会话创建 ASR 提供者"""
    from jarvis.asr_provider import create_asr_provider
    return create_asr_provider(
        bridge.settings,
        bridge.whisper,
        bridge.whisper_partial,
        initial_prompt=bridge._asr_initial_prompt(owner_id),
    )


async def _transcribe_pcm(bridge: Any, wav_bytes: bytes) -> str:
    """用 whisper 转写 PCM 音频"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="jarvis-device-") as tmpdir:
        wav_path = Path(tmpdir) / "device_input.wav"
        wav_path.write_bytes(wav_bytes)

        segments, info = await asyncio.to_thread(
            bridge.whisper.transcribe,
            str(wav_path),
            language="zh",
            vad_filter=True,
            beam_size=bridge.settings.asr_final_beam_size,
        )
        return "".join(seg.text for seg in segments).strip()


async def _ask_llm(
    bridge: Any, text: str, session_id: str, ws: web.WebSocketResponse | None = None,
) -> str:
    """
    发送文本给 LLM 获取回复。

    如果传入了 ws，会在工具执行期间实时推送状态到 ESP32 屏幕：
      - TOOL_REQUEST → 推送 "正在打开 Chrome..." 等中文描述
      - TOOL_RESULT  → 推送 "完成" 短暂确认
      - TOOL_ERROR   → 推送 "出错了" 短暂提示
    """
    from jarvis.runtime import CancellationToken, EventType

    cancellation = CancellationToken()
    unsub: Callable[[], None] | None = None

    # 如果传了 websocket，订阅工具执行事件
    if ws is not None:
        def _on_tool_event(event: Any) -> None:
            """同步回调：把事件丢进 asyncio 队列，由协程推送到 ESP32"""
            # 只关心当前 session 的事件（避免其他设备/桌面语音的事件串台）
            if event.task_id and event.task_id != f"device-{session_id}":
                return
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(_event_queue.put_nowait, event)
            except Exception:
                pass

        _event_queue: asyncio.Queue = asyncio.Queue()

        async def _push_tool_events():
            """协程：从队列读事件，推送到 ESP32 屏幕"""
            while True:
                try:
                    event = await asyncio.wait_for(_event_queue.get(), timeout=0.3)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break

                tool_name = event.data.get("tool", "")
                evt_type = str(event.type)

                if evt_type == "TOOL_REQUEST":
                    arguments = event.data.get("arguments")
                    status_text = _format_tool_status(tool_name, arguments)
                    await _push_display(ws, "WORKING", status_text)
                    # 工具执行中：紫色灯环
                    await _push_led(ws, 80, 0, 160, mode="solid")
                    logger.info("Device tool executing: %s", tool_name)

                elif evt_type == "TOOL_RESULT":
                    await _push_display(ws, "WORKING", "好的，继续...")
                    logger.info("Device tool done: %s", tool_name)

                elif evt_type == "TOOL_ERROR":
                    error_msg = event.data.get("error", "未知错误")
                    await _push_display(ws, "WORKING", f"遇到问题: {error_msg[:20]}")
                    await _push_led(ws, 160, 0, 0, mode="solid")
                    logger.warning("Device tool error: %s — %s", tool_name, error_msg)

        # 订阅事件
        unsub = bridge.event_bus.subscribe(
            _on_tool_event,
            event_types={EventType.TOOL_REQUEST, EventType.TOOL_RESULT, EventType.TOOL_ERROR},
        )
        # 启动推送协程
        push_task = asyncio.create_task(_push_tool_events())
    else:
        push_task = None

    try:
        reply = await bridge.agent.ask(
            text,
            session_id=session_id,
            cancellation=cancellation,
            task_id=f"device-{session_id}",
        )
        return reply.text
    except Exception as exc:
        logger.error("LLM ask failed: %s", exc)
        return "抱歉，我出了点问题，请稍后再试。"
    finally:
        # 清理：取消订阅 + 停止推送协程
        if unsub:
            unsub()
        if push_task and not push_task.done():
            push_task.cancel()
            try:
                await push_task
            except asyncio.CancelledError:
                pass


async def _synthesize_tts(bridge: Any, text: str) -> tuple[bytes | None, int | None]:
    """TTS 合成，返回 (wav_bytes, sample_rate)"""
    try:
        synthesizer = await bridge._get_synthesizer()
        audio_bytes, sample_rate = await asyncio.to_thread(
            synthesizer.synthesize_wav_bytes,
            text,
        )
        return audio_bytes, sample_rate
    except Exception as exc:
        logger.error("TTS synthesis failed: %s", exc)
        return None, None


def _pcm_to_wav(pcm_data: bytes, num_samples: int, sample_rate: int = 16000) -> bytes:
    """把 raw PCM 16-bit mono 包装成 WAV 文件"""
    import struct

    data_size = len(pcm_data)
    wav_header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,             # chunk size
        1,              # PCM format
        1,              # mono
        sample_rate,
        sample_rate * 2,  # byte rate
        2,              # block align
        16,             # bits per sample
        b"data",
        data_size,
    )
    return wav_header + pcm_data


async def _push_display(ws: web.WebSocketResponse, status: str, text: str):
    """推送屏幕显示内容"""
    try:
        await ws.send_json({
            "type": "display",
            "status": status,
            "text": text,
        })
    except Exception:
        pass


async def _push_led(ws: web.WebSocketResponse, r: int, g: int, b: int, mode: str = "solid"):
    """推送 LED 灯环颜色"""
    try:
        await ws.send_json({
            "type": "led",
            "mode": mode,
            "r": r,
            "g": g,
            "b": b,
        })
    except Exception:
        pass


# ==================== 注册到 bridge app ====================

def register_device_channel(app: web.Application, bridge: Any) -> None:
    """
    在 bridge 的 aiohttp app 上注册设备通道端点。

    用法（在 bridge.py 的 create_app 末尾添加）：
        from jarvis.device_channel import register_device_channel
        register_device_channel(app, bridge)
    """
    app.router.add_get("/ws/device", handle_device_ws)
    logger.info("Device channel registered: GET /ws/device")
