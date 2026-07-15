"""`--json` stdout contract gate for da-tools ops CLI tools (#1112).

THE CONTRACT (strict)
---------------------
When a tool is invoked with ``--json`` (or its ``--json-output`` spelling),
**stdout MUST contain exactly one JSON document — nothing else** — on *every*
terminal path the tool can reach: the happy path, an empty/no-op result, a
graceful skip (``--skip-if-unavailable``), a ``--dry-run``, a ``--checklist-only``
early return, a detected-violation exit, and a "wrote the report to a file"
path.  Every human-readable message (progress lines, summaries, warnings,
"Prometheus unavailable — skipping", "No tenant configs found", …) belongs on
**stderr**.

The observable form of the contract is exactly one line:

    json.loads(stdout)   # must succeed on the FULL stdout text

Full-text ``json.loads`` is deliberate: it simultaneously proves (a) valid JSON
and (b) no leading/trailing prose.  We never scan for "the first ``{``" — a
lenient parse would let prose-contaminated stdout pass, which is the very bug
this gate exists to catch.

WHY THIS FILE EXISTS
--------------------
``docs/internal/dev-rules.md`` §13 already mandates the ``--json`` +
``da-tools <cmd> --json | jq`` idiom, and the *exit-code* half of that rule is
codified in ``tests/shared/test_tool_exit_codes.py``.  The ``--json`` half was
never codified, so it rotted.  This file is that gate; its harness shape
(auto-collect + parametrize + named-tool failure messages) is copied from
``test_tool_exit_codes.py``.

SCOPE — WHAT THIS GATE ASSERTS
------------------------------
* All 37 in-scope tools: 33 spell the flag ``--json``, 4 spell it
  ``--json-output`` (``blind_spot_discovery`` / ``config_diff`` /
  ``cutover_tenant`` / ``maintenance_scheduler``).  Same contract, both spellings.
  ``test_recipe_table_covers_every_json_tool`` fails if a new tool grows a JSON
  flag without getting a recipe here, so the scope cannot silently rot.
* Multi-mode tools get one recipe **per distinct terminal path**, not one per
  tool — the known regressions all live in the non-default modes (skip / dry-run
  / early-return / write-to-file branches).
* **Mode flags are also driven in COMBINATION, not just in isolation.**  The
  first version of this table treated the modes of a tool as if they were
  mutually exclusive.  They are not — argparse happily accepts
  ``--checklist-only --dry-run`` — and ``migrate_to_operator`` combined them
  into a silent data-loss bug (below) that sailed straight through this gate.
  Any two orthogonal flags that each select a *payload* now get a combination
  recipe.
* **``doc_check``: one valid document is necessary, not sufficient.**
  ``json.loads(stdout)`` proves the tool emitted *a* well-formed document; it
  cannot prove it emitted the *right* one.  ``migrate_to_operator
  --checklist-only --dry-run --json`` served a perfectly valid *empty CRD
  preview* with no ``checklist`` field at all — the caller asked for a checklist
  and silently got something else, and this gate was green the whole time.
  Recipes whose mode identity is observable in the payload (a discriminator
  field, a must-not-be-empty field, a precedence between two flags) now carry a
  ``doc_check`` that asserts it.
* **One recipe asserts an exit code instead of a document**: ``patch_config[apply]``
  (``--json`` without ``--diff``). That combination is contradictory on a
  mutating tool, so the contract there is "reject it" (exit 2, empty stdout),
  not "emit a document" — the full rationale is inline at the recipe.

HOW THE MODES ARE DRIVEN (every dependency is mocked, none is contacted)
-----------------------------------------------------------------------
* *Prometheus / Alertmanager / OPA / Pushgateway / tenant-api* → a local stub
  HTTP server.  Its default payloads are the canonical *reachable-but-empty*
  ones (``result: []``, ``groups: []``, ``alerts: []``) — a legitimate production
  terminal path ("the server is up, nothing matched") and the one most likely to
  expose a "no data → print prose, skip the JSON" branch.  A ``/rich`` base-URL
  prefix serves non-empty payloads, so a tool that would otherwise early-return
  on no-data can also be driven down its happy path (``discover_instance_mappings``
  is gated on BOTH: the empty early-return *and* the rich happy path, because
  they print differently).
* *kubectl* → a fake ``kubectl`` on ``PATH`` (``fake_kubectl_dir``).  This is what
  makes the cluster-facing modes testable **and** safe: with the real binary
  shadowed there is no cluster to reach, so ``patch_config`` apply and
  ``cutover_tenant`` apply are exercised for real rather than skipped on danger
  grounds.

The HTTP stub is an *out-of-process server* rather than the in-process
``monkeypatch(_lib_python.http_get_json)`` seam used by
``tests/ops/test_check_alert.py``.  Reason: only 18 of the tools go through
``http_get_json``; the rest reach the network via ``urllib.urlopen`` or
``requests`` directly (``maintenance_scheduler``, ``notification_tester``,
``policy_opa_bridge``, and partially ``byo_check`` / ``federation_check`` /
``shadow_verify`` / ``threshold_govern`` / ``discover_instance_mappings``).  One
real socket covers every one of them uniformly, and subprocess execution gives us
the *actual, complete* stdout byte stream — which is the thing under test — with
no import-time module-namespace crosstalk between 37 tools.

SCOPE — WHAT THIS GATE DOES *NOT* ASSERT (honest boundaries)
------------------------------------------------------------
1. **Argparse-rejected invocations are out of scope.**  A bad/missing flag exits
   2 from argparse *before the tool's own logic runs*; stdout is empty and there
   is no terminal path to speak of.  That boundary is already gated by
   ``test_tool_exit_codes.py::test_invalid_args_exits_caller_error``.  The
   contract here covers paths the tool itself reaches after accepting its args.
2. **Nine recipes are skipped on a Windows host** — and only there.  Windows
   ``CreateProcess`` resolves a bare ``kubectl`` to ``kubectl.exe`` and ignores
   ``PATHEXT``, so the fake-kubectl shim is bypassed and the REAL kubectl would
   run.  Rather than let those recipes report a bogus "cluster unreachable"
   failure, they skip with that reason.  On POSIX (Linux CI, dev container) —
   where this gate is authoritative — **every recipe runs and nothing is skipped.**
3. **Data-shape coverage is one-deep.**  Each (tool, mode) is driven through its
   terminal path with *one* upstream payload shape.  A branch that only triggers
   on, say, a partially-converged shadow report or a specific drift class is not
   separately gated.  The contract is about *where the bytes go*, not about report
   content, so this is an accepted limit — but a tool could still own a
   prose-printing branch that no recipe here reaches.
4. **`--markdown` / `--export-patch` and other non-JSON output flags are not
   gated.**  They are a different contract; only the JSON flag is in scope.
5. **KNOWN, UNGATED CONTRADICTION — `threshold_recommend --export-patch --json`.**
   A flag-matrix sweep of all 37 tools (#1112) found exactly one place where a
   competing stdout-format flag *beats* `--json`: `--export-patch` wins and
   stdout carries a YAML patch fragment, so `json.loads(stdout)` fails and a
   caller's `... --export-patch --json | jq` breaks.  Every other tool that owns
   a competing format flag (`alert_correlate`, `alert_quality`,
   `cardinality_forecasting`, `drift_detect`, `config_diff`, and
   `threshold_recommend --markdown` itself) lets `--json` win, so this is an
   outlier, not a convention.  It is deliberately **not** gated yet: two output
   formats on one stdout is a genuine contradiction and the resolution
   (fail-loud `EXIT_CALLER_ERROR` like `patch_config[apply]`, or an argparse
   mutually-exclusive group) is a caller-facing behaviour decision for the
   owner, not something this gate should decide by fiat.  Once decided, add the
   recipe with `expect_caller_error=True`.

FAILURES ARE EXPECTED (that is the point)
-----------------------------------------
This gate lands RED.  Each red (tool, mode) is a violator for the follow-up fix
wave; the failure message names the tool, the mode, and the first 200 characters
of the offending stdout.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "tools" / "ops"

# ── Real in-repo fixtures (all asserted to exist by test_fixture_paths_exist) ──
SEED_CONF_D = REPO_ROOT / "try-local" / "seed" / "conf.d"
EXPORTER_CONF_D = REPO_ROOT / "components" / "threshold-exporter" / "config" / "conf.d"
RULE_PACKS = REPO_ROOT / "rule-packs"
K8S_MONITORING = REPO_ROOT / "k8s" / "03-monitoring"
DASHBOARD_JSON = K8S_MONITORING / "shadow-monitoring-dashboard.json"
ALERTMANAGER_YML = REPO_ROOT / "try-local" / "alertmanager.yml"

FIXTURE_PATHS = [
    SEED_CONF_D, EXPORTER_CONF_D, RULE_PACKS, K8S_MONITORING,
    DASHBOARD_JSON, ALERTMANAGER_YML,
]

TIMEOUT_S = 90

# scripts/tools/_lib_exitcodes.py is the SSOT (0 = OK, 1 = VIOLATION,
# 2 = CALLER_ERROR); test_tool_exit_codes.py pins those values.
EXIT_CALLER_ERROR = 2


# ═══════════════════════════════════════════════════════════════════════════
# Scope discovery — mirrors collect_tools() in test_tool_exit_codes.py
# ═══════════════════════════════════════════════════════════════════════════
JSON_FLAG_RE = re.compile(r'"--json(?:-output)?"')


def collect_json_tools() -> list[str]:
    """Every ops tool that declares a `--json` / `--json-output` flag."""
    out = []
    for f in sorted(OPS_DIR.glob("*.py")):
        if f.name.startswith("_") or f.name == "__init__.py":
            continue
        if JSON_FLAG_RE.search(f.read_text(encoding="utf-8")):
            out.append(f.stem)
    return out


JSON_TOOLS = collect_json_tools()


# ═══════════════════════════════════════════════════════════════════════════
# Stub server: Prometheus + Alertmanager + OPA + Pushgateway + tenant-api.
#
# Two personalities, selected by the BASE URL a recipe hands the tool:
#   http://host        → "reachable, but nothing matched"  (empty payloads)
#   http://host/rich   → non-empty payloads (drives happy paths past an
#                        early-return-on-no-data guard)
# ═══════════════════════════════════════════════════════════════════════════
_EMPTY_VECTOR = {"status": "success", "data": {"resultType": "vector", "result": []}}
_EMPTY_MATRIX = {"status": "success", "data": {"resultType": "matrix", "result": []}}

_RICH_VECTOR = {"status": "success", "data": {"resultType": "vector", "result": [
    {"metric": {"__name__": "up", "tenant": "tenant-one", "instance": "stub:9104",
                "job": "mysqld", "schema": "app"},
     "value": [1784034992.0, "1"]},
]}}
_RICH_MATRIX = {"status": "success", "data": {"resultType": "matrix", "result": [
    {"metric": {"tenant": "tenant-one", "instance": "stub:9104"},
     "values": [[1784034000.0, "10"], [1784037600.0, "12"]]},
]}}

_PLAIN_METRICS = (b"# HELP up Target up\n# TYPE up gauge\n"
                  b'mysql_up{instance="stub:9104"} 1\n')
# Carries `schema=` — one of discover_instance_mappings' PARTITION_LABEL_CANDIDATES.
_RICH_METRICS = (
    b"# HELP mysql_info Info\n# TYPE mysql_info gauge\n"
    b'mysql_info{instance="stub:9104",schema="app"} 1\n'
    b'mysql_info{instance="stub:9104",schema="reporting"} 1\n'
    b'mysql_info{instance="stub:9104",schema="billing"} 1\n'
)


def _stub_payload(path: str) -> tuple[int, bytes, str]:
    """(status, body, content_type) for a stubbed upstream path."""
    def j(obj):
        return 200, json.dumps(obj).encode(), "application/json"

    rich = path.startswith("/rich")
    if rich:
        path = path[len("/rich"):] or "/"

    if path.startswith("/api/v1/query_range"):
        return j(_RICH_MATRIX if rich else _EMPTY_MATRIX)
    if path.startswith("/api/v1/query"):
        return j(_RICH_VECTOR if rich else _EMPTY_VECTOR)
    if path.startswith("/api/v1/rules"):
        return j({"status": "success", "data": {"groups": []}})
    if path.startswith("/api/v1/alerts"):
        return j({"status": "success", "data": {"alerts": []}})
    if path.startswith("/api/v1/targets"):
        return j({"status": "success",
                  "data": {"activeTargets": [], "droppedTargets": []}})
    if path.startswith("/api/v1/label/"):
        # /api/v1/label/<name>/values
        name = path[len("/api/v1/label/"):].split("/")[0]
        if rich and name in ("schema", "database", "datname"):
            return j({"status": "success", "data": ["app", "reporting", "billing"]})
        return j({"status": "success", "data": []})
    if path.startswith("/api/v1/series"):
        if rich:
            return j({"status": "success", "data": [
                {"__name__": "mysql_info", "instance": "stub:9104", "schema": v}
                for v in ("app", "reporting", "billing", "audit")
            ]})
        return j({"status": "success", "data": []})
    if path.startswith("/api/v1/status/buildinfo"):
        return j({"status": "success", "data": {"version": "2.51.0"}})
    if path.startswith("/api/v1/status/config"):
        return j({"status": "success", "data": {"yaml": "global: {}\n"}})
    if path.startswith("/-/healthy") or path.startswith("/-/ready"):
        return 200, b"Healthy.\n", "text/plain"
    # Alertmanager v2
    if path.startswith("/api/v2/status"):
        return j({"cluster": {"status": "ready"},
                  "versionInfo": {"version": "0.27.0"},
                  "config": {"original": "route:\n  receiver: default\n"}})
    if path.startswith("/api/v2/alerts"):
        return j([])
    if path.startswith("/api/v2/silences"):
        return j([])
    # OPA REST
    if path.startswith("/v1/data"):
        return j({"result": {"violations": [], "allow": True}})
    # Pushgateway
    if path.startswith("/metrics/job"):
        return 200, b"", "text/plain"
    # tenant-api (threshold_govern --apply opens PRs through this)
    if path.startswith("/api/v1/tenants"):
        return j({"pr_url": "https://example.invalid/pr/1", "pr_number": 1,
                  "status": "open"})
    # Exporter /metrics scrape target (discover_instance_mappings --endpoint)
    if path.rstrip("/").endswith("/metrics"):
        body = _RICH_METRICS if rich else _PLAIN_METRICS
        return 200, body, "text/plain; version=0.0.4"
    return j({"status": "success", "data": {}})


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self):
        status, body, ctype = _stub_payload(self.path)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._respond()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._respond()

    def do_DELETE(self):  # noqa: N802
        self._respond()

    def log_message(self, *args):  # silence per-request stderr noise
        pass


@pytest.fixture(scope="session")
def stub_url():
    """Base URL of the stubbed Prometheus/Alertmanager/OPA/Pushgateway."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ═══════════════════════════════════════════════════════════════════════════
