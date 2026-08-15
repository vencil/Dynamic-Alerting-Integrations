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
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_OPS = PROJECT_ROOT / "scripts" / "tools" / "ops"
sys.path.insert(0, str(_OPS))
import _observed_map_lib as observed_map_lib  # noqa: E402
import scaffold_tenant  # noqa: E402
# The `<base>_critical` suffix, from the module that already owns it for the
# generators (`defaults_critical_keys` / `is_shipped_optional_key`). Spelling it
# again here would be one more copy of the very contract this gate polices.
from _registry_lib import CRITICAL_SUFFIX as _CRITICAL_SUFFIX  # noqa: E402

# ⛔ The conf.d root of a path comes from the module that already defines what a
# conf.d root IS (nearest `conf.d` ancestor, inner-most wins for nested trees).
# Re-deriving it here — "split on conf.d", "take parents[1]" — is the shape this
# gate has already been bitten by twice (#1392: a dotted path re-split downstream
# lost a whole class of keys). ⚠️ This import lives outside `scripts/tools/`, so
# the hook `files:` filter and the test that pins it both had to learn about it;
# that the pin needed a manual update is #1413.
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "ops"))
from guard_defaults_scopes import CONF_D, conf_d_root  # noqa: E402

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
# "above what that class would read if a whole group stopped yielding", and that
# claim FAILS on FOUR of the nine rows above: TWO under each of the two floors.
# The four are named below rather than counted, because this line said "two of
# the nine rows" while inviting the reader to redo the arithmetic — and doing it
# yields four, so the sentence refuted itself. "Two" was the PER-FLOOR count
# wearing the whole grid's denominator.
#
# ⛔ Which grouping produced these nine rows matters as much as the count, and
# it is not the grouping any other figure in this file uses: generators are one
# row per PRODUCER FACE (4), artifacts one row per DIRECTORY GROUP (5 — the
# seven golden-fixture roots collapse into one row, the two e2e-bench roots into
# another). It is NOT the per-conf.d-root grouping used elsewhere in this module.
# ⚠️ "Elsewhere" is ONE other grouping, not two: the bracket test's
# `_artifact_groups` (an rsplit on `/conf.d`) and `conf_d_root` (the fifth
# floor's, and this module's, definition of a root) produce the IDENTICAL
# partition — all 17 tracked artifacts agree. An earlier wording named them
# separately ("neither … nor …"), which reads as three groupings in play when
# there are two. Two independent spellings of one partition is still worth
# noting, though: if they ever diverge, the bracket test and the fifth floor
# would be bracketing different things while appearing to agree.
# ⛔ That agreement was "measured" and left at that, which made it the same
# shape as every other number this file has had to correct. It is now COMPUTED
# on every run of the suite by
# `test_the_two_artifact_groupings_are_one_partition`, which is cheap (two
# dicts over 17 paths) and legitimate — it compares two live derivations against
# each other, not a derivation against a digit copied out of prose. Divergence
# is a thing this file wants to hear about anyway, so a red there is signal.
# Do not carry "N of 9" into a sentence beside those; #1411's issue body already
# put this row count next to two figures from different (scenario × grouping ×
# floor subset) worlds and read them as comparable.
#
#   * generator floor 80 does NOT catch chart (94) or onboard (101) going to
#     zero — 2 of its 4 rows. Both are caught by EMPTY-FACE instead, which fires
#     at exactly zero — so what this floor uniquely watches is a producer that
#     SHRANK without emptying ("init dropped 40 of its 51 keys"), which nothing
#     else sees.
#   * artifact floor 60 does NOT catch try-local (66) or the recipes roots (70,
#     they carry no keys) — 2 of its 5 rows. Those two are covered by
#     `_SHIPPED_CONFD_ROOTS`.
# The two mechanisms are complements, not belt-and-braces; neither covers the
# whole grid, and pretending otherwise is how the next person stops checking.
#
# ⛔ AND THE ARTIFACT BULLET STOPPED ONE SENTENCE SHORT OF ITS SIBLING (#1434).
# The generator bullet says what its floor UNIQUELY watches; the artifact one
# only said who covers the rows it misses, so the reader was left to assume the
# other three rows are its own. They are not — and this is structural rather
# than a fact about today's numbers. `_assert_shipped_roots_intact` and
# `_assert_every_root_contributes` both run BEFORE `_assert_keys_floor` (see the
# call order at the end of `_defaults_faces`), and the fifth floor fires on ANY
# pinned root reaching zero. So for the artifact half, "a whole conf.d root
# stopped yielding" is reported by one of those two, always, and this floor
# never gets to speak. What it does own is the shape neither of them can see:
# erosion that never takes any single root to zero. It speaks once more than
# `total - floor` keys have gone, wherever they went — which is why the bracket
# below is a statement about that blind band and not about a vanished root.
# Measured through the real entry point, not by calling the floors one at a
# time (that hides the call order):
#
#   scenario                                              reported by
#   biggest conf.d root zeroed                            shipped-root floor
#   `total - floor` keys eroded, no root emptied          nobody
#   one key more than that, no root emptied               THIS floor, alone
#
# That table is asserted by
# `test_the_artifact_key_floor_owns_erosion_that_empties_no_root`, so the
# derivation cannot drift from the behaviour again; the numbers are re-derived
# there from the constants rather than copied out of this comment.
#
# ⚠️ HEADROOM, measured, and ⛔ THE TWO DIRECTIONS ARE OPPOSITE MEASUREMENTS OF
# DIFFERENT THINGS. This block said so ("BOTH directions", "nobody should
# discover the direction by guessing") and then printed one bare number per
# class anyway — the direction was named, the MAGNITUDE had no tripwire, and it
# duly went stale. Meanwhile the fifth floor's comment below quotes a "10 keys"
# figure for this same artifact floor which is the OTHER direction entirely (it
# now says DOWNWARD in so many words; it did not). Spelled out so the two can
# never be read as one, and pinned by
# `test_the_headroom_note_states_both_directions_and_both_are_current`:
#
#   UP (addition) — the paired bracket test requires each floor to stay strictly
#     ABOVE "biggest conf.d root / producer gone". A pure ADDITION raises that
#     lower bound, so too many new keys turn the TEST red. Today: artifacts
#     tolerate +8, generators +28. Red here means RAISE the floor.
#     (Was "+9" for artifacts, off by one: 70 total − 19 in the biggest root
#      = 51, and 51 + 9 = 60 is not strictly below the floor of 60.)
#   DOWN (removal) — the floor itself fires when its class drops below it, which
#     is the floor doing its job. Today the slack is total − floor: artifacts 10
#     keys, generators 22. Red here means REPAIR THE PRODUCER — and only if keys
#     were removed on purpose (a fixture retired) does it mean LOWER the floor,
#     in the same commit, naming what went.
#
# Either way it is one constant and a line in the commit message. ⛔ The two
# slacks are not comparable and must never be subtracted from one another: UP is
# measured against "biggest group gone", DOWN against the floor.
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

