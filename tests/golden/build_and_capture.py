#!/usr/bin/env python3
"""
Build the golden fixture scenarios, run describe_tenant.py against each,
capture expected source_hash + merged_hash, emit golden.json.

The scenarios exercise the deep_merge / inheritance rules listed in
test_merge_parity.py's module docstring so the Go port can verify
byte-for-byte parity. They do NOT cover every ADR-017 clause. #1550's
reserved-key null deletion, nested-null and canonical-JSON escaping rows
are scenarios 11-13 below, and #2371's YAML date / `!!binary` values and
non-string mapping keys are 17-19, and #2415's int / float values
are 20; #1550's chain-discovery gap is closed on the Go side
(config_golden_parity_test.go TestGoldenParity_ResolveEffective). What stays
open is listed in test_merge_parity.py's "Known gaps".

Exit status: 0 only when every scenario regenerated. If describe_tenant
fails for a scenario, its entry is still written to golden.json (carrying
only `error`, no hashes) so the breakage is visible there, but main()
returns 1 and names the failed scenarios on stderr. A caller must not read
a partial golden.json as a successful regeneration (#1551).
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

    describe_tenant.py used to report paths with the host separator, so a
    Windows-host regeneration wrote `db\\mariadb\\prod\\tenant-x.yaml` into
    golden.json and turned the Go parity test red on paths alone — a silent
    platform trap, since nothing about the merge semantics had changed. The Go
    side compares against `/`-joined paths, so `/` is the contract.
    describe_tenant now emits `/` itself (#1550); this stays as a no-op
    safety net for the golden file.
    """
    return p.replace("\\", "/") if isinstance(p, str) else p


def reset(scenario: str) -> Path:
    """Ensure scenario/conf.d/ exists. Does not delete existing files —
    FUSE mount on Cowork VM refuses unlink. write() will overwrite individual
    files. ⚠️ So this function still leaves stale files behind: a file a
    builder stopped writing stays on disk and IS still read by describe_tenant's
    recursive scan, so it can change a fixture's merge result. Nothing in this
    script notices. What catches it is
    test_merge_parity.py::test_fixture_trees_have_no_orphans (#1551): it
    enumerates every yaml under the fixtures' conf.d trees and fails on any
    file that golden.json does not declare (source_file ∪ defaults_chain) and
    its commented allow-list does not name. Before that test existed, an
    orphan sub-directory carrying its own `_defaults.yaml` and an extra tenant
    left the whole suite green. Run the parity tests after regenerating, and
    prefer the clean-rebuild command below over trusting this function.

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
# Scenario 6: explicit null on NON-reserved keys (alert_group,
# threshold.memory): both are RETAINED. Null deletes a reserved (`_`-prefixed)
# key only, and this tree has none — that branch is s_reserved_null_delete's
# (#1550). See s_opt_out_null_threshold below for the real-shape threshold
# case (#1339).
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
    smarthost: "smtp.example.com:587"
    from: "alertmanager@example.com"
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


# -------------------------------------------------------------------------
# Scenarios 11-13 (#1550): three shapes the oracle had no row for. All three
# are SUBTREES of the mixed-mode tree, for the carrier-selection reason above
# (its root has no defaults file, so no existing tenant's chain passes through
# them and no existing golden row moves; no new conf.d root to pin). Their
# `defaults:` keys are counted by check_threshold_reachability's artifact-key
# floor, which moved with them.
# -------------------------------------------------------------------------

# Scenario 11: canonical-JSON escaping. A tenant string value carrying `<`,
# `>`, `&` and CJK text. The merged hash is computed over canonical JSON, and
# two of its clauses were vacuous while no value in the corpus had such
# characters: Python `ensure_ascii=False` (flipping it rewrites the CJK as
# \uXXXX) and Go `SetEscapeHTML(false)` (flipping it rewrites `<>&` as
# <...). Either flip moves merged_hash on this row alone. No defaults
# file: the value reaches the hash straight from the tenant file.
def s_canonical_json_escaping():
    d = reset("mixed-mode") / "escaping"
    write(d / "tenants.yaml", """tenants:
  tenant-escape:
    _silent_mode:
      target: "warning"
      reason: "維護窗口：主庫 <primary> & 備庫 <replica> 切換"
