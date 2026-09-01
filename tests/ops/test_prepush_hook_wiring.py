"""Wiring tests for the pre-push guards — do they receive what they judge? (#1664)

WHY THIS FILE EXISTS, GIVEN ``tests/dx/test_preflight_pass_gate.py`` ALREADY HAS 16 TESTS
-----------------------------------------------------------------------------------------
Those tests are good and they still pass. All 16 of them drive the gate script
the same way::

    subprocess.run(["bash", SCRIPT], input="refs/heads/x <sha> refs/heads/x <zero>\\n")

That is, the test supplies a refspec the script would never have received in
production. (``tests/dx/test_preflight_marker.py`` adds 12 more: 7 drive the
script the same way, 5 exercise ``pr_preflight.py`` helpers in process and
never touch it. So of 28 tests across the two files, 23 invoke the script and
22 of those hand it a refspec — counting all 28 as "tests of the gate script"
is exactly the sort of number this file exists to distrust.)

⚠️ The 23rd is ``test_empty_stdin_allowed`` (``test_preflight_marker.py:178``):
it feeds the script an EMPTY stdin and asserts ``rc == 0``. That is a test
pinning the behaviour that made this bug invisible. It stays green and should:
with no pre-commit environment either, empty stdin really does mean "nothing
is being pushed". What it never asserted is that the empty set was *correct* —
that question needs the wiring, which is what this file supplies.

So the whole suite stayed green while
``scripts/ops/protect_main_push.sh`` and ``scripts/ops/require_preflight_pass.sh``
were, for four and a half months, inert on every real push: installed through
``pre-commit install --hook-type pre-push``, pre-commit consumes git's stdin
itself and spawns each hook with a stdin that is already at EOF, and both
guards read an empty refspec set as "nothing is being pushed" and exited 0.
pre-commit then printed ``Passed``.

The predicate had coverage. The WIRING had none, and the wiring is where the
defect lived. Every test below therefore drives a real ``git push`` through a
real ``pre-commit install`` and asserts on what the push actually did.

CONTROLS
--------
A must-fire assertion is worthless without something proving the harness can
produce the other answer, so each direction is pinned:

* ``test_push_to_main_is_blocked_through_precommit``    — must fire.
* ``test_the_same_harness_lets_a_clean_push_through``   — the harness CAN be
  green; the red above therefore comes from the guard, not from a broken
  fixture.
* ``test_push_to_a_feature_branch_is_not_blocked``      — the guard does not
  bite what it should tolerate.

⚠️ All pushes here use ``--dry-run``. Measured: git still runs the pre-push
hook, and the remote is left byte-identical — ``test_dry_run_still_runs_the_hook_and_leaves_the_remote_alone``
pins both halves, because a harness that silently stopped pushing would make
every "not blocked" assertion pass vacuously.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPS = _REPO_ROOT / "scripts" / "ops"

# The guards are copied into each temp repo rather than referenced by absolute
# path: pre-commit `entry:` lines are shell words, and an absolute Windows path
# (``C:\...``) is mangled by Git Bash — the reason the sibling gate tests skip
# on win32 entirely. Copying keeps the entry relative while still exercising
# the production bytes, which are re-read from disk on every test run.
_GUARD_FILES = ("protect_main_push.sh", "require_preflight_pass.sh", "_prepush_refs.sh")

# ⛔ Resolve bash instead of spelling it "bash" in argv. On Windows,
# CreateProcess searches System32 before PATH, so a bare "bash" runs
# C:\Windows\System32\bash.exe — the WSL launcher — while shutil.which()
# reports Git's bash and the two silently disagree. WSL forwards only the
# variables named in WSLENV, so every PRE_COMMIT_* the test sets arrives
# unset: measured, that turned a channel-order mutation into a false
# "SURVIVED", and made the blocking half of the parametrised test pass
# because WSL could not find the script (rc != 0 for the wrong reason).
# shutil.which is the PATH answer, which is what pre-commit itself resolves
# for `entry: bash …`, so it is also the right binary to be testing.
_BASH = shutil.which("bash")

try:  # pragma: no cover - import probe
    import pre_commit as _pre_commit_mod  # noqa: F401

    _HAS_PRE_COMMIT = True
except ImportError:  # pragma: no cover - dev hosts without pre-commit
    _HAS_PRE_COMMIT = False

_REQUIRE_PRE_COMMIT = os.environ.get("VIBE_REQUIRE_PRE_COMMIT") == "1"

# ⛔ The `and not _REQUIRE_PRE_COMMIT` half is the whole point. Without it the
# module-level skip covers `test_pre_commit_present_when_required` too, so the
# fail-closed probe is skipped by the very condition it exists to detect:
# measured, `VIBE_REQUIRE_PRE_COMMIT=1` in an interpreter without pre-commit
# gave `13 skipped`, rc=0 — a silent green, which is the #1664 shape one level
# up. With the flag set the module refuses to skip, so the probe runs and fails
# by name and the rest error loudly rather than vanishing.
pytestmark = pytest.mark.skipif(
    not _HAS_PRE_COMMIT and not _REQUIRE_PRE_COMMIT,
    reason="pre-commit is not importable; these tests drive it as the hook runner "
           "(set VIBE_REQUIRE_PRE_COMMIT=1 to make its absence a failure instead)",
)


def test_pre_commit_present_when_required() -> None:
    """Fail-closed guard against this whole file silently becoming a no-op.

    Same shape as ``VIBE_REQUIRE_CHECK_JSONSCHEMA`` /  ``VIBE_REQUIRE_HELM``:
    a dev host without pre-commit skips, but the CI job that installs it says
    so, and there a missing install is a regressed pipeline — not an optional
    check. Without this, dropping ``pre-commit`` from the job's ``pip install``
    would turn every assertion below into a skip, which is the exact failure
    mode (#1664) this file exists to detect.
    """
    if os.environ.get("VIBE_REQUIRE_PRE_COMMIT") == "1":
        assert _HAS_PRE_COMMIT, (
            "VIBE_REQUIRE_PRE_COMMIT=1 but `pre_commit` is not importable — the "
            "pre-push wiring assertions in this file would have skipped silently. "
            "It is installed by this job's pip install step."
        )


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    env = {**os.environ, **kw.pop("env_extra", {})}
    env.setdefault("GIT_AUTHOR_NAME", "t")
    env.setdefault("GIT_AUTHOR_EMAIL", "t@e")
    env.setdefault("GIT_COMMITTER_NAME", "t")
    env.setdefault("GIT_COMMITTER_EMAIL", "t@e")
    return subprocess.run(  # subprocess-timeout: ignore
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env, **kw
    )


def _commit(repo: Path, message: str) -> None:
    (repo / "a.txt").write_text(f"{message}\n", encoding="utf-8")
    assert _git(repo, "add", "a.txt").returncode == 0
    # core.hooksPath=/dev/null: this fixture's own commits must not be judged by
    # the hooks under test, otherwise a guard bug would surface as a fixture
    # error instead of a test failure.
    r = _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", message)
    assert r.returncode == 0, r.stderr


_CONFIG_HEADER = "repos:\n  - repo: local\n    hooks:\n"


def _hook_stanza(hook_id: str, name: str, entry: str) -> str:
    return (
        f"      - id: {hook_id}\n"
        f"        name: {name!r}\n"
        f"        entry: {entry}\n"
        "        language: system\n"
        "        stages: [pre-push]\n"
        "        always_run: true\n"
        "        pass_filenames: false\n"
    )


def _make_repo(tmp_path: Path, config_body: str) -> Path:
    """Bare remote + work repo with `main` already published, guards copied in."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    assert subprocess.run(  # subprocess-timeout: ignore
        ["git", "init", "--bare", "-q", str(remote)], capture_output=True
    ).returncode == 0
    assert subprocess.run(  # subprocess-timeout: ignore
        ["git", "init", "-q", "-b", "main", str(work)], capture_output=True
    ).returncode == 0
    _git(work, "config", "commit.gpgsign", "false")

    (work / "scripts" / "ops").mkdir(parents=True)
    for name in _GUARD_FILES:
        shutil.copy2(_OPS / name, work / "scripts" / "ops" / name)
    (work / ".pre-commit-config.yaml").write_text(config_body, encoding="utf-8")

    _commit(work, "init")
    assert _git(work, "remote", "add", "origin", str(remote)).returncode == 0
    assert _git(work, "push", "-q", "origin", "main").returncode == 0
    _commit(work, "second")
    return work


