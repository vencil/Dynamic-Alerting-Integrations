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

class _GateViolation(RuntimeError):
    """A floor breach: the tool ran fine, the REPOSITORY is in a state to fix.

    ⛔ Not a caller error, and the distinction is the repo's own SSOT:
    `scripts/tools/_lib_exitcodes.py` defines EXIT_VIOLATION (1) as "the tool ran
    correctly and found something the USER must act on" and EXIT_CALLER_ERROR (2)
    as "the tool could NOT do its job because of how it was invoked or its
    environment". "A shipped `_defaults.yaml` was deleted" is squarely the first,
    yet every floor in this module used to surface as `crashed: …` with rc=2 —
    and a maintainer reading "lint crashed" reaches for skip-it-and-move-on,
    which is the single reaction these floors exist to prevent (blind review).

    Subclasses RuntimeError on purpose: the floors are also asserted directly in
    tests with `pytest.raises(RuntimeError, ...)`, and narrowing that would be a
    second change riding along with this one.

    ⚠️ The READER's failures stay caller errors — an unreadable or malformed file
    genuinely means this tool could not do its job, and its message already
    carries the path.

    ⚠️ …and that deliberately includes the `defaults:` walk hitting
    `_DEFAULTS_MAX_DEPTH`, even though a recursive YAML anchor in a TRACKED file
    is a repo state rather than an environment one. It arrives through the
    reader's provenance wrapper (which is what attaches the filename), and
    splitting it out would mean classifying an exception by its text. Flagged
    here because the boundary is arguable, not because nobody noticed.
    """


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
#     `{component="<prefix>", metric="<rest>_critical", severity="warning"}`
#     (the suffix rides inside the metric label; `metric="<whole key>"` is the
#     #731 shape and names nothing), one unconsumed series per tenant per key.
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
# ⚠️ COVERAGE. ⛔ No count here, on purpose (#1392): the previous revision spelt
# one out, it was corrected once and went stale twice, and a number restated in
# a comment is a number nobody re-measures. `main()` PRINTS the live split on
# every run instead, so the honest figure is a byte of output rather than a
# claim in a comment that has to be maintained by hand.
#
# What the split used to say, and why it is gone: the reader only looked at the
# TOP level of `defaults:`, so a majority of matched files could not express
# this defect at the layer being checked — an injection one level down was
# silent, an injection at the top level was caught (both measured). The reader
# now walks the whole subtree (`_walk_defaults_keys`), so "matched" and
# "checked at every level it has" are the same set, and the only files that
# cannot express the defect are the ones with no `defaults:` section at all.
#
# ⛔ Do not reintroduce a per-file classification here. Two earlier attempts —
# one splitting by schema shape, one by which producer owns the file — were
# measured to REMOVE coverage that exists today (both would have exempted
# `tests/golden/fixtures/full-l0-l3/conf.d/db/mariadb/prod/_defaults.yaml` and
# `.../opt-out-null-threshold/conf.d/_defaults.yaml`, each of which makes this
# gate exit 1 today when injected). "Can express the defect" is a question about
# STRUCTURE, not about whether a file's content happens to look like thresholds.
_DEFAULTS_ARTIFACT_SUFFIXES = (".yaml", ".yml")

# ⛔ Explicit, and empty on purpose. A fixture that deliberately encodes the
# defective shape (to characterise the loader, say) belongs HERE with a reason —
# not hidden behind a blanket `tests/` exclusion, which is how a guard quietly
# stops covering the tree that grows fastest. Keys are repo-relative POSIX paths.
_DEFAULTS_ARTIFACT_EXEMPT: dict[str, str] = {}

# TWO non-vacuity floors, because there are two ways to end up with nothing to
# check and they need different remedies. `EMPTY-FACE` catches a face whose
# `defaults:` is empty; neither floor is about that.
#
#   SCAN floor  — the scan itself returned (almost) nothing: a broken reader, a
#                 moved tree, a pathspec that stopped matching. Remedy: repair
#                 the scan.
#   READ floor  — the scan is fine but the exemption table consumed it. Remedy:
#                 shorten the exemption list.
#
# ⛔ ONE floor is not enough, and this is a correction to the previous round.
# Moving the single floor to the scan side fixed a misleading message ("repair
# the scan or restore the file(s)" when the real cause was over-exemption) and
# in doing so REMOVED the only tripwire on the other cause: measured, with all
# 17 artifacts exempted the previous version raised nothing and read zero faces,
# and the test written alongside it asserted that silence as correct — the fix
# pinned the hole as a contract (blind review, round 5). A FIX can remove
# incidental safety; the repair is both floors, not a different single one.
#
# Measured 17 artifacts at the time of writing. Both floors sit below that so
# ordinary deletions do not trip them, and above 2 so a regression to the old
# hardcoded pair does.
_DEFAULTS_ARTIFACT_FLOOR = 10
_DEFAULTS_ARTIFACT_READ_FLOOR = 10