# Fake kubectl — shadows the real binary on PATH.
#
# Two jobs at once:
#   1. makes the cluster-facing modes *testable* with no cluster, and
#   2. makes the mutating modes (`patch_config` apply, `cutover_tenant` apply)
#      *safe* to exercise: with the real binary shadowed there is no cluster to
#      reach, so no ConfigMap can be written by a test run.
# ═══════════════════════════════════════════════════════════════════════════
_FAKE_KUBECTL_PY = r'''
import json, sys

argv = sys.argv[1:]

# `kubectl get configmap threshold-config -n monitoring -o json` — patch_config,
# batch_diagnose tenant auto-discovery.
if "get" in argv and "threshold-config" in argv:
    print(json.dumps({
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "threshold-config", "namespace": "monitoring"},
        "data": {
            "_defaults.yaml": (
                "defaults:\n  max_connections: '100'\n  slow_queries: '5'\n"
            ),
            "db-a.yaml": (
                "tenants:\n  db-a:\n    max_connections: '120'\n"
            ),
            "db-b.yaml": (
                "tenants:\n  db-b:\n    max_connections: '80'\n"
            ),
        },
    }))
    sys.exit(0)

if argv and argv[0] == "version":
    print(json.dumps({"clientVersion": {"gitVersion": "v1.29.0"}}))
    sys.exit(0)

# Any other structured read: an empty, well-formed List.
if "-o" in argv and "json" in argv:
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": []}))
    sys.exit(0)

# Writes (patch / apply / create / delete) and jsonpath reads: succeed silently.
sys.exit(0)
'''


