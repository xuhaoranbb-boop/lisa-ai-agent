from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jarvis.runtime.contracts import PermissionLevel
from jarvis.runtime.events import EventBus, EventType, RuntimeEvent
from jarvis.runtime.permissions import PermissionManager


@dataclass(frozen=True)
class DesktopObservation:
    windows: list[dict[str, Any]]
    screenshot: bytes | None = None


class DesktopAdapter(Protocol):
    async def screenshot(self) -> bytes: ...
    async def window_list(self) -> list[dict[str, Any]]: ...
    async def foreground_window(self) -> dict[str, Any]: ...
    async def focused_value(self) -> str: ...
    async def ui_tree(self, max_items: int = 120) -> list[dict[str, Any]]: ...
    async def ui_invoke(
        self,
        *,
        name: str | None = None,
        automation_id: str | None = None,
    ) -> None: ...
    async def find_and_click(
        self,
        *,
        name: str | None = None,
        automation_id: str | None = None,
    ) -> dict[str, Any]: ...
    async def window_focus(self, target: str | int) -> None: ...
    async def app_open(self, application: str) -> None: ...
    async def app_close(self, application: str) -> None: ...
    async def mouse_move(self, x: int, y: int) -> None: ...
    async def mouse_click(self, x: int | None = None, y: int | None = None) -> None: ...
    async def mouse_double_click(self, x: int | None = None, y: int | None = None) -> None: ...
    async def keyboard_type(self, text: str) -> None: ...
    async def keyboard_press(self, key: str) -> None: ...
    async def keyboard_hotkey(self, keys: list[str]) -> None: ...
    async def clipboard_read(self) -> str: ...
    async def clipboard_write(self, text: str) -> None: ...
    async def scroll(self, delta: int) -> None: ...