# ⛔ A THIRD floor, and the two above cannot do its job — they count FILES, and
# the failure they miss does not change the file count. Measured on this tree:
#
#   scenario                                        files  keys read
#   unmodified                                         17        <see below>
#   a shipped file's `defaults:` renamed away          17        drops
#   that same file re-nested under `defaults.x`        17        moves
#
# Rename the `defaults:` section of a shipped artifact and its face reads an
# empty set. Empty is LEGAL for an artifact (two recipe roots genuinely have no
# `defaults:`), so EMPTY-FACE exempts it — permanently — and from then on
# anything at all can be written into that file unseen. Measured before this
# floor existed: errors=0.
#
# So the non-vacuity signal has to be the number of KEYS actually inspected,
# which is the only quantity that moves when a reader silently stops reading.
#
# ⛔ PER CLASS, and the first draft of this floor got that wrong in exactly the
# way this file keeps getting it wrong: one global key floor of 100 against a
# measured total of 172 could not see a shipped artifact going to zero, because
# the generators contribute 102 of those keys and hold the number up on their
# own. That is the same "one class props up another" shape as the file floors
# above, one level down.
#
# Measured today — generators 102 (chart 8 / scaffold 42 / init 51 / onboard 1),
# artifacts 70 (exporter conf.d 19 / try-local 4 / recipes 0 / e2e-bench 13 /
# golden fixtures 34). Each floor sits below its class's value; what it would
# read if a whole group stopped yielding:
#
#   generators   chart 94 / scaffold 60 / init 51 / onboard 101
#   artifacts    exporter 51 / golden 36 / e2e-bench 57 / try-local 66 / recipes 70
#
# ⚠️ Stated honestly, BOTH ways — an earlier revision claimed each floor sits
# "above what that class would read if a whole group stopped yielding", which is
# false for two of the nine rows above and was caught by blind review doing the
# arithmetic:
#   * generator floor 80 does NOT catch chart (94) or onboard (101) going to
#     zero. Both are caught by EMPTY-FACE instead, which fires at exactly zero —
#     so what this floor uniquely watches is a producer that SHRANK without
#     emptying ("init dropped 40 of its 51 keys"), which nothing else sees.
#   * artifact floor 60 does NOT catch try-local (66) or the recipes roots (70,
#     they carry no keys). Those two are covered by `_SHIPPED_CONFD_ROOTS`.
# The two mechanisms are complements, not belt-and-braces; neither covers the
# whole grid, and pretending otherwise is how the next person stops checking.
#
# ⚠️ HEADROOM, measured, because these numbers need maintaining in BOTH
# directions and the cost is small but real: the paired test requires each floor
# to stay above "biggest group gone", so today artifacts tolerate +9 keys and
# generators +28 before a pure ADDITION turns that test red asking for a higher
# floor. Retiring a fixture asks for a lower one. Either way it is one constant
# and a line in the commit message — but nobody should discover the direction by
# guessing.
_DEFAULTS_GENERATOR_KEYS_FLOOR = 80
_DEFAULTS_ARTIFACT_KEYS_FLOOR = 60

# ⛔ And a FOURTH, on a different axis: the floors above are global, so one class
# can hold them up while another disappears entirely. Measured: removing every
# non-`tests/` artifact leaves 12 survivors, above both file floors, and
# `run_check` reports NOTHING (#1392) — the whole shipped class can evaporate in
# silence because fixtures outnumber it 12 to 5.
#
# Root -> (min artifacts, min keys, why this root ships). Hand-maintained ON
# PURPOSE: a floor derived from the scan would shrink along with the thing it
# guards, which is the failure the two file floors above were already written to
# avoid. The independence is the point, not an oversight.
#
# ⚠️ Both numbers are needed. `min keys` alone cannot speak for the recipes root
# (it legitimately carries zero); `min artifacts` alone is a floor of one, which
# survives deleting four of five files. Each pair sits below today's measured
# value and above the value that root would have if it lost a file.
#
# The set of roots is pinned against `.pre-commit-config.yaml` — the only place
# in the repo that states which conf.d trees are shipped, in those words — by
# `tests/lint/test_check_threshold_reachability.py`, so this table cannot drift
# from the confd-schema hooks' scope without a test going red.
_SHIPPED_CONFD_ROOTS: dict[str, tuple[int, int, str]] = {
    "components/threshold-exporter/config/conf.d": (
        2, 15,
        "the exporter's own conf.d: the dev/template tree every doc points at, "
        "plus its examples/ sibling. Both carry real threshold keys."),
    "try-local/seed/conf.d": (
        1, 3,
        "the one-command demo stack a reader runs first; its _defaults.yaml is "
        "the first platform-defaults file most people ever see."),
    "rule-packs/recipes/examples/conf.d": (
        2, 0,
        "the custom-alert recipe examples users copy from. min keys is 0 because "
        "both roots legitimately declare only other sections — the artifact "
        "count is what guards them."),
}

# Display prefix for artifact labels. ⛔ DISPLAY ONLY — it carries no meaning to
# any check, and nothing may start classifying by it again (#1393).
#
# It used to BE the discriminator: `face.startswith("artifact (")` decided
# whether the EMPTY-FACE rule applied, so a generator whose label happened to
# start with those characters was silently filed as an artifact and its broken
# reader went unreported (measured: EMPTY-FACE False, errors 0, and both
# guarding assertions still PASS).
#
# ⛔ The repair is NOT a `kind` field. Blind review measured that too: a
# generator declared `kind=ARTIFACT` reproduces the original hole character for
# character, because a hand-written field is exactly as writable as a
# hand-written label. Anything DECLARED can be declared wrong. So the two
# classes are now built by two different code paths and returned separately —
# `_defaults_faces()` returns (generators, artifacts) — and membership is a
# consequence of WHICH LOOP BUILT YOU, which nobody can mistype.
_ARTIFACT_FACE_PREFIX = "artifact ("


