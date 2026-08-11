#!/usr/bin/env python3
"""Threshold-key reachability gate — every alert-consumed threshold key must be producible by the platform-defaults path (TRK-337 / #1189).

WHY: a rule-pack alert reads its threshold from `tenant:alert_threshold:<key>`,
which is populated from `user_threshold{component,metric}` — emitted by the
threshold-exporter for keys present in `c.Defaults`
(`resolveBaseRows` iterates `c.Defaults`; `resolveCriticalRows` requires the
base key in `c.Defaults`).

⚠️ There is now a THIRD emission path (#1189 / TRK-337): `resolveDeclaredRows`
emits a key listed in `optional_overrides` when — and only when — the TENANT
supplied a value. This gate deliberately keeps measuring the platform-defaults
path, because "the platform can produce it" and "a tenant happened to set it"
are different questions and only the first is a property of what we ship.

That path is WIRED as of #1310: the platform now SHIPS those key names in the
runtime `optional_overrides:` list (helm values + the dev conf.d template), so
the per-key decision the old class-A tag left open has been made — the rump
stays declared-without-value and the TENANT calibrates it. Moving one into
`defaults:` would arm a platform-chosen number for every tenant and is a
product decision, not a mechanical repair (#1311). What that buys is "the
tenant can finally set it", NOT "the alert is live": no tenant value ⇒ no row
⇒ still silent, by design.

The platform-defaults surface is produced by
`scaffold_tenant.generate_defaults()`. So an alert that demands a key which
`generate_defaults()` never produces is DEAD — it can never fire, and nothing
says so. This is a DECLARED-BUT-UNWIRED failure: schema validation can't catch
it (it's a cross-artifact topology gap between the alert side and the
config-generation side, maintained as separate hand-copied contracts).

WHAT THIS CHECKS (identity comparison, not substring): the set of conf.d keys
DEMANDED by alerts (via `_observed_map_lib.all_threshold_keys`, the same
extractor the observed-map drift-guard uses) must be a subset of what the
platform-defaults path can SUPPLY, under the exporter's reachability rules:
  - a `_critical` key is reachable iff its base (strip `_critical`) is supplied
    (resolveCriticalRows path);
  - any other key is reachable iff it is itself supplied (resolveBaseRows path);
  - KNOWN_DEFERRED keys (threshold lives in a `:core` recording rule, not a
    `- alert:`, so the alert-based extractor can't reach them) are exempt —
    reused verbatim from `_observed_map_lib` so the two guards agree.

KNOWN_UNWIRED: the keys already dead at the time this gate landed (18 then;
the 9 B/C/D/E identity repairs shipped in #1231, 9 A-class
declared-without-value keys remain).
They are grandfathered as INFO (not errors) with a pointer to the tracking
issue so the gate could merge without first fixing all of them — the real
fixes (move / rename / delete per root cause) land per root-cause class in
follow-up PRs. The allowlist is EXIT-LOCKED (same
discipline as KNOWN_DEFERRED): a grandfathered key that becomes reachable, or
disappears from the alert demand, is a HARD error — this forces the list to
shrink as fixes land, so it can never rot into a permanent silent exemption.

TWO SUPPLY FACES (P1-C). The check above answers "can the platform-defaults
path produce this key" using `scaffold_tenant.generate_defaults()` — a TOOL'S
CAPABILITY. That is the right question for "is this key dead", but it is not
what an operator who ran `helm install` receives. The chart ships
`thresholdConfig.defaults`, which is far smaller. So the same demand set is
classified against BOTH faces:

  1. chart-armed          — the shipped chart supplies it
  2. NOT_CHART_ARMED      — the scaffold path can supply it, the chart does not
                            (exit-locked ledger; operator must supply)
  3. KNOWN_UNWIRED        — neither face can supply it (exit-locked ledger)

Before this face existed the gate was green while describing face 1+2 as one
number, so "how many shipped alerts cannot fire" was never measured by
anything. ⚠️ Both faces use the same reachability rule, which means
"chart-armed" says A TENANT CAN ARM IT — not that the alert fires out of the
box. `_critical` keys count as reachable via their base, yet
`resolveCriticalRows` iterates TENANT OVERRIDES: no `_critical` key is ever
platform-supplied, and none is in the chart set today.

THIRD FACE — DECLARED-LIST CONTAINMENT (#1310). Face 3 above says the platform
supplies no value for a KNOWN_UNWIRED key. That is a defensible posture ONLY
while the tenant can supply one instead, and a tenant can only write a key the
platform has DECLARED: `ValidateTenantKeys` refuses an unknown key and
tenant-api turns the refusal into a write rejection. So every still-dead
grandfathered key must appear on the shipped `optional_overrides:` list, else
"we leave this one to the tenant" is not a posture at all — nobody can set it
and the alert is structurally unable to fire. That containment is a HARD error,
and it is the detection this gate did not have before: dropping a key off the
list used to change nothing anyone measured.

⛔ "the shipped list" is THREE artifacts, and the one the error message is
about is not the one that is easiest to read (`_declared_faces`). The chart's
`thresholdConfig.optional_overrides` is rendered into the ConfigMap
threshold-exporter mounts. tenant-api — the writer whose refusal the message
actually describes — runs `--config-dir=/conf.d`, which an init container fills
by cloning the CUSTOMER's GitOps repo; the `_defaults.yaml` in there comes from
`scaffold_tenant.generate_defaults` or `init_project._gen_defaults_yaml`. A
containment check that only read values.yaml would assert about the wrong
process, and would have passed while every tenant write still 400'd.

Exit codes (_lib_exitcodes): 0 clean / 1 violation (--ci) / 2 caller error.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_OPS = PROJECT_ROOT / "scripts" / "tools" / "ops"
sys.path.insert(0, str(_OPS))
import _observed_map_lib as observed_map_lib  # noqa: E402
import scaffold_tenant  # noqa: E402
# The `<base>_critical` suffix, from the module that already owns it for the
# generators (`defaults_critical_keys` / `is_shipped_optional_key`). Spelling it
# again here would be one more copy of the very contract this gate polices.
from _registry_lib import CRITICAL_SUFFIX as _CRITICAL_SUFFIX  # noqa: E402

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

# The alert-demanded threshold keys that the platform-defaults path cannot
# yet supply. Grandfathered as INFO (see module docstring). Value = one-line
# root-cause tag; the real fixes are tracked in the TRK-337 follow-ups.
# Started at 18 when the gate landed; the B/C/D/E identity repairs shipped in
# #1231 (rename/move/add/delete — 9 keys removed via the exit-lock below),
# leaving the 9 A-class keys, which are NOT a pending mechanical fix.
#   A = name-correct, tier `optional_overrides`, and now SHIPPED on the runtime
#       `optional_overrides:` declared list (#1310) → the tenant sets it and it
#       fires (`resolveDeclaredRows`); the platform asserts no value on purpose,
#       so it stays silent until the tenant does. That is the END STATE, not a
#       waypoint: promoting one to `defaults:` would arm a platform-chosen
#       number for every tenant, which is a product decision (#1311), and
#       ADR-030's blind-write reference library has cross-pack counter-examples
#       for exactly these numbers. Re-deriving any single key's tier or value is
#       tracked separately (#1196 / TRK-337).
#   B = name is wrong in scaffold (e.g. _total vs _rate) → rename + move
#   C = base default exists but under a different name → align identity
#   D = key absent from scaffold entirely → add
#   E = no alert actually consumes it elsewhere / orphan → delete
#
# ⚠️ One shared constant, not nine copies: the tag is what `main()` PRINTS
# (see the `known-unwired` info below), and it was the printed string — not the
# comment above it — that kept prescribing "move to defaults" for a full release
# after the comment had been corrected. A test pins its CONTENT, not just its
# `A:` prefix, for that reason.
_A_DECLARED = (
    "A: declared-without-value — shipped on the platform's optional_overrides: "
    "list, so a tenant can set it and it fires (resolveDeclaredRows); the "
    "platform asserts no value by design, so it stays silent until they do. "
    "Deliberate end state, not a pending repair (#1310 / #1311)."
)

KNOWN_UNWIRED: dict[str, str] = {
    "oracle_wait_time_rate": _A_DECLARED,
    "oracle_process_count": _A_DECLARED,
    "oracle_pga_allocated_bytes": _A_DECLARED,
    "db2_log_usage_percent": _A_DECLARED,
    "db2_deadlock_rate": _A_DECLARED,
    "db2_tablespace_used_percent": _A_DECLARED,
    "clickhouse_max_part_count": _A_DECLARED,
    "clickhouse_replication_queue": _A_DECLARED,
    "clickhouse_memory_tracking_bytes": _A_DECLARED,
}


# ── Second supply face: what the SHIPPED chart actually installs ────────────
# `_supply()` answers "what CAN the platform-defaults path produce" — it reads
# the scaffold generator. That is the right question for "is this key dead",
# but it is NOT the question an operator who just ran `helm install` cares
# about. The chart ships `thresholdConfig.defaults`, a much smaller set than
# what the scaffold tool is capable of emitting. Everything in between only
# becomes real if somebody runs the onboarding tool or hand-writes the
# defaults — and until this face existed, nothing said so: the gate was green
# while reporting a number that described a tool's capability, not a
# deployment.
#
# ⚠️ "chart-armed" here means A TENANT CAN ARM IT, not "it fires out of the
# box". A `_critical` key counts as reachable when its BASE is supplied (the
# `resolveCriticalRows` path) — but `resolveCriticalRows` iterates TENANT
# OVERRIDES, so no `_critical` key is ever platform-supplied. Today 0 of the 16
# demanded `_critical` keys sit in the chart set and structurally none can.
NOT_CHART_ARMED: frozenset[str] = frozenset({
    "clickhouse_active_connections",
    "clickhouse_queries_rate",
    "db2_bufferpool_hit_ratio",
    "db2_connections_active",
    "db2_lock_wait_time",
    "es_disk_usage_percent",
    "es_heap_usage_percent",
    "es_pending_tasks",
    "es_search_latency_ms",
    "jvm_gc_pause",
    "jvm_gc_pause_critical",
    "jvm_memory",
    "jvm_memory_critical",
    "jvm_threads",
    "jvm_threads_critical",
    "kafka_active_controllers",
    "kafka_broker_count",
    "kafka_consumer_lag",
    "kafka_consumer_lag_critical",
    "kafka_request_rate",
    "kafka_request_rate_critical",
    "kafka_under_replicated_partitions",
    "kafka_under_replicated_partitions_critical",
    "mongodb_connections_current",
    "mongodb_opcounters_rate",
    "mongodb_replication_lag",
    "nginx_connections",
    "nginx_connections_critical",
    "nginx_request_rate",
    "nginx_request_rate_critical",
    "nginx_waiting",
    "nginx_waiting_critical",
    "oracle_sessions_active",
    "oracle_tablespace_used_percent",
    "rabbitmq_connections",
    "rabbitmq_consumers",
    "rabbitmq_node_mem_percent",
    "rabbitmq_node_mem_percent_critical",
    "rabbitmq_queue_messages",
    "rabbitmq_queue_messages_critical",
    "rabbitmq_unacked_messages",
    "redis_connected_clients",
    "redis_evicted_keys_rate",
    "redis_memory_used_bytes",
    "redis_replication_lag",
})

_CHART_VALUES = PROJECT_ROOT / "helm" / "threshold-exporter" / "values.yaml"


def _supply() -> set[str]:
    """Every conf.d threshold key the platform-defaults path can produce."""
    db_packs = [k for k in scaffold_tenant.RULE_PACKS if k != "kubernetes"]
    generated = scaffold_tenant.generate_defaults(db_packs)
    return set(generated["defaults"].keys())


def _chart_supply() -> set[str]:
    """Every threshold key the SHIPPED helm chart installs as a platform default.

    Reads the artifact, not the registry: `chart_default: true` in the registry
    is the DECLARATION, `thresholdConfig.defaults` in values.yaml is what an
    operator actually gets. The two are kept in step by
    check_threshold_registry.py; reading values.yaml here means this gate keeps
    measuring the deployment even if that equivalence ever breaks.
    """
    import yaml  # local import: only this face needs it

    doc = yaml.safe_load(_CHART_VALUES.read_text(encoding="utf-8")) or {}
    return set(((doc.get("thresholdConfig") or {}).get("defaults") or {}).keys())


def _shipped_optional() -> set[str]:
    """Every key name the SHIPPED chart DECLARES without a value (#1310).

    `thresholdConfig.optional_overrides` — the list `ThresholdConfig.
    OptionalOverrides` is loaded from, which `ValidateTenantKeys` consults as a
    second membership set. A key on it is TENANT-SETTABLE; a key off it is not
    writable by anyone.

    Same artifact-not-declaration discipline as `_chart_supply()` above: the
    registry's `tier: optional_overrides` is what we MEAN to ship, this list is
    what an operator's exporter actually recognises.
    `check_threshold_registry.py` keeps the two in step; reading values.yaml
    here means this face keeps
    measuring the deployment even if that equivalence ever breaks.
    """
    import yaml  # local import: only the chart faces need it

    doc = yaml.safe_load(_CHART_VALUES.read_text(encoding="utf-8")) or {}
    return set((doc.get("thresholdConfig") or {}).get("optional_overrides") or [])


def _onboarding_declared() -> set[str]:
    """Every key name the ONBOARDING `_defaults.yaml` declares without a value.

    ⛔ A SECOND face, not a duplicate of `_shipped_optional()`, because the two
    lists reach two different processes. The chart's list is rendered into the
    ConfigMap that threshold-exporter mounts. tenant-api — the only supported
    WRITER, the one that turns `ValidateTenantKeys` into an HTTP 400 — runs with
    `--config-dir=/conf.d`, and that directory is the customer's GitOps repo
    cloned by an init container (`helm/tenant-api/templates/deployment.yaml`),
    not the chart's ConfigMap. `config.mergeTenantConfig` reads
    `<config-dir>/_defaults.yaml` for `OptionalOverrides`, so the list a tenant
    is actually validated against is the one `scaffold_tenant.generate_defaults`
    wrote into the customer repo.

    Ship the chart list alone and the message the gate prints below ("a TENANT
    cannot set it either") stays true while the gate says everything is fine —
    which is precisely the shape of failure this face exists to catch.
    """
    db_packs = [k for k in scaffold_tenant.RULE_PACKS if k != "kubernetes"]
    generated = scaffold_tenant.generate_defaults(db_packs)
    return set(generated.get("optional_overrides") or [])


def _init_project_declared() -> set[str]:
    """Same question as `_onboarding_declared()`, for the OTHER producer.

    `da-tools init` (`init_project._gen_defaults_yaml`) writes the customer's
    `_defaults.yaml` for repos that bootstrap rather than scaffold. Covering
    only one of the two producers would leave the other free to drop the list
    silently — and "I fixed the instance the review named" is how this contract
    ended up with copies in the first place.
    """
    import yaml  # local import: only this face needs it

    # Guarded: this face is called per run (and per test), and an unguarded
    # insert would grow sys.path without bound. The module-level insert above
    # already put _OPS on the path; this is belt-and-braces for callers that
    # import the module and mutate sys.path themselves.
    if str(_OPS) not in sys.path:
        sys.path.insert(0, str(_OPS))
    import init_project  # noqa: E402

    packs = sorted(init_project.RULE_PACK_CATALOG)
    doc = yaml.safe_load(init_project._gen_defaults_yaml(packs, "monitoring")) or {}
    return set(doc.get("optional_overrides") or [])


# Face label → producer callable. Order is report order; the label is printed
# verbatim in the UNSETTABLE error so the reader knows WHICH surface to repair.
def _declared_faces() -> dict[str, set[str]]:
    """{face label: declared key names} for every surface that must carry them."""
    return {
        "chart (helm/threshold-exporter/values.yaml → threshold-exporter)":
            _shipped_optional(),
        "onboarding/scaffold (scaffold_tenant.generate_defaults → the customer "
        "conf.d that tenant-api validates against)": _onboarding_declared(),
        "onboarding/init (init_project._gen_defaults_yaml → same, for "
        "`da-tools init` repos)": _init_project_declared(),
    }


# ── FOURTH FACE — defaults-tier placement (#1218 / TRK-344) ─────────────────
#
# A different QUESTION from everything above. The faces so far ask "can this
# key be produced"; this one asks "is this key in a section where the resolver
# can act on it at all". `<base>_critical` is the one shape where the answer
# depends on WHICH FILE SECTION holds it, and where the wrong section fails
# silently in both directions at once:
#
#   * `resolveCriticalRows` iterates TENANT OVERRIDES and admits on
#     `defaults[<base>]` — a `_critical` key under `defaults:` is never even
#     looked at, so the `*Critical` alert cannot fire;
#   * `resolveBaseRows` walks `defaults` and does NOT skip the suffix, and
#     `parseMetricKey` splits on the first underscore — so the same key emits
#     `{metric="<base>_critical", severity="warning"}`, one unconsumed series
#     per tenant per key.
#
# Both directions are measured in
# `components/threshold-exporter/app/pkg/config/critical_tier_placement_test.go`.
#
# The dimensional shape rides along for the same reason and from the same
# source: `TestDocsDefaultsSamplesHaveNoTenantOnlyKeys` pins BOTH, and
# `_registry_lib.is_shipped_optional_key` treats them as one pair. A
# `metric{label="v"}` key under `defaults:` is equally inert —
# `resolveDimensionalRows` is tenant-only and never consults the defaults map,
# and `parseMetricKey` bakes the label segment into the metric NAME. Covering
# only the half this issue was reported about is how the other half survives.
#
# ⛔ Why here and not a new lint: the rule already existed as that Go test —
# aimed at DOCUMENTATION samples only. `init_project._gen_defaults_yaml` shipped
# 16 such keys across 11 packs for a year while it was green, because the
# generators are Python and produce no file for a Go test to read. This module
# already imports every producer for the declared faces, so the missing reach is
# a few lines here rather than a ninth `check_*.py` and its hook cascade.
#
# ⛔ SHAPE, not membership: this face makes no claim about whether a key belongs
# in the product. A `_critical` key is legitimate — in `<tenant>.yaml`.
# ⛔⛔ DERIVED, never enumerated. Two hardcoded `Path` constants stood here and
# blind review walked straight past them three different ways: an `examples/`
# sibling inside the very conf.d tree they named
# (`conf.d/examples/_defaults-multidb.yaml`, a copy-me file whose own header
# teaches this rule), a NESTED `_defaults.yaml` (the loader enters one at every
# depth — `config_hierarchy.go` — while the constants named depth 0 only), and a
# whole second conf.d root (`rule-packs/recipes/examples/conf.d/`). All three
# were demonstrated: inject `mysql_connections_critical` into any of them and
# this gate exited 0 without a word.
#
# ⛔ SCOPE is `git ls-files`, not a filesystem walk, and that is the second
# correction this face needed. The first draft used `PROJECT_ROOT.rglob` with a
# directory-name denylist; three reviewers converged on it independently and the
# measurement is unambiguous — from the MAIN repo root that walk yields **643
# files in 18.7s, 618 of them under `.claude/worktrees/`**, i.e. OTHER BRANCHES'
# working copies. A commit touching nothing relevant could be blocked by an
# error naming a path that is not in the author's tree, on this repo's own
# standard worktree layout. Tracked scope is 17.
#
# The denylist went with it rather than being extended: it matched any path
# COMPONENT, so a customer-shaped `conf.d/site/_defaults.yaml` (multi-site
# layouts really are `<root>/<domain>/<region>/<env>`) was silently dropped — a
# demonstrated bypass. `git ls-files` needs no denylist: build output and
# virtualenvs are not tracked.
#
# ⛔ The predicate is CASE-INSENSITIVE, matching the loader rather than the
# sibling Python lint. `config_hierarchy.go` lowercases before both its suffix
# and its filename test, so `_defaults.YAML` IS loaded; `check_confd_schema.py`
# compares case-sensitively, so borrowing its spelling inherited a divergence
# that let `_defaults.YAML` through on every platform (demonstrated), and made
# `_Defaults.yaml` red on Windows and green on Linux CI — the wrong way round.
#
# ⚠️ COVERAGE, stated honestly: 17 files match, all clean today. Only **6** of
# them can express this defect at the layer checked here — the other 11 are 2
# legitimately-empty roots plus 9 `tests/golden/fixtures/**` files on the
# hierarchy-loader schema, where thresholds live under `defaults.threshold.*`
# and `_defaults_section` reads the top level only (measured: injecting there
# is silent, injecting at top level is caught). Making the check schema-aware is
# tracked in #1392; this comment must not be shortened into "17 files covered".
_DEFAULTS_ARTIFACT_SUFFIXES = (".yaml", ".yml")

# ⛔ Explicit, and empty on purpose. A fixture that deliberately encodes the
# defective shape (to characterise the loader, say) belongs HERE with a reason —
# not hidden behind a blanket `tests/` exclusion, which is how a guard quietly
# stops covering the tree that grows fastest. Keys are repo-relative POSIX paths.
_DEFAULTS_ARTIFACT_EXEMPT: dict[str, str] = {}

# Non-vacuity floor. `EMPTY-FACE` catches a face whose `defaults:` is empty; it
# cannot catch a WALK that returned no files at all, which is what a bad
# exclusion or a moved tree looks like. Measured 17 at the time of writing; the
# floor is deliberately below that so ordinary deletions do not trip it, and
# deliberately above 2 so a regression to the old hardcoded pair does.
_DEFAULTS_ARTIFACT_FLOOR = 10

# Label prefix that tells the two face classes apart. Generators must be
# non-empty; artifacts may legitimately be (see the EMPTY-FACE branch).
# ⚠️ A STRING is a weak discriminator and the hole is on the generator side —
# a generator whose label started with this prefix would be silently treated as
# an artifact, and  cannot see it because the count does
# not change. Zero instances today (all four labels are hardcoded literals).
# Typed faces tracked in #1393.
_ARTIFACT_FACE_PREFIX = "artifact ("


def _is_defaults_artifact(name: str) -> bool:
    """The loader's own rule, lowercased on both halves (see the note above)."""
    lower = name.lower()
    return lower.startswith("_defaults") and lower.endswith(_DEFAULTS_ARTIFACT_SUFFIXES)


def _tracked_defaults_artifacts() -> list[str]:
    """Repo-relative paths of every TRACKED platform-defaults artifact.

    Separate from `_defaults_artifacts` so the floor can count what the scan
    found BEFORE exemptions are subtracted — otherwise exempting files walks the
    count down toward the floor and the floor's message ("repair the scan or
    move the file(s) back") names the one repair that is not the problem.
    """
    import subprocess  # local: only this face shells out

    out = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "ls-files", "-z", "--", "*_defaults*"],
        capture_output=True, check=True, timeout=60)
    return sorted(
        rel for rel in out.stdout.decode("utf-8", "replace").split("\0")
        if rel and _is_defaults_artifact(rel.rsplit("/", 1)[-1])
        and (PROJECT_ROOT / rel).is_file())


