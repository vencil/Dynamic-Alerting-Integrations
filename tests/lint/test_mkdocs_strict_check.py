#!/usr/bin/env python3
"""test_mkdocs_strict_check — the anchor-debt ledger in
`scripts/tools/lint/mkdocs_strict_check.sh` must survive a host whose locale
codec is not UTF-8.

Why these tests exist
---------------------
mkdocs is a Python program. Through CPython 3.14 it encodes stdout/stderr with
the *locale* encoding, so on a zh-TW Windows host every CJK anchor name it
reports reaches `mkdocs-build.log` as cp950/Big5 bytes while the committed
ledger is UTF-8. `comm` compares bytes, so the gate reported 214 of 240 ledgered
lines as fixed and 214 fresh ones as broken — and told the author to run
`--write-baseline`, which would have committed the mojibake and dropped 214 real
anchor debts. Measured on this repo, 2026-08-09.

⛔ Read this before adding a case: **a build-and-assert test cannot catch this
class on CI.** CI is Linux with a UTF-8 locale, so the buggy and the fixed script
produce byte-identical logs there; such a test is green either way. Same trap as
the CRLF regression in #1363. Every case below therefore drives the real script
with a **stub `mkdocs` on PATH** whose bytes the test chooses, in a throwaway
tree — which makes each one discriminating on Linux and Windows alike.

The throwaway tree matters for a second reason: `--write-baseline` writes to
`$SCRIPT_DIR/mkdocs-anchor-debt.txt`. Pointing these tests at the real script in
place would let a regression overwrite the committed ledger.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
# Spelled as one literal each, not joined part-by-part: verify_diff.py maps a
# changed file to the tests that cover it by scanning test sources for repo path
# strings, and a path assembled from fragments is invisible to that scan — the
# script could then be edited without this file being selected to run.
_REAL_SCRIPT = _REPO_ROOT / "scripts/tools/lint/mkdocs_strict_check.sh"
_REAL_LEDGER = _REPO_ROOT / "scripts/tools/lint/mkdocs-anchor-debt.txt"

_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    _BASH is None, reason="mkdocs_strict_check.sh is a bash script; bash not on PATH"
)

# The stub stands in for mkdocs: it records the environment it was handed (so a
# test can assert the *child* really saw the UTF-8 pin, rather than grepping the
# script's source for an env assignment that might sit in a comment or run after
# the build) and then replays a caller-chosen byte sequence as its build output.
_STUB = b"""#!/usr/bin/env bash
printf 'PYTHONIOENCODING=%s\\nPYTHONUTF8=%s\\n' \\
    "${PYTHONIOENCODING:-<unset>}" "${PYTHONUTF8:-<unset>}" > "$STUB_ENV_OUT"
cat "$STUB_LOG_SRC"
"""

_MINIMAL_MKDOCS_YML = b"""\
# Minimal stand-in for the repo's mkdocs.yml. The script only reads it to
# confirm anchor validation is still switched on before it trusts a debt figure.
validation:
  links:
    anchors: warn
