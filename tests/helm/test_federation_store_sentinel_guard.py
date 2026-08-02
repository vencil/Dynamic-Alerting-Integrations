"""test_federation_store_sentinel_guard.py — store-mount sentinel (#1316)

A ConfigMap volume only ever resolves an object in the mounting pod's OWN
namespace, and both federation store consumers mount theirs with
``optional: true``. Get the namespace wrong and Kubernetes hands the pod an
empty directory, Ready status and no event — measured on a real cluster in
#1313 — after which the gateway enforces an empty revoked set and every revoked
token is honoured until its TTL.

The runtime keys cannot answer "did my mount resolve?", because their absence is
also what a legitimately fresh store looks like (tenant-api writes store.json /
revoked.txt on the first token operation). So the tenant-api chart writes ONE
key of its own at install time and each consumer runs a preflight
init-container that looks for it.

That design puts the same string in THREE charts with no shared helper, and it
puts a shell script in two of them that has to stay identical. Everything below
exists because a silent rename or a one-sided edit disarms the check while every
render still succeeds:

  * the key: producer writes it, two consumers read it — pinned as a set;
  * the reconciler's ``items:`` allowlist: without the sentinel listed there the
    projection carries revoked.txt only, and the three-state check silently
    collapses into "does revoked.txt exist?", which is the very test #1316
    rejected because it cannot tell a misconfiguration from a fresh store;
  * the log phrase: the gateway's preflight deliberately emits the phrase the
    Lua already uses, so the shipped ``FederationGatewayRevokedSetMissing``
    alert covers it with no new metric. If the two drift, the line is written
    to a log nobody queries;
  * the init-container NAME: Vector stamps it into ``app``, which is what the
    reconciler's LogsQL filters on. Rename the container and the query stops
    matching — with no error anywhere.

The behavioural half executes the shipped script against directory layouts that
mirror what kubelet actually projects. ⛔ The mirror is load-bearing: a kubelet
ConfigMap mount is NEVER an empty directory — it always contains ``..data`` and
a timestamped payload dir, so ``ls -A`` returns entries even when the ConfigMap
does not exist at all (measured). A check written with ``ls -A`` would look
correct and never fire. The fixtures reproduce those entries as plain
dot-prefixed directories rather than kubelet's symlinks: both are invisible to a
plain ``ls``, which is the only property the script depends on, and plain dirs
keep the fixture portable. The four states themselves were also exercised
against a real cluster (kind), so this file is a fast mirror of a
cluster-verified truth rather than a substitute for one.
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
# ⛔ ONE literal path string per chart, never `/ "helm" / "..."`.
# scripts/tools/dx/verify_diff.py builds its text_map by scanning test files for
# literal path strings; the split form registers this guard against NOTHING, so
# a change to the chart would not select it (#1313 precedent).
_PRODUCER = _REPO_ROOT / "helm/tenant-api"
_GATEWAY = _REPO_ROOT / "helm/federation-gateway"
_RECONCILER = _REPO_ROOT / "helm/federation-reconciler"
_SCRIPT = _REPO_ROOT / "scripts/tools/ops/_federation_revocation_reconciler.py"

_PREFLIGHT_CONTAINER = "federation-store-preflight"

_HAS_HELM = shutil.which("helm") is not None
_needs_helm = pytest.mark.skipif(not _HAS_HELM, reason="helm CLI not on PATH")
# Set to "1" by the CI job that installs helm (ci.yml python-tests-run ``env:``).
# When set, a missing binary is a FAILURE, not a skip — a broken setup-helm step
# would otherwise turn every render assertion here into a green no-op (#1300).
_REQUIRE_HELM = os.environ.get("VIBE_REQUIRE_HELM") == "1"

_SH = shutil.which("sh")
_needs_sh = pytest.mark.skipif(_SH is None, reason="no POSIX sh on PATH")


def test_helm_present_when_required() -> None:
    """Fail-closed guard against silent disarmament."""
    if _REQUIRE_HELM:
        assert _HAS_HELM, (
            "VIBE_REQUIRE_HELM=1 but no `helm` on PATH — every render assertion "
            "in this file would have skipped silently"
        )


def test_sh_present_on_posix() -> None:
    """Same fail-closed reasoning for the behavioural half. On Linux (every CI
    runner) `sh` is always there, so a skip means the environment is broken, not
    that the tests are inapplicable."""
    if os.name != "nt":
        assert _SH is not None, "no `sh` on a POSIX host — the behavioural tests would skip silently"


# ── helpers ──────────────────────────────────────────────────────────────────
def _values(chart: Path) -> dict:
    return yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8"))


def _render(chart: Path, *overrides: str, namespace: str = "ns-under-test") -> list[dict]:
    cmd = ["helm", "template", "rel", str(chart), "--namespace", namespace]
    for o in overrides:
        cmd += ["--set", o]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=90)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _render_fails(chart: Path, *overrides: str) -> str:
    cmd = ["helm", "template", "rel", str(chart)]
    for o in overrides:
        cmd += ["--set", o]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    assert res.returncode != 0, "the render was expected to abort but succeeded"
    return res.stderr


def _preflight(chart: Path, *overrides: str) -> dict | None:
    for doc in _render(chart, *overrides):
        if doc.get("kind") != "Deployment":
            continue
        for c in doc["spec"]["template"]["spec"].get("initContainers") or []:
            if c["name"] == _PREFLIGHT_CONTAINER:
                return c
    return None


def _preflight_env(container: dict) -> dict[str, str]:
    return {e["name"]: e["value"] for e in container["env"] if "value" in e}


def _python_const(name: str) -> str:
    """Read a reconciler constant by parsing, not grepping."""
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return str(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in {_SCRIPT.name}")


# ── static layer: the key is one value living in three charts ────────────────
def test_the_sentinel_key_is_the_same_string_in_all_three_charts() -> None:
    """Producer writes it, both consumers look for it. Asserted as a SET rather
    than each against a literal — a per-chart literal test passes when two of
    the three drift together."""
    producer = _values(_PRODUCER)["federation"]["store"]["sentinelKey"]
    gateway = _values(_GATEWAY)["revokedSet"]["sentinelKey"]
    reconciler = _values(_RECONCILER)["store"]["sentinelKey"]
    assert producer == gateway == reconciler, (
        f"sentinel key disagrees across the charts that must share it: "
        f"tenant-api={producer!r}, federation-gateway={gateway!r}, "
        f"federation-reconciler={reconciler!r} — the preflight on the "
        f"disagreeing side reports every healthy store as missing"
    )


def test_the_sentinel_key_is_not_a_runtime_key() -> None:
    """The shipped default must not name a key tenant-api writes. The producer
    template refuses such a render (below); this asserts the value we actually
    ship never depends on that refusal."""
    assert _values(_PRODUCER)["federation"]["store"]["sentinelKey"] not in {
        "store.json",
        "revoked.txt",
    }


def test_both_consumers_default_to_warn() -> None:
    """`enforce` is opt-in on purpose: a consumer chart upgraded ahead of the
    producer legitimately sees a store with no sentinel yet, and this repo ships
    no gateway replica-down alert to catch an outage caused here."""
    assert _values(_GATEWAY)["preflight"]["mode"] == "warn"
    assert _values(_RECONCILER)["preflight"]["mode"] == "warn"


def test_the_two_preflight_scripts_are_identical() -> None:
    """Two charts, one check. They are copies because Helm has no cross-chart
    helper — so the copies get compared instead of trusted. Compared as the
    rendered-template TEXT so a divergence in either file fails here, without
    needing helm."""
    gw = (_GATEWAY / "templates/deployment.yaml").read_text(encoding="utf-8")
    rc = (_RECONCILER / "templates/deployment.yaml").read_text(encoding="utf-8")
    marker = '              set -eu\n'
    assert marker in gw and marker in rc, "preflight script block not found in a chart"

    def _script(text: str) -> str:
        body = text.split(marker, 1)[1]
        return marker + body.split("          volumeMounts:", 1)[0]

    assert _script(gw) == _script(rc), (
        "the gateway and reconciler preflight scripts have drifted — one of the "
        "two consumers is now running a different check"
    )


# ── static layer: the log phrase and the container name are contracts ────────
def test_the_preflight_emits_the_phrase_the_missing_alert_queries() -> None:
    """The whole point of the default (warn) mode: reuse the shipped
    `FederationGatewayRevokedSetMissing` signal instead of adding an alert. That
    only works while the string the script echoes is the string the reconciler
    greps for."""
    phrase = _python_const("GATEWAY_MISSING_PHRASE")
    gw = (_GATEWAY / "templates/deployment.yaml").read_text(encoding="utf-8")
    assert phrase in gw, (
        f"the gateway preflight no longer emits {phrase!r} — its line would be "
        f"written into VictoriaLogs and queried by nobody"
    )


def test_the_preflight_cannot_be_mistaken_for_the_other_two_warnings() -> None:
    """⛔ The gateway's three revoked-set warnings mean different things about
    what is being let through right now, and each feeds its own gauge. The
    reconciler pins pairwise non-containment for the Lua's three; the preflight
    is a FOURTH emitter of one of them and has to obey the same rule — its
    legacy line especially, which must not be counted as "no revoked set" when
    a set is in fact being enforced."""
    failopen = _python_const("GATEWAY_FAILOPEN_PHRASE")
    rejected = _python_const("GATEWAY_REJECTED_PHRASE")
    missing = _python_const("GATEWAY_MISSING_PHRASE")
    gw = (_GATEWAY / "templates/deployment.yaml").read_text(encoding="utf-8")
    legacy_lines = [ln for ln in gw.splitlines() if "store mount preflight" in ln and "echo" in ln]
    assert legacy_lines, "the legacy-store branch no longer emits anything"
    for ln in legacy_lines:
        for other in (failopen, rejected, missing):
            assert other not in ln, (
                f"the legacy-store line contains {other!r}; it would be counted "
                f"as that warning and read as a posture it does not describe"
            )


def test_the_container_name_is_the_one_the_reconciler_query_admits() -> None:
    """Vector stamps the container name into `app` from the PRISTINE kubelet
    metadata, and the reconciler's missing query filters on `app`. Rename the
    container without renaming the constant and the query silently stops
    matching the preflight's row."""
    assert _python_const("GATEWAY_PREFLIGHT_APP") == _PREFLIGHT_CONTAINER