""")


# Scenario 12: a NESTED null under an inherited reserved key (ADR-017 §Merge
# 語意). deep_merge deletes on null only when the key itself is `_`-prefixed
# (ADR-017: 判準是「是否 `_` 前綴」); the sub-keys here sit one level under
# the reserved key and are not `_`-prefixed. So:
#   inherited_list / inherited_str   inherited from `defaults:`, nulled here
#                             -> the inherited value is RETAINED
#   never_set_a / never_set_b        not inherited, nulled here
#                             -> ABSENT from effective_config: the null is a
#                                no-op, not copied through, because `_x`
#                                itself was inherited and so is merged key by
#                                key rather than replaced
#   added                     tenant-only sub-mapping -> present
# The row turns red if either merge implementation (describe_tenant /
# pkg/config) starts treating a nested null as a deletion, or copies it
# through as a JSON null.
#
# ⛔ The key is a deliberately meaningless `_x`, not `_routing` (#2417). This
# row used `_routing` until #2362 (#2291) made da-guard reject `_routing`
# inside a `defaults:` block (`routing_in_unread_location`: the route
# generator never reads routing from there), which made a whole-tree da-guard
# run over this fixture exit 1. The same change moved da-guard's routing
# reads off `EffectiveConfig["_routing"]` onto the layers the generator reads
# (layers.TenantBlock + routingpolicy.Resolve), so `_routing` bought nothing
# here that any reserved key does not. `_x` was picked because no code path special-cases
# it: both merges carry it through like any `_` key, and da-guard reports
# nothing for it. What ADR-017 says a `_routing` null does to the generated
# route is the route generator's plane (_grar_merge.py), which neither merge
# runs and this oracle does not cover.
def s_reserved_nested_null():
    d = reset("mixed-mode") / "reserved-nested-null"
    write(d / "_defaults.yaml", """defaults:
  _x:
    inherited_list: ["alertname"]
    inherited_str: "30s"
""")
    write(d / "tenants.yaml", """tenants:
  tenant-nested:
    _x:
      added:
        type: "webhook"
        url: "https://hooks.example.com/alerts"
      inherited_list: ~
      inherited_str: ~
      never_set_a: ~
      never_set_b: ~
""")


# Scenario 13: a RESERVED (`_`-prefixed) key deleted by an explicit null in
# a child after it was inherited. Before this row no scenario's
# effective_config held a `_` key at all, so removing the deletion branch on
# either side left the oracle green (#1550 item 5). Written defaults ->
# defaults, the path ADR-017 names as the reachable one (a tenant file's null
# on these keys is rejected by check_confd_schema.py). `_silent_mode` is the
# control: inherited and not nulled, so it must survive.
def s_reserved_null_delete():
    d = reset("mixed-mode") / "reserved-null"
    write(d / "_defaults.yaml", """defaults:
  _severity_dedup: "disable"
  _silent_mode: "warning"
""")
    write(d / "child" / "_defaults.yaml", """defaults:
  _severity_dedup: ~
""")
    write(d / "child" / "tenants.yaml", """tenants:
  tenant-reserved: {}
