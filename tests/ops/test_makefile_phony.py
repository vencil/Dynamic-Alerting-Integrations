#!/usr/bin/env python3
"""Every target whose recipe comes from this repo's `Makefile` is `.PHONY`.

Why this hurts
--------------
None of this repo's targets produce a file named after themselves. Without
`.PHONY`, a file of that name sitting in the working directory makes make
decide the target is already up to date: it prints `'<t>' is up to date.`,
runs **not one line** of the recipe, and exits **rc 0**. The failure direction
is a GREEN LIGHT — the caller cannot tell "the gate ran and passed" from "the
gate never ran". A skipped gate also leaves the PREVIOUS artifact in place, so
the step after it succeeds on stale bytes.

`.build/`, `dist/`, a stray `touch`, a tab-completion typo, an unzipped
artifact — anything can put that file there, and the targets #1929 found this
way include gates whose whole job is to say no: `sharded-assemble`, which
writes the `.build/config-dir` the exporter reads and carries the #1794
tenant-uniqueness question asked *before* assembly; `version-check`, the
version/count gate `make pre-tag` runs (and `make pre-tag` does not run
pytest, so nothing else stands between a drifted version reference and a
pushed tag); `sharded-check`, `assembler-render`, `validate-config` and
`validate-routes`, all "refuse before you ship" steps.

The predicate is asked of make's own rule database rather than of the Makefile
text, and names no target: this file's predecessor asserted `.PHONY` on one
target name, which is `D-05`'s "enumeration of syntax" — it sees only the
violation someone already found.

⚠️ SCOPE — what this does NOT watch. Disclosed, not handled:

* **recipe-less targets** (`all: a b c`). make never says which file they came
  from, so they cannot enter the population.
* **pattern rules** (`%.o:`, and parameterised entrypoints like `validate-%:`).
  ⚠️ NOT because they are safe: a pattern entrypoint has the SAME green-light
  defect. Measured on `validate-%:` — with no file, `make validate-foo` runs
  the recipe; after `touch validate-foo` it prints `is up to date.` at rc 0.
  What differs is the FIX: `.PHONY` does nothing for a pattern rule (measured:
  still `is up to date.`), and the repair is a `FORCE` prerequisite (measured:
  the recipe runs again). One defect, two repairs — a guard whose message is
  "add `.PHONY`" would be giving advice that cannot work here. ⛔ Do not fold
  them in; if this Makefile grows pattern entrypoints, that is its own guard.
  ⚠️ `cov\\%:` is NOT a pattern rule — an escaped `%` is an ordinary target
  named `cov%`, and it IS watched.
* **old-style suffix rules** (`.c.o:`) — the one case that lands the WRONG
  way. make turns each into a pattern rule *plus* a literal `.c.o` node under
  `# Files`, carrying a recipe from this Makefile, so it enters the population
  and gets reported (measured). `.PHONY` is not the answer for one of those.
  ⛔ Disclosed rather than special-cased: this Makefile has no suffix rules,
  and a second classifier costs more than the class is worth. If one is ever
  added, fix the population here — do not add `.PHONY` to it.
* **targets from an included makefile**, which report their own file name and
  so fall outside a population defined by this `Makefile`.

⚠️ This is NOT the pre-commit hook `makefile-targets-check`
(`scripts/tools/lint/check_makefile_targets.py`). That one also reads the real
Makefile, but it asks whether `scripts/tools/dx/` tools are reachable from an
automation entry point; it says nothing about `.PHONY`.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# make's own markers, as `--print-data-base` writes them. Both use TWO spaces
# after the `#`; the one-space `# makefile (from 'Makefile', line N)` that
# opens a target-specific-variable block is a DIFFERENT marker and must not be
# mistaken for a recipe.
_RECIPE_FROM = re.compile(
    r"^#  recipe to execute \(from '(?P<file>[^']+)', line \d+\):$")
_PHONY_MARK = "#  Phony target (prerequisite of .PHONY)."
# The rule line inside a block: target names, then `:` — but not `:=`, which is
# a variable assignment.
_RULE_LINE = re.compile(r"^(?P<names>[^\s:#=][^:#=]*?)\s*:(?!=)")
# make groups its database into named sections, emitted in a fixed order;
# real targets are under `# Files`, patterns under the earlier
# `# Implicit Rules`. ⚠️ `# Files` is the LAST line of the preceding
# statistics paragraph, not a paragraph of its own.
_SEC_IMPLICIT = "# Implicit Rules"
_SEC_FILES = "# Files"

needs_make = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("make") is None,
    reason="needs GNU make to answer for its own rule database")


class CouldNotMeasure(AssertionError):
    """Raised instead of returning `[]`. An empty violation list and an empty
    population look identical to a caller, and only one of them is good news.
    """


def _print_data_base(where: Path) -> str:
    """make's rule database for the `Makefile` in `where`. Runs no recipe."""
    r = subprocess.run(
        ["make", "--print-data-base", "--question"],
        cwd=where, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300)
    # `--question` exits 1 for "would need remaking", which is the normal
    # answer for a tree full of phony targets. 2 is a real make error.
    if r.returncode not in (0, 1):
        raise CouldNotMeasure(
            f"make exited {r.returncode} for the tree at {where} and printed "
            f"no usable database, so NOTHING was measured. What make said:\n"
            f"  {r.stderr.strip()}\n"
            f"⚠️ Read that line before assuming the Makefile is malformed. "
            f"`--question` evaluates the default goal, so a perfectly valid "
            f"Makefile whose default goal has a MISSING PREREQUISITE fails "
            f"here too ('No rule to make target …, needed by …'), and the "
            f"problem is that prerequisite, not the syntax.")
    return r.stdout


