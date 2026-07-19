"""`--dry-run` zero-side-effect contract gate for da-tools ops CLI tools.

THE CONTRACT
------------
A tool invoked with ``--dry-run`` promises a PREVIEW: it may print what it
*would* do, but it must not create, modify, or delete a single file.  (E.g.
``init_project --dry-run`` self-describes as「顯示會產生的檔案但不寫入」.)

The observable form of the contract: every recipe runs inside an isolated
tmp tree (input fixtures COPIED under it, subprocess ``cwd`` under it, every
declared output path under it).  A ``(relative-path, sha256)`` snapshot of
the WHOLE tmp tree is taken before and after the run, and the two snapshots
must be set-equal — nothing added, nothing removed, nothing rewritten.
Directories count too: even ``mkdir``-ing an empty output directory is a
side effect a "preview" has no business performing.

A vacuous pass is also guarded against: each recipe must exit with one of
its expected graceful codes (0 for most; ``generate_rule_pack_split`` also
gracefully exits 1 = EXIT_VIOLATION on its pre-existing in-repo metric
mismatch report), and a non-zero graceful exit must still have produced its
report on stdout.  A recipe that crashes before doing any work would
trivially leave the tree unchanged; these two guards prove the tool actually
walked its dry-run path.

WHY THIS FILE EXISTS
--------------------
~18 ops tools advertise ``--dry-run``, and until this gate NOTHING in the
repo asserted the filesystem actually stays untouched.  This is the #1112
lesson extended from stdout identity to *filesystem* identity: "the preview
parsed fine" proves nothing about what it silently wrote.  A dry-run-writes
regression fires exactly when a customer is "just previewing" against their
conf.d/ — high consequence, near-zero detection.

SCOPE — WHAT COUNTS AS A --dry-run TOOL
---------------------------------------
Scope discovery is argparse-based: an ops tool is in scope iff it *declares*
``add_argument("--dry-run")`` (either quote style).  18 tools match today;
``test_recipe_table_covers_every_dry_run_tool`` pins the count and fails if
a new tool grows the flag without gaining a recipe here.

Three files mention ``--dry-run`` without declaring it, and are held in an
explicit, meta-asserted allowlist (``MENTION_ONLY_ALLOWLIST``):

* ``batchpr_dispatch`` — pure forwarder; the flag belongs to the ``da-batchpr``
  Go binary (not buildable/hermetic from this Python gate; covered Go-side).
* ``drift_detect`` — docstring prose only; the tool has no ``--dry-run`` flag.
* ``threshold_govern`` — dry-run is the *default* (absence of ``--apply``),
  not a flag; its no-write default is exercised by its own test suite.

SCOPE — HONEST BOUNDARIES
-------------------------
1. **Only the tmp tree is watched.**  A tool that writes OUTSIDE it (system
   temp, the repo tree, ``$HOME`` caches) is not caught by this gate.  Repo-
   tree writes have their own guard (`threshold_recommend`'s observed-map
   writer runs sandboxed in test_json_stdout_contract.py); the rest is an
   accepted limit, not an oversight.
2. **One recipe per tool, one subprocess per tool** — runtime discipline.
   18 subprocess launches ≈ tens of seconds total; a (tool × mode) matrix
   like the --json gate's 84 recipes would triple that for little marginal
   signal, because the dry-run *branch* is what's under test, not every
   mode combination around it.
3. **Legitimate writes are excluded by recipe design, not by exemption:**
   no recipe passes a report/output-writing flag alongside ``--dry-run``
   (e.g. an explicit ``--report-file``), so any write observed is a
   violation by construction.
4. **No recipe needs kubectl or a real network** — the three recipes that
   talk HTTP get the same local stub server the --json gate uses, so this
   gate runs fully (no skips) on Windows hosts as well as POSIX CI.
5. **Data-shape coverage is one-deep** (same accepted limit as the --json
   gate): each dry-run path is driven with one upstream payload shape.
   Probed at landing time: even against the empty-stub payloads the
   report-heavy tools (e.g. ``threshold_recommend``) still walk their full
   per-tenant loop and emit a complete report, so the would-write machinery
   is genuinely exercised — but a write hidden behind a specific data shape
   no recipe produces would not be caught.
6. **Scope is ops/ only** — 15 further da-tools declare ``--dry-run``
   (dx ×14, lint ×1) and are NOT covered here (W6a blind-review F1).
   The customer-preview risk this gate exists for applies to the ops
   CLI surface; dx/lint are developer-side. Extending scope is a
   follow-up decision, not an accident of omission — the sibling
   --json gate draws the same ops-only line.

FIRST-RUN REDS → KNOWN_DRY_RUN_WRITERS
--------------------------------------
A tool caught writing in dry-run is a REAL BUG CANDIDATE, and fixing it is
a behaviour change outside this gate's remit.  Such tools are listed in
``KNOWN_DRY_RUN_WRITERS`` (value = what it wrote + one-line symptom) and
xfail(strict=True): the gate documents the violation today and goes red the
moment someone fixes the tool without delisting it.

HARNESS REUSE
-------------
The HTTP stub handler and the readiness-JSON builder are imported from
``test_json_stdout_contract`` (same directory, so pytest's prepend import
mode resolves it) — zero changes to that gate.  Fixture *copies* rather
than in-repo paths are deliberate: pointing a tool at the real
``try-local/seed/conf.d`` would leave input mutations invisible to the
snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import pytest

import test_json_stdout_contract as _jsc

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "tools" / "ops"

# Real in-repo fixtures — COPIED into the tmp tree per recipe (never
# referenced in place: input mutation must land inside the snapshot).
SEED_CONF_D = REPO_ROOT / "try-local" / "seed" / "conf.d"
RULE_PACKS = REPO_ROOT / "rule-packs"
K8S_MONITORING = REPO_ROOT / "k8s" / "03-monitoring"
DASHBOARD_JSON = K8S_MONITORING / "shadow-monitoring-dashboard.json"
ALERTMANAGER_YML = REPO_ROOT / "try-local" / "alertmanager.yml"

FIXTURE_PATHS = [SEED_CONF_D, RULE_PACKS, K8S_MONITORING, DASHBOARD_JSON,
                 ALERTMANAGER_YML]

TIMEOUT_S = 90

# ═══════════════════════════════════════════════════════════════════════════
# Scope discovery — argparse-declared --dry-run flags only
# ═══════════════════════════════════════════════════════════════════════════
# Both quote styles occur in the tree (init_project and
# generate_tenant_mapping_rules use single quotes); \s* spans the newline in
# the multi-line add_argument( \n "--dry-run" ) formatting.
DRY_RUN_FLAG_RE = re.compile(r"""add_argument\(\s*['"]--dry-run['"]""")
DRY_RUN_MENTION_RE = re.compile(r"--dry-run")

# Files that MENTION --dry-run without DECLARING it (see module docstring).
# Meta-asserted: an entry that stops mentioning the flag, or starts declaring
# it, fails test_mention_only_allowlist_is_exact.
MENTION_ONLY_ALLOWLIST: dict[str, str] = {
    "batchpr_dispatch": "forwarder — the flag lives in the da-batchpr Go binary",
    "drift_detect": "docstring prose only; no --dry-run flag declared",
    "threshold_govern": "dry-run is the default mode (no --apply), not a flag",
}


def _ops_sources() -> dict[str, str]:
    return {
        f.stem: f.read_text(encoding="utf-8")
        for f in sorted(OPS_DIR.glob("*.py"))
        if not f.name.startswith("_") and f.name != "__init__.py"
    }


def collect_dry_run_tools() -> list[str]:
    """Every ops tool that argparse-declares a `--dry-run` flag."""
    return [name for name, src in _ops_sources().items()
            if DRY_RUN_FLAG_RE.search(src)]


DRY_RUN_TOOLS = collect_dry_run_tools()


# ═══════════════════════════════════════════════════════════════════════════
# Known violators — dry-run writes observed at gate-landing time (2026-07).
# Listed, NOT fixed: the fix is a behaviour change for the owner to schedule.
# Keys are tool names; presence ⇒ that tool's recipe is xfail(strict=True).
# ═══════════════════════════════════════════════════════════════════════════
KNOWN_DRY_RUN_WRITERS: dict[str, str] = {
    # EMPTY at gate-landing time (2026-07-17): the first full run found all 18
    # in-scope dry-run paths filesystem-clean, and a mutation check of the
    # harness itself (same recipes with --dry-run dropped) confirmed the
    # snapshot diff DOES detect write-mode output (33/4/8 added entries for
    # generate_rule_pack_split / onboard_platform / migrate_rule), so the
    # green is meaningful, not vacuous.
}


# ═══════════════════════════════════════════════════════════════════════════
# Tmp-tree helpers
# ═══════════════════════════════════════════════════════════════════════════
def _tree_snapshot(root: Path) -> dict[str, str]:
    """(relative-posix-path → sha256 | "<dir>") for every entry under root."""
    snap: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if d != root:
            snap[d.relative_to(root).as_posix() + "/"] = "<dir>"
        for fn in filenames:
            p = d / fn
            snap[p.relative_to(root).as_posix()] = hashlib.sha256(
                p.read_bytes()).hexdigest()
    return snap


def _copy_into(tmp: Path, source: Path, name: str) -> str:
    """Copy a repo fixture (file or dir) under tmp/fix/<name>; return the copy."""
    dest = tmp / "fix" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, dest)
    else:
        shutil.copy2(source, dest)
    return str(dest)


