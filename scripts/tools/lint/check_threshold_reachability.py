"""Threshold-key reachability, and the producer policy (TRK-337, #3924).

TWO QUESTIONS, both about the threshold keys of
`rule-packs/threshold-registry.yaml`.

1. REACHABILITY (TRK-337). Every conf.d key an alert demands (via
   `_observed_map_lib.all_threshold_keys`, the extractor the observed-map
   drift guard uses) must be one the readers can serve: a registry key, or a
   KNOWN_DEFERRED one (its threshold lives in a `:core` recording rule, not a
   `- alert:`). Every registry key is in the readers' built-in layer (#3832,
   #4163) — valued with the platform's number when it is `arming: default`,
   off otherwise — so a tenant, subtree or profile that gives it a value is
   served without the root declaring it; a `<base>_critical` is served over
   that base. An alert that demands any other key is DEAD: it can never fire,
   and nothing else says so (a declared-but-unwired failure that schema
   validation cannot see).

2. THE PRODUCER POLICY (#3924). Nothing this repository ships writes a
   registry key into a customer's root platform defaults: no value under
   `defaults:` (at any depth, in any spelling — a #1231 alias, a
   `<base>_critical`, a dimensional `<base>{…}`) and no name under
   `optional_overrides:`. The readers already serve every one; a copy would
   pin the day's platform number (#3832 decision 7) or arm, for every tenant,
   a value only the deployment can calibrate. The faces:
     * GENERATORS — `scaffold_tenant.generate_defaults` and
       `init_project._gen_defaults_yaml` (read as the exporter reads the file
       it writes), for every pack;
     * HELM VALUES — the chart's `values.yaml` and the repo's own environment
       overlays, read as Helm reads them;
     * SHIPPED conf.d SAMPLES — every tracked `_defaults*` artifact outside the
       test fixtures (`_FIXTURE_PREFIXES`, each with its reason and an
       exit-lock).
   And the writer census (#1412) keeps the generator list honest: every module
   under `scripts/` that writes a `defaults:` mapping must be a face here or
   exempt by name with the mechanism that makes it safe.

A tenant file, a subtree `_defaults.yaml` or a profile writing a registry key
is the customer's own configuration and none of this gate's business.

Exit codes (_lib_exitcodes): 0 clean / 1 violation (--ci) / 2 caller error.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_OPS = PROJECT_ROOT / "scripts" / "tools" / "ops"
sys.path.insert(0, str(_OPS))
import _observed_map_lib as observed_map_lib  # noqa: E402
import scaffold_tenant  # noqa: E402
# The `<base>_critical` suffix, from the module that owns it.
from _registry_lib import CRITICAL_SUFFIX as _CRITICAL_SUFFIX  # noqa: E402
import _registry_lib  # noqa: E402

# ⛔ The conf.d root of a path comes from the module that already defines what a
# conf.d root IS (nearest `conf.d` ancestor, inner-most wins for nested trees).
# Re-deriving it here — "split on conf.d", "take parents[1]" — is the shape this
# gate has already been bitten by twice (#1392: a dotted path re-split downstream
# lost a whole class of keys). ⚠️ This import lives outside `scripts/tools/`, so
# the hook `files:` filter and the test that pins it both had to learn about it;
# that the pin needed a manual update is #1413.
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "ops"))
from guard_defaults_scopes import conf_d_root  # noqa: E402

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402
# ADR-036 step 4 (#2906): the tracked defaults artifacts (conf.d files) are
# read by `da-guard parse-tree`; so is the `_defaults.yaml` a generator writes
# (ADR-036 §12, #3206: it is a conf.d file once written). The chart values are
# read as Helm reads them (da-crdecode). This module reads no YAML itself.
from _lib_parse_tree import FileUnreadable as _FileUnreadable  # noqa: E402
from _lib_parse_tree import ParseTreeError as _ParseTreeError  # noqa: E402


class _GateViolation(RuntimeError):
    """A floor breach: the tool ran fine, the REPOSITORY is in a state to fix.

    ⛔ Not a caller error, and the distinction is the repo's own SSOT:
    `scripts/tools/_lib_exitcodes.py` defines EXIT_VIOLATION (1) as "the tool ran
    correctly and found something the USER must act on" and EXIT_CALLER_ERROR (2)
    as "the tool could NOT do its job because of how it was invoked or its
    environment". "A shipped `_defaults.yaml` was deleted" is squarely the first,
    yet every floor in this module used to surface as `crashed: …` with rc=2 —
    and a maintainer reading "lint crashed" reaches for skip-it-and-move-on,
    which is the single reaction these floors exist to prevent (blind review).

    Subclasses RuntimeError on purpose: the floors are also asserted directly in
    tests with `pytest.raises(RuntimeError, ...)`, and narrowing that would be a
    second change riding along with this one.

    ⚠️ The READER's failures stay caller errors — an unreadable or malformed file
    genuinely means this tool could not do its job, and its message already
    carries the path.

    ⚠️ …and that deliberately includes the `defaults:` walk hitting
    `_DEFAULTS_MAX_DEPTH`, even though a recursive YAML anchor in a TRACKED file
    is a repo state rather than an environment one. It arrives through the
    reader's provenance wrapper (which is what attaches the filename), and
    splitting it out would mean classifying an exception by its text. Flagged
    here because the boundary is arguable, not because nobody noticed.
    """


_CHART_VALUES_REL = "helm/threshold-exporter/values.yaml"
# The repo's own Helm overlays for the chart (`helm upgrade -f …`); a value in
# one deep-merges over the chart's, so they are held to the same policy.
_ENVIRONMENT_VALUES_GLOB = "environments/*/threshold-exporter.yaml"


def _registry_doc() -> dict:
    return _registry_lib.load_registry()


def _supply(doc: dict | None = None) -> set[str]:
    """Every conf.d threshold key the readers can serve with no root line: the
    registry's keys (each built in, #3832 / #4163), canonical spelling."""
    doc = _registry_doc() if doc is None else doc
    return set(_registry_lib.registry_keys(doc))


def _registry_spellings(doc: dict | None = None) -> set[str]:
    """Every name a registry key goes by: canonical keys and #1231 aliases."""
    doc = _registry_doc() if doc is None else doc
    return set(_registry_lib.registry_keys(doc)) | set(doc.get("deprecated_aliases") or {})


def _is_registry_key(name: str, spellings: set[str]) -> bool:
    """Whether a `defaults:` key or `optional_overrides:` entry names a registry
    key: itself, its `<base>_critical`, or a dimensional `<base>{…}` (labels
    are stripped before the suffix, so `<base>{…}_critical` counts too)."""
    head = name.split("{", 1)[0]
    tail = name.rsplit("}", 1)[-1] if "}" in name else ""
    for cand in (head, head + tail):
        base = cand[: -len(_CRITICAL_SUFFIX)] if cand.endswith(_CRITICAL_SUFFIX) else cand
        if cand in spellings or base in spellings:
            return True
    return False


