"""Tests for scripts/tools/lint/check_planning_status_sync.py (#379 chunk 2b).

Pinned contracts
----------------
1. **Git native trailer parser**: `extract_trailers` uses
   `git log --format='%(trailers:key=...,valueonly=true,unfold=true)'` so RFC-2822
   trailer semantics come from git itself — case-insensitive keys, blank-line-
   required separation between body and trailer block, multi-line fold tolerance.
   Tests exercise each: lowercase verbs, multi-trailer-per-commit, body-without-
   blank-line (no trailer).

2. **ID enum** is limited to live namespaces (`TRK-NNN` / `ADR-NNN` / `S#NNN`).
   Legacy `TD-NN` / `HA-NN` / `REG-NN` are intentionally NOT recognised — the
   trailer convention only fires on the new namespace per #379 chunk 5 spec.

3. **Validation**: three failure modes (`missing-entry` / `status-not-done` /
   `pr-ref-missing|mismatch`). Each is independent — one trailer can produce
   multiple issues only when both status AND pr_ref are wrong on the same entry.

4. **Exit codes**:
   - 0 = no issues, OR issues present but `--strict` not set (soft-warn)
   - 1 = issues present and `--strict`/`--ci` set
   - 2 = setup error (missing git, missing base ref, …)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LINT_DIR = REPO_ROOT / "scripts" / "tools" / "lint"
DX_DIR = REPO_ROOT / "scripts" / "dx"
sys.path.insert(0, str(LINT_DIR))
sys.path.insert(0, str(DX_DIR))

import check_planning_status_sync as cps  # noqa: E402
from generate_planning_index import PlanningEntry  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _git(*args, cwd):
    """Run git in test repo. Use plain env so existing global config doesn't bleed in."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
        timeout=30,
    )


@pytest.fixture
def tmp_git_repo(tmp_path):
    """tmp_path with `git init` + a base commit on branch `main`."""
    _git("init", "--initial-branch=main", "-q", cwd=tmp_path)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "chore: seed", cwd=tmp_path)
    # Some test invocations resolve `origin/main` — point origin at ourselves.
    _git("remote", "add", "origin", str(tmp_path), cwd=tmp_path)
    _git("fetch", "-q", "origin", cwd=tmp_path)
    return tmp_path


def _commit(repo, msg):
    """Add a tracked file edit + commit with the given message (use \\n for newlines)."""
    f = repo / "a.txt"
    prev = f.read_text(encoding="utf-8") if f.exists() else ""
    f.write_text(prev + "x\n", encoding="utf-8")
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-q", "-F", "-", cwd=repo, input=msg) if False else None
    proc = subprocess.run(
        ["git", "commit", "-q", "-F", "-"],
        cwd=str(repo),
        input=msg,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        },
        check=True,
        timeout=30,
    )
    return proc


