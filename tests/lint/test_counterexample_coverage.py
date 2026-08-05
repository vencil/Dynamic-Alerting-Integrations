"""Every face that hands a threshold number over must hand over its
counter-example too (#1176).

⛔ WHY THIS EXISTS AS A GATE RATHER THAN A CHECKLIST
Adversarial review rounds on the PR that introduced `value_counterexample` each
found a face the author had missed — two whole surfaces, then a sixth key, then
an entire TIER (`optional_overrides`), and then, after this file already
existed, THREE MORE: the `<tenant>.yaml` stub (the only file a tenant ever
opens), the one-click-copy config templates, and the chart's own declared-key
list. The author's coverage claim was wrong every single time, and every time
it was prose that asserted it.

⭐ SO THE FACE LIST IS DERIVED WHERE IT CAN BE.
`surface_specs()` is the registry's own inventory of generated blocks — every
committed surface it rewrites. Reading the face list FROM it means a new
generated surface enters this gate the day it is added, without anyone
remembering to come here; it also gives per-surface locality for free (each
pack header is its own spec, so one pack's rendering can no longer vouch for
another's). What remains hand-listed is only the faces the registry does not
generate: two portal assets and the two files `scaffold_tenant` writes.

⚠️ Deliberately NOT claimed: that a brand-new hand-written face cannot escape.
`EXTRA_FACES` is a list, and a list can go stale — that is exactly how the
three misses above happened. The honest scope is: generated surfaces are
covered by derivation, non-generated ones by this list plus the reverse check
below, and the portal-only faces by their own Vitest (see `PORTAL_FACES`).

⚠️ Deliberate non-goal: this does NOT check the wording. Wording composes per
medium (a YAML comment, a CLI prompt, a React element) around ONE shared set of
facts — `_registry_lib.COUNTEREXAMPLE_DIRECTION` / `COUNTEREXAMPLE_MARKS`.
Duplicating the sentence here would make this file the next copy of the thing
it exists to prevent.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))

import _registry_lib as registry_lib  # noqa: E402
import scaffold_tenant as scaffold  # noqa: E402

# The marker each rendered face must carry — imported, never restated. Three
# gates used to hand-copy a fragment of the renderer's sentence, which is the
# duplication this line of work exists to delete.
MARK = registry_lib.COUNTEREXAMPLE_MARK

# Single-literal joins on purpose: `tests/ops/test_portal_path_filter_coverage.py`
# scans for exactly this shape to prove every repo file a pytest reads is in
# ci.yml's `python` path filter. A join split across constants degrades to a
# directory and drops out of that scan.
PLATFORM_DATA = REPO_ROOT / "docs/assets/platform-data.json"
PORTAL_FALLBACK = REPO_ROOT / "tools/portal/src/interactive/tools/_common/data/rule-packs.js"
TEMPLATE_DATA = REPO_ROOT / "docs/assets/template-data.json"
TEMPLATE_GALLERY = REPO_ROOT / "tools/portal/src/interactive/tools/template-gallery.jsx"
MULTI_TENANT = REPO_ROOT / "tools/portal/src/interactive/tools/multi-tenant-comparison.jsx"
YAML_VALIDATOR = REPO_ROOT / "tools/portal/src/interactive/tools/YamlValidatorTab.jsx"
STARTER_YAML = REPO_ROOT / "tools/portal/src/interactive/tools/_common/sim/alert-engine.js"


@pytest.fixture(scope="module")
def doc() -> dict:
    return registry_lib.build_registry_doc()


@pytest.fixture(scope="module")
def ce_keys(doc) -> dict:
    return {k: e["value_counterexample"]
            for k, e in doc["keys"].items() if "value_counterexample" in e}


@pytest.fixture(scope="module")
def written(tmp_path_factory, doc) -> dict:
    """What `scaffold_tenant` ACTUALLY writes, via the real entry point.

    ⛔ `write_outputs`, not a re-assembly of the steps it takes. An earlier
    version of this file called `generate_defaults` → `yaml.safe_dump` →
    `annotate_defaults_counterexamples` itself, so deleting the annotate call
    from the shipping path left every gate green (blind review, #1344). The
    batch paths (`--non-interactive` / `--from-onboard`) reach this function
    without asking a human anything, so this IS the tenant-facing render.
    """
    packs = [p for p in sorted({e["pack"] for e in doc["keys"].values()})
             if p in scaffold.RULE_PACKS]
    defaults_data = scaffold.generate_defaults(packs)
    tenant_name = "acme"
    tenant_data = {"tenants": {tenant_name: {}}}
    out = tmp_path_factory.mktemp("scaffold-out")
    scaffold.write_outputs(str(out), tenant_name, defaults_data, tenant_data, "")
    return {
        "_defaults.yaml": (out / "_defaults.yaml").read_text(encoding="utf-8"),
        "tenant.yaml": (out / f"{tenant_name}.yaml").read_text(encoding="utf-8"),
        "defaults_data": defaults_data,
    }


def test_every_counterexample_ships_both_languages(ce_keys):
    """⛔ `observed_zh` is schema-optional and gate-REQUIRED.

    The ZH surfaces include `<tenant>.yaml`, the one file a tenant opens, and a
    Chinese frame wrapped around an English clause reads worse than either
    language alone (owner decision, #1344). The composers fall back to English
    rather than rendering nothing — silence has to keep meaning "not measured"
    — so without this assertion a missing translation would degrade silently
    into exactly the mixed-language output the field exists to remove.
    """
    missing = sorted(k for k, ce in ce_keys.items() if not ce.get("observed_zh"))
    assert not missing, (
        f"{missing} carry an English `observed` with no `observed_zh` — the ZH "
        "surfaces would silently fall back to the English clause")


def test_the_two_languages_are_not_the_same_string(ce_keys):
    """Non-vacuity for the pair above: copying the English into `observed_zh`
    would satisfy it while changing nothing a Chinese reader sees."""
    same = sorted(k for k, ce in ce_keys.items()
                  if ce.get("observed_zh") == ce["observed"])
    assert not same, f"{same} have an `observed_zh` identical to `observed`"


def test_there_is_something_to_cover(ce_keys):
    """Non-vacuity guard. If the registry ever carries zero counter-examples
    every assertion below passes trivially, which would read as "all faces
    covered" while covering nothing."""
    assert ce_keys, "no key carries value_counterexample — every face check below is vacuous"


# ── which keys a rendered face names ────────────────────────────────────────


def _presented_keys(doc, text: str) -> set[str]:
    """Registry keys the text actually names.

    ⛔ Token match, never `in`. `db2_lock_wait_time` is a prefix of a
    hypothetical `db2_lock_wait_time_critical`, and a substring test would
    report the shorter key as "presented" by a face that only mentions the
    longer one — the comparison being looser than the target is how the cases
    that look most like the target get waved through.
    """
    return {k for k in doc["keys"]
            if re.search(rf"(?<![0-9A-Za-z_]){re.escape(k)}(?![0-9A-Za-z_])", text)}


def _flatten(text: str) -> str:
    """Compare CONTENT, not line layout.

    Several faces wrap the sentence into fixed-width comment lines, so the
    observed clause is physically split across lines with `#` and indentation
    between the halves. Stripping comment furniture and collapsing whitespace
    keeps this a real content check while surviving a re-wrap.
    """
    out = []
    for line in text.split("\n"):
        s = line.strip()
        while s.startswith("#"):
            s = s[1:].lstrip()
        if s.startswith("- "):
            s = s[2:]
        out.append(s)
    return " ".join(" ".join(out).split())


# ── the faces ───────────────────────────────────────────────────────────────
#
# Each entry is (presented_keys, rendered_text). A key in the first set whose
# counter-example is missing from the second is the failure this file catches.


def _generated_surface_faces(doc) -> dict:
    """DERIVED: every block the registry regenerates, one face per spec.

    Per spec, not per file and not merged: `surface_specs` already emits one
    entry per pack header, so a face here cannot borrow another pack's
    rendering to satisfy its own keys.
    """
    faces = {}
    for spec in registry_lib.surface_specs(doc):
        body = "\n".join(spec["body"])
        rel = os.path.relpath(spec["path"], REPO_ROOT).replace(os.sep, "/")
        faces[f"generated:{spec['id']} ({rel})"] = (_presented_keys(doc, body), body)
    return faces


def _extra_faces(doc, written) -> dict:
    """Hand-listed: the faces nothing generates."""
    pd_text = PLATFORM_DATA.read_text(encoding="utf-8")
    pd = json.loads(pd_text)
    pd_keys = set()
    for pack in (pd.get("rulePacks") or {}).values():
        pd_keys |= set(pack.get("defaults") or {})
    for rows in (pd.get("declaredKeys") or {}).values():
        pd_keys |= {r["key"] for r in rows}

    fallback = PORTAL_FALLBACK.read_text(encoding="utf-8")

    defaults_data = written["defaults_data"]
    scaffold_presented = (set(defaults_data.get("defaults") or {})
                          | set(defaults_data.get("optional_overrides") or []))

    tenant_text = written["tenant.yaml"]
    return {
        # JSON carries the datum, not the sentence: presence of the field IS
        # the rendering for a machine-readable face.
        "docs/assets/platform-data.json": (pd_keys, pd_text),
        "portal offline fallback rule-packs.js":
            (_presented_keys(doc, fallback), fallback),
        "scaffold_tenant → _defaults.yaml (batch path)":
            (scaffold_presented, written["_defaults.yaml"]),
        # ⛔ The only file a tenant ever opens. It shipped a hand-written
        # paragraph naming two Oracle counter-examples unconditionally, so a
        # db2-only tenant was warned about keys its file did not contain while
        # the one key in it that HAD a measurement said nothing — and the
        # blanket "both over-fire" was the reversal `direction` exists to stop.
        "scaffold_tenant → <tenant>.yaml declared stub":
            (_presented_keys(doc, tenant_text), tenant_text),
        # The SAME stub in English (`init_project` writes this one). A separate
        # face because it is a separate render: different wording table, and a
        # gate that only ever exercised the language its own environment
        # happens to select would have covered exactly half of it. The write
        # path is already proven by the zh face above; what this adds is the
        # other language's renderer.
        "<tenant>.yaml declared stub (en)": _stub_face(doc, "en"),
        # ⛔ The SECOND producer of a file called `_defaults.yaml`, writing into
        # the same `conf.d/` as the stub above. It derives its declared list
        # from the same shared predicate, so it lists exactly the keys with
        # measurements — and it wrote them bare, next to a sibling file that
        # did not, because the annotator lived in the other producer's module.
        "init_project → _defaults.yaml": _init_project_defaults_face(doc),
    }


def _init_project_defaults_face(doc):
    init = _load_init_project()
    packs = sorted({e["pack"] for e in doc["keys"].values()})
    text = init._gen_defaults_yaml(packs, "monitoring")
    return _presented_keys(doc, text), text


def _load_init_project():
    import importlib
    return importlib.import_module("init_project")


def _stub_face(doc, lang: str):
    keys = registry_lib.shipped_optional_keys_for_packs(
        sorted({e["pack"] for e in doc["keys"].values()}))
    text = "\n".join(registry_lib.render_tenant_declared_stub_lines(
        keys, lang=lang))
    return _presented_keys(doc, text), text


@pytest.fixture(scope="module")
def faces(doc, written) -> dict:
    return {**_generated_surface_faces(doc), **_extra_faces(doc, written)}


def test_generated_surface_faces_are_all_derived(doc, faces):
    """Every generated block IN THE TREE must be a spec — and thus a face.

    ⛔ The first version compared `surface_specs()` against itself (both sides
    of the assertion came from one call), so it was true by construction and
    could not detect the thing its own docstring claimed: a generated surface
    that never enters the face list. The real oracle is the marker committed in
    the file — `check_threshold_registry` already verifies spec → file, so this
    verifies the other direction, file → spec.
    """
    marker_re = re.compile(
        r"#\s*>>>\s*" + re.escape(registry_lib._MARKER_STEM) + r"([\w-]+)")
    in_tree = set()
    skip = {".git", "node_modules", "site", "dist", ".venv", "__pycache__"}
    # ⛔ Not just `*.yaml`. A marker in a `.yml`, a Helm `.tpl`, a fenced block
    # in a `.md` or a `.json` was invisible, so "every generated block IN THE
    # TREE" meant "every one in a file with a single spelling of one suffix".
    for suffix in ("*.yaml", "*.yml", "*.tpl", "*.md", "*.json"):
        for path in REPO_ROOT.rglob(suffix):
            if skip & set(path.parts):
                continue
            try:
                in_tree |= set(marker_re.findall(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
    assert in_tree, "found no generated-block markers at all — scan is broken"

    spec_ids = {s["id"] for s in registry_lib.surface_specs(doc)}
    assert in_tree - spec_ids == set(), (
        f"{sorted(in_tree - spec_ids)} are generated blocks in the tree that "
        "`surface_specs()` does not emit — they regenerate from nothing and "
        "no face covers them")
    # ⛔ There is deliberately NO `spec_ids <= faced` assertion here. `faces`
    # is built by iterating `surface_specs()`, so that comparison would be true
    # by construction — the same tautology this test was rewritten to remove,
    # reintroduced one line lower (blind review, #1344). The spec→face step is
    # covered by `test_every_face_names_some_registry_key`, which fails if a
    # face is missing or empty.


def test_every_generated_surface_file_triggers_its_precommit_hook(doc):
    """A new generated surface must also enter the hook's `files:` regex.

    ⛔ The class, not the instance. Adding `try-local-optional` to
    `surface_specs()` made a third file regenerate-able, but
    `threshold-registry-check` is `pass_filenames: false` — it only runs when a
    STAGED file matches its `files:` pattern, and that pattern still listed two
    paths. A commit touching only the new surface skipped the freshness gate
    locally (blind review, #1344). Every future surface hits the same wall, so
    the assertion belongs here rather than one more path in the regex.
    """
    cfg = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook = next(
        h for repo in cfg["repos"] for h in repo.get("hooks", [])
        if h.get("id") == "threshold-registry-check")
    pattern = re.compile(hook["files"])
    missing = []
    for spec in registry_lib.surface_specs(doc):
        rel = os.path.relpath(spec["path"], REPO_ROOT).replace(os.sep, "/")
        if not pattern.search(rel):
            missing.append(rel)
    assert not missing, (
        f"{sorted(set(missing))} are regenerated surfaces that do NOT match "
        "`threshold-registry-check`'s `files:` — staging only one of them "
        "would skip the freshness hook at commit time")


def test_every_face_names_some_registry_key(faces, doc):
    """Extractor sanity. A face whose `presented` set is empty makes its own
    content check vacuous, and the most likely cause is the extraction below
    silently matching nothing — not a surface that genuinely names no key."""
    empty = sorted(n for n, (presented, _t) in faces.items() if not presented)
    assert not empty, f"these faces name no registry key at all: {empty}"


def test_face_renders_every_counterexample_it_presents(faces, ce_keys):
    """Per key: the face must carry that key's OWN observed clause.

    Keying off the shared marker alone would let one rendered key vouch for all
    the others — the precise failure mode that let a whole tier slip through.

    ⛔ Flatten BOTH sides. The rendered text is normalised (comment furniture
    stripped, whitespace collapsed) but the needle was not, so an `observed`
    containing a newline or a double space would never match its own rendering
    — a false failure, and after someone "fixed" it by loosening the check, a
    false pass. Same function on both, or the comparison is between two
    different alphabets (CodeRabbit, #1344).
    """
    problems = []
    for name, (presented, raw) in sorted(faces.items()):
        owed = presented & set(ce_keys)
        if not owed:
            continue
        # ⛔ The clause in the face's OWN language(s). Checking only the English
        # would pass on a ZH surface that never renders the translation — i.e.
        # on the fallback path `observed_zh` exists to retire.
        missing = [
            k for k in sorted(owed)
            for lang in _face_langs(name)
            if not _renders_near(
                raw, k, registry_lib.counterexample_observed(ce_keys[k], lang))]
        if missing:
            problems.append(f"{name}: presents {sorted(owed)} but renders "
                            f"no counter-example for {missing}")
    assert not problems, (
        "a number handed over with what we know about it stripped off:\n  "
        + "\n  ".join(problems))


# Measured, not guessed: the largest real key→clause distance across every
# face today is 381 characters (`db2_lock_wait_time` in platform-data.json,
# whose `desc` is a long English paragraph). 800 is ~2x headroom.
#
# ⛔ CHARACTERS on the flattened text, not lines. A line window was both too
# tight and too loose at once: `platform-data.json` puts `observed` exactly 8
# lines under its key, so adding one field before it (`critical_of` sits
# immediately before `value_counterexample` in `_FIELD_ORDER`) would have
# false-redded a correct change — while `rule-packs.js` writes a whole pack on
# ONE line, making the window the entire pack and the adjacency claim empty
# there (blind review, #1344).
_ADJACENCY_CHARS = 800

# Which language(s) a face must render the measured clause in. The two DATA
# faces carry the registry object verbatim and are consumed by both locales, so
# both clauses must be there; a rendered surface owes only its own language.
_EN_FACES = ("(en)", "init_project")
_BILINGUAL_FACES = ("platform-data.json", "rule-packs.js")


def _face_langs(name: str) -> tuple[str, ...]:
    if any(marker in name for marker in _BILINGUAL_FACES):
        return ("zh", "en")
    return ("en",) if any(m in name for m in _EN_FACES) else ("zh",)


def _renders_near(raw: str, key: str, observed: str) -> bool:
    """Is `observed` rendered NEXT TO `key`, not merely somewhere on the face?

    Adjacency, because "somewhere in this face" passes when two keys on the
    same face swap caveats — and on a face like the pack headers, which carries
    every key in a pack, that is a whole family of wrong-but-green states.
    """
    flat = _flatten(raw)
    needle = _flatten(observed)
    for m in re.finditer(
            rf"(?<![0-9A-Za-z_]){re.escape(key)}(?![0-9A-Za-z_])", flat):
        lo = max(0, m.start() - _ADJACENCY_CHARS)
        if needle in flat[lo: m.end() + _ADJACENCY_CHARS]:
            return True
    return False


# The machine-readable face presents EVERY shipped registry key by
# construction, so it satisfies any reverse-coverage question on its own.
_PRESENTS_EVERYTHING = "docs/assets/platform-data.json"


def test_every_counterexample_key_is_owed_by_a_HUMAN_face(faces, ce_keys):
    """The reverse direction: a key nobody presents would make the checks above
    pass by presenting nothing.

    ⛔ Excluding `platform-data.json`. Its `presented` set is "every key the
    generator emits", so including it made this assertion true by construction
    — it could not detect the stale face list its own docstring claimed to
    catch (blind review, #1344). What must be non-empty is the set of faces a
    HUMAN reads.
    """
    covered = set()
    for name, (presented, _text) in faces.items():
        if name == _PRESENTS_EVERYTHING:
            continue
        covered |= presented & set(ce_keys)
    orphan = sorted(set(ce_keys) - covered)
    assert not orphan, (
        f"{orphan} carry a counter-example but no human-facing listed face "
        "presents them — either the face list is stale or the key is dead")


def test_the_everything_exclusion_is_load_bearing(faces, ce_keys):
    """Pin WHY the exclusion above exists, so it cannot be deleted as noise.

    If `platform-data.json` did not present every counter-example key, dropping
    it from the reverse check would change nothing and the exclusion would be
    decoration. It does — which is exactly what made the un-excluded version
    true by construction. Mutation note: removing the exclusion cannot turn a
    test red while the face list is complete (it only makes the check looser),
    so the tautology is asserted here directly rather than demonstrated by
    mutation (#1344).
    """
    pd_presented, _text = faces[_PRESENTS_EVERYTHING]
    assert pd_presented >= set(ce_keys), (
        f"{_PRESENTS_EVERYTHING} no longer presents every counter-example key — "
        "re-check whether it still needs excluding from the reverse check")


def test_the_everything_face_is_still_the_only_one_excluded(faces, doc):
    """Guard the exclusion above: if a second face ever presents the whole
    registry, the reverse check silently goes back to being tautological."""
    everything = {name for name, (presented, _t) in faces.items()
                  if presented >= set(doc["keys"])}
    assert everything <= {_PRESENTS_EVERYTHING}, (
        f"{sorted(everything - {_PRESENTS_EVERYTHING})} also present every "
        "registry key — exclude them too or the reverse check means nothing")


def test_both_tiers_are_represented(faces, ce_keys, doc):
    """Aggregate non-vacuity with teeth: the two tiers fail differently, and a
    face list that only ever exercised `defaults` is how the whole
    `optional_overrides` tier stayed uncovered through two review rounds."""
    tiers = {doc["keys"][k].get("tier") for k in ce_keys}
    assert {"defaults", "optional_overrides"} <= tiers, (
        f"only tiers {tiers} carry a counter-example — this gate has never "
        "exercised the declared tier")
    for tier in ("defaults", "optional_overrides"):
        want = {k for k in ce_keys if doc["keys"][k].get("tier") == tier}
        hit = {n for n, (presented, _t) in faces.items() if presented & want}
        assert hit, f"no face presents any {tier}-tier counter-example key"


# ── portal-only faces: rendered in JS, asserted in Vitest ───────────────────
#
# These hand a number over in the browser, so their content check cannot live
# in pytest. What CAN live here is the wiring: that the component still routes
# through the shared renderer. The Vitest named alongside proves the renderer
# actually emits the clause; this proves the caller still calls it. Neither
# half is sufficient alone.

_VITEST = "tools/portal/tests/counterexample-portal-faces.test.ts"

PORTAL_FACES = {
    "config template gallery (one-click copy YAML)":
        (TEMPLATE_GALLERY, "annotateCounterexamples", _VITEST),
    "multi-tenant comparison (labels a number 'default')":
        (MULTI_TENANT, "counterexampleVerdict", _VITEST),
    # The third live-value face. A comment in `alert-engine.js` claimed the
    # starter template's defaults loop was "the one face that hands a number
    # over as a LIVE, copy-paste value"; this button inserts one into the
    # user's editable YAML on a single click (blind review, #1344).
    "YAML validator insert-metric button":
        (YAML_VALIDATOR, "counterexampleComment", _VITEST),
    "starter YAML (both tiers)":
        (STARTER_YAML, "counterexampleComment", _VITEST),
}


@pytest.mark.parametrize("face_name", sorted(PORTAL_FACES))
def test_portal_face_still_routes_through_the_shared_renderer(face_name):
    """⛔ IMPORTED from the shared module AND called — both, separately.

    The first version of this assertion was `"import {" in src and symbol in
    src`, which is true of any file that imports anything and mentions the name
    anywhere. Mutation-tested: deleting the import outright left it GREEN,
    because the call site still spelled the symbol. A wiring check that cannot
    tell "wired" from "spelled" is not a wiring check.
    """
    path, symbol, vitest = PORTAL_FACES[face_name]
    src = path.read_text(encoding="utf-8")
    imported = re.search(
        r"import\s*\{[^}]*\b" + re.escape(symbol) + r"\b[^}]*\}\s*from\s*"
        r"['\"][^'\"]*data/rule-packs\.js['\"]", src, re.S)
    assert imported, (
        f"{face_name}: {path.name} no longer imports `{symbol}` from the shared "
        f"rule-packs module — either it stopped rendering the caveat or it grew "
        f"its own copy of the wording, which is how the two portal copies had "
        f"already drifted apart. Content is asserted in {vitest}.")
    assert re.search(re.escape(symbol) + r"\s*\(", src), (
        f"{face_name}: {path.name} imports `{symbol}` but never calls it")
    assert (REPO_ROOT / vitest).is_file(), (
        f"{face_name} names {vitest} as its content gate, but that file is gone")


def test_template_assets_have_something_for_the_portal_to_annotate(doc, ce_keys):
    """Non-vacuity for the template face: the shipped templates really do hand
    over keys with a measured counter-example, so the Vitest above is not
    asserting over an empty set."""
    templates = json.loads(TEMPLATE_DATA.read_text(encoding="utf-8")).get("templates") or []
    keys = set()
    for tpl in templates:
        try:
            parsed = yaml.safe_load(tpl.get("yaml") or "") or {}
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict):
            keys |= set(parsed)
    owed = keys & set(ce_keys)
    assert owed, ("no shipped template hands over a counter-example key — if "
                  "that is really true, the template face can be retired")
