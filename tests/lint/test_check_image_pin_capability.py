"""Tests for scripts/tools/lint/check_image_pin_capability.py.

The gate asserts that every `ghcr.io/vencil/da-tools:vX.Y.Z` pin in the k8s /
Helm surface points at a `tools/vX.Y.Z` tag whose tree actually contains the
entry point the workload runs. Each test pins one branch of that contract:

  - the LIVE repo is green, and green for the right reason: exactly the two
    registered EXEMPTIONS violate, there is no third, and every other da-tools
    workload passes on its own merits (TestLiveRepoIsClean);
  - counterfactual — de-registering either exemption turns the gate RED, so
    the gate is demonstrably doing the catching, not the registry;
  - an exemption licenses ONE TAG: moving a pin to a different, equally
    incapable tag is a violation, not an inherited pass (TestExemptionIsTagScoped);
  - fail-closed — an unfetched tag / an unresolvable workload / an empty
    capability parse / a da-tools chart out of which no container can be
    parsed are errors, never a silent pass;
  - the Helm two-line `repository:` + `tag:` pin is recombined correctly (the
    blind spot that line-oriented scanners have by construction);
  - the text-based capability parsers stay behaviourally identical to
    _lint_helpers.py's file-based ones.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_image_pin_capability.py"

_spec = importlib.util.spec_from_file_location("check_image_pin_capability", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

# The two known-incapable workloads, as EXEMPTIONS keys (source, entry, TAG).
_GOVERN_KEY = ("k8s/03-monitoring/cronjob-threshold-govern.yaml",
               "threshold-govern", "v2.9.0")
_RECONCILER_KEY = ("helm/federation-reconciler",
                   "_federation_revocation_reconciler.py", "v2.9.0")


@pytest.fixture(scope="module")
def live():
    """(workloads, errors) collected from the real repo."""
    errors: list[str] = []
    workloads = gate.collect_k8s_workloads(errors) + gate.collect_helm_workloads(errors)
    return workloads, errors


# ── live repo ───────────────────────────────────────────────────────────────
class TestLiveRepoIsClean:
    def test_no_collection_errors(self, live):
        """Every da-tools pin in the repo is resolvable — nothing is skipped."""
        _, errors = live
        assert errors == [], "da-tools pins the gate could not check:\n" + "\n".join(errors)

    def test_both_known_workloads_are_found(self, live):
        """The two incident workloads are actually in the scanned set.

        Guards the failure mode where the gate passes because it found
        NOTHING — in particular the Helm one, whose pin is split across
        `repository:` / `tag:` lines.
        """
        workloads, _ = live
        keys = {w.key for w in workloads}
        assert _GOVERN_KEY in keys
        assert _RECONCILER_KEY in keys

    def test_only_the_registered_two_are_incapable(self, live):
        """No THIRD incapable pin has crept in behind the registry."""
        workloads, _ = live
        violations, _, _ = gate.run_check(workloads, exemptions={})
        assert len(violations) == 2, (
            "expected exactly the 2 registered incapable pins, got:\n"
            + "\n".join(violations)
        )

    def test_live_repo_passes_with_the_real_registry(self, live):
        """With EXEMPTIONS applied the repo is green and nothing is stale."""
        workloads, _ = live
        violations, exempted, stale = gate.run_check(workloads, gate.EXEMPTIONS)
        assert violations == []
        assert stale == []
        assert len(exempted) == 2

    def test_a_capable_workload_exists(self, live):
        """At least one da-tools workload passes on its own merits.

        Positive control: without it, "0 violations" could equally mean the
        capability lookup always returns success.
        """
        workloads, _ = live
        capable = [w for w in workloads if gate.evaluate(w) is None]
        assert capable, "no da-tools workload passes — capability lookup is suspect"

    def test_every_exemption_key_matches_a_real_workload(self, live):
        """A registry entry that matches nothing is dead weight (and hides drift)."""
        workloads, _ = live
        keys = {w.key for w in workloads}
        assert set(gate.EXEMPTIONS) <= keys

    def test_every_exemption_documents_an_exit_condition(self):
        for key, rationale in gate.EXEMPTIONS.items():
            assert "EXIT:" in rationale, f"{key} has no exit condition"

    def test_cli_exits_zero(self):
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(_SCRIPT)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO_ROOT), timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr


# ── counterfactual: the gate, not the registry, does the catching ───────────
@pytest.mark.parametrize("dropped", [_GOVERN_KEY, _RECONCILER_KEY],
                         ids=["threshold-govern", "federation-reconciler"])
def test_de_registering_an_exemption_turns_the_gate_red(live, dropped):
    workloads, _ = live
    reduced = {k: v for k, v in gate.EXEMPTIONS.items() if k != dropped}
    violations, exempted, stale = gate.run_check(workloads, reduced)
    assert len(violations) == 1, violations
    assert len(exempted) == 1
    assert stale == []
    # The message must name the tag and the mechanism, not just "failed".
    assert "tools/v2.9.0" in violations[0]
    assert dropped[1] in violations[0]


class TestExemptionIsTagScoped:
    """An exemption licenses one IMAGE, not the workload for all time.

    With a (source, entry) key, re-pinning to another equally incapable tag
    inherited the exemption and the gate stayed green — the exact silent pass
    the gate exists to remove. The tag is therefore part of the key.
    """

    def test_same_workload_other_incapable_tag_is_not_exempt(self, live):
        """v2.8.0 lacks the subcommand too — and is NOT covered by the v2.9.0 entry."""
        workloads, _ = live
        pinned = next(w for w in workloads if w.key == _GOVERN_KEY)
        moved = gate.Workload(pinned.source, f"{pinned.source} (container govern)",
                              "v2.8.0", pinned.kind, pinned.entry)
        # Precondition: the other tag really is incapable (else this proves nothing).
        assert gate.evaluate(moved) is not None
        violations, exempted, _ = gate.run_check([moved], gate.EXEMPTIONS)
        assert exempted == []
        assert len(violations) == 1
        assert "tools/v2.8.0" in violations[0]

    def test_key_carries_the_tag(self, live):
        workloads, _ = live
        pinned = next(w for w in workloads if w.source == _GOVERN_KEY[0])
        assert pinned.key == (pinned.source, pinned.entry, pinned.image_tag)
        assert len(pinned.key) == 3

    def test_registry_keys_are_all_tag_qualified(self):
        for key in gate.EXEMPTIONS:
            assert len(key) == 3, key
            assert gate._TAG_RE.match(key[2]), f"{key} third element is not a vX.Y.Z tag"


def test_stale_exemption_is_reported(live):
    """An entry the pinned image now SATISFIES must be flagged for deletion."""
    workloads, _ = live
    capable = next(w for w in workloads if gate.evaluate(w) is None)
    violations, _, stale = gate.run_check(
        workloads, {**gate.EXEMPTIONS, capable.key: "bogus — EXIT: never"})
    assert violations == []
    assert len(stale) == 1
    assert capable.source in stale[0]
    assert "delete" in stale[0].lower()


# ── fail-closed ─────────────────────────────────────────────────────────────
def test_unfetched_tag_is_an_error_not_a_pass():
    """A pin whose tag is absent locally must NOT be reported as capable."""
    gate._capability_cache.pop("tools/v99.99.99", None)
    bogus = gate.Workload("k8s/fake.yaml", "k8s/fake.yaml (container c)",
                          "v99.99.99", "subcommand", "threshold-govern")
    with pytest.raises(gate.CapabilityError) as excinfo:
        gate.evaluate(bogus)
    message = str(excinfo.value)
    # Must be ACTIONABLE in both environments.
    assert "fetch-depth: 0" in message
    assert "git fetch --tags" in message


def test_empty_capability_parse_is_an_error(monkeypatch):
    """A tag whose sources parse to nothing must not read as 'contains nothing → violation'.

    That direction would be a *wrong* answer dressed as a finding; the gate
    must say it could not run instead.
    """
    gate._capability_cache.pop("tools/v2.9.0", None)
    monkeypatch.setattr(gate, "parse_command_map_text", lambda _text: {})
    with pytest.raises(gate.CapabilityError) as excinfo:
        gate.capabilities_for_tag("tools/v2.9.0")
    assert "refusing" in str(excinfo.value)
    gate._capability_cache.pop("tools/v2.9.0", None)


def test_unresolvable_workload_is_an_error(tmp_path, monkeypatch):
    """A da-tools container with neither a script path nor a subcommand errors."""
    manifest = tmp_path / "k8s" / "x.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "apiVersion: v1\n"
        "kind: Pod\n"
        "spec:\n"
        "  containers:\n"
        "    - name: mystery\n"
        "      image: ghcr.io/vencil/da-tools:v2.9.0\n"
        "      args: ['--only-flags']\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    errors: list[str] = []
    workloads = gate.collect_k8s_workloads(errors)
    assert workloads == []
    assert len(errors) == 1
    assert "cannot tell what this da-tools container runs" in errors[0]


def test_digest_pinned_image_is_an_error(tmp_path, monkeypatch):
    manifest = tmp_path / "k8s" / "x.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "apiVersion: v1\n"
        "kind: Pod\n"
        "spec:\n"
        "  containers:\n"
        "    - name: c\n"
        "      image: ghcr.io/vencil/da-tools:v2.9.0@sha256:" + "a" * 64 + "\n"
        "      args: [threshold-govern]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    errors: list[str] = []
    assert gate.collect_k8s_workloads(errors) == []
    assert any("digest" in e for e in errors)


# ── Helm: the two-line pin ──────────────────────────────────────────────────
_CHART_YAML = "apiVersion: v2\nname: probe\nversion: 0.1.0\n"
_VALUES_YAML = """\
image:
  repository: ghcr.io/vencil/da-tools
  tag: "v2.9.0"
  digest: ""
  pullPolicy: IfNotPresent