def _install_precommit(work: Path) -> None:
    r = subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, "-X", "utf8", "-m", "pre_commit", "install",
         "--hook-type", "pre-push"],
        cwd=work, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"pre-commit install failed: {r.stdout}{r.stderr}"
    assert (work / ".git" / "hooks" / "pre-push").exists()


def _push(work: Path, *refspecs: str, env_extra: dict | None = None):
    """`git push --dry-run` and the combined output.

    Combined on purpose: pre-commit runs each hook with stderr merged into
    stdout (``stderr=subprocess.STDOUT`` in ``pre_commit/xargs.py``), so a
    guard's banner lands on git's stdout, while git's own diagnostics land on
    stderr. Asserting against only one stream reads a real block as a miss.
    """
    r = _git(work, "push", "--dry-run", "origin", *refspecs,
             env_extra=env_extra or {})
    return r, r.stdout + r.stderr


_PROTECT_ONLY = _CONFIG_HEADER + _hook_stanza(
    "protect-main-push",
    "Guard: block direct push to main",
    "bash scripts/ops/protect_main_push.sh",
)
_PREFLIGHT_ONLY = _CONFIG_HEADER + _hook_stanza(
    "require-preflight-pass",
    "Guard: require make pr-preflight before push",
    "bash scripts/ops/require_preflight_pass.sh",
)
_BANNER = "直推 main 被阻止"