"""


# ── helpers ────────────────────────────────────────────────────────────


def _ledger_sample(count: int = 6, *, non_ascii: bool = True) -> list[str]:
    """Real ledger lines, so the fixtures track the ledger's actual shape
    instead of a hand-written imitation of it that can drift away."""
    lines = _REAL_LEDGER.read_text(encoding="utf-8").splitlines()
    if non_ascii:
        lines = [ln for ln in lines if not ln.isascii()]
        if not lines:
            pytest.skip("ledger has no non-ASCII lines; nothing to mis-encode")
    return lines[:count]


class _Tree:
    """A throwaway repo root holding a copy of the script under test."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.script = root / "scripts" / "tools" / "lint" / "mkdocs_strict_check.sh"
        self.ledger = root / "scripts" / "tools" / "lint" / "mkdocs-anchor-debt.txt"
        self.log = root / "run.log"
        self.env_seen = root / "stub-env.txt"
        self._bin = root / "bin"

    def build(self) -> None:
        self.script.parent.mkdir(parents=True, exist_ok=True)
        self._bin.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_REAL_SCRIPT, self.script)
        (self.root / "mkdocs.yml").write_bytes(_MINIMAL_MKDOCS_YML)
        stub = self._bin / "mkdocs"
        stub.write_bytes(_STUB)
        os.chmod(stub, 0o755)

    def run(self, log_bytes: bytes, *args: str) -> subprocess.CompletedProcess[bytes]:
        (self.root / "build-output.bin").write_bytes(log_bytes)
        env = dict(os.environ)
        env["PATH"] = str(self._bin) + os.pathsep + env.get("PATH", "")
        env["MKDOCS_LOG"] = str(self.log)
        env["STUB_LOG_SRC"] = str(self.root / "build-output.bin")
        env["STUB_ENV_OUT"] = str(self.env_seen)
        # Hostile inheritance on purpose: if the script did NOT export its own
        # pin, the child would see these and the encoding assertion below would
        # be measuring pytest's environment rather than the script's behaviour.
        env["PYTHONIOENCODING"] = "cp950"
        env["PYTHONUTF8"] = "0"
        # bytes, not text=True: half these fixtures are deliberately not UTF-8,
        # and letting Python decode them with the locale codec would reintroduce
        # the very bug under test into the harness.
        return subprocess.run(
            [_BASH, str(self.script), *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            # The stub returns immediately, so this only ever fires if the
            # script blocks on something it should not be waiting for.
            timeout=120,
        )


@pytest.fixture()
def tree(tmp_path: Path) -> _Tree:
    t = _Tree(tmp_path / "fake-repo")
    t.build()
    return t


def _out(proc: subprocess.CompletedProcess[bytes]) -> str:
    return (proc.stdout + proc.stderr).decode("utf-8", errors="replace")


# ── the encoding fix ───────────────────────────────────────────────────


def test_mkdocs_child_is_handed_utf8_stdio(tree: _Tree) -> None:
    """The fix itself, asserted where it has to hold: in the child's env.

    Counterfactual: dropping either export from the script turns this red on
    every platform, which is the point — the effect it prevents is invisible on
    CI, so the guard cannot be allowed to depend on the host locale either.
    """
    lines = _ledger_sample()
    body = "\n".join(lines).encode("utf-8") + b"\n"
    tree.ledger.write_bytes(body)
    tree.run(body)

    seen = dict(
        line.split("=", 1)
        for line in tree.env_seen.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    # Normalised, not string-equal: `utf8`, `UTF-8` and `utf-8:backslashreplace`
    # are all correct answers, and pinning the literal would fail a harmless
    # future edit while still passing a broken one that spells cp950 oddly.
    io_enc = seen["PYTHONIOENCODING"].split(":", 1)[0].replace("-", "").lower()
    assert io_enc == "utf8", f"child saw PYTHONIOENCODING={seen['PYTHONIOENCODING']!r}"
    assert seen["PYTHONUTF8"] == "1", f"child saw PYTHONUTF8={seen['PYTHONUTF8']!r}"


def test_locale_encoded_log_is_rejected_not_read_as_a_ledger_change(
    tree: _Tree,
) -> None:
    """cp950 shape: the log transcodes to bytes that are not UTF-8.

    Before the fix this is what the Windows host actually got, and the script
    answered `ANCHOR_DEBT_LEDGER=grew` — an accusation about links the author
    never touched. The gate must refuse to compare instead.
    """
    if shutil.which("iconv") is None:
        pytest.skip("iconv absent; the script documents this half as warn-only")
    lines = _ledger_sample()
    tree.ledger.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    mangled = ("\n".join(lines) + "\n").encode("cp950", errors="backslashreplace")

    # Guard the fixture, not just the outcome: if this ever became valid UTF-8
    # the test would pass for the wrong reason and stop testing anything.
    with pytest.raises(UnicodeDecodeError):
        mangled.decode("utf-8")

    proc = tree.run(mangled)
    out = _out(proc)
    assert proc.returncode == 1, out
    assert "COLLECTOR=log-not-utf8" in out, out
    assert "ANCHOR_DEBT_LEDGER=" not in out, (
        "the gate reported a ledger delta computed from bytes it could not "
        f"read:\n{out}"
    )


def test_backslashreplace_log_is_rejected(tree: _Tree) -> None:
    """cp1252 shape: the codec cannot represent CJK at all, so Python's stderr
    `backslashreplace` substitutes literal `\\uXXXX`.

    The bytes are valid UTF-8, so the iconv half above is blind to this one; it
    needs its own check, and this test is what keeps that second check honest.
    """
    lines = _ledger_sample()
    tree.ledger.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    escaped = ("\n".join(lines) + "\n").encode("ascii", errors="backslashreplace")
    escaped.decode("utf-8")  # valid UTF-8 — that is the whole difficulty

    proc = tree.run(escaped)
    out = _out(proc)
    assert proc.returncode == 1, out
    assert "COLLECTOR=log-escaped" in out, out
    assert "ANCHOR_DEBT_LEDGER=" not in out, out


def test_write_baseline_refuses_a_locale_encoded_log(tree: _Tree) -> None:
    """The catastrophic path: `--write-baseline` on a mis-encoded log would
    commit mojibake AND silently discard every real anchor debt it overwrote.

    Ordering is the whole content of this test — the integrity checks are only
    worth anything if they run *before* write mode, which is where a future
    edit is most likely to put something back.
    """
    if shutil.which("iconv") is None:
        pytest.skip("iconv absent; the script documents this half as warn-only")
    lines = _ledger_sample()
    original = ("\n".join(lines) + "\n").encode("utf-8")
    tree.ledger.write_bytes(original)
    mangled = original.decode("utf-8").encode("cp950", errors="backslashreplace")

    proc = tree.run(mangled, "--write-baseline")
    out = _out(proc)
    # Content before exit code, deliberately: the corruption is the finding, and
    # a failure here should say so rather than report an exit status.
    assert tree.ledger.read_bytes() == original, (
        "--write-baseline overwrote the ledger with output it had not "
        f"validated:\n{out}"
    )
    assert proc.returncode == 1, out


def test_a_binary_looking_log_does_not_silence_the_collector(tree: _Tree) -> None:
    """grep prints "binary file matches" to stderr and nothing to stdout for a
    file it deems binary, which turns the extraction into an empty set — and an
    empty set reads as "every ledgered anchor was fixed, refresh the baseline".

    Found by running the cp950 case on glibc, where that is exactly what the
    first version of this guard did. A NUL byte is used here rather than a
    locale-dependent encoding error so the case means the same thing on both
    platforms; the log is otherwise valid UTF-8, so the encoding checks stay out
    of the way and this measures the extraction alone.
    """
    lines = _ledger_sample()
    body = ("\n".join(lines) + "\n").encode("utf-8")
    tree.ledger.write_bytes(body)

    proc = tree.run(body + b"INFO    -  build finished\x00\n")
    out = _out(proc)
    assert "ANCHOR_DEBT_LEDGER=shrank" not in out, (
        "the collector returned nothing and the gate read that as progress:\n"
        f"{out}"
    )
    assert proc.returncode == 0, out


# ── the ledger round-trip ──────────────────────────────────────────────


def test_write_baseline_output_validates_on_the_very_next_run(tree: _Tree) -> None:
    """A ledger written from a log must accept that same log unchanged.

    It did not: `--write-baseline` deduped while the reader diffs the raw
    stream, so a page linking one missing anchor twice contributed two lines to
    the run and one to the ledger. On this repo that was 240 -> 188, and the
    next run failed with "52 NEW anchor problem(s)" having changed nothing. The
    remedy the failure message hands you has to work.
    """
    dup = (
        "WARNING -  Doc file 'a.md' contains a link '#x', "
        "but there is no such anchor on this page.\n"
    )
    other = (
        "WARNING -  Doc file 'b.md' contains a link '#y', "
        "but there is no such anchor on this page.\n"
    )
    log = (dup + dup + other).encode("utf-8")

    written = tree.run(log, "--write-baseline")
    assert written.returncode == 0, _out(written)
    assert tree.ledger.read_bytes().count(b"a.md") == 2, (
        "the ledger dropped a repeated warning; the reader counts it twice, so "
        "this file can never validate the run it came from"
    )

    proc = tree.run(log)
    out = _out(proc)
    assert proc.returncode == 0, out
    assert "MKDOCS STRICT STATUS=PASS" in out, out


# ── controls: these were green before the fix too ──────────────────────


def test_committed_ledger_replayed_verbatim_passes(tree: _Tree) -> None:
    """Control (green before the fix). Guards the other direction: none of the
    new integrity checks may reject a legitimate, CJK-heavy UTF-8 run."""
    body = _REAL_LEDGER.read_bytes()
    tree.ledger.write_bytes(body)
    proc = tree.run(body)
    out = _out(proc)
    assert proc.returncode == 0, out
    assert "MKDOCS STRICT STATUS=PASS" in out, out


def test_a_genuinely_new_anchor_problem_still_fails(tree: _Tree) -> None:
    """Control (green before the fix). The checks added above the comparison
    must not short-circuit the gate's actual job."""
    lines = _ledger_sample()
    tree.ledger.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    intruder = (
        "WARNING -  Doc file 'brand-new.md' contains a link '#nope', "
        "but there is no such anchor on this page."
    )
    log = ("\n".join([*lines, intruder]) + "\n").encode("utf-8")

    proc = tree.run(log)
    out = _out(proc)
    assert proc.returncode == 1, out
    assert "ANCHOR_DEBT_LEDGER=grew" in out, out
    assert "brand-new.md" in out, out


def test_committed_ledger_contains_no_backslash_escapes() -> None:
    """Control (green before the fix). The `\\uXXXX` check would misfire on a
    ledger line that legitimately contained that text; nothing does today, and
    this is what says so out loud if that ever changes."""
    body = _REAL_LEDGER.read_text(encoding="utf-8")
    assert "\\" not in body, (
        "a ledgered anchor now contains a backslash; re-check the COLLECTOR="
        "log-escaped pattern in mkdocs_strict_check.sh before trusting it"
    )
