from __future__ import annotations

import asyncio
import json

from jarvis.runtime.browser import BrowserTool, PlaywrightBrowserAdapter


async def main() -> int:
    tool = BrowserTool(PlaywrightBrowserAdapter())
    try:
        result = await tool.open("https://example.com")
        print(
            json.dumps(
                {
                    "url": result["url"],
                    "title": result["title"],
                    "status": result["status"],
                    "text_chars": len(str(result["text"])),
                },
                ensure_ascii=False,
            )
        )
        return 0 if result["status"] == 200 else 1
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    finally:
        await tool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