"""
_DEPLOYMENT_TMPL = """\
{{- /* a multi-line template comment
       that spans lines, as real charts have */ -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "probe.name" . }}
  labels:
    {{- include "probe.labels" . | nindent 4 }}
spec:
  template:
    spec:
      containers:
        - name: probe
          image: {{ include "probe.image" . | quote }}
          {{- if .Values.enabled }}
          command:
            - python3
            - /opt/da-tools/maintenance_scheduler.py
            - --flag
          {{- end }}
"""


def _write_chart(root: Path, values: str = _VALUES_YAML, template: str = _DEPLOYMENT_TMPL):
    chart = root / "helm" / "probe"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(_CHART_YAML, encoding="utf-8")
    (chart / "values.yaml").write_text(values, encoding="utf-8")
    (chart / "templates" / "deployment.yaml").write_text(template, encoding="utf-8")
    return chart


def test_helm_two_line_pin_is_recombined(tmp_path, monkeypatch):
    """`repository:` and `tag:` on separate lines must yield one pin.

    This is the case no line-oriented regex can see — and the reason
    bump_docs.py has never noticed the federation-reconciler pin.
    """
    _write_chart(tmp_path)
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    errors: list[str] = []
    workloads = gate.collect_helm_workloads(errors)
    assert errors == []
    assert len(workloads) == 1
    assert workloads[0].image_tag == "v2.9.0"
    assert workloads[0].git_tag == "tools/v2.9.0"
    assert workloads[0].kind == "script"
    assert workloads[0].entry == "maintenance_scheduler.py"
    assert workloads[0].source == "helm/probe"


def test_helm_chart_without_a_da_tools_pin_is_skipped(tmp_path, monkeypatch):
    _write_chart(tmp_path, values=_VALUES_YAML.replace(
        "ghcr.io/vencil/da-tools", "ghcr.io/vencil/tenant-api"))
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    errors: list[str] = []
    assert gate.collect_helm_workloads(errors) == []
    assert errors == []


def test_helm_values_override_pin_is_also_checked(tmp_path, monkeypatch):
    """A `values-<env>.yaml` that re-pins image.tag is a real deployment.

    Reading only the default values.yaml would let an override ship an
    incapable tag with the gate green.
    """
    chart = _write_chart(tmp_path)
    (chart / "values-prod.yaml").write_text(
        'image:\n  repository: ghcr.io/vencil/da-tools\n  tag: "v2.8.0"\n',
        encoding="utf-8")
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    errors: list[str] = []
    workloads = gate.collect_helm_workloads(errors)
    assert errors == []
    assert sorted(w.image_tag for w in workloads) == ["v2.8.0", "v2.9.0"]


def test_helm_mixed_image_chart_is_an_error(tmp_path, monkeypatch):
    """Chart-level attribution is only sound while the chart is single-image."""
    chart = _write_chart(tmp_path)
    (chart / "values.yaml").write_text(
        _VALUES_YAML + "sidecar:\n  repository: ghcr.io/vencil/tenant-api\n"
                       '  tag: "v2.9.0"\n',
        encoding="utf-8")
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    errors: list[str] = []
    assert gate.collect_helm_workloads(errors) == []
    assert len(errors) == 1
    assert "ambiguous" in errors[0]


# ── #1316: per-container attribution unblocks the sound mixed-image case ────
# The chart-level refusal above stays for genuinely ambiguous charts. What it
# used to also block is the case where the foreign image is right there in the
# template with an `image:` naming its own values path — a sidecar or an init
# container. That is not ambiguous, and refusing it forced any chart running a
# da-tools workload to be single-image forever.
_SIDECAR_VALUES = _VALUES_YAML + (
    'preflight:\n'
    '  image:\n'
    '    repository: busybox\n'
    '    tag: "1.36"\n'
)
_SIDECAR_TMPL = _DEPLOYMENT_TMPL + """\
      initContainers:
        - name: preflight
          image: "{{ .Values.preflight.image.repository }}:{{ .Values.preflight.image.tag }}"
          command: ["/bin/sh", "-c", "test -e /etc/x"]