def _is_defaults_artifact(name: str) -> bool:
    """Case-insensitive like the loader, but a PREFIX where the loader is exact.

    ⛔ Two halves with two different justifications, and conflating them invites
    the wrong "cleanup":
      * case-insensitivity MIRRORS `config_hierarchy.go`, which lowercases before
        both its suffix and its filename test;
      * the `_defaults` PREFIX is deliberately WIDER than the loader, whose test
        is `lower == "_defaults.yaml" || lower == "_defaults.yml"` (exact). The
        width is load-bearing — `examples/_defaults-multidb.yaml` is one of the
        three files blind review used to walk past the hardcoded-path version, and
        narrowing this to the loader's equality would drop it again.
    """
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

    # ⛔ NO pathspec. `git ls-files -- "*_defaults*"` is case-SENSITIVE — measured
    # under both `core.ignorecase` values, `*_DEFAULTS*` returns 0 — so pairing it
    # with a case-insensitive predicate delivered half a fix: `_defaults.YAML`
    # got through (stem matches) but `_Defaults.yaml` and `_DEFAULTS.yml` never
    # reached the predicate at all. Two predicates with the narrower one FIRST is
    # the shape; the guard's own case-insensitivity test could not see it because
    # it tests the predicate, and the hook's `(?i:)` filter could not see it
    # because it tests the trigger. The scan had neither (blind review, round 5,
    # two reviewers independently).
    #
    # Listing the whole index and filtering in Python makes `_is_defaults_artifact`
    # the ONLY predicate. Measured cost: ~2.3k paths (2294 today), 0.13s for the git call,
    # 0.5ms for the filter — the pathspec was buying nothing.
    out = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "ls-files", "-z"],
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


# Depth cap for the `defaults:` walk below. Generous — the deepest real nesting
# in the tree is 2 — because its job is not to model a schema but to make a
# runaway walk LOUD. `yaml.safe_load` happily builds a self-referential document
# from a recursive anchor (`defaults: &a {x: *a}`, measured: the walk recurses
# forever without this), and a silent truncation there would hand back a partial
# key set that passes the placement check for the half it never reached.
_DEFAULTS_MAX_DEPTH = 16


class KeyInfo(int):
    """A key's depth, PLUS the two structural facts the walk already knows.

    ⛔ Subclasses `int` on purpose: every consumer that only wants the depth
    (`info == 0`, `info + 1`, `len(face)`, `sum(...)`) keeps working unchanged,
    so adding structure here cannot quietly alter the floors or the counts.
    Only the two call sites that were re-deriving structure from the rendered
    path read `.leaf` / `.has_children`.

    Both fields exist because blind review measured a defect for each of them,
    and both defects were the SAME mistake this module's own walk docstring
    warns about — guessing structure from a dot in the rendered string:

    - `.leaf`: `k.rsplit(".", 1)[-1]` splits on the last dot ANYWHERE, so a
      dimensional key whose label value contains a dot
      (`oracle_tablespace{tablespace=~"SYS.*"}` — the mainstream shape in this
      repo, cf. `collector_test.go:567`) yielded `*"}` and the whole class went
      undetected. Base `2e61b20a` tested `"{" in k` and caught them, so the
      dotted paths introduced here had regressed coverage.
    - `.has_children`: `depth == 0` answers "is it top level", NOT "is it a
      leaf". `defaults: {mysql_connections_critical: {a: 1}}` is top level AND
      a mapping, so it was reported with the flat consequence ("emits a
      severity=warning series") when the truth is that `Defaults
      map[string]float64` fails to unmarshal and NOTHING is emitted.
    """

    leaf: str
    has_children: bool

    def __new__(cls, depth: int, leaf: str, has_children: bool) -> "KeyInfo":
        self = super().__new__(cls, depth)
        self.leaf = leaf
        self.has_children = has_children
        return self


def _walk_defaults_keys(node: object, prefix: str = "",
                        depth: int = 0) -> dict[str, KeyInfo]:
    """{dotted path: nesting depth} for every mapping key ANYWHERE under `defaults:`.

    ⛔ Recursive, and that is the whole point (#1392). Reading only the top level
    measured 7 of 17 tracked artifacts as able to express the defect this gate
    checks for — the rest keep their keys one level down and an injection there
    was silent (measured both ways: top level caught, nested not).

    Dotted paths rather than bare leaf names so the error message can say WHICH
    level to repair; `endswith(_CRITICAL_SUFFIX)` and the `{` test are unaffected
    by the prefix, and on a flat section the path IS the key (measured: the chart
    and `init` faces return byte-identical key sets before and after #1392).

    INTERMEDIATE keys are collected too, not just leaves: a misplaced
    `mysql_connections_critical:` whose value happens to be a mapping is the same
    defect wearing a different shape, and a leaves-only walk would step over it.

    ⛔ The DEPTH is returned, not re-derived downstream from a dot in the path.
    An earlier revision returned bare paths and `_report_placement` tested
    `"." in key` to decide whether a key was nested — blind review measured the
    misdiagnosis: a flat `defaults: {"a.b_critical": 90}` (a legal YAML key that
    contains a dot) was reported with the nested consequence, which is the wrong
    failure mode and sends the reader after a metric that will not exist. The
    walk already knows the depth; throwing it away and guessing from the rendered
    string is the shape where an exception becomes structurally impossible to get
    right.
    """
    keys: dict[str, KeyInfo] = {}
    if depth > _DEFAULTS_MAX_DEPTH:
        raise RuntimeError(
            f"`defaults:` walk exceeded {_DEFAULTS_MAX_DEPTH} levels at {prefix!r}. "
            "A recursive YAML anchor builds a self-referential document that "
            "`yaml.safe_load` accepts, and truncating the walk would silently "
            "stop checking whatever lies below. Flatten the section or fix the "
            "anchor; do not raise the cap to paper over a cycle.")
    if isinstance(node, dict):
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            sub = _walk_defaults_keys(v, path, depth + 1)
            # `str(k)` is the key AS WRITTEN — never re-split out of `path`.
            keys[path] = KeyInfo(depth, str(k), bool(sub))
            keys.update(sub)
    elif isinstance(node, list):
        for item in node:
            # List ITEMS carry no key of their own; a mapping inside one does.
            keys.update(_walk_defaults_keys(item, prefix, depth + 1))
    return keys


