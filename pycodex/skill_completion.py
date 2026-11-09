"""Skill input completion shared by terminal and workspace frontends."""

import re

from .context import discover_skills

# A marker must start a whitespace-delimited token; do not complete email addresses.
SKILL_PREFIX = re.compile(r"(?:^|\s)([@$][A-Za-z0-9_-]*)$")


def skill_prefix(text):
    match = SKILL_PREFIX.search(text)
    return None if match is None else match.group(1)


def skill_catalog(codex_home):
    """Return unique, deterministic names without exposing local filesystem paths."""
    result = []
    seen = set()
    for skill in sorted(
        discover_skills(codex_home),
        key=lambda item: (item.scope_rank, item.name, str(item.path_to_skill_md)),
    ):
        if skill.name in seen:
            continue
        seen.add(skill.name)
        result.append({"name": skill.name, "description": skill.description})
    return sorted(result, key=lambda item: item["name"].lower())