""")


# -------------------------------------------------------------------------
# Scenarios 14-16 (#2387): trees the exporter SERVES.
#
# ⛔ Five of the trees above (l0-only, full-l0-l3, array-replace,
# opt-out-null, metadata-skipped) carry a ROOT `_defaults.yaml` the exporter
# drops whole: a root `defaults:` value that is not a number (a nested
# `threshold:` map, `_metadata`, an array) fails to decode, the file lands in
# parse_failed and /metrics serves none of it (#1957). They stay, because the
# deep-merge core they pin (nested maps, array replace, null on a nested
# non-reserved key, `_metadata` not inherited) cannot be written in a root
# shape the exporter accepts — but parity on them proves only that the two
# readers agree with each other, not that they describe what is served. They
# are listed, with that reason, in tests/golden/not_served.json, and
# components/threshold-exporter/app/cmd/da-guard/golden_served_test.go runs
# `da-guard served-values` over every fixture tree: a tree must exit 0 unless
# it is listed, and a listed tree must exit 3 (so the list cannot go stale).
#
# These two trees are the served counterpart for what CAN be written in the
# accepted shape: numeric root defaults (the shipped platform shape), subtree
# `_defaults.yaml` overriding them, quoted-string tenant values. Two new conf.d
# roots, pinned in check_threshold_reachability's `_DEFAULTS_CONFD_ROOTS`;
# their 8 `defaults:` keys raised the artifact-key floor by 8 (the #1674 /
# #1550 remedy).
#
# ⚠️ "Served" is da-guard's rc 0 — nothing in the tree is dropped. It is NOT
# "every golden value equals what /metrics serves", and two shapes that pass
# rc 0 are kept out of these trees on purpose because /metrics disagrees with
# both merge readers on them (measured with `da-guard served-values`, #2296):
#   * null on a threshold key a subtree `_defaults.yaml` overrides: the
#     readers resolve the L1 value, /metrics falls back to the ROOT default;
#   * a key that only a subtree `_defaults.yaml` declares: the readers merge
#     it in, /metrics serves no row for it.
# A row pinning either would record the readers' answer as if it were the
# served one.
# -------------------------------------------------------------------------

# Scenario 14 + 15: L0 -> L1 -> L2 chain, and a root-level sibling tenant.
#   tenant-served-chain  mysql_connections        tenant "60" beats L1's 70
#                        container_memory         L2's 90 beats L0's 85
#                        redis_connected_clients  L0's 5000, through 2 levels
#   tenant-served-root   chain is the root file only: L0's 80 / 85 / 5000,
#                        so neither subtree carrier leaks into a sibling
def s_served_chain():
    d = reset("served-chain")
    write(d / "_defaults.yaml", """defaults:
  mysql_connections: 80
  container_memory: 85
  redis_connected_clients: 5000
""")
    write(d / "db" / "_defaults.yaml", """defaults:
  mysql_connections: 70
""")
    write(d / "db" / "mariadb" / "_defaults.yaml", """defaults:
  container_memory: 90
""")
    write(d / "db" / "mariadb" / "tenants.yaml", """tenants:
  tenant-served-chain:
    mysql_connections: "60"
""")
    write(d / "tenants.yaml", """tenants:
  tenant-served-root: {}
""")


# Scenario 16: "disable" through a subtree chain. The tenant turns off a key
# it inherits from L0, while L1 overrides the other one:
#   mysql_connections   tenant "disable" beats L0's 80 (the sanctioned opt-out)
#   container_memory    L1's 90 beats L0's 85
def s_served_disable():
    d = reset("served-disable")
    write(d / "_defaults.yaml", """defaults:
  mysql_connections: 80
  container_memory: 85
""")
    write(d / "db" / "_defaults.yaml", """defaults:
  container_memory: 90
""")
    write(d / "db" / "tenants.yaml", """tenants:
  tenant-served-disable:
    mysql_connections: "disable"
""")


# Scenarios 17-18 (#2371): values PyYAML types as `date` / `datetime` /
# `bytes`. describe_tenant ended with a TypeError on every one of them, so no
# row could hold them; yaml.v3 reads a timestamp into time.Time and a
# `!!binary` into a string of its raw bytes, and pkg/config's canonical JSON
# renders those. These rows pin that rendering on both sides. Mixed-mode
# subtrees again, for the carrier-selection reason above. The shapes the
# Python side CANNOT align (describe_tenant.py's #2371 block) are left out on
# purpose — they are strict xfails in tests/dx/test_describe_tenant.py.
#
# Scenario 17: an unquoted date inherited from `_defaults.yaml` (Go:
# "2026-12-31T00:00:00Z") beside a tenant datetime with a fraction and an
# offset (RFC 3339, fraction's trailing zero dropped, offset kept).
def s_yaml_date():
    d = reset("mixed-mode") / "yaml-date"
    write(d / "_defaults.yaml", """defaults:
  _silent_mode:
    target: "warning"
    expires: 2026-12-31
""")
    write(d / "tenants.yaml", """tenants:
  tenant-date:
    _state_maintenance:
      target: "all"
      expires: 2026-12-31T23:59:59.50+08:00
    _silent_mode:
      reason: "inherits an unquoted date"
