"""`--ci` fail-on-finding contract gate for da-tools ops CLI tools.

THE CONTRACT
------------
dev-rules §13: Python tools use ``--ci`` to control fail-on-finding — the
same run that reports a finding as a WARNING (exit 0) without the flag must
exit 1 (``EXIT_VIOLATION``) with it.  This is the interface customer CI
pipelines consume: a ``--ci`` that silently no-ops is FAIL-OPEN — the
pipeline stays green while the platform drifts — and, like the #1112
cardinality crash, only a behaviour gate can see it.

The observable form of the contract: each recipe builds ONE fixture that is
guaranteed to produce a finding, then runs the tool TWICE on it — once
without ``--ci`` and once with it, the flag being the only argv difference —
and asserts both documented exit codes.  THE FLIP IS THE ASSERTION: equal
exit codes on both runs would mean the flag changed nothing.

Exit expectations are pinned per tool from its CURRENT documented semantics
(read from the source before pinning, not assumed):

* 12 of the 14 in-scope tools: finding => exit 0 without ``--ci``, exit 1
  (EXIT_VIOLATION) with it — the canonical fail-on-finding upgrade.
* ``gitops_check`` documents the INVERSE: without ``--ci`` anything other
  than "pass" exits 1; with ``--ci`` warnings exit 0 and only "fail" exits 1
  (help text: 「CI 模式（警告也退出碼 0，僅失敗退出碼 1）」).  Its recipe drives a
  deterministic WARN (sidecar check with kubectl guaranteed absent via a
  stripped PATH) and asserts 1 -> 0.  Still a flip; still proves the flag is
  live.
* ``state_reconcile``'s ``--ci`` only gates in ``--dry-run`` mode (pending
  migrations / manifest changes are "would-do" items; in apply mode they are
  applied, so nothing is pending).  Its recipe therefore carries
  ``--dry-run`` on both runs.

A vacuous pass is guarded three ways: the no-``--ci`` run must exit its
exact documented code (a deterministic crash exits 1 there and is caught),
both runs must produce a non-empty stdout report, and a tool whose two runs
exit identically fails the flip assertion outright.

SCOPE — WHAT COUNTS AS A --ci TOOL
----------------------------------
Argparse-based, mirroring the sibling --dry-run gate: an ops tool is in
scope iff it *declares* ``add_argument("--ci")`` (either quote style).
15 tools declare it today; ``test_recipe_table_covers_every_ci_tool`` pins
the count.

One declared flag is the SAME NAME with a DIFFERENT MEANING and is held in
``SAME_NAME_DIFFERENT_MEANING`` rather than given a recipe:

* ``init_project`` — its ``--ci`` is ``choices=['github','gitlab','both']``:
  "which CI config files to scaffold", not fail-on-finding.  dev-rules §13
  lists it as a documented exception alongside diag_pr_ci / tenant-verify.
  ``test_same_name_allowlist_is_honest`` asserts the declaration is still
  choices-shaped (NOT store_true), so if it ever morphs into a real
  fail-on-finding flag it must move into RECIPES; a companion assertion
  keeps the dev-rules §13 exception line naming it.

One file MENTIONS ``--ci`` without declaring it (``MENTION_ONLY_ALLOWLIST``):

* ``validate_config`` — forwards ``--ci`` to its ``lint_custom_rules``
  subprocess (check_custom_rules); declares no flag of its own.

SCOPE — HONEST BOUNDARIES
-------------------------
1. **One finding-fixture per tool, 2 subprocesses per tool** (28 launches
   total) — runtime discipline per the sibling gates.  The finding *class*
   driven is one of possibly several the tool gates on (e.g. runtime_audit
   is driven via MISSING, not UNHEALTHY/ORPHAN; silencer_drift_check via
   orphans, not malformed) — the gate proves the flag flips, not that every
   finding class is wired to it.
2. **alert_quality**'s finding is a below-threshold score (``--min-score
   101`` — overall_score is 0..100, so the threshold miss is guaranteed),
   not a BAD-rated alert: rating an alert BAD needs a crafted Prometheus
   rules payload for marginal extra signal over the same gate line
   (``bad_count > 0 or score < args.min_score``).
3. **gitops_check** is asserted on the warn->0 side of its inverse contract
   only; the fail->1 side shares the same exit expression
   (``EXIT_OK if overall_status != "fail" else EXIT_VIOLATION``) and is not
   separately driven (would cost a third subprocess).
4. **operator_check** rides the fake-kubectl shim (empty resource lists =>
   genuine "fail" findings with ``caller_error=False``) and therefore skips
   on Windows, where CreateProcess resolves ``kubectl`` to ``kubectl.exe``
   only and bypasses the shim — same rationale, wording and authority
   (POSIX CI) as the sibling gates.
5. **No tool source is modified.**  A --ci found to no-op is a REAL BUG
   (fail-open); it is listed in ``KNOWN_CI_NOOP`` and xfail(strict=True),
   never fixed here.
6. **Scope is ops/ only** — 76 further da-tools declare a ``--ci`` flag
   (dx ×7, lint ×69) and are NOT covered here (W6b blind-review F1; same
   ops-only line as the --json / dry-run / bilingual-help siblings).  The
   customer-CI-pipeline consumption this contract protects is the ops
   surface; dx/lint are developer-side.  Note ``dx/pr_preflight.py``'s
   ``--ci`` IS genuine fail-on-finding semantics — extending scope there
   is a separate decision, not an accident of omission.

FIRST-RUN RESULT (2026-07-18)
-----------------------------
All 14 recipes flipped on first full run — ``KNOWN_CI_NOOP`` is empty.  The
flip assertion is self-mutation-checked by construction: a no-op flag
produces equal exit codes and the parametrized test fails, so the green is
not vacuous.

HARNESS REUSE
-------------
The HTTP stub personalities, fixture builders (webhook conf.d, silences
JSON, runtime-rules JSON) and the fake-kubectl shim are imported from
``test_json_stdout_contract`` — zero changes to that gate.  The OPA
personality is extended by SUBCLASSING the stub handler here (the base stub
answers ``/v1/data`` with zero violations, which can never flip
policy_opa_bridge).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

import pytest

import test_json_stdout_contract as _jsc

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "tools" / "ops"
DEV_RULES_MD = REPO_ROOT / "docs" / "internal" / "dev-rules.md"

RULE_PACKS = REPO_ROOT / "rule-packs"
SEED_CONF_D = REPO_ROOT / "try-local" / "seed" / "conf.d"

TIMEOUT_S = 90

# scripts/tools/_lib_exitcodes.py is the SSOT; test_tool_exit_codes.py pins it.
EXIT_OK = 0
EXIT_VIOLATION = 1

# Reuse the fake-kubectl shim fixture from the --json gate (importing the
# fixture function into this module's namespace registers it with pytest).
from test_json_stdout_contract import fake_kubectl_dir  # noqa: E402,F401

# ═══════════════════════════════════════════════════════════════════════════
# Scope discovery — argparse-declared --ci flags only
# ═══════════════════════════════════════════════════════════════════════════
CI_FLAG_RE = re.compile(r"""add_argument\(\s*['"]--ci['"]""")
CI_MENTION_RE = re.compile(r"--ci\b")
# init_project's --ci must stay choices-shaped to remain allowlisted.
CI_CHOICES_RE = re.compile(r"""add_argument\(\s*['"]--ci['"],\s*choices=""")

# Same flag NAME, different MEANING — documented exceptions, no recipe.
SAME_NAME_DIFFERENT_MEANING: dict[str, str] = {
    "init_project": (
        "--ci is choices=['github','gitlab','both'] — selects which CI "
        "config files to scaffold, not fail-on-finding; listed as a "
        "documented exception in dev-rules §13"
    ),
}

# Files that MENTION --ci without DECLARING it.
MENTION_ONLY_ALLOWLIST: dict[str, str] = {
    "validate_config": (
        "forwards --ci to its lint_custom_rules subprocess "
        "(check_custom_rules); declares no --ci flag of its own"
    ),
}


def _ops_sources() -> dict[str, str]:
    return {
        f.stem: f.read_text(encoding="utf-8")
        for f in sorted(OPS_DIR.glob("*.py"))
        if not f.name.startswith("_") and f.name != "__init__.py"
    }


def collect_ci_tools() -> list[str]:
    """Every ops tool that argparse-declares a `--ci` flag."""
    return [name for name, src in _ops_sources().items()
            if CI_FLAG_RE.search(src)]


CI_TOOLS = collect_ci_tools()


# ═══════════════════════════════════════════════════════════════════════════
# Known fail-open bugs — a --ci observed to no-op at gate-landing time.
# Listed, NOT fixed: the fix is a behaviour change for the owner to schedule.
# Keys are tool names; presence ⇒ that tool's recipe is xfail(strict=True).
# ═══════════════════════════════════════════════════════════════════════════
KNOWN_CI_NOOP: dict[str, str] = {
    # EMPTY at gate-landing time (2026-07-18): all 14 in-scope fail-on-finding
    # tools flipped their exit code on the first full run.
}


# ═══════════════════════════════════════════════════════════════════════════
# Stub server — the --json gate's handler, plus an OPA personality that
# actually RETURNS violations (the base stub's /v1/data answer is an empty
# result, which can never flip policy_opa_bridge).
# ═══════════════════════════════════════════════════════════════════════════
class _CiStubHandler(_jsc._StubHandler):
    def _respond(self):  # noqa: D102
        if self.path.startswith("/v1/data"):
            body = json.dumps({"result": [{
                "msg": "tenant must declare an owner",
                "severity": "error",
                "tenant": "tenant-one",
                "field": "_metadata.owner",
            }]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super()._respond()


@pytest.fixture(scope="session")
def stub_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _CiStubHandler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ═══════════════════════════════════════════════════════════════════════════
# Finding fixtures — each guarantees the tool reports at least one finding
# ═══════════════════════════════════════════════════════════════════════════
def _write(p: Path, text: str) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p)


def _critical_alerts_json(tmp: Path) -> str:
    """Two still-firing critical alerts 60 s apart => one cluster whose
    root cause is critical (alert_correlate's --ci gate condition)."""
    return _write(tmp / "critical-alerts.json", json.dumps([
        {
            "labels": {"alertname": "DiskPressureHigh", "tenant": "tenant-one",
                       "namespace": "monitoring", "severity": "critical"},
            "startsAt": "2026-07-01T00:00:00Z", "endsAt": "",
        },
        {
            "labels": {"alertname": "DiskPressureSpill", "tenant": "tenant-two",
                       "namespace": "monitoring", "severity": "critical"},
            "startsAt": "2026-07-01T00:01:00Z", "endsAt": "",
        },
    ]))


def _denied_function_rules_dir(tmp: Path) -> str:
    """A custom-rules dir using a denied PromQL function => lint ERROR."""
    d = tmp / "custom-rules"
    _write(d / "bad-rules.yaml", (
        "groups:\n"
        "  - name: tenant-custom\n"
        "    rules:\n"
        "      - alert: ForecastBreach\n"
        "        expr: holt_winters(node_filesystem_avail_bytes[1h], 0.5, 0.5) < 0\n"
        "        for: 5m\n"
        "        labels:\n"
        "          severity: warning\n"
        "          tenant: tenant-one\n"
        "        annotations:\n"
        "          summary: forecasted breach\n"
    ))
    return str(d)


def _drifting_dir_pair(tmp: Path) -> tuple[str, str]:
    """Two conf.d dirs whose tenant file differs => unexpected drift."""
    a, b = tmp / "cluster-a", tmp / "cluster-b"
    _write(a / "_defaults.yaml", "defaults:\n  max_connections: '100'\n")
    _write(b / "_defaults.yaml", "defaults:\n  max_connections: '100'\n")
    _write(a / "tenant-one.yaml", "tenants:\n  tenant-one:\n    max_connections: '120'\n")
    _write(b / "tenant-one.yaml", "tenants:\n  tenant-one:\n    max_connections: '80'\n")
    return str(a), str(b)


def _ownerless_confd(tmp: Path) -> str:
    """A conf.d whose tenant declares NO _metadata.owner => the
    metadata-owner-required policy (severity error) must fire."""
    d = tmp / "ownerless-confd"
    _write(d / "_defaults.yaml", "defaults:\n  max_connections: '100'\n")
    _write(d / "tenant-one.yaml", "tenants:\n  tenant-one:\n    max_connections: '50'\n")
    return str(d)


def _empty_dir(tmp: Path, name: str) -> str:
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _kubectl_free_path(tmp: Path) -> str:
    """A PATH value guaranteed to contain no kubectl (one empty dir)."""
    return _empty_dir(tmp, "no-kubectl-on-path")


# ═══════════════════════════════════════════════════════════════════════════
# Recipes — one finding-fixture per in-scope tool; the test runs each argv
# twice (with/without --ci appended).
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Recipe:
    tool: str
    build: Callable[[Path, str], list[str]]   # (tmp_path, stub_url) -> argv WITHOUT --ci
    # Documented exit codes for the SAME fixture (source-read, not assumed).
    no_ci_exit: int = EXIT_OK
    ci_exit: int = EXIT_VIOLATION
    needs_kubectl: bool = False               # fake-kubectl shim (POSIX-only)
    strip_kubectl_path: bool = False          # gitops_check sidecar: force warn
    note: str = ""                            # semantics note for the reader

    @property
    def id(self) -> str:
        return self.tool


R = Recipe
RECIPES: list[Recipe] = [
    R("alert_correlate",
      lambda t, s: ["--input", _critical_alerts_json(t)],
      note="critical-root-cause cluster; 0 -> 1"),

    # HONEST BOUNDARIES §2: the guaranteed finding is the threshold miss
    # (overall_score <= 100 < 101), driving the same gate line as bad_count.
    R("alert_quality",
      lambda t, s: ["--prometheus", s, "--alertmanager", s, "--min-score", "101"],
      note="score < --min-score 101 is guaranteed; 0 -> 1"),

    # /rich matrix puts tenant cardinality at 12; --limit 5 => already over
    # the limit => classify_risk() = critical.
    R("cardinality_forecasting",
      lambda t, s: ["--prometheus", s + "/rich", "--limit", "5"],
      note="current 12 >= limit 5 => critical risk; 0 -> 1"),

    R("drift_detect",
      lambda t, s: (lambda pair: ["--dirs", f"{pair[0]},{pair[1]}",
                                  "--mode", "configmap"])(_drifting_dir_pair(t)),
      note="modified tenant yaml = unexpected drift; 0 -> 1"),

    # INVERSE contract (module docstring): warn exits 1 WITHOUT --ci and 0
    # WITH it. The warn is deterministic: PATH holds one empty dir, kubectl
    # cannot resolve, check_sidecar() returns status="warn".
    R("gitops_check",
      lambda t, s: ["sidecar"],
      no_ci_exit=EXIT_VIOLATION, ci_exit=EXIT_OK, strip_kubectl_path=True,
      note="documented inverse: warn => 1 -> 0 (only 'fail' exits 1 in CI mode)"),

    R("lint_custom_rules",
      lambda t, s: [_denied_function_rules_dir(t)],
      note="denied function holt_winters => lint ERROR; 0 -> 1"),

    # Webhook receiver at a dead endpoint (port 1) => the live send fails.
    R("notification_tester",
      lambda t, s: ["--config-dir", _jsc._confd_with_webhook(t, "http://127.0.0.1:1")],
      note="webhook send to dead endpoint fails; 0 -> 1"),

    # Fake kubectl serves empty resource lists => PrometheusRule loaded=0 with
    # expected>0 ("fail") and ServiceMonitor missing ("fail"), caller_error
    # stays False => genuine findings. POSIX-only (HONEST BOUNDARIES §4).
    R("operator_check",
      lambda t, s: ["--rule-packs-dir", str(RULE_PACKS),
                    "--config-dir", str(SEED_CONF_D)],
      needs_kubectl=True,
      note="0/N PrometheusRule + missing ServiceMonitor; 0 -> 1"),

    R("policy_engine",
      lambda t, s: ["--config-dir", _ownerless_confd(t),
                    "--policy", _jsc._policy_file(t)],
      note="required _metadata.owner absent => error violation; 0 -> 1"),

    # _CiStubHandler answers /v1/data with one severity=error violation.
    R("policy_opa_bridge",
      lambda t, s: ["--config-dir", str(SEED_CONF_D), "--opa-url", s],
      note="OPA returns an error-level violation; 0 -> 1"),

    R("rule_pack_diff",
      lambda t, s: ["--from", str(RULE_PACKS / "rule-pack-mariadb.yaml"),
                    "--to", str(RULE_PACKS / "rule-pack-redis.yaml")],
      note="cross-pack diff has removed/breaking alerts; 0 -> 1"),

    R("runtime_audit",
      lambda t, s: ["--rule-packs-dir", str(RULE_PACKS),
                    "--runtime-json", _jsc._runtime_rules_json(t)],
      note="declared packs vs empty runtime => MISSING findings; 0 -> 1"),

    R("silencer_drift_check",
      lambda t, s: ["--silences-file", _jsc._silences_json(t),
                    "--rule-source", str(RULE_PACKS)],
      note="silence matcher on a nonexistent alertname => orphan; 0 -> 1"),

    # --ci only gates PENDING work, which exists only under --dry-run
    # (module docstring); fresh state dir + absent manifest => pending
    # manifest_change on both runs, nothing written on either.
    R("state_reconcile",
      lambda t, s: ["--state-dir", _empty_dir(t, "state"),
                    "--manifest-path", str(t / "manifest.json"), "--dry-run"],
      note="pending manifest rebuild under --dry-run; 0 -> 1"),
]


# ═══════════════════════════════════════════════════════════════════════════
# Meta-tests — keep the scope honest
# ═══════════════════════════════════════════════════════════════════════════
def test_fixture_paths_exist():
    missing = [str(p) for p in (RULE_PACKS, SEED_CONF_D,
                                RULE_PACKS / "rule-pack-mariadb.yaml",
                                RULE_PACKS / "rule-pack-redis.yaml")
               if not p.exists()]
    assert not missing, f"gate fixtures missing from the repo: {missing}"


def test_recipe_table_covers_every_ci_tool():
    """A tool that grows --ci must gain a recipe (or a reasoned allowlist
    entry), or this gate rots."""
    covered = {r.tool for r in RECIPES} | set(SAME_NAME_DIFFERENT_MEANING)
    uncovered = sorted(set(CI_TOOLS) - covered)
    stale = sorted(covered - set(CI_TOOLS))
    assert not uncovered, (
        f"{len(uncovered)} tool(s) declare --ci but have neither a recipe nor "
        f"an allowlist entry: {uncovered}"
    )
    assert not stale, f"RECIPES/allowlist names tool(s) out of scope: {stale}"
    assert len(CI_TOOLS) == 15, (
        f"expected 15 argparse-declared --ci ops tools "
        f"(14 fail-on-finding + init_project), "
        f"found {len(CI_TOOLS)}: {sorted(CI_TOOLS)}"
    )


def test_one_recipe_per_tool():
    """Runtime discipline: one fixture, two subprocesses per tool."""
    tools = [r.tool for r in RECIPES]
    dupes = sorted({t for t in tools if tools.count(t) > 1})
    assert not dupes, f"one recipe per tool, but duplicated: {dupes}"


def test_mention_only_allowlist_is_exact():
    """Files mentioning --ci without declaring it need an explicit entry."""
    mention_only = {
        name for name, src in _ops_sources().items()
        if CI_MENTION_RE.search(src) and not CI_FLAG_RE.search(src)
    }
    assert mention_only == set(MENTION_ONLY_ALLOWLIST), (
        f"mention-only set drifted.\n"
        f"  unlisted (triage: recipe or allowlist+reason): "
        f"{sorted(mention_only - set(MENTION_ONLY_ALLOWLIST))}\n"
        f"  stale allowlist entries: "
        f"{sorted(set(MENTION_ONLY_ALLOWLIST) - mention_only)}"
    )


def test_same_name_allowlist_is_honest():
    """Each SAME_NAME_DIFFERENT_MEANING tool must still declare a
    choices-shaped --ci, and dev-rules §13 must keep naming it.

    If the tool's --ci ever becomes a store_true fail-on-finding flag, the
    allowlist entry is a lie and the tool belongs in RECIPES.
    """
    sources = _ops_sources()
    dev_rules = DEV_RULES_MD.read_text(encoding="utf-8")
    for name in SAME_NAME_DIFFERENT_MEANING:
        src = sources.get(name)
        assert src is not None, f"allowlisted tool {name} no longer exists"
        assert CI_CHOICES_RE.search(src), (
            f"{name}'s --ci is no longer choices-shaped — if it became a "
            f"fail-on-finding flag it needs a recipe here, and its "
            f"SAME_NAME_DIFFERENT_MEANING entry + dev-rules §13 exception "
            f"line are stale"
        )
        # Line-level, not whole-file substring (W6b blind-review F2): the
        # name must appear ON the 認可例外 line itself — a mention elsewhere
        # (e.g. the ✅Codified bullet) must not keep this green after the
        # exception line drops it.
        assert any(
            name in line and "認可例外" in line
            for line in dev_rules.splitlines()
        ), (
            f"dev-rules.md's 認可例外 line no longer names {name} as a "
            f"documented --ci exception — doc and gate drifted"
        )


def test_known_noop_entries_are_in_scope():
    """No stale exemptions: every KNOWN_CI_NOOP key must be a recipe."""
    tools = {r.tool for r in RECIPES}
    stale = sorted(set(KNOWN_CI_NOOP) - tools)
    assert not stale, f"KNOWN_CI_NOOP lists non-recipe tool(s): {stale}"


# ═══════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════
def _run(argv: list[str], tool: str, cwd: Path, env: dict,
         extra_flag: Optional[str] = None) -> subprocess.CompletedProcess:
    script = OPS_DIR / f"{tool}.py"
    full = [sys.executable, str(script), *argv]
    if extra_flag:
        full.append(extra_flag)
    return subprocess.run(full, capture_output=True, timeout=TIMEOUT_S,
                          cwd=str(cwd), env=env)


def _params():
    out = []
    for r in RECIPES:
        marks = []
        if r.tool in KNOWN_CI_NOOP:
            marks.append(pytest.mark.xfail(
                strict=True,
                reason=f"known --ci no-op (fail-open): {KNOWN_CI_NOOP[r.tool]}",
            ))
        out.append(pytest.param(r, id=r.id, marks=marks))
    return out


@pytest.mark.parametrize("recipe", _params())
def test_ci_flag_flips_exit_code_on_a_finding(recipe: Recipe, tmp_path: Path,
                                              stub_url: str,
                                              fake_kubectl_dir: Path):
    """Same fixture, argv differing only by --ci ⇒ both documented exits."""
    if recipe.needs_kubectl and os.name == "nt":
        pytest.skip(
            f"{recipe.id}: the fake-kubectl shim cannot intercept on Windows — "
            f"CreateProcess resolves a bare `kubectl` to `kubectl.exe` only and "
            f"ignores PATHEXT, so a .bat/POSIX shim is bypassed and the REAL "
            f"kubectl runs. Exercised on POSIX (Linux CI / dev container), which "
            f"is where this gate is authoritative."
        )

    argv = recipe.build(tmp_path, stub_url)
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"      # conftest sets it too; explicit here
    env.pop("PROMETHEUS_URL", None)        # never let a stray env var redirect us
    env.pop("ALERTMANAGER_URL", None)
    if recipe.needs_kubectl:
        env["PATH"] = str(fake_kubectl_dir) + os.pathsep + env.get("PATH", "")
    if recipe.strip_kubectl_path:
        env["PATH"] = _kubectl_free_path(tmp_path)

    without_ci = _run(argv, recipe.tool, cwd, env)
    with_ci = _run(argv, recipe.tool, cwd, env, extra_flag="--ci")

    def fail(why: str) -> str:
        return (
            f"\n--ci fail-on-finding contract VIOLATED\n"
            f"  tool  : {recipe.tool}.py\n"
            f"  argv  : {argv} (+ --ci on the second run)\n"
            f"  note  : {recipe.note}\n"
            f"  reason: {why}\n"
            f"  exit  : without --ci = {without_ci.returncode}, "
            f"with --ci = {with_ci.returncode}\n"
            f"  stdout[:200] (no --ci): "
            f"{without_ci.stdout.decode('utf-8', 'replace')[:200]!r}\n"
            f"  stderr[:300] (no --ci): "
            f"{without_ci.stderr.decode('utf-8', 'replace')[:300]!r}\n"
            f"  stderr[:300] (--ci)   : "
            f"{with_ci.stderr.decode('utf-8', 'replace')[:300]!r}\n"
            f"  fix   : --ci must gate the finding this fixture produces — a "
            f"silent no-op is fail-open in customer CI pipelines.\n"
        )

    # Crash guards: the no---ci run must land on its exact documented code
    # (a deterministic crash exits 1 here and cannot masquerade as a flip),
    # both runs must have produced their report, and neither run may have
    # tracebacked — a tool that prints its report and THEN crashes inside the
    # CI-gate line exits 1 with a full stdout, which exit code + stdout
    # checks alone would wave through as a "flip".
    for label, proc in (("without --ci", without_ci), ("with --ci", with_ci)):
        assert "Traceback (most recent call last)" not in \
            proc.stderr.decode("utf-8", "replace"), fail(
                f"unhandled traceback on the {label} run — that exit code is "
                f"a crash, not a documented contract exit"
            )
    assert without_ci.returncode == recipe.no_ci_exit, fail(
        f"without --ci the documented exit is {recipe.no_ci_exit} for this "
        f"fixture — the finding path was never (cleanly) walked"
    )
    assert without_ci.stdout.strip(), fail(
        "no report on stdout without --ci — nothing was actually checked"
    )

    flag_changed_nothing = with_ci.returncode == recipe.no_ci_exit
    assert with_ci.returncode == recipe.ci_exit, fail(
        f"with --ci the documented exit is {recipe.ci_exit}"
        + (" — the flag changed NOTHING (fail-open no-op)"
           if flag_changed_nothing else "")
    )
    assert with_ci.stdout.strip(), fail(
        "no report on stdout with --ci — exit code without the report is "
        "a crash, not a gated finding"
    )
