"""test_optional_overrides_render.py — #1310 chart-face render tripwire

`thresholdConfig.optional_overrides` is the runtime half of the registry's
`tier: optional_overrides`: a LIST of key names the exporter RECOGNISES but
asserts no value for (`ThresholdConfig.OptionalOverrides []string`). Ship the
list and a tenant can set those keys in its own conf.d and have them fire
(`resolveDeclaredRows`, PR-C #1314); omit it and the same keys are unwritable.

Two failure modes the chart can silently introduce, neither visible to the
registry gate (which only compares the generated block inside values.yaml
against the registry — it never renders the chart):

  1. the ConfigMap template forgets the key entirely, so an operator's values
     land nowhere and every declared key stays dead in a real deploy;
  2. the key is rendered NESTED UNDER `defaults:` instead of as its sibling.
     That is worse than forgetting it: the Go loader would decode a list under
     `defaults` (map[string]…) as a broken/zero entry rather than a
     declaration, and nothing upstream would complain.

Two layers, the shape #1286 established for chart-face assertions
(tests/lint/test_check_scrape_reachability.py):
  * static (no helm needed) — values.yaml carries the list and the template
    references it at the same nesting level as `defaults` / `state_filters`,
    so a regression is caught even on helm-less runners;
  * render (helm-gated) — `helm template` and compare the rendered
    `_defaults.yaml` against the values it was rendered from, item by item.
    `helm` exists in CI (azure/setup-helm) and in the dev container; locally
    it skips. The fail-closed guard for a broken setup-helm step is central
    (tests/shared/test_vector_projection_vrl.py::test_helm_present_when_required).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = "helm/threshold-exporter"
_VALUES = "helm/threshold-exporter/values.yaml"
_CONFIGMAP_TPL = "helm/threshold-exporter/templates/configmap.yaml"
_CONFIGMAP_NAME = "threshold-config"
_DEFAULTS_FILE = "_defaults.yaml"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


def _values(repo_root: Path) -> dict:
    doc = yaml.safe_load((repo_root / _VALUES).read_text(encoding="utf-8")) or {}
    return doc.get("thresholdConfig") or {}


# ── static layer ────────────────────────────────────────────────────────────

def test_values_ship_a_non_empty_string_list(repo_root: Path):
    """Fail loud if the shape moves — otherwise the render comparison below
    would degenerate into `[] == []` and pass vacuously."""
    listed = _values(repo_root).get("optional_overrides")
    assert isinstance(listed, list) and listed, (
        f"{_VALUES} thresholdConfig.optional_overrides must be a non-empty "
        "list — it is generated from the registry, run "
        "check_threshold_registry.py --regen")
    assert all(isinstance(k, str) for k in listed), (
        "array of STRING (docs/schemas/platform-defaults.schema.json) — a "
        "mapping here would assert a platform value, the opposite of the tier")


def test_values_list_carries_no_critical_key(repo_root: Path):
    """#1311: `<base>_critical` resolves through resolveCriticalRows, which
    keys off defaults[base] and drops the row when the base is merely
    declared. Listing one buys a tenant nothing but a write that never fires."""
    listed = _values(repo_root).get("optional_overrides") or []
    assert not [k for k in listed if k.endswith("_critical")], listed


def test_template_renders_it_as_a_sibling_of_defaults(repo_root: Path):
    """Static nesting check: `optional_overrides:` must sit at the SAME
    indentation as `defaults:` / `state_filters:` inside the _defaults.yaml
    heredoc. Catches the nest-under-defaults regression without helm."""
    txt = (repo_root / _CONFIGMAP_TPL).read_text(encoding="utf-8")
    indents = {}
    for line in txt.split("\n"):
        stripped = line.lstrip()
        for key in ("defaults:", "optional_overrides:", "state_filters:"):
            if stripped == key:
                indents.setdefault(key, len(line) - len(stripped))
    assert set(indents) == {"defaults:", "optional_overrides:", "state_filters:"}, (
        f"{_CONFIGMAP_TPL} does not emit all three top-level keys: {indents}")
    assert len(set(indents.values())) == 1, (
        "optional_overrides must be a SIBLING of defaults/state_filters, not "
        f"nested under one of them: {indents}")


def test_template_body_actually_carries_the_values_list(repo_root: Path):
    """The list must be RENDERED FROM values, not hardcoded in the template.

    ⛔ A bare `".Values.thresholdConfig.optional_overrides" in txt` cannot show
    that: the `{{- if .Values.thresholdConfig.optional_overrides }}` guard
    satisfies it on its own, so a template whose body hardcoded today's nine
    keys would pass while the values file became decoration. On a helm-less
    runner the render tests below skip, so that mutation had zero red tests.

    So look at the BODY — the lines between the `optional_overrides:` key and
    the guard's `{{- end }}` — and require the values path to be piped through
    `toYaml` there.
    """
    lines = (repo_root / _CONFIGMAP_TPL).read_text(encoding="utf-8").split("\n")
    key_idx = next(i for i, ln in enumerate(lines)
                   if ln.strip() == "optional_overrides:")
    body = []
    for ln in lines[key_idx + 1:]:
        if "{{- end }}" in ln:
            break
        body.append(ln)
    assert body, "no template body between optional_overrides: and {{- end }}"
    piped = [ln for ln in body
             if ".Values.thresholdConfig.optional_overrides" in ln
             and "toYaml" in ln]
    assert piped, (
        "the optional_overrides body never pipes "
        ".Values.thresholdConfig.optional_overrides through toYaml — the list "
        f"is not carried from values: {body}")
    # ...and the guard is a SEPARATE occurrence, so neither can stand in for the
    # other if one is deleted.
    guard = [ln for ln in lines[:key_idx]
             if "{{- if" in ln
             and ".Values.thresholdConfig.optional_overrides" in ln]
    assert guard, "the `{{- if }}` guard on the values path is missing"


def test_template_carries_the_no_platform_default_note(repo_root: Path):
    """#1321: the rendered ConfigMap must explain WHY these keys carry no value.

    `toYaml` emits the PARSED list only, so every comment in values.yaml — the
    tier rationale above the block and the per-key unit hints inside it — is
    gone by the time anyone reads the shipped ConfigMap. The chart is the one
    artifact that reaches every install; a bare list of key names there, with
    the reason living only in docs the reader may never open, is the gap #1320
    D1 chose to close ("make the silence loud" rather than invent platform
    values for keys whose suggested values measured ~40% benign false alarms).

    Static layer, so a helm-less runner still catches deletion of the note.
    """
    lines = (repo_root / _CONFIGMAP_TPL).read_text(encoding="utf-8").split("\n")
    key_idx = next(i for i, ln in enumerate(lines)
                   if ln.strip() == "optional_overrides:")
    # The note must sit ABOVE the key and INSIDE the guard: an operator who
    # empties the list should get neither the key nor an orphaned paragraph
    # explaining a key that is no longer there.
    guard_idx = max(i for i, ln in enumerate(lines[:key_idx])
                    if "{{- if" in ln
                    and ".Values.thresholdConfig.optional_overrides" in ln)
    note = "\n".join(ln for ln in lines[guard_idx + 1:key_idx]
                     if ln.lstrip().startswith("#"))
    assert note, (
        "no literal `#` comment line between the optional_overrides guard and "
        "the key — the rendered ConfigMap would carry a bare list of key names")
    assert "--show-inheritance" in note, (
        "the note must point at the tool that answers 'which of these has this "
        "tenant not set' (da-tools diagnose --show-inheritance) — otherwise the "
        "reader is told a problem exists and not how to inspect it")
    assert "len .Values.thresholdConfig.optional_overrides" in note, (
        "the note's count must be rendered FROM the list it introduces; a "
        "hardcoded number is exactly the hand-copied claim this block exists "
        "to stop shipping")


# ── render layer (helm-gated) ───────────────────────────────────────────────

def _render_defaults_raw(repo_root: Path, extra: list[str] | None = None) -> str:
    """helm template the chart and return the rendered conf.d/_defaults.yaml
    as the RAW string — comments intact.

    Separate from `_render_defaults_file` on purpose: every other assertion here
    goes through `yaml.safe_load`, which discards comments, so the #1321 note
    is structurally invisible to them. A note-shaped regression would be green
    across this entire module without a raw-text reader.
    """
    cmd = ["helm", "template", "t", str(repo_root / _CHART), "-n", "monitoring"]
    cmd += extra or []
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         check=True, timeout=120)
    cms = [d for d in yaml.safe_load_all(out.stdout)
           if d and d.get("kind") == "ConfigMap"
           and (d.get("metadata") or {}).get("name") == _CONFIGMAP_NAME]
    assert len(cms) == 1, f"expected one {_CONFIGMAP_NAME} ConfigMap, got {len(cms)}"
    data = cms[0].get("data") or {}
    assert _DEFAULTS_FILE in data, sorted(data)
    return data[_DEFAULTS_FILE]


def _render_defaults_file(repo_root: Path, extra: list[str] | None = None) -> dict:
    """helm template the chart and parse the rendered conf.d/_defaults.yaml."""
    return yaml.safe_load(_render_defaults_raw(repo_root, extra)) or {}


@_needs_helm
def test_rendered_defaults_file_matches_values_item_for_item(repo_root: Path):
    """#1310 acceptance: the rendered `optional_overrides:` equals the values
    it was rendered from, in order — no dropped, reordered or invented key."""
    rendered = _render_defaults_file(repo_root)
    expected = _values(repo_root)["optional_overrides"]
    assert rendered.get("optional_overrides") == expected, (
        "rendered _defaults.yaml optional_overrides diverges from "
        f"{_VALUES}: {rendered.get('optional_overrides')!r} != {expected!r}")
    # ...as a TOP-LEVEL key, never a child of defaults (the silent-corruption
    # mode: a list under defaults is not a declaration to the Go loader).
    assert "optional_overrides" not in (rendered.get("defaults") or {})


@_needs_helm
def test_rendered_list_is_carriage_not_a_hardcoded_template(
        repo_root: Path, tmp_path: Path):
    """Override the values and the render must follow. Without this, a
    template that hardcoded today's 9 keys would pass the test above."""
    probe = ["zzz_probe_alpha", "zzz_probe_beta"]
    overlay = tmp_path / "probe.yaml"
    overlay.write_text(
        yaml.safe_dump({"thresholdConfig": {"optional_overrides": probe}}),
        encoding="utf-8")
    rendered = _render_defaults_file(repo_root, ["-f", str(overlay)])
    assert rendered.get("optional_overrides") == probe


