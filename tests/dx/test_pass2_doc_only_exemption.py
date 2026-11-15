"""Doc-only exemption of the Self-Review-Pass-2 gate (#3935).

`check_pass2_trailer_strict` SKIPs a PR without the trailer when every path
it changes is documentation (`pr_preflight.is_doc_only_path`). The rule is
defined once in the gate; the PR template points at it instead of
restating it.

* predicate: documentation paths, and the rule files that are never
  documentation (CLAUDE.md / AGENTS.md / SKILL.md at any depth, anything
  under .agents/ or .claude/);
* gate, against throwaway repos with real git: doc-only -> 0, mixed -> 1,
  rule files only -> 1, trailer present -> 0, empty range -> 0, and the
  edge cases (revert pair, rename out of docs/, deletion, missing merge
  base fails closed);
* the template names the predicate and no longer carries its own rule.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools" / "dx"))

import pr_preflight as pp  # noqa: E402

_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}
_TRAILER = "Self-Review-Pass-2: mutated foo; test_bar caught (ok)"


# ---------------------------------------------------------------------------
# is_doc_only_path
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    # Only the `.md` suffix makes a path documentation (owner ruling on
    # #3935); the directory it sits in does not matter.
    "docs/a/b.md",
    "docs/internal/dev-rules.en.md",
    "changelog.d/3935-foo.md",
    "README.md",
    "README.en.md",
    "components/threshold-exporter/README.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
])
def test_documentation_paths_are_exempt(path):
    assert pp.is_doc_only_path(path) is True


@pytest.mark.parametrize("path", [
    # rule files, any depth, any case
    "CLAUDE.md",
    "AGENTS.md",
    "components/tenant-api/CLAUDE.md",
    "docs/internal/AGENTS.md",
    "docs/foo/SKILL.md",
    "some/dir/claude.md",
    # anything under .agents/ or .claude/, including Markdown
    ".agents/skills/vibe-workflow/SKILL.md",
    ".agents/skills/vibe-workflow/reference.md",
    ".agents/roles/reviewer.md",
    ".claude/skills/x/SKILL.md",
    ".claude/agents/reviewer.md",
    ".claude/settings.json",
    "nested/.claude/notes.md",
    # the directory exceptions ignore case too (as the file-name ones do)
    ".CLAUDE/agents/reviewer.md",
    "nested/.Agents/reference.md",
    # ordinary code / config
    "scripts/tools/dx/pr_preflight.py",
    ".github/workflows/ci.yml",
    "Makefile",
    "changelog.md.bak",
    "mydocs/readme.txt",
    "foo/docs/x.py",
    "README.MD",
    # under docs/ or changelog.d/ but not Markdown: schemas, registries and
    # scripts there can carry behaviour, so the directory does not exempt
    "docs/schemas/x.schema.json",
    "docs/assets/tool-registry.yaml",
    "docs/assets/extra.css",
    "docs/internal/audit-reports/x.py",
    "changelog.d/README",
    "changelog.d/fixture.txt",
])
def test_non_documentation_paths_require_trailer(path):
    assert pp.is_doc_only_path(path) is False


def test_rule_constant_names_every_exception():
    """The human-readable rule must mention each exception the predicate
    enforces, so the template's pointer leads to a complete description."""
    for token in (".md", "case-sensitive", "docs/", "CLAUDE.md", "AGENTS.md",
                  "SKILL.md", ".agents/", ".claude/"):
        assert token in pp.PASS2_DOC_ONLY_RULE, token


def test_template_points_at_the_gate_instead_of_restating_the_rule():
    text = (_REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    assert "is_doc_only_path" in text
    assert "scripts/tools/dx/pr_preflight.py" in text
    assert "可略" not in text, "template must not carry its own exemption rule"


# ---------------------------------------------------------------------------
# check_pass2_trailer_strict against real git
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True,
                          text=True, encoding="utf-8", timeout=30,
                          env={**os.environ, **_GIT_ENV}).stdout.strip()


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """`main` with one base commit, `feature` checked out at it, cwd inside."""
    for k, v in _GIT_ENV.items():
        monkeypatch.setenv(k, v)
    r = tmp_path / "repo-3935"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    for rel in ("scripts/x.py", "docs/old.md", "README.md", "CLAUDE.md"):
        _write(r, rel, "base\n")
    _commit(r, "base")
    _git(r, "checkout", "-q", "-b", "feature")
    monkeypatch.chdir(r)
    return r