"""


def test_a_container_reading_a_foreign_pin_is_attributed_not_refused(tmp_path, monkeypatch):
    """A container whose `image:` reads a NON-da-tools pin is not a da-tools
    container, so neither the mixed-image refusal nor the entry-point demand
    applies to it. Both would fire without attribution: the chart holds two
    repositories, and `/bin/sh -c` resolves to no da-tools entry point."""
    _write_chart(tmp_path, values=_SIDECAR_VALUES, template=_SIDECAR_TMPL)
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    errors: list[str] = []
    workloads = gate.collect_helm_workloads(errors)
    assert errors == [], errors
    assert [w.entry for w in workloads] == ["maintenance_scheduler.py"], (
        "the da-tools container must still be checked — attribution may only "
        "SUBTRACT the foreign container, never the workload being guarded"
    )


def test_an_unclaimed_foreign_pin_is_still_refused(tmp_path, monkeypatch):
    """⛔ The fail-closed half. A foreign pin that NO container's `image:`
    reads means something in this chart runs an image the gate cannot see —
    the original ambiguity, and it stays fatal. (The pre-existing
    test_helm_mixed_image_chart_is_an_error is the same statement from before
    attribution existed; this one pins that adding a claimed pin alongside does
    not launder the unclaimed one.)"""
    _write_chart(
        tmp_path,
        values=_SIDECAR_VALUES + 'stray:\n  repository: ghcr.io/vencil/tenant-api\n  tag: "v2.9.0"\n',
        template=_SIDECAR_TMPL)
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    errors: list[str] = []
    assert gate.collect_helm_workloads(errors) == []
    assert len(errors) == 1
    assert "ambiguous" in errors[0]
    assert "ghcr.io/vencil/tenant-api" in errors[0]
    assert "busybox" not in errors[0], "the CLAIMED pin must not be reported as ambiguous"


def test_an_expression_reading_the_da_tools_pin_too_is_never_attributed_away(tmp_path, monkeypatch):
    """⛔ THE FAIL-OPEN THIS ASSERTION EXISTS FOR — it survived the first cut of
    `attributed_pin`, which matched only against the FOREIGN pins.

    An `image:` that reads the da-tools repository with another pin's tag then
    looked like a clean foreign hit, so the one container the gate exists to
    check was skipped and the chart passed. Matching against every pin makes it
    ambiguous instead, and an ambiguous container is checked as before.

    ⛔ The foreign pin is named `agent` on purpose. Pin trails are compared as a
    SET, and an implementation that picks one arbitrarily instead of demanding
    exactly one match would sort `image` (da-tools) before `preflight.image` and
    pass this test by pure alphabetical luck. `agent.image` sorts BEFORE `image`,
    so an arbitrary pick lands on the foreign pin and the container is skipped —
    which is the fail-open, and now visible.
    """
    values = _VALUES_YAML + 'agent:\n  image:\n    repository: busybox\n    tag: "1.36"\n'
    template = _DEPLOYMENT_TMPL + """\
      initContainers:
        - name: preflight
          image: "{{ .Values.image.repository }}:{{ .Values.agent.image.tag }}"
          command: ["/bin/sh", "-c", "true"]