# ── FIFTH floor: every conf.d root, not just the shipped three (#1411) ───────
#
# The four floors above leave a measured gap: with the shipped roots covered by
# the table above and the rest covered only by a GLOBAL key floor with 10 keys
# of DOWNWARD slack (today's artifact total 70 − the floor of 60: how many keys
# may disappear before that floor speaks), a whole fixture tree can stop
# yielding in silence. ⛔ Not the +8 in that floor's own HEADROOM note — that
# one is UPWARD, measured against "biggest root gone", and asks for a HIGHER
# floor. Same word, opposite directions, different baselines.
#
# ⚠️ TWO FROZEN HISTORICAL MEASUREMENTS, both of the four-floor subset as it ran
# at `72fdaf56` — the commit before this floor existed. They are kept BECAUSE
# they are frozen: that subset no longer exists, so nothing can make them drift.
# They are NOT today's figures.
#
# ⛔⛔ AND THEIR VALUES ARE NOT WRITTEN HERE — only their CONDITIONS are. The
# integers live in exactly one place: the assertions in
# `test_the_frozen_measurements_replay_against_their_own_commit`, where they are
# re-measured at that commit rather than restated. Same policy as the coverage
# split above (#1392, "a number restated in a comment is a number nobody
# re-measures"), and this block is where that policy had not been applied.
# Measured before it was: every frozen figure written here could be edited to
# anything at all — including the numerator/denominator pairing this block
# forbids in words — and the suite stayed green and the gate stayed at rc=0.
#
# ⚠️ AND "EXACTLY ONE PLACE" WAS FALSE FOR A ROUND, in the sentence that says
# it. Deleting the figures from this block left three further copies standing —
# the CHANGELOG entry for #1411, the note beside `main()`'s live fraction, and
# two docstrings in the test file — and each was measured editable to the
# pairing this block forbids in words with the suite green and the gate at
# rc=0. Deleting one copy is not the same as having one source, and the claim
# was written before the count was taken. All of them carry conditions only
# now. ⛔ If a figure ever has to be restated somewhere, THIS sentence is the
# one that becomes false first, so it is the one to change with it.
#
# ⛔⛔ THEY HAVE A TRIPWIRE, AND IT IS NOT A COMPARISON AGAINST TODAY'S TREE.
# An earlier revision of this very block said "NEITHER HAS A TRIPWIRE, AND
# NEITHER CAN", and the second half was false by construction. The objection it
# rested on is still correct as far as it goes: re-deriving these numbers from
# TODAY's tree would compare across worlds and would go red the next time a
# fixture is retired — ordinary maintenance, the same objection that kills
# ratchet-to-measured two notes below. ⛔ And scraping the digits back out of
# this prose with a regex is still not a tripwire: prose has no grammar, a
# re-worded sentence breaks it, and what it would compare is still the dead
# subset. What neither objection rules out is REPLAYING the measurement in the
# world it was taken in. `git archive 72fdaf56` into a temp dir, `git init &&
# git add -A -f` (the frozen scan shells out to `git ls-files`), import THAT
# commit's own gate module, and re-run the scenarios through ITS floor
# functions and ITS floor constants. Nothing in today's tree is an input, so
# retiring a fixture cannot move the answer;
# `test_the_frozen_measurements_replay_against_their_own_commit` does exactly
# this. The cost is dominated by materialising the commit, which is paid once
# per module; the measurement then runs the frozen `_defaults_faces()` once for
# a clean baseline, then once per root and once per artifact directory. (No
# stopwatch figure here either — timings are measurements too, and the ones
# that used to stand here were a machine's, quoted to a tenth of a second.)
#
# ⛔ The replay CALLS the frozen floors; it does not re-implement their tests.
# That is not a style preference — a throwaway script written while designing
# this got ① wrong by applying the shipped root's
# FILE-COUNT check to the keys-go-to-zero scenario, where every file is still on
# disk by definition. A re-implementation is a second opinion about the floors
# wearing the floors' authority, which is the same defect `_floors_firing_on`
# was written to avoid one world over. The scenarios are staged as INPUT — the
# victim's artifacts are rewritten to hold no `defaults:` section (①) or dropped
# from the scan's output (②) — and `_defaults_faces()` at that commit is the
# oracle: it raises, or it does not.
#
# Four conditions decide each answer, so all four are stated for each — and the
# answers themselves stay in the replay.
#
#   ① grouping `conf_d_root`; scenario a root's keys go to ZERO with its files
#     still on disk; floors all four above counted together (both file floors,
#     both per-class key floors, `_SHIPPED_CONFD_ROOTS`); exempt: the root that
#     already carried zero keys at that commit, zero being its legal state,
#     which is the one `_DEFAULTS_ROOTS_MAY_BE_EMPTY` names today. This is the
#     resolution the floor itself works at.
#     ⚠️ TWO POPULATIONS, and pairing a numerator with the wrong one is the
#     exact defect this whole block is a correction of. SCANNED is the conf.d
#     roots holding a defaults artifact (the repo has more conf.d roots than
#     that — see `_DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS`); WATCHED is scanned
#     minus the exempt root. The replay therefore reports ① twice, once over
#     each population, with the numerator counted on the same side of the
#     exemption as its denominator. ⛔ A fraction that takes one half from each
#     is not a harmless slip: the post-exemption numerator is independently the
#     right answer to a DIFFERENT (scenario × exemption) pair — files-deleted,
#     same grouping — so the wrong fraction reads exactly like a right one. It
#     is a value the replay cannot produce, because each fraction is assembled
#     where its population is known and asserted as one object; two loose
#     integers cannot forbid it, which was measured.
#
#   ② the SAME four floors at a FINER resolution: grouping is each artifact's
#     own DIRECTORY (one per tracked artifact, so nested layers inside one
#     conf.d tree are separate rows); scenario is the whole group GONE, files
#     and all; exempt — none.
#     ⚠️ "and it makes no difference here" stood here and is only true of the
#     NUMERATOR: deleting the recipes root's files trips `_SHIPPED_CONFD_ROOTS`'
#     `min artifacts`, so that root is caught either way and the numerator does
#     not move. The DENOMINATOR is a different matter — applying ①'s convention
#     and subtracting the exempt root's directories would restate ② over a
#     smaller population. ②'s population is un-exempted on purpose, so the two
#     fractions are built on DIFFERENT conventions: do not subtract or compare
#     them.
#
# ⚠️ "ALL FOUR COUNTED TOGETHER" MEANS ALL FOUR WERE IN EFFECT — not that all
# four contribute. Measured on both scenarios: standing the two FILE floors down
# leaves every fraction identical, because ① never removes a file and ② removes
# one directory from a scan that stays far above them. The answers are decided
# by `_assert_keys_floor` and `_SHIPPED_CONFD_ROOTS`; the file floors are
# evaluated and stay silent. Stated because "four floors were counted" reads as
# "four floors were needed", and the next person to narrow the subset would
# otherwise expect the answer to move when it will not. The replay's
# `stand_down` accordingly takes only the two that can move it.
#
# ⛔ WHY TWO, since one measurement of one thing would be simpler: ① is the
# resolution this floor works at (it watches roots), and ② is the resolution the
# CORRECTION COMMENT on #1411 reports, so it is the one an outside reader can
# check against. ⚠️ NOT the issue BODY — that uses ②'s grouping but runs only
# `_assert_keys_floor`, so it is a third (scenario × grouping × floor subset)
# and its figure is legitimately different. A reader who checks ② against the
# body concludes the number moved when nothing did.
#
# ⚠️ "as it ran at `72fdaf56`" is precise about the FLOORS and loose about the
# exemption, deliberately: `_DEFAULTS_ROOTS_MAY_BE_EMPTY` did not exist at that
# commit (measured: zero occurrences in the file). The four floors are what was
# frozen; the exemption is a condition of how the measurement is RE-RUN, named
# because it changes ①'s answer — not a claim that the list was there. That is
# also why the replay DERIVES the exemption from the
# frozen tree ("which roots already carried zero keys there") instead of reading
# today's `_DEFAULTS_ROOTS_MAY_BE_EMPTY`: reading today's list would let an
# ordinary future addition to it move a frozen number, which is the failure this
# whole block exists to avoid. ② needs no such caveat: it does not use the
# exemption.
#
# ⛔ THE LESSON, which is what this block is really for and the one thing a
# reader should carry away: the earlier revision's figure did not go stale by
# drifting away from a measurement. It went stale when a later edit rewrote the
# sentence's GROUPING and left the numerator alone — the denominator moved
# worlds and the numerator stayed. A count and its grouping are one indivisible
# fact, and editing either half of a fraction on its own manufactures a
# measurement nobody ever took. The same defect, one step smaller, is why
# `main()`'s live line prints scanned, exempt and watched rather than a bare
# `N of M` — and why the figures above are conditions here and integers only in
# the replay, where changing one means re-running it.
#
# ⛔ TODAY's figure is deliberately not written here (#1392 policy, same as the
# coverage split above). `main()` re-derives it on every run via
# `_roots_only_the_fifth_floor_notices` and prints it with its population,
# grouping, scenario, floor subset and exemption attached, so the live number is
# a byte of output rather than a claim in a comment that nobody re-measures.
#
# ⛔ This floor deliberately watches ONE thing: a root that stops contributing
# ENTIRELY (or disappears). It does NOT watch partial shrink — "12 keys became 1"
# is not detected here, and the global key floor only sees it if the total drops
# past its own margin. Say so in the message; a floor whose scope readers guess
# at is how the next person stops checking.
#
# ⛔ Why not per-root NUMBERS for all 12 (the obvious extension of
# `_SHIPPED_CONFD_ROOTS`): 9 of the 12 roots hold a single file, and for those the
# bracket test that governs that table (`lo == 0` branch) collapses the pair to
# "at least 1 artifact, at least 1 key" — literally this floor. Twelve
# hand-maintained number pairs would buy nothing on 9 of them while adding twelve
# numbers that go stale every time a fixture is retired, and the neighbouring
# floor's own message already had to be rewritten because maintainers read
# "adjust the number" as the remedy.
#
# ⛔ Why not auto-ratcheting those numbers up to the measured value: the bracket
# test asserts `lo < min_keys < total` — STRICTLY below today's total — and its
# comment records that `<= total` was the first spelling, killed by mutation
# because a zero-headroom floor reddens on the next ordinary edit and teaches
# people that floors are adjustable. Ratchet-to-measured is that banned thing,
# automated.
#
# The SET is pinned rather than derived, for the same reason the table above is
# hand-maintained: a set built from the scan disappears along with the root it
# was supposed to notice. Adding a root is also a violation — not because new
# roots are bad, but because an unpinned new root is unwatched, and the pin has
# to stay current to mean anything (same exit-lock discipline as KNOWN_UNWIRED).
#
# ⛔ …and the "adding a root is a violation" half was, as first shipped, unable
# to fire for a whole class of new root. It compared the pin against the roots
# the DEFAULTS SCAN reached, so a new conf.d tree with no `_defaults*` file in
# it produced nothing to compare and stayed invisible — measured: two such trees
# were already in the repo when this floor landed, and adding a third kept the
# gate at rc=0. The comparison base is now every conf.d root `git ls-files`
# knows about (`_tracked_confd_roots`), and a root with no defaults artifact is
# accounted for explicitly in `_DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS` below.
_DEFAULTS_CONFD_ROOTS: frozenset[str] = frozenset({
    "components/threshold-exporter/config/conf.d",
    "rule-packs/recipes/examples/conf.d",
    "try-local/seed/conf.d",
    "tests/e2e-bench/fixture/synthetic-v1/conf.d",
    "tests/e2e-bench/fixture/synthetic-v2/conf.d",
    "tests/golden/fixtures/array-replace/conf.d",
    "tests/golden/fixtures/full-l0-l3/conf.d",
    "tests/golden/fixtures/l0-only/conf.d",
    "tests/golden/fixtures/metadata-skipped/conf.d",
    "tests/golden/fixtures/mixed-mode/conf.d",
    "tests/golden/fixtures/opt-out-null/conf.d",
    "tests/golden/fixtures/opt-out-null-threshold/conf.d",
})

