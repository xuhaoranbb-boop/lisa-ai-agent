from __future__ import annotations

import asyncio
import traceback

from jarvis.bridge import LocalVoiceBridge
from jarvis.config import Settings
from jarvis.runtime import TaskStep


async def main() -> int:
    bridge = LocalVoiceBridge(Settings())
    try:
        record = await bridge.execution_engine.run(
            "读取当前时间并验证执行引擎",
            session_id="debug-stage4",
            plan=[
                TaskStep(
                    id="step-time",
                    title="读取当前时间",
                    tool="get_current_time",
                    metadata={"arguments": {}},
                )
            ],
        )
        print(record.to_dict())
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        await bridge.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