def _banner_for(branch: str) -> str:
    """The guard interpolates the protected branch into its banner.

    ⛔ A literal `_BANNER` here made the `master` row assert a string the guard
    never prints: the push WAS blocked, and the test still failed with
    "blocked, but not by this guard". Measured — the row added to cover the
    unheld half of PROTECTED_BRANCHES caught the assertion, not the guard.
    """
    return f"直推 {branch} 被阻止"


# ---------------------------------------------------------------------------
# protect_main_push: the must-fire control and its two opposites
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("protected", ["main", "master"])
def test_push_to_a_protected_branch_is_blocked_through_precommit(
    tmp_path: Path, protected: str
) -> None:
    """The assertion that had no test at all before #1664.

    Pre-fix this is green with rc=0 and no banner — measured, not assumed.

    Both members of ``PROTECTED_BRANCHES`` are driven: with only ``main``
    covered, shrinking the constant to ``"main"`` left the whole suite green,
    so half of the guard's own list was riding on nothing.
    """
    work = _make_repo(tmp_path, _PROTECT_ONLY)
    _install_precommit(work)
    r, out = _push(work, f"HEAD:refs/heads/{protected}")
    assert r.returncode != 0, f"direct push to {protected} was allowed:\n{out}"
    assert _banner_for(protected) in out, f"blocked, but not by this guard:\n{out}"


def test_the_same_harness_lets_a_clean_push_through(tmp_path: Path) -> None:
    """Positive control: the red above is the guard, not the fixture.

    Same repo, same push, same pre-commit — only the guard is absent from the
    config. Without this, any fixture error (bad remote, unusable hook, git
    refusing the refspec) would masquerade as a working gate.
    """
    noop = _CONFIG_HEADER + _hook_stanza(
        "noop", "PROBE: always passes", "bash -c true"
    )
    work = _make_repo(tmp_path, noop)
    _install_precommit(work)
    r, out = _push(work, "HEAD:refs/heads/main")
    assert r.returncode == 0, f"harness cannot produce a green push:\n{out}"


def test_push_to_a_feature_branch_is_not_blocked(tmp_path: Path) -> None:
    """False-red control: the cheapest way to satisfy the test above is a guard
    that rejects everything, and that guard would be worse than no guard."""
    work = _make_repo(tmp_path, _PROTECT_ONLY)
    _install_precommit(work)
    r, out = _push(work, "HEAD:refs/heads/feat/wiring")
    assert r.returncode == 0, f"a feature-branch push was blocked:\n{out}"
    assert _BANNER not in out


def test_dry_run_still_runs_the_hook_and_leaves_the_remote_alone(
    tmp_path: Path,
) -> None:
    """Both halves of the measurement this file's method rests on."""
    work = _make_repo(tmp_path, _PROTECT_ONLY)
    _install_precommit(work)
    before = _git(work, "ls-remote", "--heads", "origin").stdout
    r, out = _push(work, "HEAD:refs/heads/main")
    assert r.returncode != 0 and _BANNER in out, out
    after = _git(work, "ls-remote", "--heads", "origin").stdout
    assert before == after, "--dry-run modified the remote"


# ---------------------------------------------------------------------------
# The documented native install must keep working
# ---------------------------------------------------------------------------