# Roots that legitimately carry zero threshold keys. ⛔ Explicit, because
# "contributes nothing" is exactly what this floor exists to catch — an implicit
# exemption for whatever happens to be empty today would exempt the failure.
_DEFAULTS_ROOTS_MAY_BE_EMPTY: frozenset[str] = frozenset({
    # Both files here declare only other sections (custom-alert recipe examples);
    # `_SHIPPED_CONFD_ROOTS` already records the same fact as `min keys = 0`.
    "rule-packs/recipes/examples/conf.d",
})

# Tracked conf.d roots that hold NO defaults artifact at all. Root -> why.
#
# ⛔ A DIFFERENT list from `_DEFAULTS_ROOTS_MAY_BE_EMPTY`, and merging them would
# destroy the distinction the fifth floor is built on. That list says "this tree
# HAS defaults artifacts and their key count is legitimately zero" — a claim
# about content, re-checked on every run. This one says "this tree has no
# `_defaults*` file for the scan to reach in the first place" — a claim about
# the file set. Filed together, a tree that lost its `_defaults.yaml` would be
# indistinguishable from one that never had it, and the remedies are opposite.
#
# ⛔ Both directions are enforced, so an entry cannot rot into an exemption: a
# root here that GROWS a `_defaults*` file is reported by the `unpinned` arm of
# `_assert_every_root_contributes` (it starts contributing, and it is not in
# `_DEFAULTS_CONFD_ROOTS`), and a root here that is deleted is reported by the
# `stale` arm. Neither costs a number to maintain.
#
# ⚠️ …and THIS list is the only one the `stale` arm actually speaks for. It runs
# after `missing`, which catches a vanished `_DEFAULTS_CONFD_ROOTS` entry first
# (that tree's `_defaults*` file went with it, so it contributed nothing) —
# measured: `stale` is structurally unreachable for all 12 of those and
# reachable for exactly the 2 here. The reasoning for that order is written
# beside the arm itself; the consequence is that "a root here that is deleted"
# above means literally here, not anywhere in the two lists.
_DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS: dict[str, str] = {
    "components/da-tools/app/examples/cardinality-demo/conf.d":
        "the QUICKSTART cardinality demo: one `tenants:` file carrying enough "
        "per-tenant metric keys to blow da-guard's --cardinality-limit. It "
        "demonstrates a TENANT-side budget, so there is no platform-defaults "
        "layer in it for the defaults-tier faces to read.",
    "tests/golden/fixtures/flat/conf.d":
        "the golden fixture for the FLAT (no-hierarchy) layout — its whole "
        "point is a conf.d holding a single `tenants:` file with no "
        "`_defaults.yaml` above it, so growing one would change the very "
        "shape the fixture characterises.",
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


def _artifact_face(rel: str) -> str:
    """The face label for artifact `rel`. ONE spelling, used everywhere."""
    return f"{_ARTIFACT_FACE_PREFIX}{rel})"


def _artifact_rel(face: str) -> str | None:
    """`rel` back out of a label built by `_artifact_face`, else None.

    ⛔ NOT a re-introduction of prefix classification (#1393). It is never asked
    "is this an artifact" — every caller already holds the ARTIFACTS dict, so
    class membership is still a consequence of which loop built the entry. This
    only recovers WHICH FILE an entry came from, and it lives next to its
    inverse so the two cannot drift into disagreeing.

    Returns None for anything that is not one of our labels — a hermetic caller
    injecting `{"probe": …}` gets "no path" rather than a mangled one.
    """
    if not (face.startswith(_ARTIFACT_FACE_PREFIX) and face.endswith(")")):
        return None
    return face[len(_ARTIFACT_FACE_PREFIX):-1]


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


def _tracked_paths() -> list[str]:
    """Every repo-relative path in the INDEX. The one place this module shells out.

    ⛔ A SEAM, and that is half its job (#1411). Both derivations below — the
    defaults artifacts and the conf.d roots — used to run their own identical
    `git ls-files`, which made "does this function read the index at all?"
    untestable without a repo mutation: a body that answered from a module
    constant instead was indistinguishable from a body that scanned. Every test
    of those derivations either took their output as its own baseline or
    compared them against a pin they could satisfy by echoing it. With the
    listing injectable, a test feeds a SYNTHETIC one and requires the answer to
    follow it — which no constant-returning body can do. Measured: replacing
    `_tracked_confd_roots`' body with `set(_DEFAULTS_CONFD_ROOTS) |
    set(_DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS)` left the suite at 105 passed
    and the gate at rc=0 — and a new conf.d tree with no `_defaults*` file in it
    then went unreported again, i.e. exactly the hole this floor was opened for.

    ⛔ AND THE SEAM MOVES THE PROBLEM UP ONE LEVEL — the first version of this
    note stopped one step short. Injecting `paths` proves the CALLEE follows its
    argument; it says nothing about the argument the callee builds when nobody
    passes one, which is this function. Measured as the suite stood at
    `3864b63`, before the test named below existed: replacing THIS body with a
    literal list of today's answer left it at 107 passed and the gate at rc=0.
    Every test either fed its own listing or compared this against a fresh
    `git ls-files` it ran itself, and a constant copied from today satisfies
    both — until the index next changes, which is not a tripwire, it is a delay.
    ⚠️ The population is named because the number is not today's: re-run on the
    final tree that same mutation is 1 failed / 113 passed, and the one red is
    `test_the_index_listing_really_comes_from_the_index` — i.e. the guard now
    catches it. A mutation score quoted without the suite it was taken against
    is the same defect as a fraction quoted without its population.

    So this one is pinned by pointing it at a DIFFERENT repository:
    `test_the_index_listing_really_comes_from_the_index` builds a throwaway git
    repo, re-points `PROJECT_ROOT` at it, and requires the answer to be that
    repo's files. A body that ignores `PROJECT_ROOT` cannot follow it, and no
    snapshot of THIS repo can be right about a repo that did not exist when the
    snapshot was taken.
    """
    import subprocess  # local: only this module-level scan shells out

    out = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "ls-files", "-z"],
        capture_output=True, check=True, timeout=60)
    return [rel for rel in out.stdout.decode("utf-8", "replace").split("\0") if rel]


def _tracked_defaults_artifacts(paths: list[str] | None = None) -> list[str]:
    """Repo-relative paths of every TRACKED platform-defaults artifact.

    Separate from `_defaults_artifacts` so the floor can count what the scan
    found BEFORE exemptions are subtracted — otherwise exempting files walks the
    count down toward the floor and the floor's message ("repair the scan or
    move the file(s) back") names the one repair that is not the problem.

    `paths` defaults to the real index listing, the same way `run_check` defaults
    its inputs to the real extractors; `test_the_index_listing_is_a_real_seam_on
    _both_derivations` injects a synthetic one and requires the answer to follow
    it. ⚠️ That sentence was here before the test was — the injectable half had
    no caller and no assertion, so the docstring was describing an intention.
    Both derivations that take this seam are exercised through it now.
    """
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
    if paths is None:
        paths = _tracked_paths()
    return sorted(
        rel for rel in paths
        if _is_defaults_artifact(rel.rsplit("/", 1)[-1])
        and (PROJECT_ROOT / rel).is_file())


def _defaults_artifacts() -> list[Path]:
    """The artifacts this face reads: tracked, minus the explicit exemptions."""
    return [PROJECT_ROOT / rel for rel in _tracked_defaults_artifacts()
            if rel not in _DEFAULTS_ARTIFACT_EXEMPT]


