"""Tests for scripts/tools/lint/check_threshold_reachability.py (TRK-337, #3924).

Two questions: every alert-demanded threshold key must be one the readers serve
(a registry key — each is built in, #3832 / #4163 — or known-deferred), and no
shipped producer or sample writes a registry key into a customer's root
platform defaults (#3924). Plus the readers both stand on: the schema witness
over every tracked `_defaults*` artifact, the index seam, and the writer census.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys

import pytest
import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_threshold_reachability.py"

_spec = importlib.util.spec_from_file_location("check_threshold_reachability", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)



def _stub_result(errors=(), infos=()):
    """A `run_check` return value of the current shape, for `main()` stubs.
    The stats are absurd on purpose: these stubs are about `main()`'s exit-code
    contract, and the real figures come from a real call elsewhere."""
    return {"errors": list(errors), "infos": list(infos),
            "stats": {"producer_faces": -1, "shipped_samples": -1,
                      "defaults_artifacts": -1, "defaults_writer_census": -1,
                      "defaults_writer_faces": -1, "defaults_writer_exempt": -1}}


_RENAME_HEADLINE = "MOST LIKELY: a `defaults:` section was mis-spelled"


_WITNESS_NESTED = "defaults:\n  threshold:\n    cpu: 90\n"


# ⛔ A REAL recipe shape, copied from the finance domain file under
# `rule-packs/recipes/examples/conf.d/`, not a plausible-looking one. (The path
# is named in two pieces on purpose — writing it whole wrapped it mid-token and
# `tests/ops/test_wrapped_path_references.py` caught it, which is the guard
# doing exactly its job.) The first draft here
# wrote `- name: x / expr: up == 0` and the witness rejected it for a missing
# `recipe` key — correctly, and that is the point: this witness validates the
# WHOLE platform-defaults schema, not only the set of top-level key names. A
# fixture that is merely "shaped like" the tree would have made the two silent
# rows below prove nothing.
_WITNESS_NO_SECTION = (
    "_custom_alerts:\n"
    "  - recipe: ratio\n"
    "    name: payment_failure_ratio\n"
    "    metric: payment_failed_total\n"
    "    denominator_metric: payment_attempts_total\n"
    "    op: \">\"\n"
    "    window: 5m\n"
    "    threshold: \"0.01:critical\"\n")


_WITNESS_OK = "defaults:\n  cpu: 90\n"


_WITNESS_PLACEHOLDER = "# nothing declared here yet\n"


_WITNESS_RENAMED = "defalts:\n  cpu: 90\n"


def _witness(docs: dict[str, str]) -> int:
    """Drive the REAL witness over synthetic artifact text (reader seam only);
    the documents it judged."""
    return gate._assert_defaults_artifacts_match_schema(
        sorted(docs), read=lambda rel: docs[rel])


def test_new_unreachable_key_is_an_error():
    """A freshly-added alert demanding a key no reader serves fails."""
    result = gate.run_check(
        demand={"oracle_sessions_active", "newpack_brand_new_metric"},
        supply={"oracle_sessions_active"},
        deferred=set(),
        spellings=set(),
    )
    assert any("newpack_brand_new_metric" in e and "UNREACHABLE" in e
               for e in result["errors"]), result


def test_supplied_key_is_not_flagged():
    result = gate.run_check(
        demand={"oracle_sessions_active"},
        supply={"oracle_sessions_active"},
        deferred=set(),
        spellings=set(),
    )
    assert result["errors"] == []


def test_critical_key_reachable_when_base_is_supplied():
    """A `_critical` key rides resolveCriticalRows: reachable iff its BASE is in
    supply, even though the `_critical` key itself is not."""
    result = gate.run_check(
        demand={"mysql_connection_usage_percent_critical"},
        supply={"mysql_connection_usage_percent"},          # base only
        deferred=set(),
        spellings=set(),
    )
    assert result["errors"] == [], result


def test_critical_key_dead_when_base_missing():
    result = gate.run_check(
        demand={"mysql_connection_usage_percent_critical"},
        supply=set(),                          # base absent
        deferred=set(),
        spellings=set(),
    )
    assert any("mysql_connection_usage_percent_critical" in e for e in result["errors"])


def test_known_deferred_is_exempt():
    """A key whose threshold lives in a :core recording rule is deferred, not dead."""
    result = gate.run_check(
        demand={"container_cpu"},
        supply=set(),
        deferred={"container_cpu"},
        spellings=set(),
    )
    assert result["errors"] == []


def test_main_without_ci_is_report_only(monkeypatch):
    """Bare `main([])` never fails on errors — it is report-only, so a green
    local commit is not gated on the (grandfathered) unwired set."""
    monkeypatch.setattr(gate, "run_check", lambda: _stub_result(["some drift"]))
    assert gate.main([]) == gate.EXIT_OK


def test_main_ci_fails_on_errors(monkeypatch):
    """`--ci` escalates any error (new unreachable OR exit-lock breach) to
    EXIT_VIOLATION — this is what the CI hook relies on."""
    monkeypatch.setattr(gate, "run_check", lambda: _stub_result(["a breach"]))
    assert gate.main(["--ci"]) == gate.EXIT_VIOLATION


def test_main_ci_passes_when_clean(monkeypatch):
    """`--ci` returns 0 when there are no errors, even with INFO-level
    grandfathered entries present."""
    monkeypatch.setattr(gate, "run_check",
                        lambda: _stub_result(infos=["18 known-unwired"]))
    assert gate.main(["--ci"]) == gate.EXIT_OK


def test_the_schema_witness_is_silent_on_the_real_tree():
    """Anti-vacuity first: it must be handed the real scan, and pass on it.

    ⛔ Assert the JUDGED count, not just "did not raise". Silence is what a
    stubbed reader also produces; the count is what tells the two apart.
    Since #2906 the witness reads each artifact through `da-guard
    parse-tree`: ONE document per file (a comment-only placeholder is the
    empty mapping, judged; a second document is a file it cannot read).
    """
    tracked = gate._tracked_defaults_artifacts()
    assert len(tracked) >= gate._DEFAULTS_ARTIFACT_FLOOR, tracked
    judged = gate._assert_defaults_artifacts_match_schema(tracked)  # must NOT raise

    # ⛔ EQUALITY AGAINST AN INDEPENDENT RECOUNT, not `judged >= floor`. Blind
    # review replaced the returned count with the floor constant itself and the
    # whole suite stayed green: the inequality was satisfied by the very number
    # it was compared against, and the other control stubs the witness out
    # entirely so it never sees a real return value. This side re-derives the
    # document count from the tree without calling the witness at all.
    # One document per tracked file that exists — counted from the index
    # listing, not from anything the witness returns or reads.
    expected = sum(1 for rel in tracked if (gate.PROJECT_ROOT / rel).is_file())
    assert judged == expected, (judged, expected)


def test_the_schema_witness_refuses_an_empty_input():
    """A check that validates nothing passes perfectly."""
    with pytest.raises(gate._GateViolation) as exc:
        gate._assert_defaults_artifacts_match_schema([])
    assert "no defaults artifacts" in str(exc.value), exc.value

    # …and an EMPTY file is not "nothing" since #2906: `da-guard parse-tree`
    # reads it as the empty mapping, the exporter's reading — two documents
    # judged, both clean.
    assert _witness({"a/conf.d/_defaults.yaml": "", "b/conf.d/_defaults.yaml": ""}) == 2


def test_the_witness_catches_a_renamed_section_and_the_pinned_members_stay_caught():
    """The attack reddens, and the two names that must stay CAUGHT are PINNED.

    ⛔ A PIN, NOT A CLASSIFIER, and the distinction is why the enumeration here
    is correct where the previous one was not. The version this replaces built
    `silent`/`caught` by probing every property and then asserted a PARTITION —
    which is true by construction (each name enters exactly one side), so blind
    review measured both directions escaping it: widening the schema so
    `optional_overrides` stops being caught left the suite green, and making the
    witness invent a violation on a legal `_metadata:` section also left it
    green. A classifier over an open input space cannot be enumerated; a pin
    over THIS repo's two shaped properties is closed, single, and any change to
    it should force a review.

    ⚠️ The SILENT side is deliberately NOT asserted here — see the disclosure
    block below for what that costs and why it is still the right trade.
    """
    # the attack this gate exists for: a name the schema does not allow
    for bad in ("defalts", "defaultss", "Defaults"):
        with pytest.raises(gate._GateViolation) as exc:
            _witness({"x/conf.d/_defaults.yaml": f"{bad}:\n  cpu: 90\n"})
        assert bad in str(exc.value), exc.value

    # …and the two properties whose value shape makes them catchable must stay
    # catchable. If a schema change makes one of these silent it widens the
    # #1443 bypass, and that red is correct rather than a maintenance cost.
    for name in ("_custom_alerts", "optional_overrides"):
        with pytest.raises(gate._GateViolation):
            _witness({"x/conf.d/_defaults.yaml": f"{name}:\n  cpu: 90\n"})

    # the shapes the real tree actually has must stay silent — a witness that
    # reddens on today's tree gets deleted, not fixed
    _witness({"a/conf.d/_defaults.yaml": _WITNESS_OK,
              "b/conf.d/_defaults.yaml": _WITNESS_PLACEHOLDER,
              "c/conf.d/_defaults.yaml": _WITNESS_NO_SECTION,
              "d/conf.d/_defaults.yaml": _WITNESS_NESTED})


def test_the_witness_reads_every_artifact_and_every_document_in_it():
    """A partial reader hides the attack in whatever it stopped reading.

    Blind review truncated the loop to `rels[:1]` and swapped `safe_load_all`
    for `safe_load`; both left the whole suite green, because no test had ever
    handed the witness an offender anywhere but first, and no tracked artifact
    is a multi-document file.
    """
    # offender is NOT first — `sorted()` puts the clean one ahead of it
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"a/conf.d/_defaults.yaml": _WITNESS_OK,
                  "b/conf.d/_defaults.yaml": _WITNESS_RENAMED})
    assert "b/conf.d/_defaults.yaml" in str(exc.value), exc.value

    # offender is in the SECOND YAML document of a single file: since #2906
    # (`da-guard parse-tree`, the exporter's reading) a second document is a
    # file that cannot be read at all — named, never half-judged
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"a/conf.d/_defaults.yaml":
                  _WITNESS_OK + "---\ndefalts:\n  cpu: 1\n"})
    assert "a/conf.d/_defaults.yaml" in str(exc.value), exc.value
    assert "multiple_documents" in str(exc.value), exc.value

    # ⛔ The two counts are read back as NUMBERS, not matched as a sentence.
    # The first spelling asserted the literal "2 violation(s) across 2 defaults
    # artifact(s)", so re-wording the message reddened a test whose subject is
    # arithmetic. The invariant is that the two populations differ, which needs
    # a fixture where they do: three files, one bad.
    def _counts(docs):
        with pytest.raises(gate._GateViolation) as exc:
            _witness(docs)
        found = re.findall(r"(\d+) violation\(s\) across (\d+)", str(exc.value))
        assert found, exc.value
        return tuple(int(n) for n in found[0])

    assert _counts({"a/conf.d/_defaults.yaml": _WITNESS_RENAMED,
                    "b/conf.d/_defaults.yaml": _WITNESS_RENAMED}) == (2, 2)
    assert _counts({"a/conf.d/_defaults.yaml": _WITNESS_OK,
                    "b/conf.d/_defaults.yaml": _WITNESS_OK,
                    "c/conf.d/_defaults.yaml": _WITNESS_RENAMED}) == (1, 1)


def test_the_witness_validates_artifacts_outside_conf_d_without_refusing_them():
    """A `_defaults*` file outside every conf.d tree is checked, not rejected.

    ⛔ Two wrong shapes preceded this one, both measured by blind review. A
    silent filter dropped such a file with no report, which
    `guard_defaults_scopes.py` forbids for unmanaged paths — ⚠️ though the
    authority cited for that was overstated: that module's own `resolve()`
    collects unmanaged paths and CARRIES ON, it does not stop the run. A hard
    refusal then made a perfectly legal Go-side `testdata/_defaults.yaml` fail
    the whole gate with no in-repo way to go green.

    What is left is the narrow true statement: the schema is a conf.d contract,
    so a violation found OUTSIDE a conf.d tree may be the contract not applying
    rather than the file being wrong — and the message says so instead of
    diagnosing a renamed section.
    """
    # legal content outside conf.d: checked, and silent
    _witness({"a/conf.d/_defaults.yaml": _WITNESS_OK,
              "components/x/testdata/_defaults.yaml": _WITNESS_OK})

    # a real violation outside conf.d: reported, but NOT as a renamed section
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"tools/portal/_defaults.yaml": "theme: dark\n"})
    msg = str(exc.value)
    assert "tools/portal/_defaults.yaml" in msg, msg
    assert "OUTSIDE every `conf.d` tree" in msg, msg
    assert "mis-spelled" not in msg, msg


def test_the_rename_headline_is_bound_to_the_artifact_it_diagnoses(monkeypatch):
    """The headline must come from a conf.d artifact's OWN violation.

    ⛔ THE TWO-FILE CASE IS THE WHOLE POINT. The condition used to ask its two
    halves of DIFFERENT populations — "is there any conf.d offender" over
    `offenders`, and "does anything look like a rejected key" over ALL
    `problems`. Every single-file case passes under both spellings, so a round
    of single-file tests could not tell them apart; it took one unrelated
    out-of-conf.d file to re-arm the headline for everybody.

    Measured before the fix, and the reason this test exists: `{x/conf.d:
    "state_filters: 5", tools/portal: "theme: dark"}` told the owner of
    `x/conf.d` to "restore the section name" of a `defaults:` block that file
    never lost — the exact false diagnosis this headline was made conditional
    to stop.
    """
    # ⛔ ASK THE PREDICATE FIRST, and derive its input rather than quoting one.
    # The headline is gated on `_REJECTED_TOP_LEVEL_KEY`, which matches
    # jsonschema's WORDING. A library upgrade that rewords `additionalProperties`
    # /`patternProperties` failures makes the GATE degrade to the ⚠️ branch — on
    # purpose, and safely. Without this check the MUST-FIRE half below would be
    # the thing that reddens, naming neither jsonschema nor the predicate, and
    # its cheapest green is deleting the half that has teeth.
    import json as _json

    import jsonschema as _js
    # ⛔ The literal path is here on purpose, and it is also an assertion. This
    # test's subject is the schema, but it reaches it through
    # `gate._PLATFORM_DEFAULTS_SCHEMA`, and `verify_diff.py`'s text map is built
    # from literals — so without this line a schema-only edit does not select
    # this test locally. (CI's Python leg is a catch-all and runs it regardless;
    # what is bought is the fast feedback, on the one test whose whole job is to
    # notice schema and library drift.)
    assert gate._PLATFORM_DEFAULTS_SCHEMA.as_posix().endswith(
        "docs/schemas/platform-defaults.schema.json"), gate._PLATFORM_DEFAULTS_SCHEMA
    # ⛔ `syspath_prepend`, not a bare `sys.path.insert`: this is process
    # GLOBAL state, and an unguarded insert leaves it behind for every later
    # test in the session (the sibling probe higher up in this file at least
    # guards with `not in sys.path`; this one did not). `scripts/tools/lint`
    # holds a dozen `check_*` modules, so a stale entry is a shadowing hazard,
    # not just untidiness.
    monkeypatch.syspath_prepend(
        str(gate.PROJECT_ROOT / "scripts" / "tools" / "lint"))
    import check_confd_schema as _sibling
    _real = _sibling.defaults_doc_violations(
        "probe/conf.d/_defaults.yaml", {"defalts": {"cpu": 90}},
        _json.loads(gate._PLATFORM_DEFAULTS_SCHEMA.read_text(encoding="utf-8")),
        _js)
    assert _real and any(gate._REJECTED_TOP_LEVEL_KEY(p) for p in _real), (
        "jsonschema no longer words a rejected top-level key the way "
        "`_REJECTED_TOP_LEVEL_KEY` matches it. The gate degrades safely — it "
        "stops printing the MOST LIKELY headline — so the fix is to update the "
        "predicate, NOT to relax anything below.", _real)
    # ⛔ AND THE OTHER SPELLING, which today's schema can never produce: its one
    # `additionalProperties: false` sits at the root and carries
    # `patternProperties`, so jsonschema always says "regexes". Blind review
    # measured the second disjunct silently deletable because of that. Probe it
    # against a stripped copy so it is a branch that has been RUN, not one
    # assumed to work the day a schema edit drops those prefixes.
    _stripped = {k: v for k, v in _json.loads(
        gate._PLATFORM_DEFAULTS_SCHEMA.read_text(encoding="utf-8")).items()
        if k != "patternProperties"}
    _alt = _sibling.defaults_doc_violations(
        "probe/conf.d/_defaults.yaml", {"defalts": {"cpu": 90}}, _stripped, _js)
    assert _alt and any(gate._REJECTED_TOP_LEVEL_KEY(p) for p in _alt), (
        "the `Additional properties are not allowed` spelling is no longer "
        "recognised. That is the wording jsonschema uses when the schema has no "
        "`patternProperties`; keep the predicate able to read both.", _alt)

    # MUST FIRE: the conf.d artifact's own violation IS a rejected top-level key
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"x/conf.d/_defaults.yaml": "defalts:\n  cpu: 90\n"})
    assert _RENAME_HEADLINE in str(exc.value), exc.value

    # MUST NOT FIRE: this conf.d artifact's own fault is a VALUE SHAPE; the
    # rejected key belongs to a different file, outside every conf.d tree
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"x/conf.d/_defaults.yaml": "state_filters: 5\n",
                  "tools/portal/_defaults.yaml": "theme: dark\n"})
    msg = str(exc.value)
    assert _RENAME_HEADLINE not in msg, msg
    # …and neither real finding may be lost in the process
    assert "state_filters" in msg, msg
    assert "tools/portal/_defaults.yaml" in msg, msg


def test_the_undiagnosed_branch_claims_nothing_about_keys_it_never_read():
    """A file that fails to PARSE has no top-level keys to vouch for.

    The wording this replaces said "every top-level key here is one this schema
    accepts" — told to a maintainer whose YAML was broken, about a file where
    not one top-level key had been looked at. Both disjuncts of that sentence
    were false for this input: the file is inside a conf.d tree, and nothing
    was parsed.
    """
    with pytest.raises(gate._GateViolation) as exc:
        _witness({"bad/conf.d/_defaults.yaml": "defaults:\n  cpu: 90\n bad: [\n"})
    msg = str(exc.value)
    assert "cannot read/parse YAML" in msg, msg
    # ⛔ …and the file is still COUNTED. Blind review deleted `offenders.add`
    # from the parse arm and nothing noticed: the header then reads "across 0
    # defaults artifact(s)", and an unparseable file outside conf.d loses its
    # scope note, because `outside` is derived from that same set.
    assert "across 1 defaults artifact" in msg, msg
    assert _RENAME_HEADLINE not in msg, msg
    # ⛔ NON-VACUITY: prove the ⚠️ branch is the one that was taken. Without it
    # the two assertions above are satisfied by messages that never reach it.
    assert "NOT diagnosed as a renamed" in msg, msg
    # ⛔ The defect, pinned as the ABSENCE of the sentence that shipped. The
    # property — "vouches for no key it never read" — has no positive form a
    # test can check, and the first spelling here asserted the PRESENCE of
    # today's wording instead: a pure re-wording of the branch reddened it, and
    # the cheapest green was deleting this line. ⚠️ A differently worded
    # universal claim would still pass. Disclosed, not covered.
    assert "every top-level key here is one this schema accepts" not in msg, msg


def test_the_index_listing_is_a_real_seam_on_both_derivations():
    """⛔ `_tracked_paths` is injectable so BOTH derivations can be fed a world.

    `_tracked_defaults_artifacts`' docstring said "a test injects a synthetic
    listing" while nothing did — the parameter had no caller and no assertion,
    so the sentence described an intention and the artifact half of the seam was
    dead code with a claim attached. A seam nobody pulls is indistinguishable
    from a seam that does not work.
    """
    # ⚠️ The predicate also requires the file to EXIST on disk, so a synthetic
    # listing has to name real files. Real defaults artifacts are used as the
    # positive cases and real non-artifacts as the negatives; what is synthetic
    # is the LISTING, which is the input under test.
    real = gate._tracked_defaults_artifacts()
    assert len(real) >= gate._DEFAULTS_ARTIFACT_FLOOR, real

    assert gate._tracked_defaults_artifacts([]) == []
    assert gate._tracked_defaults_artifacts(real[:2]) == sorted(real[:2])
    # a path that is in the listing but is not an artifact is filtered out…
    assert gate._tracked_defaults_artifacts(
        real[:1] + ["CHANGELOG.md", "Makefile"]) == real[:1]
    # …and one that never reaches the listing is not conjured up from anywhere.
    assert real[-1] not in gate._tracked_defaults_artifacts(real[:-1])
    # a name that matches the predicate but has no file behind it is dropped,
    # so an injected listing cannot manufacture an artifact out of a string.
    assert gate._tracked_defaults_artifacts(
        ["nowhere/at/all/conf.d/_defaults.yaml"]) == []


def test_the_index_listing_really_comes_from_the_index(tmp_path):
    """⛔ The seam moved the question up a level; this is where it stops.

    Injecting `paths` pins the CALLEES. It says nothing about the argument they
    build when nobody passes one, and that is `_tracked_paths` itself. Measured
    against the suite as it stood at `3864b63`, before this test existed:
    replacing its whole body with a literal list of today's answer left that
    suite at 107 passed and the gate at rc=0 — every test either fed its own
    listing or compared this against a `git ls-files` it ran itself, and a
    constant copied from today satisfies both. It would drift eventually, when
    some unrelated commit adds a file; "eventually" is a delay, not a tripwire.

    ⚠️ THE POPULATION IS PART OF THAT NUMBER. Re-run on the final tree, the
    same mutation is 1 failed / 113 passed and the one red is THIS test — which
    is the point, but a reader who meets "107 passed" beside a 114-test file
    has been handed a figure that no longer describes anything. Same defect as
    a fraction quoted without the set it was counted over, which is what this
    whole issue is about.

    So the function is pointed at a DIFFERENT repository. A body that ignores
    `PROJECT_ROOT` cannot follow, and no snapshot of THIS repo can be right
    about a repo that did not exist when the snapshot was taken.
    """
    (tmp_path / "some" / "conf.d").mkdir(parents=True)
    (tmp_path / "some" / "conf.d" / "_defaults.yaml").write_text(
        "defaults:\n  x: 1\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("not added\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(tmp_path), "add",
                    "some/conf.d/_defaults.yaml"], check=True,
                   capture_output=True, timeout=60)

    real_root = gate.PROJECT_ROOT
    try:
        gate.PROJECT_ROOT = tmp_path
        assert gate._tracked_paths() == ["some/conf.d/_defaults.yaml"], (
            "`_tracked_paths` did not follow PROJECT_ROOT — it is not reading "
            "an index, it is reciting one")
    finally:
        # ⛔ Restored by hand rather than with monkeypatch because
        # `_tracked_paths` is module state that everything else in this file
        # reads; a teardown that ran late would hand another test a listing of
        # a directory that has since been deleted.
        gate.PROJECT_ROOT = real_root
    assert gate.PROJECT_ROOT == real_root
    assert len(gate._tracked_paths()) > 100, "the real listing did not come back"


def test_the_hook_filter_is_as_case_insensitive_as_the_predicate():
    """⛔ A case-SENSITIVE trigger filter beside a case-INSENSITIVE reader is the
    same divergence this round fixed, one layer out: the gate would read
    `_defaults.YAML` while the hook never fired on the commit that added it.

    Derived from the predicate rather than restated — the case variants come from
    whatever `_is_defaults_artifact` accepts, so widening one and not the other
    is what fails here.
    """
    import re


    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [h for repo in config["repos"] for h in repo["hooks"]
             if h["id"] == "threshold-reachability-check"]
    assert len(hooks) == 1
    pattern = re.compile(hooks[0]["files"])

    for name in ("_defaults.yaml", "_defaults.YAML", "_Defaults.yml",
                 "_defaults-multidb.YAML"):
        assert gate._is_defaults_artifact(name), name          # the reader takes it
        rel = f"components/threshold-exporter/config/conf.d/{name}"
        assert pattern.match(rel), (rel, "reader accepts it, hook filter does not")


def test_the_artifact_predicate_matches_the_loader_not_the_sibling_lint():
    """`config_hierarchy.go` lowercases before BOTH its suffix test and its
    filename test, so `_defaults.YAML` is loaded at runtime. Borrowing
    `check_confd_schema.py`'s case-SENSITIVE spelling let that file through on
    every platform, and made `_Defaults.yaml` red on Windows / green on Linux CI
    — the wrong way round (blind review, round 4)."""
    for name in ("_defaults.yaml", "_defaults.YAML", "_Defaults.yml",
                 "_defaults-multidb.YAML"):
        assert gate._is_defaults_artifact(name), name
    for name in ("_defaultsx.txt", "defaults.yaml", "_routing_profiles.yaml"):
        assert not gate._is_defaults_artifact(name), name


def test_the_scan_hands_the_predicate_every_candidate_not_a_narrower_set():
    """⛔ The predicate being case-insensitive buys nothing if what FEEDS it is
    not. `git ls-files -- "*_defaults*"` is case-sensitive (measured under both
    `core.ignorecase` values: `*_DEFAULTS*` → 0), so pairing the two delivered
    half a fix — `_defaults.YAML` got through on its lowercase stem while
    `_Defaults.yaml` and `_DEFAULTS.yml` never reached the predicate at all.

    Neither existing test could see it: one exercises the predicate, the other
    the hook's `(?i:)` trigger filter. **The scan was the untested link.** So
    this asserts the scan lists the whole index and leaves the narrowing to
    `_is_defaults_artifact` — i.e. that there is exactly ONE predicate.
    """
    import subprocess

    listed = subprocess.run(
        ["git", "-C", str(gate.PROJECT_ROOT), "ls-files", "-z"],
        capture_output=True, check=True, timeout=60).stdout
    everything = [p for p in listed.decode("utf-8", "replace").split("\0") if p]
    assert len(everything) > 1000, len(everything)   # the whole index, not a slice

    expected = sorted(
        p for p in everything
        if gate._is_defaults_artifact(p.rsplit("/", 1)[-1])
        and (gate.PROJECT_ROOT / p).is_file())
    assert gate._tracked_defaults_artifacts() == expected, (
        "the scan is narrowing before the predicate runs — any filter applied "
        "there is a second, unaudited predicate")


def test_the_scan_is_tracked_scope_not_a_filesystem_walk():
    """⛔ Measured, and the reason this is not an `rglob`: this repo keeps git
    worktrees under `.claude/worktrees/`, so a filesystem walk from the main repo
    root yielded **643 files in 18.7s, 618 of them other branches' working
    copies** — a commit could be blocked by an error naming a path outside the
    author's tree. Tracked scope is 17.
    """
    import subprocess

    tracked = set(gate._tracked_defaults_artifacts())
    assert tracked, "empty scan — the floor should have caught this first"
    listed = subprocess.run(
        ["git", "-C", str(gate.PROJECT_ROOT), "ls-files"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, timeout=60).stdout.split()
    assert tracked <= set(listed), sorted(tracked - set(listed))
    assert not [r for r in tracked if r.startswith(".claude/")], sorted(tracked)


def test_a_new_defaults_writer_is_reported_not_counted(tmp_path, monkeypatch):
    """Plant a sixth producer; the gate must name it.

    ⛔ Planted as a real FILE under a real root and read through the real census,
    not by stubbing the census's return value — a stub would prove that the
    reporting code works while leaving the question "do the predicates see a new
    module" unanswered, which is the question #1412 is about.
    """
    root = tmp_path / "scripts"
    (root / "tools" / "dx").mkdir(parents=True)
    planted = root / "tools" / "dx" / "smuggle_defaults.py"
    planted.write_text(
        "def build():\n"
        "    doc = {}\n"
        "    doc['defaults'] = {}\n"
        "    doc['defaults']['mysql_connection_usage_percent_critical'] = 90\n"
        "    return doc\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_DEFAULTS_CENSUS_ROOT", root)

    census = gate._defaults_writer_census()
    assert "scripts/tools/dx/smuggle_defaults.py" in census, census

    errors: list[str] = []
    gate._report_defaults_writer_census(errors)
    hits = [e for e in errors if "UNCOVERED-DEFAULTS-WRITER" in e]
    assert len(hits) == 1, errors
    assert "smuggle_defaults.py" in hits[0]
    # The message has to say what to DO — add a face, or refuse at the boundary.
    assert "input boundary" in hits[0] and "generator face" in hits[0]
    # ⛔ And the exemptions are now stale against this planted tree, which is the
    # other direction of the same rule: every one of them must be reported.
    assert len([e for e in errors if "STALE-EXEMPTION" in e]) == len(
        gate._DEFAULTS_WRITER_EXEMPT)


def test_an_undecodable_module_is_reported_not_a_crash(tmp_path, monkeypatch):
    """A file that cannot be decoded must land as PARSE-FAIL, not as a traceback.

    ⛔ Measured before the fix: `read_text(encoding="utf-8")` raised
    `UnicodeDecodeError` straight out of `run_check`, so `main()` printed
    "reachability check crashed" — the census reported NOTHING, and the message
    did not even name the file. The sibling artifact reader in this module
    carries the same fix for the same reason.
    """
    root = tmp_path / "scripts"
    root.mkdir()
    (root / "latin1.py").write_bytes(b"# caf\xe9 = 1\nX = 1\n")
    (root / "nullbyte.py").write_bytes(b"X = 1\x00\n")
    monkeypatch.setattr(gate, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_DEFAULTS_CENSUS_ROOT", root)

    census = gate._defaults_writer_census()
    assert census.get("scripts/latin1.py") == {"PARSE-FAIL"}, census
    assert census.get("scripts/nullbyte.py") == {"PARSE-FAIL"}, census

    errors: list[str] = []
    gate._report_defaults_writer_census(errors)
    named = [e for e in errors if "latin1.py" in e or "nullbyte.py" in e]
    assert len(named) == 2 and all("PARSE-FAIL" in e for e in named), errors


def test_an_empty_census_fails_closed(tmp_path, monkeypatch):
    """A scan that reads nothing is a broken scan, not a clean repository."""
    empty = tmp_path / "scripts"
    empty.mkdir()
    monkeypatch.setattr(gate, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_DEFAULTS_CENSUS_ROOT", empty)

    errors: list[str] = []
    gate._report_defaults_writer_census(errors)
    assert len(errors) == 1 and "CENSUS-EMPTY" in errors[0], errors


def test_every_census_exemption_names_a_mechanism():
    """⛔ The list that could rot this floor back into an enumeration.

    An exemption is allowed to say "this is not a threshold-defaults producer",
    but only with a reason a reader can CHECK: a refusal in code, another gate
    that reads the result, or "removes keys only". A judgement ("harmless",
    "not relevant") is how a census degrades into the hand-written list it
    replaced, one plausible sentence at a time.
    """
    verbs = ("refuses", "PROJECTS", "passes", "removes", "READS", "domain")
    for module, reason in sorted(gate._DEFAULTS_WRITER_EXEMPT.items()):
        assert len(reason) >= 40, (module, reason)
        assert any(v in reason for v in verbs), (
            f"{module}'s exemption reason names no mechanism — it must say what "
            f"makes the shape impossible or who else checks it, not that it is "
            f"fine: {reason!r}"
        )
        # A module that is also a face would be exempt from its own coverage.
        assert module not in gate._DEFAULTS_FACE_MODULES, module


def test_the_two_exempted_writers_really_do_refuse_the_inert_shapes():
    """The exemptions for the two fixture tools claim a refusal; run it.

    ⛔ This is the cell that keeps the census honest. Both entries in
    `_DEFAULTS_WRITER_EXEMPT` that say "refuses … at its own input boundary" are
    load-bearing: if either tool stopped refusing, the census would keep passing
    while a producer that can emit the #1218 shape sat outside every face. Their
    own test files pin the behaviour; this asserts the CLAIM here matches it, so
    the two cannot drift apart.
    """
    import importlib.util as _ilu

    ops_dir = gate.PROJECT_ROOT / "scripts" / "tools" / "ops"
    if str(ops_dir) not in sys.path:
        sys.path.insert(0, str(ops_dir))

    for rel, bad_key in (
        ("scripts/tools/dx/generate_tenant_fixture.py", "mysql_connection_usage_percent_critical"),
        ("scripts/ops/inject_default_key.py", "pg_replication_lag_critical"),
    ):
        assert "refuses" in gate._DEFAULTS_WRITER_EXEMPT[rel]
        spec = _ilu.spec_from_file_location(
            f"_census_probe_{Path(rel).stem}", gate.PROJECT_ROOT / rel)
        assert spec is not None and spec.loader is not None
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert not mod.is_flat_key(bad_key), (
            f"{rel} binds a predicate that ACCEPTS {bad_key!r} — the refusal "
            f"the census exemption names would then let the shape through"
        )


def test_an_unparseable_module_is_reported_not_skipped(tmp_path, monkeypatch):
    """The census's fail-closed arm, measured rather than asserted in a comment.

    ⛔ A `SyntaxError` under `scripts/` must not make a module VANISH from the
    population: "we could not read it" and "it writes no `defaults:`" are the two
    states this whole floor exists to keep apart, and an `except SyntaxError:
    continue` would have collapsed them silently. The coverage delta on the
    pull request that added this floor is what surfaced the arm as untested —
    the branch existed, the claim about it was in a docstring, and nothing ran it.
    """
    root = tmp_path / "scripts"
    (root / "tools" / "dx").mkdir(parents=True)
    broken = root / "tools" / "dx" / "half_written.py"
    broken.write_text("def build(:\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(gate, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_DEFAULTS_CENSUS_ROOT", root)

    census = gate._defaults_writer_census()
    rel = "scripts/tools/dx/half_written.py"
    assert census.get(rel) == {"PARSE-FAIL"}, census

    errors: list[str] = []
    gate._report_defaults_writer_census(errors)
    named = [e for e in errors if "half_written.py" in e]
    assert len(named) == 1 and "UNCOVERED-DEFAULTS-WRITER" in named[0], errors
    assert "PARSE-FAIL" in named[0], (
        "the report must say WHY the module is in the population — a reader who "
        "cannot tell 'it writes defaults' from 'it does not parse' gets sent to "
        "the wrong repair"
    )

# ── The producer policy (#3924) ─────────────────────────────────────────────

_SPELLINGS = {"pg_connections", "mysql_threads_running", "mysql_cpu"}  # mysql_cpu: an alias


def _face(*keys: str, listed: tuple[str, ...] = ()) -> tuple[dict, list[str]]:
    return gate._walk_defaults_keys({k: 1 for k in keys}), list(listed)


def _policy(faces: dict) -> list[str]:
    return gate.run_check(demand=set(), supply=set(), deferred=set(),
                          producer_faces=faces, spellings=_SPELLINGS)["errors"]


@pytest.mark.parametrize("key", [
    "pg_connections", "pg_connections_critical", 'pg_connections{db="a"}',
    'pg_connections{db="a"}_critical', "mysql_cpu", "mysql_cpu_critical",
])
def test_a_registry_key_written_by_a_face_is_an_error_in_every_spelling(key):
    errors = _policy({"probe": _face(key)})
    assert len(errors) == 1 and errors[0].startswith("REGISTRY-KEY-WRITTEN: probe writes"), errors


def test_a_registry_key_listed_by_a_face_is_an_error():
    errors = _policy({"probe": _face(listed=("mysql_threads_running", "my_custom_key"))})
    assert errors == [e for e in errors if e.startswith("REGISTRY-KEY-LISTED")] and len(errors) == 1, errors
    assert "'mysql_threads_running'" in errors[0]


def test_a_key_outside_the_registry_is_the_deployers_own():
    assert _policy({"probe": _face("my_custom_key", "my_custom_key_critical",
                                   listed=("my_custom_key",))}) == []


def test_a_nested_registry_key_is_still_found():
    keys = gate._walk_defaults_keys({"group": {"pg_connections": 1}})
    errors = _policy({"probe": (keys, [])})
    assert len(errors) == 1 and "'group.pg_connections'" in errors[0], errors


def test_every_face_and_every_key_is_reported_not_just_the_first():
    errors = _policy({"a": _face("pg_connections", "mysql_cpu"), "b": _face("pg_connections")})
    assert len(errors) == 3, errors


def test_injected_demand_reads_no_real_producer(monkeypatch):
    def boom():
        raise AssertionError("the real producer faces were read")
    monkeypatch.setattr(gate, "_producer_faces", boom)
    assert gate.run_check(demand={"x"}, supply={"x"}, deferred=set(),
                          spellings=set())["errors"] == []


def test_the_real_repo_is_clean_and_reads_every_face():
    result = gate.run_check()
    assert result["errors"] == [], result["errors"]
    faces = gate._producer_faces()
    labels = set(faces)
    assert {"generator scaffold_tenant.generate_defaults",
            "generator init_project._gen_defaults_yaml",
            "helm values helm/threshold-exporter/values.yaml"} <= labels, labels
    assert any(l.startswith("helm values environments/") for l in labels), labels
    samples = {l for l in labels if l.startswith("sample ")}
    assert "sample try-local/seed/conf.d/_defaults.yaml" in samples, samples
    assert "sample components/threshold-exporter/config/conf.d/_defaults.yaml" in samples, samples
    assert not any(l.startswith("sample tests/") for l in samples), samples


def test_the_real_spellings_cover_the_registry_and_its_aliases():
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
    import _registry_lib as lib
    doc = lib.load_registry()
    spellings = gate._registry_spellings(doc)
    assert set(lib.registry_keys(doc)) <= spellings
    assert set(doc["deprecated_aliases"]) <= spellings


def test_samples_are_the_tracked_artifacts_minus_the_fixtures():
    """Real tracked paths on both sides of the split, so neither half is
    vacuous: a `_shipped_samples` that returned [] or everything goes red."""
    tracked = gate._tracked_defaults_artifacts()
    fixtures = [p for p in tracked if p.startswith(tuple(gate._FIXTURE_PREFIXES))]
    shipped = [p for p in tracked if p not in fixtures]
    assert fixtures and shipped, (fixtures, shipped)
    assert gate._shipped_samples(tracked) == shipped
    assert "components/threshold-exporter/config/conf.d/_defaults.yaml" in shipped
    assert "try-local/seed/conf.d/_defaults.yaml" in shipped
    # a non-defaults file handed in is not a sample
    assert gate._shipped_samples([*tracked, "examples/redis-prod.yaml"]) == shipped


def test_a_fixture_prefix_that_matches_nothing_is_stale(monkeypatch):
    monkeypatch.setattr(gate, "_FIXTURE_PREFIXES", {**gate._FIXTURE_PREFIXES, "nowhere/": "x"})
    errors: list[str] = []
    gate._report_fixture_prefixes(errors)
    assert errors == [e for e in errors if "'nowhere/'" in e] and len(errors) == 1, errors
    assert errors[0].startswith("STALE-FIXTURE-PREFIX")


def test_every_fixture_prefix_matches_an_artifact_today():
    errors: list[str] = []
    gate._report_fixture_prefixes(errors)
    assert errors == []


def test_main_reports_a_policy_violation_and_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(gate, "run_check", lambda: _stub_result(
        errors=["REGISTRY-KEY-WRITTEN: probe writes 'pg_connections' …"]))
    assert gate.main(["--ci"]) == 1
    assert "REGISTRY-KEY-WRITTEN" in capsys.readouterr().err


def test_the_census_sees_no_generator_face_writing_defaults_today():
    census = gate._defaults_writer_census()
    assert not (set(census) & gate._DEFAULTS_FACE_MODULES), census
    assert set(gate._DEFAULTS_WRITER_EXEMPT) <= set(census)


# ── the hook fires on every input the gate reads ──────────────────────────

_IMPORT_CLOSURE_DRIVER = r'''
import ast, contextlib, importlib.util, io, json, runpy, sys
from pathlib import Path

script, repo, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
_FOREIGN = {"site-packages", "dist-packages", ".venv", "venv", "node_modules"}


def in_repo(f):
    try:
        p = Path(f).resolve()
        rel = p.relative_to(repo)
    except (TypeError, ValueError, OSError):
        return None
    return None if _FOREIGN & set(rel.parts) else p


# 1. Run it the way the hook runs it: `python3 <script> --ci`, so sys.path[0]
#    is the script's directory and every insert the gate makes happens, and the
#    lazy, in-function imports on the `--ci` path actually execute.
sys.argv = [str(script), "--ci"]
sys.path[0] = str(script.parent)
rc = "no SystemExit"
with contextlib.redirect_stdout(io.StringIO()), \
        contextlib.redirect_stderr(io.StringIO()):
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        rc = exc.code
executed = {p for m in list(sys.modules.values())
            if (p := in_repo(getattr(m, "__file__", None)))}

# 2. …then close over imports that did NOT execute on this run (a branch not
#    taken today), resolved against the sys.path the gate left behind. Every
#    import statement in every repo module reached so far, at any nesting
#    depth, is resolved with find_spec — the interpreter's answer to "where
#    would this come from", not a list of places to look.
closure, frontier = set(executed), list(executed)
while frontier:
    tree = ast.parse(frontier.pop().read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        else:
            continue
        for name in names:
            try:
                spec = importlib.util.find_spec(name.split(".")[0])
            except (ImportError, ValueError):
                continue
            p = in_repo(spec and spec.origin)
            if p and p.suffix == ".py" and p not in closure:
                closure.add(p)
                frontier.append(p)

out.write_text(json.dumps({
    "rc": rc,
    "executed": sorted(p.relative_to(repo).as_posix() for p in executed),
    "closure": sorted(p.relative_to(repo).as_posix() for p in closure),
}))
'''


def _gate_repo_import_closure(tmp_path) -> set[str]:
    """Every in-repo module the gate can reach by `import`, transitively.

    Measured in a SUBPROCESS, not in this process: this module has already
    imported the gate by spec and pytest has its own sys.path, so this
    process's `sys.modules` answers a different question. The subprocess runs
    the gate exactly as the hook's `entry:` does, collects every loaded module
    whose `__file__` is inside the repo (site-packages / venvs excluded), then
    closes over import statements that did not execute on that run.
    """
    import json

    out = tmp_path / "closure.json"
    subprocess.run(
        [sys.executable, "-X", "utf8", "-c", _IMPORT_CLOSURE_DRIVER,
         str(_SCRIPT), str(REPO_ROOT), str(out)],
        cwd=REPO_ROOT, check=True, timeout=120)
    got = json.loads(out.read_text(encoding="utf-8"))
    # the gate must have actually RUN, or the lazy imports never fired and the
    # executed set is only the module-level half
    assert got["rc"] in (0, 1), got["rc"]
    executed, closure = set(got["executed"]), set(got["closure"])
    assert executed <= closure, sorted(executed - closure)
    # the lazy, in-function import #1413's live instance is about: proof the
    # run went down the `--ci` path rather than dying at argparse
    assert "scripts/tools/lint/check_confd_schema.py" in executed, sorted(executed)
    return closure


def test_precommit_filter_covers_every_input_this_gate_reads(tmp_path):
    """⛔ A gate that reads an input its trigger filter does not cover does not
    run on the change that matters (#1218: `da-tools init` shipped 16 misplaced
    keys while the gate that reads it never fired on the commit that changed
    them).

    Derived, not restated: the gate's transitive in-repo import closure
    (measured by the interpreter, #1413), its path constants, the demand-side
    pack roster, the tracked defaults artifacts, and the helm values faces —
    the chart and every `environments/*` overlay (#3924 made the overlays a
    face; the filter did not name them until this test caught it).
    A literal floor plus named members keeps a collapsed derivation from
    satisfying this vacuously.
    """
    import glob
    import pathlib

    inputs: set[str] = {"scripts/tools/lint/check_threshold_reachability.py"}
    for p in gate.observed_map_lib.default_pack_paths():
        inputs.add(pathlib.Path(p).resolve().relative_to(REPO_ROOT).as_posix())
    inputs.update(_gate_repo_import_closure(tmp_path))
    for value in vars(gate).values():
        if isinstance(value, pathlib.Path) and value.is_file():
            try:
                inputs.add(value.relative_to(REPO_ROOT).as_posix())
            except ValueError:
                pass
    artifacts = gate._tracked_defaults_artifacts()
    assert artifacts, "no tracked defaults artifact — vacuous"
    inputs.update(artifacts)
    overlays = sorted(
        pathlib.Path(p).relative_to(REPO_ROOT).as_posix()
        for p in glob.glob(str(REPO_ROOT / gate._ENVIRONMENT_VALUES_GLOB)))
    assert overlays, "no environments/* values overlay — vacuous"
    inputs.update(overlays)
    inputs.add(gate._CHART_VALUES_REL)

    assert len(inputs) >= 20, sorted(inputs)
    for expected in ("scripts/tools/ops/init_project.py",
                     "scripts/tools/ops/scaffold_tenant.py",
                     "scripts/tools/ops/_registry_lib.py",
                     "scripts/tools/lint/check_confd_schema.py",
                     "helm/threshold-exporter/values.yaml",
                     "environments/ci/threshold-exporter.yaml"):
        assert expected in inputs, (expected, sorted(inputs))
    assert any(p.startswith("rule-packs/") for p in inputs), sorted(inputs)

    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [h for repo in config["repos"] for h in repo["hooks"]
             if h["id"] == "threshold-reachability-check"]
    assert len(hooks) == 1, "hook id moved or was duplicated"
    pattern = re.compile(hooks[0]["files"])
    uncovered = sorted(p for p in inputs if not pattern.match(p))
    assert uncovered == [], (
        "threshold-reachability-check reads these but its `files:` filter does "
        "not fire on them, so a commit touching only one of them skips the "
        f"gate: {uncovered}")


@pytest.mark.parametrize("listed,names", [
    (["container_cpu"], ["container_cpu"]),
    ({"container_cpu": 5}, ["container_cpu"]),   # mistyped map: its keys
    ("container_cpu", ["container_cpu"]),        # mistyped scalar: itself
])
def test_a_mistyped_list_is_read_for_its_names_not_dropped(listed, names):
    """A map or scalar under `optional_overrides:` still names keys; reading it
    as [] would let a registry key through the producer policy (#3924)."""
    assert gate._platform_section({"optional_overrides": listed})[1] == names
    errors: list[str] = []
    gate._report_producer_policy(
        {"x": gate._platform_section({"optional_overrides": listed})},
        gate._registry_spellings(), errors)
    assert [e.split(":")[0] for e in errors] == ["REGISTRY-KEY-LISTED"], errors


@pytest.mark.parametrize("body,shape", [
    ("doc = {'optional_overrides': ['redis_evicted_keys_rate']}\n", "dict-literal"),
    ("doc = {}\ndoc['optional_overrides'] = []\n", "subscript-write"),
    ("T = '''\\noptional_overrides:\\n  - x\\n'''\n", "yaml-template"),
])
def test_the_census_sees_an_optional_overrides_writer(tmp_path, monkeypatch, body, shape):
    """A generator that emits only the list is a census hit too (#3924 review):
    the policy forbids a registry key under either root key."""
    root = tmp_path / "scripts"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "lists_only.py").write_text(body, encoding="utf-8")
    monkeypatch.setattr(gate, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_DEFAULTS_CENSUS_ROOT", root)
    assert gate._defaults_writer_census() == {"scripts/tools/lists_only.py": {shape}}
