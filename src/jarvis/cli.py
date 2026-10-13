from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jarvis.agent import Agent
from jarvis.config import Settings
from jarvis.learning import DEFAULT_TEXT_EXTENSIONS, learn_paths, normalize_extensions
from jarvis.memory import MemoryStore
from jarvis.model import OpenAICompatibleModel
from jarvis.persona import PersonaManager
from jarvis.runtime import (
    ContextEngine,
    LLMPlanner,
    MemoryEmbeddingStore,
    MemoryExtractor,
    ModelRole,
    ModelRouter,
    TaskEngine,
    TaskStore,
    TokenBudgetManager,
    VisionAnalyzer,
    mcp_server_configs_from_settings,
    permission_policy_from_settings,
)
from jarvis.tools import Toolbox

app = typer.Typer(
    name="jarvis",
    help="Local-first personal agent with memory, tools, and optional voice.",
    no_args_is_help=True,
)
memory_app = typer.Typer(help="Inspect durable memory.")
task_app = typer.Typer(help="Inspect and control durable tasks.")
permission_app = typer.Typer(help="Inspect the active permission policy.")
mcp_app = typer.Typer(help="Inspect configured MCP servers.")
vision_app = typer.Typer(help="Run the configured vision model on local images.")
persona_app = typer.Typer(help="Inspect and import the durable JARVIS persona.")
app.add_typer(memory_app, name="memory")
app.add_typer(task_app, name="task")
app.add_typer(permission_app, name="permission")
app.add_typer(mcp_app, name="mcp")
app.add_typer(vision_app, name="vision")
app.add_typer(persona_app, name="persona")
console = Console()


def build_agent(
    settings: Settings | None = None,
) -> tuple[Settings, MemoryStore, TaskStore, Agent]:
    settings = settings or Settings()
    settings.ensure_data_dir()
    memory = MemoryStore(settings.database_path)
    task_store = TaskStore(settings.database_path)
    task_engine = TaskEngine(task_store)
    router = ModelRouter(settings)
    model = OpenAICompatibleModel(settings)
    persona = PersonaManager(settings)
    semantic_memory = MemoryEmbeddingStore(
        memory,
        model,
        model_name=settings.embedding_model or settings.model,
    )
    context_engine = ContextEngine(
        memory,
        task_engine,
        persona,
        semantic_store=semantic_memory,
        provider=model,
    )
    budget_manager = TokenBudgetManager(
        request_token_budget=settings.request_token_budget,
        task_token_budget=settings.task_token_budget,
        daily_token_budget=settings.daily_token_budget,
        input_cost_per_million=settings.input_cost_per_million,
        output_cost_per_million=settings.output_cost_per_million,
    )
    toolbox = Toolbox(
        settings,
        memory,
        vision_provider=(
            router.provider_for(ModelRole.VISION)
            if settings.vision_model.strip()
            else None
        ),
    )
    agent = Agent(
        settings,
        memory,
        model,
        toolbox=toolbox,
        task_engine=task_engine,
        model_router=router,
        planner=LLMPlanner(model),
        persona=persona,
        context_engine=context_engine,
        memory_extractor=MemoryExtractor(
            memory,
            provider=model,
            semantic_store=semantic_memory,
        ),
        budget_manager=budget_manager,
    )
    return settings, memory, task_store, agent


async def _ask_once(text: str) -> None:
    settings, memory, task_store, agent = build_agent()
    try:
        reply = await agent.ask(text)
        console.print(reply.text)
    finally:
        memory.close()
        task_store.close()


@app.command()
def ask(message: Annotated[str, typer.Argument(help="One-shot message for Jarvis.")]) -> None:
    """Send one message and print the response."""
    asyncio.run(_ask_once(message))


@app.command()
def train(
    paths: Annotated[
        list[Path],
        typer.Argument(help="Files or directories containing text for Jarvis to learn."),
    ],
    extensions: Annotated[
        str,
        typer.Option("--extensions", "-e", help="Comma-separated file extensions."),
    ] = ",".join(sorted(DEFAULT_TEXT_EXTENSIONS)),
    chunk_size: Annotated[
        int,
        typer.Option(
            "--chunk-size",
            min=200,
            max=8000,
            help="Maximum characters per memory chunk.",
        ),
    ] = 1200,
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Replace previously learned knowledge before importing."),
    ] = False,
) -> None:
    """Teach Jarvis durable knowledge from local text files."""
    settings = Settings()
    settings.ensure_data_dir()
    memory = MemoryStore(settings.database_path)
    try:
        report = learn_paths(
            memory,
            paths,
            normalize_extensions(extensions),
            chunk_size=chunk_size,
            max_bytes=settings.max_file_bytes,
            replace=replace,
        )
        if report.files_learned == 0:
            console.print("No readable text files found.")
            return
        console.print(
            f"Learned {report.files_learned} file(s), "
            f"stored {report.chunks_added} memory chunk(s), "
            f"skipped {report.files_skipped} file(s)."
        )
    finally:
        memory.close()