def _tracked_confd_roots(paths: list[str] | None = None) -> set[str]:
    """Every conf.d root the INDEX knows about — the fifth floor's universe.

    ⛔ Deliberately WIDER than the defaults scan, and that width is the whole
    point. The pin used to be compared against the roots that yielded a defaults
    artifact, which makes the "a new root must be registered" arm structurally
    unable to fire for a root that has no `_defaults*` file: there is nothing to
    compare it to. Two such roots were already in the tree, measured.
    ⛔ `git ls-files`, not a filesystem walk, for the reason `_DEFAULTS_ARTIFACT_
    SUFFIXES` spells out at length: from the main repo a walk reaches every other
    branch's working copy under `.claude/worktrees/`.

    ⛔ `paths` is the seam that makes "it reads the index" ASSERTABLE rather than
    merely stated — see `_tracked_paths`. Every other test of this floor takes
    this function's own output as its baseline and adds or removes one entry, or
    asserts `tracked == registered`; a body that returned the two registration
    lists unioned satisfies all of them and re-opens #1411 in silence. Feeding a
    synthetic listing is what tells the two bodies apart.
    """
    if paths is None:
        paths = _tracked_paths()

    # ⛔ This function's NAME is "every conf.d root the index knows about" and
    # its body knew only one spelling of it (#1434). `conf_d_root` matches
    # `conf.d` exactly, while `_is_defaults_artifact` folds case ON PURPOSE
    # (it mirrors `config_hierarchy.go`, which lowercases the FILE name). The
    # asymmetry is not symmetric in consequence: measured on synthetic index
    # listings, a `CONF.D/` tree holding a `_defaults.yaml` is caught by
    # nothing, and neither is one holding no artifact — the `unpinned` and
    # `unregistered` arms below both derive their universe from here, so the
    # whole registration exit-lock goes with it. The identical tree spelled
    # `conf.d` reddens both.
    #
    # Rather than fold case here — `conf_d_root` is shared with the
    # guard-defaults workflow (#1219), and the repo's other references to
    # `conf.d` — the confd-schema hooks in `.pre-commit-config.yaml`, and the
    # `blast-radius`, `config-diff`, `backtest` and `guard-defaults-impact`
    # workflows — would stay case-sensitive, so this gate would be demanding
    # registration for a tree nothing else can see. ⚠️ An earlier wording put a
    # COUNT here ("89 references across 8 CI files"); blind review could not
    # reproduce it under any of four definitions (80/8, 66/7, 50/6, 105/10) and
    # it was self-contradicting besides ("other than" excludes a file that the
    # 8 includes). The consumers are named instead: a name can be checked by
    # opening the file, and the argument never needed the total. The fix is
    # to make the divergence unobservable: if the index only ever holds one
    # spelling, a case-sensitive and a case-insensitive reader cannot disagree.
    # ⚠️ This is a claim about the INDEX, not the filesystem, deliberately: the
    # developer machines here are NTFS and the dev container mounts them over
    # 9p, both case-insensitive, so a filesystem probe proves nothing either
    # way. `git ls-files` says the same thing everywhere.
    near_miss = sorted({
        str(parent)
        for rel in paths
        for parent in PurePosixPath(rel.replace("\\", "/")).parents
        if parent.name.casefold() == CONF_D and parent.name != CONF_D})
    if near_miss:
        raise _GateViolation(
            f"the index holds {len(near_miss)} directory(ies) that differ from "
            f"{CONF_D!r} only by case: {near_miss}. Every conf.d-aware check in "
            "this repo spells it in lower case, so a tree named this way is "
            "invisible to all of them at once — this floor, the confd-schema "
            "hooks, blast-radius, config-diff, backtest, and the "
            "guard-defaults workflow. It is not a supported layout, and the "
            "silence it buys is the whole failure this floor exists to end.\n"
            "  RENAME the directory to lower case. Registering it under this "
            "spelling is not an alternative — it would claim a set of checks "
            "are running on the tree that structurally are not.\n"
            "  ⚠️ TWO STEPS on a case-insensitive filesystem, which is what "
            "the machines this repo is developed on have: a direct "
            "`git mv A/CONF.D A/conf.d` fails with `Invalid argument`. Go via "
            "a temporary name — `git mv A/CONF.D A/_tmp && git mv A/_tmp "
            "A/conf.d`.\n"
            "  ⚠️ And the rename is not the end: you WILL still have to "
            "register the tree, in one of the two lists the next message "
            "names. Renaming makes it visible; it does not account for it.\n"
            "  ⛔ A directory whose name is merely SIMILAR (`conf_d`, `confd`) "
            "is not this; only a case variant is, because only a case variant "
            "reads as correct to a human and as absent to `git ls-files`. "
            "(#1434)")

    roots = set()
    for rel in paths:
        root = conf_d_root(rel)
        if root:
            roots.add(root)
    return roots

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
            f"are NOT counted here — {len(_DEFAULTS_ARTIFACT_EXEMPT)} exempted.) "
            "(TRK-344 / #1392)")
    to_read = _defaults_artifacts()
    if len(to_read) < _DEFAULTS_ARTIFACT_READ_FLOOR:
        raise _GateViolation(
            f"the scan found {len(scanned)} tracked file(s) but only {len(to_read)} "
            f"survive the exemption table, below the read floor of "
            f"{_DEFAULTS_ARTIFACT_READ_FLOOR}. The scan is FINE — "
            f"{len(_DEFAULTS_ARTIFACT_EXEMPT)} exemption(s) consumed it, and zero "
            "faces pass the placement check below perfectly. SHORTEN THE EXEMPTION "
            "LIST; repairing the scan is not the remedy here, and neither is "
            "lowering the floor. (TRK-344 / #1392)")
    read_pairs = [(path.relative_to(PROJECT_ROOT).as_posix(),
                   _read_artifact_keys(path)) for path in to_read]
    artifacts = {_artifact_face(rel): keys for rel, keys in read_pairs}
    by_root, all_by_root = _bucket_by_root(read_pairs)

    # ⛔ A non-vacuity floor for the fifth floor's own printed FIGURE (#1411).
    # Every other number this module prints already has one — the two file
    # floors above, the two per-class key floors below — and this one shipped
    # without. `run_check` does not get `read_pairs`; it rebuilds them by
    # INVERTING the labels with `_artifact_rel`, so the figure's whole numerator
    # rests on `_artifact_face` and its inverse agreeing. Measured on a label
    # format that stayed human-identical but stopped round-tripping: the gate
    # printed `0 of 0`, with all four conditions still spelled out word for word,
    # and exited 0 with `✅ threshold reachability OK`. A vacuous figure that
    # still reads like a measurement is worse than no figure.
    #
    # Round-trip equality rather than "did we get any root": the weaker form
    # cannot see a HALF-inverted set, and the "no root at all" end is already
    # covered — if the inversion works and still yields no root, every pinned
    # root is absent and `_assert_every_root_contributes`' `missing` arm fires.
    # ⛔ Count over the FACES, not over `inverted` — that is a set, so every
    # label failing the same way collapses to a single `None` and the message
    # would report "1 of 17" for a total failure.
    #
    # ⛔ …and count the labels that do not come back as THE REL THEY WERE BUILT
    # FROM, not the ones that come back as `None`. The two differ on the whole
    # "invertible but wrong" half of the failure space, and the message used the
    # narrower one while the TRIGGER used set inequality: measured on a label
    # pair that round-tripped to a mangled-but-non-None path, the floor fired
    # correctly and then announced `0 of 17` — a breach reported as nothing
    # broken, in the one message whose job is to say how much drifted. The
    # denominator is `read_pairs`, not `artifacts`, because a label format that
    # stopped being INJECTIVE collapses two artifacts into one dict entry and
    # `len(artifacts)` would then under-count the population it is reporting on.
    real_rels = [rel for rel, _keys in read_pairs]
    inverted = {_artifact_rel(face) for face in artifacts}
    if inverted != set(real_rels):
        broken = sum(1 for rel in real_rels
                     if _artifact_rel(_artifact_face(rel)) != rel)
        raise _GateViolation(
            f"{broken} of {len(real_rels)} "
            "artifact face label(s) no longer round-trip through "
            "`_artifact_face` / `_artifact_rel`. The fifth floor's printed "
            "figure is derived by inverting these labels, so a drift between "
            "the two makes it report `0 of 0` — a vacuous number that still "
            "carries all of its conditions and still exits 0.\n"
            "  MOST LIKELY: the label format was edited on one side only. The "
            "two functions sit next to each other precisely so they cannot "
            "drift; change both, in the same commit.\n"
            "  ⛔ This is NOT a request to widen `_artifact_rel` until it "
            "accepts anything — it returns None for labels that are not ours on "
            "purpose, so a hermetic caller's synthetic face is not mistaken for "
            "a file. It is a request that the labels this module BUILDS invert. "
            "(#1411)")

    _assert_shipped_roots_intact(by_root)
    _assert_every_root_contributes(all_by_root)
    _assert_keys_floor(generators, artifacts)
    return generators, artifacts


