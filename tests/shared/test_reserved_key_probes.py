"""Black-box probes: does the reserved-key registry say what the readers do?

components/threshold-exporter/app/pkg/config/reserved_keys.json declares, for
each top-level `_` key, which reader reads it in which layer (ADR-038
decision 2). This file measures it (decision 3): for every key and layer it
writes a minimal conf.d tree with the key in that layer and runs the readers.
Both ways, for every reader whose output can be compared (the exporter via
`da-guard served-values`, the route generator, the custom-alert compiler,
Policy-as-Code):

- a reader the registry says reads the key there must change its output —
  catches "registered, but the code no longer reads it";
- a reader the registry does NOT name for the key there must not — catches
  "the code reads it, the registry does not say so", which would make a key
  look read by nothing.

A third check ties served-values' per-key `keys` (ADR-038 decision 5) to the
registry: for the probed key it must say the exporter reads it exactly where
the registry declares the exporter does.

Policy-as-Code also reads any key a deployment's rule names as `target`;
the probe trees hold no rule naming the probed key, so for it the second
check only shows that it reads no key on its own (ADR-038 decision 3).

Not measured, and declared in the registry instead: the `annotation` reader
(`_namespaces`; nothing on the alert path reads it), and a read that changes
no output (the route generator reads `_metadata` and discards it, #3043).
Only the outputs are compared, never how a reader loads its input, so a
reader rewritten to load differently needs no change here.
"""
import functools
import json
import os
import subprocess
import sys
import textwrap

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REGISTRY = os.path.join(_REPO, "components", "threshold-exporter", "app", "pkg",
                         "config", "reserved_keys.json")
_GEN = os.path.join(_REPO, "scripts", "tools", "ops", "generate_alertmanager_routes.py")
_CAC = os.path.join(_REPO, "scripts", "tools", "dx", "compile_custom_alerts.py")
_POL = os.path.join(_REPO, "scripts", "tools", "ops", "policy_engine.py")

with open(_REGISTRY, encoding="utf-8") as _f:
    _REG = json.load(_f)

_LAYERS = list(_REG["layers"])

# One sentinel value per registered key (a prefix: one key under it), each a
# value its reader accepts, so the reader has something to act on. A new
# registry key without a row here fails test_every_registered_key_has_a_sentinel.
_CUSTOM_ALERT = textwrap.dedent("""\
    - recipe: threshold
      name: probe_conns_high
      metric: mysql_global_status_threads_connected
      op: ">"
      window: 5m
      threshold: "150:warning"
      mode: page
    """)
_SENTINELS = {
    "_silent_mode": ("_silent_mode", "warning"),
    "_severity_dedup": ("_severity_dedup", "disable"),
    "_namespaces": ("_namespaces", "[probe-ns]"),
    "_metadata": ("_metadata", "{owner: probe}"),
    "_profile": ("_profile", "p9"),
    "_routing": ("_routing", "{receiver: {type: webhook, url: 'https://w.example/probe'}}"),
    "_routing_profile": ("_routing_profile", "rp1"),
    "_custom_alerts": ("_custom_alerts", "\n" + _CUSTOM_ALERT),
    "_routing_defaults": ("_routing_defaults",
                          "{receiver: {type: webhook, url: 'https://w.example/rd'}}"),
    "_routing_enforced": ("_routing_enforced",
                          "{enabled: true, receiver: {type: webhook, url: 'https://w.example/noc'}}"),
    "_policies": ("_policies",
                  "[{name: probe, target: owner_tag, operator: required, severity: error}]"),
    # `_state_<filter>` for the filter the probe tree declares (on by default).
    "_state_": ("_state_maint", "disable"),
}

pytestmark = pytest.mark.skipif(
    not os.path.isfile(_REGISTRY), reason="the registry is not in this checkout")