@pytest.fixture(scope="session")
def fake_kubectl_dir(tmp_path_factory) -> Path:
    """A directory holding a fake `kubectl`, to be prepended to PATH.

    Ships both a POSIX launcher (extensionless, +x — Linux CI) and a `.bat`
    (Windows resolves it via PATHEXT), so the same fixture works on both.
    """
    d = tmp_path_factory.mktemp("fake_bin")
    impl = d / "fake_kubectl.py"
    impl.write_text(_FAKE_KUBECTL_PY, encoding="utf-8")

    posix = d / "kubectl"
    posix.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n',
        encoding="utf-8", newline="\n",
    )
    posix.chmod(0o755)

    (d / "kubectl.bat").write_text(
        f'@echo off\r\n"{sys.executable}" "{impl}" %*\r\n', encoding="utf-8",
    )
    return d


@pytest.fixture(scope="session")
def sandbox_repo(tmp_path_factory) -> Path:
    """A throwaway copy of `scripts/tools/` + `rule-packs/`.

    `threshold_recommend --generate-observed-map` writes to the *tracked* file
    `scripts/tools/ops/metric_observed_map.yaml` (path derived from `__file__`,
    with no CLI override).  Running it against the real tree would mutate the
    repo, so that one recipe runs from this copy instead.
    """
    root = tmp_path_factory.mktemp("sandbox_repo")
    shutil.copytree(REPO_ROOT / "scripts" / "tools", root / "scripts" / "tools")
    shutil.copytree(RULE_PACKS, root / "rule-packs")
    return root


# ═══════════════════════════════════════════════════════════════════════════
# Input-file builders (schemas reverse-engineered from each tool's loader)
# ═══════════════════════════════════════════════════════════════════════════
def _write(p: Path, text: str) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p)


def _readiness_json(tmp: Path) -> str:
    # Required fields per cutover_tenant.load_cutover_readiness(): omitting any
    # of them diverts the tool onto its `load_readiness` caller-error path.
    return _write(tmp / "cutover-readiness.json", json.dumps({
        "ready": True,
        "convergence_percentage": 100.0,
        "converged_count": 4,
        "total_pairs": 4,
        "timestamp": "2026-07-14T00:00:00Z",
        "zero_mismatch_days": 7,
        "tenants": {},
    }))


