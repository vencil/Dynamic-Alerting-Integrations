"""Verdict-flag contract gate: a flag that should change the verdict must.

THE DEFECT CLASS (#2908)
------------------------
A flag is parsed, accepted, and then never reaches the decision it names.
The run exits 0, the suite is green, and the caller believes the flag did
its job. This class has been fixed after the fact more than once:

* #2494 — ``validate_migration --tolerance`` was parsed but not passed into
  the comparison; every value judged at the 0.001 default (PR #2880).
* #1828 — ``assemble_config_dir --check --validate`` never ran ``--validate``.
* #2493 — ``batch-diagnose`` always exited 0 (PR #2881).

``test_ci_flag_contract`` already gates ONE flag (``--ci``) this way. This
module generalises its shape from ``(tool)`` to ``(tool, flag)``.

THE CONTRACT
------------
Each recipe names one fixture and runs the tool on it three times:

1. **must-fire** — WITHOUT the flag. The result must be exactly
   ``without``. Together with the flip this proves the fixture sits where
   the flag has something to change (e.g. a 5 % drift that the 0.1 % default
   tolerance rejects). When ``without`` is 0 the run alone proves no finding
   exists — the flip on the same fixture is what does.
2. **flip** — WITH the flag at a value that should change the verdict. The
   result must be exactly ``with_``, which a meta-test requires to differ
   from ``without``. Equal results would mean the flag changed nothing.
3. **boundary** — WITH the flag, but where it should NOT change the
   verdict (a tolerance tighter than the drift, ``--strict`` on a fixture
   with nothing to escalate). The result must equal ``without`` again. This
   catches a flag whose mere presence flips the result, regardless of the
   value it was given — a value parsed and then ignored passes the flip only
   if the ignored path happens to agree, and fails here.

The observed result is the exit code, and expected exit codes are 0
(EXIT_OK) or 1 (EXIT_VIOLATION) only: a 2 is a caller error and a crash also
exits non-zero, so neither may stand in for a flip. A tool whose documented
verdict is NOT 0 vs 1 (assemble_config_dir exits 2 on a validation error)
is observed through its ``--json`` report together with the exit code
(``observe``); an unparsable report observes as ``UNPARSABLE``, which no
recipe may expect. A flag read on more than one path of a tool (render vs
``--validate``, ``--dry-run`` vs write) gets one recipe per path (``mode``). Every run is also checked for
a traceback. Every recipe runs under ``DA_LANG=zh`` and ``DA_LANG=en``: some
tools build a separate parser per help language (alert_quality declares
``--min-score`` once in each), and one language would leave the other's
declaration unexercised.

Environment is pinned where it would change a verdict: a fake ``amtool``
that accepts every config is first on PATH (``amtool_accepts``), because
validate_config's routes row is WARN without amtool, PASS or FAIL with one —
and the CI Python Tests jobs install a real one.

SCOPE — WHICH FLAGS
-------------------
Every ops CLI — ``scripts/tools/ops/*.py``, plus an underscore module that
has a ``__main__`` guard — and every option string it passes to
``add_argument`` whose name matches ``VERDICT_FLAG_RE``, the name pattern
from #2908. The AST walk sees literal option strings in the tool's own file
only, so it refuses the two shapes it cannot attribute: an option string
that is not a literal, and a verdict-named flag declared inside a shared
``scripts/tools/_lib*.py`` helper (``_lib_io`` already has an ``add_*_arg``
family). Two boundaries, both stated on the issue, and both need
the owner's say-so to widen:

* ``dx/`` and ``lint/`` tools are not enumerated.
* A flag whose name is NOT in the pattern is not enumerated, even if it
  changes the verdict (``--mode``, ``--rounds``, …). The population is a
  lower bound, not the set of verdict flags.

Every enumerated ``(tool, flag)`` must be in exactly one of:

* ``RECIPES`` — gated by the three runs above;
* ``SAME_NAME_DIFFERENT_MEANING`` — the name matches the pattern but the flag
  does not decide a verdict (with the reason);
* ``PENDING`` — not gated YET (#2908). This list may only shrink: it must
  stay a subset of ``POPULATION_AT_LANDING``, the population measured when
  this gate landed. A flag that appears later cannot be parked here — it
  needs a recipe or a same-name entry in the PR that adds it.

``POPULATION_AT_LANDING`` is a record of what was measured, not a knob:
adding to it to make room in ``PENDING`` falsifies that record. Its digest
is pinned, so such an edit has to change the digest line too, in plain
sight of review.

A flag that turns out to change nothing when its recipe is written is a
real bug in that tool. It is filed as its own issue (search for a duplicate
first) and recorded in ``KNOWN_FLAG_NOOP`` — it is not fixed in the PR that
adds the recipe. A listed recipe must still behave as exactly that no-op
(must-fire as expected, flip run equal to it) and is then xfailed; any
other failure — a crash, a caller error — stays a failure, and a flag that
starts working fails until its entry is removed.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

# validate_config reads the tenants through da-guard (#2115 0-B).
pytestmark = pytest.mark.usefixtures("da_guard_env")

REPO_ROOT = Path(__file__).resolve().parents[2]
RULE_PACKS = REPO_ROOT / "rule-packs"
OPS_DIR = REPO_ROOT / "scripts" / "tools" / "ops"

TIMEOUT_S = 90

# scripts/tools/_lib_exitcodes.py is the SSOT; test_tool_exit_codes.py pins it.
EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_CALLER_ERROR = 2

# ═══════════════════════════════════════════════════════════════════════════
# Population — AST-enumerated, name-pattern scoped (#2908)
# ═══════════════════════════════════════════════════════════════════════════
VERDICT_FLAG_RE = re.compile(
    r"^--(?:strict|validate|tolerance|min-score|max-.+|threshold.*|fail-on.*"
    r"|allow-.+|warn-.+|severity|ignore.*|exclude.*|include.*"
    r"|stability-window)$"
)


TOOLS_DIR = OPS_DIR.parent


def _is_cli(path: Path) -> bool:
    """A non-underscore ops module, or an underscore one that still runs as
    a CLI (it has a ``__main__`` guard) — e.g. the federation reconciler."""
    if not path.name.startswith("_"):
        return True
    return "__name__" in path.read_text(encoding="utf-8") and any(
        isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)
        for n in ast.parse(path.read_text(encoding="utf-8")).body)


def _tool_files() -> list[Path]:
    return [p for p in sorted(OPS_DIR.glob("*.py"))
            if p.name != "__init__.py" and _is_cli(p)]


def _helper_files() -> list[Path]:
    """Shared modules a tool can call to declare its flags (``_lib_io`` has a
    family of ``add_*_arg`` helpers). A verdict flag declared in one of
    these cannot be attributed to a tool by this AST walk."""
    return sorted(TOOLS_DIR.glob("_lib*.py"))


def _add_argument_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            yield node


def _option_strings(call: ast.Call) -> list[str] | None:
    """The call's option strings, or None when any of them is not a string
    literal — the enumeration cannot see a name built at runtime."""
    if not call.args:
        return None
    out = []
    for arg in call.args:
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            return None
        out.append(arg.value)
    return out


def collect_verdict_flags() -> set[tuple[str, str]]:
    """Every (tool, option string) an ops CLI passes to add_argument whose
    name matches VERDICT_FLAG_RE. A flag declared twice (e.g. once per help
    language) counts once."""
    found: set[tuple[str, str]] = set()
    for path in _tool_files():
        for call in _add_argument_calls(path):
            for opt in _option_strings(call) or []:
                if VERDICT_FLAG_RE.match(opt):
                    found.add((path.stem, opt))
    return found


POPULATION = collect_verdict_flags()

# Measured on main df9781be when this gate landed: 13 tools, 15 distinct
# flags. #2908 reported "17 flags" on 2c36b854 for the same set — that
# counted declarations, and alert_quality / cardinality_forecasting each
# declare theirs twice (once per help language). A record, not a knob — see
# the module docstring.
POPULATION_AT_LANDING: frozenset[tuple[str, str]] = frozenset({
    ("alert_correlate", "--min-score"),
    ("alert_quality", "--min-score"),
    ("assemble_config_dir", "--validate"),
    ("blind_spot_discovery", "--exclude-jobs"),
    ("cardinality_forecasting", "--warn-days"),
    ("drift_detect", "--ignore-prefix"),
    ("explain_route", "--severity"),
    ("generate_alertmanager_routes", "--strict"),
    ("generate_alertmanager_routes", "--validate"),
    ("generate_tenant_mapping_rules", "--validate"),
    ("silencer_drift_check", "--include-inactive"),
    ("threshold_govern", "--max-prs"),
    ("validate_config", "--strict"),
    ("validate_migration", "--stability-window"),
    ("validate_migration", "--tolerance"),
})

# sha256 of the sorted landing record, one "tool flag" per line. Changing
# POPULATION_AT_LANDING means changing this line as well.
POPULATION_AT_LANDING_SHA256 = "a432124249764d467e80fc162668ba35e813ef9adafe8d28dd34e4b25b43681d"

# Not gated yet. Shrink-only: see test_pending_only_shrinks.
PENDING: dict[tuple[str, str], str] = {}

# Name matches the pattern, but the flag decides no verdict. Each entry
# says why. Empty when this gate landed.
SAME_NAME_DIFFERENT_MEANING: dict[tuple[str, str], str] = {
    ("explain_route", "--severity"): (
        "an INPUT label of the alert --trace simulates (beside --alertname / "
        "--tenant / --label), not a threshold the tool judges against: the "
        "route it takes is Alertmanager's own answer (amtool), and the tool's "
        "exit code never depends on it. Accepted silently without --trace: "
        "#2926"),
}

# A recipe whose flag was observed to change nothing: a real bug in that
# tool, filed separately and xfailed here only while it still behaves as
# exactly that no-op — never fixed in the recipe's PR. Value = the issue.
KNOWN_FLAG_NOOP: dict[tuple[str, str], str] = {}


# ═══════════════════════════════════════════════════════════════════════════
# Stub Prometheus / Alertmanager
# ═══════════════════════════════════════════════════════════════════════════
# Instant queries: the stub answers with one sample per tenant, keyed on the
# query text. validate_migration compares OLD vs NEW per tenant.
INSTANT_VALUES = {
    "migration_old": 100.0,
    "migration_new": 105.0,   # 5 % above old
    "steady_old": 100.0,      # equal on every round: converges
    "steady_new": 100.0,
}


def _alert_quality_matrix(now: float) -> list[dict]:
    """Two alerts for one tenant, graded GOOD and WARN → tenant score 75.

    GOOD: fired once a day ago, resolved after 1 h (not flapping, not stale,
    not noisy). WARN: fired once three days ago and took 25 h to resolve
    (resolution > 24 h). No BAD alert, so --ci alone exits 0.
    """
    def series(alertname: str, start: float, duration: float) -> dict:
        return {
            "metric": {"alertname": alertname, "tenant": "tenant-one",
                       "alertstate": "firing"},
            "values": [[start, "0"], [start + 60, "1"],
                       [start + 60 + duration, "0"]],
        }
    return [
        series("QualityGood", now - 86400, 3600),
        series("QualityWarn", now - 3 * 86400, 25 * 3600),
    ]


# threshold_govern: every tenant writes GOVERN_KEY at GOVERN_CURRENT, and the
# observed series it maps to (metric_observed_map.yaml) samples GOVERN_OBSERVED
# GOVERN_SAMPLES times: P95 80 vs 50 is +60 % (past the 25 % gate) at MEDIUM
# confidence (>= 100 samples), so each tenant plans exactly one change.
GOVERN_TENANTS = ("tenant-one", "tenant-two", "tenant-three")
GOVERN_KEY = "mysql_connections"
GOVERN_SERIES = "tenant:mysql_threads_connected:max"
GOVERN_CURRENT = 50
GOVERN_OBSERVED = 80.0
GOVERN_SAMPLES = 120
# tenant-api paths, optionally under a per-run prefix /ta/<run>/ so the stub
# can count each run's PUTs (and play a per-run personality, see below).
_TENANT_PATH = re.compile(r"(?:/ta/([^/]+))?/api/v1/tenants/([^/]+)")
# PUTs the stub received, per run prefix. A run whose prefix ends in
# PENDING_SUFFIX answers tenant-one's PUT with 409 pending-PR (dedup), so a
# cap that counted attempts instead of new PRs would show.
GOVERN_PUTS: dict[str, int] = {}
_GOVERN_PUTS_LOCK = threading.Lock()
PENDING_SUFFIX = "-tenant-one-pending"


def _govern_tenant_yaml(tenant: str) -> str:
    return f'tenants:\n  {tenant}:\n    {GOVERN_KEY}: "{GOVERN_CURRENT}"\n'


def _cardinality_matrix(now: float) -> list[dict]:
    """One tenant's threshold-series count over the last 10 days: 100 -> 400,
    +30/day. Against --limit 1000 that is 20 days to the limit."""
    return [{"metric": {"tenant": "tenant-one"},
             "values": [[now - (10 - i) * 86400, str(100 + 30 * i)]
                        for i in range(11)]}]


class _StubHandler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        params = parse_qs(url.query)
        tenant_path = _TENANT_PATH.fullmatch(url.path)
        if url.path == "/api/v1/query":
            query = params.get("query", [""])[0]
            if query.startswith(GOVERN_SERIES + "{"):
                # threshold_recommend's range-vector query -> a matrix.
                m = re.search(r'tenant="([^"]+)"', query)
                if m is None:
                    self._send({"error": "stub: no tenant label"}, status=400)
                    return
                tenant = m.group(1)
                now = time.time()
                self._send({"status": "success", "data": {
                    "resultType": "matrix",
                    "result": [{"metric": {"tenant": tenant},
                                "values": [[now - 60 * i, str(GOVERN_OBSERVED)]
                                           for i in range(GOVERN_SAMPLES)]}]}})
                return
            value = INSTANT_VALUES.get(query)
            result = [] if value is None else [
                {"metric": {"tenant": "tenant-one"},
                 "value": [time.time(), str(value)]}]
            self._send({"status": "success",
                        "data": {"resultType": "vector", "result": result}})
        elif url.path == "/api/v1/targets":
            # redis, postgres and mysql exporter targets (blind_spot_discovery).
            # Job names differ from the db_type they map to, so excluding by
            # job name and by db_type give different answers.
            self._send({"status": "success", "data": {"activeTargets": [
                {"labels": {"job": job, "instance": inst, "namespace": "db"}}
                for job, inst in (("redis-exporter", "r1:9121"),
                                  ("postgres-exporter", "p1:9187"),
                                  ("mysql-exporter", "m1:9104"))]}})
        elif (url.path == "/api/v1/query_range"
              and "tenant_threshold_" in params.get("query", [""])[0]):
            self._send({"status": "success",
                        "data": {"resultType": "matrix",
                                 "result": _cardinality_matrix(time.time())}})
        elif url.path == "/api/v1/query_range":
            self._send({"status": "success",
                        "data": {"resultType": "matrix",
                                 "result": _alert_quality_matrix(time.time())}})
        elif url.path == "/api/v2/alerts":
            self._send([])
        elif tenant_path:
            # tenant-api GET: the file threshold_govern edits surgically.
            self._send({"raw_yaml": _govern_tenant_yaml(unquote(tenant_path.group(2)))})
        else:
            self._send({"error": f"stub: no route for {url.path}"}, status=404)

    do_POST = do_GET  # noqa: N815

    def do_PUT(self):  # noqa: N802
        # tenant-api PR write-mode: every PUT opens a PR.
        url = urlsplit(self.path)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        tenant_path = _TENANT_PATH.fullmatch(url.path)
        if tenant_path is None:
            self._send({"error": f"stub: no PUT route for {url.path}"}, status=404)
            return
        run, tenant = tenant_path.group(1) or "", unquote(tenant_path.group(2))
        with _GOVERN_PUTS_LOCK:
            GOVERN_PUTS[run] = GOVERN_PUTS.get(run, 0) + 1
        if run.endswith(PENDING_SUFFIX) and tenant == "tenant-one":
            self._send({"code": "PENDING_PR_EXISTS", "error": "pending_pr_exists",
                        "existing_pr_url": "https://git.example/pulls/old"},
                       status=409)
            return
        self._send({"status": "pending_review", "pr_number": 1,
                    "pr_url": f"https://git.example/pulls/{tenant}"})

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def stub_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════
def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _write_str(p: Path, text: str) -> str:
    _write(p, text)
    return str(p)


def _confd_with_policy(tmp: Path, forbidden_receiver: str) -> str:
    """A conf.d whose one tenant inherits a slack receiver, under a domain
    policy forbidding `forbidden_receiver`. Forbidding slack is an ADR-007
    violation (WARN, or FAIL under --strict); forbidding anything else is
    not. Nothing else in the tree is a finding."""
    d = tmp / f"confd-forbid-{forbidden_receiver}"
    _write(d / "_defaults.yaml", (
        "defaults:\n  mysql_connections: 80\n"
        "_routing_defaults:\n"
        "  group_by: [tenant, alertname]\n  group_wait: 30s\n"
        "  repeat_interval: 4h\n"
        "  receiver:\n    type: slack\n    api_url: https://hooks.example/a\n"))
    _write(d / "tenant-one.yaml",
           'tenants:\n  tenant-one:\n    mysql_connections: "90"\n')
    _write(d / "_domain_policy.yaml", (
        "domain_policies:\n  fin:\n    tenants: [tenant-one]\n"
        "    constraints:\n"
        f"      forbidden_receiver_types: [{forbidden_receiver}]\n"))
    return str(d)


def _confd_with_group_wait(tmp: Path, group_wait: str) -> str:
    """One tenant routing to a webhook with the given group_wait. An
    unparsable duration ('5x') is rendered as the platform default and
    reported as a blocking generation warning; '30s' is clean."""
    d = tmp / f"confd-group-wait-{group_wait}"
    _write(d / "_defaults.yaml", "defaults:\n  mysql_connections: 80\n")
    _write(d / "tenant-one.yaml", (
        "tenants:\n  tenant-one:\n    mysql_connections: \"90\"\n"
        "    _routing:\n      receiver:\n        type: webhook\n"
        "        url: https://hooks.example/a\n"
        f"      group_wait: {group_wait}\n"))
    return str(d)


def _confd_with_instance_mapping(tmp: Path, mapped_tenant: str) -> str:
    """A conf.d declaring tenant-one, plus a 1:N instance mapping onto
    `mapped_tenant` — a tenant that does or does not exist."""
    d = tmp / f"confd-mapping-{mapped_tenant}"
    _write(d / "tenant-one.yaml",
           'tenants:\n  tenant-one:\n    mysql_connections: "90"\n')
    _write(d / "_instance_mapping.yaml", (
        "instance_tenant_mapping:\n  shared-db-01:\n"
        f"    - tenant: {mapped_tenant}\n"
        "      filter: 'schema=~\"app_.*\"'\n"))
    return str(d)


def _correlated_alerts(tmp: Path) -> str:
    """Two critical alerts a minute apart in one namespace with unrelated
    names: one cluster whose single pair scores 0.8 (overlap 0.4 + namespace
    0.3 + severity 0.1, no name prefix), inside the flag's 0..1 range."""
    labels = {"namespace": "monitoring", "severity": "critical"}
    return _write_str(tmp / "alerts.json", json.dumps([
        {"labels": {"alertname": "DiskFull", "tenant": "tenant-one", **labels},
         "startsAt": "2026-07-01T00:00:00Z", "endsAt": ""},
        {"labels": {"alertname": "CpuHigh", "tenant": "tenant-two", **labels},
         "startsAt": "2026-07-01T00:01:00Z", "endsAt": ""},
    ]))


