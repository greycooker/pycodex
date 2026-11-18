import pytest

from workspace_server import WorkspaceSessionManager
from test_workspace_server import make_session


@pytest.mark.asyncio
async def test_workspace_session_ids_survive_restart(tmp_path):
    path = tmp_path / "state"
    first = WorkspaceSessionManager(lambda: make_session(cwd=tmp_path), path)
    await first.start()
    await first.create_session("second")
    ids = [session["id"] for session in first.list_sessions()]
    await first.close()
    second = WorkspaceSessionManager(lambda: make_session(cwd=tmp_path), path)
    await second.start()
    try:
        assert [session["id"] for session in second.list_sessions()] == ids
    finally:
        await second.close()