def _values_doc(rel: str) -> dict:
    """A Helm values file as Helm reads it (da-crdecode, ADR-036 §12, #3206) —
    not PyYAML. Raises ConsumerReadError when that reading cannot be had."""
    import _lib_consumer_read  # noqa: PLC0415

    docs = _lib_consumer_read.any_yaml_documents(str(PROJECT_ROOT / rel))  # no manifest (#3426)
    doc = docs[0] if docs else {}
    if not isinstance(doc, dict):
        raise _lib_consumer_read.ConsumerReadError(
            f"{rel}: Helm reads it as {type(doc).__name__}, not a mapping")
    return doc


_DEFAULTS_ARTIFACT_SUFFIXES = (".yaml", ".yml")

# Measured 30 tracked `_defaults*` artifacts when #3924 landed. The floor sits
# well below that so ordinary deletions do not trip it, and above the handful a
# regression to a hardcoded list would leave: the schema witness (and the
# sample scan) judging fewer means the reader stopped reading, not that the
# tree got cleaner.
_DEFAULTS_ARTIFACT_FLOOR = 10

# The tracked `_defaults*` artifacts that are TEST FIXTURES, not shipped
# samples: they deliberately write registry keys at the root to characterise
# how the readers merge, refuse and serve them — the customer's own
# configuration, which #3924 leaves to the customer. ⛔ Each prefix names its
# reason, and each must still match an artifact (STALE-FIXTURE-PREFIX), so the
# list cannot quietly grow into "everything".
_FIXTURE_PREFIXES: dict[str, str] = {
    "tests/":
        "pytest and golden fixtures — conf.d trees built to exercise the loader",
    "components/threshold-exporter/app/cmd/da-guard/selfcheck/":
        "da-guard's self-check tree, whose expected output is pinned by its "
        "goldens (`go test ./cmd/da-guard -run SelfCheck`)",
}


def _tracked_paths() -> list[str]:
    """Every repo-relative path in the INDEX. The one place this module shells out.

    ⛔ A SEAM, and that is half its job (#1411). The derivations below take the
    listing as an argument, so a test feeds a SYNTHETIC one and requires the
    answer to follow it — which no body answering from a module constant can
    do.

    ⛔ AND THE SEAM MOVES THE PROBLEM UP ONE LEVEL — the first version of this
    note stopped one step short. Injecting `paths` proves the CALLEE follows its
    argument; it says nothing about the argument the callee builds when nobody
    passes one, which is this function. Measured as the suite stood at
    `3864b63`, before the test named below existed: replacing THIS body with a
    literal list of today's answer left it at 107 passed and the gate at rc=0.
    Every test either fed its own listing or compared this against a fresh
    `git ls-files` it ran itself, and a constant copied from today satisfies
    both — until the index next changes, which is not a tripwire, it is a delay.
    ⚠️ The population is named because the number is not today's: re-run on the
    final tree that same mutation is 1 failed / 113 passed, and the one red is
    `test_the_index_listing_really_comes_from_the_index` — i.e. the guard now
    catches it. A mutation score quoted without the suite it was taken against
    is the same defect as a fraction quoted without its population.

    So this one is pinned by pointing it at a DIFFERENT repository:
    `test_the_index_listing_really_comes_from_the_index` builds a throwaway git
    repo, re-points `PROJECT_ROOT` at it, and requires the answer to be that
    repo's files. A body that ignores `PROJECT_ROOT` cannot follow it, and no
    snapshot of THIS repo can be right about a repo that did not exist when the
    snapshot was taken.
    """
    import subprocess  # local: only this module-level scan shells out

    out = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "ls-files", "-z"],
        capture_output=True, check=True, timeout=60)
    return [rel for rel in out.stdout.decode("utf-8", "replace").split("\0") if rel]


def _tracked_defaults_artifacts(paths: list[str] | None = None) -> list[str]:
    """Repo-relative paths of every TRACKED platform-defaults artifact.

    Separate from `_defaults_artifacts` so the floor can count what the scan
    found BEFORE exemptions are subtracted — otherwise exempting files walks the
    count down toward the floor and the floor's message ("repair the scan or
    move the file(s) back") names the one repair that is not the problem.

    `paths` defaults to the real index listing, the same way `run_check` defaults
    its inputs to the real extractors;
    `test_the_index_listing_is_a_real_seam_on_both_derivations` injects a
    synthetic one and requires the answer to follow it. ⚠️ That sentence was
    here before the test was — the injectable half had
    no caller and no assertion, so the docstring was describing an intention.
    Both derivations that take this seam are exercised through it now.
    """
    # ⛔ NO pathspec. `git ls-files -- "*_defaults*"` is case-SENSITIVE — measured
    # under both `core.ignorecase` values, `*_DEFAULTS*` returns 0 — so pairing it
    # with a case-insensitive predicate delivered half a fix: `_defaults.YAML`
    # got through (stem matches) but `_Defaults.yaml` and `_DEFAULTS.yml` never
    # reached the predicate at all. Two predicates with the narrower one FIRST is
    # the shape; the guard's own case-insensitivity test could not see it because
    # it tests the predicate, and the hook's `(?i:)` filter could not see it
    # because it tests the trigger. The scan had neither (blind review, round 5,
    # two reviewers independently).
    #
    # Listing the whole index and filtering in Python makes `_is_defaults_artifact`
    # the ONLY predicate. Measured cost: ~2.3k paths (2294 today), 0.13s for the git call,
    # 0.5ms for the filter — the pathspec was buying nothing.
    if paths is None:
        paths = _tracked_paths()
    return sorted(
        rel for rel in paths
        if _is_defaults_artifact(rel.rsplit("/", 1)[-1])
        and (PROJECT_ROOT / rel).is_file())


def _is_defaults_artifact(name: str) -> bool:
    """Case-insensitive like the loader, but a PREFIX where the loader is exact.

    ⛔ Two halves with two different justifications, and conflating them invites
    the wrong "cleanup":
      * case-insensitivity MIRRORS `config_hierarchy.go`, which lowercases before
        both its suffix and its filename test;
      * the `_defaults` PREFIX is deliberately WIDER than the loader, whose test
        is `lower == "_defaults.yaml" || lower == "_defaults.yml"` (exact). The
        width is load-bearing — `examples/_defaults-multidb.yaml` is one of the
        three files blind review used to walk past the hardcoded-path version, and
        narrowing this to the loader's equality would drop it again.
    """
    lower = name.lower()
    return lower.startswith("_defaults") and lower.endswith(_DEFAULTS_ARTIFACT_SUFFIXES)