def _avg_correlations(proc: subprocess.CompletedProcess) -> object:
    """Each cluster's avg_correlation: the mean of the pair scores at or
    above --min-score (0.0 when none clears it)."""
    try:
        return [c["avg_correlation"] for c in json.loads(proc.stdout)["clusters"]]
    except (ValueError, KeyError, TypeError):
        return UNPARSABLE


def _mysql_only_confd(tmp: Path) -> str:
    """One tenant monitored for mysql only — the stub's redis and postgres
    targets are the blind spots."""
    d = tmp / "confd-mysql-only"
    _write(d / "_defaults.yaml", "defaults:\n  mysql_connections: 80\n")
    _write(d / "tenant-one.yaml", "tenants:\n  tenant-one:\n    mysql_connections: 90\n")
    return str(d)


def _blind_spots(proc: subprocess.CompletedProcess) -> object:
    try:
        return sorted(r["db_type"] for r in json.loads(proc.stdout)
                      if r["status"] == "blind_spot")
    except (ValueError, KeyError, TypeError):
        return UNPARSABLE


def _dirs_drifting_in_ignored_files(tmp: Path) -> str:
    """Two conf.d dirs that differ only in files the default --ignore-prefix
    (`_cluster_,_local_`) covers: a `_cluster_` file MODIFIED between them
    and a `_local_` file present in one only (added/removed). Each prefix of
    the default is needed, on a different diff path."""
    a, b = tmp / "drift-a", tmp / "drift-b"
    for d, value in ((a, "100"), (b, "200")):
        _write(d / "_defaults.yaml", "defaults:\n  max_connections: 100\n")
        _write(d / "tenant-one.yaml",
               "tenants:\n  tenant-one:\n    max_connections: '120'\n")
        _write(d / "_cluster_overrides.yaml", f"cluster_value: '{value}'\n")
    _write(a / "_local_only_here.yaml", "local_value: '1'\n")
    return f"{a},{b}"