@app.command()
def chat() -> None:
    """Start an interactive text conversation."""
    settings, memory, task_store, agent = build_agent()
    session_id: str | None = None
    console.print(
        Panel.fit(
            f"[bold]{settings.assistant_name}[/bold] is ready. Type [bold]exit[/bold] to quit.",
            border_style="cyan",
        )
    )
    try:
        while True:
            try:
                text = console.input("[bold cyan]You > [/bold cyan]").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if text.lower() in {"exit", "quit", ":q"}:
                break
            if not text:
                continue
            with console.status("[dim]Thinking...[/dim]"):
                reply = asyncio.run(agent.ask(text, session_id=session_id))
            session_id = reply.session_id
            console.print(f"[bold cyan]{settings.assistant_name} >[/bold cyan] {reply.text}")
    finally:
        memory.close()
        task_store.close()


@app.command()
def voice() -> None:
    """Start the local microphone and speaker voice loop."""
    try:
        from jarvis.voice import run_voice
    except ImportError as exc:
        raise typer.BadParameter(
            'Voice dependencies are missing. Run: python -m uv sync --extra voice'
        ) from exc

    settings = Settings()
    asyncio.run(run_voice(settings))


@app.command()
def speak(
    text: Annotated[str | None, typer.Argument(help="Text for Jarvis to speak.")] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional WAV output path."),
    ] = None,
    no_play: Annotated[
        bool,
        typer.Option("--no-play", help="Only synthesize the WAV file."),
    ] = False,
    demo: Annotated[
        bool,
        typer.Option("--demo", help="Speak a built-in Chinese greeting."),
    ] = False,
) -> None:
    """Speak a piece of text through the local Piper voice."""
    message = text
    if demo:
        message = "你好，我是贾维斯。我现在可以听你说话，也可以开口回答你。"
    if not message:
        raise typer.BadParameter("Provide text or use --demo.")

    try:
        from jarvis.voice import play_wav, synthesize_text
    except ImportError as exc:
        raise typer.BadParameter(
            'Voice dependencies are missing. Run: python -m uv sync --extra voice'
        ) from exc

    settings = Settings()
    path = synthesize_text(settings, message, output)
    console.print(f"Audio: {path}")
    if not no_play:
        play_wav(path)