def _shipped_samples(paths: list[str] | None = None) -> list[str]:
    """The tracked `_defaults*` artifacts a customer receives as samples: every
    one outside `_FIXTURE_PREFIXES`. Derived, so a new sample tree is covered
    without anyone listing it."""
    return [rel for rel in _tracked_defaults_artifacts(paths)
            if not any(rel.startswith(p) for p in _FIXTURE_PREFIXES)]


# The platform-defaults contract, owned elsewhere. ⛔ Read, never re-stated: the
# authority for what may sit at the top level of a `_defaults*` file is the Go
# loader's type, the JSON Schema is its written form, and `check_confd_schema.py`
# is the lint that already enforces it — on FOUR conf.d trees.
_PLATFORM_DEFAULTS_SCHEMA_REL = "docs/schemas/platform-defaults.schema.json"
_PLATFORM_DEFAULTS_SCHEMA = PROJECT_ROOT / _PLATFORM_DEFAULTS_SCHEMA_REL


def _REJECTED_TOP_LEVEL_KEY(problem: str) -> bool:
    """Does this ONE violation say "the schema will not accept that key name"?

    ⛔ Named and separate so the question is asked in ONE place, about ONE
    violation at a time. Two callers: the `MOST LIKELY` headline below, and the
    wording tripwire in
    `test_the_rename_headline_is_bound_to_the_artifact_it_diagnoses`. Nothing
    else may re-derive this from a rendered message.

    ⚠️ ONLY THE FIRST DISJUNCT IS REACHABLE TODAY, and both are probed by the
    tripwire named above rather than left to be assumed. The schema has exactly
    one `additionalProperties: false` — at the root — and it carries
    `patternProperties`, so jsonschema words the failure as "does not match any
    of the regexes". The second spelling is what it emits when those prefixes
    are absent; it is kept because a schema edit could legitimately drop them,
    and the tripwire probes it against a patternProperties-stripped copy so it
    cannot rot into a branch nobody has ever run.

    ⚠️ It matches jsonschema's WORDING, which is not a contract this repo owns:
    a future release that rewords `additionalProperties` / `patternProperties`
    failures makes this return False and the headline simply stops appearing.
    That degrades to the ⚠️ branch, which is the safe direction — it never
    invents a rename that did not happen. Stated here rather than fixed because
    the fix is structured violations out of `check_confd_schema.py`, which is a
    change to the sibling lint's contract and not this gate's to make.
    """
    return ("does not match any of the regexes" in problem
            or "Additional properties are not allowed" in problem)


