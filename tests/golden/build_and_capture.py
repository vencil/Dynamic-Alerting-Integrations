#!/usr/bin/env python3
"""
Build the golden fixture scenarios, run describe_tenant.py against each,
capture expected source_hash + merged_hash, emit golden.json.

Scenarios cover every deep_merge / inheritance semantic in ADR-017
so the Go port can verify byte-for-byte parity.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE / "fixtures"
DESCRIBE = HERE.parent.parent / "scripts" / "tools" / "dx" / "describe_tenant.py"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" for the same reason as golden.json below, and it matters more
    # here: source_hash is computed over these files' bytes, so a CRLF
    # regeneration would change every hash without changing any semantics.
    path.write_text(content, encoding="utf-8", newline="")


def _posix(p):
    """Normalise a captured relative path to forward slashes.

    describe_tenant.py reports paths with the host separator, so a Windows-host
    regeneration used to write `db\\mariadb\\prod\\tenant-x.yaml` into
    golden.json and turn the Go parity test red on paths alone — a silent
    platform trap, since nothing about the merge semantics had changed. The Go
    side compares against `/`-joined paths, so `/` is the contract.
    """
    return p.replace("\\", "/") if isinstance(p, str) else p


def reset(scenario: str) -> Path:
    """Ensure scenario/conf.d/ exists. Does not delete existing files —
    FUSE mount on Cowork VM refuses unlink. write() will overwrite individual
    files. ⚠️ Leftover files are NOT checked by anything: the parity test only
    reads the paths golden.json names, and nothing here globs the tree. A stray
    file a builder stopped writing stays on disk and IS still read by
    describe_tenant's recursive scan, so it can change a fixture's merge result
    while every assertion keeps passing. Measured: an orphan sub-directory
    carrying its own `_defaults.yaml` and an extra tenant left the suite green.
    A stray `_defaults.yml` is no longer the sharp version it was (Python used
    to merge both spellings, Go kept one): since #1674 both sides read ONE
    carrier per directory, pinned by the `carrier-selection` scenario below.
    But a stray carrier in a directory that had none is still read by both,
    and nothing here would notice. Prefer the clean-rebuild command below over
    trusting this function.

    For a clean rebuild, run this from Dev Container (NTFS side):
        docker exec -w /workspaces/vibe-k8s-lab/tests/golden vibe-dev-container \\
          bash -c 'rm -rf fixtures/*/conf.d && python3 build_and_capture.py'
    """
    d = ROOT / scenario / "conf.d"
    d.mkdir(parents=True, exist_ok=True)
    return d


# -------------------------------------------------------------------------
# Scenario 1: flat — single tenant, no defaults
# -------------------------------------------------------------------------
def s_flat():
    d = reset("flat")
    write(d / "tenants.yaml", """tenants:
  tenant-a:
    threshold:
      cpu: 80
      memory: 75
    alert_group: default
""")


# -------------------------------------------------------------------------
# Scenario 2: L0 only — root _defaults + tenant file
# -------------------------------------------------------------------------
def s_l0_only():
    d = reset("l0-only")
    write(d / "_defaults.yaml", """defaults:
  threshold:
    cpu: 70
    memory: 65
  alert_group: baseline
""")
    write(d / "tenants.yaml", """tenants:
  tenant-b:
    threshold:
      cpu: 85
""")


# -------------------------------------------------------------------------
# Scenario 3: full L0→L3 chain
# -------------------------------------------------------------------------
def s_full_l0_l3():
    d = reset("full-l0-l3")
    write(d / "_defaults.yaml", """defaults:
  level: L0
  threshold:
    cpu: 70
  pages:
    - a
""")
    write(d / "db" / "_defaults.yaml", """defaults:
  level: L1
  threshold:
    memory: 75
  pages:
    - b
""")
    write(d / "db" / "mariadb" / "_defaults.yaml", """defaults:
  level: L2
  threshold:
    connections: 90
""")
    write(d / "db" / "mariadb" / "prod" / "_defaults.yaml", """defaults:
  level: L3
  region: us-east
""")
    write(d / "db" / "mariadb" / "prod" / "tenant-x.yaml", """tenants:
  tenant-x:
    threshold:
      cpu: 95
    custom_tag: leaf
""")


# -------------------------------------------------------------------------
# Scenario 4: mixed mode — flat + hierarchical coexist
# -------------------------------------------------------------------------
def s_mixed_mode():
    d = reset("mixed-mode")
    write(d / "flat-tenant.yaml", """tenants:
  tenant-flat:
    threshold:
      cpu: 55
