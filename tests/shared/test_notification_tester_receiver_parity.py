"""notification_tester tests the receivers the route generator emits (#2752).

#2115 ruling (c): a tenant's `_routing` is what the route generator
(`generate_alertmanager_routes`) resolves — `_routing_defaults` chain, routing
profile, platform `tenants:` overlay, tenant files in subdirectories. Before
#2752, `notification_tester` read `_routing` itself and found receivers only
in root tenant files: on the platform-overlay, subdirectory and
`_routing_defaults` trees below it reported "no receivers" at rc 0 while the
generator rendered one.

Each row writes a conf.d tree, runs BOTH tools as subprocesses, and compares
what they say about receivers:

* the generator (render mode, `--dry-run`): the `receivers:` of its fragment
  — how many, and every `http(s)://` endpoint they carry;
* notification_tester (`--dry-run --json`): `summary.total_receivers` and the
  `url_tested` of every receiver.

A refused row is a tree the generator refuses in every mode: the generator
exits non-zero, and notification_tester must exit 2 with no receivers —
not report an empty list at rc 0.

Neither side is computed from the other's source: the expectation column
(`n`, `urls`) is hand-written per row, and BOTH tools must equal it.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "tools" / "ops"
GEN = OPS / "generate_alertmanager_routes.py"
NT = OPS / "notification_tester.py"
_TIMEOUT = 60

_DEFAULTS = "defaults:\n  mysql_connections: 80\n"
_HOOK = ("    _routing:\n      receiver:\n        type: webhook\n"
         "        url: \"https://hooks.example.com/x\"\n")

# (id, {relative path: content}, expected receiver count, expected endpoints)
_ROWS = [
    ("tenant_file", {
        "_defaults.yaml": _DEFAULTS,
        "tx.yaml": "tenants:\n  tx:\n" + _HOOK,
    }, 1, {"https://hooks.example.com/x"}),
    ("platform_overlay", {
        "_defaults.yaml": _DEFAULTS,
        "_platform.yaml": "tenants:\n  tx:\n" + _HOOK,
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections: \"5\"\n",
    }, 1, {"https://hooks.example.com/x"}),
    ("subdirectory", {
        "_defaults.yaml": _DEFAULTS,
        "team/tx.yaml": "tenants:\n  tx:\n" + _HOOK,
    }, 1, {"https://hooks.example.com/x"}),
    ("routing_defaults", {
        "_defaults.yaml": _DEFAULTS + (
            "_routing_defaults:\n  receiver:\n    type: webhook\n"
            "    url: \"https://hooks.example.com/d\"\n"),
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections: \"5\"\n",
    }, 1, {"https://hooks.example.com/d"}),
    ("routing_profile_with_route", {
        "_defaults.yaml": _DEFAULTS,
        "_routing_profiles.yaml": (
            "routing_profiles:\n  team-a:\n    receiver:\n      type: webhook\n"
            "      url: \"https://hooks.example.com/p\"\n    routes:\n"
            "      - match: { severity: critical }\n        receiver:\n"
            "          type: webhook\n          url: \"https://hooks.example.com/pc\"\n"),
        "tx.yaml": "tenants:\n  tx:\n    _routing_profile: team-a\n",
    }, 2, {"https://hooks.example.com/p", "https://hooks.example.com/pc"}),
]

_REFUSED_ROWS = [
    ("duplicate_tenant", {
        "_defaults.yaml": _DEFAULTS,
        "tx.yaml": "tenants:\n  tx:\n" + _HOOK,
        "tx2.yaml": "tenants:\n  tx:\n" + _HOOK,
    }),
    ("unreadable_tenant_file", {
        "_defaults.yaml": _DEFAULTS,
        "tx.yaml": "tenants:\n  tx:\n" + _HOOK,
        "alpha.yaml": b"tenants:\n  ty:\n    a: \"\xff\"\n",
    }),
]


def _write(root: Path, files: dict) -> Path:
    d = root / "conf.d"
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    return d


def _run(script: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(script), *argv],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=_TIMEOUT)


def _endpoints(obj: object) -> list[str]:
    if isinstance(obj, dict):
        return [u for v in obj.values() for u in _endpoints(v)]
    if isinstance(obj, list):
        return [u for v in obj for u in _endpoints(v)]
    if isinstance(obj, str) and obj.startswith(("http://", "https://")):
        return [obj]
    return []


def _generator_receivers(confd: Path) -> tuple[int, set[str]]:
    p = _run(GEN, "--config-dir", str(confd), "--dry-run")
    assert p.returncode == 0, p.stderr[-800:]
    body = p.stdout.split("--- DRY RUN OUTPUT ---", 1)[1]
    body = body.rsplit("\n--- ", 1)[0]
    receivers = (yaml.safe_load(body) or {}).get("receivers") or []
    return len(receivers), set(_endpoints(receivers))


def _tester_receivers(confd: Path) -> tuple[int, set[str]]:
    p = _run(NT, "--config-dir", str(confd), "--dry-run", "--json")
    assert p.returncode == 0, p.stderr[-800:]
    doc = json.loads(p.stdout)
    urls = {r["url_tested"] for t in doc["tenants"] for r in t["receivers"]}
    return doc["summary"]["total_receivers"], urls


@pytest.mark.parametrize("name, files, n, urls", _ROWS, ids=[r[0] for r in _ROWS])
def test_tester_finds_the_receivers_the_generator_emits(tmp_path, name, files, n, urls):
    confd = _write(tmp_path, files)
    gen = _generator_receivers(confd)
    assert gen == (n, urls), f"{name}: the row's expectation no longer matches the generator"
    assert _tester_receivers(confd) == gen, (
        f"{name}: notification_tester must test exactly the receivers the "
        "route generator emits (#2752)")


@pytest.mark.parametrize("name, files", _REFUSED_ROWS, ids=[r[0] for r in _REFUSED_ROWS])
def test_a_tree_the_generator_refuses_is_refused_not_empty(tmp_path, name, files):
    confd = _write(tmp_path, files)
    gen = _run(GEN, "--config-dir", str(confd), "--dry-run")
    assert gen.returncode != 0, f"{name}: the row no longer exercises a generator refusal"
    p = _run(NT, "--config-dir", str(confd), "--dry-run", "--json")
    assert p.returncode == 2, (p.returncode, p.stderr[-800:])
    assert "route generator refuses this tree" in p.stderr, p.stderr[-800:]
    doc = json.loads(p.stdout)
    assert doc["status"] == "caller_error"
    assert doc["reason"] == "routing_tree_refused"
    assert doc["tenants"] == [] and doc["summary"]["total_receivers"] == 0