def test_the_native_install_instruction_from_the_script_header_works(
    tmp_path: Path,
) -> None:
    """Run the header's install recipe verbatim and check it actually guards.

    ``protect_main_push.sh`` now sources a sibling helper, so the instruction
    it used to carry (``cp`` the one file into .git/hooks/pre-push) would
    break. The replacement is an exec wrapper. A shipped instruction that
    nobody executes is how the previous one rotted, so this executes it.
    """
    work = _make_repo(tmp_path, _PROTECT_ONLY)
    hook = work / ".git" / "hooks" / "pre-push"
    # ⛔ `exec bash <path>`, not `exec <path>`: the guard is mode 100644 in git,
    # so exec'ing it directly is "Permission denied" on Linux — measured here,
    # and invisible on Windows, which is why this assertion runs the shipped
    # recipe verbatim instead of paraphrasing it.
    hook.write_text(
        "#!/usr/bin/env bash\n"
        'exec bash "$(git rev-parse --show-toplevel)/scripts/ops/protect_main_push.sh" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)
    r, out = _push(work, "HEAD:refs/heads/main")
    assert r.returncode != 0, f"native install did not guard main:\n{out}"
    assert _BANNER in out, out


# ---------------------------------------------------------------------------
# Neither channel carrying a refspec must be loud, not green
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "label"),
    [(_PROTECT_ONLY, "protect-main-push"), (_PREFLIGHT_ONLY, "require-preflight-pass")],
    # ⛔ Explicit ids: without them pytest builds the id from the parameter
    # value, and the value here is a whole YAML document — the test name in CI
    # output became nine lines of embedded config.
    ids=["protect-main-push", "require-preflight-pass"],
)
def test_guard_refuses_when_no_channel_carries_a_refspec(
    tmp_path: Path, config: str, label: str
) -> None:
    """`pre-commit run --hook-stage pre-push` is the one measured way to reach
    the state where stdin is empty AND pre-commit exported nothing.

    Allowing there is not an option: pre-commit swallows a PASSING hook's
    stdout and stderr entirely (measured — a hook writing a marker to both and
    exiting 0 produced zero visible bytes), so "warn and allow" would be the
    same picture as the original defect.

    ⛔ Driven for BOTH guards. With only ``protect-main-push`` installed,
    turning ``require_preflight_pass.sh``'s refusal back into ``exit 0``
    restored the #1664 defect in that guard with the whole suite green — the
    config, not the logic, was the gap.
    """
    work = _make_repo(tmp_path, config)
    _install_precommit(work)
    r = subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, "-X", "utf8", "-m", "pre_commit", "run",
         "--all-files", "--hook-stage", "pre-push"],
        cwd=work, capture_output=True, text=True,
    )
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"guard passed while blind:\n{out}"
    assert "cannot see what is being pushed" in out, out


# ---------------------------------------------------------------------------
# The first push of a branch to an empty remote — pre-commit exports no TO_REF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expect_block"),
    [("main", True), ("feat/first", False)],
)
def test_first_push_to_an_empty_remote_is_still_judged(
    tmp_path: Path, target: str, expect_block: bool
) -> None:
    """A single-refspec push that pre-commit describes WITHOUT a to-ref.

    ``hook_impl._pre_push_ns`` has an ``all_files=True`` branch for the case
    where the first ancestor missing from the remote is the root commit — the
    first push to an empty remote. It returns a namespace with ``to_ref=None``,
    so pre-commit exports ``PRE_COMMIT_REMOTE_BRANCH`` and no
    ``PRE_COMMIT_TO_REF``.

    Measured before the field order was fixed: the emitted row began with an
    empty field, default-IFS ``read`` collapsed it, ``remote_ref`` came out
    empty and the row was dropped — ``Guard: block direct push to main ...
    Passed`` on a plain ``git push``. Not the disclosed multi-ref residual: one
    refspec, no flags.

    The second row is the control. A guard that started refusing everything
    when the sha is missing would satisfy the first row and be worse than the
    bug.
    """
    remote = tmp_path / "empty.git"
    work = tmp_path / "work"
    assert subprocess.run(  # subprocess-timeout: ignore
        ["git", "init", "--bare", "-q", str(remote)], capture_output=True
    ).returncode == 0
    assert subprocess.run(  # subprocess-timeout: ignore
        ["git", "init", "-q", "-b", "main", str(work)], capture_output=True
    ).returncode == 0
    _git(work, "config", "commit.gpgsign", "false")
    (work / "scripts" / "ops").mkdir(parents=True)
    for name in _GUARD_FILES:
        shutil.copy2(_OPS / name, work / "scripts" / "ops" / name)
    (work / ".pre-commit-config.yaml").write_text(_PROTECT_ONLY, encoding="utf-8")
    _commit(work, "root")
    assert _git(work, "remote", "add", "origin", str(remote)).returncode == 0
    _install_precommit(work)

    r, out = _push(work, f"HEAD:refs/heads/{target}")
    if expect_block:
        assert r.returncode != 0, f"first push to an empty remote bypassed the guard:\n{out}"
        assert _BANNER in out, f"blocked, but not by the guard:\n{out}"
    else:
        assert r.returncode == 0, f"a legitimate first push was blocked:\n{out}"