def _entry(key, value, indent):
    pad = " " * indent
    if value.startswith("\n"):
        return f"{pad}{key}:" + textwrap.indent(value, pad + "  ")
    return f"{pad}{key}: {value}\n"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _tree(d, name, layer, routed):
    """A tree with one tenant, `t2`, in a subdirectory (so a subtree
    `_defaults.yaml` reaches it), naming threshold profile p1. *layer* None
    is the baseline: the same tree without the probed key."""
    key, value = _SENTINELS[name]
    put = (lambda here, indent: _entry(key, value, indent) if layer == here else "")
    _write(os.path.join(d, "_defaults.yaml"),
           "defaults:\n  cpu: 80\n  mem: 60\n"
           "state_filters:\n  maint:\n    reasons: [X]\n    severity: critical\n"
           + put("platform_top", 0))
    _write(os.path.join(d, "_profiles.yaml"),
           "profiles:\n  p9:\n    mem: 33\n  p1:\n    mem: 75\n" + put("profile", 4))
    _write(os.path.join(d, "_routing_profiles.yaml"),
           "routing_profiles:\n  rp1:\n    receiver: {type: webhook, url: 'https://w.example/rp'}\n")
    _write(os.path.join(d, "team", "_defaults.yaml"),
           "defaults:\n  cpu: 81\n" + put("subtree_defaults", 2))
    if layer == "platform_tenants":
        _write(os.path.join(d, "_platform.yaml"), "tenants:\n  t2:\n" + put("platform_tenants", 4))
    body = "    cpu: 70\n"
    if key != "_profile":  # the probe of `_profile` itself starts from none
        body += "    _profile: p1\n"
    if routed and not (key == "_routing" and layer == "tenant"):
        body += "    _routing: {receiver: {type: webhook, url: 'https://w.example/base'}}\n"
    _write(os.path.join(d, "team", "t2.yaml"), "tenants:\n  t2:\n" + body + put("tenant", 4))


def _run(cmd, env):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       timeout=180, env=env)
    return p


class _Readers:
    def __init__(self, guard, tmp):
        self.guard = guard
        self.tmp = tmp
        self.env = dict(os.environ, DA_GUARD_BINARY=guard)

    def exporter(self, d):
        p = _run([self.guard, "served-values", "--config-dir", d], self.env)
        assert p.returncode == 0, f"served-values rc {p.returncode} on {d}:\n{p.stderr[-800:]}"
        t = json.loads(p.stdout)["tenants"]["t2"]
        # `routing` is left out: it is the exporter module's reading of a key
        # the collector does not read (the route generator's).
        return {k: t[k] for k in ("values", "series", "severities", "state_filters", "time_boxes")}

    def exporter_keys(self, d):
        """served-values' per-key verdict for t2 (ADR-038 decision 5)."""
        p = _run([self.guard, "served-values", "--config-dir", d], self.env)
        assert p.returncode == 0, f"served-values rc {p.returncode} on {d}:\n{p.stderr[-800:]}"
        return json.loads(p.stdout)["tenants"]["t2"]["keys"]

    def _file_out(self, cmd, d, out, ok):
        """(rc, the file written) — rc is part of the output: the route
        generator answers 1 for "No valid routes or inhibit rules generated"."""
        p = _run(cmd, self.env)
        assert p.returncode in ok, f"{os.path.basename(cmd[1])} rc {p.returncode} on {d}:\n" \
                                   f"{p.stdout[-400:]}{p.stderr[-800:]}"
        body = ""
        if os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                body = f.read()
            os.remove(out)
        return (p.returncode, body)

    def generator(self, d):
        out = d + ".routes.yaml"
        return self._file_out([sys.executable, _GEN, "--config-dir", d, "-o", out], d, out,
                              ok=(0, 1))

    def custom_alert_compiler(self, d):
        out = d + ".pack.yaml"
        return self._file_out([sys.executable, _CAC, "--config-dir", d, "--out", out,
                               "--allow-empty"], d, out, ok=(0,))

    def policy_engine(self, d):
        p = _run([sys.executable, _POL, "--config-dir", d, "--json"], self.env)
        assert p.returncode in (0, 1), f"policy_engine rc {p.returncode}:\n{p.stderr[-800:]}"
        return (p.returncode, p.stdout)


@pytest.fixture(scope="module")
def readers(da_guard_binary, tmp_path_factory):
    return _Readers(da_guard_binary, str(tmp_path_factory.mktemp("probes")))


def _registered():
    """(name, read_by) for every key and every prefix something reads."""
    out = [(k, e["read_by"]) for k, e in _REG["keys"].items()]
    out += [(p, e["read_by"]) for p, e in _REG["prefixes"].items() if e["read_by"]]
    return out


def test_every_registered_key_has_a_sentinel():
    names = {n for n, _ in _registered()}
    assert names == set(_SENTINELS), (
        f"add a sentinel for {sorted(names - set(_SENTINELS))}, "
        f"drop {sorted(set(_SENTINELS) - names)}")