def _defaults_section(text: str, *, unwrap_chart: bool = False) -> dict[str, KeyInfo]:
    """{key path: depth} under `defaults:` of a `_defaults.yaml` / chart values.

    ⛔ `unwrap_chart` is OFF by default and only the chart face turns it on. The
    unwrap exists because `helm/threshold-exporter/values.yaml` nests everything
    one level down under `thresholdConfig:`; applying it to conf.d artifacts as
    well was measured to be a hole (blind review): `root = doc.get(...) or doc`
    means ANY `_defaults.yaml` that grows a top-level `thresholdConfig:` key
    stops having its real `defaults:` read at all — injected `_critical` and
    everything else silently vanished, rc=0. A conf.d file has no
    `thresholdConfig:` in its schema, so for those the top level IS the root.
    """
    import yaml  # local import: only the artifact faces need it

    doc = yaml.safe_load(text) or {}
    root = (doc.get("thresholdConfig") or doc) if unwrap_chart else doc
    return _walk_defaults_keys(root.get("defaults") or {})


def _defaults_faces() -> tuple[dict[str, dict[str, KeyInfo]], dict[str, dict[str, KeyInfo]]]:
    """(generators, artifacts) — {face label: `defaults:` key paths} for each class.

    TWO CLASSES, and the split is not cosmetic: the four GENERATORS below are
    enumerated (they have no artifact on disk — a `da-tools init` customer's
    `_defaults.yaml` exists only in their repo, so the generator IS the surface),
    while the ARTIFACTS are DERIVED (`_defaults_artifacts`, and see the note at
    the top of this section for why enumerating those was wrong).

    ⛔ TWO RETURN VALUES, not one dict with a `kind` on each entry (#1393). The
    classes have OPPOSITE empty-set semantics — a generator that reads empty is
    a broken reader, an artifact that reads empty is a real and legal state — so
    something has to tell them apart, and every DECLARED discriminator can be
    declared wrong. That was measured twice: the old label-prefix test filed a
    generator as an artifact whenever its label happened to start with
    `artifact (`, and a `kind=ARTIFACT` field reproduces the same hole exactly,
    because a hand-written field is as writable as a hand-written label. Both
    times the two guarding assertions still passed. Returning the classes
    separately makes membership a consequence of which loop built you.

    ⛔ "Five of these six were already correct when the other was not" stood
    here and was wrong twice over: the face list has not been six since the
    artifact half became derived, AND two of the original six were defective,
    not one — `init` shipped 16 misplaced keys and the `onboard` probe shipped
    1 (`4fd49561:scripts/tools/ops/onboard_platform.py:607` wrote
    `f"{key}_critical"` straight into `defaults:`, with a test asserting it).
    Understating it to one weakened the very argument it was making for
    per-face checking, and a reader who counted six faces would have gone
    looking for the two this round deliberately stopped hardcoding.

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

    generators = {
        # GENERATORS — no artifact on disk to read; the producer IS the surface.
        # ⛔ Only this one unwraps `thresholdConfig:` — see `_defaults_section`.
        "chart (helm/threshold-exporter/values.yaml)":
            _defaults_section(_CHART_VALUES.read_text(encoding="utf-8"),
                              unwrap_chart=True),
        # ⛔ These two hand back a live mapping rather than YAML text, so they
        # used to be flattened with `{k: 0 for k in ...}` — assuming depth 0 by
        # construction. Blind review measured the cost: `main()` prints
        # "inspected at every depth of `defaults:`" for EVERY face, and that
        # sentence was false for two of the four generators. It is only true
        # today because every pack's default happens to be a scalar; the day one
        # becomes a mapping, the claim keeps printing and the walk has still
        # never run. Walk the mapping directly — same walk, same KeyInfo, no
        # round-trip through YAML text.
        "onboarding/scaffold (scaffold_tenant.generate_defaults)":
            _walk_defaults_keys(
                scaffold_tenant.generate_defaults(scaffold_packs)["defaults"]),
        "onboarding/init (init_project._gen_defaults_yaml)":
            _defaults_section(
                init_project._gen_defaults_yaml(init_packs, "monitoring")),
        "migration/onboard (onboard_platform.generate_defaults_from_candidates, "
        "probed)": _walk_defaults_keys(onboard_suggestion.get("defaults") or {}),
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
        raise _GateViolation(
            f"defaults-artifact scan found only {len(scanned)} tracked file(s), "
            f"below the floor of {_DEFAULTS_ARTIFACT_FLOOR}. A scan that returns "
            "(almost) nothing passes the placement check vacuously, which is the "
            "shape the hardcoded-path version of this face shipped with. Repair "
            "the scan or restore the file(s); do not lower the floor. (Exemptions "
            f"are NOT counted here — {len(_DEFAULTS_ARTIFACT_EXEMPT)} exempted.)")
    to_read = _defaults_artifacts()
    if len(to_read) < _DEFAULTS_ARTIFACT_READ_FLOOR:
        raise _GateViolation(
            f"the scan found {len(scanned)} tracked file(s) but only {len(to_read)} "
            f"survive the exemption table, below the read floor of "
            f"{_DEFAULTS_ARTIFACT_READ_FLOOR}. The scan is FINE — "
            f"{len(_DEFAULTS_ARTIFACT_EXEMPT)} exemption(s) consumed it, and zero "
            "faces pass the placement check below perfectly. SHORTEN THE EXEMPTION "
            "LIST; repairing the scan is not the remedy here, and neither is "
            "lowering the floor.")
    artifacts: dict[str, dict[str, KeyInfo]] = {}
    by_root: dict[str, list[tuple[str, dict[str, KeyInfo]]]] = {
        r: [] for r in _SHIPPED_CONFD_ROOTS}
    for path in to_read:
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        # ⛔ The reader names the file. `read_text` raises with the path attached,
        # but `yaml.safe_load` does not — its error says `<unicode string>`, and
        # with the scope derived rather than 2 hardcoded paths, "which file" is
        # precisely the information that was missing (blind review, round 4).
        try:
            keys = _defaults_section(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — re-raised with provenance
            raise RuntimeError(f"{rel}: {exc}") from exc
        artifacts[f"{_ARTIFACT_FACE_PREFIX}{rel})"] = keys
        for root in _SHIPPED_CONFD_ROOTS:
            if rel == root or rel.startswith(root + "/"):
                by_root[root].append((rel, keys))
                break

    _assert_shipped_roots_intact(by_root)
    _assert_keys_floor(generators, artifacts)
    return generators, artifacts


def _assert_shipped_roots_intact(
        by_root: dict[str, list[tuple[str, dict[str, KeyInfo]]]]) -> None:
    """Every shipped conf.d root must still contribute what it contributes today.

    Per ROOT, because the global floors are a single number that one class can
    hold up for another: measured, deleting every non-`tests/` artifact leaves 12
    fixture survivors, clears both file floors, and reports nothing.
    """
    for root, (min_artifacts, min_keys, why) in sorted(_SHIPPED_CONFD_ROOTS.items()):
        found = by_root[root]
        n_keys = sum(len(keys) for _rel, keys in found)
        if len(found) >= min_artifacts and n_keys >= min_keys:
            continue
        # ⛔ Cause order matters, and the previous wording had it backwards.
        # "Restore the file(s)" led, so a maintainer whose files were all still
        # present read past it to the second clause and did what it said: edited
        # the floor. Measured (blind review): typo `defaults:` in a shipped file
        # → this fires → change `1, 3` to `1, 0` → green → then a `_critical`
        # injected into that same file is silent, and the suite is 68 passed.
        # The renamed-section case is FIRST now, and the "adjust the table"
        # branch is explicitly not for making this message go away.
        raise _GateViolation(
            f"shipped conf.d root {root!r} now contributes {len(found)} "
            f"artifact(s) / {n_keys} key(s), below its floor of {min_artifacts} "
            f"artifact(s) / {min_keys} key(s). This root ships: {why} Its "
            "artifacts are what make this gate a statement about what customers "
            "receive rather than about what the test fixtures happen to contain "
            "— fixtures outnumber them and will hold every global floor up on "
            "their own.\n"
            "  MOST LIKELY: a `defaults:` section in this tree was renamed, "
            "re-nested, or emptied — the file is still there, so the file counts "
            "above did not move. Check the section name first.\n"
            "  OR: the file(s) were deleted or moved — restore them.\n"
            "  ⛔ LOWERING THESE NUMBERS IS NOT A REMEDY. A shipped root whose "
            "keys went to zero is precisely what this floor is for; editing "
            "_SHIPPED_CONFD_ROOTS to match the new reality re-creates the blind "
            "spot. Only change the table when a tree genuinely moved or stopped "
            "shipping, and then change the confd-schema hooks in "
            ".pre-commit-config.yaml in the same commit (a test pins the ROOT "
            "SET to those hooks — it does NOT police these two numbers, so they "
            "are on you).")


def _assert_keys_floor(generators: dict[str, dict[str, KeyInfo]],
                       artifacts: dict[str, dict[str, KeyInfo]]) -> None:
    """The only non-vacuity signal that moves when a reader silently stops reading.

    One floor per class, because a single combined floor lets the bigger class
    hold the number up for the smaller one (measured: generators are 102 of the
    172 keys).
    """
    # ⛔ Per-class WORDING as well as per-class numbers. The two classes had one
    # shared message and blind review measured two sentences in it that are false
    # on the generator side: an empty generator face does NOT "pass the placement
    # check" (EMPTY-FACE catches it), and the exemption table cannot affect a
    # generator count at all — it sent the reader to a table unrelated to their
    # problem.
    n_gen = sum(len(v) for v in generators.values())
    if n_gen < _DEFAULTS_GENERATOR_KEYS_FLOOR:
        raise _GateViolation(
            f"the defaults-tier GENERATOR faces yielded {n_gen} key(s) across "
            f"{len(generators)} face(s), below the floor of "
            f"{_DEFAULTS_GENERATOR_KEYS_FLOOR}. A generator that emits NOTHING is "
            "already caught by EMPTY-FACE; this floor is for the other shape — a "
            "producer that SHRANK without emptying (a pack catalog trimmed, a "
            "tier split, a render path that started skipping keys), which no "
            "other check can see. Repair the producer. If a producer legitimately "
            "got smaller, re-measure and update the floor in the same commit, "
            "saying what shrank and why.")

    n_art = sum(len(v) for v in artifacts.values())
    if n_art < _DEFAULTS_ARTIFACT_KEYS_FLOOR:
        # ⛔ Do not assert what did or did not change. The previous wording said
        # "The face COUNT is therefore not what changed", and blind review hit it
        # by retiring three golden fixtures — where the face count is exactly
        # what changed. Print both numbers and let the reader see it.
        raise _GateViolation(
            f"the defaults-tier ARTIFACT faces yielded {n_art} key(s) across "
            f"{len(artifacts)} face(s), below the floor of "
            f"{_DEFAULTS_ARTIFACT_KEYS_FLOOR} "
            f"({len(_DEFAULTS_ARTIFACT_EXEMPT)} exempted, "
            f"{sum(1 for v in artifacts.values() if not v)} face(s) empty).\n"
            "  If the face count is UNCHANGED: a `defaults:` section was renamed "
            "or re-nested and that file is now a blind spot — empty is legal for "
            "an artifact, so EMPTY-FACE will not say so. Repair the reader or the "
            "section, and do not lower the floor to match.\n"
            "  If the face count DROPPED because artifacts were legitimately "
            "removed (retiring fixtures is ordinary maintenance): re-measure and "
            "lower the floor in the SAME commit, naming what was removed. This "
            "floor is hand-maintained against a measured value; it is not a "
            "claim that the number can only grow.\n"
            "  If you just added an exemption and that exemption is genuinely "
            "right (a byte-hashed golden fixture you must not edit): shorten "
            "_DEFAULTS_ARTIFACT_EXEMPT if you can, and if you cannot, lower this "
            "floor in the SAME commit naming the exempted file. ⛔ There has to "
            "be an exit here — an earlier wording forbade editing the file, "
            "forbade lowering the floor, and pointed at the exemption table as "
            "the cause, which left a >10-key hashed fixture with no legal move "
            "at all.\n"
            "  ⚠️ ADDING artifacts pushes this floor UP, not down: it is a "
            "hand-maintained lower bound, not a tracker. A pure addition can "
            "make the paired test go red asking for a higher number — that is "
            "the same maintenance, in the other direction.")


def _report_placement(face: str, keys: dict[str, KeyInfo], errors: list[str]) -> None:
    """Both halves of the defaults-tier placement rule, for ONE face.

    ⛔ One function, called from both class loops. The two checks used to sit in
    a single loop body precisely so "a new face can never pick up one check and
    miss the other"; splitting generators from artifacts (#1393) would have
    quietly retired that guarantee, so it moves here instead — there is exactly
    one place that knows what "checked" means.
    """
    for k in sorted(k for k in keys if k.endswith(_CRITICAL_SUFFIX)):
        base = k[: -len(_CRITICAL_SUFFIX)]
        leaf = keys[k].leaf
        if keys[k] == 0 and keys[k].has_children:
            # Top level, but the VALUE is a mapping. ⛔ Neither of the other two
            # branches is true here and blind review measured both misreadings:
            # `depth == 0` alone answers "is it top level", not "is it a leaf",
            # so this landed in the flat branch and promised a
            # severity="warning" series. `Defaults` is `map[string]float64`
            # (types.go:208), so the value fails to unmarshal and the whole file
            # is rejected — nothing is emitted at all, warning tier included.
            # The nested wording is wrong too: the key name IS at the top level,
            # so "no key of that name exists" would be false.
            errors.append(
                f"CRITICAL-IN-DEFAULTS: {face} puts {k!r} at the top level of "
                "`defaults:` but gives it a MAPPING value. `Defaults` is "
                "`map[string]float64`, so this does not decode: the whole file "
                "is rejected and NO series is emitted — not the critical one, "
                "and not the warning-tier fallback the flat case would have "
                "produced. Two independent repairs are needed: give this key a "
                f"scalar value, and move the critical tier to a `<tenant>.yaml` "
                f"override keyed {leaf!r} with "
                f"{leaf[: -len(_CRITICAL_SUFFIX)]!r} present in the flat "
                "`defaults:` map (resolveCriticalRows skips any override key "
                "without the `_critical` suffix, then skips again if the base "
                "is absent). (#1218 / TRK-344)"
            )
            continue
        if keys[k] == 0:
            # Flat: the key IS a `Defaults` key, and the failure is the silent
            # one this gate was built for.
            # ⛔ Describe the SHAPE; do not spell the labels out. `parseMetricKey`
            # splits on the FIRST underscore, so the emitted series is
            # {component=<prefix>, metric=<rest incl. _critical>} — writing
            # `metric="<whole key>"` here (as this message did) sends the reader
            # to a series that does not exist. It is also the exact shape of
            # #731, whose fix ships with an anti-echo-chamber guard
            # (`app/rulepack_contract_test.go`) warning that a PYTHON re-impl of
            # that split would just be a new echo chamber — so this does not
            # re-derive the labels either.
            errors.append(
                f"CRITICAL-IN-DEFAULTS: {face} puts {k!r} under `defaults:`, "
                "which is not the critical tier and never becomes one: "
                "resolveCriticalRows iterates TENANT OVERRIDES, so this key is "
                "emitted by resolveBaseRows instead — as a "
                'severity="warning" `user_threshold` series whose labels come '
                "from parseMetricKey splitting on the FIRST underscore, so the "
                "`_critical` text lands inside the component/metric labels "
                "instead of becoming a severity (`cpu_critical` → "
                '{component="cpu",metric="critical"}; a key with no underscore → '
                'component="default"). See app/rulepack_contract_test.go for that '
                f"contract. No recording rule joins it, tenant:alert_threshold:{k} "
                "stays empty, and nothing says so. The *Critical alert cannot "
                "fire. Move it to the tenant side (a `<tenant>.yaml` override), "
                f"keeping {base!r} under `defaults:` so the critical tier is "
                "admitted. (#1218 / TRK-344)"
            )
            continue
        # Nested. ⛔ Do NOT describe the nesting's own consequence as if this key
        # caused it, and do NOT offer "keep the base under `defaults:`" — blind
        # review measured both mistakes on the previous wording: a maintainer who
        # moved only the `_critical` half left the nesting in place and this gate
        # went GREEN on a file that still does not decode. Two separate problems,
        # said separately, and the repair for the one being reported is stated
        # without promising it fixes the other.
        errors.append(
            f"CRITICAL-IN-DEFAULTS: {face} puts {k!r} under `defaults:` at depth "
            f"{keys[k] + 1}, and `Defaults` is a FLAT map — no key of that name "
            "exists at any tier, critical or otherwise, so the *Critical alert "
            "cannot fire. ⛔ The nesting is a SECOND, independent problem: "
            "removing this key does not make the section decodable, and this "
            "gate deliberately does not police nesting on its own (a flat-loader "
            "file raises da_config_parse_failure_total at runtime; a "
            "hierarchy-merge fixture does not go through that path at all). Fix "
            "the nesting for the file's own reasons, and put the critical tier "
            "where it is read — a `<tenant>.yaml` override keyed "
            f"{leaf!r} (the LAST path segment, unnested; the dotted path above "
            "is this tool's rendering of where the key sits, not a key name), "
            f"AND {leaf[: -len(_CRITICAL_SUFFIX)]!r} present in the FLAT "
            "`defaults:` map. ⛔ Both halves are load-bearing: "
            "resolveCriticalRows skips any override key that does not end in "
            "`_critical`, and then skips it again if the base key is absent "
            "from defaults — so dropping the suffix, or leaving the base inside "
            "the nesting, each produce no critical row at all. If "
            "this file's BYTES are an input to another gate (a golden parity "
            "fixture hashes them), do not edit it to satisfy this one: register "
            "it in _DEFAULTS_ARTIFACT_EXEMPT with a reason. (#1218 / TRK-344)"
        )
    # The other half of the same rule (docs_defaults_sample_test.go pins both;
    # is_shipped_optional_key refuses both).
    #
    # ⛔ Two things this loop got wrong until blind review, BOTH of them the
    # `_critical` half's bugs repeated verbatim one loop down — "fixed the
    # instance, not the class":
    #   1. `"{" in k` tested the whole dotted PATH, so every child of a
    #      dimensional key was reported too. Measured on
    #      `defaults: {'redis_q{queue="a"}': {inner: 1, other_critical: 2}}` —
    #      3 errors, two of them naming paths that are not keys anywhere.
    #      Only the key ITSELF can carry the label selector. ⛔ The first fix
    #      for this asked `"{" in k.rsplit(".", 1)[-1]`, which split on the last
    #      dot ANYWHERE and so lost every dimensional key whose label VALUE
    #      contains a dot (`{tablespace=~"SYS.*"}`, IPs, versions) — a coverage
    #      regression against base, which caught them with `"{" in k`. The walk
    #      knows the leaf; ask it.
    #   2. The consequence was stated as if the key were flat. Nested, the file
    #      does not decode at all and `resolveDimensionalRows` never enters the
    #      picture.
    for k in sorted(k for k in keys if "{" in keys[k].leaf):
        if keys[k] == 0 and keys[k].has_children:
            # Same third case as the `_critical` loop above, and it had the same
            # bug: top level with a mapping value is neither flat nor nested.
            # ⛔ A test asserted the flat wording for exactly this input, so the
            # defect had a guard certifying it.
            errors.append(
                f"DIMENSIONAL-IN-DEFAULTS: {face} puts {k!r} at the top level of "
                "`defaults:` with a MAPPING value. `Defaults` is "
                "`map[string]float64`, so the file does not decode and NOTHING "
                "is emitted — resolveDimensionalRows is never reached, so its "
                "tenant-only rule is not why this fails. Two independent "
                "repairs: give the key a scalar value, and move dimensional "
                "thresholds to a `<tenant>.yaml` override (they have no default "
                "path at any tier). (#1218 / TRK-344)"
            )
            continue
        if keys[k] == 0:
            errors.append(
                f"DIMENSIONAL-IN-DEFAULTS: {face} puts {k!r} under `defaults:`, "
                "which gives dimensional thresholds no default path: "
                "resolveDimensionalRows is tenant-only and never consults the "
                "defaults map, while parseMetricKey bakes the label segment into "
                "the metric NAME. ValidateTenantKeys reports nothing. Move it to "
                "a `<tenant>.yaml` override. (#1218 / TRK-344)"
            )
        else:
            errors.append(
                f"DIMENSIONAL-IN-DEFAULTS: {face} puts {k!r} under `defaults:` at "
                f"depth {keys[k] + 1}. `Defaults` is a FLAT map, so this path is "
                "not a key at any tier, and dimensional thresholds have no "
                "default path in the first place (resolveDimensionalRows is "
                "tenant-only). ⛔ The nesting is a SECOND, independent problem — "
                "removing this key does not make the section decodable. Move the "
                "threshold to a `<tenant>.yaml` override and fix the nesting for "
                "the file's own reasons. (#1218 / TRK-344)"
            )


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
    defaults_faces: tuple[dict[str, dict[str, KeyInfo]], dict[str, dict[str, KeyInfo]]] | None = None,
) -> dict[str, object]:
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
        # ledger to make inconsistent — and defaulting it to empty would mean the
        # only test that exercises it is one written specifically for it. Tests
        # that need it silent pass `defaults_faces=({}, {})` explicitly.
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
    # Per FACE, not one global count. Measured on the pre-fix tree: of the six
    # faces that existed then, TWO were defective — `init` shipped 16 misplaced
    # keys and the `onboard` probe shipped 1 — so a single "no _critical
    # anywhere" assertion would have been satisfied by neither, but a gate that
    # read any ONE face could have been green while both shipped. (An earlier
    # version of this comment said "four clean faces while the fifth ships
    # sixteen", which was true of four of them and understated the evidence for
    # the design it justifies.)
    generator_faces, artifact_faces = defaults_faces

    # ⛔ The vacuity rule applies to GENERATORS only, and the asymmetry is
    # measured, not assumed: every generator ships a non-empty `defaults:`, so an
    # empty one means its reader broke — but 2 of the derived ARTIFACTS
    # legitimately carry none (the recipes example root
    # `rule-packs/recipes/examples/conf.d/_defaults.yaml` and its `finance/`
    # child declare only the other sections). Applying the generator rule to
    # artifacts turned this gate red on arrival for two correct files.
    #
    # Vacuity is still closed for artifacts, by three links: a file that cannot
    # be read RAISES (fail-closed, `main()` → EXIT_CALLER_ERROR with the path), a
    # walk that returns (almost) nothing trips `_DEFAULTS_ARTIFACT_FLOOR`, and a
    # face that silently stops YIELDING trips the per-class key floors — the third
    # is the one the file floors could never see, since renaming a `defaults:`
    # section leaves the file count untouched. What is left — "parsed fine,
    # genuinely has no `defaults:`" — is a real state of a real file.
    for face, keys in sorted(generator_faces.items()):
        if not keys:
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
        _report_placement(face, keys, errors)
    for face, keys in sorted(artifact_faces.items()):
        _report_placement(face, keys, errors)

    # ⛔ The coverage figure is COMPUTED and printed, never written into a
    # comment. The comment that used to carry it was corrected once and went
    # stale twice; a number nobody re-measures is a number that will be wrong.
    #
    # It is a third result key rather than an `info`, because `infos` is asserted
    # empty by tests about unrelated branches — appending there made a coverage
    # line into a false failure for them, which is how a reporting change turns
    # into pressure to weaken someone else's assertion. (`check_scrape_
    # reachability.run_check` already returns a non-error key the same way.)
    stats = {
        "generator_faces": len(generator_faces),
        "artifact_faces": len(artifact_faces),
        "generator_keys": sum(len(v) for v in generator_faces.values()),
        "artifact_keys": sum(len(v) for v in artifact_faces.values()),
        "artifacts_without_defaults": sum(1 for v in artifact_faces.values() if not v),
    }

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

    return {"errors": errors, "infos": infos, "stats": stats}


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
    except _GateViolation as exc:
        # ⛔ A floor breach is USER-actionable (see `_GateViolation`). Reporting
        # it as "crashed" with rc=2 told the reader their environment was broken,
        # and "the lint crashed" is read as flaky-skip-it — the one reaction
        # these floors exist to prevent.
        print(f"❌ {exc}", file=sys.stderr)
        # ⛔ State the FULL blast radius. An earlier wording said only "the
        # placement checks did NOT run", and blind review measured what that
        # understates: the floors raise out of `_defaults_faces()`, which
        # `run_check` calls while resolving its inputs — before a single error is
        # appended. Measured with a stale ledger entry present: floors intact →
        # 59 lines of stderr including STALE-EXEMPTION; one floor broken → 6
        # lines and no STALE-EXEMPTION, no known-unwired INFO, no
        # NOT-CHART-ARMED. Nothing in this module ran, including its headline
        # TRK-337 reachability check and both exit-locked ledgers.
        print(
            "\n1 defaults-tier floor breach — TRK-344 / #1392. ⛔ NOTHING ELSE "
            "IN THIS MODULE RAN: not the TRK-337 reachability check, not the "
            "KNOWN_UNWIRED or NOT_CHART_ARMED exit-locks, not the placement "
            "checks, and no INFO. The floors are evaluated while the faces are "
            "built — before any check — precisely because an empty face passes "
            "every one of them vacuously. Fix this and re-run to see the rest.",
            file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK
    except Exception as exc:  # noqa: BLE001 — caller error, not a violation
        print(f"ERROR: reachability check crashed: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    errors = result["errors"]
    infos = result["infos"]
    stats = result["stats"]

    # Printed on EVERY run, pass or fail — the defaults-tier coverage figure has
    # no home in a comment (it went stale twice there), and a reader who wants to
    # know how much this gate actually looks at should get today's number.
    print(
        "INFO: defaults-tier coverage: "
        f"{stats['generator_faces']} generator face(s) / "
        f"{stats['generator_keys']} key(s), "
        f"{stats['artifact_faces']} artifact face(s) / "
        f"{stats['artifact_keys']} key(s), inspected at every depth of "
        f"`defaults:`; {stats['artifacts_without_defaults']} artifact(s) declare "
        "no `defaults:` section at all.", file=sys.stderr)

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