def _silences(tmp: Path, name: str, alertname_of) -> str:
    """One silence per way of being (in)active: status active, expired,
    pending, and no status with an endsAt in the past (decided by timestamp).
    `alertname_of(id)` picks each silence's alertname — a gone alert makes
    it an orphan once in scope, a shipped one never does."""
    def silence(sid: str, state: str | None, ends: str) -> dict:
        s = {"id": sid, "createdBy": "gate", "comment": "verdict-flag probe",
             "matchers": [{"name": "alertname", "value": alertname_of(sid),
                           "isRegex": False, "isEqual": True}],
             "startsAt": "2026-01-01T00:00:00Z", "endsAt": ends}
        if state:
            s["status"] = {"state": state}
        return s
    return _write_str(tmp / f"silences-{name}.json", json.dumps([
        silence("active", "active", "2030-01-01T00:00:00Z"),
        silence("expired", "expired", "2026-01-02T00:00:00Z"),
        silence("pending", "pending", "2030-01-01T00:00:00Z"),
        silence("ended-by-timestamp", None, "2026-01-02T00:00:00Z"),
    ]))


def _orphan_ids(proc: subprocess.CompletedProcess) -> object:
    try:
        return sorted(o["silence_id"] for o in json.loads(proc.stdout)["orphans"])
    except (ValueError, KeyError, TypeError):
        return UNPARSABLE


