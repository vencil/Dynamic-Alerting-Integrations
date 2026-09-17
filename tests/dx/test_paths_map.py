"""Tests for scripts/session-guards/paths_map.py and `.agents/paths-map.json`.

  - glob semantics (`**` spans dirs and may be empty; `*` stays in a segment)
  - map schema validation names the first violation
  - candidate paths: editing tools use file_path; Bash tokens must exist on
    disk; both are made repo-relative via the nearest `.git` (dir or file)
  - hook contract (subprocess): additionalContext on a hit, once per session
    per entry, nothing on a miss, fail-loud once on a broken map, never blocks
  - repo invariant: every `read` target of the checked-in map exists and the
    named section heading is present
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Literal repo path on purpose: verify_diff's text scan maps this test to the
# script only when the path appears verbatim (a `/`-joined Path is invisible).
_SCRIPT = _REPO_ROOT / "scripts/session-guards/paths_map.py"
_MAP = _REPO_ROOT / ".agents" / "paths-map.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("paths_map", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _entry(eid="helm-chart", paths=("helm/**",), note="n", read=None):
    return {"id": eid, "paths": list(paths), "note": note,
            "read": read if read is not None else [{"file": "docs/x.md", "section": "S"}]}


def _valid_map(*entries):
    return {"version": 1, "entries": list(entries) or [_entry()]}


# ---------------------------------------------------------------------------
# glob_to_regex / matches
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pattern,path,expected", [
    ("helm/**", "helm/a/b.yaml", True),
    ("helm/**", "helm/Chart.yaml", True),
    ("helm/**", "helmx/a", False),
    ("helm/**", "helm", False),
    ("**/*.go", "a.go", True),
    ("**/*.go", "x/y/a.go", True),
    ("**/*.go", "x/a.gox", False),
    ("docs/**/*.md", "docs/a.md", True),
    ("docs/**/*.md", "docs/a/b/c.md", True),
    ("docs/**/*.md", "docs/a.txt", False),
    ("*.md", "a.md", True),
    ("*.md", "a/b.md", False),
    ("a?c", "abc", True),
    ("a?c", "a/c", False),
    ("CHANGELOG.md", "CHANGELOG.md", True),
    ("CHANGELOG.md", "CHANGELOG.md.bak", False),
    ("a.b", "aXb", False),
])
def test_glob_semantics(pattern, path, expected):
    mod = _load_module()
    assert bool(mod.glob_to_regex(pattern).match(path)) is expected


def test_matches_returns_the_first_hit_pattern():
    mod = _load_module()
    assert mod.matches("helm/x", ["docs/**", "helm/**", "**"]) == "helm/**"
    assert mod.matches("other", ["docs/**", "helm/**"]) is None


# ---------------------------------------------------------------------------
# validate_map
# ---------------------------------------------------------------------------

class TestValidate:
    def test_valid_map_returns_entries(self):
        mod = _load_module()
        assert [e["id"] for e in mod.validate_map(_valid_map(_entry(), _entry("b")))] == ["helm-chart", "b"]

    @pytest.mark.parametrize("data,needle", [
        ({}, "entries"),
        ({"entries": "x"}, "entries"),
        ({"entries": ["x"]}, "must be an object"),
        (_valid_map(_entry(eid="Bad_Id")), "slug"),
        (_valid_map(_entry(), _entry()), "duplicate"),
        (_valid_map(_entry(paths=())), "paths"),
        (_valid_map(_entry(paths=("/abs/**",))), "relative"),
        (_valid_map(_entry(paths=("",))), "paths"),
        (_valid_map(_entry(paths=("a,b/**",))), "comma"),
        (_valid_map(_entry(read=[{"section": "S"}])), "read"),
        (_valid_map(_entry(read=[{"file": "a", "section": 3}])), "read"),
        (_valid_map(_entry(note="  ")), "note"),
    ])
    def test_each_violation_is_named(self, data, needle):
        mod = _load_module()
        with pytest.raises(mod.MapError) as excinfo:
            mod.validate_map(data)
        assert needle in str(excinfo.value)

    def test_read_without_section_is_allowed(self):
        mod = _load_module()
        mod.validate_map(_valid_map(_entry(read=[{"file": "AGENTS.md"}])))


# ---------------------------------------------------------------------------
# candidate paths
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_repo(tmp_path):
    """A main checkout (`.git/` dir) and a worktree (`.git` file), each with helm/."""
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    (main / "helm").mkdir()
    (main / "helm" / "values.yaml").write_text("a: 1\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n", encoding="utf-8")
    (wt / "helm").mkdir()
    (wt / "helm" / "values.yaml").write_text("a: 2\n", encoding="utf-8")
    return main, wt


class TestCandidates:
    def test_absolute_path_in_main_and_worktree_are_both_repo_relative(self, fake_repo):
        mod = _load_module()
        main, wt = fake_repo
        assert mod.to_repo_relative(str(main / "helm" / "values.yaml"), None) == "helm/values.yaml"
        assert mod.to_repo_relative(str(wt / "helm" / "values.yaml"), None) == "helm/values.yaml"

    def test_a_directory_is_written_with_a_trailing_slash_and_the_root_is_dropped(self, fake_repo):
        mod = _load_module()
        main, _wt = fake_repo
        assert mod.to_repo_relative(str(main / "helm"), None) == "helm/"
        assert mod.to_repo_relative("helm", str(main)) == "helm/"
        assert mod.to_repo_relative(".", str(main)) is None

    def test_relative_token_resolves_against_cwd(self, fake_repo):
        mod = _load_module()
        main, _wt = fake_repo
        assert mod.to_repo_relative("helm/values.yaml", str(main)) == "helm/values.yaml"
        assert mod.to_repo_relative("./helm/values.yaml", str(main)) == "helm/values.yaml"
        assert mod.to_repo_relative("helm/values.yaml", None) is None

    def test_nonexistent_and_out_of_repo_tokens_are_dropped(self, fake_repo, tmp_path):
        mod = _load_module()
        main, _wt = fake_repo
        assert mod.to_repo_relative("origin/main", str(main)) is None
        loose = tmp_path / "loose.txt"
        loose.write_text("x", encoding="utf-8")
        assert mod.to_repo_relative(str(loose), None) is None, "no .git above it"

    def test_bash_tokens_include_the_value_side_of_assignments(self):
        mod = _load_module()
        toks = mod.bash_tokens("python x.py --base=origin/main 'helm/values.yaml'")
        assert "origin/main" in toks and "helm/values.yaml" in toks
        assert mod.bash_tokens("echo 'unbalanced") == ["echo", "'unbalanced"]

    def test_edit_of_a_file_that_does_not_exist_yet_is_still_relative(self, fake_repo):
        mod = _load_module()
        main, _wt = fake_repo
        payload = {"tool_name": "Write", "cwd": str(main),
                   "tool_input": {"file_path": str(main / "helm" / "new.yaml")}}
        assert mod.candidate_paths(payload) == ["helm/new.yaml"]

    def test_bash_candidates_are_existing_paths_only(self, fake_repo):
        mod = _load_module()
        main, _wt = fake_repo
        payload = {"tool_name": "Bash", "cwd": str(main),
                   "tool_input": {"command": "cat helm/values.yaml helm/missing.yaml | head"}}
        assert mod.candidate_paths(payload) == ["helm/values.yaml"]

    def test_backslash_paths_are_normalised(self, fake_repo):
        mod = _load_module()
        main, _wt = fake_repo
        win = str(main / "helm" / "values.yaml").replace("/", "\\")
        assert mod.to_repo_relative(win, None) == "helm/values.yaml"


# ---------------------------------------------------------------------------
# hook contract (subprocess)
# ---------------------------------------------------------------------------

def _run(payload, map_path: Path, *args: str) -> subprocess.CompletedProcess:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(_SCRIPT), "--map", str(map_path), *args],
        input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def _context(proc) -> str | None:
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


class TestHook:
    @pytest.fixture
    def env(self, fake_repo, tmp_path):
        main, _wt = fake_repo
        map_path = tmp_path / "map.json"
        map_path.write_text(json.dumps(_valid_map(
            _entry("helm-chart", ("helm/**",), note="helm note"),
            _entry("changelog", ("CHANGELOG.md",), note="cl note"),
        )), encoding="utf-8")
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        return main, map_path, scratch

    def _payload(self, main, scratch, sid="s1", **tool):
        return {"session_id": sid, "cwd": str(main), "scratchpad_dir": str(scratch),
                "hook_event_name": "PreToolUse", **tool}

    def test_edit_hit_injects_once_per_session(self, env):
        main, map_path, scratch = env
        p = self._payload(main, scratch, tool_name="Edit",
                          tool_input={"file_path": str(main / "helm" / "values.yaml")})
        first = _run(p, map_path)
        assert first.returncode == 0, first.stderr
        ctx = _context(first)
        assert ctx and "helm-chart" in ctx and "helm note" in ctx and "docs/x.md §S" in ctx
        second = _run(p, map_path)
        assert second.returncode == 0 and _context(second) is None, "same session, same entry"
        other = _run(self._payload(main, scratch, sid="s2", tool_name="Edit",
                                   tool_input={"file_path": str(main / "helm" / "values.yaml")}),
                     map_path)
        assert _context(other), "a new session gets the entry again"

    def test_a_read_only_bash_does_not_spend_the_edit_injection(self, env):
        """Blind-review finding: `cat helm/values.yaml` then `Edit` is the
        normal rhythm; the guidance must still arrive at the edit."""
        main, map_path, scratch = env
        bash = self._payload(main, scratch, tool_name="Bash",
                             tool_input={"command": "cat helm/values.yaml"})
        assert "helm-chart" in (_context(_run(bash, map_path)) or "")
        assert _context(_run(bash, map_path)) is None, "second Bash hit of the same entry is silent"
        edit = self._payload(main, scratch, tool_name="Edit",
                             tool_input={"file_path": str(main / "helm" / "values.yaml")})
        assert "helm-chart" in (_context(_run(edit, map_path)) or ""), "the edit still gets it"
        assert _context(_run(edit, map_path)) is None

    def test_a_directory_token_matches_its_globstar_entry(self, env):
        main, map_path, scratch = env
        p = self._payload(main, scratch, tool_name="Bash", tool_input={"command": "ls helm"})
        assert "helm-chart" in (_context(_run(p, map_path)) or "")

    def test_write_into_a_directory_that_does_not_exist_yet_is_still_matched(self, env):
        main, map_path, scratch = env
        p = self._payload(main, scratch, tool_name="Write",
                          tool_input={"file_path": str(main / "helm" / "new" / "deep" / "values.yaml")})
        assert "helm-chart" in (_context(_run(p, map_path)) or "")

    def test_bash_hit_via_existing_path_token(self, env):
        main, map_path, scratch = env
        p = self._payload(main, scratch, tool_name="Bash",
                          tool_input={"command": "yq . helm/values.yaml"})
        assert "helm-chart" in (_context(_run(p, map_path)) or "")

    def test_miss_emits_nothing(self, env):
        main, map_path, scratch = env
        (main / "README.md").write_text("r", encoding="utf-8")
        p = self._payload(main, scratch, tool_name="Edit",
                          tool_input={"file_path": str(main / "README.md")})
        proc = _run(p, map_path)
        assert proc.returncode == 0 and proc.stdout == ""

    def test_two_entries_hit_in_one_call_are_both_injected(self, env):
        main, map_path, scratch = env
        (main / "CHANGELOG.md").write_text("c", encoding="utf-8")
        p = self._payload(main, scratch, tool_name="Bash",
                          tool_input={"command": "git add helm/values.yaml CHANGELOG.md"})
        ctx = _context(_run(p, map_path))
        assert ctx and "helm-chart" in ctx and "changelog" in ctx

    def test_broken_map_is_reported_once_then_silent(self, env):
        main, map_path, scratch = env
        map_path.write_text("{not json", encoding="utf-8")
        p = self._payload(main, scratch, tool_name="Edit",
                          tool_input={"file_path": str(main / "helm" / "values.yaml")})
        first = _run(p, map_path)
        assert first.returncode == 0
        assert "unreadable" in (_context(first) or "")
        second = _run(p, map_path)
        assert second.returncode == 0 and second.stdout == ""
        assert "warning" in second.stderr

    def test_garbage_stdin_never_blocks(self, env):
        _main, map_path, _scratch = env
        assert _run("{nope", map_path).returncode == 0
        assert _run("", map_path).returncode == 0

    @pytest.mark.parametrize("tool", ["Read", "NotebookEdit"])
    def test_tools_outside_the_matcher_are_ignored(self, env, tool):
        main, map_path, scratch = env
        p = self._payload(main, scratch, tool_name=tool,
                          tool_input={"file_path": str(main / "helm" / "values.yaml")})
        proc = _run(p, map_path)
        assert proc.returncode == 0 and proc.stdout == ""

    def test_validate_and_match_cli(self, env):
        _main, map_path, _scratch = env
        proc = subprocess.run([sys.executable, "-X", "utf8", str(_SCRIPT), "--map", str(map_path),
                               "--validate"], capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert proc.returncode == 0 and "2 entries" in proc.stdout
        proc = subprocess.run([sys.executable, "-X", "utf8", str(_SCRIPT), "--map", str(map_path),
                               "--match", "helm/a.yaml", "nope.txt"],
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert proc.returncode == 0
        assert "helm-chart" in proc.stdout and "no entry" in proc.stdout
        map_path.write_text("{}", encoding="utf-8")
        proc = subprocess.run([sys.executable, "-X", "utf8", str(_SCRIPT), "--map", str(map_path),
                               "--validate"], capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert proc.returncode == 1 and "INVALID" in proc.stderr


# ---------------------------------------------------------------------------
# repo invariants — the checked-in map
# ---------------------------------------------------------------------------

def _heading_lines(path: Path) -> list[str]:
    return [l.lstrip("#").strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.startswith("#")]


def test_checked_in_map_validates():
    mod = _load_module()
    assert len(mod.load_map(_MAP)) > 0


def test_every_read_target_exists_and_its_section_heading_is_present():
    """A pointer to a section that was renamed is worse than no pointer: the
    agent goes looking, finds nothing, and learns to ignore the injection."""
    mod = _load_module()
    checked = 0
    for entry in mod.load_map(_MAP):
        for r in entry["read"]:
            target = _REPO_ROOT / r["file"]
            assert target.is_file(), f"{entry['id']}: {r['file']} missing"
            if r.get("section"):
                heads = _heading_lines(target)
                assert any(r["section"] in h for h in heads), \
                    f"{entry['id']}: no heading containing {r['section']!r} in {r['file']}"
                checked += 1
    assert checked >= 10


def test_every_pattern_of_the_checked_in_map_matches_at_least_one_tracked_path():
    """A glob that matches nothing in the tree is a dead entry (renamed dir,
    typo) — it would never fire and nothing else would say so."""
    mod = _load_module()
    # tracked + untracked-but-not-ignored, so a freshly generated projection
    # counts before it is staged (the generator's own outputs are an entry).
    tracked = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=str(_REPO_ROOT), capture_output=True,
                             text=True, encoding="utf-8", timeout=60, check=True).stdout.splitlines()
    for entry in mod.load_map(_MAP):
        for pat in entry["paths"]:
            rx = mod.glob_to_regex(pat)
            assert any(rx.match(p) for p in tracked), f"{entry['id']}: {pat!r} matches no tracked file"


def test_an_unmapped_path_hits_nothing_in_the_checked_in_map():
    mod = _load_module()
    assert mod.select_hits(mod.load_map(_MAP), ["LICENSE"], set()) == []
