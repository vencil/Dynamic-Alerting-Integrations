"""Tests for scripts/tools/lint/check_chart_pull_surface.py.

The gate asserts one direction only: a chart the repo TELLS customers to pull
over OCI must have a `helm push` that publishes it. Each test pins one branch:

  - the LIVE repo is clean, and clean for the right reason — da-portal is both
    taught and pushed (that is #1352 case A, fixed), while recipe-preview is
    pushed and taught nowhere, which must stay legal;
  - the counterfactual — a taught chart with no push site is RED, and the
    message names where it is taught;
  - fail-closed — a `helm push` whose chart name cannot be read is an ERROR,
    never a silently uncounted chart;
  - the scanned surface is what the docstring claims: `tests/` and `site/` are
    excluded, `.github/workflows/release.yaml` deliberately is NOT (its own
    artifact list is #1352 case B's carrier).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT = REPO_ROOT / "scripts" / "tools" / "lint"
_SCRIPT = _LINT / "check_chart_pull_surface.py"

sys.path.insert(0, str(_LINT))
_spec = importlib.util.spec_from_file_location("check_chart_pull_surface", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(gate)

_PUSH_LINE = (
    "          helm push .build/{name}-${{{{ steps.version.outputs.VERSION }}}}.tgz \\\n"
    "            oci://${{{{ env.REGISTRY }}}}/${{{{ env.IMAGE_OWNER }}}}/charts\n"
)


def _make_repo(tmp_path: Path, *, workflow_extra: str = "",
               files: dict[str, str] | None = None) -> Path:
    """A git repo shaped like this one: release.yaml with an env: block."""
    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "release.yaml").write_text(
        "env:\n  REGISTRY: ghcr.io\n  IMAGE_OWNER: acme\n\njobs:\n"
        + workflow_extra,
        encoding="utf-8",
    )
    for rel, body in (files or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=60)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, timeout=60)
    return repo


# ── the live repo ───────────────────────────────────────────────────────────
class TestLiveRepoIsClean:
    def test_no_violations(self) -> None:
        assert gate.check(REPO_ROOT) == []

    def test_da_portal_is_both_taught_and_pushed(self) -> None:
        """#1352 case A, pinned: the README's install line now has an artifact.

        Asserting BOTH halves is the point — asserting only "no violations"
        would also pass if da-portal stopped being taught, which is the other
        way to make this gate green and not the fix that landed.
        """
        taught = gate.taught_charts(REPO_ROOT)
        assert "da-portal" in taught, "nothing teaches da-portal any more"
        assert any(w.startswith("components/da-portal/README.md:")
                   for w in taught["da-portal"]), taught["da-portal"]
        assert "da-portal" in gate.discover_pushed_charts(REPO_ROOT)

    def test_a_pushed_chart_nobody_teaches_is_legal(self) -> None:
        """recipe-preview is published by its job and taught nowhere.

        The gate is one-directional on purpose: demanding a document for every
        published chart would be a policy this gate invented. If recipe-preview
        ever gains an install doc this assertion should be re-aimed, not the
        gate loosened.
        """
        pushed = gate.discover_pushed_charts(REPO_ROOT)
        assert "recipe-preview" in pushed
        assert "recipe-preview" not in gate.taught_charts(REPO_ROOT)

    def test_registry_and_owner_come_from_the_workflow(self) -> None:
        assert gate.registry_and_owner(REPO_ROOT) == ("ghcr.io", "vencil")


# ── the violation ───────────────────────────────────────────────────────────
def test_a_taught_chart_with_no_push_is_red(tmp_path: Path) -> None:
    """The #1352 case A shape, in miniature."""
    repo = _make_repo(
        tmp_path,
        workflow_extra=_PUSH_LINE.format(name="shipped"),
        files={"components/widget/README.md":
               "helm install widget oci://ghcr.io/acme/charts/widget --version 1.0.0\n"},
    )
    violations = gate.check(repo)
    assert len(violations) == 1, violations
    assert "widget" in violations[0]
    assert "components/widget/README.md:1" in violations[0]


def test_a_taught_chart_that_is_pushed_is_green(tmp_path: Path) -> None:
    """The control: same fixture, plus the push. Without this the test above
    would also pass an implementation that flags every taught chart."""
    repo = _make_repo(
        tmp_path,
        workflow_extra=_PUSH_LINE.format(name="widget"),
        files={"components/widget/README.md":
               "helm install widget oci://ghcr.io/acme/charts/widget --version 1.0.0\n"},
    )
    assert gate.check(repo) == []


def test_another_owners_oci_chart_is_none_of_our_business(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        workflow_extra=_PUSH_LINE.format(name="widget"),
        files={"docs/x.md": "helm install foo oci://ghcr.io/someone-else/charts/foo\n"},
    )
    assert gate.check(repo) == []