def _a_shipped_alertname() -> str:
    """An alert a shipped rule pack really declares (read, not hardcoded)."""
    m = re.search(r"^\s*-?\s*alert:\s*([A-Za-z0-9_]+)",
                  (RULE_PACKS / "rule-pack-mariadb.yaml").read_text(encoding="utf-8"),
                  re.MULTILINE)
    assert m, "rule-pack-mariadb.yaml declares no alert"
    return m.group(1)


def _assembly(tmp: Path, malformed: bool) -> list[str]:
    """assemble_config_dir argv over one or two sources; the second carries
    a carrier whose top level is a list, which validates as an ERROR. Each
    call gets its own --output, so no run sees another's artifact."""
    name = "with-list" if malformed else "clean"
    a, b = tmp / f"asm-{name}-a", tmp / f"asm-{name}-b"
    _write(a / "tenant-one.yaml",
           'tenants:\n  tenant-one:\n    mysql_connections: "90"\n')
    sources = [a]
    if malformed:
        _write(b / "tenant-two.yaml", "- just\n- a list\n")
        sources.append(b)
    # Claimed by mkdir, not by counting: every call (one per run) gets an
    # output directory no earlier call has.
    n = 0
    while True:
        out = tmp / f"asm-out-{name}-{n}"
        try:
            out.mkdir(parents=True)
            break
        except FileExistsError:
            n += 1
    return ["--sources", ",".join(str(x) for x in sources),
            "--output", str(out), "--json"]