def _assert_defaults_artifacts_match_schema(
        rels: list[str],
        read: "Callable[[str], str] | None" = None) -> int:
    """Every TRACKED defaults artifact must satisfy platform-defaults.schema.json.

    Each artifact is read by `da-guard parse-tree` (`_parse_artifact`, ADR-036
    step 4, #2906): one document per file, an empty or comment-only file the
    empty mapping (judged, and clean), several documents a file it cannot
    read (a problem, as any other parse failure). The judgement is the sibling
    lint's (`check_confd_schema.defaults_doc_violations`), never restated here.

    `read` defaults to reading the file at `PROJECT_ROOT / rel`; a hermetic
    caller hands in text. ⛔ The seam is on the READER only — the schema, the
    judgement and the message are not injectable, so a test cannot prove its
    own copy of them.

    Why it matters to the producer policy: a `defaults:` or
    `optional_overrides:` block renamed to a spelling the schema does not
    allow (`defalts:`) would hide its keys from the policy, and that rename is
    a schema violation here. ⚠️ Moving the block under another LEGAL section
    name (`state_filters:` accepts a mapping) is NOT caught; neither is a
    duplicated key. The caught spellings are pinned by
    `test_the_witness_catches_a_renamed_section_and_the_pinned_members_stay_caught`.

    EVERY tracked artifact is judged, including ones outside a conf.d tree
    (`_is_defaults_artifact` matches a basename anywhere in the index); what
    the conf.d question changes is the MESSAGE, at the `outside` computation
    below. A deliberately defective fixture therefore cannot be tracked
    anywhere this scan reaches; one that is needed belongs under a Go-side
    testdata path or behind an explicit mechanism.
    """
    # ⛔ NOTHING IS FILTERED OUT HERE. `rels` is used whole; the conf.d question
    # is answered in the message, at the `outside` computation below. A
    # `[rel for rel in rels if conf_d_root(rel) is None]` binding stood here,
    # left over from the withdrawn refusal, and was read by nobody.
    if not rels:
        raise _GateViolation(
            "the schema witness was handed no defaults artifacts at "
            "all. It is called with the TRACKED defaults scan, which "
            "`_DEFAULTS_ARTIFACT_FLOOR` has already refused to let fall below "
            f"{_DEFAULTS_ARTIFACT_FLOOR}, so an empty list here means the "
            "wiring changed rather than the tree. A "
            "check that validates nothing passes perfectly. (#1443)")

    lint_dir = str(Path(__file__).resolve().parent)
    if lint_dir not in sys.path:
        sys.path.insert(0, lint_dir)
    # ⛔ Every import here stays INSIDE the function, following this module's own
    # convention (`yaml` is local to each face). Two of them have a reason
    # beyond convention: `check_threshold_registry.py` imports this module for
    # `KNOWN_UNWIRED` (itself from inside a function, so it inherits nothing
    # either way), and `--help` has to keep working in an env without
    # jsonschema — the sibling lint's docstring records the same choice. ⚠️ Without
    # jsonschema, `--help` still works and `--ci` does not: `main()` calls this
    # witness on every run.
    import json  # noqa: E402
    try:
        import jsonschema  # noqa: E402
    except ImportError as exc:  # pragma: no cover — env, not tree
        # ⛔ RuntimeError, NOT `_GateViolation`. `main()` turns the latter into
        # EXIT_VIOLATION under a banner that says "1 defaults-tier floor breach"
        # — a statement about the TREE — while a missing dependency is a
        # statement about the ENVIRONMENT, which this module already has an exit
        # code for. Blind review measured the first spelling changing `--ci`
        # from rc=2 to rc=1 and blaming the tree for a broken venv.
        raise RuntimeError(
            f"the schema witness needs jsonschema and it is not importable "
            f"({exc}). Install it (`pip install jsonschema`); the pre-commit "
            "hook carries it in `additional_dependencies` and CI's Python "
            "Tests job installs it. ⛔ Do not make this check optional — a "
            "witness that disappears with a missing dependency is the failure "
            "mode it was written against. (#1443)") from exc
    import check_confd_schema  # noqa: E402 — sibling lint: the ONE judgement

    if read is None:
        def read(rel: str) -> str:
            return (PROJECT_ROOT / rel).read_text(encoding="utf-8")

    schema = json.loads(_PLATFORM_DEFAULTS_SCHEMA.read_text(encoding="utf-8"))
    problems: list[str] = []
    # ⛔ The offending files are COLLECTED, not re-derived from the message
    # strings. The first spelling counted them with `p.split(":")[1]`, which is
    # the same "downstream re-guesses the structure from a rendered string"
    # this module's own walk docstring forbids — and it raised IndexError the
    # first time a caller supplied a message without a colon in it.
    offenders: set[str] = set()
    # ⛔ PER FILE, for the same reason `offenders` is collected rather than
    # re-derived from the message strings: the `MOST LIKELY` headline must speak
    # about ONE artifact's own violation. Asking "is there a conf.d offender"
    # and "does any problem anywhere look like a rejected key" of two different
    # populations lets an unrelated out-of-conf.d file arm the headline for a
    # conf.d file whose only fault is a value shape.
    # ⚠️ Binding it per file does not make the phrase test robust to jsonschema
    # rewording its messages; that fragility is unchanged.
    renamed_shape: set[str] = set()
    judged = 0
    for rel in rels:
        # ⛔ Per FILE, and the file is named on every path out. Without this the
        # sibling's `_CallerError` discipline was lost: a malformed artifact
        # escaped as a bare parser traceback saying `<unicode string>`, and a
        # YAML 1.1 boolean key (`on:`) escaped as a jsonschema `TypeError` —
        # in both cases `rel` was in hand and not in the message.
        try:
            docs = [_parse_artifact(rel, read(rel).encode("utf-8"))]
        except _ParseTreeError as exc:
            if not isinstance(exc, _FileUnreadable):
                raise  # no usable da-guard: the environment, not this file
            problems.append(f"ERROR: {rel}: cannot read/parse YAML: {exc.kind}: {exc.message}")
            offenders.add(rel)
            continue
        except Exception as exc:  # noqa: BLE001 — see below
            # ⛔ `Exception`, NOT the `(OSError, yaml.YAMLError)` pair this started as. Blind
            # review fed a non-UTF-8 artifact: `UnicodeDecodeError` is a
            # `ValueError`, so it escaped as a bare traceback naming
            # `<unicode string>` — under a comment claiming the file is named on
            # every path out. A repo whose comments are largely CJK will meet
            # that one.
            problems.append(f"ERROR: {rel}: cannot read/parse YAML: {exc}")
            offenders.add(rel)
            continue
        for doc in docs:
            judged += 1
            try:
                found = check_confd_schema.defaults_doc_violations(
                    rel, doc, schema, jsonschema)
            except TypeError as exc:
                # ⛔ Translate ONLY when the document really has a non-string
                # key. `TypeError` also arrives when the sibling's signature
                # drifts, and the first spelling translated that into "quote
                # your YAML key" — a confident wrong diagnosis on every
                # artifact at once (blind review, measured). Anything else
                # re-raises and `main()` reports it as a caller error.
                if isinstance(doc, dict) and all(
                        isinstance(k, str) for k in doc):
                    raise
                found = [
                    f"ERROR: {rel}: not schema-checkable ({exc}). A non-string "
                    "top-level key — YAML 1.1 reads bare `on`/`off`/`yes`/`no` "
                    "as booleans and an unquoted date as a date — cannot be "
                    "matched against the schema's key patterns. Quote it."]
            if found:
                offenders.add(rel)
                if any(_REJECTED_TOP_LEVEL_KEY(p) for p in found):
                    renamed_shape.add(rel)
            problems += found

    # ⛔ RETURNS DOCUMENTS JUDGED, and the strong floor on it lives at the CALL
    # SITE rather than here. The distinction between "files handed in" and
    # "documents judged" was not hypothetical: under PyYAML a comment-only
    # artifact yielded ZERO documents and the loop body never ran — "0
    # violations across all 17 artifacts" was a claim about 17 files of which
    # 16 were ever looked at. Since #2906 parse-tree reads such a file as the
    # empty mapping (one document, judged), and a file it cannot read is a
    # problem, so the two counts differ only by the files that failed.
    # ⛔ A floor of `_DEFAULTS_ARTIFACT_FLOOR` *inside* this function would be
    # measuring whatever population the caller passed, and every hermetic caller
    # passes a handful — so it would either be vacuous or force tests to pad
    # their fixtures to satisfy it, which is a floor measuring the test. The
    # real scan's size is known only where the real scan is made.
    # ⛔ PROBLEMS FIRST. An artifact that fails to parse contributes a problem
    # and NO judged document, so ordering the vacuity guard ahead of this
    # replaced a message naming the broken file with one saying "zero documents"
    # — the reader was blamed for the tree.
    # ⛔ The conf.d question is answered in the MESSAGE, not by dropping or
    # refusing the file. Two earlier shapes were measured wrong: a silent
    # filter (no report at all) and a hard refusal (a legal Go-side
    # `testdata/_defaults.yaml` failed the whole gate with no in-repo remedy).
    # What survives is the narrow true claim — this schema describes a conf.d
    # file, so a violation outside one may mean the contract does not apply.
    outside = sorted({rel for rel in offenders if conf_d_root(rel) is None})
    if not problems:
        if judged == 0:
            raise _GateViolation(
                f"the schema witness judged ZERO documents from {len(rels)} "
                "conf.d artifact(s) — every one of them yielded no YAML at "
                "all, and none of them failed to parse either. That is what a "
                "reader returning empty text looks like, and it is "
                "indistinguishable from a clean tree in every assertion that "
                "only checks this check stays quiet. (#1443)")
        return judged

    raise _GateViolation(
        f"{len(problems)} violation(s) across {len(offenders)} defaults "
        f"artifact(s) do not satisfy {_PLATFORM_DEFAULTS_SCHEMA_REL}:\n  "
        + "\n  ".join(problems)
        # ⛔ CONDITIONAL, AND THE CONDITION IS ABOUT ONE FILE. It applies when a
        # conf.d artifact's OWN violation is about a top-level key the schema
        # will not accept. Printed unconditionally — or gated on a question
        # asked of every file at once — it tells a maintainer whose value SHAPE
        # is wrong to "restore the section name" of a block they never lost.
        + ("\n  MOST LIKELY: a `defaults:` section was mis-spelled — a typo, "
           "or a rename that was not carried through. The declarations are "
           "still in the file and nothing is reading them. Restore the "
           "section name.\n"
           if (renamed_shape - set(outside)) else
           # ⛔ NO UNIVERSAL CLAIM HERE. This branch fires for a file that could
           # not be PARSED and for one whose top-level key is not a string — in
           # neither was a single key ever looked at. Say what was observed, not
           # what it implies about keys. ⚠️ The list below is illustrative, not
           # a closed set.
           "\n  ⚠️ NOT diagnosed as a renamed `defaults:` section: no violation "
           "in a conf.d artifact has the shape of a top-level key this schema "
           "refuses. A value-shape error, a file that could not be parsed at "
           "all, and offenders that sit only outside a conf.d tree all land "
           "here — in none of them would that headline name a section the file "
           "ever had.\n")
        + ("" if not outside else
           f"  ⚠️ {outside} sit OUTSIDE every `conf.d` tree. This schema "
           "describes a conf.d file, so the finding above may mean the "
           "contract does not apply to them rather than that they are wrong — "
           "decide which, and if it is the former say so in this gate rather "
           "than moving the file to satisfy a scan. (#1443 / #1434)\n")
        # ⛔ FORBID THE CHEAP WRONG FIX WITHOUT PUBLISHING IT. One blind round
        # asked for this warning (a maintainer who "fixes" the red by moving
        # the block under a legal section name leaves the thresholds unread);
        # the next round pointed out that the first spelling handed over the
        # bypass WITH A COUNT of how many names work, under a message that
        # closes every legitimate door. Both are right, because the line
        # carries two questions. The prohibition stays; the enumeration and the
        # number go, and live in the docstring where somebody designing the
        # guard reads them rather than somebody under pressure to go green.
        + "  ⛔ DO NOT move the block under a different top-level section name. "
        "Some of them are legal here and this check would go quiet while the "
        "thresholds stay unread — which is the defect being reported, not a "
        "fix for it.\n"
        "  OR: a genuinely new top-level section is being introduced. Then the "
        "schema is what has to learn about it, in its own commit, naming the "
        "Go loader side — `docs/schemas/platform-defaults.schema.json` is the "
        "written form of a contract owned by "
        "`components/threshold-exporter/app/pkg/config`, not a list this gate "
        "keeps. ⛔ That is the ONLY reason to touch the schema. Widening it to "
        "make this message go away is not local: `check_confd_schema.py` "
        "validates four shipped conf.d trees against the same file, so a key "
        "added to quiet this also stops being reported on the trees customers "
        "read.\n"
        "  ⛔ AND AN EXEMPTION IS NOT AVAILABLE: no table of this gate "
        "reaches this check, on purpose. (#1443)")