def _out(tmp: Path, name: str) -> str:
    """A declared output path under tmp/out — deliberately NOT created.

    Dry-run must not even mkdir it; if the tool does, the snapshot diff
    reports `out/<name>/` as an added entry.
    """
    return str(tmp / "out" / name)


def _existing_dir(tmp: Path, name: str) -> str:
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / "fix" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p)


def _threshold_cr(tmp: Path) -> str:
    """Minimal ThresholdConfig CR for da_assembler --render-cr (offline)."""
    return _write(tmp, "tc.yaml", (
        "apiVersion: dynamicalerting.io/v1alpha1\n"
        "kind: ThresholdConfig\n"
        "metadata:\n"
        "  name: tenant-one\n"
        "  namespace: monitoring\n"
        "spec:\n"
        "  tenants:\n"
        "    tenant-one:\n"
        "      max_connections: '100'\n"
    ))


def _legacy_rules(tmp: Path) -> str:
    """Minimal legacy Prometheus rules file for migrate_rule."""
    return _write(tmp, "legacy-rules.yaml", (
        "groups:\n"
        "  - name: legacy\n"
        "    rules:\n"
        "      - alert: HighConnectionCount\n"
        "        expr: mysql_global_status_threads_connected > 100\n"
        "        for: 5m\n"
        "        labels:\n"
        "          severity: warning\n"
        "        annotations:\n"
        "          summary: connection count high\n"
    ))