@_needs_helm
def test_empty_values_render_no_key_at_all(repo_root: Path, tmp_path: Path):
    """An operator who empties the list gets NO `optional_overrides:` key —
    not `optional_overrides: null`, which the schema tolerates but which reads
    as an accident. Pins the `{{- if ... }}` guard."""
    overlay = tmp_path / "empty.yaml"
    overlay.write_text(
        yaml.safe_dump({"thresholdConfig": {"optional_overrides": []}}),
        encoding="utf-8")
    rendered = _render_defaults_file(repo_root, ["-f", str(overlay)])
    assert "optional_overrides" not in rendered
    # the rest of the file must still render (guard must not swallow siblings)
    assert rendered.get("defaults") and rendered.get("state_filters")


@_needs_helm
def test_rendered_note_ships_and_its_count_tracks_the_list(
        repo_root: Path, tmp_path: Path):
    """#1321 acceptance: the note reaches the ConfigMap, and its count follows
    the list it introduces.

    The override half is what makes the count claim real — a note that
    hardcoded today's nine would pass the default render unchanged.
    """
    raw = _render_defaults_raw(repo_root)
    expected = len(_values(repo_root)["optional_overrides"])
    m = re.search(r"以下\s*(\d+)\s*個 key", raw)
    assert m, f"the #1321 note is absent from the rendered {_DEFAULTS_FILE}:\n{raw}"
    assert int(m.group(1)) == expected, (
        f"note says {m.group(1)} keys, values ship {expected}")
    assert "--show-inheritance" in raw, (
        "the note reached the ConfigMap without its inspection pointer")

    probe = ["zzz_probe_alpha", "zzz_probe_beta"]
    overlay = tmp_path / "probe.yaml"
    overlay.write_text(
        yaml.safe_dump({"thresholdConfig": {"optional_overrides": probe}}),
        encoding="utf-8")
    moved = re.search(r"以下\s*(\d+)\s*個 key",
                      _render_defaults_raw(repo_root, ["-f", str(overlay)]))
    assert moved and int(moved.group(1)) == len(probe), (
        "the note's count did not follow the overridden list — it is hardcoded")