# Depth cap for the `defaults:` walk below. Generous — the deepest real nesting
# in the tree is 2 — because its job is not to model a schema but to make a
# runaway walk LOUD. PyYAML built a self-referential document from a recursive
# anchor (`defaults: &a {x: *a}`, measured: the walk recursed forever without
# this); the readers this gate uses now refuse that text (parse-tree:
# decode_error "anchor 'a' value contains itself") or cannot express it (Helm's
# JSON), and the cap stays for any mapping a caller hands in — a silent
# truncation would hand back a partial key set that passes the placement check
# for the half it never reached.
_DEFAULTS_MAX_DEPTH = 16


class KeyInfo(int):
    """A key's depth, PLUS the two structural facts the walk already knows.

    ⛔ Subclasses `int` on purpose: every consumer that only wants the depth
    (`info == 0`, `info + 1`, `len(face)`, `sum(...)`) keeps working unchanged,
    so adding structure here cannot quietly alter the floors or the counts.
    Only the two call sites that were re-deriving structure from the rendered
    path read `.leaf` / `.has_children`.

    Both fields exist because blind review measured a defect for each of them,
    and both defects were the SAME mistake this module's own walk docstring
    warns about — guessing structure from a dot in the rendered string:

    - `.leaf`: `k.rsplit(".", 1)[-1]` splits on the last dot ANYWHERE, so a
      dimensional key whose label value contains a dot
      (`oracle_tablespace{tablespace=~"SYS.*"}` — the mainstream shape in this
      repo, cf. `collector_test.go:567`) yielded `*"}` and the whole class went
      undetected. Base `2e61b20a` tested `"{" in k` and caught them, so the
      dotted paths introduced here had regressed coverage.
    - `.has_children`: `depth == 0` answers "is it top level", NOT "is it a
      leaf". `defaults: {mysql_connection_usage_percent_critical: {a: 1}}` is top level AND
      a mapping, so it was reported with the flat consequence ("emits a
      severity=warning series") when the truth is that `Defaults
      map[string]float64` fails to unmarshal and NOTHING is emitted.
    """

    leaf: str
    has_children: bool

    def __new__(cls, depth: int, leaf: str, has_children: bool) -> "KeyInfo":
        self = super().__new__(cls, depth)
        self.leaf = leaf
        self.has_children = has_children
        return self


def _walk_defaults_keys(node: object, prefix: str = "",
                        depth: int = 0) -> dict[str, KeyInfo]:
    """{dotted path: nesting depth} for every mapping key ANYWHERE under `defaults:`.

    ⛔ Recursive, and that is the whole point (#1392). Reading only the top level
    measured 7 of 17 tracked artifacts as able to express the defect this gate
    checks for — the rest keep their keys one level down and an injection there
    was silent (measured both ways: top level caught, nested not).

    Dotted paths rather than bare leaf names so the error message can say WHICH
    level to repair; `endswith(_CRITICAL_SUFFIX)` and the `{` test are unaffected
    by the prefix, and on a flat section the path IS the key (measured: the chart
    and `init` faces return byte-identical key sets before and after #1392).

    INTERMEDIATE keys are collected too, not just leaves: a misplaced
    `mysql_connection_usage_percent_critical:` whose value happens to be a mapping is the same
    defect wearing a different shape, and a leaves-only walk would step over it.

    ⛔ The DEPTH is returned, not re-derived downstream from a dot in the path.
    An earlier revision returned bare paths and a downstream reader tested
    `"." in key` to decide whether a key was nested — blind review measured the
    misdiagnosis: a flat `defaults: {"a.b_critical": 90}` (a legal YAML key that
    contains a dot) was reported with the nested consequence, which is the wrong
    failure mode and sends the reader after a metric that will not exist. The
    walk already knows the depth; throwing it away and guessing from the rendered
    string is the shape where an exception becomes structurally impossible to get
    right.
    """
    keys: dict[str, KeyInfo] = {}
    if depth > _DEFAULTS_MAX_DEPTH:
        raise RuntimeError(
            f"`defaults:` walk exceeded {_DEFAULTS_MAX_DEPTH} levels at {prefix!r}. "
            "A self-referential mapping (a cycle) has no bottom, and truncating "
            "the walk would silently stop checking whatever lies below. "
            "Flatten the section or break the cycle; do not raise the cap to "
            "paper over it.")
    if isinstance(node, dict):
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            sub = _walk_defaults_keys(v, path, depth + 1)
            # `str(k)` is the key AS WRITTEN — never re-split out of `path`.
            # ⛔ "has children" means "the value is not a scalar": an EMPTY
            # mapping or a list of scalars yields no sub-keys but still fails
            # `map[string]float64` decoding (#3478 — once the flat dimensional
            # arm went, `'x{l="v"}': {}` passed the gate as if it were a leaf).
            keys[path] = KeyInfo(depth, str(k),
                                 bool(sub) or isinstance(v, (dict, list)))
            keys.update(sub)
    elif isinstance(node, list):
        for item in node:
            # List ITEMS carry no key of their own; a mapping inside one does.
            keys.update(_walk_defaults_keys(item, prefix, depth + 1))
    return keys