# ---------------------------------------------------------------------------
# Channel precedence — stdin must win over a stray environment
# ---------------------------------------------------------------------------


def test_bash_present_when_required() -> None:
    """Fail-closed, mirroring ``VIBE_REQUIRE_SHELL_TOOLS`` in ci.yml.

    The channel-order test below is the only one that invokes a guard
    directly rather than through git, so it is the only one that needs a
    resolvable bash. ubuntu-latest ships one; an absence there means the
    runner image regressed, not that the check became optional.
    """
    if os.environ.get("VIBE_REQUIRE_SHELL_TOOLS") == "1":
        assert _BASH is not None, (
            "VIBE_REQUIRE_SHELL_TOOLS=1 but no `bash` on PATH — the "
            "channel-order assertions would have skipped silently."
        )


# Same shape as the module skip above: with VIBE_REQUIRE_SHELL_TOOLS=1 the CI
# job asserts bash is there, so an absence must fail rather than skip past the
# only assertions that invoke a guard directly.
@pytest.mark.skipif(
    _BASH is None and os.environ.get("VIBE_REQUIRE_SHELL_TOOLS") != "1",
    reason="no bash on PATH to invoke the guard",
)
@pytest.mark.parametrize(
    ("piped_branch", "expect_block"),
    [("refs/heads/feat/x", False), ("refs/heads/main", True)],
)
def test_stdin_wins_over_a_stray_precommit_environment(
    tmp_path: Path, piped_branch: str, expect_block: bool
) -> None:
    """A ``PRE_COMMIT_*`` environment inherited from an unrelated parent must
    not override a refspec that was actually handed to the guard.

    Both rows matter: the first shows the env does not manufacture a block,
    the second shows the stdin path still blocks while that env is present —
    a guard that simply ignored both channels would satisfy one of them.

    The script is invoked with a RELATIVE path from inside the temp repo:
    Git Bash mangles ``C:\\path\\file`` arguments, which is why the sibling
    gate tests skip on win32 wholesale.
    """
    work = _make_repo(tmp_path, _PROTECT_ONLY)
    stdin = f"HEAD 1111111111111111111111111111111111111111 {piped_branch} 0\n"
    r = subprocess.run(  # subprocess-timeout: ignore
        [_BASH, "scripts/ops/protect_main_push.sh"],
        cwd=work, input=stdin, capture_output=True, text=True,
        env={**os.environ, "PRE_COMMIT": "1",
             "PRE_COMMIT_REMOTE_BRANCH": "refs/heads/main",
             "PRE_COMMIT_TO_REF": "2" * 40},
    )
    out = r.stdout + r.stderr
    if expect_block:
        # ⛔ rc != 0 alone is NOT enough here. Measured: with a bare "bash"
        # argv this row was satisfied by the WSL launcher failing to find the
        # script (see _BASH below) while the guard never ran at all. The
        # banner is what proves the verdict came from the guard.
        assert r.returncode != 0, f"piped main was not blocked:\n{out}"
        assert _BANNER in out, f"blocked, but not by the guard:\n{out}"
    else:
        assert r.returncode == 0, (
            "the environment overrode the piped refspec — channel order "
            f"regressed:\n{out}"
        )


# ---------------------------------------------------------------------------
# The second guard on the same wiring
# ---------------------------------------------------------------------------


def test_preflight_gate_sees_the_push_through_precommit(tmp_path: Path) -> None:
    """`require_preflight_pass.sh` shares the defect and the fix.

    STRICT mode is used so the verdict does not depend on a `gh` shim: the
    branch has no preflight marker, so the only question left is whether the
    gate learned that anything is being pushed at all. Pre-fix it did not, and
    exited 0 through the `pushing_any_commit=0` branch.
    """
    body = _CONFIG_HEADER + _hook_stanza(
        "require-preflight-pass",
        "Guard: require make pr-preflight before push",
        "bash scripts/ops/require_preflight_pass.sh",
    )
    work = _make_repo(tmp_path, body)
    _install_precommit(work)
    r, out = _push(work, "HEAD:refs/heads/feat/no-marker",
                   env_extra={"GIT_PREFLIGHT_STRICT": "1"})
    assert r.returncode != 0, f"missing preflight marker was allowed:\n{out}"
    assert "Push blocked" in out, out