# ── scanned surface ─────────────────────────────────────────────────────────
# Derived from the gate's own declaration rather than restated. Two reasons,
# both learned the hard way: an exclusion added to the gate without a test here
# would otherwise go silently untested, and a literal repo path in a test module
# is harvested by tests/ops/test_ci_path_filter_coverage.py as a file this test
# READS — which would demand ci.yml widen its `python` path filter for a fixture
# that never touches the real tree.
_EXCLUDED_CARRIERS = (
    [f"{prefix}fixture-that-does-not-exist.md" for prefix in gate.EXCLUDED_PREFIXES]
    + list(gate.EXCLUDED_FILES)
)


@pytest.mark.parametrize("rel", _EXCLUDED_CARRIERS)
def test_excluded_carriers_do_not_fire(tmp_path: Path, rel: str) -> None:
    """Each exclusion is a derived or historical copy, not a hole."""
    repo = _make_repo(
        tmp_path,
        workflow_extra=_PUSH_LINE.format(name="widget"),
        files={rel: "oci://ghcr.io/acme/charts/ghost\n"},
    )
    assert gate.check(repo) == []


def test_release_yaml_is_deliberately_in_scope(tmp_path: Path) -> None:
    """#1352 case B lived in release.yaml's own artifact list.

    Excluding the file that declares what a release produces would have made
    this gate blind to half the ticket that motivated it.
    """
    repo = _make_repo(
        tmp_path,
        workflow_extra=(
            "# 產出:\n"
            "#   - oci://ghcr.io/acme/charts/never-cut (Helm chart)\n"
            + _PUSH_LINE.format(name="widget")
        ),
    )
    violations = gate.check(repo)
    assert len(violations) == 1, violations
    assert "never-cut" in violations[0]
    assert ".github/workflows/release.yaml:" in violations[0]


def test_an_untracked_file_is_not_scanned(tmp_path: Path) -> None:
    """The universe is `git ls-files`: build output nobody committed is not
    guidance, and scanning it would make the gate depend on a dirty tree."""
    repo = _make_repo(tmp_path, workflow_extra=_PUSH_LINE.format(name="widget"))
    (repo / "scratch.md").write_text("oci://ghcr.io/acme/charts/ghost\n",
                                     encoding="utf-8")
    assert gate.check(repo) == []


def test_a_binary_file_does_not_break_the_scan(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, workflow_extra=_PUSH_LINE.format(name="widget"))
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00binary")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, timeout=60)
    assert gate.check(repo) == []


# ── fail-closed ─────────────────────────────────────────────────────────────
def test_an_unreadable_push_is_an_error_not_a_skip(tmp_path: Path) -> None:
    """A push whose chart name is templated must refuse, not go uncounted.

    Skipping it would let a chart reach customers that every gate built on
    discover_pushed_charts() believes does not exist — fail-OPEN, and the exact
    direction #1352 failed in.
    """
    repo = _make_repo(
        tmp_path,
        workflow_extra="          helm push .build/${{ matrix.chart }}.tgz oci://x/y/charts\n",
    )
    with pytest.raises(gate.CallerError) as exc:
        gate.check(repo)
    assert "cannot name" in str(exc.value)


@pytest.mark.parametrize("spelling", ['ghcr.io', '"ghcr.io"', "'ghcr.io'"])
def test_a_quoted_registry_is_the_same_registry(tmp_path: Path, spelling: str) -> None:
    """⛔ Parsed as YAML, not with a line regex.

    `REGISTRY: "ghcr.io"` is the same document as `REGISTRY: ghcr.io`. A regex
    that kept the quotes built a pattern nothing matched, so adding quotes —
    a legal, invisible edit — turned this gate green while it looked in the
    wrong place. Measured before the fix: 1 violation became 0.
    """
    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "release.yaml").write_text(
        f"env:\n  REGISTRY: {spelling}\n  IMAGE_OWNER: acme\n\njobs:\n"
        + _PUSH_LINE.format(name="widget"),
        encoding="utf-8")
    (repo / "README.md").write_text("oci://ghcr.io/acme/charts/ghost\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=60)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, timeout=60)
    assert gate.registry_and_owner(repo) == ("ghcr.io", "acme")
    assert len(gate.check(repo)) == 1


@pytest.mark.parametrize("env_block", [
    "jobs:\n  build:\n",                       # no env: at all
    "env:\n  REGISTRY: ghcr.io\njobs:\n",      # half of it
    "env:\n  REGISTRY: ''\n  IMAGE_OWNER: acme\njobs:\n",   # present but empty
    "env: not-a-mapping\njobs:\n",
])
def test_an_unusable_env_block_is_an_error(tmp_path: Path, env_block: str) -> None:
    """Guessing would be worse than refusing: a wrong registry matches nothing
    and the gate would report success on a surface it never looked at."""
    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "release.yaml").write_text(env_block, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=60)
    with pytest.raises(gate.CallerError) as exc:
        gate.registry_and_owner(repo)
    assert "will not guess" in str(exc.value)


