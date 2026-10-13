from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import wave
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from typing import Any

import edge_tts
import piper
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.piper.tts import PiperTTSService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.workers.runner import WorkerRunner
from piper import PiperVoice
from piper.config import SynthesisConfig

from jarvis.agent import build_system_prompt
from jarvis.config import Settings
from jarvis.memory import MemoryStore, memory_lines
from jarvis.persona import PersonaManager
from jarvis.tools import Toolbox


def ensure_espeak_data_dir() -> Path:
    """Return an ASCII-safe espeak data directory for the native Piper bridge."""
    package_data = Path(piper.__file__).parent / "espeak-ng-data"
    if os.name != "nt":
        return package_data

    target = Path(
        os.environ.get(
            "JARVIS_ESPEAK_DATA_DIR",
            r"C:\Users\Public\Jarvis\espeak-ng-data",
        )
    ).expanduser()
    if not (target / "phontab").exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(package_data, target, dirs_exist_ok=True)
    return target


def patch_piper_espeak_path() -> None:
    """Inject an ASCII espeak path because the native bridge rejects non-ASCII paths."""
    if getattr(PiperVoice.load, "_jarvis_patched", False):
        return

    default_espeak_data_dir = ensure_espeak_data_dir()
    original_load = PiperVoice.load

    def load_with_ascii_data(
        model_path: str | Path,
        config_path: str | Path | None = None,
        use_cuda: bool = False,
        espeak_data_dir: str | Path | None = None,
        download_dir: str | Path | None = None,
        include_alignments: bool = False,
    ) -> PiperVoice:
        return original_load(
            model_path,
            config_path=config_path,
            use_cuda=use_cuda,
            espeak_data_dir=espeak_data_dir or default_espeak_data_dir,
            download_dir=download_dir,
            include_alignments=include_alignments,
        )

    load_with_ascii_data._jarvis_patched = True  # type: ignore[attr-defined]
    PiperVoice.load = staticmethod(load_with_ascii_data)


class SpeechSynthesizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backend = settings.tts_backend
        self.voice: PiperVoice | None = None
        if self.backend == "piper":
            patch_piper_espeak_path()
            piper_voice = settings.tts_piper_voice or settings.tts_voice
            voice_path = settings.data_dir / "piper" / f"{piper_voice}.onnx"
            if not voice_path.exists():
                raise FileNotFoundError(f"Piper voice not found: {voice_path}")
            self.voice = PiperVoice.load(voice_path)

    def synthesize(self, text: str, output: Path) -> Path:
        text = prepare_text_for_speech(text)
        if not text:
            raise ValueError("No speakable text remained after normalization.")
        output_path = output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.backend == "edge":
            with tempfile.TemporaryDirectory(prefix="jarvis-edge-tts-") as temporary:
                mp3_path = Path(temporary) / "speech.mp3"
                self._synthesize_edge_mp3(text, mp3_path)
                completed = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(mp3_path),
                        "-ar",
                        "24000",
                        "-ac",
                        "1",
                        "-f",
                        "wav",
                        str(output_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr.decode(errors="ignore"))
            return output_path
        if self.voice is None:
            raise RuntimeError("Piper voice was not initialized.")
        with wave.open(str(output_path), "wb") as wav_file:
            self.voice.synthesize_wav(
                text,
                wav_file,
                syn_config=self._piper_config(),
            )
        return output_path

    def synthesize_wav_bytes(self, text: str) -> tuple[bytes, int]:
        """Synthesize speech in memory so streaming does not touch the disk."""
        text = prepare_text_for_speech(text)
        if not text:
            return b"", 24_000
        if self.backend == "edge":
            with tempfile.TemporaryDirectory(prefix="jarvis-edge-tts-") as temporary:
                mp3_path = Path(temporary) / "speech.mp3"
                self._synthesize_edge_mp3(text, mp3_path)
                completed = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(mp3_path),
                        "-ar",
                        "24000",
                        "-ac",
                        "1",
                        "-f",
                        "wav",
                        "pipe:1",
                    ],
                    check=True,
                    capture_output=True,
                )
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr.decode(errors="ignore"))
                return completed.stdout, 24_000
        if self.voice is None:
            raise RuntimeError("Piper voice was not initialized.")
        buffer = BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            self.voice.synthesize_wav(
                text,
                wav_file,
                syn_config=self._piper_config(),
            )
        audio = buffer.getvalue()
        return audio, wav_sample_rate_bytes(audio)

    async def stream_audio(self, text: str) -> AsyncIterator[tuple[bytes, str, int]]:
        """Yield seamless WAV(PCM) chunks for smooth gapless playback."""
        text = prepare_text_for_speech(text)
        if not text:
            return
        if self.backend == "edge":
            # 先把整句从 mp3 转成 PCM，再切成小块——块间天然无缝，杜绝解码边界卡顿
            audio, sample_rate = await asyncio.to_thread(self.synthesize_wav_bytes, text)
            if not audio or len(audio) <= 44:
                return
            payload = audio[44:]
            chunk_size = 24_000  # 约 0.5 秒 24kHz 16-bit PCM
            if len(payload) <= chunk_size:
                yield audio, "wav", sample_rate
                return
            for offset in range(0, len(payload), chunk_size):
                segment = payload[offset : offset + chunk_size]
                buffer = BytesIO()
                with wave.open(buffer, "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(sample_rate)
                    wav_file.writeframes(segment)
                yield buffer.getvalue(), "wav", sample_rate
            return
        audio, sample_rate = await asyncio.to_thread(self.synthesize_wav_bytes, text)
        if audio:
            yield audio, "wav", sample_rate

    def _piper_config(self) -> SynthesisConfig:
        return SynthesisConfig(
            volume=1.2,
            length_scale=self.settings.tts_length_scale,
        )

    def _synthesize_edge_mp3(self, text: str, output: Path) -> None:
        async def run() -> None:
            communicate = edge_tts.Communicate(
                text,
                self.settings.tts_voice,
                rate=self.settings.tts_rate,
            )
            await communicate.save(str(output))

        asyncio.run(run())


def split_text_for_speech(text: str, max_chars: int = 120) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    parts = re.findall(r"[^。！？!?；;，,\n]+[。！？!?；;，,]?", normalized)
    chunks: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        while len(part) > max_chars:
            chunks.append(part[:max_chars])
            part = part[max_chars:]
        if part:
            chunks.append(part)
    return chunks


def prepare_text_for_speech(text: str) -> str:
    """Remove screen-oriented markup that should not be read aloud."""
    value = re.sub(r"```.*?```", " 代码部分请查看屏幕。 ", text, flags=re.DOTALL)
    value = re.sub(r"```[^\n]*", " 代码部分请查看屏幕。 ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"https?://\S+", " 链接 ", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"(?m)^\s*(?:#{1,6}|[-*+]|\d+[.)])\s*", "", value)
    value = value.replace("**", "").replace("__", "").replace("~~", "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def wav_sample_rate_bytes(audio: bytes) -> int:
    if len(audio) < 28:
        return 24_000
    return int.from_bytes(audio[24:28], "little")


def synthesize_text(settings: Settings, text: str, output: Path | None = None) -> Path:
    """Synthesize text to a WAV file with the configured Piper voice."""
    patch_piper_espeak_path()
    settings.ensure_data_dir()
    output_path = output or (settings.data_dir / "artifacts" / "speech.wav")
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    text = prepare_text_for_speech(text)
    if not text:
        raise ValueError("No speakable text remained after normalization.")
    synthesizer = SpeechSynthesizer(settings)
    synthesizer.synthesize(text, output_path)
    return output_path


def play_wav(path: Path) -> None:
    """Play a PCM WAV file through the default Windows audio output."""
    if os.name == "nt":
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return

    import pyaudio

    with wave.open(str(path), "rb") as wav_file:
        audio = pyaudio.PyAudio()
        stream = None
        try:
            stream = audio.open(
                format=audio.get_format_from_width(wav_file.getsampwidth()),
                channels=wav_file.getnchannels(),
                rate=wav_file.getframerate(),
                output=True,
            )
            chunk_size = 4096
            data = wav_file.readframes(chunk_size)
            while data:
                stream.write(data)
                data = wav_file.readframes(chunk_size)
        finally:
            if stream is not None:
                stream.stop_stream()
                stream.close()
            audio.terminate()


async def run_voice(settings: Settings) -> None:
    """Run the local microphone/speaker assistant loop."""
    patch_piper_espeak_path()
    settings.ensure_data_dir()
    memory = MemoryStore(settings.database_path)
    persona = PersonaManager(settings)
    toolbox = Toolbox(settings, memory)

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        )
    )

    stt = WhisperSTTService(
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        settings=WhisperSTTService.Settings(
            model=settings.whisper_model,
            language=Language.ZH,
            no_speech_prob=0.4,
        ),
    )
    tts = PiperTTSService(
        download_dir=settings.data_dir / "piper",
        settings=PiperTTSService.Settings(
            voice=settings.tts_piper_voice or settings.tts_voice,
            language=Language.ZH,
        ),
    )
    llm_params = None
    if settings.reasoning_effort:
        llm_params = OpenAILLMService.InputParams(
            extra={"reasoning_effort": settings.reasoning_effort}
        )

    llm = OpenAILLMService(
        api_key=settings.model_api_key.get_secret_value(),
        base_url=settings.model_base_url,
        params=llm_params,
        settings=OpenAILLMService.Settings(
            model=settings.model,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            system_instruction=build_system_prompt(
                settings,
                [],
                persona_prompt=persona.prompt_for(),
            ),
        ),
    )

    context = LLMContext(tools=_voice_tools(toolbox))
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )
    runner = WorkerRunner()
    await runner.add_workers(worker)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport: Any, client: Any) -> None:
        recalled = memory.recall_for_context("用户偏好 重要事项", settings.max_memory_results)
        if recalled:
            context.add_message(
                {
                    "role": "developer",
                    "content": "Relevant durable memory:\n"
                    + "\n".join(f"- {line}" for line in memory_lines(recalled)),
                }
            )
        context.add_message(
            {
                "role": "developer",
                "content": (
                    f"Introduce yourself as {settings.assistant_name} and ask how you can help."
                    " Keep the greeting short."
                ),
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport: Any, client: Any) -> None:
        await runner.cancel()

    try:
        await runner.run()
    finally:
        memory.close()


def _voice_tools(toolbox: Toolbox) -> list[Any]:
    """Adapt core tools to Pipecat function-call handlers."""

    async def get_current_time(
        params: FunctionCallParams,
        timezone: str | None = None,
    ) -> None:
        """Get the current date and time in a named time zone.

        Args:
            timezone: IANA timezone such as Asia/Shanghai.
        """
        await params.result_callback(await toolbox.get_current_time(timezone))

    async def remember(
        params: FunctionCallParams,
        content: str,
        category: str = "general",
    ) -> None:
        """Store a durable fact, preference, project detail, or commitment about the user.

        Args:
            content: The fact to remember.
            category: Short category such as preference, project, or commitment.
        """
        await params.result_callback(await toolbox.remember(content, category))

    async def recall(
        params: FunctionCallParams,
        query: str,
        limit: int = 6,
    ) -> None:
        """Search durable memory for relevant facts or preferences.

        Args:
            query: Search query.
            limit: Maximum number of memories.
        """
        await params.result_callback(await toolbox.recall(query, limit))

    async def update_memory(
        params: FunctionCallParams,
        memory_id: int,
        content: str | None = None,
        category: str | None = None,
        importance: int | None = None,
        expires_at: str | None = None,
    ) -> None:
        """Update an existing durable memory.

        Args:
            memory_id: Memory id returned by recall.
            content: Replacement content.
            category: Replacement category.
            importance: New importance from 1 to 5.
            expires_at: ISO-8601 expiry timestamp.
        """
        await params.result_callback(
            await toolbox.update_memory(
                memory_id,
                content,
                category,
                importance,
                expires_at,
            )
        )

    async def forget_memory(params: FunctionCallParams, memory_id: int) -> None:
        """Delete a durable memory that is no longer true.

        Args:
            memory_id: Memory id returned by recall.
        """
        await params.result_callback(await toolbox.forget_memory(memory_id))

    async def get_weather(params: FunctionCallParams, city: str) -> None:
        """Get current weather and a short daily forecast for a city.

        Args:
            city: City name, optionally with country or region.
        """
        await params.result_callback(await toolbox.get_weather(city))

    async def search_nearby(
        params: FunctionCallParams,
        keyword: str,
        location: str | None = None,
        radius: int = 2_000,
        limit: int = 8,
    ) -> None:
        """Search nearby places around a location or the configured home city.

        Args:
            keyword: What to look for, such as 咖啡店 or 火锅.
            location: Anchor place such as 上海人民广场; defaults to the home city.
            radius: Search radius in meters.
            limit: Maximum number of places to return.
        """
        await params.result_callback(
            await toolbox.search_nearby(keyword, location, radius, limit)
        )

    async def web_search(
        params: FunctionCallParams,
        query: str,
        max_results: int = 5,
    ) -> None:
        """Search the public web and return ranked titles, URLs, and snippets.

        Args:
            query: Search query.
            max_results: Maximum number of results.
        """
        await params.result_callback(await toolbox.web_search(query, max_results))

    async def web_read_page(
        params: FunctionCallParams,
        url: str,
        max_chars: int = 8_000,
    ) -> None:
        """Read and extract a web page.

        Args:
            url: Absolute HTTP or HTTPS URL.
            max_chars: Maximum extracted characters.
        """
        await params.result_callback(await toolbox.web_read_page(url, max_chars))

    async def browser_open_url(params: FunctionCallParams, url: str) -> None:
        """Open a URL in the default browser.

        Args:
            url: Absolute HTTP or HTTPS URL.
        """
        await params.result_callback(await toolbox.browser_open_url(url))

    async def get_system_info(params: FunctionCallParams) -> None:
        """Inspect the local operating system, CPU, and disk space."""
        await params.result_callback(await toolbox.get_system_info())

    async def list_workspace(params: FunctionCallParams, path: str = ".") -> None:
        """List files and directories inside the configured workspace.

        Args:
            path: Relative path inside the workspace.
        """
        await params.result_callback(await toolbox.list_workspace(path))

    async def read_workspace_file(params: FunctionCallParams, path: str) -> None:
        """Read a bounded UTF-8 text file from the configured workspace.

        Args:
            path: Relative file path inside the workspace.
        """
        await params.result_callback(await toolbox.read_workspace_file(path))

    async def computer_window_list(params: FunctionCallParams) -> None:
        """List visible desktop windows."""
        await params.result_callback(await toolbox.computer_window_list())

    async def computer_screenshot(params: FunctionCallParams) -> None:
        """Capture the current desktop screenshot."""
        await params.result_callback(await toolbox.computer_screenshot())

    async def computer_analyze_screen(
        params: FunctionCallParams,
        prompt: str = "Describe the screen and identify actionable UI elements.",
    ) -> None:
        """Analyze the current desktop with the configured vision model.

        Args:
            prompt: Question or instruction for the vision model.
        """
        await params.result_callback(await toolbox.computer_analyze_screen(prompt))

    async def computer_ui_tree(
        params: FunctionCallParams,
        max_items: int = 120,
    ) -> None:
        """Inspect the Windows UI Automation tree.

        Args:
            max_items: Maximum number of UI elements to return.
        """
        await params.result_callback(await toolbox.computer_ui_tree(max_items))

    async def computer_ui_invoke(
        params: FunctionCallParams,
        name: str | None = None,
        automation_id: str | None = None,
    ) -> None:
        """Invoke a UI Automation element.

        Args:
            name: Visible element name.
            automation_id: Automation identifier.
        """
        await params.result_callback(
            await toolbox.computer_ui_invoke(name, automation_id)
        )

    async def computer_find_and_click(
        params: FunctionCallParams,
        name: str | None = None,
        automation_id: str | None = None,
    ) -> None:
        """Find and click a UI Automation element.

        Args:
            name: Visible element name.
            automation_id: Automation identifier.
        """
        await params.result_callback(
            await toolbox.computer_find_and_click(name, automation_id)
        )

    async def computer_open_app(params: FunctionCallParams, application: str) -> None:
        """Open an application on the desktop.

        Args:
            application: Application name or executable.
        """
        await params.result_callback(await toolbox.computer_open_app(application))

    async def computer_close_app(params: FunctionCallParams, application: str) -> None:
        """Close an application on the desktop.

        Args:
            application: Application name or executable.
        """
        await params.result_callback(await toolbox.computer_close_app(application))

    async def computer_focus_window(params: FunctionCallParams, target: str) -> None:
        """Focus a desktop window.

        Args:
            target: Window title or process identifier.
        """
        await params.result_callback(await toolbox.computer_focus_window(target))

    async def computer_keyboard_type(params: FunctionCallParams, text: str) -> None:
        """Type text into the focused desktop window.

        Args:
            text: Text to type.
        """
        await params.result_callback(await toolbox.computer_keyboard_type(text))

    async def computer_keyboard_press(params: FunctionCallParams, key: str) -> None:
        """Press a key in the focused desktop window.

        Args:
            key: Key name such as enter or escape.
        """
        await params.result_callback(await toolbox.computer_keyboard_press(key))

    async def computer_mouse_click(
        params: FunctionCallParams,
        x: int | None = None,
        y: int | None = None,
    ) -> None:
        """Click the desktop.

        Args:
            x: Optional X coordinate.
            y: Optional Y coordinate.
        """
        await params.result_callback(await toolbox.computer_mouse_click(x, y))

    async def computer_scroll(params: FunctionCallParams, delta: int) -> None:
        """Scroll the desktop.

        Args:
            delta: Wheel delta.
        """
        await params.result_callback(await toolbox.computer_scroll(delta))

    async def run_workspace_command(
        params: FunctionCallParams,
        command: str,
        args: list[str] | None = None,
        timeout: float = 60,
        cwd: str = ".",
    ) -> None:
        """Run an allowlisted code or test command inside the workspace.

        Args:
            command: Allowlisted executable name.
            args: Command arguments.
            timeout: Maximum runtime in seconds.
            cwd: Relative working directory inside the workspace.
        """
        await params.result_callback(
            await toolbox.run_workspace_command(command, args, timeout, cwd)
        )

    async def run_background_command(
        params: FunctionCallParams,
        command: str,
        args: list[str] | None = None,
        timeout: float = 60,
        cwd: str = ".",
        name: str | None = None,
    ) -> None:
        """Run an allowlisted workspace command in the background.

        Args:
            command: Allowlisted executable name.
            args: Command arguments.
            timeout: Maximum runtime in seconds.
            cwd: Relative working directory inside the workspace.
            name: Optional background job name.
        """
        await params.result_callback(
            await toolbox.run_background_command(
                command,
                args,
                timeout,
                cwd,
                name,
            )
        )

    async def schedule_reminder(
        params: FunctionCallParams,
        message: str,
        after_seconds: float,
        name: str | None = None,
    ) -> None:
        """Schedule a one-shot reminder.

        Args:
            message: Reminder text.
            after_seconds: Delay before the reminder.
            name: Optional reminder name.
        """
        await params.result_callback(
            await toolbox.schedule_reminder(message, after_seconds, name)
        )

    tools: list[Any] = [
        get_current_time,
        remember,
        recall,
        update_memory,
        forget_memory,
        get_weather,
        search_nearby,
        web_search,
        web_read_page,
        browser_open_url,
        get_system_info,
        list_workspace,
        read_workspace_file,
        computer_window_list,
        computer_screenshot,
        computer_analyze_screen,
        computer_ui_tree,
        computer_ui_invoke,
        computer_find_and_click,
        computer_open_app,
        computer_close_app,
        computer_focus_window,
        computer_keyboard_type,
        computer_keyboard_press,
        computer_mouse_click,
        computer_scroll,
        run_workspace_command,
    ]
    if toolbox.scheduler is not None:
        tools.extend([schedule_reminder, run_background_command])
    if toolbox.vision_provider is None:
        tools = [tool for tool in tools if tool is not computer_analyze_screen]
    return tools