"""
    _write_chart(tmp_path, values=values, template=template)
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    errors: list[str] = []
    workloads = gate.collect_helm_workloads(errors)
    assert errors, "a container reading the da-tools pin was attributed away and the chart passed"
    assert workloads == [], "an ambiguous chart must yield no verified workloads"


def test_an_image_expression_touching_two_foreign_pins_stays_unattributed(tmp_path, monkeypatch):
    """Ambiguity inside ONE expression is refused rather than guessed at."""
    values = _SIDECAR_VALUES + 'other:\n  image:\n    repository: alpine\n    tag: "3"\n'
    template = _DEPLOYMENT_TMPL + """\
      initContainers:
        - name: preflight
          image: "{{ .Values.preflight.image.repository }}:{{ .Values.other.image.tag }}"
          command: ["/bin/sh", "-c", "true"]
"""
    _write_chart(tmp_path, values=values, template=template)
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    errors: list[str] = []
    gate.collect_helm_workloads(errors)
    assert errors, "an expression spanning two pins was silently attributed"


def test_indexed_stripping_parses_to_the_same_structure() -> None:
    """The attribution pass must not see a different document than the original
    one. Asserted over every shipped chart template, because a divergence would
    show up as containers that exist for one pass and not the other."""
    import yaml as _yaml

    for chart in sorted((gate.REPO_ROOT / "helm").iterdir()):
        for template in sorted((chart / "templates").rglob("*.yaml")) if (chart / "templates").is_dir() else []:
            text = template.read_text(encoding="utf-8")
            plain = list(_yaml.safe_load_all(gate.strip_helm_actions(text)))
            indexed_text, _ = gate.strip_helm_actions_indexed(text)
            indexed = list(_yaml.safe_load_all(indexed_text))
            assert len(plain) == len(indexed), template
            for a, b in zip(plain, indexed):
                assert [c.get("name") for c in gate.iter_containers(a)] == \
                       [c.get("name") for c in gate.iter_containers(b)], template


def test_yml_extension_is_scanned(tmp_path, monkeypatch):
    """The pre-commit `files:` regex accepts `.yml`; the scanner must too."""
    manifest = tmp_path / "k8s" / "job.yml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "apiVersion: v1\n"
        "kind: Pod\n"
        "spec:\n"
        "  containers:\n"
        "    - name: c\n"
        "      image: ghcr.io/vencil/da-tools:v2.9.0\n"
        "      args: [threshold-govern]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    errors: list[str] = []
    workloads = gate.collect_k8s_workloads(errors)
    assert errors == []
    assert [(w.source, w.entry) for w in workloads] == [("k8s/job.yml", "threshold-govern")]


class TestDaToolsChartCannotSilentlyYieldNothing:
    """Four chart shapes that used to report `workloads=0, errors=[]` — i.e. OK.

    Each is an ordinary thing a chart may legitimately do; none of them is a
    reason to declare a da-tools pin verified. The gate must say it could not
    check, not that everything is fine.
    """

    def test_repository_without_a_tag_key_is_an_error(self, tmp_path, monkeypatch):
        """`repository:` with no `tag:` — the template defaults to .Chart.AppVersion."""
        _write_chart(tmp_path, values=(
            "image:\n"
            "  repository: ghcr.io/vencil/da-tools\n"
            "  pullPolicy: IfNotPresent\n"))
        monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
        errors: list[str] = []
        assert gate.collect_helm_workloads(errors) == []
        assert len(errors) == 1
        assert "no `tag:` key" in errors[0].lower() or "no `tag:` key" in errors[0]

    def test_containers_injected_by_a_whole_line_include_is_an_error(
            self, tmp_path, monkeypatch):
        """`{{- include "x.containers" . | nindent 8 }}` is stripped as control flow."""
        _write_chart(tmp_path, template=(
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            '      {{- include "probe.containers" . | nindent 6 }}\n'))
        monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
        errors: list[str] = []
        assert gate.collect_helm_workloads(errors) == []
        assert len(errors) == 1
        assert "NO container could be parsed" in errors[0]

    def test_templates_with_no_yaml_at_all_is_an_error(self, tmp_path, monkeypatch):
        chart = _write_chart(tmp_path)
        (chart / "templates" / "deployment.yaml").unlink()
        (chart / "templates" / "NOTES.txt").write_text("hi\n", encoding="utf-8")
        monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
        errors: list[str] = []
        assert gate.collect_helm_workloads(errors) == []
        assert len(errors) == 1
        assert "NO container could be parsed" in errors[0]

    def test_template_in_a_subdirectory_is_scanned(self, tmp_path, monkeypatch):
        """The pre-commit `files:` regex matches templates/<subdir>/x.yaml.

        A non-recursive glob left the hook firing on a file the scanner never
        opened — and printing OK.
        """
        chart = _write_chart(tmp_path)
        (chart / "templates" / "deployment.yaml").unlink()
        nested = chart / "templates" / "workloads"
        nested.mkdir()
        (nested / "deployment.yaml").write_text(_DEPLOYMENT_TMPL, encoding="utf-8")
        monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
        errors: list[str] = []
        workloads = gate.collect_helm_workloads(errors)
        assert errors == []
        assert [w.entry for w in workloads] == ["maintenance_scheduler.py"]
        assert "workloads/deployment.yaml" in workloads[0].where

    def test_a_chart_the_gate_can_read_stays_clean(self, tmp_path, monkeypatch):
        """Positive control: the zero-container error must not fire on a normal chart."""
        _write_chart(tmp_path)
        monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
        errors: list[str] = []
        assert len(gate.collect_helm_workloads(errors)) == 1
        assert errors == []


def test_helm_digest_pin_is_an_error(tmp_path, monkeypatch):
    _write_chart(tmp_path, values=_VALUES_YAML.replace(
        'digest: ""', 'digest: "sha256:' + "b" * 64 + '"'))
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    errors: list[str] = []
    assert gate.collect_helm_workloads(errors) == []
    assert any("digest" in e for e in errors)


def test_helm_template_actions_are_stripped_to_parseable_yaml():
    stripped = gate.strip_helm_actions(_DEPLOYMENT_TMPL)
    assert "{{" not in stripped and "}}" not in stripped
    import yaml
    doc = yaml.safe_load(stripped)
    containers = list(gate.iter_containers(doc))
    assert len(containers) == 1
    assert containers[0]["command"][1] == "/opt/da-tools/maintenance_scheduler.py"


def test_every_shipped_helm_template_survives_stripping():
    """Regression pin: every real chart template must stay YAML-parseable.

    If one stops parsing, the gate reports an error rather than silently
    missing a container — but the error would be noise, so pin it here.
    """
    import yaml
    for template in sorted(REPO_ROOT.glob("helm/*/templates/*.yaml")):
        stripped = gate.strip_helm_actions(template.read_text(encoding="utf-8"))
        try:
            list(yaml.safe_load_all(stripped))
        except yaml.YAMLError as exc:  # pragma: no cover - failure path
            pytest.fail(f"{template.relative_to(REPO_ROOT).as_posix()}: {exc}")


# ── entry-point resolution ──────────────────────────────────────────────────
@pytest.mark.parametrize("container,expected", [
    ({"args": ["threshold-govern", "--json"]}, ("subcommand", "threshold-govern")),
    ({"command": ["python3", "/opt/da-tools/x.py", "-v"]}, ("script", "x.py")),
    # `command:` REPLACES the image entrypoint, so args[0] is not a subcommand.
    ({"command": ["python3", "/opt/da-tools/x.py"], "args": ["threshold-govern"]},
     ("script", "x.py")),
    ({"args": ["--only-flags"]}, None),
    ({"args": []}, None),
    ({"command": ["/bin/sh", "-c", "echo hi"]}, None),
    ({}, None),
])
def test_resolve_entry(container, expected):
    assert gate.resolve_entry(container) == expected


@pytest.mark.parametrize("image,expected", [
    ("ghcr.io/vencil/da-tools:v2.9.0", ("ghcr.io/vencil/da-tools", "v2.9.0", None)),
    ("ghcr.io/vencil/da-tools", ("ghcr.io/vencil/da-tools", None, None)),
    ("ghcr.io/vencil/da-tools:v2.9.0@sha256:" + "c" * 64,
     ("ghcr.io/vencil/da-tools", "v2.9.0", "sha256:" + "c" * 64)),
])
def test_parse_image_ref(image, expected):
    assert gate.parse_image_ref(image) == expected


# ── capability extraction ───────────────────────────────────────────────────
def test_v290_lacks_both_entry_points():
    """Ground truth for the two incidents, read straight off the tag."""
    command_map, tool_files = gate.capabilities_for_tag("tools/v2.9.0")
    assert "threshold-govern" not in command_map
    assert "threshold_govern.py" not in tool_files
    assert "_federation_revocation_reconciler.py" not in tool_files
    # …and the positive control that makes those absences meaningful.
    assert command_map.get("maintenance-scheduler") == "maintenance_scheduler.py"
    assert "maintenance_scheduler.py" in tool_files


def test_text_parsers_match_lib_lint_helpers_on_head():
    """The text-based parsers must not drift from _lint_helpers.py's file-based ones."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "lint"))
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
    import _lint_helpers  # noqa: PLC0415

    entrypoint = (REPO_ROOT / "components" / "da-tools" / "app" / "entrypoint.py")
    build_sh = (REPO_ROOT / "components" / "da-tools" / "app" / "build.sh")
    assert (gate.parse_command_map_text(entrypoint.read_text(encoding="utf-8"))
            == _lint_helpers.parse_command_map())
    assert (gate.parse_tool_files_text(build_sh.read_text(encoding="utf-8"))
            == _lint_helpers.parse_build_sh_tools())


def test_head_would_satisfy_both_workloads():
    """HEAD already contains both entry points — so the ONLY thing missing is a release.

    This is what makes the EXEMPTIONS exit condition ("bump the pin in the
    next tools/v* release") a real, reachable trigger rather than a wish.
    """
    entrypoint = (REPO_ROOT / "components" / "da-tools" / "app" / "entrypoint.py")
    build_sh = (REPO_ROOT / "components" / "da-tools" / "app" / "build.sh")
    command_map = gate.parse_command_map_text(entrypoint.read_text(encoding="utf-8"))
    tool_files = gate.parse_tool_files_text(build_sh.read_text(encoding="utf-8"))
    assert command_map.get("threshold-govern") == "threshold_govern.py"
    assert "threshold_govern.py" in tool_files
    assert "_federation_revocation_reconciler.py" in tool_files
