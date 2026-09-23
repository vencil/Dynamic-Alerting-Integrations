"""Tests for check_pipestatus_errexit.py — TRK-382 / #1845.

Every refuse/exempt predicate is tested in both directions: a miss (a real
violation goes green) and an over-rejection (a legitimate site goes red).
Both lose real violations silently — the first directly, the second by
training authors to paste exemptions.

Known boundaries are pinned as boundaries (``test_boundary_*``): they assert
today's documented behaviour, including the misses, so a change to them is
a visible decision rather than drift.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_pipestatus_errexit as lint  # noqa: E402

_STRICT_SHORT = "set -euo pipefail\ngo test ./... | tee out.log\nrc=${PIPESTATUS[0]}\n"
# #1845 hole 7: long flags. bash ground truth: the read never runs.
_STRICT_LONG = ("set -o errexit\nset -o pipefail\n"
                "false | cat\nrc=${PIPESTATUS[0]}\necho reached\n")
# #1845 hole 8: trailing `&`. bash ground truth: the read runs (rc 0).
_ASYNC = ("set -euo pipefail\n( exit 7 ) | cat &\nwait\n"
          "rc=${PIPESTATUS[0]}{EXEMPT}\necho reached\n")


def _viol(text: str, path: str = "x.sh"):
    return lint.scan_text(path, text).violations


# --- detection (miss direction) --------------------------------------------

def test_strict_short_flags_read_is_flagged():
    assert [ln for ln, _ in _viol(_STRICT_SHORT)] == [3]


def test_strict_long_flags_read_is_flagged():
    assert [ln for ln, _ in _viol(_STRICT_LONG)] == [4]


def test_errexit_and_pipefail_on_one_set_line_with_long_errexit():
    assert _viol("set -o errexit -o pipefail\nx | y\nr=${PIPESTATUS[1]}\n")


@pytest.mark.parametrize("arm", [
    "set -o pipefail -e",
    "set -o pipefail -o errexit",
    "set -u -o pipefail -e",
    "set -o pipefail\t-e",
    "set -euo pipefail;",          # separator glued to the last flag
    "( set -euo pipefail )",
    'set -euo "pipefail"',            # quoted option argument
    "set -o 'errexit' -o pipefail",
])
def test_errexit_flag_after_other_flags_is_seen(arm):
    # Miss direction: errexit need not be the first argument of `set`.
    assert _viol(f"{arm}\nx | y\nr=${{PIPESTATUS[0]}}\n")


@pytest.mark.parametrize("line", [
    "set -o pipefail; grep -e foo f",   # a later command's -e
    "echo set foo -e; set -o pipefail",  # `set` is only an argument
    "set -- -e; set -o pipefail",        # -e is a positional parameter
])
def test_boundary_arming_is_read_wide(line):
    # Documented over-report: arming is read wide on purpose, so a wrong
    # "armed" is a loud red with an exemption exit, never a silent miss.
    assert lint.scan_text("x.sh", f"{line}\nx | y\nr=${{PIPESTATUS[0]}}\n").strict


@pytest.mark.parametrize("line", ["offset -e", "reset -e", "x.set -e", "$set -e", "set-e"])
def test_set_must_be_a_whole_word_to_arm(line):
    assert not lint.scan_text("x.sh", f"{line}\nset -o pipefail\n").strict


def test_backslash_continuation_is_one_logical_line():
    # Miss direction: bash joins the continuation before parsing `set`.
    text = "set -e \\\n  -o pipefail\nx | y\nrc=${PIPESTATUS[0]}\n"
    assert [ln for ln, _ in _viol(text)] == [4]


def test_continuation_inside_an_option_cluster_joins_without_a_space():
    # bash deletes backslash-newline and inserts nothing: this is
    # `set -euo pipefail` (see test_ground_truth_split_cluster_arms_both).
    text = "set -eu\\\no pipefail\nx | y\nrc=${PIPESTATUS[0]}\n"
    assert [ln for ln, _ in _viol(text)] == [4]


def test_workflow_run_block_is_scanned_like_a_script():
    text = ("jobs:\n  a:\n    steps:\n      - run: |\n"
            "          set -euo pipefail\n"
            "          make x | tee log\n"
            "          rc=\"${PIPESTATUS[0]}\"\n")
    assert [ln for ln, _ in _viol(text, ".github/workflows/w.yaml")] == [7]


# --- relaxation (over-rejection direction) ---------------------------------

@pytest.mark.parametrize("relax", [
    "set +e", "set +eu", "set +o errexit", "set +o pipefail",
    # over-rejection direction: the relaxing flag need not come first
    "set -e +o pipefail", "set -o pipefail +e", "set -u +o errexit",
    "  set +e  # relaxed for the loop below", "set +e;",
])
def test_relaxed_file_is_not_flagged(relax):
    text = f"set -euo pipefail\n{relax}\nx | y\nrc=${{PIPESTATUS[0]}}\nset -e\n"
    assert _viol(text) == []


def test_relaxation_must_not_be_matched_as_errexit_on():
    # `set +e` alone contains `e` after a flag sigil; it must not make the
    # file look strict on its own.
    assert not lint.scan_text("x.sh", "set +e\nset -o pipefail\n").strict


@pytest.mark.parametrize("text", [
    "set -e\nx | y\nrc=${PIPESTATUS[0]}\n",            # Actions default: no pipefail
    "set -o pipefail\nx | y\nrc=${PIPESTATUS[0]}\n",   # no errexit
    "set -xo pipefail\nx | y\nrc=${PIPESTATUS[0]}\n",  # -x is not -e
    "set -e -o nounset\nx | y\nrc=${PIPESTATUS[0]}\n",  # -o other than pipefail
])
def test_not_strict_file_is_not_flagged(text):
    assert _viol(text) == []


def test_comment_mentions_are_not_reads():
    # The shape of bench_wrapper.sh after #1820: the guard is gone and only
    # a tombstone comment names PIPESTATUS.
    text = ("set -euo pipefail\n"
            "# ⛔ do not read ${PIPESTATUS[0]} after this pipeline\n"
            "    #   GO_RC=\"${PIPESTATUS[0]}\"\n"
            "go test | tee log\n")
    assert _viol(text) == []


@pytest.mark.parametrize("line", [
    "set -- +e",          # `--` ends options: +e is a positional parameter
    "set - +e",           # so does a lone `-`
    "echo set foo +e",    # `set` is an argument, not the command
    "true  # set +e",     # only in a trailing comment
    "set -o pipefail; +e",  # +e belongs to no `set` after the separator
    "set +x",             # relaxes xtrace, not errexit
    "set +f",             # both exist in the live tree
])
def test_not_a_relaxation_does_not_clear_the_file(line):
    # Miss direction: each of these would silently clear every read in the
    # file if it were read as relaxing. bash ground truth for `set -- +e`:
    # errexit stays on (see test_ground_truth_set_dashdash_keeps_errexit).
    text = f"set -euo pipefail\n{line}\nx | y\nr=${{PIPESTATUS[0]}}\n"
    assert _viol(text)


def test_boundary_relaxation_not_at_line_start_is_not_seen():
    # Documented over-report: a real relaxation that does not start the
    # line is missed on purpose (loud, exemptable), not guessed at.
    text = "set -euo pipefail\nfoo; set +e\nx | y\nr=${PIPESTATUS[0]}\n"
    assert _viol(text)


def test_commented_relaxation_does_not_relax():
    # Miss direction: a `set +e` that only appears in a comment must not
    # clear the file.
    text = "set -euo pipefail\n# set +e here would be wrong\nx | y\nr=${PIPESTATUS[0]}\n"
    assert _viol(text)


def test_commented_flags_do_not_make_a_file_strict():
    text = "# set -euo pipefail\nx | y\nr=${PIPESTATUS[0]}\n"
    assert _viol(text) == []


# --- exemption (both directions) -------------------------------------------

def test_exempted_async_read_is_green():
    res = lint.scan_text("x.sh", _ASYNC.replace(
        "{EXEMPT}", "  # pipestatus-ok: pipeline ran in the background; wait returned 0"))
    assert res.violations == [] and res.exempted == [4]


def test_unexempted_async_read_is_red():
    # The predicate is coarse on purpose: this read IS reachable, and the
    # rule still flags it. The exemption is the exit, not a smarter rule.
    assert [ln for ln, _ in _viol(_ASYNC.replace("{EXEMPT}", ""))] == [4]


@pytest.mark.parametrize("marker", ["# pipestatus-ok:", "# pipestatus-ok:   "])
def test_exemption_with_empty_reason_is_red(marker):
    v = _viol(f"set -euo pipefail\nx | y\nr=${{PIPESTATUS[0]}}  {marker}\n")
    assert v and "empty reason" in v[0][1]


def test_exemption_on_another_line_does_not_count():
    text = ("set -euo pipefail\n# pipestatus-ok: covers the next line\n"
            "x | y\nr=${PIPESTATUS[0]}\n")
    assert _viol(text)


def test_misspelled_exemption_does_not_count():
    assert _viol("set -euo pipefail\nx | y\nr=${PIPESTATUS[0]}  # pipestatus-okay: x\n")


# --- documented boundaries --------------------------------------------------

def test_boundary_line_start_relaxation_in_a_heredoc_clears_the_file():
    # Known miss: the text rule cannot tell a heredoc body from code.
    text = ("set -euo pipefail\ncat <<'EOF'\nset +e\nEOF\n"
            "x | y\nr=${PIPESTATUS[0]}\n")
    assert _viol(text) == []


def test_long_option_cluster_token_is_linear():
    import time
    tok = "e" * 200000 + "!"
    t0 = time.monotonic()
    lint.scan_text("x.sh", f"echo set foo -{tok}\nset +{tok}\n")
    assert time.monotonic() - t0 < 2.0


def test_long_line_with_many_set_words_is_linear():
    import time
    line = ("set " + "x " * 20) * 8000
    t0 = time.monotonic()
    lint.scan_text("x.sh", line + "\n")
    assert time.monotonic() - t0 < 2.0


def test_boundary_relaxation_is_file_level():
    # Known miss: the read after `set -e` is re-armed, but one `set +e`
    # anywhere clears the file.
    text = ("set -euo pipefail\nset +e\na | b\nr1=${PIPESTATUS[0]}\nset -e\n"
            "c | d\nr2=${PIPESTATUS[0]}\n")
    assert _viol(text) == []


def test_boundary_actions_shell_bash_is_invisible():
    # Known miss: `shell: bash` implies -eo pipefail, but no flag is in the text.
    text = ("      - shell: bash\n        run: |\n"
            "          make x | tee log\n          rc=${PIPESTATUS[0]}\n")
    assert _viol(text, ".github/workflows/w.yaml") == []


# --- bash ground truth for the fixtures ------------------------------------

_BASH = shutil.which("bash")


@pytest.mark.skipif(_BASH is None, reason="bash not on PATH — ground truth not measured")
def test_ground_truth_long_flag_read_is_unreachable():
    p = subprocess.run([_BASH, "-c", _STRICT_LONG], capture_output=True, text=True, timeout=30)
    assert p.returncode != 0 and "reached" not in p.stdout


@pytest.mark.skipif(_BASH is None, reason="bash not on PATH — ground truth not measured")
def test_ground_truth_set_dashdash_keeps_errexit():
    p = subprocess.run([_BASH, "-c", "set -e; set -- +e; [[ -o errexit ]] && echo on"],
                       capture_output=True, text=True, timeout=30)
    assert p.stdout.strip() == "on"


@pytest.mark.skipif(_BASH is None, reason="bash not on PATH — ground truth not measured")
def test_ground_truth_split_cluster_arms_both():
    p = subprocess.run([_BASH, "-c", "set -eu\\\no pipefail\n"
                        "[[ -o errexit && -o pipefail ]] && echo both"],
                       capture_output=True, text=True, timeout=30)
    assert p.stdout.strip() == "both"


@pytest.mark.skipif(_BASH is None, reason="bash not on PATH — ground truth not measured")
def test_ground_truth_async_read_is_reachable():
    p = subprocess.run([_BASH, "-c", _ASYNC.replace("{EXEMPT}", "")],
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and "reached" in p.stdout


# --- population + exit codes ------------------------------------------------

def _git_repo(tmp_path: Path, files: dict) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=30)
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, timeout=30)
    return tmp_path


def test_population_covers_scripts_and_workflows_only(tmp_path):
    root = _git_repo(tmp_path, {
        "a/b/run.sh": _STRICT_SHORT,
        ".github/workflows/w.yml": _STRICT_SHORT,
        ".github/workflows/w2.yaml": _STRICT_SHORT,
        "notes.md": _STRICT_SHORT,
        ".github/actions/x/action.yml": _STRICT_SHORT,
    })
    assert lint.enumerate_population(root) == [
        ".github/workflows/w.yml", ".github/workflows/w2.yaml", "a/b/run.sh"]


def test_untracked_file_is_not_in_population(tmp_path):
    root = _git_repo(tmp_path, {"ok.sh": "echo hi\n"})
    (root / "new.sh").write_text(_STRICT_SHORT, encoding="utf-8")
    assert lint.enumerate_population(root) == ["ok.sh"]


def test_rc_1_with_ci_and_0_without(tmp_path, capsys):
    root = _git_repo(tmp_path, {"run.sh": _STRICT_SHORT})
    assert lint.main(["--ci", "--root", str(root)]) == 1
    assert lint.main(["--root", str(root)]) == 0
    assert "run.sh:3" in capsys.readouterr().out


def test_rc_0_on_clean_population(tmp_path, capsys):
    root = _git_repo(tmp_path, {"run.sh": "set -euo pipefail\necho hi\n"})
    assert lint.main(["--ci", "--root", str(root)]) == 0
    assert "population=1 strict_files=1 violations=0" in capsys.readouterr().out


def test_rc_2_when_not_a_git_repo(tmp_path, capsys):
    (tmp_path / "run.sh").write_text(_STRICT_SHORT, encoding="utf-8")
    assert lint.main(["--ci", "--root", str(tmp_path)]) == 2
    assert "cannot measure" in capsys.readouterr().err


def test_rc_2_on_empty_population(tmp_path, capsys):
    root = _git_repo(tmp_path, {"README.md": "x\n"})
    assert lint.main(["--ci", "--root", str(root)]) == 2
    assert "population is empty" in capsys.readouterr().err


def test_rc_2_on_unreadable_tracked_file(tmp_path, capsys):
    root = _git_repo(tmp_path, {"ok.sh": "echo hi\n", "gone.sh": "echo x\n"})
    (root / "gone.sh").unlink()
    assert lint.main(["--ci", "--root", str(root)]) == 2
    assert "gone.sh" in capsys.readouterr().err


def test_json_output_lists_violations(tmp_path, capsys):
    import json
    root = _git_repo(tmp_path, {"run.sh": _STRICT_SHORT})
    assert lint.main(["--json", "--root", str(root)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["population"] == 1 and data["violations"][0]["line"] == 3


def test_live_repo_is_measured_and_clean(capsys):
    assert lint.main(["--ci"]) == 0
    out = capsys.readouterr().out
    # instrument alive: something was scanned and something was strict
    assert "population=0" not in out and "strict_files=0" not in out
