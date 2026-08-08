"""Tests for scripts/ops/recover_index.sh.

Covers:
  - --check on clean repo exits 0 with ✅
  - --check on corrupted index exits 2 with 🔴 signature
  - --check leaves .git/index byte-identical (it is documented diagnose-only)
  - rebuild: corrupts real index, runs script, verifies git status works again
  - unknown / empty / extra arguments never reach the rebuild (exit 64)
  - the structural invariants that keep the rebuild un-reachable by accident
    (accepted-spelling SET, one ACTION=repair, arity first, index derived from
    git, GIT_INDEX_FILE unset, verify branch captures before printing)
  - `make recover-index` diagnoses whenever CHECK is MENTIONED, at any value
  - the shellcheck hook that replaced this module's own backtick scanner keeps
    its fail-closed args, and CI still invokes it
  - --help renders the whole Usage + Exit codes block, both edges exact

NOT covered here: unescaped backticks in the script's own text. That class is
ShellCheck SC2006's job — see the `shellcheck` hook in .pre-commit-config.yaml.
A hand-written scanner briefly lived here and was wrong in both directions on
heredoc bodies, `$'…'` quoting and `$( )` nesting; the OSS engine already reads
shell correctly and covers every script in the repo instead of this one.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Same family as the 3 modules skipped by PR #346: Git Bash on Windows mangles
# backslash arguments — `bash C:\path\file.sh` is parsed as
# `bash C:Usersvencs...recover_index.sh` (backslashes eaten as escape chars),
# so the script can never be located. Linux CI runs the bash-gated tests; the
# static ones below carry no mark and run everywhere.
# Applied PER TEST rather than as a module-level `pytestmark`, so that a future
# test needing no bash still runs on Windows — the host whose FUSE breakage this
# script exists for. A module-level skip silently takes those with it.
_needs_posix_bash = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Git Bash on Windows mangles backslash path args to bash scripts; "
           "Linux CI exercises these fully (see PR #346 for the same "
           "fix applied to test_verify_release.py / test_preflight_pass_gate.py / "
           "test_preflight_marker.py).",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ops" / "recover_index.sh"


def _init_tmp_repo(tmp_path: Path) -> Path:
    subprocess.run(  # subprocess-timeout: ignore
        ["git", "init", "-q", "-b", "main", str(tmp_path)], check=True
    )
    (tmp_path / "seed.txt").write_text("seed\n")
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(tmp_path), "add", "seed.txt"], check=True, env=env
    )
    subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"],
        check=True,
        env=env,
    )
    return tmp_path


@_needs_posix_bash
def test_check_clean_exits_0(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    proc = subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SCRIPT), "--check"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "healthy" in proc.stdout.lower()


@_needs_posix_bash
def test_check_corrupt_exits_2(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)

    # Corrupt the index by overwriting with garbage that doesn't parse
    (repo / ".git" / "index").write_bytes(b"DIRC\x00\x00\x00\x99garbage")

    proc = subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SCRIPT), "--check"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2, f"stdout={proc.stdout} stderr={proc.stderr}"
    assert "corruption" in proc.stdout.lower() or "🔴" in proc.stdout


@_needs_posix_bash
def test_rebuild_recovers_corrupt_index(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)

    # Corrupt
    (repo / ".git" / "index").write_bytes(b"DIRC\x00\x00\x00\x99garbage")

    # Sanity: git status should fail pre-rebuild
    pre = subprocess.run(  # subprocess-timeout: ignore
        ["git", "status", "--short"],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    assert pre.returncode != 0

    # Run recovery
    proc = subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout} stderr={proc.stderr}"
    assert "recovered" in proc.stdout.lower()

    # git status should work now
    post = subprocess.run(  # subprocess-timeout: ignore
        ["git", "status", "--short"],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    assert post.returncode == 0


@_needs_posix_bash
def test_check_does_not_touch_the_index(tmp_path: Path, monkeypatch) -> None:
    """`--check` is documented as diagnose-only — it must not write .git/index.

    `test_check_corrupt_exits_2` above asserts the exit code and the message, and
    stayed green while `--check` was REBUILDING the index and discarding the
    operator's staged work: the rebuild still ends in exit 2 with "corruption
    detected". Assert on the BYTES — an exit code and a human-readable line are
    both unchanged by the destruction they are supposed to rule out.

    The cause was a bare backtick pair in the advice line — bash ran
    `make recover-index`, whose CHECK-unset branch is the REPAIR path. TWO fixture
    conditions make it reachable, and no other test here has either, which is why
    the whole module was blind: a Makefile on the invocation path, AND `make`
    itself installed. Without the second this test passes for the wrong reason —
    measured: with the bug reintroduced but `make` off PATH, the module reports
    5 passed. Hence the explicit precondition below rather than a silent skip.

    An operator hitting FUSE corruption reaches for the read-only diagnostic from
    the repo root — where both conditions hold — and lost staged work.
    """
    if shutil.which("make") is None:
        pytest.skip("needs `make` on PATH: without it the substitution this test "
                    "exists to catch cannot fire, and the test would pass vacuously")

    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)

    # A `make recover-index` target on the invocation path: the condition under
    # which an unescaped substitution in the advice line becomes destructive.
    (repo / "Makefile").write_text(
        ".PHONY: recover-index\nrecover-index:\n\t@git read-tree HEAD\n"
    )

    # Corrupt, because the advice line only runs on the corruption branch — a
    # clean-repo variant of this test would never reach the defect.
    (repo / ".git" / "index").write_bytes(b"DIRC\x00\x00\x00\x99garbage")
    index_before = (repo / ".git" / "index").read_bytes()

    proc = subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SCRIPT), "--check"], capture_output=True, text=True,
    )
    assert proc.returncode == 2, f"stdout={proc.stdout} stderr={proc.stderr}"

    assert (repo / ".git" / "index").read_bytes() == index_before, (
        "--check REWROTE .git/index while reporting exit 2 ('corruption detected, "
        "no rebuild'). A corrupt index may still hold recoverable staged work; "
        "replacing it with HEAD discards that permanently, and the operator was "
        "told nothing was written."
    )


@_needs_posix_bash
def test_unknown_argument_never_reaches_the_repair_path(tmp_path: Path, monkeypatch) -> None:
    """An argument this script does not recognise must NOT rebuild the index.

    `MODE="${1:-run}"` plus a single `[ "$MODE" = "--check" ]` test meant every
    other spelling fell through to REPAIR. Measured against a corrupt index
    before the guard: `--help`, `-check` and `--CHECK` each rebuilt `.git/index`
    and printed "✅ Index recovered." — asking this tool for HELP discarded the
    operator's staged work, during the incident it exists for.

    Checked on a CORRUPT index on purpose: on a healthy one every spelling
    short-circuits to "nothing to recover", so a clean-repo version of this test
    passes for all of them and proves nothing.
    """
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    corrupt = b"DIRC\x00\x00\x00\x99garbage"

    for arg in ("--help", "-check", "check", "--CHECK", "--check=1", ""):
        (repo / ".git" / "index").write_bytes(corrupt)
        proc = subprocess.run(  # subprocess-timeout: ignore
            ["bash", str(_SCRIPT), arg], capture_output=True, text=True,
        )
        assert (repo / ".git" / "index").read_bytes() == corrupt, (
            f"{arg!r} rewrote .git/index. Only an explicit repair request may do "
            "that; an unrecognised argument resolving to the destructive default "
            "is how `--help` came to destroy staged work."
        )
        if arg in ("--help",):
            assert proc.returncode == 0, f"--help should succeed: {proc.stderr}"
        else:
            assert proc.returncode == 64, (
                f"{arg!r} should be a usage error (64), got {proc.returncode}"
            )

    # The two RECOGNISED spellings still work: no-arg repairs, --check does not.
    (repo / ".git" / "index").write_bytes(corrupt)
    assert subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SCRIPT), "--check"], capture_output=True, text=True,
    ).returncode == 2
    assert (repo / ".git" / "index").read_bytes() == corrupt
    assert subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SCRIPT)], capture_output=True, text=True,
    ).returncode == 0
    assert (repo / ".git" / "index").read_bytes() != corrupt, (
        "guard over-tightened: the default no-argument invocation must still repair"
    )


def test_destructive_path_invariants_are_pinned() -> None:
    """Every structural property that keeps the rebuild un-reachable by accident.

    Started as an accepted-spellings set and grew as each round found another
    way in; the name now matches what it actually asserts. Each clause below
    is here because reverting the corresponding fix left this module GREEN.

    The behavioural test above walks a fixed list of REJECTED spellings, which is
    an enumeration: adding an accepted alias it does not happen to mention leaves
    it green. Measured — appending `-c` to the accept-list alone left the whole
    module passing while `recover_index.sh -c` rebuilt the index, because the
    accept-list and the action dispatch used to be two independent lists.

    Pinning the set closes that: a new spelling cannot appear without this test
    failing, which forces whoever adds it to say what it DOES. Parsed from the
    source rather than from `--help`, so the two cannot silently disagree either.

    Static, so it runs on Windows too — the platform whose FUSE breakage this
    script exists for, and where every behavioural test here is skipped.
    """
    src = _SCRIPT.read_text(encoding="utf-8")
    block = re.search(r'case "\$1" in\n(.*?)\n    esac', src, re.S)
    assert block, "the argument-validation case block is gone — this guard is vacuous"

    labels: set[str] = set()
    for line in block.group(1).splitlines():
        m = re.match(r"\s{8}([^\s(][^)]*)\)", line)   # `        -h|--help)` etc.
        if m and m.group(1) != "*":
            labels.update(p.strip() for p in m.group(1).split("|"))

    assert labels == {"-h", "--help", "--check"}, (
        f"the set of accepted argument spellings changed to {sorted(labels)}.\n"
        "Every accepted spelling must be assigned an ACTION in that same case — "
        "adding one to the accept-list alone makes it fall through to the REPAIR "
        "path, which discards staged work. Update this set deliberately, and add "
        "the new spelling to the behavioural test above."
    )
    # REPAIR must stay reachable only via no-argument: giving it a spelling is how
    # `run` became an accepted, destructive, undocumented mode.
    assert 'if [ "$#" -eq 0 ]; then\n    ACTION=repair' in src, (
        "the repair action is no longer keyed to the zero-argument case; if it has "
        "a spelling, that spelling is a typeable path to discarding staged work"
    )
    # ...and EXACTLY ONCE. Pinning only the case labels left the door open one
    # level up: an `elif [ "$1" = "--force" ]; then ACTION=repair` added OUTSIDE
    # the case passed the whole module while `--force` rewrote the index.
    n_repair = len(re.findall(r"^\s*ACTION=repair\b", src, re.M))
    assert n_repair == 1, (
        f"ACTION=repair is assigned {n_repair} times; exactly one assignment may "
        "exist. A second one is a second path to discarding staged work, and the "
        "case-label set above cannot see it."
    )
    # The arity check must precede the case: --help exits 0 from inside it, so
    # checking arity afterwards let `--help foo bar` succeed against a header that
    # promises 64.
    assert src.index('if [ "$#" -gt 1 ]') < src.index('if [ "$#" -eq 0 ]'), (
        "the arity check moved after the argument dispatch; extra arguments then "
        "go unreported for any spelling that exits from within the dispatch"
    )
    # Diagnose and repair must resolve the SAME index. Deriving it from git is what
    # makes GIT_DIR / worktrees consistent instead of diagnose-A-overwrite-B.
    # Anchored on the ASSIGNMENT, not the bare phrase: the surrounding comment
    # explains `--git-path index`, so a substring check passed even after the
    # assignment itself was reverted to the hardcoded path (measured).
    assert re.search(r'^INDEX="\$\(git rev-parse --git-path index', src, re.M), (
        "the index path is no longer DERIVED from git. Hardcoding $REPO_ROOT/.git/"
        "index made `git status` and the rebuild target disagree under GIT_DIR / "
        "GIT_INDEX_FILE / linked worktrees — measured: it destroyed a healthy "
        "repo's staged work while reporting the rebuild had failed"
    )
    assert "unset GIT_INDEX_FILE" in src, (
        "GIT_INDEX_FILE is no longer unset; it has no path form for --git-path to "
        "resolve, so it re-opens the diagnose-one/overwrite-another split"
    )
    # The verify-failure branch must capture before printing: piping git into sed
    # under `set -euo pipefail` propagates git's 128 and aborts before `exit 1`.
    assert re.search(r'^VERIFY_ERR="\$\(git ', src, re.M), (
        "the final verify branch pipes git directly again, so it exits 128 instead "
        "of the 1 the header documents"
    )
    assert re.search(r"^unset GIT_INDEX_FILE$", src, re.M), (
        "GIT_INDEX_FILE unset is no longer a statement (a comment mentioning it "
        "would satisfy a substring check while the code does nothing)"
    )

    # ...and the action dispatch must branch on ACTION, not re-test the spelling:
    # re-testing $MODE is what made these two independent lists in the first place.
    assert 'if [ "$ACTION" = "diagnose" ]' in src, (
        "the diagnose branch no longer keys off ACTION; if it re-tests $MODE, the "
        "accept-list and the dispatch can drift apart again"
    )


def test_shellcheck_hook_keeps_its_fail_closed_configuration() -> None:
    """The hook that replaced this module's own backtick scanner must stay armed.

    Nothing else references its configuration: `check_orphan_lint.py` only globs
    first-party `check_*.py`, so this third-party hook is outside it. Deleting the
    hook is caught indirectly (the auto-hook count drops and `bump_docs
    --sync-counts --check` fails), but MIS-configuring it is caught by nothing —
    and three one-line edits each silently disarm it:

      - narrowing `--include` back to SC2006 alone reopens the parse/shebang doors;
      - `--severity=error` filters out SC2006 itself (a *style* code) while the
        SC10xx error codes keep firing, so the hook still looks like it works;
      - dropping `--norc` lets a `.shellcheckrc` `disable=SC2006` switch it off.

    Lives here because this script is why the hook exists — its unescaped
    `` `make recover-index` `` is the defect SC2006 catches.
    """
    cfg = (_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    block = re.search(r"- id: shellcheck\n(.*?)(?=\n  - repo:|\Z)", cfg, re.S)
    assert block, "the shellcheck hook is gone — the backtick class is unguarded"
    args = block.group(1)

    for required, why in (
        ("--norc", "a .shellcheckrc with `disable=SC2006` would switch the hook off"),
        ("--severity=style", "SC2006 is a STYLE code; a stricter severity filters it "
                             "out while the SC10xx codes keep the hook looking alive"),
        ("SC2006", "the backtick code itself"),
        ("SC1071", "shebang refusal → silent pass; this is the real shebang door"),
        ("SC1072", "parse refusal → silent pass"),
        ("SC1073", "parse refusal → silent pass"),
        ("SC1008", "defensive only — measured NOT to be a door in 0.10.0 (the file "
                   "is still analysed and SC2006 still fires), kept in case a "
                   "future version turns an unknown shebang into a refusal"),
    ):
        assert required in args, (
            f"the shellcheck hook no longer passes {required!r}: {why}.\nargs:\n{args}"
        )

    ci = (_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pre-commit run shellcheck --all-files" in ci, (
        "CI no longer invokes the shellcheck hook by name. The lint job runs hooks "
        "individually, so without this line the hook is local-only and never gates a PR"
    )


def test_makefile_wrapper_treats_any_truthy_check_as_diagnose() -> None:
    """`make recover-index CHECK=<anything non-empty>` must not repair.

    The wrapper is the half of this contract most people actually use, and it was
    testing `= "1"`, so `CHECK=true` / `CHECK=yes` went down the REPAIR path and
    discarded staged work. The behavioural test above writes its OWN dummy
    Makefile, so nothing there stops a revert of the real recipe — measured: the
    revert leaves the whole module green. Hence this static pin.
    """
    mk = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"^recover-index:.*?(?=^\S|\Z)", mk, re.S | re.M)
    assert recipe, "the recover-index target is gone — this guard is vacuous"
    body = recipe.group(0)

    assert '"$(origin CHECK)" != "undefined"' in body, (
        "the recover-index recipe no longer branches on whether CHECK was MENTIONED.\n"
        "Two earlier spellings both sent a typo down the REPAIR path, discarding "
        "staged work: `= \"1\"` repaired on CHECK=true/yes, and `-n` repaired on "
        f"`CHECK=` and on an empty exported CHECK. Recipe:\n{body}"
    )
    assert "--check" in body, "the diagnose branch no longer passes --check"


@_needs_posix_bash
def test_help_prints_the_whole_contract_and_nothing_else(tmp_path: Path, monkeypatch) -> None:
    """`--help` must render the full Usage + Exit codes block, both edges exact.

    It was extracted with a hardcoded `sed -n '15,29p'`, which had ALREADY drifted:
    the exit-64 entry was being cut mid-sentence. Worse, the failure is silent —
    `--help` still prints something plausible, so a header edit that shifts the
    range shows up as a slightly-wrong contract rather than an error. Now
    marker-delimited; this pins both ends so the markers cannot rot either.
    """
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    proc = subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SCRIPT), "--help"], capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout

    assert out.lstrip().startswith("Usage"), f"help starts mid-paragraph: {out[:80]!r}"
    for required in ("Exit codes", "0 —", "1 —", "2 —", "64 —"):
        assert required in out, f"help omits {required!r} — the range is cut short"
    # The safety-critical fact must be IN the help, not only in the source: the
    # rebuild has no spelling, so nothing an operator can type requests it.
    assert "no argument" in out and "64" in out, (
        "help no longer states that the rebuild is reachable only by passing no "
        "argument. That is the property that stops a typo from discarding staged "
        "work, and help is the only place an operator reads it"
    )
    # --help must document itself; it did not, and an operator cannot discover a
    # flag that the usage block omits.
    assert "--help" in out, "the usage block does not mention --help"
    # The LAST entry's continuation line: this is exactly what the old hardcoded
    # range truncated, and a truncation is invisible without asserting on it.
    assert "never treated as a rebuild request" in out, (
        "the final exit-code entry (64) is cut mid-sentence — the extraction "
        "range ends too early. This is the entry a hardcoded line range truncated "
        "before, and the truncation is invisible without asserting on its tail."
    )
    # The help text is read by an operator with no source in front of them, so it
    # must not point at things only visible in the file.
    for deixis in ("see the guard below", "the tracked follow-up", "fixed above"):
        assert deixis not in out, (
            f"help text says {deixis!r} — that is source-comment deixis; an "
            "operator mid-incident has no 'below' and no issue id to follow"
        )
    # ...and it must not bleed into the neighbouring paragraphs on either side.
    assert "then replace .git/index" not in out, "help bled into the text ABOVE Usage"
    assert "temp-index + cp pattern" not in out, "help bled into the text BELOW"


@_needs_posix_bash
def test_rebuild_on_clean_is_noop(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    proc = subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "nothing to recover" in proc.stdout.lower()