def _parse_db(db: str, makefile: str = "Makefile") -> list[str]:
    """Split from `_phony_violations` so that a database make would never
    produce — a renamed section header — can be handed in directly.
    """
    all_lines = db.splitlines()
    if _SEC_FILES not in all_lines:
        raise CouldNotMeasure(
            f"make's database has no {_SEC_FILES!r} section header, so this "
            f"parse cannot tell a real target from a pattern rule and every "
            f"verdict it could return would be guesswork. Most likely make "
            f"renamed the header. NOTHING was checked; this is not a clean "
            f"tree.")
    # ⛔ Membership is a POSITION taken once, not running state. A
    # target-specific variable whose value is a multi-line `define` prints
    # arbitrary text INTO the `# Files` section, so any state machine fed by
    # "does this line read `# Implicit Rules`" can be pushed out of the
    # section it is standing in and never pushed back — silently, because the
    # blocks counted before it keep the empty-population check from firing.
    # A stray header cannot move a fixed offset.
    # ⚠️ The same text placed EARLIER only widens the population (pattern
    # rules start being judged), which surfaces as a red, not as a miss.
    ours: dict[str, bool] = {}
    for para in "\n".join(all_lines[all_lines.index(_SEC_FILES) + 1:]).split("\n\n"):
        lines = para.splitlines()
        src = next((m.group("file") for m in
                    (_RECIPE_FROM.match(ln) for ln in lines) if m), None)
        if src != makefile:
            continue
        phony = _PHONY_MARK in lines
        rule = next((m for m in
                     (_RULE_LINE.match(ln) for ln in lines) if m), None)
        if rule is None:
            raise CouldNotMeasure(
                "a rule block says its recipe came from "
                f"'{makefile}' but no target name could be read out of it, so "
                "that target is invisible to this guard:\n" + "\n".join(lines[:8]))
        for name in rule.group("names").split():
            ours[name] = ours.get(name, True) and phony
    if not ours:
        raise CouldNotMeasure(
            f"no rule block in make's database claims a recipe from "
            f"'{makefile}'. Either the makefile defines no targets, or the "
            f"marker this parse matches — {_RECIPE_FROM.pattern!r} — no longer "
            f"matches what make prints. Both mean NOTHING was checked; this is "
            f"not a clean tree.")
    return sorted(name for name, phony in ours.items() if not phony)


def _phony_violations(where: Path, makefile: str = "Makefile") -> list[str]:
    """Targets defined in `<where>/<makefile>` that make does NOT call phony.

    The production predicate. Every control group below calls this same
    function: one that ran a second copy of the logic would prove nothing
    about the copy that ships.
    """
    return _parse_db(_print_data_base(where), makefile)


