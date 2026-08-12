"""A file reference must not be split across a line break (#1378).

A path written in a comment can wrap mid-token:

    # ... see docs/integration/example-long-guide-
    # name.md §3.1

`git grep docs/integration/example-long-guide-name.md` then returns NOTHING for
that file. The reference exists, is correct, and is invisible.

(That illustration uses a path that does not exist — with a real one the guard
flags its own docstring, which is how the first run of it went.)

⛔ Why that is worse than it sounds: the answer you get is "clean", not
"unknown". #1373 renamed a guard module and swept for references with exactly
that grep; one reference had wrapped mid-identifier, the sweep reported the tree
clean, and a pointer to a now-deleted file shipped in the PR. It was caught by
an external reviewer, not by the sweep. The fix landed as a prose ⛔ note next to
the one line involved — which protects one filename in one place, so this guard
replaces it with the derivable rule.

THE RULE: rejoin comment continuations; every path-like token that then names a
real repo file must ALSO appear contiguously in the raw text.

⛔ The metric is defined HERE, not in a ticket. Four different counts of "how
many wrapped references exist" were produced while scoping this (16 / 28 / 35 /
44) — every one honest, every one measuring something slightly different
(full-path only, plus bare names, plus more extensions, minus a regex bug).
A number that needs its definition restated to mean anything is not a baseline.
Since the tree is now at zero, the definition below IS the number.

⛔ KNOWN GAP, hit while writing this: a path split across two STRING LITERALS is
invisible here, because the quotes sit between the halves and break the token —

    "# ... （components/threshold-exporter/",
    "# app/pkg/config/resolve.go）。",

That is `scripts/tools/ops/_registry_lib.py`, which GENERATES the comment blocks
spliced into `rule-packs/*.yaml`. This guard saw the six generated outputs and
not their source, so fixing only the outputs was silently reverted by
`check_threshold_registry.py --regen` — the repo's own staleness gate is what
caught it. If you are unwrapping a path and the gate keeps coming back, look
for a generator.

⛔ That class was swept to ZERO by hand and is NOT held there by anything. The
sweep: for each adjacent pair of lines where the first ends in a quote and the
second starts with one, splice them and ask the same question this guard asks.
It found 7 (`_registry_lib.py` ×2, `waveform_score.py` ×2,
`check_devcontainer_dep_parity.py`, `check_image_pin_capability.py`,
`test_check_scrape_reachability.py`); all 7 are fixed. Two of them sat two lines
apart, and the first pass caught only one — the number is recorded here because
"known gap" without a size reads as "handled". Re-run that sweep when it
matters; nothing will tell you.

What is deliberately NOT modelled (under-detection, the safe direction):
  * a token split across two string literals — see the gap above;
  * a token split across THREE or more lines — the window is two lines;
  * a reference assembled from variables, or reached through a glob;
  * a comment marker outside the five `LINE_PREFIX` knows (`>` blockquote,
    `::`/`REM`, `%`, `!`) — blind review demonstrated every one of them;
  * a RELATIVE spelling (`./x.md`, `../x.md`, `/x.md`): the token comes out
    with the prefix attached and `_resolves` compares exactly, so it does not
    match a tracked path. Also blind review;
  * a bare filename that is not repo-unique, shorter than `_MIN_BARE_NAME`, or
    containing neither `_` nor `-` — that last condition alone excludes 129
    otherwise-unique names, and all three are here to keep `utils.py` from
    flooding the scan;
  * binary content is read too, decoded with replacement — nothing is
    skipped, because silently dropping input is how a scan reports "clean"
    while never having looked (and pinning WHICH files get dropped turned out
    to be environment-dependent; see the note by `_read_tracked`).

⚠️ SELECTION, not coverage: this guard reads the whole tree, but
`verify_diff.py` maps it through `text_map`, i.e. only the handful of paths it
happens to NAME. Wrap a path somewhere else and a local `verify_diff --base
origin/main` will not pick this test. That is a dev-loop latency cost, not a
hole in the same sense: the pre-commit hook only runs `verify_diff --check`
(map freshness), never selection, and CI's `python-tests-run` runs `pytest
tests/` in full. The rules schema cannot express "any tracked file" —
`always_run` takes a single `trigger` (`verify_diff.py` validates it) — so
widening selection would mean changing that schema. Raised by CodeRabbit
on #1383.

⛔ FIXED, and worth keeping the shape written down. `python-tests-run` used to
be genuinely path-gated while 101 tracked files fell outside ci.yml's `python`
filter, so a PR touching only such a file did not run this guard at all.
⚠️ The required check `Python Tests (3.13)` is reported by the always-run
aggregate, not by the skipped leg — so it reported SUCCESS, never `skipped`.
The defect was never "a skipped required check satisfies branch protection";
it was `detect-changes` answering "not needed" for a suite that was needed.
Same #1368 shape, one level up.

⛔ The fix is a catch-all rather than "add those 101 paths" because of this
module: it reads the WHOLE tracked tree, so no filter NARROWER than everything
can express its input set — any such filter is a guess that is wrong by
construction. ci.yml's `python` filter now carries `**`, pinned by
`test_python_tests_run_cannot_be_path_skipped`; the enumerated entries stay
beside it and stay machine-checked, because the guard asks its coverage
questions against the enumerated view only.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Longest extension in the repo worth treating as one. Six covers `.config`
# and everything shorter; beyond that the tail is dotted version fragments.
_MAX_EXTENSION = 6


@lru_cache(maxsize=1)
def _extensions() -> tuple[str, ...]:
    """Every extension the tree actually uses, LONGEST FIRST.

    ⛔ Derived, not listed, for two reasons that both bit this module:

    1. A hand-written list omits what the repo already has. The first version
       missed `png`, so a wrapped reference to one of the tracked `.png`
       baselines would have passed. Caught in review, not by any test here.
    2. A hand-written list has to be ORDERED by hand, and Python's `|` is
       first-match, not longest-match: with `js` before `jsx`, `foo.jsx` matches
       as `foo.js` — and because the truncated name is often a real build
       artefact, the scan reports a reference to the WRONG file, plausibly
       enough to survive a full read-through. It did. Sorting by length
       descending makes that bug structurally impossible rather than pinned.
    """
    found = set()
    for path in _tracked():
        name = path.rsplit("/", 1)[-1]
        if "." not in name:
            continue
        ext = name.rsplit(".", 1)[-1]
        if ext.isalnum() and 0 < len(ext) <= _MAX_EXTENSION:
            found.add(ext.lower())
    return tuple(sorted(found, key=lambda e: (-len(e), e)))


@lru_cache(maxsize=1)
def _extension_token() -> re.Pattern[str]:
    return re.compile(
        r"[A-Za-z0-9_.\-/]+\.(?:" + "|".join(_extensions()) + r")(?![A-Za-z0-9])")


@lru_cache(maxsize=1)
def _extensionless_token() -> re.Pattern[str]:
    """The tracked paths an extension-anchored pattern can never produce.

    ⛔ `Makefile`, `Dockerfile` ×8, `LICENSE`, every `.gitignore`-family file,
    `components/da-tools/app/VERSION`, `scripts/hooks/commit-msg`,
    `tests/rulepacks/vm_engine_version` — 43 tracked files whose basename has no
    usable extension. The first version required `.<ext>`, so none of them was
    ever a token: not "the resolver let it through", but "the tokeniser never
    saw it". A live violation was sitting in `ci.yml` the whole time
    (`tests/rulepacks/` / `vm_engine_version`, wrapped across two comment lines,
    invisible to a whole-name grep) and the guard was green. Blind review.

    Derived from `_tracked()` for the same reason `_extensions()` is: a list
    would rot the moment someone adds a `Dockerfile`.
    """
    names = {p for p in _tracked() if not _extension_token().fullmatch(p)}
    names |= {
        name for name, path in _unique_basenames().items()
        if path in names
    }
    if not names:  # pragma: no cover - the repo always has a Makefile
        return re.compile(r"(?!)")
    ordered = sorted(names, key=lambda n: (-len(n), n))
    return re.compile(
        r"(?<![A-Za-z0-9_.\-/])(?:"
        + "|".join(re.escape(n) for n in ordered)
        + r")(?![A-Za-z0-9_.\-/])")


def _tokens(text: str) -> set[str]:
    return set(_extension_token().findall(text)) | set(
        _extensionless_token().findall(text))


# A continuation: the newline, the next line's indent, and its comment marker if
# it has one. `*` covers `/* … */` blocks that carry a leading star; a block
# comment without one is just indentation, which the `?` allows.
# ⚠️ Five markers only. `>` (Markdown blockquote), `::`/`REM` (batch), `%`, `!`
# are NOT modelled — a path wrapped inside one of those is invisible. Blind
# review demonstrated all of them; none occurs in the 46 wraps this change
# fixed, but the gap is real, so it is named rather than implied.
LINE_PREFIX = re.compile(r"^[ \t]*(?:#|//|\*|--|;)?[ \t]*")

# A bare filename counts as a reference only if it is unambiguous. Both bounds
# earn their place: without uniqueness, `values.yaml` matches 30 charts; without
# the length/separator floor, `main.go` and `index.md` flood the scan.
_MIN_BARE_NAME = 12

# Coverage floors — they answer "did the scan run at all?", nothing finer.
# ⛔ Do NOT read them as protecting the token model: blind review cut
# `_extensions()` down to a SINGLE extension and all three floors still passed
# (19× slack on tokens alone). What actually holds the model honest is
# `test_extensions_are_derived_and_longest_first`. `_MIN_FILES` is weaker
# still — `_unread_drift` runs first and demands every tracked file, so by the
# time this is reached the count is always the full tree; it only fires if the
# repo itself shrinks. Kept as a cheap tripwire, described for what it is.
# Independent of anything `_tracked()` computes, on purpose: a floor taken from
# the thing it protects is not a floor.
_MIN_TRACKED_FILES = 1000

_MIN_FILES = 1500
_MIN_TOKENS = 2000
_MIN_RESOLVING = 300

# ⛔ NOTHING is skipped: every tracked file is decoded with `errors="replace"`.
#
# The first version instead pinned the exact set of files that fail to decode as
# UTF-8, on the reasoning that a decoder silently dropping input turns "found
# nothing" into "clean". The reasoning was right; the implementation was not —
# WHICH files fail to decode is ENVIRONMENT-DEPENDENT. The three names it pinned
# are LFS-backed PNG baselines (`.gitattributes` routes `tests/e2e/*-snapshots/`
# through Git LFS): on a dev machine with LFS smudged they are real PNGs and
# undecodable, while `actions/checkout` does not fetch LFS by default, so on CI
# they are UTF-8 pointer files and decode fine. Green locally, red on CI.
#
# Replacing bytes instead of skipping files removes the question entirely, and
# `_unread_drift` keeps the original intent in a form that does not depend on
# where the test runs: every tracked file must have been READ.


@lru_cache(maxsize=1)
def _tracked() -> tuple[str, ...]:
    out = subprocess.run(
        # ⛔ `-z` + a NUL split, not the newline default. Under the default
        # `core.quotePath` this command C-quotes any non-ASCII tracked path, so
        # a newline-split listing silently misrepresents it — and this scan's
        # whole point is that nothing is quietly dropped. The sibling guard
        # (tests/ops/test_ci_path_filter_coverage.py `_split_tracked`) fixed
        # this and documented the half-fix trap; this copy was left behind in
        # the same change and blind review caught the asymmetry.
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True,
        # An inherited invalid stdin handle makes subprocess raise WinError 6
        # on some Windows pytest runners; see the same note in
        # tests/ops/test_ci_path_filter_coverage.py's `_tracked_files`.
        stdin=subprocess.DEVNULL,
        check=True, timeout=120).stdout
    return _split_listing(out)


def _split_listing(raw: str) -> tuple[str, ...]:
    """Pure, so the floor below can be handed degenerate input.

    ⛔ Inline over live data, neither check below can fail — nothing tells
    "the check is here" from "the check was deleted". Split out so
    `test_tracked_split_refuses_a_narrowed_listing` can drive them directly.
    """
    files = tuple(sorted(p for p in raw.split("\0") if p))
    assert len(files) >= _MIN_TRACKED_FILES, (
        f"`git ls-files -z` yielded {len(files)} path(s) — not a plausible "
        "listing of this repo. `_unread_drift` compares the scan against THIS "
        "sequence, so a truncated or wrongly-split listing makes the "
        "'nothing was skipped' check agree with itself and pass.")
    assert len(files) >= raw.count("\0"), (
        f"{raw.count(chr(0))} NUL separators but only {len(files)} paths "
        "survived — something between git and here is dropping entries.")
    return files


@lru_cache(maxsize=1)
def _unique_basenames() -> dict[str, str]:
    """Repo-unique basenames that are distinctive enough to match on."""
    seen: dict[str, list[str]] = {}
    for path in _tracked():
        seen.setdefault(path.rsplit("/", 1)[-1], []).append(path)
    return {
        name: paths[0]
        for name, paths in seen.items()
        if len(paths) == 1 and len(name) >= _MIN_BARE_NAME and re.search(r"[_-]", name)
    }


def _resolves(token: str) -> bool:
    return token in set(_tracked()) or token in _unique_basenames()


def _wrapped_references(text: str) -> list[tuple[int, str]]:
    """(1-based line of the break, token) for every reference split in two.

    A token is reported only when it appears in the REJOINED two-line window and
    NOT in the raw window — i.e. the line break is what hides it.
    """
    lines = text.split("\n")
    found: list[tuple[int, str]] = []
    for index in range(len(lines) - 1):
        raw = lines[index] + "\n" + lines[index + 1]
        prefix = LINE_PREFIX.match(lines[index + 1]).group(0)
        rejoined = lines[index] + lines[index + 1][len(prefix):]
        for token in sorted(_tokens(rejoined)):
            if token in raw or not _resolves(token):
                continue
            found.append((index + 1, token))
    return found


# ⛔ THE TRIPWIRES BELOW ARE PURE FUNCTIONS, and that shape is the whole point.
#
# Each one answers "is anything wrong?" for a condition that is currently FALSE:
# the tree has no wrapped reference, the unreadable set matches, coverage is
# ample. An `assert` written inline against a currently-false condition cannot
# be tested — deleting it is invisible, because no input distinguishes the two
# implementations. Mutation testing said so out loud: clearing `offenders`,
# neutering the unreadable check, and flattening the coverage floor ALL survived
# a full run of the first version of this module.
#
# Splitting each tripwire into a function the tests can feed degenerate input to
# is what makes "the tripwire still exists" an assertion rather than a hope.
# (testing-playbook §v2.10.0 ⑤ — a mutant that survives because no case can tell
# the difference is a test-DESIGN bug, not coverage.)


def _offenders(files: list[tuple[str, str]]) -> dict[str, list[str]]:
    """path -> human-readable hits, for every file with a wrapped reference."""
    found: dict[str, list[str]] = {}
    for path, text in files:
        for line, token in _wrapped_references(text):
            found.setdefault(path, []).append(f"line {line} → {token}")
    return found


def _unread_drift(read_paths: set[str]) -> list[str]:
    """Non-empty when a tracked file never reached the scan.

    Environment-independent by construction: it compares against `git ls-files`
    rather than against a pinned list of names, which is what made the first
    version pass locally and fail on CI.
    """
    missing = sorted(set(_tracked()) - read_paths)
    if not missing:
        return []
    return [f"{len(missing)} tracked file(s) were never read: {missing[:5]}"]


def _decoded_shortfall(read_chars: int, on_disk: int) -> str | None:
    """⛔ Guards the CONSUMED corpus, not the bytes handed to the decoder.

    `_decode_whole` asserts `len(raw) == st_size`, which is an assertion about
    its INPUT — moving the truncation one line later (`raw.decode(...)[:4096]`)
    satisfies it and still drops most of the corpus. Blind review measured that
    edit: every path survived, all three floors below cleared, the run got 2.7x
    faster, and a wrapped reference injected past the cut went unseen.

    The bound is arithmetic, not a tuned constant: UTF-8 uses at most 4 bytes
    per code point, so decoding N bytes yields at least N/4 characters. Any
    complete decode clears it with room; a prefix truncation cannot.
    """
    if read_chars * 4 < on_disk:
        return (f"only {read_chars} character(s) decoded from {on_disk} byte(s) "
                "on disk — below the arithmetic floor for a complete UTF-8 "
                "decode, so content was dropped AFTER the per-file size check. "
                "The path set can be intact while the corpus is a prefix.")
    return None


def _coverage_shortfalls(files: list[tuple[str, str]]) -> list[str]:
    """Non-empty when the scan stopped reaching enough of the tree."""
    tokens = [t for _, text in files for t in _tokens(text)]
    resolving = {t for t in tokens if _resolves(t)}
    problems = []
    on_disk = 0
    for path in _tracked():
        try:
            on_disk += (ROOT / path).stat().st_size
        except OSError:
            continue
    truncated = _decoded_shortfall(sum(len(t) for _, t in files), on_disk)
    if truncated:
        problems.append(truncated)
    if len(files) <= _MIN_FILES:
        problems.append(f"only {len(files)} tracked files read (floor {_MIN_FILES})")
    if len(tokens) <= _MIN_TOKENS:
        problems.append(f"only {len(tokens)} path-like tokens seen (floor {_MIN_TOKENS})")
    if len(resolving) <= _MIN_RESOLVING:
        problems.append(
            f"only {len(resolving)} distinct tokens resolved to real files "
            f"(floor {_MIN_RESOLVING}) — the resolver, not the regex, is what "
            "decides whether a hit is a reference")
    return problems


def _read_tracked() -> list[tuple[str, str]]:
    """Every tracked file, decoded with replacement so none is ever skipped.

    Binary content turns into replacement characters, which cannot form a token
    that RESOLVES to a real repo path — so reading everything costs nothing in
    precision and removes the "which files did we quietly not look at?" question
    that the pinned-set version got wrong across environments.
    """
    files: list[tuple[str, str]] = []
    for path in _tracked():
        try:
            # ⛔ ONE handle, and `fstat` on it — not two path-based calls.
            # Separate `read_bytes()` + `Path.stat()` can resolve to different
            # inodes if the path is replaced between them, and swapping their
            # ORDER does not help: the check below is an equality, so a file
            # that changes size trips it either way (read-first reports short,
            # stat-first reports long). Same handle makes both numbers describe
            # the same file.
            with (ROOT / path).open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                raw = handle.read()
        except OSError:
            continue
        files.append((path, _decode_whole(raw, size, path)))
    return files


def _decode_whole(raw: bytes, size: int, path: str) -> str:
    """Decode, refusing a PARTIAL read.

    ⛔ Every other guard here pins the PATH SET — `_unread_drift`, the
    independent `git ls-files` cross-check, the no-slicing check. None of them
    sees content: `read_bytes()[:4096]` keeps all 2293 paths, satisfies all
    three, and still truncates 1615 files, dropping ~74% of the corpus and
    ~58% of the path-like tokens. Measured: a wrapped reference injected past
    the cut is invisible while the suite stays green (and the run gets 2.4x
    faster, which is what makes the edit attractive).

    Pure, so `test_partial_read_is_refused` can hand it a short buffer — an
    inline check over real files can never fail and so can never be shown to
    still exist.
    """
    assert len(raw) == size, (
        f"{path}: read {len(raw)} byte(s) of a {size}-byte file. The scan then "
        "covers a PREFIX of the tree's content while every path-set guard "
        "still reports a full sweep — the failure this module exists to "
        "prevent, one layer in.")
    return raw.decode("utf-8", "replace")


def test_no_reference_is_split_across_a_line_break() -> None:
    files = _read_tracked()

    drift = _unread_drift({path for path, _ in files})
    assert not drift, (
        "the scan never looked at part of the tree, so a green result below "
        "would mean nothing: " + "; ".join(drift))

    shortfalls = _coverage_shortfalls(files)
    assert not shortfalls, (
        "the scan stopped reaching the tree, so a green result below would mean "
        "nothing: " + "; ".join(shortfalls))

    offenders = _offenders(files)
    assert not offenders, (
        "a file reference is split across a line break, so grepping its whole "
        "name will not find it — and a rename sweep that greps the whole name "
        "will report the tree clean while leaving this pointer behind (#1373 "
        "shipped exactly that).\n"
        "Fix by reflowing so the path sits on ONE line; the surrounding prose "
        "can wrap wherever you like.\n"
        + "\n".join(
            f"    {path}:\n" + "\n".join(f"      {hit}" for hit in hits)
            for path, hits in sorted(offenders.items())))


def test_detector_finds_a_synthetic_wrap() -> None:
    """Anti-vacuity: the tree is at zero, so the assertion above proves nothing
    on its own. A scanner that returned `[]` unconditionally would pass it."""
    target = "tests/ops/test_wrapped_path_references.py"
    assert target in _tracked(), "this module moved; re-point the fixture"
    head, tail = target[:20], target[20:]

    for wrapped, label in (
        (f"# see {head}\n# {tail} for the rule\n", "hash comment"),
        (f"// see {head}\n//     {tail} for the rule\n", "slash comment"),
        (f" * see {head}\n *  {tail} for the rule\n", "star block"),
        (f"    see {head}\n    {tail} for the rule\n", "bare indent"),
    ):
        hits = _wrapped_references(wrapped)
        assert [t for _, t in hits] == [target], f"{label}: {hits}"

    contiguous = f"# see {target} for the rule\n# and more prose here\n"
    assert _wrapped_references(contiguous) == [], (
        "a reference that is NOT split must not be reported — otherwise every "
        "normal citation reds and the fix is to delete the guard")


def test_extensions_are_derived_and_longest_first() -> None:
    """Two properties, both of which failed in earlier versions of this module.

    Derived: the set must cover what the tree actually contains — the first
    version hand-listed 16 extensions and omitted `png`, which the tree
    contains three tracked files of.
    Longest-first: Python's `|` is first-match, so `js` before `jsx` makes
    `.jsx` unreachable and the scan reports the truncated name, which is often
    a real build artefact. Sorting by length removes the failure mode instead
    of pinning it.
    """
    exts = _extensions()
    assert exts == tuple(sorted(exts, key=lambda e: (-len(e), e))), (
        "extensions must be ordered longest-first before joining into an "
        "alternation")

    for required in ("jsx", "tsx", "yaml", "png", "go", "py"):
        assert required in exts, (
            f"{required!r} is used in the tree but missing from the derived set")

    for name in ("tools/portal/src/a-really-long-name.jsx",
                 "tests/e2e/some-long-spec.tsx",
                 "helm/vector/tests/projection_tests.yaml",
                 "tests/e2e/visual.spec.ts-snapshots/a-really-long-shot.png"):
        assert _extension_token().findall(name) == [name], (
            f"{name} was truncated by the extension alternation")


def test_extensionless_paths_are_tokenised_too() -> None:
    """The class the first version could not see at all.

    43 tracked files have no usable extension (`Makefile`, eight `Dockerfile`s,
    `LICENSE`, the `.gitignore` family, `components/da-tools/app/VERSION`,
    `tests/rulepacks/vm_engine_version`, …). An extension-anchored pattern never
    produces them as tokens, so a wrapped reference to one was not "let through
    by the resolver" — it was never looked at. One was live in `ci.yml`.
    """
    blind = [p for p in _tracked() if not _extension_token().fullmatch(p)]
    assert len(blind) > 20, (
        f"only {len(blind)} extensionless tracked paths — if that collapsed, "
        "the derivation stopped seeing them and this whole class went dark")

    for path in ("Makefile", "tests/rulepacks/vm_engine_version"):
        assert path in _tracked(), f"{path} moved; re-point this fixture"
        assert _extension_token().findall(path) == [], (
            f"{path} should be invisible to the extension-anchored pattern")
        assert path in _tokens(path), (
            f"{path} must still be tokenised by the extensionless pattern")

    target = "tests/rulepacks/vm_engine_version"
    head, tail = target[:16], target[16:]
    wrapped = f"# see {head}\n# {tail} here\n"
    assert [tok for _, tok in _wrapped_references(wrapped)] == [target], (
        "a wrapped extensionless path must be reported")
    contiguous = f"# see {target} here\n# and more\n"
    assert _wrapped_references(contiguous) == [], (
        "a contiguous extensionless path must NOT be reported")

    # ⛔ The BARE-name half of `_extensionless_token`, which mutation testing
    # showed was inert: deleting it changed nothing, because every case above
    # uses a full path. It is kept for symmetry with the extension half (which
    # accepts unique basenames via `_resolves`), so it needs a case that can
    # tell the difference — otherwise it is a branch nobody would notice going.
    bare = sorted(
        name for name, path in _unique_basenames().items()
        if not _extension_token().fullmatch(path)
        and name != path  # root dotfiles are already covered as full paths
    )
    assert bare, (
        "no repo-unique extensionless basename qualifies any more — either the "
        "tree changed or `_unique_basenames`' bounds did. Re-decide whether the "
        "bare-name half still earns its place instead of leaving it untested.")
    subject = bare[0]
    split = max(1, len(subject) // 2)
    hits = _wrapped_references(
        f"# see {subject[:split]}\n# {subject[split:]} here\n")
    assert [tok for _, tok in hits] == [subject], (
        f"a wrapped bare extensionless name ({subject}) must be reported")


def test_tracked_split_refuses_a_narrowed_listing() -> None:
    """The floor and the NUL cross-check, driven directly.

    ⛔ Inline over the real listing neither can fail, so deleting them is
    invisible. `_split_listing` is pure precisely so they can be driven.
    """
    blob = "\0".join(f"zdir/zfile{i}.txt" for i in range(2000))
    assert len(_split_listing(blob)) == 2000

    # the half-fix: `-z` requested, newline split kept -> ONE element
    with pytest.raises(AssertionError, match="plausible"):
        _split_listing(blob.replace("\0", "\n"))
    with pytest.raises(AssertionError, match="plausible"):
        _split_listing("")

    # ⛔ The NUL cross-check needs its OWN case. Both inputs above trip the
    # >=_MIN_TRACKED_FILES floor first, so the separator assert never ran and
    # replacing it with `assert True` left this test green (blind review).
    # Drive it with enough paths to clear the floor AND more separators than
    # survivors — doubled NULs, the shape a sloppy join produces.
    doubled = "\0\0".join(f"zdir/zfile{i}.txt" for i in range(2000))
    with pytest.raises(AssertionError, match="NUL separators"):
        _split_listing(doubled)


def test_decoded_shortfall_refuses_a_truncated_corpus() -> None:
    """The corpus floor, driven directly.

    Over the real tree it can never fail, so nothing distinguishes "the check
    is here" from "the check was deleted" — the reason it is a pure function.
    """
    assert _decoded_shortfall(1_000_000, 1_000_000) is None   # ASCII
    assert _decoded_shortfall(400_000, 1_000_000) is None     # dense CJK
    assert "arithmetic floor" in (_decoded_shortfall(4096, 1_000_000) or "")


def test_partial_read_is_refused() -> None:
    """The content-length check, driven directly.

    ⛔ Over real files it can never fail, so nothing distinguishes "the check
    is here" from "the check was deleted" — the same reason `_split_listing`
    is a separate function.
    """
    body = b"zline-one\nzline-two\n"
    assert _decode_whole(body, len(body), "zfake.txt").endswith("two\n")
    with pytest.raises(AssertionError, match="byte\\(s\\) of a"):
        _decode_whole(body[:5], len(body), "zfake.txt")


def test_tracked_set_matches_an_independent_git_listing() -> None:
    """⛔ The floor has ~1300 files of headroom, so it cannot see a narrowing
    that stays above it — blind review dropped every `.md` (306 files, the
    exact surface this scan exists for: CHANGELOG.md alone carries 601
    resolvable references) and the whole module stayed green, because
    `_unread_drift` compares the scan against the SAME narrowed sequence and
    so agrees with itself.

    Independent BY CONSTRUCTION: its own argv, its own split.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
        stdin=subprocess.DEVNULL, text=True, check=True, timeout=120).stdout
    expected = {p for p in out.split("\0") if p}
    actual = set(_tracked())
    assert actual == expected, (
        f"`_tracked()` is not `git ls-files -z`: {len(actual)} path(s) "
        f"against {len(expected)}. Every file missing here is a file this "
        "guard silently never scans.\n"
        f"    dropped: {sorted(expected - actual)[:10]}\n"
        f"    extra:   {sorted(actual - expected)[:10]}")