def test_the_missing_query_admits_both_producers_and_the_others_do_not() -> None:
    """Widened for the preflight — and ONLY the missing query. The other two
    warnings mean "read it and could not use it", which needs an actual reload
    attempt the preflight never makes, so admitting the preflight there could
    only add a way to miscount."""
    import sys

    # Same import shape tests/ops/test_federation_revocation_reconciler.py uses.
    # ⛔ NOT importlib.util.spec_from_file_location: the module defines frozen
    # dataclasses, and a module that is not registered in sys.modules makes
    # dataclasses' own type resolution fail with an unrelated AttributeError.
    tools_dir = str(_REPO_ROOT / "scripts/tools/ops")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import _federation_revocation_reconciler as mod  # noqa: PLC0415

    missing = mod.build_missing_query(600, 60)
    assert f'app:"{mod.GATEWAY_APP}"' in missing
    assert f'app:"{mod.GATEWAY_PREFLIGHT_APP}"' in missing
    for other in (mod.build_failopen_query(600, 60), mod.build_rejected_query(600, 60)):
        assert mod.GATEWAY_PREFLIGHT_APP not in other


# ── render layer ─────────────────────────────────────────────────────────────
@_needs_helm
def test_the_producer_writes_exactly_one_key() -> None:
    """⛔ The reason this ConfigMap carried no `data` at all: a key this chart
    templates is a key every `helm upgrade` overwrites. Exactly one is the
    invariant — a second one re-opens the wipe hazard for whatever it names."""
    cms = [
        d
        for d in _render(_PRODUCER, "federation.enabled=true")
        if d.get("kind") == "ConfigMap"
        and d["metadata"]["name"] == _values(_PRODUCER)["federation"]["store"]["configMapName"]
    ]
    assert len(cms) == 1, "the store ConfigMap is not rendered exactly once"
    data = cms[0].get("data") or {}
    assert list(data) == [_values(_PRODUCER)["federation"]["store"]["sentinelKey"]], (
        f"the store ConfigMap templates {sorted(data)} — it must template the "
        f"sentinel and nothing else, or `helm upgrade` starts overwriting live state"
    )