@_needs_helm
def test_rendered_note_says_some_of_these_keys_have_a_measured_counterexample(
        repo_root: Path):
    """#1176: the ConfigMap is the artifact that reaches EVERY install, and
    `toYaml` strips the per-key `⚠ 參考庫實測反例` lines exactly as it strips
    the tier rationale (#1321's lesson, re-learned here).

    ⛔ What is asserted is the POINTER, not the clauses. Reproducing them in a
    Helm template would be a hand-copy that `--regen` cannot reach — the drift
    this line of work exists to delete. What must survive is that a reader of
    the ConfigMap alone knows (a) some of these keys have a measurement and
    (b) the failure runs in BOTH directions, because "the platform's numbers
    run low" is false for the under_fire one.
    """
    raw = _render_defaults_raw(repo_root)
    assert "參考庫實測反例" in raw, (
        "the ConfigMap ships the declared list with no hint that several of "
        f"those keys have a measured counter-example:\n{raw}")
    assert "誤警" in raw and "接不到" in raw, (
        "the note names only one failure direction — a blanket 'these values "
        "are too low' caveat is false for the under_fire key")
    # And the clauses themselves must NOT be here: a template copy cannot be
    # regenerated, so it would go stale on the next recalibration.
    for observed in _shipped_observed_clauses(repo_root):
        assert observed not in raw, (
            f"the template hand-copies a registry clause ({observed!r}) — it "
            "is not reachable by `check_threshold_registry.py --regen`")