def _risk_and_rc(proc: subprocess.CompletedProcess) -> object:
    try:
        return (json.loads(proc.stdout)["tenants"][0]["risk_level"], proc.returncode)
    except (ValueError, KeyError, TypeError, IndexError):
        return UNPARSABLE


def _govern_confd(tmp: Path) -> str:
    """GOVERN_TENANTS, each in its own top-level file writing GOVERN_KEY."""
    d = tmp / "confd-govern"
    for tenant in GOVERN_TENANTS:
        _write(d / f"{tenant}.yaml", _govern_tenant_yaml(tenant))
    return str(d)


def _govern_argv(tmp: Path, stub: str, pending: bool = False) -> list[str]:
    """threshold_govern --apply against the stub, under a fresh run prefix
    (one per call, so each run's PUTs are counted on their own)."""
    run = uuid.uuid4().hex + (PENDING_SUFFIX if pending else "")
    return ["--config-dir", _govern_confd(tmp), "--prometheus", stub,
            "--apply", "--tenant-api-url", f"{stub}/ta/{run}",
            "--identity-groups", "threshold-governance",
            "--throttle-seconds", "0", "--json"]


def _prs_opened_puts_and_rc(proc: subprocess.CompletedProcess) -> object:
    """(outcomes reported pr_opened, PUTs the tenant-api actually received,
    exit code). The cap skips with exit 0, so the exit code alone cannot see
    it; and a cap applied after the PUT would still report `skipped`, so the
    PUTs the stub saw are counted too."""
    try:
        outcomes = json.loads(proc.stdout)["outcomes"]
        url = proc.args[proc.args.index("--tenant-api-url") + 1]
        run = url.rsplit("/ta/", 1)[1]
    except (ValueError, KeyError, TypeError, IndexError):
        return UNPARSABLE
    with _GOVERN_PUTS_LOCK:
        puts = GOVERN_PUTS.get(run, 0)
    return (sum(1 for o in outcomes if o["status"] == "pr_opened"), puts,
            proc.returncode)


# ═══════════════════════════════════════════════════════════════════════════
# Recipes
# ═══════════════════════════════════════════════════════════════════════════
Argv = Callable[[Path, str], list[str]]   # (tmp_path, stub_url) -> argv


@dataclass(frozen=True)
class Recipe:
    tool: str
    flag: str
    base: Argv                    # the fixture's argv, WITHOUT the flag
    flip: tuple[str, ...]         # flag (+ value) that must change the result
    boundary: tuple[str, ...]     # flag (+ value) that must NOT change it
    without: object = EXIT_OK     # must-fire result (no flag)
    with_: object = EXIT_VIOLATION   # result under `flip`
    boundary_base: Argv | None = None   # boundary on another fixture, if any
    note: str = ""
    # A second recipe for the same flag in another mode of the tool (the flag
    # can be read on one path and dropped on another); empty for the first.
    mode: str = ""
    # Reads the result from the run instead of its exit code (see docstring).
    observe: Callable[[subprocess.CompletedProcess], object] | None = None
    expected: tuple[int, ...] = field(default=(EXIT_OK, EXIT_VIOLATION), repr=False)

    def result(self, proc: subprocess.CompletedProcess) -> object:
        return proc.returncode if self.observe is None else self.observe(proc)

    @property
    def key(self) -> tuple[str, str]:
        return (self.tool, self.flag)

    @property
    def id(self) -> str:
        return f"{self.tool}{self.flag}" + (f"[{self.mode}]" if self.mode else "")


UNPARSABLE = "<unparsable --json report>"


def _json_status_and_rc(proc: subprocess.CompletedProcess) -> object:
    """The --json status AND the exit code: reading the report alone would
    pass a tool that reports the failure but exits 0 for it."""
    try:
        return (json.loads(proc.stdout)["status"], proc.returncode)
    except (ValueError, KeyError, TypeError):
        return UNPARSABLE


