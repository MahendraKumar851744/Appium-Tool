from __future__ import annotations

from appium_tool.exploration.actions import ManagedSession, SessionManager


class FakeExplorer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_idle_session_is_closed_and_removed(tmp_path):
    now = [100.0]
    manager = SessionManager(
        tmp_path,
        ttl_seconds=30,
        clock=lambda: now[0],
    )
    explorer = FakeExplorer()
    manager._sessions["session_1"] = ManagedSession(
        explorer=explorer,
        store=None,
        created_at=100.0,
        last_used_at=100.0,
    )

    now[0] = 131.0

    assert manager.expire_idle() == ["session_1"]
    assert explorer.closed is True
    assert "session_1" not in manager._sessions
