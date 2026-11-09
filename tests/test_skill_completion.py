import asyncio

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import ThreadedCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pycodex.cli import SkillCompleter, prompt_session_kwargs
from pycodex.context import ContextConfig, ContextManager
from pycodex.skill_completion import skill_catalog, skill_prefix


def write_skill(home, folder, name, description="A test skill"):
    path = home / "skills" / folder / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nname: {}\ndescription: {}\n---\nBody\n".format(name, description)
    )
    return path


@pytest.mark.parametrize(
    "text, expected",
    [
        ("@", "@"),
        ("$", "$"),
        ("use @wor", "@wor"),
        ("line\n$work-", "$work-"),
        ("mail@example", None),
        ("foo$work", None),
        ("${HOME}", None),
        ("$(pwd)", None),
        ("$HOME/", None),
        ("@work ", None),
    ],
)
def test_skill_marker_boundaries(text, expected):
    assert skill_prefix(text) == expected


def test_catalog_matches_model_context_and_reloads(tmp_path):
    write_skill(tmp_path, "work", "work-context")
    write_skill(tmp_path, ".system/work", "work-context", "System definition")
    hidden = write_skill(tmp_path, "hidden", "hidden")
    metadata = hidden.parent / "agents" / "openai.yaml"
    metadata.parent.mkdir()
    metadata.write_text("policy:\n  allow_implicit_invocation: false\n")
    (tmp_path / "skills" / "invalid").mkdir()
    (tmp_path / "skills" / "invalid" / "SKILL.md").write_text("No frontmatter")
    assert skill_catalog(tmp_path) == [
        {"name": "work-context", "description": "System definition"}
    ]
    context = ContextManager(ContextConfig(codex_home=tmp_path))
    assert {item.name for item in context._discover_skills()} == {"work-context"}
    write_skill(tmp_path, "new", "new-skill")
    assert [item["name"] for item in skill_catalog(tmp_path)] == [
        "new-skill",
        "work-context",
    ]


@pytest.mark.parametrize("marker", ["@", "$"])
def test_prefix_completions_preserve_surrounding_text(tmp_path, marker):
    write_skill(tmp_path, "work", "work-context")
    completer = SkillCompleter(tmp_path)
    text = "please " + marker + "WOR"
    completions = list(
        completer.get_completions(Document(text + " next", len(text)), None)
    )
    assert len(completions) == 1
    assert (
        text[: len(text) + completions[0].start_position] + completions[0].text
        == "please $work-context "
    )
    assert list(completer.get_completions(Document("$work-context", 3), None)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["@", "$"])
@pytest.mark.parametrize("accept_key", ["\r", "\t"])
async def test_terminal_accepts_skill_before_submitting(tmp_path, marker, accept_key):
    write_skill(tmp_path, "work", "work-context")
    with create_pipe_input() as pipe:
        kwargs = prompt_session_kwargs()
        kwargs.update(
            input=pipe,
            output=DummyOutput(),
            completer=ThreadedCompleter(SkillCompleter(tmp_path)),
            complete_while_typing=True,
        )
        session = PromptSession(**kwargs)
        task = asyncio.ensure_future(session.prompt_async("> "))
        try:
            pipe.send_text(marker + "wor")
            for _ in range(100):
                if (
                    session.default_buffer.complete_state is not None
                    and session.default_buffer.complete_state.completions
                ):
                    break
                await asyncio.sleep(0.01)
            assert session.default_buffer.complete_state is not None
            pipe.send_text(accept_key)
            for _ in range(100):
                if session.default_buffer.text == "$work-context ":
                    break
                await asyncio.sleep(0.01)
            assert session.default_buffer.text == "$work-context "
            assert not task.done()
            pipe.send_text("\r")
            assert await asyncio.wait_for(task, 2) == "$work-context "
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