def test_preflight_gate_still_allows_a_push_carrying_its_marker(
    tmp_path: Path,
) -> None:
    """Opposite direction, so the test above cannot be satisfied by a gate that
    blocks unconditionally."""
    body = _CONFIG_HEADER + _hook_stanza(
        "require-preflight-pass",
        "Guard: require make pr-preflight before push",
        "bash scripts/ops/require_preflight_pass.sh",
    )
    work = _make_repo(tmp_path, body)
    _install_precommit(work)
    head = _git(work, "rev-parse", "HEAD").stdout.strip()
    (work / ".git" / f".preflight-ok.{head}").touch()
    r, out = _push(work, "HEAD:refs/heads/feat/with-marker",
                   env_extra={"GIT_PREFLIGHT_STRICT": "1"})
    assert r.returncode == 0, f"a marked push was blocked:\n{out}"


# ---------------------------------------------------------------------------
# Residual — pinned as a measurement, not left as a guess
# ---------------------------------------------------------------------------


def test_precommit_env_channel_carries_one_ref_while_git_carries_all(
    tmp_path: Path,
) -> None:
    """The known residual, measured on both channels in one run.

    ``hook_impl._pre_push_ns`` returns on the first pushable line it finds, so
    a push updating N refs reaches a pre-commit-installed guard as ONE. This
    test does not assert that main slips — asserting a defect would read as
    requiring it. It asserts the CAUSE: git hands over every row, pre-commit's
    environment hands over exactly one. If a future pre-commit widens the
    channel this goes red, which is the point: the disclosure in
    ``scripts/ops/_prepush_refs.sh`` must not outlive the measurement behind it.
    """
    probe = _CONFIG_HEADER + _hook_stanza(
        "env-probe",
        "PROBE: record the exported refspec",
        "bash scripts/ops/env_probe.sh",
    )
    work = _make_repo(tmp_path, probe)
    # A file, not an inline `bash -c`: pre-commit shlex-splits `entry:`, so
    # nested quoting there is its own source of silent breakage.
    (work / "scripts" / "ops" / "env_probe.sh").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "${PRE_COMMIT_REMOTE_BRANCH:-<unset>}" >> env_rows.txt\n',
        encoding="utf-8",
        newline="\n",
    )
    assert _git(work, "add", "-A").returncode == 0
    assert _git(work, "-c", "core.hooksPath=/dev/null", "commit", "-q",
                "-m", "probe").returncode == 0

    # Publish a second branch so both refs are fast-forwards with a real
    # remote sha — otherwise the two rows are not comparable.
    assert _git(work, "push", "-q", "origin", "HEAD:refs/heads/aaa-first").returncode == 0
    _commit(work, "third")

    # Channel 1: git's own protocol, via a native hook.
    hook = work / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/usr/bin/env bash\ncat > \"$(git rev-parse --show-toplevel)/native_rows.txt\"\n",
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)
    _push(work, "HEAD:refs/heads/aaa-first", "HEAD:refs/heads/main")
    native_rows = [
        ln for ln in (work / "native_rows.txt").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]

    # Channel 2: the same push, through pre-commit.
    hook.unlink()
    _install_precommit(work)
    _push(work, "HEAD:refs/heads/aaa-first", "HEAD:refs/heads/main")
    env_rows = [
        ln for ln in (work / "env_rows.txt").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]

    assert len(native_rows) == 2, f"git did not offer both refs: {native_rows}"
    assert any(" refs/heads/main " in row for row in native_rows), (
        f"the control row is missing — this push did not target main: {native_rows}"
    )
    # ⛔ WHICH ref survives is lexicographic, not the order you typed. Measured:
    # `git push origin main zzz` and `git push origin zzz main` produce
    # byte-identical stdin (main, then zzz), so "name main first" is not a way
    # to stay safe — a co-pushed branch sorting BEFORE refs/heads/main is what
    # hides it. Pinned here because the disclosure would otherwise read as if
    # the ordering were the pusher's to control.
    native_refs = [row.split()[2] for row in native_rows]
    assert native_refs == sorted(native_refs), (
        f"git no longer feeds pre-push rows in sorted ref order: {native_refs}"
    )
    assert env_rows == [sorted(native_refs)[0]], (
        "pre-commit exported a different ref than git's first row — the "
        f"residual's shape changed: env={env_rows} native={native_refs}"
    )
    assert len(env_rows) == 1, (
        "pre-commit's environment channel widened to carry more than one ref — "
        f"got {env_rows}. That is good news, but the residual disclosed in "
        "scripts/ops/_prepush_refs.sh is now stale: re-measure and rewrite it."
    )