# ---------------------------------------------------------------------------
# extract_trailers — exercises git native trailer parsing
# ---------------------------------------------------------------------------
class TestExtractTrailers:
    def test_single_resolves_trailer(self, tmp_git_repo):
        _commit(
            tmp_git_repo,
            "feat: thing\n\nbody line\n\nResolves: TRK-228\n",
        )
        hits = cps.extract_trailers("origin/main", repo_root=tmp_git_repo)
        assert len(hits) == 1
        assert hits[0].id == "TRK-228"
        assert hits[0].verb == "Resolves"

    def test_lowercase_verb_also_matched(self, tmp_git_repo):
        """git's trailer parser is case-insensitive on the key."""
        _commit(
            tmp_git_repo,
            "feat: thing\n\nbody\n\nresolves: TRK-100\n",
        )
        hits = cps.extract_trailers("origin/main", repo_root=tmp_git_repo)
        assert len(hits) == 1 and hits[0].id == "TRK-100"

    def test_multiple_trailer_keys_per_commit(self, tmp_git_repo):
        _commit(
            tmp_git_repo,
            "feat: thing\n\nbody\n\nResolves: TRK-1\nFixes: ADR-020\n",
        )
        ids = {h.id for h in cps.extract_trailers("origin/main", repo_root=tmp_git_repo)}
        assert ids == {"TRK-1", "ADR-020"}

    def test_trailer_without_blank_line_above_not_matched(self, tmp_git_repo):
        """git treats the trailer block as starting only AFTER a blank line below the body."""
        _commit(
            tmp_git_repo,
            "feat: thing\nbody on same paragraph\nResolves: TRK-9\n",
        )
        # No blank line before "Resolves" → git does not classify it as a trailer.
        hits = cps.extract_trailers("origin/main", repo_root=tmp_git_repo)
        assert hits == []

    def test_legacy_id_namespaces_ignored(self, tmp_git_repo):
        """`TD-NN` / `HA-NN` / `REG-NN` must be translated to TRK- before resolving."""
        _commit(
            tmp_git_repo,
            "fix: x\n\nResolves: TD-022\nFixes: HA-11\nCloses: REG-004\n",
        )
        assert cps.extract_trailers("origin/main", repo_root=tmp_git_repo) == []

    def test_sprint_namespace_recognised(self, tmp_git_repo):
        _commit(
            tmp_git_repo,
            "fix: x\n\nCloses: S#74\n",
        )
        hits = cps.extract_trailers("origin/main", repo_root=tmp_git_repo)
        assert [(h.verb, h.id) for h in hits] == [("Closes", "S#74")]

    def test_no_trailers_returns_empty(self, tmp_git_repo):
        _commit(tmp_git_repo, "chore: no-trailer commit\n")
        assert cps.extract_trailers("origin/main", repo_root=tmp_git_repo) == []

    def test_subpr_letter_suffix_id_matched(self, tmp_git_repo):
        """Sub-PR IDs like TRK-230c per planning-id-mapping.md must match."""
        _commit(
            tmp_git_repo,
            "feat: x\n\nResolves: TRK-230c\n",
        )
        hits = cps.extract_trailers("origin/main", repo_root=tmp_git_repo)
        assert len(hits) == 1 and hits[0].id == "TRK-230c"

    def test_missing_base_ref_raises_checkerror(self, tmp_git_repo):
        with pytest.raises(cps.CheckError, match="base ref"):
            cps.extract_trailers("origin/nonexistent", repo_root=tmp_git_repo)

    def test_comma_separated_multi_ids_on_single_trailer_line(self, tmp_git_repo):
        """Captures a real authoring style: `Resolves: TRK-228, TRK-229`. The
        parser must split tokens by whitespace and strip trailing `,;`.

        Caught during PR #481 self-review — pinned here so a future regex
        regression doesn't silently drop one of the IDs.
        """
        _commit(
            tmp_git_repo,
            "feat: x\n\nbody\n\nResolves: TRK-228, TRK-229\n",
        )
        ids = sorted(h.id for h in cps.extract_trailers("origin/main", repo_root=tmp_git_repo))
        assert ids == ["TRK-228", "TRK-229"]


# ---------------------------------------------------------------------------
# GitHub Actions inline annotations
# ---------------------------------------------------------------------------
class TestGhaAnnotations:
    """Soft-warn mode is invisible without `::warning::` workflow commands —
    pinned during PR #481 self-review.
    """

    def test_emits_workflow_command_per_issue(self, capsys):
        issues = [
            cps.ValidationIssue(id="TRK-1", kind="status-not-done", detail="foo"),
            cps.ValidationIssue(id="TRK-2", kind="missing-entry", detail="bar"),
        ]
        cps._emit_gha_warnings(issues)
        out = capsys.readouterr().out
        assert "::warning title=planning-sync (status-not-done)::TRK-1: foo" in out
        assert "::warning title=planning-sync (missing-entry)::TRK-2: bar" in out

    def test_escapes_newlines_and_percent_in_detail(self, capsys):
        """`\\n` and `%` would otherwise terminate / corrupt the GHA command."""
        issues = [
            cps.ValidationIssue(
                id="TRK-9",
                kind="status-not-done",
                detail="multi\nline\n%detail",
            ),
        ]
        cps._emit_gha_warnings(issues)
        out = capsys.readouterr().out
        assert "%0A" in out  # newlines escaped
        assert "%25" in out  # literal % escaped before %0A substitution
        assert "\n" not in out.rstrip("\n")  # no raw newlines mid-line


