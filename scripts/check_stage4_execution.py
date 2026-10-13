from __future__ import annotations

import json
import time

import httpx

BRIDGE_URL = "http://127.0.0.1:8765"


def main() -> int:
    session_id = "stage4-verify-session"
    headers = {"X-Jarvis-Session": session_id}
    with httpx.Client(timeout=30) as client:
        created = client.post(
            f"{BRIDGE_URL}/api/executions",
            headers=headers,
            json={
                "goal": "读取当前时间并验证执行引擎",
                "session_id": session_id,
                "plan": [
                    {
                        "id": "step-time",
                        "title": "读取当前时间",
                        "tool": "get_current_time",
                        "arguments": {},
                    }
                ],
            },
        ).json()
        execution_id = created["execution_id"]
        execution: dict[str, object] = {}
        for _ in range(50):
            response = client.get(
                f"{BRIDGE_URL}/api/executions/{execution_id}",
                headers=headers,
            )
            if response.status_code != 200:
                print(
                    json.dumps(
                        {
                            "execution_id": execution_id,
                            "status_code": response.status_code,
                            "body": response.text,
                        },
                        ensure_ascii=False,
                    )
                )
                return 1
            execution = response.json()["execution"]
            if execution["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                break
            time.sleep(0.1)
        print(
            json.dumps(
                {
                    "execution_id": execution_id,
                    "status": execution.get("status"),
                    "current_step": execution.get("current_step"),
                    "steps": execution.get("steps"),
                },
                ensure_ascii=False,
            )
        )
        return 0 if execution.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
