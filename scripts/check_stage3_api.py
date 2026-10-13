from __future__ import annotations

import json

import httpx

BRIDGE_URL = "http://127.0.0.1:8765"


def main() -> int:
    session_id = "stage3-verify-session"
    headers = {
        "X-Jarvis-Session": session_id,
        "X-Jarvis-Owner": "local",
    }
    with httpx.Client(timeout=30) as client:
        created = client.post(
            f"{BRIDGE_URL}/api/memory",
            headers=headers,
            json={
                "content": "阶段三验证记忆：用户偏好清晰、简短的执行报告。",
                "category": "preference",
                "importance": 4,
                "confidence": 0.93,
                "session_id": session_id,
            },
        ).json()["memory"]
        searched = client.get(
            f"{BRIDGE_URL}/api/memory",
            headers=headers,
            params={"query": "阶段三验证记忆", "session_id": session_id},
        ).json()["memories"]
        updated = client.put(
            f"{BRIDGE_URL}/api/memory/{created['id']}",
            headers=headers,
            json={"importance": 5, "session_id": session_id},
        ).json()["memory"]
        context = client.get(
            f"{BRIDGE_URL}/api/context",
            headers=headers,
            params={"session_id": session_id, "query": "阶段三验证记忆"},
        ).json()["context"]
        persona = client.get(f"{BRIDGE_URL}/api/persona", headers=headers).json()["persona"]
        tasks = client.get(
            f"{BRIDGE_URL}/api/tasks",
            headers=headers,
            params={"session_id": session_id},
        ).json()["tasks"]
        deleted = client.delete(
            f"{BRIDGE_URL}/api/memory/{created['id']}",
            headers=headers,
            params={"session_id": session_id},
        ).json()["deleted"]

    print(
        json.dumps(
            {
                "memory_created": created["id"],
                "search_hits": len(searched),
                "updated_importance": updated["importance"],
                "context_session": context["session_id"],
                "context_summary_present": bool(context["summary"]),
                "persona_version": persona["version"],
                "task_count_for_session": len(tasks),
                "memory_deleted": deleted,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