@needs_make
def test_every_target_defined_in_this_makefile_is_phony() -> None:
    violations = _phony_violations(REPO)
    assert not violations, (
        f"{len(violations)} target(s) defined in the Makefile are not "
        f"declared `.PHONY`: {', '.join(violations)}.\n"
        "⛔ The failure direction is a GREEN LIGHT, not a red one. None of "
        "these targets creates a file named after itself, so a file of that "
        "name in the working directory (a `touch`, an unpacked artifact, a "
        "directory someone made by hand) makes make answer `'<target>' is up "
        "to date.` and exit rc 0 WITHOUT RUNNING ONE LINE of the recipe. A "
        "caller — CI, `make pre-tag`, a customer following the docs — cannot "
        "distinguish that from the gate having run and passed, and whatever "
        "artifact the previous run left behind is what the next step "
        "consumes.\n"
        "Fix: add `.PHONY: <target>` on the line above each one, which is how "
        "the other targets in this Makefile already do it.\n"
        "⚠️ UNLESS the target genuinely BUILDS a file of its own name (a real "
        "file target such as `dist/app.bin:`). `.PHONY` on one of those is "
        "actively harmful — it turns off the up-to-date check that is the "
        "whole point of the rule, so it rebuilds every single time. If that is "
        "what you are looking at, the thing to change is THIS GUARD's "
        "population (which targets it considers its business), not the "
        "Makefile."
    )


@needs_make
def test_a_target_without_a_phony_declaration_is_reported(tmp_path) -> None:
    """Both arms on purpose: a predicate that flags everything would make the
    repo arm above red for the wrong reason."""
    unguarded = tmp_path / "unguarded"
    unguarded.mkdir()
    (unguarded / "Makefile").write_text(
        "bare-target:\n\t@echo ran\n", encoding="utf-8")
    assert _phony_violations(unguarded) == ["bare-target"]

    guarded = tmp_path / "guarded"
    guarded.mkdir()
    (guarded / "Makefile").write_text(
        ".PHONY: bare-target\nbare-target:\n\t@echo ran\n", encoding="utf-8")
    assert _phony_violations(guarded) == []


@needs_make
def test_a_pattern_rule_is_not_reported(tmp_path) -> None:
    """A false red on advice that cannot work (`.PHONY` does not repair a
    pattern rule — see SCOPE) is how a guard gets deleted by the next
    person."""
    pat = tmp_path / "pattern"
    pat.mkdir()
    (pat / "Makefile").write_text(
        ".PHONY: %.probe\n%.probe:\n\t@echo hi\n\nreal:\n\t@echo real\n"
        ".PHONY: real\n", encoding="utf-8")
    assert _phony_violations(pat) == []

    # ...and the ordinary target in the SAME tree is still judged, so this
    # cannot be a blanket "stop looking at this file".
    (pat / "Makefile").write_text(
        ".PHONY: %.probe\n%.probe:\n\t@echo hi\n\nreal:\n\t@echo real\n",
        encoding="utf-8")
    assert _phony_violations(pat) == ["real"]


@needs_make
def test_an_escaped_percent_target_is_still_judged(tmp_path) -> None:
    """`cov\\%:` is an escaped percent: an ordinary target literally named
    `cov%`, carrying the full defect — `touch 'cov%'` makes `make 'cov%'`
    answer `is up to date.` at rc 0 — and `.PHONY: cov%` repairs it.

    ⚠️ The two spellings are not interchangeable and this fixture depends on
    that: the target must be written `cov\\%:` (escaped) while the `.PHONY`
    prerequisite must be written `cov%` (bare). Measured — `.PHONY: cov\\%`
    does NOT mark it phony, and make still skips the recipe.
    """
    esc = tmp_path / "escaped"
    esc.mkdir()
    (esc / "Makefile").write_text(
        "cov\\%:\n\t@echo covran\n\nreal:\n\t@echo real\n.PHONY: real\n",
        encoding="utf-8")
    assert _phony_violations(esc) == ["cov%"]

    (esc / "Makefile").write_text(
        "cov\\%:\n\t@echo covran\n.PHONY: cov%\n\nreal:\n\t@echo real\n"
        ".PHONY: real\n", encoding="utf-8")
    assert _phony_violations(esc) == []