class WindowsDesktopAdapter:
    """Windows desktop adapter using Win32 calls and PowerShell bridges."""

    async def screenshot(self) -> bytes:
        output = Path(os.environ.get("TEMP", ".")) / f"jarvis-screenshot-{time.time_ns()}.png"
        escaped = str(output).replace("'", "''")
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bitmap.Save('{escaped}', [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()
"""
        await self._run_powershell(script)
        try:
            return output.read_bytes()
        finally:
            output.unlink(missing_ok=True)

    async def window_list(self) -> list[dict[str, Any]]:
        script = (
            "Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | "
            "Select-Object Id,ProcessName,MainWindowTitle,MainWindowHandle | "
            "ConvertTo-Json -Compress"
        )
        stdout = await self._run_powershell(script)
        if not stdout.strip():
            return []
        parsed = json.loads(stdout)
        if isinstance(parsed, dict):
            return [parsed]
        return list(parsed)

    async def foreground_window(self) -> dict[str, Any]:
        script = """
Add-Type -ErrorAction SilentlyContinue @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class JarvisForegroundWindow {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern int GetWindowText(
        IntPtr hWnd, StringBuilder text, int count);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(
        IntPtr hWnd, out uint processId);
}
"@
$hwnd = [JarvisForegroundWindow]::GetForegroundWindow()
$text = New-Object System.Text.StringBuilder 512
[JarvisForegroundWindow]::GetWindowText($hwnd, $text, $text.Capacity) | Out-Null
$processId = 0
[JarvisForegroundWindow]::GetWindowThreadProcessId($hwnd, [ref]$processId) | Out-Null
$process = Get-Process -Id $processId -ErrorAction SilentlyContinue
[PSCustomObject]@{
    title = $text.ToString()
    process_name = if ($process) { $process.ProcessName } else { "" }
    handle = [int64]$hwnd
} | ConvertTo-Json -Compress
"""
        stdout = await self._run_powershell(script)
        if not stdout.strip():
            return {}
        parsed = json.loads(stdout)
        return parsed if isinstance(parsed, dict) else {}

    async def focused_value(self) -> str:
        script = """
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$focused = [System.Windows.Automation.AutomationElement]::FocusedElement
if ($null -eq $focused) { exit }
$pattern = $null
if ($focused.TryGetCurrentPattern(
    [System.Windows.Automation.ValuePattern]::Pattern,
    [ref]$pattern
)) {
    $pattern.Current.Value
}
"""
        return (await self._run_powershell(script)).strip()

    async def ui_tree(self, max_items: int = 120) -> list[dict[str, Any]]:
        script = f"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -ErrorAction SilentlyContinue @"
using System;
using System.Runtime.InteropServices;
public static class JarvisForegroundNative {{
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}}
"@
$hwnd = [JarvisForegroundNative]::GetForegroundWindow()
try {{
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
}} catch {{
    $root = [System.Windows.Automation.AutomationElement]::RootElement
}}
$elements = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
)
$items = @()
$limit = [Math]::Min({max(1, min(max_items, 500))}, $elements.Count)
for ($index = 0; $index -lt $limit; $index++) {{
    $element = $elements.Item($index)
    $current = $element.Current
    $bounds = $current.BoundingRectangle
    $items += [PSCustomObject]@{{
        name = $current.Name
        automationId = $current.AutomationId
        controlType = $current.ControlType.ProgrammaticName
        className = $current.ClassName
        enabled = $current.IsEnabled
        offscreen = $current.IsOffscreen
        bounds = [PSCustomObject]@{{
            x = $bounds.X
            y = $bounds.Y
            width = $bounds.Width
            height = $bounds.Height
        }}
    }}
}}
$items | ConvertTo-Json -Depth 4 -Compress
"""
        stdout = await self._run_powershell(script)
        if not stdout.strip():
            return []
        parsed = json.loads(stdout)
        if isinstance(parsed, dict):
            return [parsed]
        return list(parsed)

    async def ui_invoke(
        self,
        *,
        name: str | None = None,
        automation_id: str | None = None,
    ) -> None:
        if not name and not automation_id:
            raise ValueError("name or automation_id is required.")
        if automation_id:
            escaped = automation_id.replace("'", "''")
            condition = (
                "$condition = New-Object System.Windows.Automation.PropertyCondition("
                "[System.Windows.Automation.AutomationElement]::AutomationIdProperty, "
                f"'{escaped}')"
            )
        else:
            escaped = str(name).replace("'", "''")
            condition = (
                "$condition = New-Object System.Windows.Automation.PropertyCondition("
                "[System.Windows.Automation.AutomationElement]::NameProperty, "
                f"'{escaped}')"
            )
        script = f"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
{condition}
$root = [System.Windows.Automation.AutomationElement]::RootElement
$element = $root.FindFirst(
    [System.Windows.Automation.TreeScope]::Descendants,
    $condition
)
if ($null -eq $element) {{ throw 'UI element not found.' }}
$pattern = $null
if (-not $element.TryGetCurrentPattern(
    [System.Windows.Automation.InvokePattern]::Pattern,
    [ref]$pattern
)) {{ throw 'InvokePattern is unavailable for this element.' }}
$pattern.Invoke()
"""
        await self._run_powershell(script)

    async def find_and_click(
        self,
        *,
        name: str | None = None,
        automation_id: str | None = None,
    ) -> dict[str, Any]:
        if not name and not automation_id:
            raise ValueError("name or automation_id is required.")
        target = (automation_id or name or "").lower()
        tree = await self.ui_tree(max_items=500)
        match = next(
            (
                item
                for item in tree
                if (
                    automation_id
                    and str(item.get("automationId") or "").lower() == target
                )
                or (
                    name
                    and str(item.get("name") or "").lower() == target
                )
            ),
            None,
        )
        if match is None:
            raise LookupError(f"UI element not found: {automation_id or name}")
        bounds = match.get("bounds") or {}
        x = int(bounds.get("x", 0)) + int(bounds.get("width", 0)) // 2
        y = int(bounds.get("y", 0)) + int(bounds.get("height", 0)) // 2
        try:
            await self.ui_invoke(name=name, automation_id=automation_id)
            return {
                "method": "uia",
                "name": match.get("name"),
                "automation_id": match.get("automationId"),
                "bounds": bounds,
            }
        except Exception:
            await self.mouse_click(x, y)
            return {
                "method": "mouse_fallback",
                "name": match.get("name"),
                "automation_id": match.get("automationId"),
                "x": x,
                "y": y,
                "bounds": bounds,
            }

    async def window_focus(self, target: str | int) -> None:
        await asyncio.to_thread(_focus_window_win32, target)

    async def app_open(self, application: str) -> None:
        if os.name != "nt":
            await asyncio.create_subprocess_exec(application)
            return

        candidates = _windows_app_candidates(application)
        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                if resolved.lower().endswith((".cmd", ".bat")):
                    subprocess.Popen(["cmd.exe", "/c", resolved])
                else:
                    subprocess.Popen([resolved])
                return
            if Path(candidate).expanduser().exists():
                os.startfile(str(Path(candidate).expanduser()))  # type: ignore[attr-defined]
                return
        shortcut = _find_start_menu_shortcut(application)
        if shortcut is not None:
            os.startfile(str(shortcut))  # type: ignore[attr-defined]
            return
        try:
            os.startfile(application)  # type: ignore[attr-defined]
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"Application not found: {application}. "
                f"Tried: {', '.join(candidates)}"
            ) from exc

    async def app_close(self, application: str) -> None:
        if os.name != "nt":
            raise NotImplementedError("app_close is currently implemented on Windows only.")
        candidates = _windows_app_candidates(application)
        process_name = next(
            (candidate for candidate in candidates if candidate.lower().endswith(".exe")),
            f"{application}.exe",
        )
        completed = await asyncio.create_subprocess_exec(
            "taskkill",
            "/IM",
            process_name,
            "/T",
            "/F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await completed.wait()
        if completed.returncode != 0:
            raise RuntimeError(f"Unable to close application: {application}")

    async def mouse_move(self, x: int, y: int) -> None:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))

    async def mouse_click(self, x: int | None = None, y: int | None = None) -> None:
        if x is not None and y is not None:
            await self.mouse_move(x, y)
        user32 = ctypes.windll.user32
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)

    async def mouse_double_click(self, x: int | None = None, y: int | None = None) -> None:
        await self.mouse_click(x, y)
        await self.mouse_click()

    async def keyboard_type(self, text: str) -> None:
        for character in text:
            if character == "\n":
                await self.keyboard_press("enter")
                continue
            code = ctypes.windll.user32.VkKeyScanW(ord(character))
            if code == -1:
                continue
            virtual_key = code & 0xFF
            shift = bool(code >> 8 & 1)
            if shift:
                ctypes.windll.user32.keybd_event(0x10, 0, 0, 0)
            ctypes.windll.user32.keybd_event(virtual_key, 0, 0, 0)
            ctypes.windll.user32.keybd_event(virtual_key, 0, 0x0002, 0)
            if shift:
                ctypes.windll.user32.keybd_event(0x10, 0, 0x0002, 0)

    async def keyboard_press(self, key: str) -> None:
        mapping = {
            "enter": 0x0D,
            "tab": 0x09,
            "escape": 0x1B,
            "space": 0x20,
            "ctrl": 0x11,
            "alt": 0x12,
            "shift": 0x10,
            "win": 0x5B,
            "backspace": 0x08,
            "delete": 0x2E,
        }
        normalized = key.lower()
        virtual_key = mapping.get(normalized)
        if virtual_key is None and len(key) == 1:
            virtual_key = ord(key.upper())
        if virtual_key is None:
            raise ValueError(f"Unsupported key: {key}")
        ctypes.windll.user32.keybd_event(virtual_key, 0, 0, 0)
        ctypes.windll.user32.keybd_event(virtual_key, 0, 0x0002, 0)

    async def keyboard_hotkey(self, keys: list[str]) -> None:
        if len(keys) < 2:
            raise ValueError("A hotkey requires at least two keys.")
        mapping = {
            "ctrl": 0x11,
            "control": 0x11,
            "alt": 0x12,
            "shift": 0x10,
            "win": 0x5B,
            "enter": 0x0D,
            "tab": 0x09,
            "escape": 0x1B,
            "space": 0x20,
        }
        virtual_keys: list[int] = []
        for key in keys:
            normalized = key.lower()
            virtual_key = mapping.get(normalized)
            if virtual_key is None and len(key) == 1:
                virtual_key = ord(key.upper())
            if virtual_key is None:
                raise ValueError(f"Unsupported hotkey key: {key}")
            virtual_keys.append(virtual_key)
        for virtual_key in virtual_keys:
            ctypes.windll.user32.keybd_event(virtual_key, 0, 0, 0)
        for virtual_key in reversed(virtual_keys):
            ctypes.windll.user32.keybd_event(virtual_key, 0, 0x0002, 0)

    async def clipboard_read(self) -> str:
        if os.name != "nt":
            raise NotImplementedError("Clipboard access is Windows-only in this adapter.")
        return await self._run_powershell("Get-Clipboard -Raw")

    async def clipboard_write(self, text: str) -> None:
        if os.name != "nt":
            raise NotImplementedError("Clipboard access is Windows-only in this adapter.")
        escaped = text.replace("'", "''")
        await self._run_powershell(f"Set-Clipboard -Value '{escaped}'")

    async def scroll(self, delta: int) -> None:
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, int(delta), 0)

    @staticmethod
    async def _run_powershell(script: str) -> str:
        # 中文 Windows 默认按 GBK 输出；先切到 UTF-8 再执行脚本，避免中文乱码。
        preamble = (
            "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}; "
            "try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}; "
        )
        completed = await asyncio.to_thread(
            subprocess.run,
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                preamble + script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "PowerShell command failed.")
        return completed.stdout.strip()


class DesktopController:
    """Permission-gated desktop actions with runtime events."""

    def __init__(
        self,
        adapter: DesktopAdapter | None = None,
        permission_manager: PermissionManager | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.adapter = adapter or WindowsDesktopAdapter()
        self.permission_manager = permission_manager or PermissionManager()
        self.event_bus = event_bus or EventBus()

    async def observe(self, *, include_screenshot: bool = False) -> DesktopObservation:
        windows = await self.adapter.window_list()
        image = None
        if include_screenshot:
            try:
                image = await self.adapter.screenshot()
            except (FileNotFoundError, OSError, RuntimeError):
                image = None
        return DesktopObservation(windows=windows, screenshot=image)

    async def execute(
        self,
        action: str,
        arguments: dict[str, Any] | None = None,
        *,
        enforce_permission: bool = True,
    ) -> Any:
        arguments = arguments or {}
        if enforce_permission:
            level, resource = self._classify(action, arguments)
            await self.permission_manager.require(
                level,
                action,
                resource,
                metadata={
                    "resource_type": self._resource_type(action),
                    "category": "desktop",
                },
            )
        await self.event_bus.emit_async(
            RuntimeEvent(
                type=EventType.TOOL_START,
                data={"tool": f"desktop.{action}", "arguments": arguments},
            )
        )
        before_tree: list[dict[str, Any]] | None = None
        if action in {
            "ui_invoke",
            "find_and_click",
            "mouse_click",
            "mouse_double_click",
            "keyboard_press",
            "keyboard_hotkey",
            "scroll",
        }:
            tree_method = getattr(self.adapter, "ui_tree", None)
            try:
                before_tree = (
                    await tree_method(max_items=200)
                    if callable(tree_method)
                    else None
                )
            except Exception:
                before_tree = None
        try:
            method = getattr(self.adapter, action)
            result = await method(**arguments)
            verified, reason = await self._verify_action(
                action,
                arguments,
                result,
                before_tree,
            )
            if not verified:
                error = f"Desktop verification failed for {action}: {reason}"
                await self.event_bus.emit_async(
                    RuntimeEvent(
                        type=EventType.TOOL_ERROR,
                        data={
                            "tool": f"desktop.{action}",
                            "error": error,
                            "verification": {"ok": False, "reason": reason},
                        },
                    )
                )
                raise RuntimeError(error)
        except Exception as exc:
            await self.event_bus.emit_async(
                RuntimeEvent(
                    type=EventType.TOOL_ERROR,
                    data={"tool": f"desktop.{action}", "error": str(exc)},
                )
            )
            raise
        await self.event_bus.emit_async(
            RuntimeEvent(
                type=EventType.TOOL_RESULT,
                data={
                    "tool": f"desktop.{action}",
                    "result": result,
                    "verification": {"ok": True, "reason": reason},
                },
            )
        )
        return result

    async def _verify_action(
        self,
        action: str,
        arguments: dict[str, Any],
        result: Any,
        before_tree: list[dict[str, Any]] | None,
    ) -> tuple[bool, str]:
        if action == "screenshot":
            return bool(result), f"screenshot bytes={len(result) if result else 0}"
        if action == "app_open":
            return await self._wait_for_application(
                str(arguments.get("application") or ""),
                present=True,
            )
        if action == "app_close":
            return await self._wait_for_application(
                str(arguments.get("application") or ""),
                present=False,
            )
        if action == "window_focus":
            target = str(arguments.get("target") or "").strip().lower()
            foreground_method = getattr(self.adapter, "foreground_window", None)
            foreground = (
                await foreground_method()
                if callable(foreground_method)
                else {}
            )
            title = str(foreground.get("title") or "").lower()
            process = str(foreground.get("process_name") or "").lower()
            ok = bool(target) and (target in title or target in process)
            return ok, f"foreground title={title!r} process={process!r}"
        if action == "clipboard_write":
            expected = str(arguments.get("text") or "")
            actual = await self.adapter.clipboard_read()
            return actual == expected, "clipboard contents match"
        if action == "keyboard_type":
            expected = str(arguments.get("text") or "")
            focused_method = getattr(self.adapter, "focused_value", None)
            try:
                actual = (
                    await focused_method()
                    if callable(focused_method)
                    else ""
                )
            except Exception:
                actual = ""
            if actual:
                normalized_expected = _normalize_typed_text(expected)
                normalized_actual = _normalize_typed_text(actual)
                matched = (
                    actual == expected
                    or normalized_expected in normalized_actual
                    or normalized_actual in normalized_expected
                )
                return matched, "focused input value matches"
            foreground_method = getattr(self.adapter, "foreground_window", None)
            foreground = (
                await foreground_method()
                if callable(foreground_method)
                else {}
            )
            return bool(foreground.get("title") or foreground.get("process_name")), (
                "focused value unavailable; foreground window present"
            )
        if before_tree is not None:
            tree_method = getattr(self.adapter, "ui_tree", None)
            try:
                after_tree = (
                    await tree_method(max_items=200)
                    if callable(tree_method)
                    else []
                )
            except Exception:
                after_tree = []
            changed = _tree_fingerprint(before_tree) != _tree_fingerprint(after_tree)
            return True, "ui state changed" if changed else "no observable UI change"
        return True, "no verification required"

    async def _wait_for_application(
        self,
        application: str,
        *,
        present: bool,
        timeout_seconds: float = 15.0,
    ) -> tuple[bool, str]:
        deadline = time.monotonic() + max(0.5, timeout_seconds)
        last_count = 0
        while time.monotonic() < deadline:
            windows = await self.adapter.window_list()
            matching = [
                window
                for window in windows
                if _application_matches(window, application)
            ]
            last_count = len(matching)
            if bool(matching) == present:
                return True, f"matching windows={last_count}"
            await asyncio.sleep(0.25)
        return False, f"matching windows={last_count}, expected present={present}"

    @staticmethod
    def _resource_type(action: str) -> str:
        if action.startswith("app_"):
            return "app"
        if action in {"screenshot", "window_list"}:
            return "screen"
        return "input"

    @staticmethod
    def _classify(action: str, arguments: dict[str, Any]) -> tuple[PermissionLevel, str | None]:
        if action in {"screenshot", "window_list"}:
            return PermissionLevel.READ_ONLY, None
        if action == "ui_tree":
            return PermissionLevel.READ_ONLY, None
        if action == "ui_invoke":
            return PermissionLevel.NORMAL, str(
                arguments.get("name") or arguments.get("automation_id") or ""
            )
        if action == "find_and_click":
            return PermissionLevel.HIGH_RISK, str(
                arguments.get("name") or arguments.get("automation_id") or ""
            )
        if action == "window_focus":
            return PermissionLevel.LOW_RISK, str(arguments.get("target") or "")
        if action == "app_open":
            return PermissionLevel.LOW_RISK, str(arguments.get("application") or "")
        if action == "app_close":
            return PermissionLevel.HIGH_RISK, str(arguments.get("application") or "")
        if action in {"mouse_move", "mouse_click", "mouse_double_click", "scroll"}:
            return PermissionLevel.NORMAL, None
        if action in {"keyboard_type", "keyboard_press"}:
            return (
                PermissionLevel.MEDIUM_RISK,
                str(arguments.get("text") or arguments.get("key") or ""),
            )
        if action == "keyboard_hotkey":
            return PermissionLevel.MEDIUM_RISK, ",".join(arguments.get("keys") or [])
        if action == "clipboard_read":
            return PermissionLevel.READ_ONLY, None
        if action == "clipboard_write":
            return PermissionLevel.MEDIUM_RISK, None
        return PermissionLevel.MEDIUM_RISK, None


class ComputerAgentLoop:
    """Bounded observe-plan-act-observe-verify loop."""

    def __init__(
        self,
        controller: DesktopController,
        *,
        max_iterations: int = 8,
        event_bus: EventBus | None = None,
    ) -> None:
        self.controller = controller
        self.max_iterations = max(1, max_iterations)
        self.event_bus = event_bus or controller.event_bus

    async def run(
        self,
        goal: str,
        planner: Any,
        verifier: Any | None = None,
    ) -> dict[str, Any]:
        history: list[dict[str, Any]] = []
        for iteration in range(self.max_iterations):
            observation = await self.controller.observe(include_screenshot=True)
            action = await planner(goal, observation, history)
            if not action:
                break
            result = await self.controller.execute(
                str(action["action"]),
                dict(action.get("arguments") or {}),
            )
            history.append({"action": action, "result": result})
            if verifier is not None and await verifier(goal, observation, history):
                return {"ok": True, "iterations": iteration + 1, "history": history}
        return {
            "ok": False,
            "iterations": len(history),
            "history": history,
            "error": "Computer loop reached its iteration limit.",
        }


def _windows_app_candidates(application: str) -> list[str]:
    normalized = application.strip().lower()
    aliases: dict[str, list[str]] = {
        "微信": [
            "Weixin.exe",
            "WeChat.exe",
            os.path.expandvars(r"%ProgramFiles%\Tencent\Weixin\Weixin.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Tencent\Weixin\Weixin.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Tencent\WeChat\WeChat.exe"),
            os.path.expandvars(r"%APPDATA%\Tencent\WeChat\WeChat.exe"),
        ],
        "wechat": [
            "Weixin.exe",
            "WeChat.exe",
            os.path.expandvars(r"%ProgramFiles%\Tencent\Weixin\Weixin.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Tencent\Weixin\Weixin.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Tencent\WeChat\WeChat.exe"),
            os.path.expandvars(r"%APPDATA%\Tencent\WeChat\WeChat.exe"),
        ],
        "weixin": [
            "Weixin.exe",
            "WeChat.exe",
            os.path.expandvars(r"%ProgramFiles%\Tencent\Weixin\Weixin.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Tencent\Weixin\Weixin.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Tencent\WeChat\WeChat.exe"),
            os.path.expandvars(r"%APPDATA%\Tencent\WeChat\WeChat.exe"),
        ],
        "vs code": [
            "Code.exe",
            "code.cmd",
            "code",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft VS Code\Code.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe"),
        ],
        "vscode": [
            "Code.exe",
            "code.cmd",
            "code",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft VS Code\Code.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe"),
        ],
        "visual studio code": [
            "Code.exe",
            "code.cmd",
            "code",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft VS Code\Code.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe"),
        ],
        "code": [
            "Code.exe",
            "code.cmd",
            "code",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft VS Code\Code.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe"),
        ],
        "notepad": ["notepad.exe"],
        "记事本": ["notepad.exe"],
        "calc": ["calc.exe", "CalculatorApp.exe", "计算器"],
        "calculator": ["calc.exe", "CalculatorApp.exe", "计算器"],
        "计算器": ["calc.exe", "CalculatorApp.exe", "计算器"],
        "edge": ["msedge.exe"],
        "microsoft edge": ["msedge.exe"],
        "chrome": ["chrome.exe"],
        "google chrome": ["chrome.exe"],
        "explorer": ["explorer.exe"],
        "file explorer": ["explorer.exe"],
        "文件资源管理器": ["explorer.exe"],
        "cmd": ["cmd.exe"],
        "command prompt": ["cmd.exe"],
        "powershell": ["powershell.exe"],
    }
    candidates = aliases.get(normalized, [])
    if application not in candidates:
        candidates.append(application)
    return candidates


def _normalize_typed_text(value: str) -> str:
    """Normalize text for verification: unify math symbols and strip spaces."""
    return (
        value.replace("×", "*")
        .replace("÷", "/")
        .replace("−", "-")
        .replace("–", "-")
        .replace("\u00a0", "")
        .replace(" ", "")
    )


def _focus_window_win32(target: str | int) -> None:
    """Focus a visible window by title/process name (or PID) using Win32 APIs."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    requested = str(target).strip().lower()
    if not requested:
        raise ValueError("window_focus requires a target.")
    requested_pid = int(requested) if requested.isdigit() else None
    process_query_limited = 0x1000

    def _process_name(hwnd: int) -> str:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = kernel32.OpenProcess(process_query_limited, False, pid.value)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return Path(buffer.value).stem.lower()
        finally:
            kernel32.CloseHandle(handle)
        return ""

    matched: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _enum_window(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title_buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, length + 1)
        title = title_buffer.value.strip().lower()
        if not title:
            return True
        if requested_pid is not None:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == requested_pid:
                matched.append(hwnd)
            return True
        if requested in title or requested in _process_name(hwnd):
            matched.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(_enum_window), 0)
    if not matched:
        raise RuntimeError(f"No visible window matches: {target}")
    # 优先选择无属主的顶层窗口（可在任务栏/Alt-Tab 并可以置前）
    gw_owner = 4
    hwnd = next(
        (
            candidate
            for candidate in matched
            if not user32.GetWindow(candidate, gw_owner)
        ),
        matched[0],
    )
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE for minimized windows
    if not user32.SetForegroundWindow(hwnd):
        user32.ShowWindow(hwnd, 9)
        if not user32.SetForegroundWindow(hwnd):
            # 后台进程调用 SetForegroundWindow 常被系统限制：
            # 先附加到前台窗口线程，再置前（标准变通方案）
            foreground = user32.GetForegroundWindow()
            target_thread = user32.GetWindowThreadProcessId(hwnd, None)
            current_thread = kernel32.GetCurrentThreadId()
            foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
            attached: list[int] = []
            for thread_id in {foreground_thread, target_thread}:
                if thread_id and thread_id != current_thread:
                    user32.AttachThreadInput(current_thread, thread_id, True)
                    attached.append(thread_id)
            try:
                user32.ShowWindow(hwnd, 9)
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            finally:
                for thread_id in attached:
                    user32.AttachThreadInput(current_thread, thread_id, False)
            if user32.GetForegroundWindow() != hwnd:
                raise RuntimeError(
                    f"Could not bring window to the foreground: {target}"
                )


def _find_start_menu_shortcut(application: str) -> Path | None:
    """Find a Start Menu shortcut matching the request, preferring precise names.

    Shortcuts that uninstall or repair software are never returned.
    """
    requested = application.strip().lower()
    if not requested or "/" in requested or "\\" in requested:
        return None
    blocked = ("卸载", "uninstall", "删除", "修复", "帮助", "readme")
    roots = (
        Path(os.environ.get("APPDATA", ""))
        / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("ProgramData", ""))
        / "Microsoft/Windows/Start Menu/Programs",
    )
    prefix_match: Path | None = None
    contains_match: Path | None = None
    for root in roots:
        if not root.is_dir():
            continue
        for shortcut in root.rglob("*.lnk"):
            stem = shortcut.stem.lower()
            if any(token in stem for token in blocked):
                continue
            if stem == requested:
                return shortcut
            if prefix_match is None and stem.startswith(requested):
                prefix_match = shortcut
            elif contains_match is None and requested in stem:
                contains_match = shortcut
    return prefix_match or contains_match


def _application_matches(window: dict[str, Any], application: str) -> bool:
    requested = application.strip().lower()
    if not requested:
        return False
    title = str(
        window.get("MainWindowTitle")
        or window.get("title")
        or ""
    ).lower()
    process = str(
        window.get("ProcessName")
        or window.get("process_name")
        or ""
    ).lower()
    for candidate in _windows_app_candidates(application):
        normalized = candidate.strip().lower()
        if not normalized:
            continue
        stem = normalized.removesuffix(".exe")
        if normalized in title or stem in title or stem in process:
            return True
    return requested in title or requested in process


def _tree_fingerprint(items: list[dict[str, Any]]) -> str:
    return json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