R = Recipe
RECIPES: list[Recipe] = [
    # Three tenants, one planned change each, a tenant-api that opens every
    # PR. The default cap 10 and a cap equal to the tenant count open all
    # three; a cap of 1 opens one, and the other two get no PUT at all.
    R("threshold_govern", "--max-prs",
      lambda t, s: _govern_argv(t, s),
      flip=("--max-prs", "1"), boundary=("--max-prs", str(len(GOVERN_TENANTS))),
      without=(3, 3, EXIT_OK), with_=(1, 1, EXIT_OK),
      observe=_prs_opened_puts_and_rc,
      note="3 actionable tenants: default 10 and 3 open 3 PRs, 1 opens 1 and "
           "PUTs only once; every run exits 0"),

    # The cap counts NEW PRs only (threshold_govern: "dedup skips / errors
    # don't consume the alert-fatigue budget"). tenant-one is already pending
    # (409), so a cap of 1 still opens a PR for the next tenant; a cap of 2
    # opens both others, as the default does.
    R("threshold_govern", "--max-prs", mode="one tenant already pending",
      base=lambda t, s: _govern_argv(t, s, pending=True),
      flip=("--max-prs", "1"), boundary=("--max-prs", "2"),
      without=(2, 3, EXIT_OK), with_=(1, 2, EXIT_OK),
      observe=_prs_opened_puts_and_rc,
      note="409 on tenant-one does not use the cap: cap 1 -> 1 new PR after "
           "2 PUTs; cap 2 and the default -> 2 new PRs after 3 PUTs"),
    # #2494's regression. New is 5 % above old: the 0.1 % default rejects it,
    # 10 % accepts it, 1 % still rejects it.
    R("validate_migration", "--tolerance",
      lambda t, s: ["--prometheus", s, "--old", "migration_old",
                    "--new", "migration_new", "-o", str(t / "out")],
      flip=("--tolerance", "0.1"), boundary=("--tolerance", "0.01"),
      without=EXIT_VIOLATION, with_=EXIT_OK,
      note="5 % drift: default 0.001 and 0.01 reject it, 0.1 accepts it"),

    # ADR-007 domain-policy violation: WARN without --strict, FAIL with it.
    # Boundary: the same tree under a policy the tenant does not violate.
    R("validate_config", "--strict",
      lambda t, s: ["--config-dir", _confd_with_policy(t, "slack")],
      flip=("--strict",), boundary=("--strict",),
      boundary_base=lambda t, s: ["--config-dir",
                                  _confd_with_policy(t, "pagerduty")],
      note="forbidden slack receiver: WARN -> FAIL; no violation stays 0. "
           "With amtool accepting, the boundary tree has no WARN at all, so "
           "a --strict that escalated every WARN would still pass here"),

    # --min-score is read only under --ci (it gates the same line as BAD
    # alerts). The stub's tenant scores 75 with no BAD alert: 80 fails it,
    # 70 does not.
    R("alert_quality", "--min-score",
      lambda t, s: ["--prometheus", s, "--alertmanager", s, "--ci"],
      flip=("--min-score", "80"), boundary=("--min-score", "70"),
      note="score 75, no BAD alert: min 80 fails, min 70 and the 0 default pass"),

    # Watch mode, 3 rounds, every round matching. Converged = the last
    # `window` rounds all match, so the default 5 cannot converge in 3
    # rounds (exit 1), 2 does (exit 0), 4 still cannot.
    R("validate_migration", "--stability-window",
      lambda t, s: ["--prometheus", s, "--old", "steady_old",
                    "--new", "steady_new", "-o", str(t / "out"),
                    "--watch", "--interval", "0", "--rounds", "3",
                    "--auto-detect-convergence"],
      flip=("--stability-window", "2"), boundary=("--stability-window", "4"),
      without=EXIT_VIOLATION, with_=EXIT_OK,
      note="3 matching rounds: window 5 (default) and 4 never converge, 2 does"),

    # #1828's tool. A carrier whose top level is a list assembles fine and
    # is caught only by --validate, whose documented exit for it is 2 — so
    # the verdict is read from the --json status, not the exit code.
    R("assemble_config_dir", "--validate",
      lambda t, s: _assembly(t, malformed=True),
      flip=("--validate",), boundary=("--validate",),
      boundary_base=lambda t, s: _assembly(t, malformed=False),
      without=("ok", EXIT_OK), with_=("validation_error", EXIT_CALLER_ERROR),
      observe=_json_status_and_rc,
      note="list-shaped carrier: (ok, 0) -> (validation_error, 2) — exit 2 is "
           "the documented validation-error code (#1831, owner decision); "
           "a clean assembly stays (ok, 0)"),

    # Render mode replaces an unreadable group_wait with the default and
    # exits 0; --validate counts that replacement as blocking.
    R("generate_alertmanager_routes", "--validate",
      lambda t, s: ["--config-dir", _confd_with_group_wait(t, "5x")],
      flip=("--validate",), boundary=("--validate",),
      boundary_base=lambda t, s: ["--config-dir", _confd_with_group_wait(t, "30s")],
      note="group_wait 5x: render 0, --validate 1; group_wait 30s validates"),

    # Same ADR-007 fixture as validate_config --strict: the generator's own
    # --strict must fail on the violation it otherwise only warns about.
    R("generate_alertmanager_routes", "--strict",
      lambda t, s: ["--config-dir", _confd_with_policy(t, "slack")],
      flip=("--strict",), boundary=("--strict",),
      boundary_base=lambda t, s: ["--config-dir",
                                  _confd_with_policy(t, "pagerduty")],
      note="forbidden slack receiver: WARN -> exit 1; no violation stays 0"),

    # --validate mode: there the policy violation reaches the verdict only
    # through load_tenant_tree(strict_policies=...), a different path from
    # render mode's `if args.strict` block above.
    R("generate_alertmanager_routes", "--strict", mode="with --validate",
      base=lambda t, s: ["--config-dir", _confd_with_policy(t, "slack"), "--validate"],
      flip=("--strict",), boundary=("--strict",),
      boundary_base=lambda t, s: ["--config-dir",
                                  _confd_with_policy(t, "pagerduty"), "--validate"],
      note="--validate alone passes the violation as WARN (0); --strict fails it"),

    R("generate_tenant_mapping_rules", "--validate", mode="with --dry-run",
      base=lambda t, s: ["--config-dir",
                         _confd_with_instance_mapping(t, "tenant-ghost"),
                         "--metrics", "m1", "--dry-run"],
      flip=("--validate",), boundary=("--validate",),
      boundary_base=lambda t, s: ["--config-dir",
                                  _confd_with_instance_mapping(t, "tenant-one"),
                                  "--metrics", "m1", "--dry-run"],
      note="--dry-run has its own exit for a failed validation; same verdicts"),

    # --min-score filters which pair scores a cluster reports (score_cluster:
    # "pairs exceeding min_score"); it does NOT decide the --ci verdict, which
    # reads the root cause's severity only. Observed through the pair scores.
    # The pair scores 0.8, so both values sit inside the flag's 0..1 range
    # and the boundary lands exactly on the `>=`.
    R("alert_correlate", "--min-score",
      lambda t, s: ["--input", _correlated_alerts(t), "--json"],
      flip=("--min-score", "0.9"), boundary=("--min-score", "0.8"),
      without=[0.8], with_=[0.0], observe=_avg_correlations,
      note="one pair scoring 0.8: default 0.3 and 0.8 (>=) keep it, 0.9 drops "
           "it; the flag does not change the --ci verdict"),

    # Two job names in one comma list, and a boundary that names the same
    # services by db_type instead of job: excluding is by job name.
    R("blind_spot_discovery", "--exclude-jobs",
      lambda t, s: ["--prometheus", s, "--config-dir", _mysql_only_confd(t),
                    "--json-output"],
      flip=("--exclude-jobs", "postgres-exporter,redis-exporter"),
      boundary=("--exclude-jobs", "redis,postgresql"),
      without=["postgresql", "redis"], with_=[], observe=_blind_spots,
      note="unmonitored redis + postgres: excluding both jobs clears them; "
           "their db_type names exclude nothing"),

    # The flag's DEFAULT does the work: both of its prefixes are needed, one
    # on the modified path and one on the added/removed path. Configmap mode
    # only — operator mode needs a cluster (kubectl) and is not covered here.
    R("drift_detect", "--ignore-prefix",
      lambda t, s: ["--dirs", _dirs_drifting_in_ignored_files(t),
                    "--mode", "configmap", "--ci"],
      flip=("--ignore-prefix", "_cluster_"),
      boundary=("--ignore-prefix", "_local_,_cluster_"),
      note="drift only in a modified _cluster_ file and a one-sided _local_ "
           "file: default and the reversed pair ignore both (0); _cluster_ "
           "alone reports the _local_ file (1)"),

    R("silencer_drift_check", "--include-inactive",
      lambda t, s: ["--silences-file",
                    _silences(t, "gone", lambda sid: f"GoneAlert_{sid.replace('-', '_')}"),
                    "--rule-source", str(RULE_PACKS), "--json"],
      flip=("--include-inactive",), boundary=("--include-inactive",),
      boundary_base=lambda t, s: [
          "--silences-file",
          _silences(t, "inactive-live", lambda sid: "GoneAlert_active" if sid == "active"
                    else _a_shipped_alertname()),
          "--rule-source", str(RULE_PACKS), "--json"],
      without=["active"],
      with_=["active", "ended-by-timestamp", "expired", "pending"],
      observe=_orphan_ids,
      note="silences on gone alerts: only the active one is in scope by "
           "default; --include-inactive adds expired, pending and the one "
           "ended by timestamp, and keeps the active one. Boundary: the same "
           "flag over inactive silences on shipped alerts adds no orphan and "
           "still keeps the active one"),

    # 20 days to the limit. classify_risk: critical when <= warn_days,
    # warning when <= 3 * warn_days. Default 7 -> warning (--ci 0); 25 ->
    # critical (1); 15 -> still warning (0).
    R("cardinality_forecasting", "--warn-days",
      lambda t, s: ["--prometheus", s, "--limit", "1000", "--ci", "--json"],
      flip=("--warn-days", "20"), boundary=("--warn-days", "19"),
      without=("warning", EXIT_OK), with_=("critical", EXIT_VIOLATION),
      observe=_risk_and_rc,
      note="exactly 20 days to the limit: critical needs warn_days >= 20 "
           "(`<=`), so 20 flips it and 19 does not"),

    # The warning band is 3 * warn_days and never reaches the --ci exit, so
    # it is observed through the reported risk level.
    R("cardinality_forecasting", "--warn-days", mode="warning band",
      base=lambda t, s: ["--prometheus", s, "--limit", "1000", "--ci", "--json"],
      flip=("--warn-days", "6"), boundary=("--warn-days", "7"),
      without=("warning", EXIT_OK), with_=("safe", EXIT_OK),
      observe=_risk_and_rc,
      note="20 days to the limit: within 3*7 = 21 (warning), beyond 3*6 = 18 "
           "(safe; 400 is under 80 % of 1000)"),

    R("generate_tenant_mapping_rules", "--validate",
      lambda t, s: ["--config-dir", _confd_with_instance_mapping(t, "tenant-ghost"),
                    "--metrics", "m1"],
      flip=("--validate",), boundary=("--validate",),
      boundary_base=lambda t, s: ["--config-dir",
                                  _confd_with_instance_mapping(t, "tenant-one"),
                                  "--metrics", "m1"],
      note="mapping onto an undeclared tenant: 0 -> 1; a declared one stays 0"),
]


