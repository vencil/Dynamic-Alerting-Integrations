"""Shape guard for `.agents/skills/<name>/evals/trigger.json`: every skill has
one, it parses to {query, should_trigger} items of both classes without
duplicates, and no query names a skill or role (that would measure name
recall, not description quality)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS = REPO_ROOT / ".agents" / "skills"
ROLES = REPO_ROOT / ".agents" / "roles"
SKILL_DIRS = sorted(d for d in SKILLS.iterdir() if (d / "SKILL.md").is_file())
AGENT_NAMES = sorted({d.name for d in SKILL_DIRS} | {p.stem for p in ROLES.glob("*.md")})


def _load(d: Path) -> list:
    return json.loads((d / "evals" / "trigger.json").read_text(encoding="utf-8"))


def test_every_skill_has_an_eval_set_and_every_eval_set_has_a_skill():
    missing = [d.name for d in SKILL_DIRS if not (d / "evals" / "trigger.json").is_file()]
    assert not missing, f"skills without evals/trigger.json: {missing}"
    orphans = [p for p in SKILLS.glob("*/evals/trigger.json") if not (p.parents[1] / "SKILL.md").is_file()]
    assert not orphans, f"eval sets under a dir with no SKILL.md: {orphans}"


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=[d.name for d in SKILL_DIRS])
def test_eval_set_shape(skill_dir: Path):
    items = _load(skill_dir)
    assert isinstance(items, list) and items, "must be a non-empty JSON list"
    for i, it in enumerate(items):
        assert isinstance(it, dict) and set(it) == {"query", "should_trigger"}, f"item {i}: {it}"
        assert isinstance(it["query"], str) and it["query"].strip(), f"item {i}: empty query"
        assert isinstance(it["should_trigger"], bool), f"item {i}: should_trigger not bool"
    classes = {it["should_trigger"] for it in items}
    assert classes == {True, False}, "both should-trigger and should-not queries are required"
    queries = [it["query"] for it in items]
    assert len(set(queries)) == len(queries), "duplicate query in set"


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=[d.name for d in SKILL_DIRS])
def test_no_query_names_a_skill_or_role(skill_dir: Path):
    assert len(AGENT_NAMES) > len(SKILL_DIRS), "roles must be in the name set too"
    assert [n for n in AGENT_NAMES if n.casefold() in "用 VIBE-RELEASE 收尾".casefold()] == ["vibe-release"]
    leaks = [(it["query"], n) for it in _load(skill_dir) for n in AGENT_NAMES
             if n.casefold() in it["query"].casefold()]
    assert not leaks, f"queries naming a skill/role: {leaks}"