""")
    write(d / "db" / "_defaults.yaml", """defaults:
  threshold:
    memory: 60
""")
    write(d / "db" / "hier-tenant.yaml", """tenants:
  tenant-hier:
    threshold:
      cpu: 88
""")


# -------------------------------------------------------------------------
# Scenario 5: array replace (NOT concat)
# -------------------------------------------------------------------------
def s_array_replace():
    d = reset("array-replace")
    write(d / "_defaults.yaml", """defaults:
  receivers:
    - email-default
    - slack-default
  threshold:
    cpu: 70
""")
    write(d / "tenants.yaml", """tenants:
  tenant-arr:
    receivers:
      - pagerduty-custom
""")


# -------------------------------------------------------------------------
# Scenario 6: explicit null — deletes a reserved (`_`-prefixed) key only.
# The non-reserved keys here (alert_group, threshold.memory) are RETAINED;
# see s_opt_out_null_threshold below for the real-shape threshold case (#1339).
# -------------------------------------------------------------------------
def s_opt_out_null():
    d = reset("opt-out-null")
    write(d / "_defaults.yaml", """defaults:
  threshold:
    cpu: 70
    memory: 75
  alert_group: baseline
""")
    write(d / "tenants.yaml", """tenants:
  tenant-optout:
    alert_group: ~
    threshold:
      memory: ~
      connections: 50
""")


# -------------------------------------------------------------------------
# Scenario 7: _metadata never inherited
# -------------------------------------------------------------------------
def s_metadata_skipped():
    d = reset("metadata-skipped")
    write(d / "_defaults.yaml", """defaults:
  _metadata:
    domain: db
    region: global
  threshold:
    cpu: 70
""")
    write(d / "tenants.yaml", """tenants:
  tenant-meta:
    threshold:
      cpu: 80
""")


# -------------------------------------------------------------------------
# Scenario 8: null on a THRESHOLD key is not an opt-out (#1339 P0)
#
# Every other fixture here uses the synthetic `threshold: {cpu, memory}` /
# `alert_group` shape, which no shipped _defaults.yaml has — that is why this
# regression could sit behind a green parity suite. This one uses the REAL
# shape: `defaults:` holds flat metric keys (unquoted numbers) and tenant
# values are quoted strings.
#
# Pins all three states of the threshold tri-state at once:
#   mysql_connections: ~          -> null is NOT an opt-out; 80 is retained,
#                                    because collector.go emits 80 for it
#   container_memory: "disable"   -> the sanctioned way to stop alerting
#   redis_connected_clients       -> omitted, inherits 5000
# -------------------------------------------------------------------------
def s_opt_out_null_threshold():
    d = reset("opt-out-null-threshold")
    write(d / "_defaults.yaml", """defaults:
  mysql_connections: 80
  container_memory: 85
  redis_connected_clients: 5000
""")
    write(d / "tenants.yaml", """tenants:
  tenant-null:
    mysql_connections: ~
    container_memory: "disable"
""")


# -------------------------------------------------------------------------
# Scenario 8b: a tenant declared with a NULL body (#1677 F2)
#
# `tenants:\n  tenant-null-body:\n` — the exporter's flat plane serves this
# tenant with every inherited default, but pkg/config extractTenantRaw used
# to reject it ("not in file": /effective 500, da-guard failed the scope, the
# exporter skipped its merged hash) and describe_tenant.py crashed on it
# (AttributeError: 'NoneType' object has no attribute 'items'). Both now read
# the null body as an empty override; this pins that the two agree on the
# merged hash. Written into the opt-out-null-threshold tree as its OWN file
# (no existing byte moves, no new conf.d root for the reachability floors),
# so it inherits that tree's real flat-metric-key _defaults.yaml.
# -------------------------------------------------------------------------
def s_null_body():
    d = reset("opt-out-null-threshold")
    write(d / "null-body.yaml", """tenants:
  tenant-null-body:
""")


# -------------------------------------------------------------------------
# Scenario 9: a `defaults:` wrapper WITH sibling top-level keys.
#
# `components/threshold-exporter/config/conf.d/_defaults.yaml` carries
# `state_filters` / `optional_overrides` / `_routing_defaults` alongside
# `defaults:`. Both merge implementations unwrap to the `defaults:` block
# (describe_tenant.py `ddata.get("defaults", ddata)`; Go
# `pkg/config.extractDefaultsBlock`), so those siblings reach no tenant's
# EFFECTIVE CONFIG — which is not the same as reaching no tenant: all three
# are consumed elsewhere (merge_tenant.go for the first two, _grar_parse.py
# for the third), so this fixture is not evidence that they are dead.
#
# Before it existed, no fixture here had anything beside `defaults:`, so a
# change to either implementation's unwrap moved nothing in this suite.
# Measured: merging the siblings alongside the block leaves the old golden
# green on both legs and reddens only this scenario on the new one.
#
# ⛔ RECORDS the behaviour, does not ENDORSE it. Whether the siblings belong
# in the effective config is open — ADR-017's worked example annotates
# `_routing_defaults.group_wait` as inherited, which reads the other way.
#
# ⚠️ NOT GUARDED: nothing asserts this fixture keeps its shape. Deleting the
# three sibling keys leaves every leg green (measured), because source_hash
# hashes the tenant file and merged_hash cannot move for keys that are
# dropped before the merge. A witness for that was written and withdrawn —
# it went through two review rounds and produced a new defect in each, and
# its silent failure does not bring the original defect back: the fixture
# still pins the merge, only edits TO the fixture go unnoticed. Tracked
# separately (#1516 follow-up).
# -------------------------------------------------------------------------
def s_wrapper_siblings():
    d = reset("wrapper-siblings")
    write(d / "_defaults.yaml", """defaults:
  mysql_connections: 80
  container_memory: 85
state_filters:
  container_crashloop:
    reasons: ["CrashLoopBackOff"]
    severity: "critical"
optional_overrides:
  - oracle_process_count
_routing_defaults:
  receiver:
    type: "email"
    to: ["dba-oncall@example.com"]
  group_wait: "30s"
""")
    write(d / "tenants.yaml", """tenants:
  tenant-sib:
    mysql_connections: "70"
""")


# -------------------------------------------------------------------------
# Scenario 10: ONE defaults carrier per directory (#1674, B8)
#
# Two measurements from main 98d2184e, in one tree:
#   pair         `_defaults.yaml` + `_defaults.yml` in one directory:
#                describe_tenant merged BOTH while the exporter's chain read
#                the `.yaml` only — so merged_hash differed for every tenant
#                under that directory.
#   subtree      `sub2/_DEFAULTS.YML`: describe_tenant put it in the chain,
#                the exporter's chain (exact lower-case names) did not.
# Both sides now select with one rule (`_lib_confd.select_defaults_carrier`,
# Go `SelectDefaultsCarriers`): a `.yaml` spelling beats `.yml`, case-folded.
# The Go end (config_golden_parity_test.go ScannerChainOrder / MergedHash)
# reads the same golden.json rows, so a chain or hash drift on either side
# is red. Tenant bodies are empty: every effective key is inherited, so the
# chain alone decides the hash.
#
# ⚠️ Written as a `carrier/` SUBTREE of the mixed-mode tree (whose root has
# no defaults file), not as its own conf.d root — the null-body precedent: a
# new root must be pinned by check_threshold_reachability's floors, and
# neither existing mixed-mode tenant's chain passes through `carrier/`, so no
# existing golden row moves. The ROOT-level pair (which also moves the flat
# plane's global Defaults) is pinned on the Go side by
# app/config_defaults_carrier_test.go.
# -------------------------------------------------------------------------
def s_carrier_selection():
    d = reset("mixed-mode") / "carrier"
    # ONE key per file, on purpose: every defaults key here is counted by
    # check_threshold_reachability's artifact-key floor, whose headroom note
    # moves with it. One key is enough — reading the `.yml` instead shows up
    # as cpu_pct 90, and merging both shows up in `defaults_chain`, which
    # both legs assert.
    write(d / "_defaults.yaml", """defaults:
  cpu_pct: 50
""")
    write(d / "_defaults.yml", """defaults:
  cpu_pct: 90
""")
    write(d / "tenants.yaml", """tenants:
  tenant-pair: {}
""")
    write(d / "sub2" / "_DEFAULTS.YML", """defaults:
  cpu_pct: 95
""")
    write(d / "sub2" / "tenants.yaml", """tenants:
  tenant-sub: {}