# ═══════════════════════════════════════════════════════════════════════════
# Coverage gate
# ═══════════════════════════════════════════════════════════════════════════
def _buckets() -> dict[str, set[tuple[str, str]]]:
    return {
        "RECIPES": {r.key for r in RECIPES},
        "SAME_NAME_DIFFERENT_MEANING": set(SAME_NAME_DIFFERENT_MEANING),
        "PENDING": set(PENDING),
    }


def test_population_is_not_empty():
    """An enumerator that finds nothing would make every check below pass."""
    assert ("validate_migration", "--tolerance") in POPULATION, sorted(POPULATION)


def test_every_verdict_flag_is_gated_or_explained():
    buckets = _buckets()
    covered = set().union(*buckets.values())
    uncovered = sorted(POPULATION - covered)
    assert not uncovered, (
        f"{len(uncovered)} verdict-named flag(s) have no recipe and no "
        f"SAME_NAME_DIFFERENT_MEANING entry: {uncovered}. PENDING is closed "
        f"to new flags (#2908) — add a recipe in this PR.")
    stale = {name: sorted(keys - POPULATION) for name, keys in buckets.items()
             if keys - POPULATION}
    assert not stale, f"entries name flags that are no longer declared: {stale}"


def test_each_flag_is_in_exactly_one_bucket():
    buckets = _buckets()
    names = list(buckets)
    clashes = {
        f"{a} & {b}": sorted(buckets[a] & buckets[b])
        for i, a in enumerate(names) for b in names[i + 1:]
        if buckets[a] & buckets[b]
    }
    assert not clashes, clashes
    keys = [(r.key, r.mode) for r in RECIPES]
    assert len(keys) == len(set(keys)), f"duplicate recipes: {keys}"


def test_pending_only_shrinks():
    """PENDING may only hold flags that existed when the gate landed, and
    each entry must point at #2908."""
    grown = sorted(set(PENDING) - POPULATION_AT_LANDING)
    assert not grown, (
        f"PENDING gained flag(s) the landing population did not have: "
        f"{grown}. A new verdict flag needs a recipe, not a PENDING row.")
    assert all("#2908" in why for why in PENDING.values()), PENDING


def test_landing_record_is_unchanged():
    """The record PENDING is measured against is pinned by digest: growing
    it to park a new flag in PENDING cannot go unnoticed."""
    lines = "\n".join(f"{t} {f}" for t, f in sorted(POPULATION_AT_LANDING))
    digest = hashlib.sha256(lines.encode("utf-8")).hexdigest()
    assert len(POPULATION_AT_LANDING) == 15, len(POPULATION_AT_LANDING)
    assert digest == POPULATION_AT_LANDING_SHA256, (
        "POPULATION_AT_LANDING changed. It records what #2908 measured when "
        "this gate landed; a new flag needs a recipe, not a landing row.")


