import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from pycodex import AssistantMessage, ContextConfig, ModelResponse, ToolRegistry
from pycodex.utils import uuid7_string
from pycodex.utils.session_persist import resolve_codex_home, rollout_path_for_session
from tests.fakes import ScriptedModelClient, make_agent, make_runtime
from workspace_server import WorkspaceInteractiveSession, create_app


def make_session():
    model = ScriptedModelClient(
        response_factory=lambda prompt, count: ModelResponse([AssistantMessage("done")])
    )
    session_id = uuid7_string()
    return WorkspaceInteractiveSession(make_runtime(make_agent(
        model, ToolRegistry(), ContextConfig(),
        session_file_path=rollout_path_for_session(resolve_codex_home(), session_id),
        session_id=session_id,
    )))


def wait_snapshot(client, predicate, path="/api/session"):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(path)
        assert response.status_code == 200
        snapshot = response.json()["snapshot"]
        if predicate(snapshot):
            return snapshot
        time.sleep(0.01)
    raise AssertionError("session did not reach expected state")


def chat(browser, session_id, prompt):
    response = browser.post(
        "/api/session/message", json={"session_id": session_id, "prompt": prompt},
    )
    assert response.status_code == 200, response.text
    return response.json()


def finished_rollout(browser, session_id):
    snapshot = wait_snapshot(
        browser,
        lambda item: item["turns"] and item["turns"][-1]["response"] == "done"
        and item["recorded_rollout_path"],
        path="/api/session?session_id=" + session_id,
    )
    path = Path(snapshot["recorded_rollout_path"])
    deadline = time.monotonic() + 3
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()
    return path


def test_delete_command_removes_only_this_sessions_record(tmp_path):
    with TestClient(create_app(make_session, tmp_path / "board")) as browser:
        first = browser.get("/api/sessions").json()["sessions"][0]["id"]
        second = browser.post("/api/sessions").json()["session_id"]
        chat(browser, first, "keep me")
        chat(browser, second, "delete me")
        keep = finished_rollout(browser, first)
        doomed = finished_rollout(browser, second)
        assert keep != doomed

        result = chat(browser, second, " /delete ")

        assert result["type"] == "deleted"
        assert result["deleted_session_id"] == second
        assert result["removed_files"] == [str(doomed.resolve())]
        assert result["session_id"] == first
        assert [item["id"] for item in result["sessions"]] == [first]
        assert not doomed.exists()
        assert keep.exists()
        assert browser.get("/api/session?session_id=" + second).status_code == 404
        follow = browser.get("/api/session?session_id=" + first).json()["snapshot"]
        assert [turn["prompt"] for turn in follow["turns"]] == ["keep me"]

    with TestClient(create_app(make_session, tmp_path / "board")) as browser:
        sessions = browser.get("/api/sessions").json()["sessions"]
        assert [item["id"] for item in sessions] == [first]


def test_delete_command_on_last_session_opens_a_fresh_one(tmp_path):
    with TestClient(create_app(make_session, tmp_path / "board")) as browser:
        only = browser.get("/api/sessions").json()["sessions"][0]["id"]
        chat(browser, only, "only one")
        doomed = finished_rollout(browser, only)

        result = chat(browser, only, "/delete")

        assert result["type"] == "deleted"
        assert not doomed.exists()
        assert result["session_id"] != only
        assert [item["id"] for item in result["sessions"]] == [result["session_id"]]
        assert result["snapshot"]["turns"] == []


def test_delete_keeps_a_rollout_still_used_by_another_tab(tmp_path):
    with TestClient(create_app(make_session, tmp_path / "board")) as browser:
        first = browser.get("/api/sessions").json()["sessions"][0]["id"]
        chat(browser, first, "shared")
        shared = finished_rollout(browser, first)
        second = browser.post("/api/sessions").json()["session_id"]
        chat(browser, second, "/resume 1")

        result = chat(browser, second, "/delete")

        assert result["type"] == "deleted"
        assert result["removed_files"] == []
        assert shared.exists()


@pytest.mark.asyncio
async def test_delete_never_touches_files_outside_codex_sessions(tmp_path):
    from workspace_server.workspaces import WorkspaceSessionManager

    manager = WorkspaceSessionManager(make_session, tmp_path / "state")
    await manager.start()
    try:
        outside = tmp_path / "outside.jsonl"
        outside.write_text("{}\n")
        victim = await manager.create_session()
        manager._tab_states[victim]["rollout_path"] = str(outside)
        result = await manager.delete_session(victim)
        assert result["removed_files"] == []
        assert outside.exists()
        assert victim not in [item["id"] for item in manager.list_sessions()]
    finally:
        await manager.close()


def test_delete_text_inside_a_message_is_an_ordinary_prompt(tmp_path):
    with TestClient(create_app(make_session, tmp_path / "board")) as browser:
        first = browser.get("/api/sessions").json()["sessions"][0]["id"]
        result = chat(browser, first, "please /delete nothing")
        assert result["type"] == "submitted"
        finished_rollout(browser, first)
        assert [item["id"] for item in browser.get("/api/sessions").json()["sessions"]] == [first]