@app.command()
def bridge(
    host: Annotated[str, typer.Option(help="Bridge listen host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535, help="Bridge listen port.")] = 8765,
) -> None:
    """Run the local voice bridge used by the Ashley desktop UI."""
    try:
        from jarvis.bridge import run_bridge
    except ImportError as exc:
        raise typer.BadParameter(
            'Bridge dependencies are missing. Run: python -m uv sync --extra voice'
        ) from exc

    settings = Settings()
    run_bridge(settings, host, port)


@persona_app.command("show")
def persona_show() -> None:
    """Print the active persona profile."""
    settings = Settings()
    console.print_json(PersonaManager(settings).profile.model_dump_json())


@persona_app.command("import")
def persona_import(path: Path) -> None:
    """Import and persist a persona profile from JSON."""
    settings = Settings()
    profile = PersonaManager(settings).import_profile(path)
    console.print(
        f"Imported persona {profile.name} v{profile.version} "
        f"({profile.archetype}, mode={profile.default_mode})."
    )


@app.command()
def doctor() -> None:
    """Check model connectivity, storage, and optional voice dependencies."""
    settings = Settings()
    settings.ensure_data_dir()
    table = Table(title="Jarvis Doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")

    table.add_row("Data directory", "OK", str(settings.data_dir))
    table.add_row("Workspace", "OK", str(settings.workspace))

    try:
        memory = MemoryStore(settings.database_path)
        table.add_row("SQLite memory", "OK", str(settings.database_path))
        memory.close()
    except Exception as exc:
        table.add_row("SQLite memory", "FAIL", str(exc))

    base_url = settings.model_base_url.rstrip("/")
    health_url = f"{base_url}/models"
    try:
        response = httpx.get(
            health_url,
            headers={"Authorization": f"Bearer {settings.model_api_key.get_secret_value()}"},
            timeout=8.0,
        )
        if response.status_code < 400:
            table.add_row("Model endpoint", "OK", f"{settings.model} at {base_url}")
        else:
            table.add_row(
                "Model endpoint",
                "WARN",
                f"HTTP {response.status_code}; verify URL/model/provider",
            )
    except Exception as exc:
        table.add_row("Model endpoint", "FAIL", str(exc))

    if settings.vision_model.strip():
        vision_base_url = (
            settings.vision_base_url or settings.model_base_url
        ).rstrip("/")
        vision_key = (
            settings.vision_api_key.get_secret_value()
            or settings.model_api_key.get_secret_value()
        )
        try:
            response = httpx.get(
                f"{vision_base_url}/models",
                headers={"Authorization": f"Bearer {vision_key}"},
                timeout=8.0,
            )
            if response.status_code < 400:
                table.add_row(
                    "Vision endpoint",
                    "OK",
                    f"{settings.vision_model} at {vision_base_url}",
                )
            else:
                table.add_row(
                    "Vision endpoint",
                    "WARN",
                    f"HTTP {response.status_code}; verify vision provider",
                )
        except Exception as exc:
            table.add_row("Vision endpoint", "FAIL", str(exc))
    else:
        table.add_row(
            "Vision endpoint",
            "OPTIONAL",
            "set JARVIS_VISION_MODEL to enable screenshot analysis",
        )

    try:
        import importlib

        importlib.import_module("pipecat.transports.local.audio")
        importlib.import_module("pipecat.services.whisper.stt")
        importlib.import_module("pipecat.services.piper.tts")

        table.add_row("Pipecat voice", "OK", "voice dependencies installed")
    except Exception as exc:
        table.add_row(
            "Pipecat voice",
            "OPTIONAL",
            f"install with --extra voice ({type(exc).__name__})",
        )

    try:
        import pyaudio

        audio = pyaudio.PyAudio()
        try:
            input_name = audio.get_default_input_device_info().get("name", "unknown")
            output_name = audio.get_default_output_device_info().get("name", "unknown")
        finally:
            audio.terminate()
        table.add_row("Audio devices", "OK", f"in={input_name}; out={output_name}")
    except Exception as exc:
        table.add_row("Audio devices", "WARN", str(exc))

    console.print(table)


@memory_app.command("list")
def memory_list(
    limit: Annotated[int, typer.Option(min=1, max=500, help="Maximum rows.")] = 50,
) -> None:
    settings = Settings()
    memory = MemoryStore(settings.database_path)
    try:
        rows = memory.list_memories(limit)
        if not rows:
            console.print("No memories stored.")
            return
        for item in rows:
            console.print(f"[bold]#{item.id}[/bold] [{item.category}] {item.content}")
    finally:
        memory.close()


@memory_app.command("search")
def memory_search(query: str, limit: int = 20) -> None:
    settings = Settings()
    memory = MemoryStore(settings.database_path)
    try:
        rows = memory.recall(query, limit)
        if not rows:
            console.print("No matching memories.")
            return
        for item in rows:
            console.print(f"[bold]#{item.id}[/bold] [{item.category}] {item.content}")
    finally:
        memory.close()


@memory_app.command("forget")
def memory_forget(memory_id: int) -> None:
    settings = Settings()
    memory = MemoryStore(settings.database_path)
    try:
        if memory.forget(memory_id):
            console.print(f"Deleted memory #{memory_id}.")
        else:
            console.print(f"No memory #{memory_id}.")
    finally:
        memory.close()


@memory_app.command("clear")
def memory_clear(
    force: Annotated[bool, typer.Option("--force", help="Skip confirmation.")] = False,
) -> None:
    if not force:
        typer.confirm("Delete all durable memories?", abort=True)
    settings = Settings()
    memory = MemoryStore(settings.database_path)
    try:
        count = memory.clear_memories()
        console.print(f"Deleted {count} memories.")
    finally:
        memory.close()


@task_app.command("list")
def task_list(
    limit: Annotated[int, typer.Option(min=1, max=500, help="Maximum rows.")] = 50,
) -> None:
    settings = Settings()
    store = TaskStore(settings.database_path)
    try:
        tasks = store.list(limit)
        if not tasks:
            console.print("No tasks stored.")
            return
        for task in tasks:
            console.print(
                f"[bold]{task.id}[/bold] [{task.status}] "
                f"{task.progress:.0%} {task.goal}"
            )
    finally:
        store.close()


@task_app.command("show")
def task_show(task_id: str) -> None:
    settings = Settings()
    store = TaskStore(settings.database_path)
    try:
        task = store.get(task_id)
        if task is None:
            console.print(f"No task {task_id}.")
            return
        console.print(f"[bold]{task.goal}[/bold]")
        console.print(f"Status: {task.status}")
        console.print(f"Progress: {task.progress:.0%}")
        for index, step in enumerate(task.plan, start=1):
            console.print(f"{index:02d}. [{step.status}] {step.title} ({step.tool})")
    finally:
        store.close()


@task_app.command("cancel")
def task_cancel(task_id: str) -> None:
    settings = Settings()
    store = TaskStore(settings.database_path)
    engine = TaskEngine(store)
    try:
        task = engine.get_task(task_id)
        if task is None:
            console.print(f"No task {task_id}.")
            return
        asyncio.run(engine.cancel_task(task_id))
        console.print(f"Cancelled task {task_id}.")
    finally:
        store.close()


@task_app.command("resume")
def task_resume(task_id: str) -> None:
    settings = Settings()
    store = TaskStore(settings.database_path)
    engine = TaskEngine(store)
    try:
        task = engine.get_task(task_id)
        if task is None:
            console.print(f"No task {task_id}.")
            return
        asyncio.run(engine.resume_task(task_id))
        console.print(f"Resumed task {task_id}.")
    finally:
        store.close()


@task_app.command("retry")
def task_retry(task_id: str) -> None:
    settings = Settings()
    store = TaskStore(settings.database_path)
    engine = TaskEngine(store)
    try:
        task = engine.get_task(task_id)
        if task is None:
            console.print(f"No task {task_id}.")
            return
        asyncio.run(engine.retry_task(task_id))
        console.print(f"Retry queued for task {task_id}.")
    finally:
        store.close()


@task_app.command("continue")
def task_continue(task_id: str) -> None:
    settings, memory, task_store, agent = build_agent()
    try:
        reply = asyncio.run(agent.continue_task(task_id))
        console.print(reply.text)
    finally:
        memory.close()
        task_store.close()


@permission_app.command("show")
def permission_show() -> None:
    policy = permission_policy_from_settings(Settings())
    auto_approve = ", ".join(sorted(level.value for level in policy.auto_approve))
    console.print(f"Auto approve: {auto_approve}")
    console.print(f"Allowed apps: {', '.join(policy.allowed_apps) or '(all)'}")
    console.print(f"Allowed folders: {', '.join(policy.allowed_folders) or '(all)'}")
    console.print(f"Allowed commands: {', '.join(policy.allowed_commands) or '(all)'}")
    console.print(f"Blocked apps: {', '.join(policy.blocked_apps) or '(none)'}")
    console.print(f"Blocked paths: {', '.join(policy.blocked_paths) or '(none)'}")
    console.print(f"Blocked commands: {', '.join(policy.blocked_commands) or '(none)'}")


@mcp_app.command("list")
def mcp_list() -> None:
    configs = mcp_server_configs_from_settings(Settings())
    if not configs:
        console.print("No MCP servers configured.")
        return
    for config in configs:
        console.print(f"{config.name}: {' '.join(config.command)}")


@vision_app.command("analyze")
def vision_analyze(
    path: Annotated[Path, typer.Argument(help="Local image file to analyze.")],
    prompt: Annotated[
        str,
        typer.Option("--prompt", "-p", help="Question or instruction for the vision model."),
    ] = "Describe the image and identify important details.",
) -> None:
    settings = Settings()
    if not settings.vision_model.strip():
        raise typer.BadParameter("JARVIS_VISION_MODEL is not configured.")
    image_path = path.expanduser().resolve()
    if not image_path.is_file():
        raise typer.BadParameter(f"Image not found: {image_path}")
    provider = ModelRouter(settings).provider_for(ModelRole.VISION)
    result = asyncio.run(
        VisionAnalyzer(provider).describe(image_path.read_bytes(), prompt)
    )
    console.print(result)


if __name__ == "__main__":
    app()