@needs_make
def test_a_stray_section_header_cannot_silence_the_guard(tmp_path) -> None:
    """A target-specific variable whose value is a multi-line `define` puts
    arbitrary text into the `# Files` section of the database, including a
    line that reads exactly `# Implicit Rules`.

    ⚠️ Do not shrink this fixture. make does not emit blocks in Makefile
    order, so whether the old bug showed up as a silent `[]` or as a raise
    depended on how many blocks happened to be counted before the stray
    header — a small tree raises, which LOOKS like fail-closed and hides it.
    """
    tree = tmp_path / "sneak"
    tree.mkdir()
    body = ["define EXTRA", "a", _SEC_IMPLICIT, "endef", "",
            "inj: EXTRA := $(EXTRA)", "inj: ; @echo inj", ""]
    body += [ln for i in range(40)
             for ln in (f"t{i:02d}: ; @echo t{i:02d}", f".PHONY: t{i:02d}")]
    body += ["", "BADTARGET: ; @echo bad", ""]
    (tree / "Makefile").write_text("\n".join(body), encoding="utf-8")

    # `BADTARGET` has the real defect; it must not be lost behind the header.
    assert _phony_violations(tree) == ["BADTARGET", "inj"]


@needs_make
def test_a_block_whose_target_name_cannot_be_read_is_not_skipped() -> None:
    """The remaining fail-closed arm. A block that claims a recipe from our
    Makefile but whose rule line does not parse must stop the run, because
    skipping it would drop a target out of the population without a word."""
    db = "\n".join([
        _SEC_FILES, "",
        "ok:", "#  recipe to execute (from 'Makefile', line 1):", "\t@echo ok",
        "", ":", "#  recipe to execute (from 'Makefile', line 2):", "\t@echo x",
        ""])
    with pytest.raises(CouldNotMeasure, match="no target name"):
        _parse_db(db)


@needs_make
def test_a_database_without_the_files_header_is_not_reported_as_clean(
        tmp_path) -> None:
    """⚠️ Reached by taking a REAL database from make and renaming the header
    in it, so the arm is exercised without owning a make that renames it."""
    tree = tmp_path / "hdr"
    tree.mkdir()
    (tree / "Makefile").write_text("bare:\n\t@echo hi\n", encoding="utf-8")
    db = _print_data_base(tree)
    assert _SEC_FILES in db.splitlines(), "fixture no longer exercises the arm"
    assert _parse_db(db) == ["bare"]

    drifted = db.replace("\n" + _SEC_FILES + "\n", "\n# Filez\n")
    assert _SEC_FILES not in drifted.splitlines()
    with pytest.raises(CouldNotMeasure, match="section header"):
        _parse_db(drifted)


@needs_make
def test_a_makefile_make_itself_cannot_read_is_not_reported_as_clean(
        tmp_path) -> None:
    """⛔ THIS TEST DOES NOT DISCRIMINATE the return-code check it looks like
    it covers. Measured: disabling that check leaves this green, because make
    printed no database either way and the empty-population check raises the
    same exception one step later. What is pinned here is the BEHAVIOUR
    (unreadable makefile ⇒ raise, never `[]`); which arm delivers it is pinned
    by nothing. The return-code check earns its place by putting make's stderr
    in the message, not by catching a class the other arm would miss.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "Makefile").write_text("VAR := 1\n", encoding="utf-8")
    with pytest.raises(CouldNotMeasure):
        _phony_violations(empty)


@needs_make
def test_an_empty_population_is_not_reported_as_clean(tmp_path) -> None:
    """The arm a wording drift in make's output would take: if the recipe
    marker is ever renamed, every block stops matching and a predicate that
    returned `[]` would report a clean tree forever.

    Reached by asking about a makefile name make never printed, which leaves
    make working normally and the parse succeeding — the same shape a rename
    produces, without waiting for one.
    """
    tree = tmp_path / "live"
    tree.mkdir()
    (tree / "Makefile").write_text(
        ".PHONY: real\nreal:\n\t@echo real\n", encoding="utf-8")
    assert _phony_violations(tree) == []
    with pytest.raises(CouldNotMeasure, match="no longer"):
        _phony_violations(tree, makefile="Makefile.nope")