def _read_artifact_keys(path: Path) -> dict[str, KeyInfo]:
    """One artifact's `defaults:` keys, with the FILE NAMED on any failure.

    ⛔ The reader names the file. `read_text` raises with the path attached, but
    `yaml.safe_load` does not — its error says `<unicode string>`, and with the
    scope derived rather than 2 hardcoded paths, "which file" is precisely the
    information that was missing (blind review, round 4).

    Extracted as its own function so a test can make ONE root read empty and
    still go through the real `_defaults_faces()` — which is what pins the fifth
    floor's WIRING rather than merely re-checking that the floor function exists.
    """
    rel = path.relative_to(PROJECT_ROOT).as_posix()
    try:
        return _defaults_section(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — re-raised with provenance
        raise RuntimeError(f"{rel}: {exc}") from exc


def _bucket_by_root(
    pairs: list[tuple[str, dict[str, KeyInfo]]],
) -> tuple[dict[str, list[tuple[str, dict[str, KeyInfo]]]],
           dict[str, list[tuple[str, dict[str, KeyInfo]]]]]:
    """(shipped-root view, every-root view) of the artifacts that were read.

    ⛔ ONE implementation, called by `_defaults_faces` and by the floor probe
    below. A probe that re-bucketed with its own copy of these two rules would
    be reporting on its copy — and would keep answering confidently after the
    real bucketing changed shape, which is the exact way this file has been
    wrong before.
    """
    by_root: dict[str, list[tuple[str, dict[str, KeyInfo]]]] = {
        r: [] for r in _SHIPPED_CONFD_ROOTS}
    # ⛔ NOT pre-seeded from the pin: a root that vanished must show up as
    # absent, and seeding it with an empty list would make "gone" and "present
    # but empty" indistinguishable — the two have different remedies and the
    # floor below says so separately.
    all_by_root: dict[str, list[tuple[str, dict[str, KeyInfo]]]] = {}
    for rel, keys in pairs:
        for root in _SHIPPED_CONFD_ROOTS:
            if rel == root or rel.startswith(root + "/"):
                by_root[root].append((rel, keys))
                break
        # ⛔ EVERY root, by the repo's own definition — not just the shipped
        # three. An artifact outside any `conf.d` tree yields None and is simply
        # not covered by the fifth floor: it is still scanned, still counted by
        # the file and key floors, and still placement-checked; what it cannot
        # have is a per-ROOT floor, there being no root. There were none when
        # this was written and none in the history up to that point — stated as
        # a past measurement rather than "today", because nothing re-counts it.
        # ⚠️ This used to say the gap was "recorded in the floor's own
        # docstring". It was not — that docstring listed partial shrink and
        # re-nesting and stopped, so the pointer sent a reader somewhere the
        # sentence did not exist (#1434). It is now recorded there, and this
        # comment no longer promises on its behalf.
        all_root = conf_d_root(rel)
        if all_root:
            all_by_root.setdefault(all_root, []).append((rel, keys))
    return by_root, all_by_root


_FLOOR_SHIPPED = "shipped-root floor (_SHIPPED_CONFD_ROOTS)"
_FLOOR_KEYS = "global per-class key floor"


def _floors_firing_on(
    generators: dict[str, dict[str, KeyInfo]],
    pairs: list[tuple[str, dict[str, KeyInfo]]],
) -> list[str]:
    """Which of the two simulable floors fire on exactly this input.

    ⛔ It CALLS the real `_assert_shipped_roots_intact` and `_assert_keys_floor`
    and catches `_GateViolation`. It does NOT re-implement their arithmetic —
    a local `total - keys < FLOOR` would be asserting the MODEL, and would go on
    answering confidently after a floor changed shape, reporting a blind spot
    that had been closed or missing one that had opened.
    """
    by_root, _all_by_root = _bucket_by_root(pairs)
    artifacts = {_artifact_face(rel): keys for rel, keys in pairs}

    fired: list[str] = []
    try:
        _assert_shipped_roots_intact(by_root)
    except _GateViolation:
        fired.append(_FLOOR_SHIPPED)
    try:
        _assert_keys_floor(generators, artifacts)
    except _GateViolation:
        fired.append(_FLOOR_KEYS)
    return fired


def _floors_that_notice_a_zeroed_root(
    generators: dict[str, dict[str, KeyInfo]],
    read_pairs: list[tuple[str, dict[str, KeyInfo]]],
    root: str,
) -> list[str]:
    """Which of the four EARLIER floors fire BECAUSE `root`'s keys went to zero.

    Empty list == all four stay silent, i.e. that root is in the blind spot the
    fifth floor was added for (#1411).

    ⛔ BEFORE and AFTER, not just after — the same discipline this floor was
    justified by. A floor already breached on the unmodified input would
    otherwise be credited with catching every root in turn, and the derived
    figure would silently read zero blind spots at exactly the moment the gate
    was most broken. Only a floor that was quiet and then spoke counts.

    ⛔ Floors 1–2 are not simulated because they CANNOT move here: they count
    tracked FILES, and this scenario leaves every file exactly where it is. That
    is the defining feature of the scenario, not an omission — the files-deleted
    scenario is a different measurement (see the fifth floor's own comment).
    """
    before = _floors_firing_on(generators, read_pairs)
    zeroed = [(rel, ({} if conf_d_root(rel) == root else keys))
              for rel, keys in read_pairs]
    return [f for f in _floors_firing_on(generators, zeroed) if f not in before]


def _roots_only_the_fifth_floor_notices(
    generators: dict[str, dict[str, KeyInfo]],
    read_pairs: list[tuple[str, dict[str, KeyInfo]]],
) -> list[str]:
    """Roots whose keys could go to zero today with only the fifth floor speaking.

    The universe is the roots the defaults scan actually reached, minus
    `_DEFAULTS_ROOTS_MAY_BE_EMPTY` (zero is their legal state, so "could go to
    zero unnoticed" is not a defect for them).
    """
    roots = {conf_d_root(rel) for rel, _keys in read_pairs}
    roots.discard(None)
    return sorted(
        root for root in roots  # type: ignore[misc]
        if root not in _DEFAULTS_ROOTS_MAY_BE_EMPTY
        and not _floors_that_notice_a_zeroed_root(generators, read_pairs, root))


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
            + ("" if not _DEFAULTS_ARTIFACT_EXEMPT else
               "  OR: you just added a _DEFAULTS_ARTIFACT_EXEMPT entry for a "
               f"file in this tree ({len(_DEFAULTS_ARTIFACT_EXEMPT)} "
               "entry/entries). An exemption removes the file from the read "
               "set, so its keys stop counting here — on a shipped root that "
               "is not something an exemption may buy. Carry the keys in a "
               "sibling `_defaults*` file, move the exempted file out of the "
               "conf.d tree, or do not exempt it. (#1434)\n")
            + "  ⛔ LOWERING THESE NUMBERS IS NOT A REMEDY. A shipped root whose "
            "keys went to zero is precisely what this floor is for; editing "
            "_SHIPPED_CONFD_ROOTS to match the new reality re-creates the blind "
            "spot. Only change the table when a tree genuinely moved or stopped "
            "shipping, and then change the confd-schema hooks in "
            ".pre-commit-config.yaml in the same commit (a test pins the ROOT "
            "SET to those hooks — it does NOT police these two numbers, so they "
            "are on you). (TRK-344 / #1392)")