# ═══════════════════════════════════════════════════════════════════════════
# Stub server — reuses the --json gate's handler (Prometheus / Alertmanager
# personalities); only cutover_tenant / maintenance_scheduler /
# threshold_recommend touch it.
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="session")
def stub_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _jsc._StubHandler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ═══════════════════════════════════════════════════════════════════════════
# Recipes — exactly one dry-run invocation per in-scope tool.
#
# Twelve argv shapes are lifted from test_json_stdout_contract.py's dry-run
# recipes (fixture paths swapped for tmp copies); six tools had no recipe
# there and get a minimal invocation here (baseline_discovery, da_assembler,
# generate_alertmanager_routes, generate_tenant_mapping_rules, init_project,
# migrate_rule).
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Recipe:
    tool: str
    build: Callable[[Path, str], list[str]]   # (tmp_path, stub_url) -> argv
    # Graceful exit codes for this invocation. 0 for almost every tool;
    # a tool whose dry-run legitimately ends in a detected-violation REPORT
    # (exit 1 = EXIT_VIOLATION) widens this — never 2 (caller error), which
    # would mean the recipe itself is broken.
    ok_exits: tuple[int, ...] = (0,)

    @property
    def id(self) -> str:
        return self.tool


R = Recipe
RECIPES: list[Recipe] = [
    # ── new minimal invocations (no --json-gate dry-run recipe existed) ────
    R("baseline_discovery",
      lambda t, s: ["--tenant", "tenant-one", "--dry-run"]),

    R("da_assembler",
      lambda t, s: ["--render-cr", _threshold_cr(t),
                    "--config-dir", _out(t, "rendered"), "--dry-run"]),

    R("generate_alertmanager_routes",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--dry-run"]),

    R("generate_tenant_mapping_rules",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--dry-run"]),

    R("init_project",
      lambda t, s: ["--non-interactive", "--tenants", "tenant-one,tenant-two",
                    "--dry-run", "-o", _out(t, "project")]),

    R("migrate_rule",
      lambda t, s: [_legacy_rules(t), "--dry-run",
                    "-o", _out(t, "migration")]),

    # ── argv shapes reused from the --json gate's dry-run recipes ──────────
    R("batch_diagnose",
      lambda t, s: ["--tenants", "tenant-one,tenant-two", "--dry-run", "--json"]),

    R("cutover_tenant",
      lambda t, s: ["--readiness-json", _jsc._readiness_json(t / "fix"),
                    "--tenant", "tenant-one",
                    "--prometheus", s, "--dry-run", "--json-output"]),

    # ok_exits: the CURRENT in-repo packs carry a clickhouse edge/central
    # metric mismatch, so the tool ends in its graceful detected-violation
    # report (exit 1 = EXIT_VIOLATION) — same result against the un-copied
    # repo dir, so it is the data, not this gate's fixture copy. The
    # stdout-report guard below keeps this from masking a crash.
    R("generate_rule_pack_split",
      lambda t, s: ["--rule-packs-dir", _copy_into(t, RULE_PACKS, "rule-packs"),
                    "--dry-run", "--json"],
      ok_exits=(0, 1)),

    R("grafana_import",
      lambda t, s: ["--dashboard", _copy_into(t, DASHBOARD_JSON, "dashboard.json"),
                    "--dry-run", "--json"]),

    R("maintenance_scheduler",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--alertmanager", s, "--dry-run", "--json-output"]),

    R("migrate_to_operator",
      lambda t, s: ["--source-dir", _copy_into(t, K8S_MONITORING, "monitoring"),
                    "--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--output-dir", _out(t, "crds"), "--dry-run", "--json"]),

    R("notification_tester",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--dry-run", "--json"]),

    R("onboard_platform",
      lambda t, s: ["--alertmanager-config",
                    _copy_into(t, ALERTMANAGER_YML, "alertmanager.yml"),
                    "-o", _out(t, "onboard"), "--dry-run", "--json"]),

    R("operator_generate",
      lambda t, s: ["--rule-packs-dir", _copy_into(t, RULE_PACKS, "rule-packs"),
                    "--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--output-dir", _out(t, "operator"), "--dry-run", "--json"]),

    R("policy_opa_bridge",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--dry-run", "--json"]),

    R("state_reconcile",
      lambda t, s: ["--state-dir", _existing_dir(t, "state"),
                    "--manifest-path", _out(t, "manifest.json"),
                    "--dry-run", "--json"]),

    R("threshold_recommend",
      lambda t, s: ["--config-dir", _copy_into(t, SEED_CONF_D, "conf.d"),
                    "--prometheus", s, "--dry-run", "--json"]),
]


# ═══════════════════════════════════════════════════════════════════════════
# Meta-tests — keep the scope honest
# ═══════════════════════════════════════════════════════════════════════════
def test_fixture_paths_exist():
    missing = [str(p) for p in FIXTURE_PATHS if not p.exists()]
    assert not missing, f"gate fixtures missing from the repo: {missing}"


def test_recipe_table_covers_every_dry_run_tool():
    """A tool that grows --dry-run must gain a recipe, or this gate rots."""
    covered = {r.tool for r in RECIPES}
    uncovered = sorted(set(DRY_RUN_TOOLS) - covered)
    stale = sorted(covered - set(DRY_RUN_TOOLS))
    assert not uncovered, (
        f"{len(uncovered)} tool(s) declare --dry-run but have no recipe in "
        f"RECIPES: {uncovered}"
    )
    assert not stale, f"RECIPES names tool(s) out of scope: {stale}"
    assert len(DRY_RUN_TOOLS) == 18, (
        f"expected 18 argparse-declared --dry-run ops tools, "
        f"found {len(DRY_RUN_TOOLS)}: {sorted(DRY_RUN_TOOLS)}"
    )


def test_one_recipe_per_tool():
    """Runtime discipline: exactly one subprocess per tool (see docstring §2)."""
    tools = [r.tool for r in RECIPES]
    dupes = sorted({t for t in tools if tools.count(t) > 1})
    assert not dupes, f"one recipe per tool, but duplicated: {dupes}"


def test_mention_only_allowlist_is_exact():
    """Files mentioning --dry-run without declaring it need an explicit entry.

    Catches BOTH drifts: a new passthrough tool advertising the flag in help
    text (must be triaged into a recipe or this allowlist), and a listed tool
    that starts genuinely declaring the flag (then it belongs in RECIPES and
    its allowlist entry is stale).
    """
    mention_only = {
        name for name, src in _ops_sources().items()
        if DRY_RUN_MENTION_RE.search(src) and not DRY_RUN_FLAG_RE.search(src)
    }
    assert mention_only == set(MENTION_ONLY_ALLOWLIST), (
        f"mention-only set drifted.\n"
        f"  unlisted (triage: recipe or allowlist+reason): "
        f"{sorted(mention_only - set(MENTION_ONLY_ALLOWLIST))}\n"
        f"  stale allowlist entries: "
        f"{sorted(set(MENTION_ONLY_ALLOWLIST) - mention_only)}"
    )


def test_known_writers_are_in_scope():
    """No stale exemptions: every KNOWN_DRY_RUN_WRITERS key must be a recipe."""
    tools = {r.tool for r in RECIPES}
    stale = sorted(set(KNOWN_DRY_RUN_WRITERS) - tools)
    assert not stale, f"KNOWN_DRY_RUN_WRITERS lists non-recipe tool(s): {stale}"


# ═══════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════
def _params():
    out = []
    for r in RECIPES:
        marks = []
        if r.tool in KNOWN_DRY_RUN_WRITERS:
            marks.append(pytest.mark.xfail(
                strict=True,
                reason=f"known dry-run writer: {KNOWN_DRY_RUN_WRITERS[r.tool]}",
            ))
        out.append(pytest.param(r, id=r.id, marks=marks))
    return out


@pytest.mark.parametrize("recipe", _params())
def test_dry_run_leaves_filesystem_untouched(recipe: Recipe, tmp_path: Path,
                                             stub_url: str):
    """--dry-run ⇒ the tmp tree is byte-identical before and after the run."""
    argv = recipe.build(tmp_path, stub_url)
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"      # conftest sets it too; explicit here
    env.pop("PROMETHEUS_URL", None)        # never let a stray env var redirect us
    env.pop("ALERTMANAGER_URL", None)

    before = _tree_snapshot(tmp_path)

    script = OPS_DIR / f"{recipe.tool}.py"
    proc = subprocess.run(
        [sys.executable, str(script), *argv],
        capture_output=True, timeout=TIMEOUT_S, cwd=str(cwd), env=env,
    )

    after = _tree_snapshot(tmp_path)

    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")

    def fail(why: str) -> str:
        return (
            f"\n--dry-run zero-side-effect contract VIOLATED\n"
            f"  tool  : {recipe.tool}.py\n"
            f"  argv  : {argv}\n"
            f"  reason: {why}\n"
            f"  exit  : {proc.returncode}\n"
            f"  stdout[:200]: {stdout[:200]!r}\n"
            f"  stderr[:300]: {stderr[:300]!r}\n"
            f"  fix   : the dry-run branch must return BEFORE any "
            f"mkdir/write/unlink — preview means preview.\n"
        )

    # Guard against a vacuous pass: a crash touches nothing and proves nothing.
    assert proc.returncode in recipe.ok_exits, fail(
        f"recipe exited {proc.returncode}, expected one of {recipe.ok_exits} — "
        f"the dry-run path was never (fully) walked, so an unchanged tree "
        f"would be a vacuous green"
    )
    if proc.returncode != 0:
        # A graceful non-zero MUST come with its report; an unhandled
        # traceback also exits 1 but leaves stdout empty.
        assert stdout.strip(), fail(
            "non-zero exit with EMPTY stdout — that is a crash, not a "
            "graceful detected-violation report"
        )
        if "--json" in argv:
            # Mid-report crash leaves partial (unparseable) output that a
            # bare non-empty check would wave through (W6a blind-review F2).
            try:
                json.loads(stdout)
            except ValueError:
                raise AssertionError(fail(
                    "non-zero exit with UNPARSEABLE --json stdout — "
                    "partial report = crash, not a graceful violation report"
                )) from None

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after)
                     if before[k] != after[k])
    if added or removed or changed:
        raise AssertionError(fail(
            "filesystem changed during --dry-run:\n"
            + json.dumps({"added": added, "removed": removed,
                          "changed": changed}, indent=2)
        ))