def _parse_artifact(rel: str, data: bytes) -> dict:
    """A tracked defaults artifact's content as `da-guard parse-tree` reads it
    (ADR-036 step 4, #2906): yaml.v3 — the exporter's parser — in parse-tree's
    file mode. *rel* is repo-relative; the bytes are read as the file at its
    path under its conf.d root (the whole of *rel* when it has none), judged
    in an empty directory: WHICH artifacts are read is this gate's own
    question (tracked files, a `_defaults` PREFIX wider than the loader's),
    not the loader's selection, so no tree is walked. Raises
    `_lib_parse_tree.FileUnreadable` (bad syntax, several documents, a key
    written twice, a top level that is not a mapping …) and ParseTreeError."""
    import tempfile  # noqa: PLC0415
    import _lib_parse_cache  # noqa: PLC0415
    root = conf_d_root(rel)
    inner = rel[len(root) + 1:] if root else rel
    with tempfile.TemporaryDirectory(prefix="da-reach-") as empty:
        return _lib_parse_cache.read_text_as(empty, inner, data)


def _read_defaults_text(text: str) -> dict:
    """A `_defaults.yaml` a generator writes (`init_project._gen_defaults_yaml`),
    read as the exporter and tenant-api read that file in the customer's
    conf.d (ADR-036 §12, #3206): `da-guard parse-tree`'s file mode, as the
    root `_defaults.yaml` of an empty tree. Raises FileUnreadable (``file``
    is `_defaults.yaml`; the caller names the generator) and ParseTreeError."""
    return _parse_artifact("_defaults.yaml", text.encode("utf-8"))


def _generated_defaults(producer: str, text: str) -> dict:
    """`_read_defaults_text` of *producer*'s output, a refusal raised as the
    floor breach it is: the generator writes a `_defaults.yaml` the exporter
    refuses whole, so none of its keys exists anywhere — never an empty face.
    No usable da-guard stays ParseTreeError (a caller error in `main`)."""
    try:
        return _read_defaults_text(text)
    except _FileUnreadable as exc:
        raise _GateViolation(
            f"{producer} writes a `_defaults.yaml` the exporter refuses whole "
            f"({exc.kind}: {exc.message}); `da-guard parse-tree` reads it as the "
            "exporter does (ADR-036 §12, #3206). Fix the generator.") from exc


def _platform_section(doc: dict) -> tuple[dict[str, KeyInfo], list[str]]:
    """(`defaults:` key paths at every depth, `optional_overrides:` entries)
    of a root platform-defaults mapping.

    ⛔ A mistyped list is read for the NAMES it carries, not dropped: a mapping
    contributes its keys and a scalar itself. Dropping it would let
    `optional_overrides: {<registry key>: 5}` through this policy (the chart
    template refuses the shape, but a sample or an overlay is never rendered
    by it)."""
    listed = doc.get("optional_overrides") or []
    if isinstance(listed, dict):
        names = [str(k) for k in listed]
    elif isinstance(listed, list):
        names = [str(x) for x in listed]
    else:
        names = [str(listed)]
    return _walk_defaults_keys(doc.get("defaults") or {}), names


def _producer_faces() -> dict[str, tuple[dict[str, KeyInfo], list[str]]]:
    """{face label: (`defaults:` keys, `optional_overrides:` names)} for every
    face of the producer policy. Every reader raises rather than hand back an
    empty face: a face that cannot be read is a caller error naming it, never
    a vacuous pass."""
    import glob  # noqa: PLC0415

    if str(_OPS) not in sys.path:
        sys.path.insert(0, str(_OPS))
    import init_project  # noqa: E402

    all_packs = list(scaffold_tenant.RULE_PACKS)
    faces: dict[str, tuple[dict[str, KeyInfo], list[str]]] = {
        "generator scaffold_tenant.generate_defaults":
            _platform_section(scaffold_tenant.generate_defaults(all_packs)),
        "generator init_project._gen_defaults_yaml":
            _platform_section(_generated_defaults(
                "init_project._gen_defaults_yaml",
                init_project._gen_defaults_yaml(
                    sorted(init_project.RULE_PACK_CATALOG), "monitoring"))),
    }
    values = [_CHART_VALUES_REL] + sorted(
        os.path.relpath(p, PROJECT_ROOT)
        for p in glob.glob(str(PROJECT_ROOT / _ENVIRONMENT_VALUES_GLOB)))
    for rel in values:
        faces[f"helm values {rel}"] = _platform_section(
            _values_doc(rel).get("thresholdConfig") or {})
    for rel in _shipped_samples():
        try:
            doc = _parse_artifact(rel, (PROJECT_ROOT / rel).read_bytes())
        except Exception as exc:  # noqa: BLE001 — re-raised with provenance
            raise RuntimeError(f"{rel}: {exc}") from exc
        faces[f"sample {rel}"] = _platform_section(doc)
    return faces


def _report_producer_policy(faces: dict[str, tuple[dict[str, KeyInfo], list[str]]],
                            spellings: set[str], errors: list[str]) -> None:
    for face, (keys, listed) in sorted(faces.items()):
        for path, info in sorted(keys.items()):
            if _is_registry_key(info.leaf, spellings):
                errors.append(
                    f"REGISTRY-KEY-WRITTEN: {face} writes {path!r} under "
                    "`defaults:`, a threshold of the registry. The readers build "
                    "every registry key in (the platform's value, or off), so "
                    "this copy pins today's number for every tenant or arms one "
                    "only the deployment can calibrate. Delete it; a tenant, "
                    "subtree or profile that needs a value writes its own. "
                    "(#3924)")
        for name in listed:
            if _is_registry_key(name, spellings):
                errors.append(
                    f"REGISTRY-KEY-LISTED: {face} lists {name!r} under "
                    "`optional_overrides:`. Every registry key is built into the "
                    "readers, so the listing changes nothing; delete it. (#3924)")