def test_every_add_argument_option_is_a_literal():
    """An option string built at runtime is invisible to this enumeration;
    refuse it rather than miss a flag."""
    opaque = [f"{p.relative_to(REPO_ROOT).as_posix()}:{c.lineno}"
              for p in _tool_files() + _helper_files()
              for c in _add_argument_calls(p) if _option_strings(c) is None]
    assert not opaque, (
        f"add_argument with a non-literal option string: {opaque}. Spell the "
        f"option strings as literals, or teach collect_verdict_flags the shape.")


def test_no_shared_helper_declares_a_verdict_flag():
    """A verdict flag declared in a _lib helper reaches tools this walk
    cannot name; model the helper (map it to its callers) before adding one."""
    hidden = [f"{p.name}: {opt}" for p in _helper_files()
              for c in _add_argument_calls(p) for opt in (_option_strings(c) or [])
              if VERDICT_FLAG_RE.match(opt)]
    assert not hidden, f"verdict-named flags declared in shared helpers: {hidden}"


def test_the_enumeration_sees_underscore_clis():
    names = {p.name for p in _tool_files()}
    assert "_federation_revocation_reconciler.py" in names, sorted(names)


def test_same_name_entries_carry_a_reason():
    empty = [k for k, why in SAME_NAME_DIFFERENT_MEANING.items() if not why.strip()]
    assert not empty, f"SAME_NAME_DIFFERENT_MEANING entries without a reason: {empty}"


def test_explain_route_severity_stays_a_trace_input():
    """Keeps the explain_route --severity entry honest: every read of
    args.severity is an argument of a trace_alert_routing(...) call, and the
    tool never exits with EXIT_VIOLATION. If either stops being true, the
    flag may have become a verdict input and needs a recipe."""
    path = OPS_DIR / "explain_route.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    reads = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "severity"
             and isinstance(n.value, ast.Name) and n.value.id == "args"]
    trace_args = {id(a) for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == "trace_alert_routing"
                  for a in [*n.args, *(k.value for k in n.keywords)]}
    assert reads, "explain_route no longer reads args.severity"
    stray = [n.lineno for n in reads if id(n) not in trace_args]
    assert not stray, f"args.severity read outside trace_alert_routing(...): lines {stray}"
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "EXIT_VIOLATION" not in names, (
        "explain_route now uses EXIT_VIOLATION — its --severity may decide a "
        "verdict; re-check the SAME_NAME_DIFFERENT_MEANING entry")


def test_known_noop_entries_are_recipes():
    stale = sorted(set(KNOWN_FLAG_NOOP) - {r.key for r in RECIPES})
    assert not stale, f"KNOWN_FLAG_NOOP names non-recipe flag(s): {stale}"


def test_recipes_expect_a_real_flip():
    """A recipe whose two expectations are equal could pass on a no-op flag,
    and an expected 2 could be met by a caller error or a crash."""
    for r in RECIPES:
        assert r.without != r.with_, f"{r.id}: without == with_ ({r.without})"
        if r.observe is None:
            assert {r.without, r.with_} <= set(r.expected), (
                f"{r.id}: expectations must be EXIT_OK / EXIT_VIOLATION, "
                f"got {r.without} -> {r.with_}")
        else:
            assert UNPARSABLE not in (r.without, r.with_), r.id
        assert r.flag in r.flip and r.flag in r.boundary, r.id


# ═══════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════
def _run(tool: str, argv: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(OPS_DIR / f"{tool}.py"), *argv],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=TIMEOUT_S, cwd=str(cwd), env=env)


LANGS = ("zh", "en")


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("recipe", [pytest.param(r, id=r.id) for r in RECIPES])
def test_verdict_flag_changes_the_result(recipe: Recipe, lang: str, tmp_path: Path,
                                         stub_url: str, amtool_accepts: Path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = dict(os.environ)   # PATH already starts with amtool_accepts
    env["PYTHONIOENCODING"] = "utf-8"
    env["DA_LANG"] = lang
    env.pop("PROMETHEUS_URL", None)
    env.pop("ALERTMANAGER_URL", None)

    # Each run builds its own fixture argv, so a builder that hands out a
    # fresh output path per call gives every run its own artifact.
    boundary_base = recipe.boundary_base or recipe.base
    runs = {
        "must-fire (no flag)": (recipe.base(tmp_path, stub_url), recipe.without),
        f"flip ({' '.join(recipe.flip)})":
            (recipe.base(tmp_path, stub_url) + list(recipe.flip), recipe.with_),
        f"boundary ({' '.join(recipe.boundary)})":
            (boundary_base(tmp_path, stub_url) + list(recipe.boundary), recipe.without),
    }
    results = {label: _run(recipe.tool, argv, cwd, env)
               for label, (argv, _) in runs.items()}

    def report() -> str:
        lines = [f"\nverdict-flag contract: {recipe.tool}.py {recipe.flag}",
                 f"  note: {recipe.note}"]
        for label, (argv, want) in runs.items():
            p = results[label]
            lines.append(f"  {label}: want {want!r}, got {recipe.result(p)!r} "
                         f"(rc {p.returncode})  argv={argv}")
            lines.append(f"    stdout[-300:]={p.stdout[-300:]!r}")
            lines.append(f"    stderr[-300:]={p.stderr[-300:]!r}")
        return "\n".join(lines)

    for label, p in results.items():
        assert "Traceback (most recent call last)" not in p.stderr, (
            f"{label} run crashed" + report())

    if recipe.key in KNOWN_FLAG_NOOP:
        must_fire, flip = list(results.values())[:2]
        assert recipe.result(must_fire) == recipe.without, (
            "KNOWN_FLAG_NOOP recipe: the must-fire run itself is off" + report())
        assert recipe.result(flip) == recipe.result(must_fire), (
            f"{recipe.id} is listed in KNOWN_FLAG_NOOP but the flag now changes "
            f"the result — remove the entry" + report())
        pytest.xfail(f"known no-op flag: {KNOWN_FLAG_NOOP[recipe.key]}")

    for label, (_, want) in runs.items():
        assert recipe.result(results[label]) == want, (
            f"{label} run: the flag did not decide the verdict" + report())