def _policy_file(tmp: Path) -> str:
    """A standalone Policy-as-Code doc in the shape `--policy` expects.

    NB the repo's `examples/_domain_policy.yaml` is a *different* schema
    (`domain_policies:`), so it cannot stand in here.
    """
    return _write(tmp / "policy.yaml", (
        "policies:\n"
        "  - name: metadata-owner-required\n"
        "    description: every tenant must declare an owner\n"
        "    target: _metadata.owner\n"
        "    operator: required\n"
        "    severity: error\n"
    ))


def _confd_with_webhook(tmp: Path, stub: str) -> str:
    """A conf.d whose only receiver is a webhook pointed at the stub server.

    Lets `notification_tester` run its LIVE send path without touching a real
    Slack/PagerDuty endpoint.
    """
    d = tmp / "confd_webhook"
    _write(d / "_defaults.yaml", "defaults:\n  mysql_connections: '100'\n")
    _write(d / "tenant-one.yaml",
           "tenants:\n"
           "  tenant-one:\n"
           "    mysql_connections: '50'\n"
           "    _routing:\n"
           "      receiver:\n"
           "        type: \"webhook\"\n"
           f"        url: \"{stub}/hook\"\n")
    return str(d)


def _silences_json(tmp: Path) -> str:
    return _write(tmp / "silences.json", json.dumps([{
        "id": "stub-1",
        "status": {"state": "active"},
        "matchers": [{"name": "alertname", "value": "GoneAlert",
                      "isRegex": False, "isEqual": True}],
        "createdBy": "gate", "comment": "orphan probe",
        "startsAt": "2026-01-01T00:00:00Z", "endsAt": "2030-01-01T00:00:00Z",
    }]))


def _runtime_rules_json(tmp: Path) -> str:
    return _write(tmp / "runtime-rules.json", json.dumps(
        {"status": "success", "data": {"groups": []}}))


def _mapping_yaml(tmp: Path) -> str:
    return _write(tmp / "prefix-mapping.yaml",
                  "legacy_prefix_a: new_prefix_a\nlegacy_prefix_b: new_prefix_b\n")


def _report_csv(tmp: Path) -> str:
    return _write(tmp / "validation-report.csv",
                  "tenant,metric,legacy_value,new_value,match\n"
                  "tenant-one,conn_count,10,10,true\n")


def _alerts_json(tmp: Path) -> str:
    return _write(tmp / "alerts.json", json.dumps([]))


def _empty_dir(tmp: Path, name: str) -> str:
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _out(tmp: Path, name: str) -> str:
    return str(tmp / name)


# ═══════════════════════════════════════════════════════════════════════════
# Recipes: one per (tool, mode) — i.e. per distinct terminal path.
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Recipe:
    tool: str
    mode: str
    build: Callable[[Path, str], list[str]]   # (tmp_path, stub_url) -> argv tail
    sandbox: bool = False                     # run from the sandbox_repo copy
    needs_kubectl: bool = False               # relies on the fake-kubectl shim
    # A flag combination the tool must REJECT rather than serve. The contract
    # for such a recipe is the exit code, not a JSON document on stdout — see
    # `patch_config[apply]` for the only case, and the exemption note there.
    expect_caller_error: bool = False
    # OPTIONAL payload assertion, run after the document parses. Returns a
    # reason string when the document is wrong, or None when it is fine.
    #
    # Why this exists (#1112, CodeRabbit): `json.loads(stdout)` proves the tool
    # emitted ONE well-formed document — it does NOT prove it emitted the
    # RIGHT one. `migrate_to_operator --checklist-only --dry-run --json` served
    # a perfectly valid *empty CRD preview* with no `checklist` field at all,
    # and this gate stayed green: the caller asked for a checklist and silently
    # got something else. Use `doc_check` on any mode whose *identity* is
    # observable in the payload (a discriminator field, a must-not-be-empty
    # field) — especially where two modes are COMBINED and one could swallow
    # the other. Keep it about mode identity, not report content (see SCOPE §3).
    doc_check: Callable[[object], str | None] | None = None

    @property
    def id(self) -> str:
        return f"{self.tool}[{self.mode}]"


def _checklist_doc(doc: object) -> str | None:
    """`--checklist-only` must serve THE CHECKLIST, whatever else is combined with it.

    Pins the payload identity that plain `json.loads` cannot: the mode's
    discriminator, and the one field the mode exists to produce.
    """
    if not isinstance(doc, dict):
        return f"expected a JSON object, got {type(doc).__name__}"
    if "metadata" in doc and "checklist" not in doc:
        return ("this is the DRY-RUN CRD-preview document, not the checklist "
                "document — `--checklist-only` fell through to the dry-run branch")
    if doc.get("status") != "checklist_only":
        return f"status must be 'checklist_only', got {doc.get('status')!r}"
    checklist = doc.get("checklist")
    if not isinstance(checklist, str) or not checklist.strip():
        return ("the `checklist` field is missing/empty — `--checklist-only` "
                "served a document WITHOUT the checklist the caller asked for")
    return None


def _kustomization_doc(doc: object) -> str | None:
    """`--kustomize` must put the kustomization INSIDE the one document.

    The pre-#1112 shape emitted it as a second `---`-separated YAML/JSON blob.
    Combined with `--dry-run` (a combination no recipe drove before) the
    kustomization could just as easily have been dropped on the floor: the
    document would still parse, and a `json.loads`-only assertion would still
    be green. So assert it is actually THERE and counted.
    """
    if not isinstance(doc, dict):
        return f"expected a JSON object, got {type(doc).__name__}"
    if set(doc) != {"crds", "kustomization", "summary"}:
        return f"top-level keys must be {{crds, kustomization, summary}}, got {sorted(doc)}"
    kust = doc.get("kustomization")
    if not isinstance(kust, dict) or kust.get("kind") != "Kustomization":
        return ("`--kustomize` was passed but the `kustomization` field is not a "
                f"Kustomization object (got {kust!r}) — it was dropped, not embedded")
    if doc.get("summary", {}).get("kustomization") != 1:
        return "summary.kustomization must count the kustomization (1)"
    return None


