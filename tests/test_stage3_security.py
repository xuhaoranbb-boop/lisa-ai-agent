from types import SimpleNamespace

import pytest
from aiohttp import web

from jarvis.bridge import LocalVoiceBridge
from jarvis.config import Settings


def test_bridge_token_is_enforced_for_scoped_apis() -> None:
    bridge = object.__new__(LocalVoiceBridge)
    bridge.settings = Settings(bridge_token="stage3-secret")

    bridge._authorize_request(
        SimpleNamespace(
            headers={"X-Jarvis-Token": "stage3-secret"},
            query={},
        )
    )
    bridge._authorize_request(
        SimpleNamespace(
            headers={"Authorization": "Bearer stage3-secret"},
            query={},
        )
    )

    with pytest.raises(web.HTTPUnauthorized):
        bridge._authorize_request(
            SimpleNamespace(
                headers={"X-Jarvis-Token": "wrong"},
                query={},
            )
        )