def test_doc_only_range_without_trailer_skips(repo, capsys):
    _write(repo, "docs/new.md")
    _write(repo, "changelog.d/1-x.md")
    _commit(repo, "docs: add page")
    _write(repo, "README.md", "changed\n")
    _commit(repo, "docs: readme")
    assert pp.check_pass2_trailer_strict(base_ref="main") == 0
    out = capsys.readouterr().out
    assert "documentation" in out and "is_doc_only_path" in out


def test_mixed_range_without_trailer_fails(repo, capsys):
    _write(repo, "docs/new.md")
    _write(repo, "scripts/x.py", "changed\n")
    _commit(repo, "feat: mixed")
    assert pp.check_pass2_trailer_strict(base_ref="main") == 1
    assert "scripts/x.py" in capsys.readouterr().err


@pytest.mark.parametrize("rel", [
    "CLAUDE.md",
    "AGENTS.md",
    "docs/sub/SKILL.md",
    ".agents/skills/x/SKILL.md",
    ".agents/roles/r.md",
    ".claude/settings.json",
])
def test_rule_files_only_without_trailer_fail(repo, rel):
    _write(repo, rel, "changed\n")
    _commit(repo, "chore: rule edit")
    assert pp.check_pass2_trailer_strict(base_ref="main") == 1


def test_docs_schema_only_without_trailer_fails(repo, capsys):
    """A non-.md file under docs/ is not documentation (#3935 narrowing)."""
    _write(repo, "docs/schemas/tenant.schema.json", "{}\n")
    _commit(repo, "docs: schema")
    assert pp.check_pass2_trailer_strict(base_ref="main") == 1
    assert "docs/schemas/tenant.schema.json" in capsys.readouterr().err


@pytest.mark.parametrize("rel", ["docs/new.md", "scripts/x.py", "CLAUDE.md"])
def test_trailer_present_passes_regardless_of_paths(repo, rel):
    _write(repo, rel, "changed\n")
    _commit(repo, f"chore: edit\n\n{_TRAILER}")
    assert pp.check_pass2_trailer_strict(base_ref="main") == 0


def test_empty_range_skips(repo, capsys):
    assert pp.check_pass2_trailer_strict(base_ref="main") == 0
    assert "No commits" in capsys.readouterr().out


def test_revert_pair_with_empty_diff_still_requires_trailer(repo):
    _write(repo, "scripts/x.py", "changed\n")
    _commit(repo, "feat: change")
    _git(repo, "revert", "--no-edit", "HEAD")
    assert _git(repo, "diff", "--name-only", "main...HEAD") == ""
    assert pp.check_pass2_trailer_strict(base_ref="main") == 1


def test_rename_out_of_code_into_docs_counts_both_sides(repo):
    # Without --no-renames, --name-only would list only docs/x.md (a doc
    # path) and hide the removed scripts/x.py.
    _git(repo, "mv", "scripts/x.py", "docs/x.md")
    _commit(repo, "docs: move")
    assert pp.check_pass2_trailer_strict(base_ref="main") == 1


def test_deleting_code_is_not_doc_only(repo):
    _git(repo, "rm", "-q", "scripts/x.py")
    _commit(repo, "chore: delete")
    assert pp.check_pass2_trailer_strict(base_ref="main") == 1


def test_deleting_docs_is_doc_only(repo):
    _git(repo, "rm", "-q", "docs/old.md")
    _commit(repo, "docs: delete")
    assert pp.check_pass2_trailer_strict(base_ref="main") == 0


def test_base_changes_after_branching_do_not_count(repo):
    """Merge-base diff: code that landed on base meanwhile is not the PR's."""
    _write(repo, "docs/new.md")
    _commit(repo, "docs: page")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "scripts/x.py", "base moved\n")
    _commit(repo, "feat: on main")
    _git(repo, "checkout", "-q", "feature")
    assert pp.check_pass2_trailer_strict(base_ref="main") == 0


def test_missing_merge_base_fails_closed(repo, capsys):
    """rev-list accepts unrelated histories but `base...HEAD` has no merge
    base: the exemption cannot be decided, so the gate must fail."""
    _git(repo, "checkout", "-q", "--orphan", "unrelated")
    _git(repo, "rm", "-rq", "--cached", ".")
    _write(repo, "docs/only.md")
    _git(repo, "add", "docs/only.md")
    _git(repo, "commit", "-q", "-m", "docs: orphan")
    assert pp.check_pass2_trailer_strict(base_ref="main") == 1
    assert "failing closed" in capsys.readouterr().err
