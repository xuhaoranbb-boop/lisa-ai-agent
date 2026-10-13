"""Minimal stdio MCP server used to verify GCAI JARVIS MCP integration."""

from __future__ import annotations

import json
import sys
from typing import Any


def respond(request_id: Any, result: Any) -> None:
    print(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}),
        flush=True,
    )


def main() -> None:
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            continue

        if method == "initialize":
            respond(
                request_id,
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "jarvis-example", "version": "0.1.0"},
                },
            )
        elif method == "tools/list":
            respond(
                request_id,
                {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Return the supplied text unchanged.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                },
                                "required": ["text"],
                            },
                        }
                    ]
                },
            )
        elif method == "tools/call":
            params = message.get("params") or {}
            arguments = params.get("arguments") or {}
            respond(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": str(arguments.get("text") or ""),
                        }
                    ]
                },
            )
        else:
            respond(request_id, {})


if __name__ == "__main__":
    main()
