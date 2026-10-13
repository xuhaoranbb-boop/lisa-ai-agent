from __future__ import annotations

import json
import time

import httpx

BRIDGE_URL = "http://127.0.0.1:8765"


def main() -> int:
    session_id = "stage5-verify-session"
    headers = {"X-Jarvis-Session": session_id}
    with httpx.Client(timeout=60) as client:
        system = client.get(f"{BRIDGE_URL}/api/system/monitor", headers=headers).json()
        screen = client.get(f"{BRIDGE_URL}/api/screen", headers=headers).json()
        plugins = client.get(f"{BRIDGE_URL}/api/plugins", headers=headers).json()
        trace_before = client.get(f"{BRIDGE_URL}/api/trace", headers=headers).json()
        workflow_response = client.post(
            f"{BRIDGE_URL}/api/workflows",
            headers=headers,
            json={
                "name": "stage5-check",
                "session_id": session_id,
                "steps": [
                    {
                        "id": "time",
                        "title": "读取时间",
                        "goal": "读取当前时间",
                        "tool": "get_current_time",
                        "arguments": {},
                    }
                ],
            },
        ).json()
        workflow_id = workflow_response["workflow"]["workflow_id"]
        client.post(
            f"{BRIDGE_URL}/api/workflows/{workflow_id}/run",
            headers=headers,
        )
        workflow: dict[str, object] = {}
        for _ in range(50):
            workflow = client.get(
                f"{BRIDGE_URL}/api/workflows/{workflow_id}",
                headers=headers,
            ).json()["workflow"]
            if workflow["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                break
            time.sleep(0.1)
        trace_after = client.get(f"{BRIDGE_URL}/api/trace", headers=headers).json()
        print(
            json.dumps(
                {
                    "system_ok": system.get("ok"),
                    "screen_confidence": screen.get("screen", {}).get("confidence"),
                    "plugin_count": len(plugins.get("plugins", [])),
                    "trace_before": len(trace_before.get("trace", [])),
                    "trace_after": len(trace_after.get("trace", [])),
                    "workflow_status": workflow.get("status"),
                },
                ensure_ascii=False,
            )
        )
        return 0 if workflow.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