""")


# Scenario 18: `!!binary` values: the payload's UTF-8 text (ASCII and CJK).
# ⛔ An INVALID UTF-8 byte cannot be a golden row: golden.json stores
# effective_config as JSON text, which cannot hold that byte, so the Go
# EffectiveConfig / ResolveEffective legs would compare the exporter's
# six-character escape (backslash, `ufffd`) with a real U+FFFD and go red
# on a correct pair. That
# shape's Go rendering is pinned in tests/dx/test_describe_tenant.py.
def s_yaml_binary():
    d = reset("mixed-mode") / "yaml-binary"
    write(d / "tenants.yaml", """tenants:
  tenant-binary:
    _routing:
      receiver:
        type: "webhook"
        url: !!binary aHR0cHM6Ly9ob29rcy5leGFtcGxlLmNvbS9hbGVydHM=
    _silent_mode:
      target: "warning"
      reason: !!binary 5Lit5paH
""")


# Scenario 19: non-string mapping KEYS below the tenant id (#2371). yaml.v3
# decodes such a mapping into map[any]any and pkg/config spells each key with
# `%v`: a date key is "2026-12-31 00:00:00 +0000 UTC" (time.Time.String()),
# `0x1F` is "31", `1.0` is "1", `True` is "true"; a quoted `"010"` stays text.
# The date key is written in BOTH files: one key to Go, so the two bodies
# deep-merge — describe_tenant must merge them too, not emit two keys.
def s_yaml_keys():
    d = reset("mixed-mode") / "yaml-keys"
    write(d / "_defaults.yaml", """defaults:
  _x:
    2026-12-31:
      from_defaults: 1
    0x1F: "hex"
""")
    write(d / "tenants.yaml", """tenants:
  tenant-keys:
    _x:
      2026-12-31:
        from_tenant: 2
      2026-12-31T10:20:30.5+08:00: "zoned"
      1.0: "float"
      True: "bool"
      "010": "quoted"
""")


# Scenario 20 (#2415): int / float VALUES yaml.v3 types by YAML 1.2's core
# schema, where PyYAML's YAML 1.1 typing differs — `1e3` / `0o17` / `+.5` /
# `08` are numbers to yaml.v3 only, `12:30:45` / `1_2:30` (sexagesimal) are
# strings there — and floats as encoding/json writes them (`1.0` is `1`,
# `1.5e-5` is `0.000015`). Tenant file only: a `_defaults.yaml` here would
# add artifact keys that make this subtree's root the biggest counted group
# of check_threshold_reachability's key floor. The full per-shape table, in
# the tenant file AND in `_defaults.yaml`, every row measured with pkg/config,
# is tests/shared/yaml_number_value_matrix.json.
def s_yaml_numbers():
    d = reset("mixed-mode") / "yaml-numbers"
    write(d / "tenants.yaml", """tenants:
  tenant-numbers:
    _x:
      whole_float: 1.0
      octal_1_2: 0o17
      exponent: 1e3
      sexagesimal: 12:30:45
      sexagesimal_underscore: 1_2:30
      small: 1.5e-5
      list: [+.5, 08, 0x1F, 1_000, "0o17"]
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
    ("canonical-json-escaping", "tenant-escape", s_canonical_json_escaping),
    ("reserved-nested-null", "tenant-nested", s_reserved_nested_null),
    ("reserved-null-delete", "tenant-reserved", s_reserved_null_delete),
    ("served-chain", "tenant-served-chain", s_served_chain),  # 2 tenants, 1 tree
    ("served-root", "tenant-served-root", None),
    ("served-disable", "tenant-served-disable", s_served_disable),
    ("yaml-date", "tenant-date", s_yaml_date),
    ("yaml-binary", "tenant-binary", s_yaml_binary),
    ("yaml-keys", "tenant-keys", s_yaml_keys),
    ("yaml-numbers", "tenant-numbers", s_yaml_numbers),
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
        "canonical-json-escaping": "mixed-mode",
        "reserved-nested-null": "mixed-mode",
        "reserved-null-delete": "mixed-mode",
        "served-chain": "served-chain",
        "served-root": "served-chain",
        "served-disable": "served-disable",
        "yaml-date": "mixed-mode",
        "yaml-binary": "mixed-mode",
        "yaml-keys": "mixed-mode",
        "yaml-numbers": "mixed-mode",
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
    failed = [f"{g['scenario']}/{g['tenant_id']}" for g in golden if "error" in g]
    if failed:
        # Non-zero is the contract (#1551): golden.json was still written, but
        # the failed entries carry no hashes and will break both parity legs.
        print(f"ERROR: {len(failed)} scenario(s) failed to regenerate: "
              f"{', '.join(failed)}; golden.json is incomplete",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