# ---------------------------------------------------------------------------
# The REPO's own wiring — every test above builds its own config
# ---------------------------------------------------------------------------


_EXPECTED_PREPUSH_HOOKS = {
    "protect-main-push": "scripts/ops/protect_main_push.sh",
    "require-preflight-pass": "scripts/ops/require_preflight_pass.sh",
    "mkdocs-strict-pre-push": "scripts/ops/pre_push_mkdocs_strict.sh",
}


def test_the_shipped_precommit_config_still_wires_all_three_guards() -> None:
    """Everything else in this file synthesises its own config, so nothing here
    touched the stanzas the repo actually ships.

    Measured on that gap: deleting the ``protect-main-push`` stanza, moving it
    to ``stages: [manual]``, dropping ``always_run: true``, pointing ``entry:``
    at a filename that does not exist, and swapping the guard for an unrelated
    no-op pre-push hook were ALL green across the guard suites. The one thing
    that noticed any of it was a hook COUNT in CLAUDE.md — which a swap keeps
    satisfied. So the cheapest way to disarm the guard was to replace it with
    some other pre-push hook.

    This is a pin, not a classifier: a closed, named set of three artifacts in
    one repo, where any edit should force a review. Set equality both ways, so
    adding a fourth pre-push hook also reds and gets a look.
    """
    config = yaml.safe_load(
        (_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    found = {}
    for repo in config.get("repos", []):
        for hook in repo.get("hooks", []):
            if "pre-push" in (hook.get("stages") or []):
                found[hook["id"]] = hook

    assert set(found) == set(_EXPECTED_PREPUSH_HOOKS), (
        "the set of pre-push hooks changed. Adding or removing one is fine, but "
        "it must be a deliberate edit here too — this assertion is the only "
        f"thing that looks at the shipped stanzas. got={sorted(found)}"
    )
    for hook_id, script in _EXPECTED_PREPUSH_HOOKS.items():
        hook = found[hook_id]
        assert hook.get("entry") == f"bash {script}", (
            f"{hook_id}: entry no longer runs {script} — got {hook.get('entry')!r}"
        )
        assert (_REPO_ROOT / script).is_file(), (
            f"{hook_id}: entry points at {script}, which does not exist"
        )
        assert hook.get("always_run") is True, (
            f"{hook_id}: always_run dropped — with no files matched the hook is "
            "skipped and the guard silently stops running"
        )
        assert hook.get("pass_filenames") is False, (
            f"{hook_id}: pass_filenames must stay false; pre-commit's default is "
            "true, which appends filenames the guard does not accept"
        )


# ---------------------------------------------------------------------------
# Contracts the guards' own comments claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script", ["protect_main_push.sh", "require_preflight_pass.sh"]
)
def test_the_helper_is_sourced_without_spawning_anything(script: str) -> None:
    """Both guards must locate the helper with parameter expansion only.

    ``test_gh_missing_*`` in tests/dx/test_preflight_pass_gate.py strips PATH to
    ``bash git basename sh cat`` to prove the preflight gate still works without
    ``gh`` — a ``$(dirname …)`` there fails with ``command not found`` and takes
    the whole gate down. That behavioural coverage exists for
    ``require_preflight_pass.sh`` only; ``protect_main_push.sh`` carries the same
    comment with nothing driving it (measured: rewriting its sourcing with
    ``$(dirname …)`` left 41/41 green). This holds the property for both, and it
    runs on every platform, which the PATH-stripping tests do not.
    """
    lines = (_OPS / script).read_text(encoding="utf-8").splitlines()

    # ⛔ Strip quoted-heredoc bodies FIRST. The guards print a reinstall recipe
    # that legitimately contains `$(git rev-parse …)` as literal text; a scanner
    # that reads it as code flags the message instead of the mechanism. (It did,
    # on the first version of this test.)
    code, in_heredoc, terminator = [], False, ""
    for ln in lines:
        if in_heredoc:
            if ln.strip() == terminator:
                in_heredoc = False
            continue
        m = re.search(r"<<-?'([A-Za-z_][A-Za-z0-9_]*)'", ln)
        if m:
            in_heredoc, terminator = True, m.group(1)
            continue
        if ln.lstrip().startswith("#"):
            continue
        code.append(ln)

    mechanism = [ln for ln in code if "_prepush_dir" in ln]
    assert len(mechanism) >= 3, (
        f"{script}: could not find the sourcing mechanism; got {mechanism}"
    )
    assert any(ln.strip() == '_prepush_dir="${BASH_SOURCE[0]%/*}"' for ln in mechanism), (
        f"{script}: the directory is no longer derived by parameter expansion:\n"
        + "\n".join(mechanism)
    )
    joined = "\n".join(mechanism)
    assert "$(" not in joined and "`" not in joined, (
        f"{script}: the sourcing mechanism spawns a subshell:\n{joined}\n"
        "Use parameter expansion — see scripts/ops/_prepush_refs.sh, EXTERNAL COMMANDS."
    )


def test_a_legacy_single_file_install_says_how_to_reinstall(tmp_path: Path) -> None:
    """The pre-#1664 recipe was ``cp scripts/ops/protect_main_push.sh
    .git/hooks/pre-push``. That copy can no longer find its sibling helper, and
    ``set -e`` turns the failed source into a total abort — feature-branch
    pushes die too. Measured before this message existed: a bare
    ``_prepush_refs.sh: No such file or directory`` and rc=1, whose three
    cheapest greens (``--no-verify``, delete the hook, freeze a copy of the
    helper in .git/hooks) all make things worse.
    """
    work = _make_repo(tmp_path, _PROTECT_ONLY)
    hook = work / ".git" / "hooks" / "pre-push"
    hook.write_bytes((_OPS / "protect_main_push.sh").read_bytes())
    hook.chmod(0o755)
    r, out = _push(work, "HEAD:refs/heads/feat/legacy")
    assert r.returncode != 0, f"a broken install silently allowed the push:\n{out}"
    assert "pre-commit install --hook-type pre-push" in out, (
        f"the failure names no way back to a working install:\n{out}"
    )


def test_tag_pushes_are_allowed_as_the_header_promises(tmp_path: Path) -> None:
    """``require_preflight_pass.sh``'s header has promised "tag push → allow"
    since it was written; the code never did it.

    ``${remote_ref##refs/heads/}`` leaves ``refs/tags/v1.2.3`` intact, so a tag
    looked like a branch name and the marker was demanded. Harmless while the
    guard was inert — #1664 made it live, and the six-line release tag push runs
    from a dev container that has no ``gh``, which is exactly the state that
    forces the marker. STRICT mode is used here so the verdict does not depend
    on a ``gh`` shim; the branch row is the control that proves the gate is
    otherwise armed under the same conditions.
    """
    work = _make_repo(tmp_path, _PREFLIGHT_ONLY)
    _install_precommit(work)
    assert _git(work, "tag", "v0.0.0-test").returncode == 0

    r, out = _push(work, "refs/tags/v0.0.0-test",
                   env_extra={"GIT_PREFLIGHT_STRICT": "1"})
    assert r.returncode == 0, f"a tag push was blocked:\n{out}"

    r2, out2 = _push(work, "HEAD:refs/heads/feat/control",
                     env_extra={"GIT_PREFLIGHT_STRICT": "1"})
    assert r2.returncode != 0, (
        f"control failed: the gate is not armed, so the tag row proves nothing:\n{out2}"
    )


@pytest.mark.skipif(
    _BASH is None and os.environ.get("VIBE_REQUIRE_SHELL_TOOLS") != "1",
    reason="no bash on PATH",
)
def test_the_resolved_bash_forwards_the_environment() -> None:
    """``_BASH`` had no negative control, and reverting it to the bare string
    ``"bash"`` made the channel-order guarantee untestable with nothing red.

    On Windows a bare ``bash`` argv reaches ``C:\\Windows\\System32\\bash.exe``
    — the WSL launcher — which forwards only the variables named in ``WSLENV``.
    Every ``PRE_COMMIT_*`` a test sets then arrives unset, so an assertion about
    channel precedence passes no matter what the code does. This pins the
    property that actually matters: the interpreter we hand the guard to must
    carry our environment into it.
    """
    r = subprocess.run(  # subprocess-timeout: ignore
        [_BASH, "-c", 'printf "%s" "${PREPUSH_PROBE:-<unset>}"'],
        capture_output=True, text=True,
        env={**os.environ, "PREPUSH_PROBE": "carried"},
    )
    assert r.stdout.strip() == "carried", (
        "the resolved bash dropped an exported variable — on Windows this is the "
        "WSL launcher, which only forwards WSLENV. Every environment-channel "
        f"assertion in this file would be vacuous. stdout={r.stdout!r}"
    )