@_needs_helm
@pytest.mark.parametrize("key", ["off", "no", "yes", "123"])
def test_the_rendered_sentinel_key_is_always_a_string(key: str) -> None:
    """⛔ An UNQUOTED Go-template map key is resolved by YAML 1.1. Measured on
    the shipped chart before the `| quote` went in: `off`/`no` rendered the
    BOOLEAN key `false`, `yes` rendered `true`, and `123` rendered an integer.
    Both consumers quote their side, so they would look for the literal string
    and never find it — every gateway and reconciler in the fleet would report
    its store missing forever, from a values edit that renders and applies
    perfectly cleanly. Parametrised over the reserved words rather than asserted
    once, because `off` alone would pass a template that special-cased it."""
    docs = _render(_PRODUCER, "federation.enabled=true", f"federation.store.sentinelKey={key}")
    cms = [d for d in docs if d.get("kind") == "ConfigMap"
           and d["metadata"]["name"] == "tenant-federation-store"]
    assert len(cms) == 1
    rendered = list(cms[0]["data"])
    assert rendered == [key], f"sentinel key was coerced: expected {[key]!r}, got {rendered!r}"


@_needs_helm
def test_the_producer_refuses_a_sentinel_aimed_at_a_runtime_key() -> None:
    """The load-bearing guard. Setting the sentinel to a runtime key would make
    every upgrade reset the live token store — the ADR-028 un-revoke, caused by
    the upgrade itself. A comment is not a control, so the render aborts."""
    for key in ("store.json", "revoked.txt"):
        err = _render_fails(_PRODUCER, "federation.enabled=true", f"federation.store.sentinelKey={key}")
        assert "RUNTIME key" in err, err[-400:]