# ---------------------------------------------------------------------------
# validate_sync — pure function, three failure modes
# ---------------------------------------------------------------------------
class TestValidateSync:
    def _entry(self, **kwargs):
        defaults = dict(
            id="TRK-500", title="T", tracking_kind="tech-debt",
            status="done", source_path="docs/x.md", pr_ref="480",
        )
        defaults.update(kwargs)
        return PlanningEntry(**defaults)

    def _hit(self, id="TRK-500"):
        return cps.TrailerHit(verb="Resolves", id=id, commit_sha="abc1234")

    def test_all_aligned_no_issues(self):
        entries = [self._entry()]
        issues = cps.validate_sync([self._hit()], entries, pr_number="480")
        assert issues == []

    def test_missing_entry_reported(self):
        issues = cps.validate_sync([self._hit("TRK-999")], [], pr_number=None)
        assert len(issues) == 1
        assert issues[0].kind == "missing-entry"
        assert "TRK-999" in issues[0].detail

    def test_status_not_done_reported(self):
        entries = [self._entry(status="in-progress")]
        issues = cps.validate_sync([self._hit()], entries, pr_number=None)
        assert [i.kind for i in issues] == ["status-not-done"]
        assert "in-progress" in issues[0].detail

    def test_pr_ref_missing_reported(self):
        entries = [self._entry(pr_ref="")]
        issues = cps.validate_sync([self._hit()], entries, pr_number="480")
        assert [i.kind for i in issues] == ["pr-ref-missing"]

    def test_pr_ref_mismatch_reported(self):
        entries = [self._entry(pr_ref="479")]
        issues = cps.validate_sync([self._hit()], entries, pr_number="480")
        assert [i.kind for i in issues] == ["pr-ref-mismatch"]
        assert "479" in issues[0].detail and "480" in issues[0].detail

    def test_pr_ref_skipped_when_not_provided(self):
        """`--pr-number` omitted → don't enforce pr_ref alignment."""
        entries = [self._entry(pr_ref="479")]  # wrong, but pr_number=None
        issues = cps.validate_sync([self._hit()], entries, pr_number=None)
        assert issues == []

    def test_multiple_issues_per_entry(self):
        """status + pr_ref both wrong → two issues on same entry."""
        entries = [self._entry(status="proposed", pr_ref="479")]
        issues = cps.validate_sync([self._hit()], entries, pr_number="480")
        kinds = {i.kind for i in issues}
        assert kinds == {"status-not-done", "pr-ref-mismatch"}


# ---------------------------------------------------------------------------
# CLI — end-to-end exit codes
# ---------------------------------------------------------------------------
class TestMainCli:
    """End-to-end exit-code contracts.

    Limitations: the script hard-codes `REPO_ROOT = SCRIPT_DIR.parent.parent.parent`
    so subprocess invocations always exercise the real repo's git state. We
    therefore can't easily set up a clean trailers-vs-no-trailers fixture for the
    CLI here — that path is covered exhaustively by the unit tests above.
    What we DO test at the CLI layer is the setup-error contract (exit 2 with
    actionable message) so contributors hitting a bad `--base` know to add
    `fetch-depth: 0` to their workflow.
    """

    def test_missing_base_ref_exits_2(self, tmp_path):
        """Setup error path: bogus base ref → exit 2 with actionable message."""
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(
            [
                sys.executable,
                str(LINT_DIR / "check_planning_status_sync.py"),
                "--base", "origin/does-not-exist-anywhere",
            ],
            capture_output=True, text=True, encoding="utf-8",
            env=env, timeout=60,
        )
        assert proc.returncode == 2
        assert "base ref" in proc.stderr or "fetch-depth" in proc.stderr


# ---------------------------------------------------------------------------
# ID regex contract — ensure the allowlist matches the spec
# ---------------------------------------------------------------------------
class TestIdRegex:
    @pytest.mark.parametrize(
        "candidate",
        ["TRK-001", "TRK-228", "TRK-230c", "ADR-020", "S#74"],
    )
    def test_live_namespaces_match(self, candidate):
        assert cps.ID_RE.match(candidate) is not None

    @pytest.mark.parametrize(
        "candidate",
        ["TD-022", "HA-11", "REG-004", "TECH-DEBT-007", "TRAP-12", "TRK-", "trk-1"],
    )
    def test_excluded_forms_no_match(self, candidate):
        assert cps.ID_RE.match(candidate) is None