def _assert_every_root_contributes(
        by_root: dict[str, list[tuple[str, dict[str, KeyInfo]]]],
        tracked_roots: set[str] | None = None) -> None:
    """Every pinned conf.d root must still contribute at least one key (#1411).

    ⛔ SCOPE, stated because the neighbouring floors' scopes had to be corrected
    twice: this catches a root going to ZERO or vanishing. It does not catch
    partial shrink, and it does not catch re-nesting (a key moved one level down
    is still a key — `_walk_defaults_keys` counts every level, by design).

    ⚠️ And a third thing it does not cover, moved here from `_bucket_by_root`,
    which claimed this docstring already said it (#1434): a defaults artifact
    that sits under NO `conf.d` tree gets no root, so no per-root floor can
    speak for it. It is not unwatched — the scan, both file floors, the key
    floors and the placement check all still see it — it simply has no root to
    be a member of. Measured at 0 such artifacts today, and `git log` finds
    none in this repo's history either.

    `tracked_roots` defaults to the real index scan, the same way `run_check`
    defaults its inputs to the real extractors; hermetic callers inject a set.

    ⚠️ ONE ARM HAS NO SUCH SEAM, on purpose: the `_DEFAULTS_ROOTS_MAY_BE_EMPTY`
    falsifier reads the artifacts off disk, so for a hermetic caller — whose
    rels do not exist — it reads zero and that arm is inert. A `threshold_shape`
    parameter was added for it and then removed: nothing ever passed it (the
    test that needed it patches the module name instead), so it was a second
    way in that only one of the two was ever proven to work.
    """
    if tracked_roots is None:
        tracked_roots = _tracked_confd_roots()
    seen = set(by_root)
    missing = _DEFAULTS_CONFD_ROOTS - seen
    if missing:
        # ⛔ The exemption half is CONDITIONAL, and printing it unconditionally
        # was measured as its own small defect (blind review): with the table
        # empty the message said "0 entry/entries today" and then spent its
        # longest paragraph on what to do about the entry you did not add,
        # burying the three causes that could actually be yours.
        raise _GateViolation(
            f"conf.d root(s) {sorted(missing)} contributed no artifact at all. "
            "They are pinned in _DEFAULTS_CONFD_ROOTS, so the scan finding "
            "nothing under them means the tree moved, was deleted, or its "
            "files stopped matching `_is_defaults_artifact`. ⛔ Removing the "
            "entry is only correct if the tree genuinely stopped existing — "
            "otherwise it re-creates the blind spot this floor was added for "
            "(#1411)." + ("" if not _DEFAULTS_ARTIFACT_EXEMPT else
            f"\n  ⛔ FOURTH CAUSE, and it applies here: "
            f"_DEFAULTS_ARTIFACT_EXEMPT holds {len(_DEFAULTS_ARTIFACT_EXEMPT)} "
            "entry/entries, and an exemption removes its file from the read "
            "set — so exempting the last `_defaults*` file in a root empties "
            "that root and lands you here. The three remedies above are then "
            "not yours, and following them leads in a circle: dropping the pin "
            "sends you to the 'registered nowhere' message, and the only edit "
            "that turns this green from there is filing the tree under "
            "_DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS — which is a claim about "
            "FILES, and the file is still sitting there. That entry would be "
            "false the moment it was written, and a later 'no longer exists in "
            "the index' report would quote it back as fact.\n"
            "  What is actually available — the same three the shipped-root "
            "floor offers, in the same words on purpose: carry the keys in a "
            "sibling `_defaults*` file, move the exempted file out of the "
            "conf.d tree, or do not exempt it. An exemption is for a file "
            "whose BYTES "
            "must not change; it was never a way to hand a whole root's "
            "coverage back. If none of those three fit your case, this gate "
            "needs to change — say so in an issue rather than making a table "
            "say something untrue. (#1434)"))

    unpinned = sorted(r for r in seen if r not in _DEFAULTS_CONFD_ROOTS)
    if unpinned:
        raise _GateViolation(
            f"conf.d root(s) {unpinned} are not pinned in _DEFAULTS_CONFD_ROOTS. "
            "A new root is not a problem — an UNPINNED one is: nothing would "
            "notice it later going to zero, and a pin that lags reality cannot "
            "detect a disappearance either. Add it (one line). If it "
            "legitimately carries no threshold keys, add it to "
            "_DEFAULTS_ROOTS_MAY_BE_EMPTY as well, with the reason. If it is "
            "currently filed under _DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS, "
            "that entry is now wrong — the tree grew a `_defaults*` file, so "
            "move it here rather than keeping both. (#1411)")

    # ── The registration exit-lock, against the INDEX rather than the scan ──
    # ⛔ Comparing the pin against `seen` — the roots the defaults scan reached —
    # is what the first version of this floor did, and it left the "a new root
    # must be registered" arm structurally unable to fire for a root with no
    # `_defaults*` file: such a root never appears in `seen`, so there is nothing
    # to find unpinned. Measured: two such roots were already tracked when this
    # floor shipped, and adding a third kept the gate at rc=0. Every conf.d root
    # in the index has to be accounted for by exactly one of the two lists.
    registered = set(_DEFAULTS_CONFD_ROOTS) | set(
        _DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS)
    unregistered = sorted(tracked_roots - registered)
    if unregistered:
        raise _GateViolation(
            f"tracked conf.d root(s) {unregistered} are registered nowhere. "
            "Every conf.d tree in the index must be accounted for by exactly "
            "one of two lists, and which one is a question about FILES, not "
            "about content:\n"
            "  * it holds one or more `_defaults*.ya?ml` → _DEFAULTS_CONFD_ROOTS, "
            "and this floor then watches it for going to zero;\n"
            "  * it holds none → _DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS, with "
            "a one-line reason saying why that tree has no platform-defaults "
            "layer.\n"
            "  ⛔ The second list is NOT _DEFAULTS_ROOTS_MAY_BE_EMPTY. That one "
            "means 'has defaults artifacts, and zero keys is legal for them' — "
            "filing a tree with no artifacts there would claim a check is "
            "running on it that structurally cannot. (#1411)")

    # ⛔ SCOPE, and it is narrower than it looks. In practice this arm speaks
    # only for `_DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS`. A tree pinned in
    # `_DEFAULTS_CONFD_ROOTS` that leaves the index takes its `_defaults*` file
    # with it, so it is absent from `by_root` and the `missing` arm above has
    # already raised — measured: this arm is structurally unreachable for all 12
    # of those, and reachable for exactly the 2 artifact-less ones.
    #
    # ⛔ The ORDER is deliberate, not incidental: `missing` first is what a
    # maintainer should meet, because it names three causes (moved, deleted, or
    # the files stopped matching `_is_defaults_artifact`) and the third is the
    # subtle one — a tree still fully present whose files were renamed. Putting
    # `stale` first would answer "no longer exists in the index" to a tree that
    # very much still exists, and lose that hypothesis. So the arms stay in this
    # order and the scope is written down instead.
    stale = sorted(registered - tracked_roots)
    if stale:
        raise _GateViolation(
            f"conf.d root(s) {stale} are registered but no longer exist in the "
            "index. A pin that lags reality cannot detect a disappearance — it "
            "IS the disappearance, already absorbed. If the tree genuinely went "
            "away, delete the entry; if it merely moved, update the path.\n"
            # ⚠️ DISCLOSED GAP, not an oversight — and disclosed rather than
            # guarded on purpose (the stopping rule this round ran under: a
            # guard is only worth adding when its silent failure lets the
            # original defect back). This clause quotes the `missing` arm's own
            # words, "contributed no artifact at all", verbatim. Anything
            # downstream that tells the two arms apart by matching on message
            # TEXT will therefore file a `stale` breach under `missing`.
            # ⚠️ AND ONE READER ALREADY DOES — in
            # tests/lint/test_check_threshold_reachability.py, the test
            # test_a_root_that_vanished_is_reported_differently_from_one_that_emptied
            # asserts `"no artifact at all" in msg`, i.e. it tells the
            # arms apart by exactly this string. An earlier wording here said
            # "nothing downstream does that today", which was false when it was
            # written. It is still harmless — that test drives the `missing`
            # arm, whose input cannot reach `stale` — so the cost stays a
            # misattributed grep rather than a missed breach, and the disclosure
            # stands rather than becoming a guard. If you ever route on these
            # messages, give the arms distinct identifiers FIRST.
            "  ⚠️ Reaching THIS message means the tree held no `_defaults*` file "
            "— it was filed in _DEFAULTS_CONFD_ROOTS_WITHOUT_ARTIFACTS. A pinned "
            "tree that disappears is reported by the 'contributed no artifact at "
            "all' message above instead, and a SHIPPED tree by the shipped-root "
            "floor before either (that one is where the reminder to update the "
            "confd-schema hook in .pre-commit-config.yaml lives, because that is "
            "the arm a shipped tree actually reaches). (#1411)")

    for root in sorted(_DEFAULTS_CONFD_ROOTS - _DEFAULTS_ROOTS_MAY_BE_EMPTY):
        n_keys = sum(len(keys) for _rel, keys in by_root[root])
        if n_keys:
            continue
        files = [rel for rel, _k in by_root[root]]
        raise _GateViolation(
            f"conf.d root {root!r} still has {len(files)} artifact(s) "
            f"({', '.join(files)}) but they now contribute ZERO threshold keys. "
            "The files are present, so every file-count floor is unmoved, and "
            "the global key floor has margin — this root is invisible to all of "
            "them.\n"
            "  MOST LIKELY: the `defaults:` section was renamed, emptied, or "
            "moved under another key. Check the section name first; a renamed "
            "section reads as an empty face, and an empty face passes the "
            "placement check vacuously.\n"
            "  ⛔ Adding this root to _DEFAULTS_ROOTS_MAY_BE_EMPTY is NOT the "
            "remedy unless it genuinely stopped declaring thresholds on "
            "purpose — that list exempts a root from the only check that can "
            "see it. (#1411)")

    # ── The exemption's own exit-lock ──────────────────────────────────────
    # ⛔ The loop above trusts `_DEFAULTS_ROOTS_MAY_BE_EMPTY` completely, so the
    # list is the one place a maintainer can silence this floor with a single
    # line and no other signal — measured: adding a root that carries real keys
    # left the gate at rc=0 and the suite fully green. So the list has to earn
    # its entries on every run: "legitimately carries zero" is a claim about
    # TODAY, and a claim about today is checkable today.
    #
    # ⛔ In the GATE, not only in a test, for the same reason the `unpinned` arm
    # above is here: the exemption's cost is paid by anyone running the gate, so
    # the gate is what has to refuse it. Scope, stated: this checks the entries
    # are still zero. It does not check that an entry is one of the pinned roots
    # (a test does that) — an entry naming nothing at all exempts nothing.
    for root in sorted(_DEFAULTS_ROOTS_MAY_BE_EMPTY):
        n_keys = sum(len(keys) for _rel, keys in by_root.get(root, ()))
        if not n_keys:
            continue
        raise _GateViolation(
            f"conf.d root {root!r} is listed in _DEFAULTS_ROOTS_MAY_BE_EMPTY, "
            f"but it contributes {n_keys} threshold key(s) today. That list "
            "means 'this tree legitimately declares no thresholds', and it "
            "turns the fifth floor OFF for whatever is named in it — so an "
            "entry that still has keys is an exemption covering a tree that "
            "does not need one, and it would go on covering it after the keys "
            "later disappeared.\n"
            "  MOST LIKELY: the entry was added to make a failure go away. The "
            "floor's own message says that is not the remedy; re-read what it "
            "reported.\n"
            "  OR: this tree genuinely started declaring thresholds — good "
            "news. Remove the line, and the floor now watches it. (#1411)")

    # ⛔⛔ AND THE LOCK ABOVE CANNOT SEE THE CASE IT WAS CITED FOR (#1434).
    # `if not n_keys: continue` refuses an entry whose root STILL CARRIES KEYS
    # — and the moment a maintainer reaches for this list is the moment the
    # root has just gone to ZERO and the floor above has said so. The two
    # conditions are complementary. Measured before this arm existed: rename a
    # `defaults:` section away (that floor's own "MOST LIKELY" cause), add one
    # line to the list, and the gate returned rc=0 with the suite green —
    # seven of the twelve roots could be silenced that way, i.e. #1411's blind
    # spot restored by a one-line diff.
    #
    # What separates the two states is NOT "is the section there" — a renamed
    # section is absent in exactly the same way a legitimate one is. It is
    # whether the tree still holds data SHAPED like threshold declarations.
    # `Defaults` is `map[string]float64` (types.go) with null meaning opt-out
    # (ADR-017), so the falsifier is `name: <number|null>` anywhere in the
    # document, whatever the enclosing key is called. Renaming moves the keys;
    # it does not delete the numbers.
    #
    # ⚠️ THE SEPARATION IS MEASURED, and stated with the direction that costs
    # something. Measured when written, across the twelve roots: the
    # legitimately-empty one held ZERO defaults-shaped blocks and every other
    # root held at least one, both as they stand and after renaming their
    # `defaults:` away — so the falsifier separates the two states with margin
    # on every root. ⚠️ That is a property of today's fixtures, not a
    # guarantee, and the two ways it can be wrong are named in
    # `_defaults_shaped_blocks` rather than left to be discovered.
    for root in sorted(_DEFAULTS_ROOTS_MAY_BE_EMPTY):
        stranded = {rel: n for rel, n in
                    ((rel, _defaults_shaped_blocks(rel))
                     for rel, _keys in by_root.get(root, ()))
                    if n}
        if not stranded:
            continue
        raise _GateViolation(
            f"conf.d root {root!r} is listed in _DEFAULTS_ROOTS_MAY_BE_EMPTY "
            "and reads as empty, but its artifacts still hold "
            f"defaults-SHAPED blocks: {stranded} (a non-empty mapping, "
            "appearing as a value, all of whose values are numbers — the "
            "shape `Defaults map[string]float64` actually has). 'This tree "
            "legitimately declares no thresholds' is not true of a tree that "
            "still has such a block in it: the keys have stopped being READ, "
            "which is the failure this floor exists to report, not a reason "
            "to exempt the tree from it.\n"
            "  MOST LIKELY: a `defaults:` section here was renamed or "
            "re-nested, the floor above said so, and this list looked like "
            "the way to make it stop. It is not; repair the section.\n"
            "  OR: that block genuinely is not thresholds — some other schema "
            "whose values happen to be all numbers. Then MOVE IT OUT of the "
            "`_defaults*` file: those files ARE the platform-defaults layer, "
            "so configuration that is not platform defaults is misfiled in "
            "one, and relocating it is a real fix rather than a way around "
            "this arm. ⛔ Deleting this arm is not, because the same shape is "
            "how the blind spot comes back.\n"
            "  ⚠️ It does NOT flag: a recipe entry with string fields, a "
            "tenant stub, `defaults: {}`, booleans, or numeric strings — all "
            "measured, all silent. If you are seeing this for something that "
            "looks like one of those, that IS worth an issue. (#1411 / #1434)")