@_needs_helm
def test_the_producer_refuses_an_empty_sentinel() -> None:
    """An empty key renders a ConfigMap the consumers can never satisfy: every
    healthy gateway would report its store missing, and the alert that means
    "revocation is not being enforced" would fire everywhere at once."""
    err = _render_fails(_PRODUCER, "federation.enabled=true", "federation.store.sentinelKey=")
    assert "is empty" in err, err[-400:]


@_needs_helm
@pytest.mark.parametrize("chart,key_path", [
    (_GATEWAY, ("revokedSet", "sentinelKey")),
    (_RECONCILER, ("store", "sentinelKey")),
])
def test_the_consumer_preflight_looks_for_the_shipped_key(chart: Path, key_path: tuple) -> None:
    """What the container receives, not what values.yaml says — the two are
    joined by a template that could reference the wrong path and still render."""
    vals = _values(chart)
    for k in key_path:
        vals = vals[k]
    container = _preflight(chart)
    assert container is not None, f"{chart.name} renders no {_PREFLIGHT_CONTAINER}"
    assert _preflight_env(container)["SENTINEL_KEY"] == vals


@_needs_helm
@pytest.mark.parametrize("chart", [_GATEWAY, _RECONCILER])
def test_the_preflight_runs_as_a_pinned_non_root_uid(chart: Path) -> None:
    """busybox's image USER is root. A pod that asserts runAsNonRoot with no uid
    of its own — which federation-reconciler's does — rejects such a container
    with CreateContainerConfigError, i.e. the preflight would wedge every pod it
    was meant to protect. The uid is pinned per-container so neither chart
    depends on its podSecurityContext for this."""
    sc = _preflight(chart)["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert isinstance(sc.get("runAsUser"), int) and sc["runAsUser"] != 0


@_needs_helm
@pytest.mark.parametrize("chart", [_GATEWAY, _RECONCILER])
def test_mode_off_renders_no_init_container(chart: Path) -> None:
    assert _preflight(chart, "preflight.mode=off") is None


@pytest.mark.parametrize("chart,name", [
    (_GATEWAY, "federation-gateway"),
    (_RECONCILER, "federation-reconciler"),
])
def test_the_preflight_guard_is_reachable_from_a_render(chart: Path, name: str) -> None:
    """A `fail` nobody calls is not a guard. Static, so it holds without helm:
    the define has to be INCLUDED from something every render evaluates."""
    helpers = (chart / "templates/_helpers.tpl").read_text(encoding="utf-8")
    assert f'define "{name}.validatePreflight"' in helpers
    included = helpers + "".join(
        p.read_text(encoding="utf-8") for p in (chart / "templates").glob("*.yaml"))
    assert f'include "{name}.validatePreflight"' in included, (
        "the preflight guard is defined but never included — it would never run"
    )


@_needs_helm
@pytest.mark.parametrize("chart,key_path", [
    (_GATEWAY, "revokedSet.sentinelKey"),
    (_RECONCILER, "store.sentinelKey"),
])
def test_a_consumer_refuses_an_empty_sentinel_key(chart: Path, key_path: str) -> None:
    """⛔ REPRODUCED, not theorised. The check is `[ -e "$STORE_DIR/$SENTINEL_KEY" ]`;
    with an empty key that tests the mount DIRECTORY, which always exists — so the
    preflight exits 0 on every pod in every mode, including on a mount that
    resolved to nothing. `set -u` does not catch it either (the variable is set,
    just empty). The whole feature would be disarmed with no signal anywhere."""
    err = _render_fails(chart, f"{key_path}=")
    assert "must not be empty" in err, err[-400:]


@_needs_helm
@pytest.mark.parametrize("chart", [_GATEWAY, _RECONCILER])
@pytest.mark.parametrize("bad,expected", [
    ("enforced", "preflight.mode must be"),
    ("Enforce", "preflight.mode must be"),
    ("on", "preflight.mode must be"),
    # `--set x=true` is typed by Helm, so this one lands on the BOOLEAN branch
    # rather than the enum branch — a distinct message on purpose, because the
    # fix is different (quote it, not "pick another word").
    ("true", "arrived as a BOOLEAN"),
])
def test_a_consumer_refuses_a_mode_outside_the_enum(chart: Path, bad: str, expected: str) -> None:
    """The script treats "not enforce" as warn, so a typo renders happily and
    silently downgrades an operator who asked to fail CLOSED into failing OPEN —
    the one direction this feature must never take by accident. `Enforce` is in
    the set on purpose: case-insensitivity is exactly the assumption that makes
    this mistake feel safe."""
    err = _render_fails(chart, f"preflight.mode={bad}")
    assert expected in err, err[-400:]


@_needs_helm
@pytest.mark.parametrize("chart", [_GATEWAY, _RECONCILER])
def test_a_consumer_names_the_yaml_11_trap_for_an_unquoted_off(chart: Path, tmp_path) -> None:
    """⛔ MEASURED. Helm parses values FILES as YAML 1.1, where a bare `off` is
    the boolean false — so the documented way to disable this feature, written
    the way anyone would write it, does not render. Failing is the right
    direction; failing with `got %!q(bool=false)` is not, because the operator's
    file says `off` and the error says nothing about quoting."""
    vf = tmp_path / "v.yaml"
    vf.write_text("preflight:\n  mode: off\n", encoding="utf-8")
    res = subprocess.run(
        ["helm", "template", "rel", str(chart), "-f", str(vf)],
        capture_output=True, text=True, timeout=90)
    assert res.returncode != 0, "an unquoted `off` rendered — it is the boolean false, not the mode"
    assert "BOOLEAN" in res.stderr and "Quote it" in res.stderr, res.stderr[-400:]
    # ...and the quoted form must work, or the advice in the error is wrong.
    vf.write_text('preflight:\n  mode: "off"\n', encoding="utf-8")
    ok = subprocess.run(
        ["helm", "template", "rel", str(chart), "-f", str(vf)],
        capture_output=True, text=True, timeout=90)
    assert ok.returncode == 0, ok.stderr[-400:]


@_needs_sh
def test_the_script_itself_refuses_an_empty_sentinel_key(tmp_path) -> None:
    """Backstop for a hand-edited manifest, and the reason the template guard
    above is not the only line of defence. Distinct from the render guard: this
    one proves the SCRIPT does not silently pass, which is what would actually
    happen in the cluster if the manifest were edited after rendering."""
    key = _values(_PRODUCER)["federation"]["store"]["sentinelKey"]
    d = _mount(tmp_path, sentinel=False, payload=False, sentinel_key=key)
    res = _run(d, "warn", "")
    assert res.returncode != 0, (
        "an empty sentinel key passed silently — the check would be a no-op on "
        "every pod"
    )


@_needs_helm
@pytest.mark.parametrize("chart,cm_path", [
    (_GATEWAY, ("revokedSet", "configMapName")),
    (_RECONCILER, ("store", "configMapName")),
])
def test_the_preflight_inspects_the_same_volume_enforcement_reads(chart: Path, cm_path: tuple) -> None:
    """If the preflight ever mounted a second, separately-named volume it could
    pass against one object while the workload read another."""
    vals = _values(chart)
    for k in cm_path:
        vals = vals[k]
    for doc in _render(chart):
        if doc.get("kind") != "Deployment":
            continue
        spec = doc["spec"]["template"]["spec"]
        pre = [c for c in spec["initContainers"] if c["name"] == _PREFLIGHT_CONTAINER][0]
        mount = [m for m in pre["volumeMounts"] if m["mountPath"] == "/etc/revoked"][0]
        vol = [v for v in spec["volumes"] if v["name"] == mount["name"]][0]
        assert vol["configMap"]["name"] == vals
        assert vol["configMap"]["optional"] is True, (
            "optional:true is deliberate — see the volume's comment; flipping it "
            "moves the failure to the kubelet, where it surfaces as "
            "ContainerCreating, which this platform's stuck-pod alerting excludes"
        )
        return
    raise AssertionError("no Deployment rendered")


@_needs_helm
def test_the_reconciler_projects_the_sentinel_at_all() -> None:
    """⛔ THE REGRESSION THIS ASSERTION EXISTS FOR. The reconciler's volume uses
    `items:`, which is an ALLOWLIST. Drop the sentinel from it and the mount
    carries revoked.txt only — the preflight then never finds the sentinel, and
    its three-state check degrades into "does revoked.txt exist?", the exact
    test #1316 rejected because a fresh store fails it too. Nothing else would
    fail: the render succeeds, the pod starts, and every fresh install reports
    its store as missing."""
    sentinel = _values(_RECONCILER)["store"]["sentinelKey"]
    for doc in _render(_RECONCILER):
        if doc.get("kind") != "Deployment":
            continue
        vol = [v for v in doc["spec"]["template"]["spec"]["volumes"] if v["name"] == "revoked"][0]
        keys = {i["key"] for i in vol["configMap"]["items"]}
        assert sentinel in keys, (
            f"the sentinel is not in the reconciler's items: allowlist ({sorted(keys)}) "
            f"— it would never be projected and the preflight would report every "
            f"healthy store as missing"
        )
        return
    raise AssertionError("no Deployment rendered")


# ── behavioural layer: the four states the acceptance criteria name ──────────
def _mount(tmp_path: Path, *, sentinel: bool, payload: bool, sentinel_key: str) -> Path:
    """A directory shaped like a projected ConfigMap mount.

    ⛔ `..data` and the timestamped payload dir are ALWAYS present, even when the
    ConfigMap does not exist — measured on a real cluster. That is why the
    script cannot use `ls -A` to decide emptiness, and why they are here.
    """
    d = tmp_path / "etc-revoked"
    payload_dir = d / "..2026_08_02_00_00_00.000000000"
    payload_dir.mkdir(parents=True)
    (d / "..data").mkdir()
    if sentinel:
        (payload_dir / sentinel_key).write_text("release=r\n", encoding="utf-8")
        (d / sentinel_key).write_text("release=r\n", encoding="utf-8")
    if payload:
        (payload_dir / "revoked.txt").write_text("ftk_dead\n", encoding="utf-8")
        (d / "revoked.txt").write_text("ftk_dead\n", encoding="utf-8")
    return d


def _shipped_script() -> str:
    text = (_GATEWAY / "templates/deployment.yaml").read_text(encoding="utf-8")
    marker = "              set -eu\n"
    body = marker + text.split(marker, 1)[1].split("          volumeMounts:", 1)[0]
    return "\n".join(ln[14:] if ln.startswith(" " * 14) else ln for ln in body.splitlines())


def _run(store_dir: Path, mode: str, sentinel_key: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_SH, "-c", _shipped_script()],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "STORE_DIR": str(store_dir),
            "SENTINEL_KEY": sentinel_key,
            "STORE_CM": "tenant-federation-store",
            "PREFLIGHT_MODE": mode,
            "POD_NAMESPACE": "monitoring",
        },
    )