def _opa_input_doc(doc: object) -> str | None:
    """`--dry-run` OUTRANKS `--opa-url`: the document is the OPA *input*, not a report.

    Pins a PRECEDENCE that is currently implicit. Both flags are accepted
    together and each selects a different payload (input document vs evaluation
    report), so one necessarily wins. Here dry-run winning is correct by design
    — dry-run means "show me what you WOULD send to that OPA" — but nothing
    codified it, so a refactor could silently flip it and start firing real OPA
    queries at a caller who asked for a dry run.
    """
    if not isinstance(doc, dict):
        return f"expected a JSON object, got {type(doc).__name__}"
    if "violations" in doc or "tenants_evaluated" in doc:
        return ("this is the OPA evaluation REPORT — `--dry-run` must outrank "
                "`--opa-url` and emit the OPA input document instead (no query sent)")
    if "platform_version" not in doc or "tenants" not in doc:
        return f"expected the OPA input document (platform_version/tenants), got {sorted(doc)}"
    return None


R = Recipe
RECIPES: list[Recipe] = [
    # ── alert_correlate ────────────────────────────────────────────────────
    R("alert_correlate", "prometheus",
      lambda t, s: ["--prometheus", s, "--json"]),
    R("alert_correlate", "input-file",
      lambda t, s: ["--input", _alerts_json(t), "--json"]),

    # ── alert_quality ──────────────────────────────────────────────────────
    R("alert_quality", "prometheus",
      lambda t, s: ["--prometheus", s, "--alertmanager", s, "--json"]),

    # ── analyze_rule_pack_gaps ─────────────────────────────────────────────
    R("analyze_rule_pack_gaps", "config-dir",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--json"]),
    R("analyze_rule_pack_gaps", "output-file",
      lambda t, s: ["--config-dir", str(SEED_CONF_D),
                    "--output", _out(t, "gaps.json"), "--json"]),

    # ── assemble_config_dir ────────────────────────────────────────────────
    R("assemble_config_dir", "check",
      lambda t, s: ["--sources", str(SEED_CONF_D), "--check", "--json"]),
    R("assemble_config_dir", "assemble",
      lambda t, s: ["--sources", str(SEED_CONF_D),
                    "--output", _out(t, "assembled"), "--json"]),
    # COMBINATION recipe (#1112 flag-matrix sweep): `--check` returns early, so
    # `--validate` is silently ignored when both are given (see the sweep notes
    # in the PR — a semantics question for the owner, not a stdout-contract
    # break). Gated here for the stdout contract only: the combined path must
    # still emit exactly one document.
    R("assemble_config_dir", "check-validate",
      lambda t, s: ["--sources", str(SEED_CONF_D), "--check", "--validate", "--json"]),

    # ── backtest_threshold  (⚠ known multi-mode trap) ──────────────────────
    R("backtest_threshold", "single-tenant",
      lambda t, s: ["--tenant", "tenant-one", "--metric", "max_connections",
                    "--old-value", "100", "--new-value", "150",
                    "--prometheus", s, "--json"]),
    R("backtest_threshold", "skip-if-unavailable",
      lambda t, s: ["--tenant", "tenant-one", "--metric", "max_connections",
                    "--old-value", "100", "--new-value", "150",
                    # deliberately unreachable: port 1 is never listening
                    "--prometheus", "http://127.0.0.1:1",
                    "--skip-if-unavailable", "--json"]),
    R("backtest_threshold", "no-changes",
      lambda t, s: ["--config-dir", str(SEED_CONF_D),
                    "--baseline", str(SEED_CONF_D),   # identical => zero changes
                    "--prometheus", s, "--json"]),

    # ── batch_diagnose  (⚠ known multi-mode trap) ──────────────────────────
    R("batch_diagnose", "dry-run",
      lambda t, s: ["--tenants", "tenant-one,tenant-two", "--dry-run", "--json"]),
    R("batch_diagnose", "run",
      lambda t, s: ["--tenants", "tenant-one,tenant-two",
                    "--prometheus", s, "--json"]),
    R("batch_diagnose", "autodiscover",
      lambda t, s: ["--prometheus", s, "--json"], needs_kubectl=True),   # tenants from the ConfigMap

    # ── blind_spot_discovery  (--json-output spelling) ─────────────────────
    R("blind_spot_discovery", "scan",
      lambda t, s: ["--prometheus", s, "--config-dir", str(SEED_CONF_D),
                    "--json-output"]),

    # ── byo_check ──────────────────────────────────────────────────────────
    R("byo_check", "prometheus",
      lambda t, s: ["prometheus", "--prometheus", s, "--json"]),
    R("byo_check", "alertmanager",
      lambda t, s: ["alertmanager", "--alertmanager", s, "--json"]),
    R("byo_check", "all",
      lambda t, s: ["all", "--prometheus", s, "--alertmanager", s, "--json"]),

    # ── cardinality_forecasting ────────────────────────────────────────────
    R("cardinality_forecasting", "forecast",
      lambda t, s: ["--prometheus", s, "--json"]),

    # ── check_alert ────────────────────────────────────────────────────────
    R("check_alert", "query",
      lambda t, s: ["HighConnectionCount", "tenant-one", "--prometheus", s, "--json"]),

    # ── config_diff  (--json-output spelling) ──────────────────────────────
    R("config_diff", "json-output",
      lambda t, s: ["--old-dir", str(SEED_CONF_D), "--new-dir", str(EXPORTER_CONF_D),
                    "--json-output"]),
    R("config_diff", "format-json-empty-diff",
      lambda t, s: ["--old-dir", str(SEED_CONF_D), "--new-dir", str(SEED_CONF_D),
                    "--format", "json"]),

    # ── cutover_tenant  (--json-output spelling; confirmed violator) ───────
    R("cutover_tenant", "dry-run",
      lambda t, s: ["--readiness-json", _readiness_json(t), "--tenant", "tenant-one",
                    "--prometheus", s, "--dry-run", "--json-output"]),
    R("cutover_tenant", "apply",
      lambda t, s: ["--readiness-json", _readiness_json(t), "--tenant", "tenant-one",
                    "--prometheus", s, "--json-output"], needs_kubectl=True),

    # ── diagnose ───────────────────────────────────────────────────────────
    R("diagnose", "basic",
      lambda t, s: ["tenant-one", "--prometheus", s, "--json"]),
    R("diagnose", "show-inheritance",
      lambda t, s: ["db-demo", "--prometheus", s, "--config-dir", str(SEED_CONF_D),
                    "--show-inheritance", "--json"]),

    # ── discover_instance_mappings  (⚠ known multi-mode trap) ──────────────
    R("discover_instance_mappings", "prometheus",
      lambda t, s: ["--prometheus", s, "--instance", "stub:9104", "--json"]),
    R("discover_instance_mappings", "prometheus-with-labels",
      lambda t, s: ["--prometheus", s + "/rich", "--instance", "stub:9104", "--json"]),
    R("discover_instance_mappings", "output-file",
      lambda t, s: ["--prometheus", s + "/rich", "--instance", "stub:9104",
                    "--json", "--output", _out(t, "mappings.json")]),
    R("discover_instance_mappings", "endpoint-scrape",
      lambda t, s: ["--endpoint", f"{s}/rich/metrics", "--json"]),

    # ── drift_detect ───────────────────────────────────────────────────────
    R("drift_detect", "configmap",
      lambda t, s: ["--dirs", f"{SEED_CONF_D},{EXPORTER_CONF_D}",
                    "--mode", "configmap", "--json"]),
    R("drift_detect", "operator",
      lambda t, s: ["--dirs", str(SEED_CONF_D), "--mode", "operator", "--json"], needs_kubectl=True),

    # ── explain_route ──────────────────────────────────────────────────────
    R("explain_route", "all-tenants",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--json"]),
    R("explain_route", "trace",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--tenant", "db-demo",
                    "--trace", "--alertname", "HighConnectionCount",
                    "--severity", "critical", "--json"]),

    # ── federation_check ───────────────────────────────────────────────────
    R("federation_check", "edge",
      lambda t, s: ["edge", "--prometheus", s, "--json"]),
    R("federation_check", "central",
      lambda t, s: ["central", "--prometheus", s, "--json"]),
    R("federation_check", "e2e",
      lambda t, s: ["e2e", "--prometheus", s, "--edge-urls", s, "--json"]),

    # ── generate_rule_pack_split ───────────────────────────────────────────
    R("generate_rule_pack_split", "dry-run",
      lambda t, s: ["--rule-packs-dir", str(RULE_PACKS), "--dry-run", "--json"]),
    R("generate_rule_pack_split", "write",
      lambda t, s: ["--rule-packs-dir", str(RULE_PACKS),
                    "--output-dir", _out(t, "split"), "--json"]),

    # ── gitops_check ───────────────────────────────────────────────────────
    R("gitops_check", "local",
      lambda t, s: ["local", "--dir", str(SEED_CONF_D), "--json"]),
    R("gitops_check", "repo",
      lambda t, s: ["repo", "--url", REPO_ROOT.as_uri(), "--json"]),
    R("gitops_check", "sidecar", lambda t, s: ["sidecar", "--json"], needs_kubectl=True),

    # ── grafana_import ─────────────────────────────────────────────────────
    R("grafana_import", "dry-run",
      lambda t, s: ["--dashboard", str(DASHBOARD_JSON), "--dry-run", "--json"]),
    R("grafana_import", "verify", lambda t, s: ["--verify", "--json"], needs_kubectl=True),
    # COMBINATION recipe (#1112 flag-matrix sweep): `--dry-run` + `--verify`.
    # `--verify` wins; it is read-only, so nothing is lost by `--dry-run` being
    # vacuous here — but the combined path is a distinct branch and must still
    # emit exactly one document.
    R("grafana_import", "dry-run-verify",
      lambda t, s: ["--dashboard", str(DASHBOARD_JSON), "--dry-run", "--verify", "--json"],
      needs_kubectl=True),

    # ── maintenance_scheduler  (--json-output spelling; confirmed violator) ─
    R("maintenance_scheduler", "dry-run",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--alertmanager", s,
                    "--dry-run", "--json-output"]),
    R("maintenance_scheduler", "report-only",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--json-output"]),
    R("maintenance_scheduler", "apply-silences",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--alertmanager", s,
                    "--pushgateway", s, "--json-output"]),

    # ── migrate_to_operator  (⚠ known multi-mode trap) ─────────────────────
    R("migrate_to_operator", "dry-run",
      lambda t, s: ["--source-dir", str(K8S_MONITORING),
                    "--config-dir", str(SEED_CONF_D),
                    "--output-dir", _out(t, "crds"), "--dry-run", "--json"]),
    R("migrate_to_operator", "checklist-only",
      lambda t, s: ["--source-dir", str(K8S_MONITORING),
                    "--config-dir", str(SEED_CONF_D),
                    "--output-dir", _out(t, "crds2"), "--checklist-only", "--json"],
      doc_check=_checklist_doc),
    # THE COMBINATION RECIPE (#1112, CodeRabbit). The two mode flags above are
    # orthogonal, not mutually exclusive — argparse accepts both — and the
    # recipe table originally tested them only in isolation. Combined, the tool
    # served an empty CRD preview and dropped the checklist entirely; valid
    # JSON, so a `json.loads`-only assertion could never see it. Hence
    # `doc_check`: this recipe is worthless without it.
    R("migrate_to_operator", "checklist-only-dry-run",
      lambda t, s: ["--source-dir", str(K8S_MONITORING),
                    "--config-dir", str(SEED_CONF_D),
                    "--output-dir", _out(t, "crds2b"),
                    "--checklist-only", "--dry-run", "--json"],
      doc_check=_checklist_doc),
    R("migrate_to_operator", "write",
      lambda t, s: ["--source-dir", str(K8S_MONITORING),
                    "--config-dir", str(SEED_CONF_D),
                    "--output-dir", _out(t, "crds3"), "--json"]),

    # ── notification_tester ────────────────────────────────────────────────
    R("notification_tester", "dry-run",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--dry-run", "--json"]),
    R("notification_tester", "live-send",
      lambda t, s: ["--config-dir", _confd_with_webhook(t, s), "--json"]),

    # ── onboard_platform ───────────────────────────────────────────────────
    R("onboard_platform", "dry-run",
      lambda t, s: ["--alertmanager-config", str(ALERTMANAGER_YML),
                    "-o", _out(t, "onboard"), "--dry-run", "--json"]),
    R("onboard_platform", "write",
      lambda t, s: ["--alertmanager-config", str(ALERTMANAGER_YML),
                    "-o", _out(t, "onboard2"), "--json"]),

    # ── operator_check ─────────────────────────────────────────────────────
    R("operator_check", "check",
      lambda t, s: ["--rule-packs-dir", str(RULE_PACKS),
                    "--config-dir", str(SEED_CONF_D), "--json"], needs_kubectl=True),
    R("operator_check", "with-target-health",
      lambda t, s: ["--rule-packs-dir", str(RULE_PACKS),
                    "--config-dir", str(SEED_CONF_D),
                    "--prometheus", s, "--json"], needs_kubectl=True),

    # ── operator_generate  (⚠ known multi-mode trap: kustomize branch) ─────
    R("operator_generate", "dry-run",
      lambda t, s: ["--rule-packs-dir", str(RULE_PACKS),
                    "--config-dir", str(SEED_CONF_D),
                    "--output-dir", _out(t, "op1"), "--dry-run", "--json"]),
    R("operator_generate", "write",
      lambda t, s: ["--rule-packs-dir", str(RULE_PACKS),
                    "--config-dir", str(SEED_CONF_D),
                    "--output-dir", _out(t, "op2"), "--json"]),
    R("operator_generate", "kustomize",
      lambda t, s: ["--rule-packs-dir", str(RULE_PACKS),
                    "--config-dir", str(SEED_CONF_D),
                    "--output-dir", _out(t, "op3"), "--kustomize", "--json"],
      doc_check=_kustomization_doc),
    # COMBINATION recipe (#1112 flag-matrix sweep): `--dry-run` and `--kustomize`
    # are orthogonal and were only ever driven in isolation. Verified correct —
    # the kustomization IS embedded in the dry-run document — so this locks in
    # working behaviour rather than fixing broken behaviour.
    R("operator_generate", "dry-run-kustomize",
      lambda t, s: ["--rule-packs-dir", str(RULE_PACKS),
                    "--config-dir", str(SEED_CONF_D),
                    "--output-dir", _out(t, "op4"),
                    "--dry-run", "--kustomize", "--json"],
      doc_check=_kustomization_doc),

    # ── patch_config  (⚠ known multi-mode trap: apply vs --diff) ───────────
    R("patch_config", "diff",
      lambda t, s: ["db-a", "max_connections", "150", "--diff", "--json"], needs_kubectl=True),
    # THE ONE EXIT-CODE RECIPE, not a JSON-document one (#1112 fiat).
    #
    # `--json` without `--diff` is a CONTRADICTORY flag combination on a
    # *mutating* tool: `--json` asks for the diff-preview document (the help
    # text has always said "requires --diff"), while the absence of `--diff`
    # means "apply for real". The tool used to resolve that contradiction by
    # silently dropping `--json` and APPLYING the change — a caller that asked
    # for a preview got a live ConfigMap write instead.
    #
    # The contract cannot be "emit one JSON document" here, because there is no
    # honest document to emit: serving the request at all is the bug. So the
    # fix is fail-loud — error on stderr, EXIT_CALLER_ERROR, nothing applied —
    # and the assertion is therefore `exit == 2`, not `json.loads(stdout)`.
    # (Contradictory flags = caller error, same as an argparse rejection, which
    # `test_tool_exit_codes.py::test_invalid_args_exits_caller_error` gates.)
    R("patch_config", "apply",
      lambda t, s: ["db-a", "max_connections", "150", "--json"],
      needs_kubectl=True, expect_caller_error=True),

    # ── policy_engine ──────────────────────────────────────────────────────
    R("policy_engine", "no-policies",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--json"]),
    R("policy_engine", "with-policy-file",
      lambda t, s: ["--config-dir", str(SEED_CONF_D),
                    "--policy", _policy_file(t), "--json"]),

    # ── policy_opa_bridge ──────────────────────────────────────────────────
    R("policy_opa_bridge", "dry-run",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--dry-run", "--json"]),
    R("policy_opa_bridge", "opa-url",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--opa-url", s, "--json"]),
    R("policy_opa_bridge", "empty-config-dir",
      lambda t, s: ["--config-dir", _empty_dir(t, "empty_confd"),
                    "--dry-run", "--json"]),
    # COMBINATION recipe (#1112 flag-matrix sweep): `--dry-run` + `--opa-url`.
    # Two payload selectors accepted together — exactly the shape that bit
    # migrate_to_operator. Here dry-run correctly wins (no query is sent), but
    # nothing codified that precedence; `doc_check` now does.
    R("policy_opa_bridge", "dry-run-outranks-opa-url",
      lambda t, s: ["--config-dir", str(SEED_CONF_D),
                    "--dry-run", "--opa-url", s, "--json"],
      doc_check=_opa_input_doc),

    # ── rule_pack_diff ─────────────────────────────────────────────────────
    R("rule_pack_diff", "no-diff",
      lambda t, s: ["--from", str(RULE_PACKS / "rule-pack-mariadb.yaml"),
                    "--to", str(RULE_PACKS / "rule-pack-mariadb.yaml"), "--json"]),
    R("rule_pack_diff", "breaking-diff",
      lambda t, s: ["--from", str(RULE_PACKS / "rule-pack-mariadb.yaml"),
                    "--to", str(RULE_PACKS / "rule-pack-redis.yaml"), "--json"]),

    # ── runtime_audit ──────────────────────────────────────────────────────
    R("runtime_audit", "runtime-json",
      lambda t, s: ["--rule-packs-dir", str(RULE_PACKS),
                    "--runtime-json", _runtime_rules_json(t), "--json"]),
    R("runtime_audit", "prometheus",
      lambda t, s: ["--rule-packs-dir", str(RULE_PACKS), "--prometheus", s, "--json"]),

    # ── shadow_verify ──────────────────────────────────────────────────────
    R("shadow_verify", "preflight",
      lambda t, s: ["preflight", "--mapping", _mapping_yaml(t),
                    "--prometheus", s, "--alertmanager", s, "--json"]),
    R("shadow_verify", "runtime",
      lambda t, s: ["runtime", "--prometheus", s, "--alertmanager", s, "--json"]),
    R("shadow_verify", "convergence",
      lambda t, s: ["convergence", "--readiness-json", _readiness_json(t),
                    "--report-csv", _report_csv(t), "--prometheus", s, "--json"]),
    R("shadow_verify", "all",
      lambda t, s: ["all", "--mapping", _mapping_yaml(t),
                    "--readiness-json", _readiness_json(t),
                    "--report-csv", _report_csv(t),
                    "--prometheus", s, "--alertmanager", s, "--json"]),

    # ── silencer_drift_check ───────────────────────────────────────────────
    R("silencer_drift_check", "orphan-found",
      lambda t, s: ["--silences-file", _silences_json(t),
                    "--rule-source", str(RULE_PACKS), "--json"]),

    # ── state_reconcile ────────────────────────────────────────────────────
    R("state_reconcile", "dry-run",
      lambda t, s: ["--state-dir", _empty_dir(t, "state"),
                    "--manifest-path", _out(t, "manifest.json"),
                    "--dry-run", "--json"]),
    R("state_reconcile", "apply",
      lambda t, s: ["--state-dir", _empty_dir(t, "state2"),
                    "--manifest-path", _out(t, "manifest2.json"), "--json"]),

    # ── threshold_govern ───────────────────────────────────────────────────
    R("threshold_govern", "dry-run",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--prometheus", s, "--json"]),
    R("threshold_govern", "apply",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--prometheus", s,
                    "--apply", "--tenant-api-url", s,
                    "--identity-email", "gate@example.invalid",
                    "--identity-groups", "platform-admins",
                    "--auth-token", "stub-token", "--json"]),

    # ── threshold_recommend  (⚠ known multi-mode trap) ─────────────────────
    R("threshold_recommend", "recommend",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--prometheus", s, "--json"]),
    R("threshold_recommend", "dry-run",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--prometheus", s,
                    "--dry-run", "--json"]),
    R("threshold_recommend", "generate-observed-map",
      lambda t, s: ["--generate-observed-map", "--json"], sandbox=True),

    # ── validate_config ────────────────────────────────────────────────────
    R("validate_config", "basic",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--json"]),
    R("validate_config", "full",
      lambda t, s: ["--config-dir", str(SEED_CONF_D),
                    "--rule-packs", str(RULE_PACKS), "--version-check", "--json"]),
]


# ═══════════════════════════════════════════════════════════════════════════
# Meta-tests — keep the scope honest
# ═══════════════════════════════════════════════════════════════════════════
def test_fixture_paths_exist():
    """Every in-repo fixture this gate leans on must actually be there."""
    missing = [str(p) for p in FIXTURE_PATHS if not p.exists()]
    assert not missing, f"gate fixtures missing from the repo: {missing}"


def test_recipe_table_covers_every_json_tool():
    """A tool that grows a --json flag must gain a recipe, or this gate rots.

    (33 tools spell it `--json`, 4 spell it `--json-output`; all 37 are in scope.)
    """
    covered = {r.tool for r in RECIPES}
    uncovered = sorted(set(JSON_TOOLS) - covered)
    stale = sorted(covered - set(JSON_TOOLS))
    assert not uncovered, (
        f"{len(uncovered)} tool(s) declare a --json/--json-output flag but have "
        f"no recipe in RECIPES: {uncovered}"
    )
    assert not stale, f"RECIPES names tool(s) that no longer exist: {stale}"
    assert len(JSON_TOOLS) == 37, (
        f"expected 37 JSON-flag tools (33 --json + 4 --json-output), "
        f"found {len(JSON_TOOLS)}: {sorted(JSON_TOOLS)}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════
def _run(recipe: Recipe, tmp_path: Path, stub: str, sandbox_root: Path,
         fake_bin: Path) -> subprocess.CompletedProcess:
    root = sandbox_root if recipe.sandbox else REPO_ROOT
    script = root / "scripts" / "tools" / "ops" / f"{recipe.tool}.py"
    env = dict(os.environ)
    # Windows hosts default to cp950 -> tools with CJK output would raise
    # UnicodeEncodeError before we ever see stdout. That's an environment
    # artefact, not the contract under test.
    env["PYTHONIOENCODING"] = "utf-8"
    # Shadow the real kubectl: no cluster can be reached, so even the mutating
    # modes (patch_config apply, cutover_tenant apply) are safe to exercise.
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env.pop("PROMETHEUS_URL", None)      # never let a stray env var redirect us
    env.pop("ALERTMANAGER_URL", None)
    return subprocess.run(
        [sys.executable, str(script), *recipe.build(tmp_path, stub)],
        capture_output=True, timeout=TIMEOUT_S, cwd=str(root), env=env,
    )


@pytest.mark.parametrize("recipe", RECIPES, ids=[r.id for r in RECIPES])
def test_json_mode_emits_exactly_one_json_document(
    recipe: Recipe, tmp_path: Path, stub_url: str, sandbox_repo: Path,
    fake_kubectl_dir: Path,
):
    """`--json` ⇒ stdout is exactly one JSON document, on every terminal path.

    Full-text `json.loads(stdout)` is the assertion: it proves the document
    parses AND that no prose leaked in before or after it.
    """
    if recipe.needs_kubectl and os.name == "nt":
        pytest.skip(
            f"{recipe.id}: the fake-kubectl shim cannot intercept on Windows — "
            f"CreateProcess resolves a bare `kubectl` to `kubectl.exe` only and "
            f"ignores PATHEXT, so a .bat/POSIX shim is bypassed and the REAL "
            f"kubectl runs. Exercised on POSIX (Linux CI / dev container), which "
            f"is where this gate is authoritative."
        )

    proc = _run(recipe, tmp_path, stub_url, sandbox_repo, fake_kubectl_dir)
    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")

    def fail(why: str) -> str:
        return (
            f"\n--json stdout contract VIOLATED\n"
            f"  tool  : {recipe.tool}.py\n"
            f"  mode  : {recipe.mode}\n"
            f"  reason: {why}\n"
            f"  exit  : {proc.returncode}\n"
            f"  stdout[:200]: {stdout[:200]!r}\n"
            f"  stderr[:200]: {stderr[:200]!r}\n"
            f"  fix   : route every human-readable line to stderr; emit the one "
            f"JSON document to stdout on THIS path too.\n"
        )

    if recipe.expect_caller_error:
        # A rejected flag combination: the contract is the exit code (nothing was
        # served, nothing was applied), and stdout carries no document at all.
        assert proc.returncode == EXIT_CALLER_ERROR, fail(
            f"contradictory flags must be rejected with "
            f"EXIT_CALLER_ERROR ({EXIT_CALLER_ERROR}), got {proc.returncode}"
        )
        assert not stdout.strip(), fail(
            "a rejected invocation must not write anything to stdout"
        )
        return

    assert stdout.strip(), fail("stdout is EMPTY — no JSON document at all")

    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(fail(
            f"stdout is not a single JSON document ({e}). Either prose is mixed "
            f"in with the JSON, or the path emits no JSON at all."
        )) from None

    # ONE well-formed document is necessary, not sufficient: a mode can serve a
    # valid document that is the WRONG one (see Recipe.doc_check).
    if recipe.doc_check is not None:
        reason = recipe.doc_check(doc)
        assert reason is None, fail(f"wrong document for this mode — {reason}")