def _report_fixture_prefixes(errors: list[str], paths: list[str] | None = None) -> None:
    arts = _tracked_defaults_artifacts(paths)
    for prefix in sorted(_FIXTURE_PREFIXES):
        if not any(rel.startswith(prefix) for rel in arts):
            errors.append(
                f"STALE-FIXTURE-PREFIX: {prefix!r} is exempt from the producer "
                "policy as test fixtures, but no tracked `_defaults*` artifact is "
                "under it any more — remove the entry so the exemption cannot "
                "cover a future sample tree. (#3924)")


# ── The writer census (#1412) ────────────────────────────────────────────────
#
# The generator faces above are ENUMERATED; the census catches a producer that
# ARRIVES. It parses `scripts/**/*.py` for three shapes — a dict literal
# carrying a `defaults` key, a subscript write into `…["defaults"]`, a
# multi-line string literal with a `defaults:` line (a YAML template) — and
# every module it finds must be a face or exempt BY NAME, with the MECHANISM
# that makes it safe. A stale exemption is itself a violation. Scope:
# `scripts/**` only; the chart's values and the samples are faces above
# because they are not Python.

_DEFAULTS_CENSUS_ROOT = PROJECT_ROOT / "scripts"

# The generator faces above: allowed to match the census, and read by the
# producer policy. #3924 left both writing no `defaults:` mapping, so they do
# not match today; the census's must-fire control is the exemption table
# (STALE-EXEMPTION: every exempt module must keep matching it).
_DEFAULTS_FACE_MODULES = frozenset({
    "scripts/tools/ops/scaffold_tenant.py",
    "scripts/tools/ops/init_project.py",
})

_DEFAULTS_WRITER_EXEMPT: dict[str, str] = {
    "scripts/tools/ops/onboard_platform.py":
        "PROJECTS the thresholds it recovered from the customer's own "
        "Alertmanager rules into a draft `_defaults.yaml` the customer reviews — "
        "the customer's numbers, not values the platform chose, which is what "
        "#3924 keeps out",
    "scripts/tools/dx/generate_tenant_fixture.py":
        "writes bench/test fixture trees from caller-supplied `--extra-defaults`; "
        "nothing it writes is shipped, and it refuses a `_critical` or "
        "dimensional key at that boundary (`_registry_lib.is_flat_key`); "
        "pinned by tests/dx/test_generate_tenant_fixture.py",
    "scripts/ops/inject_default_key.py":
        "injects one key into a bench-fixture tree; nothing it writes is shipped, "
        "and it refuses the same shapes at the same boundary; pinned by "
        "tests/ops/test_inject_default_key.py",
    "scripts/tools/dx/generate_platform_data.py":
        "PROJECTS the registry into docs data (docs/assets/platform-data.json); "
        "it writes no conf.d",
    "scripts/tools/ops/da_assembler.py":
        "passes an operator-supplied `defaults` block through into a conf.d "
        "document; the keys are the operator's own",
    "scripts/tools/ops/migrate_rule.py":
        "render_defaults_snippet PROJECTS the base of a rule the customer is "
        "migrating — the customer's own threshold, printed for review; pinned by "
        "tests/ops/test_migrate_threshold_wiring.py",
    "scripts/tools/ops/policy_opa_bridge.py":
        "READS `_defaults.yaml` (`load_defaults`); the census hit is the dict "
        "shape in its module docstring",
    "scripts/tools/dx/waveform_score.py":
        "a different domain entirely — its `defaults:` is the severity to "
        "tolerance-ceiling matrix, not threshold defaults",
    "scripts/tools/ops/backtest_threshold.py":
        "writes a TemporaryDirectory that `_served_written` removes after one "
        "`da-guard served-values` run (`_written_tree`, #3140 `--tenant`), so no "
        "conf.d it writes is shipped or read again",
    "scripts/tools/ops/config_diff.py":
        "READS `_` files (through `da-guard parse-tree`) and writes no conf.d; "
        "the census hit is RENDERED_CARRIERS, the table mapping the top-level "
        "key name `defaults` to the render that reads it (ADR-037 decision 3)",
}


# The two root keys the producer policy forbids a registry key under. ⛔ Both,
# not `defaults` alone: a generator that emitted only
# `optional_overrides: [<registry key>]` would otherwise be neither a face nor
# a census hit (#3924 review).
_CENSUS_KEYS = ("defaults", "optional_overrides")