""")


SCENARIOS = [
    ("flat", "tenant-a", s_flat),
    ("l0-only", "tenant-b", s_l0_only),
    ("full-l0-l3", "tenant-x", s_full_l0_l3),
    ("mixed-mode-flat", "tenant-flat", s_mixed_mode),       # same fixture, 2 tenants
    ("mixed-mode-hier", "tenant-hier", None),               # no re-build
    ("array-replace", "tenant-arr", s_array_replace),
    ("opt-out-null", "tenant-optout", s_opt_out_null),
    ("opt-out-null-threshold", "tenant-null", s_opt_out_null_threshold),
    ("null-body", "tenant-null-body", s_null_body),         # same fixture dir, own file
    ("metadata-skipped", "tenant-meta", s_metadata_skipped),
    ("wrapper-siblings", "tenant-sib", s_wrapper_siblings),
    ("carrier-selection-pair", "tenant-pair", s_carrier_selection),  # 2 tenants, 1 tree
    ("carrier-selection-sub", "tenant-sub", None),
]


def run_describe(scenario_dir: str, tenant_id: str) -> dict:
    conf_d = ROOT / scenario_dir / "conf.d"
    cmd = [
        sys.executable,
        str(DESCRIBE),
        tenant_id,
        "--conf-d", str(conf_d),
        "--show-sources",
        "--format", "json",
    ]
    # encoding= is required, not cosmetic: describe_tenant.py emits non-ASCII
    # (emoji status markers) and a Windows host defaults text mode to cp950,
    # which raises UnicodeDecodeError before we ever see the JSON.
    # 120s, not 30s: describe_tenant walks the fixture tree recursively, and on
    # a Windows-mounted worktree that walk is I/O-bound to the point of taking
    # ~34s wall for the deepest fixture (measured: 0.09s user, 0.08s sys — it is
    # all filesystem latency, not compute). A 30s cap turned a cold cache into a
    # TimeoutExpired traceback that reads like a hang in the tool under test.
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", timeout=120)
    if result.returncode != 0:
        print(f"FAIL {scenario_dir}:{tenant_id}: {result.stderr}", file=sys.stderr)
        return {"error": result.stderr}
    return json.loads(result.stdout)


def main() -> int:
    # Build all distinct fixtures (mixed-mode scenarios share a dir)
    builders_seen: set = set()
    scenario_dir_map = {
        "flat": "flat",
        "l0-only": "l0-only",
        "full-l0-l3": "full-l0-l3",
        "mixed-mode-flat": "mixed-mode",
        "mixed-mode-hier": "mixed-mode",
        "array-replace": "array-replace",
        "opt-out-null": "opt-out-null",
        "opt-out-null-threshold": "opt-out-null-threshold",
        "null-body": "opt-out-null-threshold",
        "metadata-skipped": "metadata-skipped",
        "wrapper-siblings": "wrapper-siblings",
        "carrier-selection-pair": "mixed-mode",
        "carrier-selection-sub": "mixed-mode",
    }
    for scenario, tenant_id, builder in SCENARIOS:
        if builder is not None and builder not in builders_seen:
            builder()
            builders_seen.add(builder)

    golden: list[dict] = []
    for scenario, tenant_id, _ in SCENARIOS:
        fixture_dir = scenario_dir_map[scenario]
        result = run_describe(fixture_dir, tenant_id)
        if "error" in result:
            golden.append({"scenario": scenario, "tenant_id": tenant_id,
                           "fixture_dir": fixture_dir, "error": result["error"]})
            continue
        golden.append({
            "scenario": scenario,
            "tenant_id": tenant_id,
            "fixture_dir": fixture_dir,
            "source_file": _posix(result.get("source_file")),
            "source_hash": result.get("source_hash"),
            "merged_hash": result.get("merged_hash"),
            "defaults_chain": [_posix(p) for p in (result.get("defaults_chain") or [])],
            "effective_config": result.get("effective_config"),
        })

    out = HERE / "golden.json"
    # newline="" pins LF on every platform: write_text() defaults to
    # os.linesep translation, so a Windows-host regeneration would rewrite the
    # whole file as CRLF. The trailing newline is likewise not cosmetic —
    # without it the end-of-file-fixer hook rewrites golden.json every commit.
    out.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8", newline="")
    print(f"Wrote {out}")
    print(f"Captured {len(golden)} scenarios")
    for g in golden:
        if "error" in g:
            print(f"  [FAIL] {g['scenario']}/{g['tenant_id']}: {g['error'][:80]}")
        else:
            print(f"  [OK]   {g['scenario']}/{g['tenant_id']}: "
                  f"src={g['source_hash']} merged={g['merged_hash']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
