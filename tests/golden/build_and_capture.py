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
    path.write_text(content, encoding="utf-8")


def reset(scenario: str) -> Path:
    """Ensure scenario/conf.d/ exists. Does not delete existing files —
    FUSE mount on Cowork VM refuses unlink. write() will overwrite individual
    files; unused files (if any) are checked by the parity test.

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
# Scenario 6: opt-out via explicit null deletes key
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


SCENARIOS = [
    ("flat", "tenant-a", s_flat),
    ("l0-only", "tenant-b", s_l0_only),
    ("full-l0-l3", "tenant-x", s_full_l0_l3),
    ("mixed-mode-flat", "tenant-flat", s_mixed_mode),       # same fixture, 2 tenants
    ("mixed-mode-hier", "tenant-hier", None),               # no re-build
    ("array-replace", "tenant-arr", s_array_replace),
    ("opt-out-null", "tenant-optout", s_opt_out_null),
    ("opt-out-null-threshold", "tenant-null", s_opt_out_null_threshold),
    ("metadata-skipped", "tenant-meta", s_metadata_skipped),
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
        "metadata-skipped": "metadata-skipped",
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
            "source_file": result.get("source_file"),
            "source_hash": result.get("source_hash"),
            "merged_hash": result.get("merged_hash"),
            "defaults_chain": result.get("defaults_chain"),
            "effective_config": result.get("effective_config"),
        })

    out = HERE / "golden.json"
    # Trailing newline is not cosmetic: without it the end-of-file-fixer hook
    # rewrites golden.json on every commit, so each regeneration would show a
    # spurious one-line diff against the committed copy.
    out.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