@functools.lru_cache(maxsize=None)
def _baseline(readers, name, reader, routed):
    d = os.path.join(readers.tmp, f"base-{name}-{reader}-{int(routed)}")
    _tree(d, name, None, routed)
    return getattr(readers, reader)(d)


def _probe(readers, name, layer, reader, routed):
    d = os.path.join(readers.tmp, f"probe-{name}-{layer}-{reader}-{int(routed)}")
    _tree(d, name, layer, routed)
    return getattr(readers, reader)(d)


_CASES = [(name, layer) for name, _ in _registered() for layer in _LAYERS]


@pytest.mark.parametrize("name,layer", _CASES, ids=[f"{n}@{l}" for n, l in _CASES])
def test_a_declared_reader_changes_its_output(readers, name, layer):
    read_by = dict(_registered())[name]
    declared = [r for r, layers in read_by.items() if layer in layers and r != "annotation"]
    if not declared:
        pytest.skip(f"no reader declared for {name} in {layer}")
    for reader in declared:
        # The route generator renders a tenant's own `_routing`; some keys act
        # only with it, others only without it — so either variant counts.
        variants = (False, True) if reader == "generator" else (False,)
        changed = any(_probe(readers, name, layer, reader, routed)
                      != _baseline(readers, name, reader, routed) for routed in variants)
        assert changed, (
            f"reserved_keys.json says {reader} reads {name} in {layer}, but its output "
            f"did not change when the probe wrote it there: fix the registry or the reader")


_MEASURED = ("exporter", "generator", "custom_alert_compiler", "policy_engine")


@pytest.mark.parametrize("name,layer", _CASES, ids=[f"{n}@{l}" for n, l in _CASES])
def test_an_undeclared_reader_does_not_change_its_output(readers, name, layer):
    """The other way round: a reader NOT declared for this key in this layer
    must not act on it. Otherwise the registry under-declares, and a verdict
    built on it calls a key nothing reads when a reader does."""
    read_by = dict(_registered())[name]
    undeclared = [r for r in _MEASURED if layer not in read_by.get(r, [])]
    for reader in undeclared:
        # Whole output, not "is the key in values": served-values lists every
        # tenant's `_metadata`, `_severity_dedup`, `_silent_mode` and
        # `_state_<filter>` whether or not anything set them.
        variants = (False, True) if reader == "generator" else (False,)
        for routed in variants:
            assert _probe(readers, name, layer, reader, routed) == \
                _baseline(readers, name, reader, routed), (
                    f"{reader}'s output changed with {_SENTINELS[name][0]} in {layer}, but "
                    f"reserved_keys.json does not say it reads it there: add the layer to "
                    f"read_by.{reader}, or stop {reader} reading it")


def test_a_retired_key_changes_no_exporter_output(readers, tmp_path):
    for name in sorted(_REG["retired"]):
        _SENTINELS[name] = (name, "x")
        try:
            for layer in _LAYERS:
                base, probe = str(tmp_path / f"b-{layer}"), str(tmp_path / f"p-{layer}")
                _tree(base, name, None, False)
                _tree(probe, name, layer, False)
                assert readers.exporter(probe) == readers.exporter(base), (name, layer)
        finally:
            del _SENTINELS[name]


@pytest.mark.parametrize("name,layer", _CASES, ids=[f"{n}@{l}" for n, l in _CASES])
def test_served_values_keys_agree_with_the_registry(readers, name, layer):
    """served-values' `keys` says whether the exporter reads each key of a
    tenant's merged config. For the probed key it must say what the
    registry declares for that layer — the same declaration the two tests
    above measure — so a reader of `keys` and a reader of the registry
    cannot disagree."""
    key = _SENTINELS[name][0]
    d = os.path.join(readers.tmp, f"keys-{name}-{layer}")
    _tree(d, name, layer, False)
    entry = readers.exporter_keys(d).get(key)
    declared = layer in dict(_registered())[name].get("exporter", [])
    if entry is None:
        # Not in the tenant's merged config: the exporter's build did not
        # take it in, so it cannot be one the registry says it reads.
        assert not declared, (
            f"reserved_keys.json says the exporter reads {key} in {layer}, but served-values "
            f"has no keys entry for it")
        return
    assert entry["exporter"] == declared, (
        f"served-values keys[{key}] = {entry} with {key} in {layer}; reserved_keys.json says "
        f"the exporter {'reads' if declared else 'does not read'} it there")
    assert entry.get("registered") is True, entry
