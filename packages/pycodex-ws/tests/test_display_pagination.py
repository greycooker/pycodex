import pytest
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from pycodex import AssistantMessage, UserMessage
from pycodex.utils.history import conversation_history_to_turns
from pycodex.utils.session_persist import SessionRolloutRecorder, load_display_history
from workspace_server import WebSessionView
from test_session_api import api_setup, session_base, wait_submission


def rows(count):
    items = []
    for index in range(count):
        items.extend((UserMessage("question {0}".format(index)),
                      AssistantMessage("answer {0}".format(index), phase="final")))
        if index == 30:
            items.append(None)
    return conversation_history_to_turns(items)


def test_display_pages_keep_ids_and_cursor_across_live_appends():
    view = WebSessionView()
    history = rows(213)
    view.load_session_history("long chat", history)
    snapshot = view.snapshot()
    assert len(snapshot["turns"]) == 80
    cursor = snapshot["history_cursor"]
    view.write_line("new live control")
    page = view.display_history_page(cursor, 80)
    earlier = view.display_history_page(page["next_cursor"], 80)
    all_turns = earlier["turns"] + page["turns"] + snapshot["turns"]
    assert [turn["turn_id"] for turn in all_turns] == [row["turn_id"] for row in history]
    assert any(turn["kind"] == "compaction" for turn in all_turns)
    assert earlier["next_cursor"] is None
    view.set_session_title("renamed")
    assert view.display_history_page(cursor)["turns"]
    view.load_session_history("replaced", rows(1))
    with pytest.raises(HTTPException) as error:
        view.display_history_page(cursor)
    assert error.value.status_code == 409


def test_restored_history_loads_older_rows_on_demand(tmp_path):
    recorder = SessionRolloutRecorder.create(
        tmp_path, "session", tmp_path, "test", None, "", tmp_path / "rollout.jsonl",
    )
    for index in range(213):
        recorder.append_history_items((
            UserMessage("question {0}".format(index)),
            AssistantMessage("answer {0}".format(index), phase="final"),
        ))
    view = WebSessionView()
    view.load_session_history("restored", load_display_history(recorder.rollout_path))
    view._recorded_rollout_path = recorder.rollout_path
    view._history_truncated = True
    snapshot = view.snapshot()
    assert len(view._turns) == 80
    view.write_line("live update")
    cursor = snapshot["history_cursor"]
    collected = snapshot["turns"]
    while cursor:
        page = view.display_history_page(cursor)
        collected = page["turns"] + collected
        cursor = page["next_cursor"]
    assert len(collected) == 213
    assert len({turn["turn_id"] for turn in collected}) == 213
    assert collected[0]["prompt"] == "question 0"
    assert view.snapshot()["turns"][-1]["response"] == "live update"


def test_display_format_routes_and_validation(api_setup):
    app, prefix = api_setup()
    with TestClient(app) as client:
        base = session_base(client, prefix)
        for index in range(3):
            result = client.post(prefix + "/api/session/message",
                                 json={"prompt": "question {0}".format(index)}).json()
            wait_submission(client, base, result["submission_id"])
        page = client.get(base + "/history?format=display&limit=2")
        assert page.status_code == 200
        assert len(page.json()["turns"]) == 2
        assert page.json()["next_cursor"]
        assert client.get(base + "/history?format=wrong").status_code == 422
        assert client.get(base + "/history?format=display&before=wrong").status_code == 409
