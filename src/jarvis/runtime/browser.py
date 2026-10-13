from __future__ import annotations

import asyncio
import webbrowser
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote_plus

import httpx

from jarvis.runtime.contracts import PermissionLevel, ToolSpec
from jarvis.runtime.tooling import RuntimeTool, ToolRegistry


@dataclass(frozen=True)
class BrowserObservation:
    url: str
    title: str
    text: str
    status: int = 0


class BrowserAdapter(Protocol):
    async def open(self, url: str) -> BrowserObservation: ...
    async def read(self, url: str) -> BrowserObservation: ...
    async def click(self, selector: str) -> BrowserObservation: ...
    async def type_text(self, selector: str, text: str) -> BrowserObservation: ...
    async def scroll(self, delta: int) -> BrowserObservation: ...
    async def extract(self, selector: str = "body") -> BrowserObservation: ...
    async def close(self) -> None: ...


class PlaywrightBrowserAdapter:
    """Optional Playwright adapter. It uses Edge when available."""

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_page(self) -> Any:
        async with self._lock:
            if self._page is not None:
                return self._page
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "Playwright is not installed. Install the browser extra."
                ) from exc
            self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(
                    channel="msedge",
                    headless=self._headless,
                )
            except Exception:
                self._browser = await self._playwright.chromium.launch(
                    headless=self._headless
                )
            self._page = await self._browser.new_page()
            return self._page

    async def open(self, url: str) -> BrowserObservation:
        page = await self._ensure_page()
        response = await page.goto(url, wait_until="domcontentloaded")
        return await self._observation(page, response.status if response else 0)

    async def read(self, url: str) -> BrowserObservation:
        page = await self._ensure_page()
        response = await page.goto(url, wait_until="domcontentloaded")
        return await self._observation(page, response.status if response else 0)

    async def click(self, selector: str) -> BrowserObservation:
        page = await self._ensure_page()
        await page.locator(selector).first.click()
        await page.wait_for_load_state("domcontentloaded")
        return await self._observation(page)

    async def type_text(self, selector: str, text: str) -> BrowserObservation:
        page = await self._ensure_page()
        await page.locator(selector).first.fill(text)
        return await self._observation(page)

    async def scroll(self, delta: int) -> BrowserObservation:
        page = await self._ensure_page()
        await page.mouse.wheel(0, int(delta))
        return await self._observation(page)

    async def extract(self, selector: str = "body") -> BrowserObservation:
        page = await self._ensure_page()
        text = await page.locator(selector).first.inner_text()
        return BrowserObservation(
            url=str(page.url),
            title=str(await page.title()),
            text=text,
        )

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._page = None

    @staticmethod
    async def _observation(page: Any, status: int = 0) -> BrowserObservation:
        return BrowserObservation(
            url=str(page.url),
            title=str(await page.title()),
            text=str(await page.locator("body").inner_text())[:20_000],
            status=status,
        )


class BrowserTool:
    """Browser operations exposed through the central ToolRegistry."""

    def __init__(self, adapter: BrowserAdapter | None = None) -> None:
        self.adapter = adapter or PlaywrightBrowserAdapter()

    async def open(self, url: str) -> dict[str, Any]:
        observation = await self.adapter.open(url)
        return _observation_dict(observation)

    async def search(self, query: str) -> dict[str, Any]:
        url = f"https://duckduckgo.com/?q={quote_plus(query)}"
        observation = await self.adapter.open(url)
        return _observation_dict(observation)

    async def read(self, url: str) -> dict[str, Any]:
        observation = await self.adapter.read(url)
        return _observation_dict(observation)

    async def click(self, selector: str) -> dict[str, Any]:
        return _observation_dict(await self.adapter.click(selector))

    async def type_text(self, selector: str, text: str) -> dict[str, Any]:
        return _observation_dict(await self.adapter.type_text(selector, text))

    async def scroll(self, delta: int) -> dict[str, Any]:
        return _observation_dict(await self.adapter.scroll(delta))

    async def extract(self, selector: str = "body") -> dict[str, Any]:
        return _observation_dict(await self.adapter.extract(selector))

    async def close(self) -> None:
        await self.adapter.close()

    def register(self, registry: ToolRegistry) -> list[str]:
        definitions = [
            (
                "browser_open",
                "Open a URL in the browser agent.",
                {"url": {"type": "string"}},
                ["url"],
                PermissionLevel.LOW_RISK,
                self.open,
            ),
            (
                "browser_search",
                "Search the web in the browser agent.",
                {"query": {"type": "string"}},
                ["query"],
                PermissionLevel.LOW_RISK,
                self.search,
            ),
            (
                "browser_read",
                "Read the current page or a URL.",
                {"url": {"type": "string"}},
                ["url"],
                PermissionLevel.READ_ONLY,
                self.read,
            ),
            (
                "browser_click",
                "Click an element by selector and observe the page.",
                {"selector": {"type": "string"}},
                ["selector"],
                PermissionLevel.MEDIUM_RISK,
                self.click,
            ),
            (
                "browser_type",
                "Type into an element by selector and observe the page.",
                {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                },
                ["selector", "text"],
                PermissionLevel.MEDIUM_RISK,
                self.type_text,
            ),
            (
                "browser_scroll",
                "Scroll the current page.",
                {"delta": {"type": "integer"}},
                ["delta"],
                PermissionLevel.LOW_RISK,
                self.scroll,
            ),
            (
                "browser_extract",
                "Extract readable text from the current page.",
                {"selector": {"type": "string"}},
                [],
                PermissionLevel.READ_ONLY,
                self.extract,
            ),
        ]
        names: list[str] = []
        for name, description, properties, required, permission, handler in definitions:
            registry.register(
                RuntimeTool(
                    spec=ToolSpec(
                        name=name,
                        description=description,
                        parameters={
                            "type": "object",
                            "properties": properties,
                            "required": required,
                            "additionalProperties": False,
                        },
                        permission=permission,
                        capability="browser",
                        category="BROWSER",
                        risk_level=permission,
                    ),
                    handler=handler,
                )
            )
            names.append(name)
        return names


class WebBrowserAdapter:
    """Lightweight fallback for open/read; interactive actions require Playwright."""

    async def open(self, url: str) -> BrowserObservation:
        await asyncio.to_thread(webbrowser.open, url, 2, True)
        return await self.read(url)

    async def read(self, url: str) -> BrowserObservation:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "GCAI-JARVIS/0.4"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        return BrowserObservation(
            url=str(response.url),
            title="",
            text=response.text[:20_000],
            status=response.status_code,
        )

    async def click(self, selector: str) -> BrowserObservation:
        raise RuntimeError("Interactive browser click requires Playwright.")

    async def type_text(self, selector: str, text: str) -> BrowserObservation:
        raise RuntimeError("Interactive browser typing requires Playwright.")

    async def scroll(self, delta: int) -> BrowserObservation:
        raise RuntimeError("Interactive browser scrolling requires Playwright.")

    async def extract(self, selector: str = "body") -> BrowserObservation:
        raise RuntimeError("Interactive extraction requires Playwright.")

    async def close(self) -> None:
        return None


def _observation_dict(observation: BrowserObservation) -> dict[str, Any]:
    return {
        "url": observation.url,
        "title": observation.title,
        "text": observation.text,
        "status": observation.status,
    }