def _shipped_observed_clauses(repo_root: Path) -> list[str]:
    registry = yaml.safe_load(
        (repo_root / "rule-packs" / "threshold-registry.yaml").read_text(
            encoding="utf-8"))
    return [e["value_counterexample"]["observed"]
            for e in (registry.get("keys") or {}).values()
            if "value_counterexample" in e]


@_needs_helm
@pytest.mark.parametrize("bad,kind", [
    # `len` aborts on these …
    (True, "bool"), (5, "float64"), ({"oracle_wait_time_rate": 5}, "map"),
    # The EMPTY map is its own case: Helm logs
    # "destination … is a table. Ignoring non-table value" and drops the
    # shipped list, so before the type check this rendered nothing at all —
    # a silently emptied declaration tier rather than an error.
    ({}, "map"),
    # … and silently miscounts these (character count), so the guard cannot
    # key off whether `len` survives the value.
    ("oracle_wait_time_rate", "string"),
    # FALSY non-lists: invisible to a truthy guard, hence the type check sits
    # outside it. Without that, `false` / `0` / `""` render as "deliberately
    # unset" — the operator gets silence instead of an error (CodeRabbit #1336).
    (False, "bool"), (0, "float64"), ("", "string"),
])
def test_non_list_values_fail_with_a_reason_not_a_template_error(
        repo_root: Path, tmp_path: Path, bad, kind):
    """A mistyped `optional_overrides` must fail with an explanation.

    Counting the list made `len` load-bearing, and `len` aborts the whole
    render on a non-collection — over one mistyped value the operator would
    lose Deployment/Service/PDB too, with only `error calling len: len of type
    bool` to go on. Worse, a scalar does NOT abort: `len` counts CHARACTERS, so
    `optional_overrides: "oracle_wait_time_rate"` rendered a confident
    "21 keys". A map renders too, and its key count is even accidentally
    right — while asserting values, which is the exact opposite of this tier.

    So the guard is on the TYPE, not on whether `len` happens to survive it.
    """
    overlay = tmp_path / "bad.yaml"
    overlay.write_text(
        yaml.safe_dump({"thresholdConfig": {"optional_overrides": bad}}),
        encoding="utf-8")
    proc = subprocess.run(
        ["helm", "template", "t", str(repo_root / _CHART), "-n", "monitoring",
         "-f", str(overlay)],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode != 0, (
        f"a {kind} was accepted; stdout:\n{proc.stdout[:800]}")
    assert "must be a LIST of key names" in proc.stderr, (
        "failed without naming the offending value — that is the opaque "
        f"template error this guard exists to replace:\n{proc.stderr[:800]}")


@_needs_helm
def test_empty_values_render_no_orphaned_note(repo_root: Path, tmp_path: Path):
    """Emptying the list must drop the explanation together with the key it
    explains. An orphaned '以下 0 個 key' paragraph would be worse than none:
    it describes a section the file no longer has."""
    overlay = tmp_path / "empty.yaml"
    overlay.write_text(
        yaml.safe_dump({"thresholdConfig": {"optional_overrides": []}}),
        encoding="utf-8")
    raw = _render_defaults_raw(repo_root, ["-f", str(overlay)])
    assert "個 key" not in raw and "--show-inheritance" not in raw, (
        f"note survived an emptied list:\n{raw}")