def _defaults_shaped_blocks(rel: str) -> int:
    """How many `defaults:`-SHAPED blocks one artifact's document still holds.

    The falsifier for a `_DEFAULTS_ROOTS_MAY_BE_EMPTY` entry: it answers "does
    this tree still hold threshold declarations that stopped being read",
    which "is there a `defaults:` section" cannot — a renamed section and an
    absent one look identical from there. Renaming moves the block; it does
    not delete it.

    ⛔ A BLOCK, NOT A LEAF, and the first version counted leaves — every bare
    number or null anywhere in the document. Blind review measured what that
    cost on the ONE root this arm actually guards, whose files are Custom
    Alerts recipe examples: `min_events` (the recipe schema's only integer
    field, which the docs tell you to declare at domain level, i.e. in exactly
    these files) turned the gate red, and so did writing `defaults:` with no
    value — the explicit spelling of the very claim the exemption list makes.
    Both are correct edits; neither has anything to do with a renamed block.

    So the shape is the one `Defaults map[string]float64` actually has: a
    NON-EMPTY MAPPING, APPEARING AS A VALUE, ALL of whose values are numbers.
    A recipe entry has string fields, a tenant stub has null, `defaults: {}`
    is empty, `on:`/`no:` are booleans — none of them are that shape.

    ⛔ WHOLE DOCUMENT, not the `defaults:` subtree, and lists are walked: the
    point is to find the block wherever it moved to. A file the scan cannot
    read at all counts as zero — that is a different failure with its own
    reporting, and raising here would blame the exemption list for it.

    ⚠️ TWO LIMITS, both disclosed rather than papered over:
      * a renamed block whose values are ALL null (ADR-017 opt-out) reads as
        zero. Measured when written: no tracked artifact had a single null
        leaf, so this half had no live input either way;
      * an all-numeric block that is NOT thresholds (say `scrape: {interval:
        30}`) reads as one. The message says what to do about that, and it is
        a real answer rather than a shrug: a `_defaults*` file IS the
        platform-defaults layer, so non-defaults config in one is misfiled.
    """
    import yaml  # local import: the module's convention for the YAML faces

    path = PROJECT_ROOT / rel
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — the reader above names it properly
        return 0

    def is_number(value: object) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def walk(node: object, *, is_root: bool) -> int:
        found = 0
        if isinstance(node, dict):
            if not is_root and node and all(is_number(v) for v in node.values()):
                found += 1
            for value in node.values():
                found += walk(value, is_root=False)
        elif isinstance(node, list):
            for value in node:
                found += walk(value, is_root=False)
        return found

    return walk(doc, is_root=True)


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
            "saying what shrank and why. (TRK-344 / #1392)")

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
            "the same maintenance, in the other direction. (TRK-344 / #1392)")


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
            "it in _DEFAULTS_ARTIFACT_EXEMPT with a reason.\n"
            "  ⛔ UNLESS it is the only `_defaults*` file in its conf.d root, "
            "which most of them are. An exemption takes the file out of the "
            "read set, so exempting the last one empties its root and a "
            "per-root floor reports THAT instead — a second failure whose "
            "message is about something else. The three ways out are the ones "
            "those floors will offer you, in the same words on purpose: carry "
            "the keys in a sibling `_defaults*` file, move the exempted file "
            "out of the conf.d tree, or do not exempt it. There is no third "
            "table to put it in. (#1218 / TRK-344 / #1434)"
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
    # The same discipline applied to the fifth floor's own figure (#1411). The
    # comment that used to carry it put "11" over the 12 conf.d roots, whose two
    # halves came from two different groupings; under this module's own grouping
    # the measurement is a different number, and it took a re-measurement to
    # find that out — so this one is derived on every run too, by the same probe
    # the tests use, and never restated by hand. ⛔ Spelled out rather than
    # quoted as `11 of 12`: that literal never appeared anywhere (see the note
    # in `main()`), and putting it in backticks sends the next reader to a
    # `git grep` that finds nothing, which reads as "the claim is false" rather
    # than "the claim is a paraphrase". This sentence is where that correction
    # was written down and then not applied.
    #
    # ⛔ Rebuilt from the ARTIFACT faces. `_artifact_rel` only recovers WHICH
    # FILE an entry names — the class was already decided by which dict it is in
    # (#1393) — and it returns None for any label this module did not build, so
    # a hermetic caller injecting `{"probe": …}` contributes no path at all.
    #
    # ⚠️ That is a claim about labels that are not OURS, and not the stronger one
    # an earlier wording made ("synthetic labels yield no conf.d root rather than
    # something invented"). A synthetic label SHAPED like ours is taken at face
    # value and does yield a root: measured, `artifact (made/up/conf.d/
    # _defaults.yaml)` produces the root `made/up/conf.d`, which is on no disk
    # anywhere. There is no defence against that and none is wanted — the figure
    # describes the faces it was handed, and a caller that hands it invented
    # files gets a figure about invented files. What IS defended is the real
    # run: `_defaults_faces()` refuses to return labels that do not round-trip,
    # so the numerator cannot silently go vacuous there.
    read_pairs = [(rel, keys) for rel, keys in
                  ((_artifact_rel(face), keys)
                   for face, keys in artifact_faces.items())
                  if rel is not None]
    blind_roots = _roots_only_the_fifth_floor_notices(generator_faces, read_pairs)
    scanned_roots = {conf_d_root(rel) for rel, _k in read_pairs} - {None}
    # ⛔ The DENOMINATOR the numerator is actually drawn from. `blind_roots`
    # comes out of `_roots_only_the_fifth_floor_notices`, which subtracts
    # `_DEFAULTS_ROOTS_MAY_BE_EMPTY` before it starts — so printing it over
    # `scanned` was two different populations sharing one fraction bar. The
    # predicate ("keys go to zero and all four earlier floors stay silent") is
    # counted over the WATCHED population; counted over `scanned` it can only
    # come out the same or larger, since the exemption only ever removes roots.
    # `1 exempt` was printed, but inside a parenthesis about grouping — a fact
    # on the line, not a modifier on the fraction. All three numbers are printed
    # now so the reader can re-derive: watched = scanned − exempt.
    # ⛔ NO INTEGERS HERE, deliberately, and this comment used to carry three of
    # them. Today's are on `main()`'s output every run; the frozen historical
    # pair is re-measured by
    # `test_the_frozen_measurements_replay_against_their_own_commit`. A
    # restatement in a comment beside the code that computes the number is the
    # most convincing possible copy and still nothing re-measures it.
    exempt_roots = scanned_roots & set(_DEFAULTS_ROOTS_MAY_BE_EMPTY)

    stats = {
        "generator_faces": len(generator_faces),
        "artifact_faces": len(artifact_faces),
        "generator_keys": sum(len(v) for v in generator_faces.values()),
        "artifact_keys": sum(len(v) for v in artifact_faces.values()),
        "artifacts_without_defaults": sum(1 for v in artifact_faces.values() if not v),
        "confd_roots_scanned": len(scanned_roots),
        "confd_roots_exempt_from_fifth_floor": len(exempt_roots),
        "confd_roots_watched_by_fifth_floor": len(scanned_roots - exempt_roots),
        "roots_only_the_fifth_floor_notices": blind_roots,
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
        # ⛔ NO TICKET HERE, on purpose. This banner used to say
        # "TRK-344 / #1392", which is the fourth face's ticket pair — right for
        # the five floors that face introduced and WRONG for every arm the fifth
        # floor added, and a breach of one of those sent the reader to a ticket
        # that says nothing about what fired. A banner cannot name the ticket of
        # a floor it does not know; the floor names its own, on the line above
        # this one. (Measured: of the module's `_GateViolation` sites, six spoke
        # for the fifth floor while this banner claimed the fourth face's pair.)
        print(
            "\n1 defaults-tier floor breach — the ❌ line above names the floor "
            "and its ticket. ⛔ NOTHING ELSE IN THIS MODULE RAN: not the "
            "TRK-337 reachability check, not the KNOWN_UNWIRED or "
            "NOT_CHART_ARMED exit-locks, not the placement checks, and no INFO. "
            "The floors are evaluated while the faces are built — before any "
            "check — precisely because an empty face passes every one of them "
            "vacuously. Fix this and re-run to see the rest.",
            file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK
    except Exception as exc:  # noqa: BLE001 — caller error, not a violation
        print(f"ERROR: reachability check crashed: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    errors = result["errors"]
    infos = result["infos"]
    stats = result["stats"]

    # Printed on every run that gets this far — a clean pass and a run with
    # violations both print it — because the defaults-tier coverage figure has
    # no home in a comment (it went stale twice there), and a reader who wants to
    # know how much this gate actually looks at should get today's number.
    # ⚠️ NOT on the floor-breach path, and the earlier "pass OR fail" here
    # overstated it: the floors are evaluated while the faces are built, so
    # `run_check` raises before returning and nothing below runs (measured: no
    # coverage line at all, and rc=1 UNDER `--ci` — without the flag the same
    # breach is report-only and returns rc=0, as the `except` clause above
    # spells out). That is the intended shape — a breached floor means the face
    # set itself is not trustworthy, and printing a coverage figure over it
    # would be reporting a measurement of a broken scan — but it is a "pass or
    # violation" line, not a "pass or fail" one.
    print(
        "INFO: defaults-tier coverage: "
        f"{stats['generator_faces']} generator face(s) / "
        f"{stats['generator_keys']} key(s), "
        f"{stats['artifact_faces']} artifact face(s) / "
        f"{stats['artifact_keys']} key(s), inspected at every depth of "
        f"`defaults:`; {stats['artifacts_without_defaults']} artifact(s) declare "
        "no `defaults:` section at all.", file=sys.stderr)

    # The fifth floor's own figure, re-derived rather than restated (#1411).
    # ⛔ The COUNT never travels alone. On its own it is the exact shape that
    # went stale here before: the claim that 11 of the 12 conf.d roots could go
    # to zero unseen was written into SIX places in one commit (`188cfb17`) and
    # rewritten wholesale, and a bare number carries no way to check it. ⚠️ Six,
    # and none of them spells it as the literal `11 of 12`. Two are in English —
    # the gate comment ("of the 12 distinct conf.d roots, 11") and the test file
    # ("11 of the 12 conf.d roots") — and four are the Chinese wording: the
    # CHANGELOG entry twice, the COMMIT SUBJECT, and the COMMIT BODY. The
    # subject and body are the copies `git log` shows, the only ones a reader
    # meets without opening a file. ⚠️ This count has now been wrong twice in
    # the same direction: an earlier wording said FOUR (missing the subject) and
    # its correction said FIVE (missing the body), and each time the sentence's
    # whole point was that a number written in many places is checked in none.
    # That earlier wording also put the figure in backticks, which sends anyone
    # checking to `git grep '11 of 12'` for a string that was never there.
    # FIVE things change the answer and all five are on the line — the
    # DENOMINATOR's population (watched = scanned − exempt), the grouping, the
    # scenario, the floor subset, and the exemption — so the reader can
    # re-derive it from what is printed. (The frozen measurements beside the
    # fifth floor's constant state the same conditions, and ① is reported over
    # BOTH of its populations rather than picking one: holding grouping,
    # scenario, floors and exemption fixed does not by itself settle whether the
    # denominator is `scanned` or `watched`, and choosing silently is how the
    # two halves of a fraction came to be counted on different sides of the
    # exemption in the first place.)
    #
    # ⛔ NUMERATOR AND DENOMINATOR SHARE A POPULATION, which cost a correction:
    # the numerator is post-exemption, so printing it over `scanned` was a
    # fraction whose two halves counted different sets. See the note on
    # `exempt_roots` in `run_check`.
    only_fifth = stats["roots_only_the_fifth_floor_notices"]
    print(
        f"INFO: defaults-tier fifth-floor coverage: {len(only_fifth)} of "
        f"{stats['confd_roots_watched_by_fifth_floor']} WATCHED conf.d root(s) "
        f"(grouped by `conf_d_root`; watched = {stats['confd_roots_scanned']} "
        "scanned roots holding a defaults artifact minus "
        f"{stats['confd_roots_exempt_from_fifth_floor']} exempt via "
        "_DEFAULTS_ROOTS_MAY_BE_EMPTY — the numerator is counted after that "
        "subtraction, so both halves of this fraction are the same population) "
        "would have their keys go to zero — files left in place — with all four "
        "earlier floors silent (both file floors, both per-class key floors, "
        "_SHIPPED_CONFD_ROOTS), i.e. only the fifth floor speaks for them: "
        f"{only_fifth}", file=sys.stderr)

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