def test_an_unparseable_workflow_is_an_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "release.yaml").write_text(
        "env:\n  REGISTRY: [unclosed\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=60)
    with pytest.raises(gate.CallerError) as exc:
        gate.registry_and_owner(repo)
    assert "cannot parse" in str(exc.value)


def test_a_workflow_without_the_env_block_is_an_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "release.yaml").write_text(
        "jobs:\n  build:\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=60)
    with pytest.raises(gate.CallerError) as exc:
        gate.registry_and_owner(repo)
    assert "REGISTRY" in str(exc.value)


# ── call-site parsing ───────────────────────────────────────────────────────
@pytest.mark.parametrize("line,expected", [
    ("          helm push .build/a-${{ steps.version.outputs.VERSION }}.tgz oci://x", "a"),
    ("\t@helm push .build/threshold-exporter-$(CHART_VER).tgz oci://x", "threshold-exporter"),
    ("  helm push .build/tenant-api-${VERSION}.tgz oci://x", "tenant-api"),
    ("  helm push .build/recipe-preview-2.9.0.tgz oci://x", "recipe-preview"),
])
def test_push_call_sites_parse_in_every_dialect(tmp_path: Path, line, expected) -> None:
    repo = _make_repo(tmp_path, workflow_extra=line + "\n")
    assert sorted(gate.discover_pushed_charts(repo)) == [expected]


def test_a_commented_out_push_is_not_a_call_site(tmp_path: Path) -> None:
    """⛔ The guard must filter comment lines FIRST.

    A commented-out push read as a real one would let this gate certify a
    chart as published on the strength of a line that does nothing — a `#`
    turning a red into a green.
    """
    repo = _make_repo(
        tmp_path,
        workflow_extra="      # helm push .build/ghost-1.0.0.tgz oci://x\n"
                       + _PUSH_LINE.format(name="widget"),
        files={"docs/x.md": "oci://ghcr.io/acme/charts/ghost\n"},
    )
    assert sorted(gate.discover_pushed_charts(repo)) == ["widget"]
    violations = gate.check(repo)
    assert len(violations) == 1 and "ghost" in violations[0]


# ── unreadable inputs ───────────────────────────────────────────────────────
# ⛔ monkeypatch, not chmod: the dev container runs as root (#1264), so a
# `chmod 000` fixture passes in CI and silently exercises nothing locally.
def test_an_unreadable_workflow_is_an_error(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path, workflow_extra=_PUSH_LINE.format(name="widget"))
    monkeypatch.setattr(Path, "read_text",
                        lambda self, **kw: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(gate.CallerError) as exc:
        gate.registry_and_owner(repo)
    assert "cannot read" in str(exc.value)


def test_an_unreadable_tracked_file_is_an_error(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path, workflow_extra=_PUSH_LINE.format(name="widget"),
                      files={"docs/x.md": "nothing\n"})
    real = Path.read_text

    def boom(self, **kw):
        if self.name == "x.md":
            raise OSError("boom")
        return real(self, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(gate.CallerError) as exc:
        gate.taught_charts(repo)
    assert "docs/x.md" in str(exc.value)


def test_a_failing_git_ls_files_is_an_error(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path, workflow_extra=_PUSH_LINE.format(name="widget"))
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("no git")))
    with pytest.raises(gate.CallerError) as exc:
        gate._tracked_files(repo)
    assert "cannot list tracked files" in str(exc.value)


# ── CLI contract ────────────────────────────────────────────────────────────
def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-X", "utf8", str(_SCRIPT), *args],
                          capture_output=True, text=True, timeout=180)


def test_help_exits_zero() -> None:
    assert _run("--help").returncode == 0


def test_invalid_args_exit_two() -> None:
    assert _run("--no-such-flag").returncode == 2


def test_main_on_the_live_repo_exits_zero() -> None:
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr


def test_main_returns_exit_violation_and_prints_each_one(monkeypatch, capsys) -> None:
    """The exit CODE is the whole contract with pre-commit: `check()` finding a
    violation is worthless if `main()` still returns 0."""
    monkeypatch.setattr(gate, "check", lambda: ["ghost: taught but never pushed"])
    assert gate.main([]) == gate.EXIT_VIOLATION
    assert "ghost" in capsys.readouterr().err


def test_main_returns_exit_caller_error(monkeypatch, capsys) -> None:
    def boom():
        raise gate.CallerError("pushes a chart archive this gate cannot name")

    monkeypatch.setattr(gate, "check", boom)
    assert gate.main([]) == gate.EXIT_CALLER_ERROR
    assert "cannot name" in capsys.readouterr().err


def test_main_on_the_live_repo_in_process() -> None:
    assert gate.main([]) == gate.EXIT_OK