@_needs_sh
@pytest.mark.parametrize("mode", ["warn", "enforce"])
@pytest.mark.parametrize("sentinel,payload,label", [
    (True, True, "correct topology"),
    (True, False, "fresh install — sentinel present, nothing revoked yet"),
])
def test_a_resolved_mount_starts_silently_in_every_mode(tmp_path, mode, sentinel, payload, label) -> None:
    """Two of the four acceptance states. `enforce` is parametrised in
    deliberately: a check that only ever passes in warn mode would satisfy the
    other tests here while making enforce unusable."""
    key = _values(_PRODUCER)["federation"]["store"]["sentinelKey"]
    res = _run(_mount(tmp_path, sentinel=sentinel, payload=payload, sentinel_key=key), mode, key)
    assert res.returncode == 0, f"{label}: exit {res.returncode}, stdout={res.stdout!r}"
    assert res.stdout.strip() == "", f"{label}: expected silence, got {res.stdout!r}"


@_needs_sh
@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_the_upgrade_transition_starts_and_says_so(tmp_path, mode) -> None:
    """Third acceptance state: the store predates the chart revision that writes
    the sentinel. ⛔ It must start EVEN IN ENFORCE — the producer and the
    consumers are separate releases on separate version lines, so nothing orders
    their upgrades, and blocking here would make the upgrade order itself an
    outage."""
    key = _values(_PRODUCER)["federation"]["store"]["sentinelKey"]
    res = _run(_mount(tmp_path, sentinel=False, payload=True, sentinel_key=key), mode, key)
    assert res.returncode == 0, f"enforce blocked a legacy store: {res.stdout!r}"
    assert "store mount preflight" in res.stdout
    assert _python_const("GATEWAY_MISSING_PHRASE") not in res.stdout, (
        "a legacy store is still ENFORCING a set — reporting it as 'missing' "
        "would tell the incident responder the opposite of the truth"
    )