def _defaults_writer_census() -> dict[str, set[str]]:
    """{module path: the census shapes it matches} over `scripts/**/*.py`.

    ⛔ Parsed, not grepped. A grep for the subscript text cannot tell a write
    from a read, and a comment mentioning the word counts the same as code.

    ⚠️ A file that does not parse is reported as `PARSE-FAIL` rather than
    skipped: skipping would make a syntactically broken producer invisible,
    which is the failure direction this whole floor is about.
    """
    import ast

    found: dict[str, set[str]] = {}

    def mark(path: Path, shape: str) -> None:
        found.setdefault(
            path.relative_to(PROJECT_ROOT).as_posix(), set()).add(shape)

    for path in sorted(_DEFAULTS_CENSUS_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            # ⛔ Three, not one (#1948 review). `SyntaxError` alone leaves two
            # ways for an unreadable file to CRASH this gate instead of being
            # reported: `read_text` raises `UnicodeDecodeError` on a file under
            # `scripts/` that is not valid UTF-8, and `ast.parse` raises
            # `ValueError` for an embedded null byte. Both escape `run_check`,
            # so `main()` prints "reachability check crashed" and the census
            # reports nothing at all — and the decode error does not even name
            # the file. `_assert_defaults_artifacts_match_schema` above learned
            # the same lesson from a blind review ("a repo whose comments are
            # largely CJK will meet that one"); this is that fix, one reader
            # over.
            mark(path, "PARSE-FAIL")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                if any(isinstance(k, ast.Constant) and k.value in _CENSUS_KEYS
                       for k in node.keys):
                    mark(path, "dict-literal")
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
                for target in targets:
                    cur = target
                    while isinstance(cur, ast.Subscript):
                        if (isinstance(cur.slice, ast.Constant)
                                and cur.slice.value in _CENSUS_KEYS):
                            mark(path, "subscript-write")
                        cur = cur.value
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                # ⛔ A `defaults:` line inside a MULTI-LINE literal. The
                # single-line form matched this module's own predicate string
                # (`line.strip() == "defaults:"`) and reported the gate itself as
                # an uncovered producer on the first run — a census that finds
                # its own source text is measuring the wrong thing, and the
                # shape it is actually looking for is a rendered document.
                lines = node.value.splitlines()
                if len(lines) > 1 and any(ln.strip() in {f"{k}:" for k in _CENSUS_KEYS}
                                          for ln in lines):
                    mark(path, "yaml-template")
    return found


def _report_defaults_writer_census(errors: list[str], stats: dict | None = None) -> None:
    """Every census hit must be a face, or an exemption naming a mechanism."""
    census = _defaults_writer_census()
    if not census:
        errors.append(
            "CENSUS-EMPTY: the `defaults:` writer census found nothing under "
            f"{_DEFAULTS_CENSUS_ROOT.name}/, which cannot be true — the generator "
            "faces live there. The scan or its predicates broke. (#1412)")
        return
    for module in sorted(census):
        if module in _DEFAULTS_FACE_MODULES or module in _DEFAULTS_WRITER_EXEMPT:
            continue
        errors.append(
            f"UNCOVERED-DEFAULTS-WRITER: {module} writes a `defaults:` mapping "
            f"({', '.join(sorted(census[module]))}) and is neither a face of this "
            "gate nor exempt — it may ship a registry key into a customer's root "
            "defaults (#3924). Add it as a generator face, or make the shape "
            "impossible at its input boundary and exempt it here naming that "
            "mechanism. (#1412)")
    for module in sorted(_DEFAULTS_WRITER_EXEMPT):
        if module not in census:
            errors.append(
                f"STALE-EXEMPTION: {module} is exempt from the `defaults:` writer "
                "census but no longer matches it — remove the entry. (#1412)")
    if stats is not None:
        stats["defaults_writer_census"] = len(census)
        stats["defaults_writer_faces"] = len(set(census) & _DEFAULTS_FACE_MODULES)
        stats["defaults_writer_exempt"] = len(set(census) & set(_DEFAULTS_WRITER_EXEMPT))


def _reachable(key: str, supply: set[str], deferred: set[str]) -> bool:
    if key in deferred:
        return True
    if key.endswith(_CRITICAL_SUFFIX):
        return key[: -len(_CRITICAL_SUFFIX)] in supply
    return key in supply


def run_check(
    demand: set[str] | None = None,
    supply: set[str] | None = None,
    deferred: set[str] | None = None,
    producer_faces: dict[str, tuple[dict[str, KeyInfo], list[str]]] | None = None,
    spellings: set[str] | None = None,
) -> dict[str, object]:
    """Return {errors, infos, stats}. errors fail --ci.

    Inputs default to the real extractors; hermetic tests inject synthetic sets.
    A caller that injects a synthetic demand is exercising reachability, so the
    producer faces then default to none (the census, the fixture exit-lock and
    the schema witness read the real repo and still run only for a real run).
    """
    real = demand is None
    if demand is None:
        demand = observed_map_lib.all_threshold_keys(observed_map_lib.default_pack_paths())
    if supply is None:
        supply = _supply()
    if deferred is None:
        deferred = set(observed_map_lib.KNOWN_DEFERRED)
    if spellings is None:
        spellings = _registry_spellings()
    if producer_faces is None:
        producer_faces = _producer_faces() if real else {}

    errors: list[str] = []
    infos: list[str] = []
    stats: dict[str, object] = {"producer_faces": len(producer_faces)}

    for k in sorted(k for k in demand if not _reachable(k, supply, deferred)):
        errors.append(
            f"UNREACHABLE: alert-demanded threshold key {k!r} is not a key of "
            "rule-packs/threshold-registry.yaml (nor the base of one, nor "
            "known-deferred), so no reader serves it and this alert can never "
            "fire. Add the key to the registry, or fix the name mismatch. (TRK-337)")

    _report_producer_policy(producer_faces, spellings, errors)

    if real:
        _report_fixture_prefixes(errors)
        scanned = _tracked_defaults_artifacts()
        judged = _assert_defaults_artifacts_match_schema(scanned)
        if judged < _DEFAULTS_ARTIFACT_FLOOR:
            raise _GateViolation(
                f"the schema witness judged only {judged} document(s) out of "
                f"{len(scanned)} tracked defaults artifact(s), below the floor of "
                f"{_DEFAULTS_ARTIFACT_FLOOR} — the reader stopped reading rather "
                "than the tree got cleaner. (#1443)")
        stats["defaults_artifacts"] = len(scanned)
        stats["shipped_samples"] = len(_shipped_samples())
        _report_defaults_writer_census(errors, stats)

    return {"errors": errors, "infos": infos, "stats": stats}


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "閾值 key 可達性與生產器政策：每個 alert 讀的閾值 key 都要在登錄表裡；出貨的生產器與範例不得把登錄表的鍵寫進根層 defaults（TRK-337、#3924）",
            "Threshold-key reachability and producer policy: every alert-read key "
            "must be a registry key; no shipped producer or sample may write a "
            "registry key into root defaults (TRK-337, #3924)"))
    parser.add_argument(
        "--ci", action="store_true",
        help=i18n_text("有任何違規即 exit 1", "exit 1 on any violation"))
    args = parser.parse_args(argv)

    try:
        result = run_check()
    except _GateViolation as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK
    except Exception as exc:  # noqa: BLE001 — caller error, not a violation
        import _lib_consumer_read  # noqa: PLC0415
        if isinstance(exc, _lib_consumer_read.ConsumerReadError):
            print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
            return EXIT_CALLER_ERROR
        print(f"ERROR: reachability check crashed: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    errors, stats = result["errors"], result["stats"]
    print(
        f"INFO: producer policy: {stats['producer_faces']} face(s) read "
        f"({stats.get('shipped_samples', 0)} shipped sample(s) of "
        f"{stats.get('defaults_artifacts', 0)} tracked `_defaults*` artifact(s)); "
        f"defaults-writer census: {stats.get('defaults_writer_census', 0)} module(s), "
        f"{stats.get('defaults_writer_faces', 0)} face(s), "
        f"{stats.get('defaults_writer_exempt', 0)} exempt.", file=sys.stderr)
    for msg in result["infos"]:
        print(f"INFO: {msg}", file=sys.stderr)
    for msg in errors:
        print(f"❌ {msg}", file=sys.stderr)
    if errors:
        print(f"\n{len(errors)} threshold-reachability violation(s) — TRK-337 / #3924. "
              "See scripts/tools/lint/check_threshold_reachability.py.", file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK
    print("✅ threshold reachability OK — every alert-read key is a registry key, "
          "and no shipped producer or sample writes one.", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