def test_tracked_is_never_narrowed_at_a_use_site() -> None:
    """The same disarm one frame out, where the in-helper checks cannot see it.

    ⚠️ Same honest bound as the sibling module's copy: direct subscript, a
    local bound from the call, and a wrapper call are recognised; `islice`
    and filtering comprehensions are not.
    """
    tree = ast.parse(Path(__file__).resolve().read_text(encoding="utf-8"))

    def _is_call(node: ast.AST) -> bool:
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_tracked")

    bound = {
        target.id
        for node in ast.walk(tree) if isinstance(node, ast.Assign)
        and _is_call(node.value)
        for target in node.targets if isinstance(target, ast.Name)
    }
    sliced = sorted(
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and (_is_call(node.value)
             or (isinstance(node.value, ast.Name) and node.value.id in bound)
             or (isinstance(node.value, ast.Call)
                 and any(_is_call(a) for a in node.value.args)))
    )
    assert not sliced, (
        f"`_tracked()` is subscripted at line(s) {sliced}. The scan then "
        "covers a SUBSET while `_unread_drift` compares against the same "
        "subset and reports nothing skipped. Iterate the whole set.")


def test_each_tripwire_fires_on_degenerate_input() -> None:
    """The three tripwires above all guard conditions that are currently FALSE.

    Without this test, deleting any of them is invisible: no input distinguishes
    "checks and passes" from "does not check". Mutation testing proved it — all
    three survived until they were given a case that can tell the difference.
    """
    target = "tests/ops/test_wrapped_path_references.py"
    head, tail = target[:24], target[24:]
    wrapped = f"# see {head}\n# {tail} for the rule\n"
    assert _offenders([("fake.py", wrapped)]), (
        "the offender aggregation reported nothing for a file that plainly "
        "contains a wrapped reference")
    assert _offenders([("fake.py", "# nothing to see here\n# at all\n")]) == {}

    assert _unread_drift(set()), "reading nothing at all must be drift"
    # ⛔ A literal, NOT a slice of `_tracked()`: a degenerate input taken from
    # the thing it protects moves with it, and slicing the sweep at a use site
    # is itself the disarm `test_tracked_is_never_narrowed_at_a_use_site`
    # exists to refuse.
    assert _unread_drift({"README.md"}), (
        "reading only a handful of tracked files must be drift")
    assert _unread_drift(set(_tracked())) == []

    assert _coverage_shortfalls([]), "an empty scan must be a shortfall"
    # 4 = files / tokens / resolving / decoded-corpus. The last one guards the
    # CONSUMED corpus rather than the bytes fed to the decoder; adding it moved
    # this count, which is exactly what this assertion is for.
    assert len(_coverage_shortfalls([])) == 4, (
        "every floor must report independently, so one collapsing does not hide "
        "the others")