@_needs_sh
def test_an_empty_mount_reports_and_only_enforce_blocks(tmp_path) -> None:
    """Fourth acceptance state, both dispositions. The message must carry the
    phrase the alert queries AND name the namespace, because "which namespace"
    is the entire fix."""
    key = _values(_PRODUCER)["federation"]["store"]["sentinelKey"]
    d = _mount(tmp_path, sentinel=False, payload=False, sentinel_key=key)

    warn = _run(d, "warn", key)
    assert warn.returncode == 0
    assert _python_const("GATEWAY_MISSING_PHRASE") in warn.stdout
    assert "monitoring" in warn.stdout, "the message does not name the namespace to fix"

    enforce = _run(d, "enforce", key)
    assert enforce.returncode != 0, "enforce did not block an empty mount"
    assert _python_const("GATEWAY_MISSING_PHRASE") in enforce.stdout


@_needs_sh
def test_the_empty_mount_message_does_not_assert_a_cause_it_cannot_know(tmp_path) -> None:
    """⛔ THE ONE BOTH BLIND REVIEWERS FOUND INDEPENDENTLY.

    "the ConfigMap does not exist" and "it exists but has no keys yet" project
    to the SAME empty directory — measured on a real cluster, and the reason
    this file's fixtures cannot tell them apart either. The second is a
    perfectly healthy state: a tenant-api chart older than the one that writes
    the sentinel creates the ConfigMap with no `data` at all, and the runtime
    keys only appear on the first token write. So a message that states the
    ConfigMap is missing is a page that asserts something false about a working
    deployment — and the runbook tells the responder to act on it.

    This pins the wording as a contract: both causes named, plus the kubectl
    that actually distinguishes them.
    """
    key = _values(_PRODUCER)["federation"]["store"]["sentinelKey"]
    out = _run(_mount(tmp_path, sentinel=False, payload=False, sentinel_key=key), "warn", key).stdout
    assert "INDISTINGUISHABLE" in out, "the message reads as if the cause were known"
    assert "kubectl" in out and "get configmap" in out, (
        "the message does not tell the responder how to tell the two causes apart"
    )
    assert "2.9.20" in out, "the benign cause (a producer chart that predates the sentinel) is not named"


@_needs_sh
def test_ls_dash_A_would_not_have_worked(tmp_path) -> None:
    """The measurement that shaped the script, kept as a test so nobody
    'simplifies' the emptiness check back into `ls -A`. A missing ConfigMap
    still projects `..data` and a timestamped dir, so `ls -A` is non-empty for
    every one of the four states — an `ls -A` implementation would classify a
    completely absent store as 'legacy, starting anyway' and never fire."""
    key = _values(_PRODUCER)["federation"]["store"]["sentinelKey"]
    d = _mount(tmp_path, sentinel=False, payload=False, sentinel_key=key)
    assert sorted(p.name for p in d.iterdir()) == ["..2026_08_02_00_00_00.000000000", "..data"]
    plain = [p.name for p in d.iterdir() if not p.name.startswith(".")]
    assert plain == [], "the fixture no longer models an empty projected mount"