def _defaults_artifacts() -> list[Path]:
    """The artifacts this face reads: tracked, minus the explicit exemptions."""
    return [PROJECT_ROOT / rel for rel in _tracked_defaults_artifacts()
            if rel not in _DEFAULTS_ARTIFACT_EXEMPT]

# The probe fed to the `onboard` face below. A severity pair on one metric is
# the minimal input that used to manufacture the defect, and it is spelled here
# rather than derived so the probe cannot go vacuous with the producer.
_ONBOARD_PROBE = (
    {"status": "perfect", "metric_key": "zzz_probe_metric",
     "severity": "warning", "threshold_value": "80"},
    {"status": "perfect", "metric_key": "zzz_probe_metric",
     "severity": "critical", "threshold_value": "150"},
)


def _defaults_section(text: str) -> set[str]:
    """The key names under `defaults:` of a rendered `_defaults.yaml`/values.yaml."""
    import yaml  # local import: only the artifact faces need it

    doc = yaml.safe_load(text) or {}
    root = doc.get("thresholdConfig") or doc  # chart values nest one level
    return set((root.get("defaults") or {}).keys())


def _defaults_faces() -> dict[str, set[str]]:
    """{face label: `defaults:` key names} for every producer of that section.

    ⛔ Every producer, and the list is the point. Five of these six were already
    correct when the other was not, so a gate that read "the" defaults file
    would have been green on any one of them — the same one-face blindness the
    UNSETTABLE check above had to grow out of. The GENERATED faces are rendered
    here rather than read off disk because a `da-tools init` customer's
    `_defaults.yaml` exists only in their repo; the generator IS the artifact.

    ⛔ `onboard` is a PROBE, not a render, and it is the face this list would
    most easily have been written without. `da-tools onboard` reverse-engineers
    a customer's existing Alertmanager estate and writes
    `phase2-rules/_defaults-suggestion.yaml`, whose own header says "Review and
    merge into conf.d/_defaults.yaml" — so until #1218 it turned every
    critical-severity rule it recovered into a `<key>_critical` under
    `defaults:`, i.e. it manufactured this defect from customer input, on the
    one code path whose entire purpose was carrying a critical tier across. It
    has no shipped artifact to read, so the face feeds it a fixed candidate pair
    and reads what it renders. Found by blind review, not by the issue.
    """
    if str(_OPS) not in sys.path:
        sys.path.insert(0, str(_OPS))
    import init_project  # noqa: E402
    import onboard_platform  # noqa: E402

    init_packs = sorted(init_project.RULE_PACK_CATALOG)
    scaffold_packs = [k for k in scaffold_tenant.RULE_PACKS if k != "kubernetes"]
    onboard_suggestion = onboard_platform.generate_defaults_from_candidates(
        [dict(c) for c in _ONBOARD_PROBE])

    faces = {
        # GENERATORS — no artifact on disk to read; the producer IS the surface.
        "chart (helm/threshold-exporter/values.yaml)":
            _defaults_section(_CHART_VALUES.read_text(encoding="utf-8")),
        "onboarding/scaffold (scaffold_tenant.generate_defaults)":
            set(scaffold_tenant.generate_defaults(scaffold_packs)["defaults"]),
        "onboarding/init (init_project._gen_defaults_yaml)":
            _defaults_section(
                init_project._gen_defaults_yaml(init_packs, "monitoring")),
        "migration/onboard (onboard_platform.generate_defaults_from_candidates, "
        "probed)": set(onboard_suggestion.get("defaults") or {}),
    }

    # ARTIFACTS — derived (see `_defaults_artifacts`). The floor fires BEFORE the
    # per-face loop, because an empty scan produces zero faces and zero faces
    # pass every check in that loop perfectly.
    #
    # ⛔ It counts what the SCAN found, not what survives the exemption table.
    # Counting after subtraction makes exemptions walk the total toward the floor
    # and then blames the scan: the message would say "repair the scan or move
    # the file(s) back" while the only correct action is to shorten the exemption
    # list, which it explicitly forbids.
    scanned = _tracked_defaults_artifacts()
    if len(scanned) < _DEFAULTS_ARTIFACT_FLOOR:
        raise RuntimeError(
            f"defaults-artifact scan found only {len(scanned)} tracked file(s), "
            f"below the floor of {_DEFAULTS_ARTIFACT_FLOOR}. A scan that returns "
            "(almost) nothing passes the placement check vacuously, which is the "
            "shape the hardcoded-path version of this face shipped with. Repair "
            "the scan or restore the file(s); do not lower the floor. (Exemptions "
            f"are NOT counted here — {len(_DEFAULTS_ARTIFACT_EXEMPT)} exempted.)")
    for path in _defaults_artifacts():
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        # ⛔ The reader names the file. `read_text` raises with the path attached,
        # but `yaml.safe_load` does not — its error says `<unicode string>`, and
        # with the scope at 17 files rather than 2 hardcoded ones, "which file"
        # is precisely the information that was missing (blind review, round 4).
        try:
            faces[f"{_ARTIFACT_FACE_PREFIX}{rel})"] = _defaults_section(
                path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — re-raised with provenance
            raise RuntimeError(f"{rel}: {exc}") from exc
    return faces


def _reachable(key: str, supply: set[str], deferred: set[str]) -> bool:
    if key in deferred:
        return True
    if key.endswith(_CRITICAL_SUFFIX):
        return key[: -len(_CRITICAL_SUFFIX)] in supply
    return key in supply


def run_check(
    demand: set[str] | None = None,
    supply: set[str] | None = None,
    deferred: set[str] | None = None,
    known_unwired: dict[str, str] | None = None,
    chart_supply: set[str] | None = None,
    not_chart_armed: frozenset[str] | None = None,
    declared_faces: dict[str, set[str]] | None = None,
    defaults_faces: dict[str, set[str]] | None = None,
) -> dict[str, list[str]]:
    """Return {errors, infos}. errors fail --ci; infos are report-only.

    Inputs default to the real extractors; hermetic tests inject synthetic sets
    to exercise each branch without editing repo artifacts.
    """
    if demand is None:
        demand = observed_map_lib.all_threshold_keys(observed_map_lib.default_pack_paths())
    injected_supply = supply is not None
    if supply is None:
        supply = _supply()
    if deferred is None:
        deferred = set(observed_map_lib.KNOWN_DEFERRED)
    injected_known_unwired = known_unwired is not None
    if known_unwired is None:
        known_unwired = KNOWN_UNWIRED
    if declared_faces is None:
        # Same hermeticity rule the chart face states below, applied to the
        # ledger this face is scoped by: a caller that injected a synthetic
        # KNOWN_UNWIRED is exercising some other branch, and defaulting to the
        # real artifacts would flag every one of its made-up keys. No
        # declared-list info given ⇒ containment is vacuous for that call
        # (zero faces to check, rather than one face that trivially contains
        # everything — same outcome, and it cannot accidentally pass a real
        # containment assertion).
        declared_faces = {} if injected_known_unwired else _declared_faces()
    if defaults_faces is None:
        # ⛔ Unlike the faces above, this one does NOT go vacuous for hermetic
        # callers. It asserts a property of the SHIPPED artifacts and reads no
        # synthetic input, so there is nothing for an injected demand/supply/
        # ledger to make inconsistent — and defaulting it to `{}` would mean the
        # only test that exercises it is one written specifically for it. Tests
        # that need it silent pass `defaults_faces={}` explicitly.
        defaults_faces = _defaults_faces()
    if chart_supply is None:
        # The chart face is a NARROWING of the supply face. A caller that
        # injected a synthetic supply but said nothing about the chart is
        # exercising the unreachability contract, not this one — defaulting to
        # the real values.yaml there would silently un-hermeticise their test
        # and flag every synthetic key. No chart info given ⇒ no narrowing.
        chart_supply = supply if injected_supply else _chart_supply()
    if not_chart_armed is None:
        not_chart_armed = frozenset() if injected_supply else NOT_CHART_ARMED

    dead = {k for k in demand if not _reachable(k, supply, deferred)}

    errors: list[str] = []
    infos: list[str] = []

    # NEW dead keys (not grandfathered) — a fresh declared-but-unwired drift.
    for k in sorted(dead - set(known_unwired)):
        errors.append(
            f"UNREACHABLE: alert-demanded threshold key {k!r} is not produced by "
            "generate_defaults() and is not a known-deferred key — this alert can "
            "never fire. Add the key to the platform defaults, or fix the name "
            "mismatch. (TRK-337)"
        )

    # Grandfathered keys are report-only WHILE still dead...
    for k in sorted(dead & set(known_unwired)):
        infos.append(f"known-unwired {k} — {known_unwired[k]}")

    # ...and only while a TENANT can still supply what the platform will not.
    # Declared-list containment (#1310): "the platform asserts no value here"
    # is a posture only if the key is on the shipped `optional_overrides:`
    # list. Off the list `ValidateTenantKeys` rejects it as unknown (and
    # tenant-api turns that into a write rejection), so nobody can set it and
    # the alert is structurally unable to fire — with no other signal saying
    # so. Scoped to the still-dead entries: a grandfathered key that is no
    # longer dead is already reported by the exit-lock below, and saying
    # "...and it is off the list too" would just double-report it.
    #
    # ⛔ EVERY face, not just the chart one. The message says "a TENANT cannot
    # set it either", and the process that enforces that on a tenant is
    # tenant-api reading the CUSTOMER's `_defaults.yaml` — a different artifact
    # from the chart's ConfigMap, written by a different producer. A one-face
    # check would keep printing that sentence while the face it names is fine
    # and the face it means is empty. Which face is missing is named, because
    # the repair differs per producer.
    for k in sorted(dead & set(known_unwired)):
        missing = [face for face, declared in declared_faces.items()
                   if k not in declared]
        if not missing:
            continue
        errors.append(
            f"UNSETTABLE: threshold key {k!r} is demanded by an alert and is "
            "grandfathered in KNOWN_UNWIRED (the platform deliberately asserts "
            "no value for it), but it is NOT on the shipped optional_overrides: "
            "declared list of: " + "; ".join(missing) + " — so a TENANT cannot "
            "set it either (ValidateTenantKeys refuses an undeclared key) and "
            "that alert is structurally unable to fire. Ship the key on every "
            "declared face (edit scaffold_tenant.RULE_PACKS, then "
            "check_threshold_registry.py --regen), or land a real fix and drop "
            "it from KNOWN_UNWIRED. (#1310 / TRK-337)"
        )

    # ...but the allowlist is exit-locked: a grandfathered key that got FIXED
    # (now reachable) or was REMOVED from the packs must be dropped from the
    # list, else it rots into a permanent silent exemption.
    for k in sorted(set(known_unwired) - dead):
        if k in demand:
            errors.append(
                f"STALE-EXEMPTION: {k!r} is in KNOWN_UNWIRED but is now REACHABLE — "
                "the fix landed; remove it from KNOWN_UNWIRED so the gate protects it."
            )
        else:
            errors.append(
                f"STALE-EXEMPTION: {k!r} is in KNOWN_UNWIRED but no alert demands it "
                "anymore — remove it from KNOWN_UNWIRED."
            )

    # ── Defaults-tier placement (#1218 / TRK-344) ────────────────────────────
    # Per FACE, not one global count: a single "no _critical anywhere" assertion
    # is satisfied by four clean faces while the fifth ships sixteen, which is
    # how this survived a year.
    for face, keys in sorted(defaults_faces.items()):
        # ⛔ The vacuity rule applies to GENERATORS only, and the asymmetry is
        # measured, not assumed: every generator ships a non-empty `defaults:`,
        # so an empty one means its reader broke — but 2 of the 17 derived
        # ARTIFACTS legitimately carry none — the recipes example root
        # `rule-packs/recipes/examples/conf.d/_defaults.yaml` and its `finance/`
        # child declare only the other sections. Applying the generator rule to
        # artifacts turned this gate red on arrival for two correct files.
        #
        # Vacuity is still closed for artifacts, by two other links: a file that
        # cannot be read RAISES (fail-closed, `main()` → EXIT_CALLER_ERROR with
        # the path), and a walk that returns (almost) nothing trips
        # `_DEFAULTS_ARTIFACT_FLOOR` before this loop runs. What is left —
        # "parsed fine, genuinely has no `defaults:`" — is a real state of a real
        # file, not a broken reader.
        if not keys and not face.startswith(_ARTIFACT_FACE_PREFIX):
            errors.append(
                f"EMPTY-FACE: the defaults-tier face {face!r} yielded no keys at "
                "all, and an empty set passes the placement check below "
                "vacuously. Every GENERATOR ships a non-empty `defaults:`, so "
                "either its `defaults:` section was renamed / re-nested, or a "
                "generator stopped emitting one. (An artifact that is MISSING "
                "does not reach here — the reader raises and `main()` exits "
                "EXIT_CALLER_ERROR naming the path.) Repair the reader, do not "
                "delete the face."
            )
            continue
        for k in sorted(k for k in keys if k.endswith(_CRITICAL_SUFFIX)):
            base = k[: -len(_CRITICAL_SUFFIX)]
            errors.append(
                f"CRITICAL-IN-DEFAULTS: {face} puts {k!r} under `defaults:`, which "
                "is not the critical tier and never becomes one: resolveCriticalRows "
                "iterates TENANT OVERRIDES, so this emits "
                f'user_threshold{{metric="{k}",severity="warning"}} — a series no '
                f"recording rule joins — while tenant:alert_threshold:{k} stays "
                "empty and the *Critical alert cannot fire. Move it to the tenant "
                f"side (a `<tenant>.yaml` override), keeping {base!r} under "
                "`defaults:` so the critical tier is admitted. (#1218 / TRK-344)"
            )
        # The other half of the same rule (docs_defaults_sample_test.go pins
        # both; is_shipped_optional_key refuses both). Kept in this loop rather
        # than a second one so a new face can never pick up one check and miss
        # the other.
        for k in sorted(k for k in keys if "{" in k):
            errors.append(
                f"DIMENSIONAL-IN-DEFAULTS: {face} puts {k!r} under `defaults:`, "
                "which gives dimensional thresholds no default path: "
                "resolveDimensionalRows is tenant-only and never consults the "
                "defaults map, while parseMetricKey bakes the label segment into "
                "the metric NAME. ValidateTenantKeys reports nothing. Move it to "
                "a `<tenant>.yaml` override. (#1218 / TRK-344)"
            )

    # ── Chart face (C): scaffold-reachable but NOT shipped by the chart ──────
    # Scope: only keys that are reachable at all. A key that is dead
    # everywhere is already reported above; saying "…and it isn't in the chart
    # either" would be noise.
    reachable = demand - dead
    not_armed = {k for k in reachable if not _reachable(k, chart_supply, deferred)}

    for k in sorted(not_armed - not_chart_armed):
        errors.append(
            f"NOT-CHART-ARMED: threshold key {k!r} is demanded by an alert and the "
            "scaffold path can supply it, but the shipped chart does NOT — an "
            "operator who only ran `helm install` gets an alert that cannot fire. "
            "Ship it in helm/threshold-exporter/values.yaml, or add it to "
            "NOT_CHART_ARMED with a deliberate operator-supplied decision. (TRK-337)"
        )

    for k in sorted(not_armed & not_chart_armed):
        infos.append(f"not-chart-armed {k} — operator must supply this default")

    # Exit-lock, both directions — same discipline as KNOWN_UNWIRED.
    for k in sorted(not_chart_armed - not_armed):
        if k not in demand:
            errors.append(
                f"STALE-EXEMPTION: {k!r} is in NOT_CHART_ARMED but no alert demands "
                "it anymore — remove it from NOT_CHART_ARMED."
            )
        elif k in dead:
            errors.append(
                f"STALE-EXEMPTION: {k!r} is in NOT_CHART_ARMED but is now UNREACHABLE "
                "everywhere — it belongs in KNOWN_UNWIRED, not here."
            )
        else:
            errors.append(
                f"STALE-EXEMPTION: {k!r} is in NOT_CHART_ARMED but the chart now "
                "ships it — remove it from NOT_CHART_ARMED so the gate protects it."
            )

    return {"errors": errors, "infos": infos}


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "閾值 key 可達性 gate：每個 alert 消費的閾值 key 都須能由平台 defaults 產生（TRK-337）",
            "Threshold-key reachability gate: every alert-consumed key must be "
            "producible by the platform-defaults path (TRK-337)"))
    parser.add_argument(
        "--ci", action="store_true",
        help=i18n_text("新增不可達 key、ledger exit-lock 破口、或列管 key 掉出出貨宣告清單即 exit 1（列管中的 A 類為 INFO）",
                       "exit 1 on NEW unreachable keys, a ledger exit-lock breach, or a tracked key dropping off the shipped declared list (the tracked A-class keys are INFO)"))
    args = parser.parse_args(argv)

    try:
        result = run_check()
    except Exception as exc:  # noqa: BLE001 — caller error, not a violation
        print(f"ERROR: reachability check crashed: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    errors = result["errors"]
    infos = result["infos"]

    for msg in infos:
        print(f"INFO: {msg}", file=sys.stderr)
    for msg in errors:
        print(f"❌ {msg}", file=sys.stderr)

    if errors:
        print(
            f"\n{len(errors)} threshold-reachability violation(s) — TRK-337.\n"
            "A declared-but-unwired alert key can never fire and no other gate "
            "catches it. See scripts/tools/lint/check_threshold_reachability.py.",
            file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK

    n_unwired = sum(1 for m in infos if m.startswith("known-unwired "))
    n_not_armed = sum(1 for m in infos if m.startswith("not-chart-armed "))
    print(
        f"✅ threshold reachability OK — {n_unwired} unreachable-everywhere "
        f"(KNOWN_UNWIRED, TRK-337) / {n_not_armed} reachable but NOT shipped by "
        "the chart (NOT_CHART_ARMED — operator must supply), 0 new drift.",
        file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
