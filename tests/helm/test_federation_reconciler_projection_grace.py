"""test_federation_reconciler_projection_grace.py — #1238 / TRK-352

`reconcile.projectionGraceSeconds` is the knob that decides how old a revocation
event must be before the reconciler will treat its absence from the live set as
tamper. It exists because #1238 latched `FederationRevocationTamperSuspected`
(`max_over_time(...[1h]) > 0`), and the `for: 5m` it replaced had been silently
doing a second job: absorbing the one-pass false positive you get when an event
is reconciled before this pod's own kubelet ConfigMap projection has caught up.

Two things about it are easy to get wrong, and both are pinned here because
neither is visible from the Python side.

**1. `0` must mean zero, not "unset".**
Every doc this feature ships with (values.yaml, Chart.yaml, CHANGELOG, runbook)
tells the operator that setting it to 0 reproduces the pre-#1238 windows
exactly. Sprig's `default` treats an explicit numeric 0 as empty, so the obvious
`{{ .Values... | default 180 }}` renders **180** for `--set ...=0` — the guard
stays fully armed while the operator believes they disabled it, and nothing
anywhere says so. The template therefore uses `hasKey` (the shape helm/vector's
evidenceChannel guards already use for the same reason). This file is what stops
someone "simplifying" it back.

**2. The number 180 is now written in three independent places.**
values.yaml (what ships), the template's absent-key fallback (what a
`--reuse-values` upgrade from a pre-0.2.0 release gets), and the Python
`DEFAULT_PROJECTION_GRACE_S` (what a hand-run of the script gets). They are only
correct as a SET: if the chart's fallback and the script's default disagree, the
same deployment answers "what is the grace?" differently depending on which one
you read, and the documented revert story stops being true. Asserted as mutual
equality rather than each against a literal — a literal-per-place test passes
when two of the three drift together.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
# ⛔ ONE literal "helm/federation-reconciler" string, not `/ "helm" / "federation-reconciler"`.
# scripts/tools/dx/verify_diff.py builds its text_map by scanning test files for
# literal path strings; the split form registers this guard against NOTHING, so
# a change to the chart would not select it (#1313 precedent).
_CHART = _REPO_ROOT / "helm/federation-reconciler"
_VALUES = _CHART / "values.yaml"
_SCRIPT = _REPO_ROOT / "scripts/tools/ops/_federation_revocation_reconciler.py"

_FLAG = "--projection-grace"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")
# Set to "1" by the CI job that installs helm (ci.yml python-tests-run ``env:``).
# When set, a missing binary is a FAILURE, not a skip — a broken setup-helm step
# would otherwise turn every render assertion here into a green no-op (#1300).
_REQUIRE_HELM = os.environ.get("VIBE_REQUIRE_HELM") == "1"


def test_helm_present_when_required() -> None:
    """Fail-closed guard against silent disarmament."""
    if _REQUIRE_HELM:
        assert _HAS_HELM, (
            "VIBE_REQUIRE_HELM=1 but no `helm` on PATH — every render assertion "
            "in this file would have skipped silently"
        )


def _rendered_grace(*overrides: str) -> str:
    """The value the container actually receives after ``--projection-grace``."""
    cmd = ["helm", "template", "fr", str(_CHART)]
    for o in overrides:
        cmd += ["--set", o]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
    for doc in yaml.safe_load_all(out.stdout):
        if not doc or doc.get("kind") != "Deployment":
            continue
        for c in doc["spec"]["template"]["spec"]["containers"]:
            argv = list(c.get("command", [])) + list(c.get("args", []))
            if _FLAG in argv:
                return str(argv[argv.index(_FLAG) + 1])
    raise AssertionError(f"{_FLAG} not found in the rendered Deployment")


def _script_default() -> int:
    """DEFAULT_PROJECTION_GRACE_S, read by parsing rather than grepping."""
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DEFAULT_PROJECTION_GRACE_S"
            for t in node.targets
        ):
            return int(ast.literal_eval(node.value))
    raise AssertionError("DEFAULT_PROJECTION_GRACE_S not found in the reconciler")


@_needs_helm
def test_explicit_zero_survives_rendering() -> None:
    """⛔ THE REGRESSION THIS FILE EXISTS FOR.

    `| default 180` renders 180 here, because Sprig treats numeric 0 as empty.
    The operator disables the guard, `helm upgrade` reports success, and the
    flag is unchanged — indistinguishable from "I set it and it had no effect".
    """
    assert _rendered_grace("reconcile.projectionGraceSeconds=0") == "0", (
        "explicit 0 was swallowed — the documented way to revert to the "
        "pre-#1238 windows does not work; use hasKey, not `default`"
    )


@_needs_helm
def test_explicit_non_default_value_is_passed_through() -> None:
    """The non-zero control for the case above. Without it, a template that
    hard-coded the flag to a literal would satisfy the zero test by accident
    (it would just always be wrong in the other direction)."""
    assert _rendered_grace("reconcile.projectionGraceSeconds=30") == "30"


@_needs_helm
def test_absent_key_falls_back(recwarn) -> None:
    """`--set k=null` DELETES the key in Helm, which is also the shape a
    `--reuse-values` upgrade from a pre-0.2.0 release presents: the stored
    values simply have no such key. That path must arm the guard, not disable
    it — absent is not the same statement as an explicit 0."""
    assert _rendered_grace("reconcile.projectionGraceSeconds=null") == str(_script_default())


@_needs_helm
def test_shipped_default_matches_the_template_fallback_and_the_script() -> None:
    """The three independent copies of this number must agree as a set."""
    shipped = yaml.safe_load(_VALUES.read_text(encoding="utf-8"))["reconcile"]["projectionGraceSeconds"]
    fallback = int(_rendered_grace("reconcile.projectionGraceSeconds=null"))
    script = _script_default()
    assert shipped == fallback == script, (
        f"projection grace disagrees across its three copies: values.yaml={shipped}, "
        f"template absent-key fallback={fallback}, DEFAULT_PROJECTION_GRACE_S={script}"
    )


@_needs_helm
def test_default_render_uses_the_shipped_value() -> None:
    """A plain `helm install` with no overrides must arm the guard. Distinct
    from the absent-key case above: this one exercises what values.yaml ships,
    that one exercises the template's own fallback, and they are allowed to be
    the same number only because the test above pins them equal."""
    assert _rendered_grace() == str(_script_default())
