#!/usr/bin/env python3
"""
init_project.py — Bootstrap a Dynamic Alerting integration in a customer repo.

Generates:
  1. conf.d/ directory with _defaults.yaml + tenant stubs
  2. CI/CD pipeline (GitHub Actions / GitLab CI / both)
  3. Kustomize overlays for ConfigMap generation
  4. .pre-commit-config.yaml snippet for shift-left validation
  5. .da-init.yaml marker for upgrade detection

Usage:
  da-tools init                                   # Interactive mode
  da-tools init --ci github --tenants db-a,db-b   # Non-interactive
  da-tools init --ci both --rule-packs mariadb,redis --deploy kustomize
"""
from __future__ import annotations

import argparse
import os
import sys
import textwrap
from datetime import datetime, timezone
from typing import Optional

from pathlib import Path

import yaml

_THIS_DIR = Path(__file__).resolve().parent

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, str(_THIS_DIR))  # Docker flat layout
sys.path.insert(0, str(_THIS_DIR.parent))  # Repo subdir layout
from _lib_python import detect_cli_lang, write_text_secure  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
# #1310 — the declared-without-value key names shipped in `optional_overrides:`.
# Same import shape as scaffold_tenant.py's (see its comment): the derivation is
# shared so the two customer-side `_defaults.yaml` producers cannot disagree
# about which keys a tenant may set.
#
# ⛔ SCOPE of the sharing, deliberately narrow: the DECLARED-KEY list and the
# `<base>_critical` SUFFIX constant (`CRITICAL_SUFFIX`, #1218) — both are
# contract shape, not content. `RULE_PACK_CATALOG` below stays this module's own
# copy of the `defaults:` VALUES — that copy is a known divergence from
# `scaffold_tenant.RULE_PACKS` (different key spellings, e.g. db2_log_usage vs
# db2_log_usage_percent) whose consolidation is tracked separately. This change
# deliberately does NOT start that merge, and must not be read as a first
# instalment of it: splitting the consolidation into stages is explicitly warned
# against, because a half-merged contract is a fourth contract.
from _registry_lib import (  # noqa: E402
    CRITICAL_SUFFIX,
    annotate_defaults_counterexamples,
    append_tenant_declared_stub,
    render_tenant_critical_note_lines,
    render_tenant_declared_note_lines,
    shipped_optional_keys_for_packs,
)

_LANG = detect_cli_lang()

# ============================================================
# Bilingual help strings
# ============================================================
_HELP = {
    'description': {
        'zh': '在客戶 repo 中初始化 Dynamic Alerting 整合骨架',
        'en': 'Bootstrap a Dynamic Alerting integration in your repository',
    },
    'ci': {
        'zh': 'CI/CD 平台: github, gitlab, both (預設: both)',
        'en': 'CI/CD platform: github, gitlab, both (default: both)',
    },
    'tenants': {
        'zh': '逗號分隔的租戶名稱 (例如 db-a,db-b)',
        'en': 'Comma-separated tenant names (e.g., db-a,db-b)',
    },
    'rule_packs': {
        'zh': '逗號分隔的 Rule Pack (例如 mariadb,redis,kubernetes)',
        'en': 'Comma-separated Rule Packs (e.g., mariadb,redis,kubernetes)',
    },
    'deploy': {
        'zh': '部署方式: kustomize, helm, argocd (預設: kustomize)',
        'en': 'Deployment method: kustomize, helm, argocd (default: kustomize)',
    },
    'output_dir': {
        'zh': '輸出根目錄 (預設: 當前目錄)',
        'en': 'Output root directory (default: current directory)',
    },
    'non_interactive': {
        'zh': '跳過互動提示',
        'en': 'Skip interactive prompts',
    },
    'namespace': {
        'zh': 'Kubernetes monitoring namespace (預設: monitoring)',
        'en': 'Kubernetes monitoring namespace (default: monitoring)',
    },
    'da_tools_image': {
        'zh': 'da-tools Docker image (預設: ghcr.io/vencil/da-tools:latest)',
        'en': 'da-tools Docker image (default: ghcr.io/vencil/da-tools:latest)',
    },
    'config_source': {
        'zh': '配置來源: configmap (預設) 或 git (git-sync sidecar 模式)',
        'en': 'Config source: configmap (default) or git (git-sync sidecar mode)',
    },
    'git_repo': {
        'zh': 'Git 倉庫 URL (--config-source git 時必填)',
        'en': 'Git repository URL (required when --config-source git)',
    },
    'git_branch': {
        'zh': 'Git 分支 (預設: main)',
        'en': 'Git branch (default: main)',
    },
    'git_path': {
        'zh': 'Git 倉庫中 conf.d/ 的路徑 (預設: conf.d)',
        'en': 'Path to conf.d/ inside the git repo (default: conf.d)',
    },
    'git_period': {
        'zh': 'git-sync 同步間隔秒數 (預設: 60)',
        'en': 'git-sync poll interval in seconds (default: 60)',
    },
    'epilog': {
        'zh': '''範例:
  %(prog)s                                                    # 互動模式
  %(prog)s --ci github --tenants db-a,db-b                    # GitHub Actions
  %(prog)s --ci both --rule-packs mariadb,redis --deploy kustomize
  %(prog)s --ci gitlab --tenants prod-db --deploy helm -o /path/to/repo''',
        'en': '''Examples:
  %(prog)s                                                    # Interactive mode
  %(prog)s --ci github --tenants db-a,db-b                    # GitHub Actions
  %(prog)s --ci both --rule-packs mariadb,redis --deploy kustomize
  %(prog)s --ci gitlab --tenants prod-db --deploy helm -o /path/to/repo''',
    },
}


# ============================================================
# Bilingual Helper & Validation Functions
# ============================================================

def _h(key: str) -> str:
    return _HELP[key].get(_LANG, _HELP[key]['en'])


# ============================================================
# Customer-delivered container image pins (#1337 ②④)
# ============================================================
# Every ref below is emitted into a file the CUSTOMER runs: the GitLab CI
# apply stage carries `environment: name: production` plus cluster-write
# credentials, and the git-sync patch is applied straight into their cluster.
#
# ⛔ Pinned to a concrete VERSION TAG, deliberately NOT a digest. The
# customer's repo has no updater of ours to re-resolve a digest, and an
# un-maintained digest pin inhibits security updates — OpenSSF Scorecard says
# exactly that inside the Pinned-Dependencies check that asks for pinning.
# Every upstream tool that generates config for someone else makes the same
# call: `helm create` emits a tag via `.Chart.AppVersion`, GitLab's own
# shipped CI templates emit `${AUTO_DEPLOY_IMAGE_VERSION}`, Argo CD's
# install.yaml emits `quay.io/argoproj/argocd:vX.Y.Z`. Contrast
# `scripts/tools/lint/check_iac_helm.py`, which DOES pin its fallback images
# by digest — those run in OUR CI, where we own the bump loop and a test
# enforces the digest (tests/lint/test_fallback_image_digest_pin.py).
#
# ⛔ On bump, confirm the ref resolves anonymously BEFORE committing:
#     docker manifest inspect <ref>
# A ref that 404s here is a broken pipeline in someone else's repo.

# Chosen for what it CONTAINS, not only for how it can be pinned. Measured
# (`docker run --entrypoint sh`): a shell, `kubectl` v1.34.9, and `kustomize`
# v5.8.1. All three are load-bearing — the apply stage is a GitLab `script:`
# block, which the runner executes through a shell inside this image, and the
# first line of that block invokes standalone `kustomize`.
# ⛔ Two images were rejected on measurement, not on preference:
#   * `bitnami/kubectl` is no longer pinnable at all — Broadcom moved the
#     versioned catalog behind a subscription and deleted the free version tags
#     on 2025-09-29, leaving only `latest` (their docs call it dev-only). It
#     also ships no `kustomize`, so this job never actually worked.
#   * `registry.k8s.io/kubectl` is the Kubernetes project's own image and has
#     no `latest` tag, which is attractive — but it is distroless-static: NO
#     shell at all, so a GitLab `script:` block cannot run in it under any
#     entrypoint override. Pinnability is worthless if the job cannot start.
# `alpine/k8s` also publishes no `latest` tag (verified 404), so the
# floating-reference argument survives the swap. Same publisher as
# GITLAB_HELM_IMAGE below — one trust decision, not two.
# ⚠️ kubectl supports ±1 minor of skew from the cluster — override
# DA_KUBECTL_IMAGE if the customer's control plane sits further back.
GITLAB_KUBECTL_IMAGE = 'alpine/k8s:1.34.9'

# ⛔ Held on the Helm 3 line ON PURPOSE — pinned by
# `test_helm_image_stays_on_the_helm_3_line`, because a comment alone did not
# stop this: `alpine/helm:latest` now resolves to Helm 4.x (4.0.0 GA'd
# 2025-11-12; `latest` and `4.2.3` share a digest), and Helm 4 ships "backward
# incompatible changes including to the flags and output of the Helm CLI", so
# the previous floating tag walked existing customer pipelines across a major
# boundary with no change on their side. A future "just bump it" would do the
# same thing deliberately; the test is what makes that a decision instead of an
# accident. The image's own README also says not to use `latest` in production.
# ⓘ Measured: ships a shell, but `ENTRYPOINT ["helm"]` — see the
# `entrypoint: [""]` override in the emitted job, without which the job dies
# before its first script line.
GITLAB_HELM_IMAGE = 'alpine/helm:3.21.3'

# quay.io, NOT Docker Hub. `argoproj/argocd` on Docker Hub was last pushed
# 2022-01-21 and carries no tag beyond v2.6.15, so a pipeline built on it runs
# a CLI years out of step with the customer's Argo CD server. quay.io is the
# registry Argo CD's own install manifests use.
# ⓘ Argo CD's CI guidance is that the CLI should track the server, so two
# alternatives are worth offering: download it from the customer's own server
# (`https://$ARGOCD_SERVER/download/argocd-linux-amd64`), or enable auto-sync
# and drop this stage entirely.
GITLAB_ARGOCD_IMAGE = 'quay.io/argoproj/argocd:v3.5.0'

# registry.k8s.io/git-sync publishes ONLY exact patch tags (no `latest`, no
# `v4`), so a consumer is forced to name a version. The previous pin, v4.4.0
# (2024-12-13), was 8 releases and ~19 months behind when this was caught: it
# misses CVE-2025-30204 (High, reached transitively through golang-jwt and
# fixed in v4.4.1) plus every base-image rebuild from v4.4.3 onward. git-sync
# publishes no per-project advisory feed, so a stale pin here emits no signal
# whatsoever — the staleness is only ever found by looking.
GIT_SYNC_IMAGE = 'registry.k8s.io/git-sync/git-sync:v4.7.1'

# The tool image every generated project runs, and the only ref that reaches
# 100% of them: it lands in the GitHub workflow `env`, in the GitLab `variables`
# block, AND in `.pre-commit-config.da.yaml` as a `language: docker_image` entry,
# so it executes on developer workstations as well as in the customer's CI.
#
# ⛔ FLOATING ON PURPOSE, and that is a different decision from the four
# third-party pins. A customer should get the current published tool without us
# shipping them a new project, so the tag is mutable and we are the ones who move
# it. That also makes it the one delivered ref where OUR lever reaches EXISTING
# customers — which is why it belongs in the delivered scan face even though the
# floating-shape guard deliberately exempts it.
#
# ⛔ A CONSTANT, not two literals. It used to be spelled out at the defaults dict
# and again at the argparse default, which is exactly the "a ref exists in
# exactly two places" contract this module documents for the pins below being
# quietly violated for the most-executed image of the set.
DA_TOOLS_IMAGE = 'ghcr.io/vencil/da-tools:latest'

# deploy method -> (GitLab CI variable name, pinned ref). Single source for
# BOTH the `variables:` block and the apply stage's `image:` line, so the two
# cannot drift apart.
_GITLAB_APPLY_IMAGES = {
    'kustomize': ('DA_KUBECTL_IMAGE', GITLAB_KUBECTL_IMAGE),
    'helm': ('DA_HELM_IMAGE', GITLAB_HELM_IMAGE),
    'argocd': ('DA_ARGOCD_IMAGE', GITLAB_ARGOCD_IMAGE),
}


def _gitlab_apply_image(deploy_method: str) -> tuple[str, str]:
    """(variable name, pinned ref) for the apply stage's runner image.

    Mirrors _build_gitlab_apply_stage's branching *including its fallback*:
    an unrecognised deploy method lands in the argocd branch there, so it has
    to land there here too.

    ⚠️ The failure mode is a TOOL/COMMAND MISMATCH, not an undeclared variable.
    Both the `variables:` entry and the job's `image:` read this one function,
    so the name is always declared and always referenced — what diverging
    fallbacks would produce is `argocd app sync` running inside the kubectl
    image, i.e. a production deploy that starts and then dies on a missing
    binary. `test_apply_image_matches_the_command_it_runs` is the guard, and it
    has to re-derive the branch from the rendered script text; comparing the
    two sides of this lookup to each other proves nothing.
    """
    return _GITLAB_APPLY_IMAGES.get(deploy_method, _GITLAB_APPLY_IMAGES['argocd'])


# ============================================================
# Rule Pack catalog (metric keys per rule pack)
# ============================================================
# TWO TIERS, and the split is not cosmetic — it is where the key ends up in the
# generated files, which is what decides whether it produces anything (#1218 /
# TRK-344):
#
#   'defaults'           → the `defaults:` section of `_defaults.yaml`.
#                          `resolveBaseRows` walks that map, so every key here
#                          emits `user_threshold{...,severity="warning"}` for
#                          every tenant.
#   'critical_overrides' → the `<tenant>.yaml` stub. `resolveCriticalRows`
#                          iterates TENANT OVERRIDES only and admits on
#                          `defaults[<base>]`, so this is the ONLY section in
#                          which a `<base>_critical` key does anything.
#
# ⛔ A `<base>_critical` written into 'defaults' does not "also work, just less
# neatly" — it silently becomes a DIFFERENT metric. `parseMetricKey` splits on
# the first underscore, so `pg_connections_critical` resolves to
# `{component="pg", metric="connections_critical", severity="warning"}`: a series
# no recording rule joins, while `tenant:alert_threshold:pg_connections_critical`
# (which reads `severity="critical"`) stays empty and the `*Critical` alert
# cannot fire. Measured both directions in
# `components/threshold-exporter/app/pkg/config/critical_tier_placement_test.go`.
# The placement rule is enforced for every `_defaults.yaml` producer by
# `check_threshold_reachability.py`'s defaults-tier face, so a key put in the
# wrong dict below fails CI rather than shipping.
RULE_PACK_CATALOG = {
    'mariadb': {
        'label': 'MariaDB / MySQL',
        'defaults': {
            'mysql_connections': 80,
            'mysql_threads_running': 30,  # running-thread saturation warning (NOT host CPU%); 80→30 PMM/Nichter (#944); renamed from mysql_cpu (#1231)
            'mysql_slow_queries': 10,
            'mysql_replication_lag': 30,
            'mysql_aborted_connections': 50,
            'mysql_table_locks_waited': 100,
        },
        'critical_overrides': {
            'mysql_connections_critical': 150,
            # ⛔ #951 added this key "for parity" and put it under `defaults:`,
            # where it could not do the job the commit message claimed it did.
            # 30/50 is the saturation pair from #944 (PMM pt-osc, Nichter); it
            # only ever reaches MariaDBHighThreadsRunningCritical from here.
            'mysql_threads_running_critical': 50,
            'mysql_replication_lag_critical': 120,
        },
    },
    'postgresql': {
        'label': 'PostgreSQL',
        'defaults': {
            'pg_connections': 80,
            'pg_replication_lag': 30,
            'pg_cache_hit_ratio': 95,
            'pg_deadlocks': 5,
            'pg_long_queries': 300,
        },
        'critical_overrides': {
            'pg_connections_critical': 150,
            'pg_replication_lag_critical': 120,
        },
    },
    'redis': {
        'label': 'Redis',
        'defaults': {
            'redis_memory_usage': 80,
            'redis_connected_clients': 500,
            'redis_evicted_keys': 100,
            # redis_keyspace_misses_ratio removed (#1196 E): supply-side orphan —
            # no alert consumes it, and this catalog's 50 vs the registry's 0.3
            # was a third hand-copied unit universe (ratio vs percent).
        },
        'critical_overrides': {
            'redis_memory_usage_critical': 95,
        },
    },
    'mongodb': {
        'label': 'MongoDB',
        'defaults': {
            'mongodb_connections': 80,
            'mongodb_replication_lag': 10,
            'mongodb_opcounters': 10000,
            'mongodb_page_faults': 100,
        },
        'critical_overrides': {
            'mongodb_connections_critical': 150,
        },
    },
    'elasticsearch': {
        'label': 'Elasticsearch',
        'defaults': {
            'es_heap_usage': 80,
            'es_cluster_status': 1,
            'es_pending_tasks': 50,
            'es_query_latency': 500,
            'es_indexing_latency': 200,
        },
        'critical_overrides': {
            'es_heap_usage_critical': 90,
        },
    },
    'oracle': {
        'label': 'Oracle',
        'defaults': {
            'oracle_tablespace_used_percent': 85,
            'oracle_active_sessions': 100,
            'oracle_blocking_sessions': 5,
        },
        'critical_overrides': {
            'oracle_tablespace_used_percent_critical': 95,
        },
    },
    'db2': {
        'label': 'IBM DB2',
        'defaults': {
            'db2_connections': 80,
            'db2_lock_waits': 50,
            'db2_tablespace_usage': 85,
            'db2_log_usage': 80,
        },
    },
    'clickhouse': {
        'label': 'ClickHouse',
        'defaults': {
            'clickhouse_queries': 100,
            'clickhouse_merge_latency': 300,
            'clickhouse_replication_lag': 30,
            'clickhouse_memory_usage': 80,
        },
    },
    'kafka': {
        'label': 'Apache Kafka',
        'defaults': {
            'kafka_consumer_lag': 10000,
            'kafka_under_replicated_partitions': 0,
            'kafka_active_controllers': 1,
            'kafka_offline_partitions': 0,
        },
        'critical_overrides': {
            'kafka_consumer_lag_critical': 50000,
        },
    },
    'rabbitmq': {
        'label': 'RabbitMQ',
        'defaults': {
            'rabbitmq_queue_messages': 10000,
            'rabbitmq_consumers': 1,
            'rabbitmq_unacked_messages': 5000,
            'rabbitmq_memory_usage': 80,
        },
        'critical_overrides': {
            'rabbitmq_queue_messages_critical': 50000,
        },
    },
    'jvm': {
        'label': 'JVM Applications',
        'defaults': {
            'jvm_heap_usage': 80,
            'jvm_gc_pause': 500,
            'jvm_threads': 500,
        },
        'critical_overrides': {
            'jvm_heap_usage_critical': 95,
        },
    },
    'nginx': {
        'label': 'Nginx',
        'defaults': {
            'nginx_error_rate': 5,
            'nginx_request_latency_p99': 1000,
            'nginx_active_connections': 1000,
        },
        'critical_overrides': {
            'nginx_error_rate_critical': 15,
        },
    },
    'kubernetes': {
        'label': 'Kubernetes',
        'defaults': {
            'container_cpu': 80,
            'container_cpu_throttle': 25,  # chronic CFS throttle: % of ACTIVE periods throttled (#944 PR-2c)
            'container_memory': 85,
        },
        'critical_overrides': {
            'container_cpu_critical': 95,
            'container_cpu_throttle_critical': 50,
            'container_memory_critical': 95,
        },
    },
    'operational': {
        'label': 'Operational (auto-enabled)',
        'auto_enabled': True,
        'defaults': {},
    },
    'platform': {
        'label': 'Platform Self-Monitoring (auto-enabled)',
        'auto_enabled': True,
        'defaults': {},
    },
}

# ============================================================
# Template generators
# ============================================================


def _catalog_defaults(rule_packs: list[str]) -> dict:
    """The `defaults:` mapping this run will write into `_defaults.yaml`.

    ONE derivation, consumed by both files a run produces: `_gen_defaults_yaml`
    dumps it, and `_gen_tenant_yaml` reads it to decide what its header may
    claim about `<base>_critical` (#1321). Re-spelling the loop in the second
    caller would recreate exactly the drift this fix exists to remove — the
    tenant header would then be describing a `defaults:` section that is only
    assumed to match the one on disk.

    ⛔ It passes the merged mapping through UNFILTERED, and that is deliberate
    (#1218 blind review). An earlier draft dropped any `_critical` key here as
    "belt-and-braces". It was the opposite: `check_threshold_reachability`'s
    defaults-tier face reads THIS function's rendered output, so the filter made
    the only gate that polices the placement structurally unable to fire, while
    a comment claimed it still would. A misfiled key would then have vanished
    from BOTH generated files — no warning row, no critical row, no gate — which
    is strictly worse than the bug being fixed. A misfile now flows through to
    `defaults:`, where the gate names it and CI fails.
    """
    defaults: dict = {}
    for rp in rule_packs:
        if rp in RULE_PACK_CATALOG:
            defaults.update(RULE_PACK_CATALOG[rp]['defaults'])
    return defaults


def _catalog_critical(rule_packs: list[str]) -> dict:
    """The `<base>_critical` mapping this run will seed into `<tenant>.yaml`.

    Sibling of `_catalog_defaults`, and the reason the pair exists: these keys
    are worthless in `_defaults.yaml` and load-bearing in the tenant file, so
    the two generators must not read one mapping and hope. Iteration follows the
    CALLER's pack order, so `--rule-packs a,b` and `b,a` seed the same keys in a
    different order (measured — the rendered stubs are not byte-identical).

    Keys whose base is not in the same run's `defaults:` are dropped rather
    than emitted: `resolveCriticalRows` admits on `defaults[<base>]`, so such a
    key produces nothing and `ValidateTenantKeys` rejects the tenant write
    outright (a dangling `_critical` is an Error, not a warning, since #1227).
    Seeding a stub that the only supported writer refuses is worse than seeding
    nothing. Today every catalog `_critical` has its base in the same pack, so
    this drops nothing — it is a guard against a pack losing a base key later.
    """
    defaults = _catalog_defaults(rule_packs)
    critical: dict = {}
    for rp in rule_packs:
        if rp not in RULE_PACK_CATALOG:
            continue
        for key, value in RULE_PACK_CATALOG[rp].get('critical_overrides', {}).items():
            # Suffix-guard before slicing: a key without `_critical` would have
            # nine arbitrary characters chopped off it, and the membership test
            # below would then pass or fail for a reason unrelated to this key.
            # `test_catalog_tiers_do_not_overlap_and_are_suffix_correct` pins the
            # shipped catalog, so this is defensive only — but a wrong-for-the-
            # wrong-reason branch is exactly what the rest of this fix is about.
            if not key.endswith(CRITICAL_SUFFIX):
                continue
            base = key[: -len(CRITICAL_SUFFIX)]
            if base in defaults:
                critical[key] = value
    return critical


def _critical_prefill_note(critical: dict) -> str:
    """The sentences `render_tenant_critical_note_lines` cannot say for us.

    That renderer is shared with `scaffold_tenant`, and it describes the
    `_defaults.yaml` regime ("the platform ships no critical value; set one
    here"). True for both generators — but only THIS one also pre-fills them,
    and a tenant told to "set one here" while N are already set below would go
    looking for a section it has. Kept local rather than adding a third regime
    to the shared table: the difference being described is this generator's
    behaviour, not a property of the `_defaults.yaml` it writes.

    ⛔ It says "already set below", not "the platform supplies these" — the
    sentence above it (correctly) says the platform supplies no critical value,
    and a follow-up that read like a platform assertion would contradict it
    (blind review, #1218).

    ⛔ And it does NOT say "a later platform recalibration cannot reach these
    copies" full stop, which an earlier draft did. That sentence is true and
    misleading in the same breath: stated about the `_critical` lines alone it
    implies the sibling `defaults:` keys DO keep flowing, and for this
    generator's customers they do not — nothing in the generated tree
    references the chart's `thresholdConfig` values (measured across all three
    `--deploy` values). The pre-fill's marginal freeze is therefore zero.

    ⛔ Nor does it name the ConfigMap wiring, which an earlier draft also did
    ("this tool wires conf.d/ straight into the threshold-config ConfigMap").
    That mechanism only exists for `--deploy kustomize`: `--deploy helm` and
    `--deploy argocd` generate no `kustomize/` tree at all (measured — the
    output is `.da-init.yaml`, two CI files, `.pre-commit-config.da.yaml` and
    `conf.d/`, nothing else), and `_gen_tenant_yaml` never receives `deploy`,
    so a deploy-specific sentence here cannot be true for two of the three.
    The claim was narrowed to the property that holds for all three: nothing
    generated tracks the platform (blind review, #1218).

    ⛔ The costs it DOES carry are the ones a tenant cannot infer, and every
    one of them is measured against the real resolver, not read off it:
      * `<base>: "disable"` does not cascade to `<base>_critical`.
        `resolveCriticalRows` tests the `_critical` override's own value and
        `defaults[<base>]`'s existence, never the base override — measured:
        base disabled + critical pre-filled yields exactly one row,
        `{metric="connections", severity="critical"}`, with `ValidateTenantKeys`
        silent. Before the pre-fill this could bite at most one accidental key;
        now it is every critical key of every selected pack.
      * …and DELETE is not a synonym for disable, which an earlier draft
        offered as one ("Disable or delete BOTH lines"). Measured, same three
        cases: both disabled → 0 rows; both deleted → 1 row,
        `{severity="warning", value=<the platform default>}`, because an absent
        key is State 2. Telling a tenant who wants the metric off to delete
        both lines hands them back the warning tier they were trying to remove.
      * A dangling `<base>_critical` is write-blocking on the tenant-api path
        (`ValidateTenantKeys` blocking `Errors` → `gitops/writer.go:599`
        `keyErrs`, the set every write gate turns into `ErrValidation`).
        ⛔ The note names the tenant-api path as the ONLY blocker and says a
        green pipeline proves nothing — deliberately WITHOUT describing the
        state of the generated CI, which two earlier drafts did in opposite
        directions and both were one merge away from being false. Draft 1
        promised "EVERY write of this file is rejected" (a stop the customer's
        own pipeline does not perform). Draft 2 corrected it by saying that
        pipeline "fails before it validates anything" — true only while the
        generated invocations passed `--ci`, which `validate_config.py` does not
        accept (#1380).

        ⛔ And that is no longer even a hypothetical: #1347 merged as PR #1390
        (`b016f77e`) WHILE THIS PR WAS IN REVIEW, removing `--ci` from the three
        `validate-config` invocations (measured: the generated tree still
        contains three `--ci`, all of them on `da-tools lint`, which does accept
        the flag — saying "all three invocations" without the qualifier reads as
        "all of them"). Draft 2 would now be describing a defect that no
        longer exists, in a file customers keep. The reasoning paragraph itself
        went stale the same way the sentence would have — which is the strongest
        argument available for the rule it states.

        What survives every regime is the invariant: bare `validate-config`
        WARNs and exits 0 on this input (measured, `--strict` included), so its
        verdict never establishes that a dangling `_critical` is gone. A
        customer-facing file must carry the invariant, not the current state of
        a neighbouring defect.

    ⛔ It also no longer says the values are "at the platform's suggested
    starting values" — three lines under a paragraph that (correctly) states
    the platform supplies no critical value, that reads as exactly the platform
    assertion the docstring above forbids.

    Derived from the mapping actually seeded (same rule as its neighbours): an
    empty tier renders nothing at all, so the file never points at an absent
    section.
    """
    if not critical:
        return ''
    return (
        '\n# {n} of them are written in below, as a starting point. The lines\n'
        '# are in THIS file, so they are yours: retune them freely. Like every\n'
        '# key in the sibling _defaults.yaml, they are a one-time copy into\n'
        '# your repo — nothing generated here tracks the platform. Picking up\n'
        '# a later upstream recalibration means re-running this tool with\n'
        '# `--force`, which REWRITES both files and discards every hand edit\n'
        '# in them, so diff before you keep the result.\n'
        '#\n'
        '# Three things editing alone will not tell you:\n'
        '#   * `<base>: "disable"` does NOT disable its `<base>_critical`\n'
        '#     twin. The warning row goes away and the critical row keeps\n'
        '#     firing, with no warning tier beneath it and no validation\n'
        '#     message anywhere. BOTH lines must say "disable".\n'
        '#   * …and most `<base>` lines are NOT in this file. The illustrative\n'
        '#     base overrides above come from the FIRST selected pack only,\n'
        '#     while the critical tier is seeded for every selected pack — so a\n'
        '#     `<base>_critical` here often has no twin to edit. To silence one\n'
        '#     of those, ADD the `<base>: "disable"` line yourself; omitting it\n'
        '#     means "inherit the platform value", not "off".\n'
        '#   * DELETING both lines is not the same thing: an absent key falls\n'
        '#     back to the platform value, so the warning row returns at the\n'
        '#     _defaults.yaml number. Delete only suppresses the critical row.\n'
        '#   * If a `<base>` ever leaves `defaults:`, its `<base>_critical`\n'
        '#     line here must go too. ONLY the tenant-api write path treats a\n'
        '#     dangling one as blocking, and it rejects THIS WHOLE FILE —\n'
        '#     every other change in the same save with it. `da-tools\n'
        '#     validate-config` reports it as a WARNING and exits 0, so its\n'
        '#     verdict never establishes that this is fixed.'
    ).format(n=len(critical))


def _gen_defaults_yaml(rule_packs: list[str], namespace: str) -> str:
    """Generate _defaults.yaml with selected rule pack defaults.

    Also emits the `optional_overrides:` DECLARED-KEY list (#1310) — key names
    only, no values: the platform recognises them, the tenant supplies the
    number. Without it in THIS file, a tenant on the GitOps topology gets an
    HTTP 400 for every one of those keys, because tenant-api's `--config-dir`
    is the customer repo clone and `config.mergeTenantConfig` reads its
    `_defaults.yaml` for `OptionalOverrides` (the chart-side list only ever
    reaches threshold-exporter).
    """
    defaults = _catalog_defaults(rule_packs)

    # DERIVED from the shared predicate, never listed here — see the import
    # comment for why this one derivation is shared while the values above are
    # not. Pack names are the registry's; any catalog-only name (`operational`,
    # `platform`) simply contributes nothing.
    optional_overrides = shipped_optional_keys_for_packs(rule_packs)

    state_filters = {
        'container_crashloop': {
            'reasons': ['CrashLoopBackOff'],
            'severity': 'critical',
        },
        'container_imagepull': {
            'reasons': ['ImagePullBackOff', 'InvalidImageName'],
            'severity': 'warning',
        },
        'maintenance': {
            'reasons': [],
            'severity': 'info',
            'default_state': 'disable',
        },
    }

    routing_defaults = {
        'receiver': {
            'type': 'webhook',
            'url': 'https://your-webhook-endpoint.example.com/alerts',
        },
        'group_by': ['alertname', 'tenant'],
        'group_wait': '30s',
        'group_interval': '5m',
        'repeat_interval': '4h',
    }

    config = {'defaults': defaults}
    if optional_overrides:
        # Omitted when empty rather than emitted as `[]` (an empty list reads
        # as an accident; the Go loader treats absent and empty the same).
        config['optional_overrides'] = optional_overrides
    config['state_filters'] = state_filters
    config['_routing_defaults'] = routing_defaults

    header = textwrap.dedent("""\
    # _defaults.yaml — Platform global defaults
    # Managed by Platform Team. Tenant files should NOT contain this section.
    #
    # Three-state logic — for the keys under `defaults:` below, which are the
    # only ones that HAVE a platform value to fall back to:
    #   - Custom value:  metric_key: 42     → Override platform default
    #   - Omitted:       (not in tenant YAML) → Use this default
    #   - Disable:       metric_key: "disable" → Suppress metric entirely
    #
    # optional_overrides (if present): key NAMES only, no values. The platform
    # RECOGNISES these keys — a tenant may set them in its own file and they
    # take effect — but asserts no value of its own, so an unset key stays
    # silent. That silence is the intended end state, not a missing default:
    # these are thresholds only the tenant's own baseline can calibrate.
    # Do NOT move one into `defaults:` to "fix" the silence — that arms a
    # platform-chosen number for every tenant.
    #
    # Generated by: da-tools init
    # Rule Packs: {rule_packs}
    """).format(rule_packs=', '.join(rule_packs))

    # ⛔ Same annotation as `scaffold_tenant.write_outputs`, from the same
    # composer. This is the SECOND producer of a file called `_defaults.yaml`
    # in the same `conf.d/`: it derives its declared list from the same shared
    # predicate, so it lists exactly the keys ADR-030's reference library has
    # counter-examples for. Without this, `da-tools init` wrote the caveat into
    # `<tenant>.yaml` (which shares the stub renderer) and left the sibling
    # `_defaults.yaml` bare — one annotated file and one not, side by side
    # (blind review, #1344). English, because everything this tool writes is.
    return header + annotate_defaults_counterexamples(
        yaml.dump(config, default_flow_style=False, allow_unicode=True,
                  sort_keys=False),
        lang="en")


def _gen_tenant_yaml(tenant: str, rule_packs: list[str]) -> str:
    """Generate a tenant stub YAML.

    ⛔ This is the ONE file a tenant opens, so the header has to be true for
    EVERY population of key (#1321). It used to say "Omitted keys inherit from
    _defaults.yaml", which holds only for keys `_defaults.yaml` gives a value
    to; for the DECLARED tier (`optional_overrides:` — key names, no values) it
    is exactly backwards: there is nothing to inherit, so omission is silence.
    The declared keys for the selected packs are appended as a commented,
    valueless block — see `_registry_lib.append_tenant_declared_stub`.

    ⛔ Two buckets is still not the whole taxonomy: `<base>_critical` is in
    neither block, yet it takes effect whenever `<base>` has a value under
    `defaults:` (resolveCriticalRows keys off exactly that) — AND ONLY when it
    is written HERE. Until #1218 this generator put 16 of them into `defaults:`
    instead, where they emitted `{component="<prefix>",
    metric="<rest>_critical"}` (parseMetricKey splits on the first underscore) warning
    series and no critical tier at all; the header rendered from that mapping
    then told the tenant "the critical row fires without you doing anything",
    which was the opposite of what shipped. The paragraph is still rendered
    from the very `defaults:` mapping this run writes
    (`_registry_lib.render_tenant_critical_note_lines`) — that is what made the
    regime flip when the keys moved, instead of leaving a stale sentence.

    ⛔ The pointer at the declared block ("listed at the end of this file") is
    derived for the same reason. The block is appended only when the declared
    list is non-empty, and most packs' optional tier is `_critical`-only
    (measured: mariadb, postgresql, redis, mongodb, elasticsearch, kafka,
    rabbitmq, jvm, nginx, kubernetes all yield []), so `da-tools init
    --rule-packs mariadb` used to hand the tenant a header pointing at a
    section that file does not contain — see
    `_registry_lib.render_tenant_declared_note_lines`.
    """
    # The mapping `_gen_defaults_yaml` will dump for this same run — the header
    # claims below are read off it, not asserted about it. Same for the declared
    # list: one derivation per run, shared by the header sentence and the block
    # appended at the bottom, so the two cannot disagree.
    defaults = _catalog_defaults(rule_packs)
    critical = _catalog_critical(rule_packs)
    declared_keys = shipped_optional_keys_for_packs(rule_packs)
    critical_note = '\n'.join(render_tenant_critical_note_lines(defaults, lang='en'))
    critical_note += _critical_prefill_note(critical)
    declared_note = '\n'.join(
        render_tenant_declared_note_lines(declared_keys, lang='en'))
    header = textwrap.dedent("""\
    # {tenant}.yaml — Tenant threshold overrides
    # Only the 'tenants' section is allowed in tenant files.
    # Omitting a key that _defaults.yaml gives a value to (its `defaults:`
    # section) inherits that value.
    {declared_note}
    {critical_note}
    # Set a key to "disable" to suppress that metric.
    #
    # Generated by: da-tools init
    """).format(tenant=tenant, declared_note=declared_note,
                critical_note=critical_note)

    tenant_config: dict = {}

    # Add a few example overrides from the first rule pack. Reads the BASE tier
    # only — before #1218 this slice ran over a mapping that still held the
    # `_critical` keys, so `--rule-packs mariadb` spent one of its three
    # illustrative lines on `mysql_connections_critical` for whichever pack
    # happened to be first, and on nothing for every other pack.
    if rule_packs and rule_packs[0] in RULE_PACK_CATALOG:
        pack_defaults = _catalog_defaults([rule_packs[0]])
        for k in list(pack_defaults)[:3]:
            tenant_config[k] = str(pack_defaults[k])

    # The critical tier, for EVERY selected pack — not just the first. This is
    # the section the keys had to move to (#1218): `resolveCriticalRows`
    # iterates tenant overrides, so a `<base>_critical` anywhere else produces
    # no critical row. Seeded with a value rather than commented out because
    # that is what the platform's number is FOR, and it is the same position
    # `scaffold_tenant` takes (its prompt offers the registry value as the
    # Enter-default, and `generate_profile` writes the `_critical` twins while
    # deliberately dropping the flat declared keys). The customer edits or
    # deletes them in review — they are in the customer's own file, which is
    # exactly the tier boundary `defaults:` violated.
    for k, v in critical.items():
        tenant_config[k] = str(v)

    # Add routing stub
    tenant_config['_routing'] = {
        'receiver': {
            'type': 'webhook',
            'url': f'https://webhook.{tenant}.example.com/alerts',
        },
    }

    config = {'tenants': {tenant: tenant_config}}
    body = yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # Same derivation, same argument, same call as `_gen_defaults_yaml` above —
    # the two files are generated side by side from one `rule_packs`, so the
    # stub can only ever advertise keys that file actually declares. And it is
    # the SAME `declared_keys` the header sentence above was rendered from, so
    # "listed at the end of this file" is a fact about this string, not a hope.
    return append_tenant_declared_stub(header + body, declared_keys, lang='en')


# ============================================================
# CI/CD Pipeline Generators (GitHub Actions / GitLab CI)
# ============================================================

def _build_github_apply_stage(deploy_method: str, namespace: str) -> str:
    """Build GitHub Actions apply stage, as a COLUMN-0 block.

    ⛔ Contract with _gen_github_actions: every branch below returns a block
    whose top-level key (``apply:``) sits at column 0 after ``dedent``. It is
    the CALLER that indents it into ``jobs:`` (``textwrap.indent``), never this
    function and never the literal indentation of these templates. Keep the
    branches dedent-to-column-0; if you want the block nested, change the
    caller.

    ⛔ Second contract — ``needs:`` must NOT list ``generate`` (#1356). Every
    branch below runs only on ``workflow_dispatch``, while ``generate`` runs
    only on ``pull_request``. GitHub's rule is "If a job fails or is SKIPPED,
    all jobs that need it are skipped unless the jobs use a conditional
    expression that causes the job to continue" — so on a ``workflow_dispatch``
    run ``generate`` is skipped, and ``apply`` was skipped with it. There is no
    event under which both could run: the shipped ``apply`` had ZERO reachable
    paths. Listing ``validate`` alone is not a weakening — ``generate`` produces
    nothing ``apply`` consumes: it uploads no artifact, its only output is a
    sticky PR comment, and the two branches that need a working tree check it
    out themselves (the argocd branch needs none — ``argocd app sync`` talks to
    the server, so it has no checkout step at all).
    Enforced by the reachability assertion in
    tests/ops/test_generated_ci_artifacts.py.
    """
    if deploy_method == 'kustomize':
        return textwrap.dedent("""\

      # ── Stage 3: Apply (manual trigger only) ──────────────
      # `needs: [validate]` only — `generate` is pull_request-only, and a
      # skipped job skips everything that needs it (#1356).
      apply:
        needs: [validate]
        runs-on: ubuntu-latest
        if: github.event_name == 'workflow_dispatch'
        environment: production
        steps:
          - uses: actions/checkout@v4
          - name: Build ConfigMaps via Kustomize
            run: |
              kustomize build kustomize/overlays/prod > /tmp/manifests.yaml
          - name: Apply to cluster (dry-run first)
            run: |
              kubectl apply --dry-run=server -f /tmp/manifests.yaml
              echo "--- Dry-run passed. Applying... ---"
              kubectl apply -f /tmp/manifests.yaml
          - name: Reload Prometheus
            run: |
              kubectl rollout restart deployment/prometheus -n ${{{{ env.MONITORING_NS }}}}
    """).format(namespace=namespace)

    elif deploy_method == 'helm':
        return textwrap.dedent("""\

      # ── Stage 3: Apply via Helm (manual trigger only) ─────
      # `needs: [validate]` only — `generate` is pull_request-only, and a
      # skipped job skips everything that needs it (#1356).
      apply:
        needs: [validate]
        runs-on: ubuntu-latest
        if: github.event_name == 'workflow_dispatch'
        environment: production
        steps:
          - uses: actions/checkout@v4
          - name: Helm upgrade threshold-exporter
            run: |
              helm upgrade --install threshold-exporter \\
                oci://ghcr.io/vencil/charts/threshold-exporter \\
                -f environments/prod/values.yaml \\
                -n ${{{{ env.MONITORING_NS }}}} \\
                --wait --timeout 5m
    """).format(namespace=namespace)

    else:  # argocd
        return textwrap.dedent("""\

      # ── Stage 3: Sync ArgoCD Application ──────────────────
      # `needs: [validate]` only — `generate` is pull_request-only, and a
      # skipped job skips everything that needs it (#1356).
      apply:
        needs: [validate]
        runs-on: ubuntu-latest
        if: github.event_name == 'workflow_dispatch'
        environment: production
        steps:
          - name: Trigger ArgoCD sync
            run: |
              argocd app sync dynamic-alerting --prune --timeout 300
    """)


def _gen_github_actions(
    namespace: str,
    da_tools_image: str,
    deploy_method: str,
) -> str:
    """Generate GitHub Actions workflow for Dynamic Alerting CI/CD."""
    # ⛔ Indent the apply block IN CODE, not by hand-matching two templates.
    #
    # This is the #1347 bug, and the reason the fix looks over-engineered for
    # "two spaces". Both templates are `textwrap.dedent`ed, so where the apply
    # block lands depended on the ARITHMETIC of two independent literal
    # indents: the sub-template's common margin (6) vs. the outer template's
    # common margin (4). Because dedent strips each template by ITS OWN margin,
    # the sub-template came back at column 0 and the `{apply_stage}` line was
    # already at column 0 too — so `apply:` was emitted as a TOP-LEVEL workflow
    # key instead of a job, and GitHub refused to load the whole file. All
    # three --deploy variants shipped that way, and the outer template still
    # *looked* correctly nested in the source.
    #
    # That coupling is an implicit contract between two string literals: any
    # future re-indent of either template (a reflow, a wrapping `if`, a linter)
    # silently breaks it again, and nothing in the source says the two must
    # agree. So the nesting is now explicit and mechanical — the sub-template
    # owns a column-0 block, this line owns "put it under `jobs:`" — and
    # tests/ops/test_generated_ci_artifacts.py parses the RESULT rather than
    # trusting either literal.
    #
    # `{apply_stage}` below must therefore stay at the outer template's own
    # margin (i.e. column 0 after dedent); the two spaces added here are the
    # ONLY thing placing `apply:` at the same level as `validate:`/`generate:`.
    apply_stage = textwrap.indent(
        _build_github_apply_stage(deploy_method, namespace), '  ',
    )

    return textwrap.dedent("""\
    # Dynamic Alerting CI/CD Pipeline
    # Generated by: da-tools init
    # Docs: https://vencil.github.io/Dynamic-Alerting-Integrations/scenarios/gitops-ci-integration/
    #
    # Three stages:
    #   1. Validate: Schema + routing guardrails + domain policy
    #   2. Generate: Alertmanager routes + blast radius diff (PR comment)
    #   3. Apply:    Deploy to cluster (manual trigger only)

    name: Dynamic Alerting

    on:
      pull_request:
        paths:
          - 'conf.d/**'
          - 'kustomize/**'
          - 'rule-packs/**'
      # ⛔ No `branches:` filter, deliberately. `on.push.branches` takes literals
      # only — no expressions — so any value we write here is a guess about the
      # customer's default branch, and `main` is wrong for every `master` /
      # `trunk` / `develop` repo. Enumerating the common names is the same
      # denylist mistake one size larger. Omitting the filter is the only form
      # that is correct for everyone, and it can only ADD runs: a push to the
      # default branch is still a push, so the post-merge re-validation this leg
      # exists for is unchanged. The extra runs are `validate` alone (`generate`
      # is pull_request-only, `apply` is workflow_dispatch-only) — a read-only
      # `docker run validate-config` with no credentials, already narrowed by
      # the paths filter below. The GitLab leg gets portability from
      # `$CI_DEFAULT_BRANCH`; this is the GitHub equivalent.
      push:
        paths:
          - 'conf.d/**'
      workflow_dispatch:

    # Least-privilege, and `pull-requests: write` is LOAD-BEARING, not
    # boilerplate: the generate job's only output is a sticky PR comment, and
    # GITHUB_TOKEN defaults to read-only on repositories created after 2023-02.
    # Without this, Stage 2's comment step 403s and the customer's blast-radius
    # review never appears. Same declaration this platform's own backtest.yaml
    # carries for the same action.
    #
    # ⛔ SCOPE — this does NOT rescue a pull_request from a FORK. GitHub hard-
    # locks GITHUB_TOKEN to read-only for fork PRs and `permissions:` cannot
    # raise it; only `pull_request_target` / `workflow_run` get an elevated
    # token, and this workflow deliberately uses neither (both run trusted code
    # against untrusted input). So on a fork PR the comment step still 403s,
    # exactly as before. A customer whose contributors work from forks needs a
    # different design, not a wider `permissions:` block — tracked as a known
    # boundary rather than silently implied to be fixed.
    #
    # ⛔ The write scope sits on the `generate` job, NOT here. At workflow level
    # every job inherits it, including `apply` — the one carrying
    # `environment: production` and cluster credentials, which posts no comment
    # and needs no PR write. This is the shape this platform uses on itself in
    # 6 workflows / 9 jobs (bench-on-demand.yaml's `gate` job is identical:
    # `contents: read` at the top, `pull-requests: write` on the one job that
    # comments).
    permissions:
      contents: read

    env:
      DA_TOOLS_IMAGE: {da_tools_image}
      CONFIG_DIR: conf.d
      {monitoring_ns_env}

    jobs:
      # ── Stage 1: Validate ─────────────────────────────────
      validate:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4

          - name: Validate config (schema + routing + policy)
            run: |
              docker run --rm \\
                -v ${{{{ github.workspace }}}}/${{{{ env.CONFIG_DIR }}}}:/data/conf.d:ro \\
                ${{{{ env.DA_TOOLS_IMAGE }}}} \\
                validate-config --config-dir /data/conf.d

          - name: Lint custom rules (if any)
            run: |
              if [ -d "rule-packs/custom" ]; then
                docker run --rm \\
                  -v ${{{{ github.workspace }}}}/rule-packs/custom:/data/rules:ro \\
                  ${{{{ env.DA_TOOLS_IMAGE }}}} \\
                  lint /data/rules --ci
              fi

      # ── Stage 2: Generate routes + blast radius ────────────
      generate:
        needs: validate
        runs-on: ubuntu-latest
        if: github.event_name == 'pull_request'
        # The only job that writes anything back to the PR. A job-level block
        # REPLACES the workflow one rather than merging, so `contents: read` is
        # restated here — dropping it would 403 the checkout.
        permissions:
          contents: read
          pull-requests: write
        steps:
          - uses: actions/checkout@v4

          - name: Prepare output directory
            run: mkdir -p .output

          - name: Generate Alertmanager routes
            run: |
              docker run --rm \\
                -v ${{{{ github.workspace }}}}/${{{{ env.CONFIG_DIR }}}}:/data/conf.d:ro \\
                -v ${{{{ github.workspace }}}}/.output:/data/output \\
                ${{{{ env.DA_TOOLS_IMAGE }}}} \\
                generate-routes --config-dir /data/conf.d \\
                  -o /data/output/alertmanager-routes.yaml \\
                  --validate

          - name: Checkout base branch config for diff
            if: github.event_name == 'pull_request'
            run: |
              git show ${{{{ github.event.pull_request.base.sha }}}}:conf.d > /dev/null 2>&1 && \\
                git archive ${{{{ github.event.pull_request.base.sha }}}} conf.d/ | tar -x -C .output/base/ || \\
                mkdir -p .output/base/conf.d

          - name: Config diff (blast radius)
            run: |
              docker run --rm \\
                -v ${{{{ github.workspace }}}}/.output/base/conf.d:/data/conf.d.base:ro \\
                -v ${{{{ github.workspace }}}}/${{{{ env.CONFIG_DIR }}}}:/data/conf.d:ro \\
                -v ${{{{ github.workspace }}}}/.output:/data/output \\
                ${{{{ env.DA_TOOLS_IMAGE }}}} \\
                config-diff --old-dir /data/conf.d.base --new-dir /data/conf.d \\
                  --format markdown > .output/blast-radius.md

          - name: Post PR comment with blast radius
            if: github.event_name == 'pull_request'
            uses: marocchino/sticky-pull-request-comment@v2
            with:
              path: .output/blast-radius.md
              header: dynamic-alerting-blast-radius
    {apply_stage}
    """).format(
        da_tools_image=da_tools_image,
        namespace=namespace,
        apply_stage=apply_stage,
        # ⛔ Declared only where something reads it. The argocd branch runs
        # `argocd app sync` and never names a namespace, so emitting
        # MONITORING_NS there would ship the customer a knob that does nothing —
        # the #1361 class, in `env:` rather than in `workflow_dispatch.inputs`.
        # Held by the knob-reachability assertion in
        # tests/ops/test_generated_ci_artifacts.py.
        # ⚠️ A COMMENT, not an empty string, and the placeholder stays INDENTED
        # in the template: a substitution at column 0 inside a textwrap.dedent
        # block resets the common prefix and un-indents the whole document —
        # which is #1347 itself. Measured while writing this: the column-0 form
        # broke `test_github_workflow_parses_as_yaml`.
        monitoring_ns_env=(
            "# (no MONITORING_NS — the argocd branch never names a namespace)"
            if deploy_method == "argocd"
            else f"MONITORING_NS: {namespace}"
        ),
    )


def _build_gitlab_apply_stage(deploy_method: str, namespace: str) -> str:
    """Build GitLab CI apply stage based on deployment method.

    The runner image is emitted as `$VAR`, never as a literal ref: the pin
    lives once in _GITLAB_APPLY_IMAGES and reaches the customer through the
    `variables:` block, so they can override it without editing generated
    YAML. ⛔ The branching here and in _gitlab_apply_image must stay in step —
    including the `else` fallback (see that function's docstring).
    """
    image_var, _ = _gitlab_apply_image(deploy_method)

    if deploy_method == 'kustomize':
        return textwrap.dedent("""\

    # ── Stage 3: Apply ───────────────────────────────────────
    apply:
      stage: apply
      image:
        name: ${image_var}
        # ⛔ Load-bearing, not boilerplate. GitLab runs `script:` through a shell
        # INSIDE this image, so an image whose ENTRYPOINT is the tool itself
        # (alpine/helm is `ENTRYPOINT ["helm"]`) turns that shell invocation into
        # arguments to the tool and the job dies before the first script line.
        # Harmless for images that already start a shell.
        entrypoint: [""]
      environment:
        name: production
      rules:
        # ⛔ The `if:` is load-bearing, not decoration. A `rules:` entry with no
        # `if:` / `changes:` / `exists:` matches EVERY pipeline, so a bare
        # `- when: manual` attaches this job — which holds `environment:
        # production` and cluster-write credentials — to every push and every
        # merge request, including one opened by any Developer-role contributor
        # against code that was never merged and never passed validate. Pinning
        # it to the default branch is what removes that.
        #
        # ⛔ NOT parity with the GitHub sibling, and it would be wrong to read it
        # that way: `workflow_dispatch` gates the EVENT, not the ref. Whoever
        # dispatches picks the branch in the Run-workflow dialog, so the GitHub
        # `apply` is still dispatchable from an unmerged branch while this one is
        # not. Closing that gap needs `github.ref` in the job's `if:`, which the
        # reachability evaluator cannot parse today — tracked as a stated
        # boundary rather than half-done here. Held by the trigger-scope
        # assertion in tests/ops/test_generated_ci_artifacts.py.
        - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
          when: manual
      script:
        - kustomize build kustomize/overlays/prod > /tmp/manifests.yaml
        - kubectl apply --dry-run=server -f /tmp/manifests.yaml
        - kubectl apply -f /tmp/manifests.yaml
        - kubectl rollout restart deployment/prometheus -n $MONITORING_NS
    """).format(namespace=namespace, image_var=image_var)

    elif deploy_method == 'helm':
        return textwrap.dedent("""\

    # ── Stage 3: Apply via Helm ──────────────────────────────
    apply:
      stage: apply
      image:
        name: ${image_var}
        # ⛔ Load-bearing, not boilerplate. GitLab runs `script:` through a shell
        # INSIDE this image, so an image whose ENTRYPOINT is the tool itself
        # (alpine/helm is `ENTRYPOINT ["helm"]`) turns that shell invocation into
        # arguments to the tool and the job dies before the first script line.
        # Harmless for images that already start a shell.
        entrypoint: [""]
      environment:
        name: production
      rules:
        # ⛔ The `if:` is load-bearing, not decoration. A `rules:` entry with no
        # `if:` / `changes:` / `exists:` matches EVERY pipeline, so a bare
        # `- when: manual` attaches this job — which holds `environment:
        # production` and cluster-write credentials — to every push and every
        # merge request, including one opened by any Developer-role contributor
        # against code that was never merged and never passed validate. Pinning
        # it to the default branch is what removes that.
        #
        # ⛔ NOT parity with the GitHub sibling, and it would be wrong to read it
        # that way: `workflow_dispatch` gates the EVENT, not the ref. Whoever
        # dispatches picks the branch in the Run-workflow dialog, so the GitHub
        # `apply` is still dispatchable from an unmerged branch while this one is
        # not. Closing that gap needs `github.ref` in the job's `if:`, which the
        # reachability evaluator cannot parse today — tracked as a stated
        # boundary rather than half-done here. Held by the trigger-scope
        # assertion in tests/ops/test_generated_ci_artifacts.py.
        - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
          when: manual
      script:
        - |
          helm upgrade --install threshold-exporter \\
            oci://ghcr.io/vencil/charts/threshold-exporter \\
            -f environments/prod/values.yaml \\
            -n $MONITORING_NS \\
            --wait --timeout 5m
    """).format(namespace=namespace, image_var=image_var)

    else:  # argocd
        return textwrap.dedent("""\

    # ── Stage 3: Sync ArgoCD Application ─────────────────────
    apply:
      stage: apply
      image:
        name: ${image_var}
        # ⛔ Load-bearing, not boilerplate. GitLab runs `script:` through a shell
        # INSIDE this image, so an image whose ENTRYPOINT is the tool itself
        # (alpine/helm is `ENTRYPOINT ["helm"]`) turns that shell invocation into
        # arguments to the tool and the job dies before the first script line.
        # Harmless for images that already start a shell.
        entrypoint: [""]
      environment:
        name: production
      rules:
        # ⛔ The `if:` is load-bearing, not decoration. A `rules:` entry with no
        # `if:` / `changes:` / `exists:` matches EVERY pipeline, so a bare
        # `- when: manual` attaches this job — which holds `environment:
        # production` and cluster-write credentials — to every push and every
        # merge request, including one opened by any Developer-role contributor
        # against code that was never merged and never passed validate. Pinning
        # it to the default branch is what removes that.
        #
        # ⛔ NOT parity with the GitHub sibling, and it would be wrong to read it
        # that way: `workflow_dispatch` gates the EVENT, not the ref. Whoever
        # dispatches picks the branch in the Run-workflow dialog, so the GitHub
        # `apply` is still dispatchable from an unmerged branch while this one is
        # not. Closing that gap needs `github.ref` in the job's `if:`, which the
        # reachability evaluator cannot parse today — tracked as a stated
        # boundary rather than half-done here. Held by the trigger-scope
        # assertion in tests/ops/test_generated_ci_artifacts.py.
        - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
          when: manual
      script:
        - argocd app sync dynamic-alerting --prune --timeout 300
    """).format(image_var=image_var)


def _gen_gitlab_ci(
    namespace: str,
    da_tools_image: str,
    deploy_method: str,
) -> str:
    """Generate GitLab CI pipeline for Dynamic Alerting CI/CD."""
    apply_stage = _build_gitlab_apply_stage(deploy_method, namespace)
    apply_image_var, apply_image_ref = _gitlab_apply_image(deploy_method)

    return textwrap.dedent("""\
    # Dynamic Alerting CI/CD Pipeline (GitLab CI)
    # Generated by: da-tools init
    # Docs: https://vencil.github.io/Dynamic-Alerting-Integrations/scenarios/gitops-ci-integration/
    #
    # Three stages: validate → generate → apply

    stages:
      - validate
      - generate
      - apply

    variables:
      DA_TOOLS_IMAGE: {da_tools_image}
      # Apply-stage runner image. Pinned to a version tag we verified resolves;
      # override it here (not in the job) to track your own cluster / Helm line.
      {apply_image_var}: {apply_image_ref}
      CONFIG_DIR: conf.d
      {monitoring_ns_var}

    # ── Stage 1: Validate ────────────────────────────────────
    validate-config:
      stage: validate
      image: $DA_TOOLS_IMAGE
      rules:
        - changes:
            - conf.d/**/*
            - rule-packs/**/*
      script:
        - da-tools validate-config --config-dir $CONFIG_DIR

    lint-custom-rules:
      stage: validate
      image: $DA_TOOLS_IMAGE
      rules:
        - changes:
            - rule-packs/custom/**/*
          exists:
            - rule-packs/custom/
      # ⛔ NO `allow_failure:`. `da-tools lint --ci` exits non-zero on ERROR
      # only (lint_custom_rules.py: `if args.ci and errors`), and its ERRORs are
      # the governance deny-list on tenant-authored raw PromQL — denied
      # functions, denied patterns, and missing required labels, the last of
      # which is what four-layer routing keys on. WARN-level naming nits do not
      # affect the exit code at all, so the tool has already made the
      # severity call and `allow_failure` was overriding it.
      #
      # The GitHub artifact runs the identical check as a step inside `validate`
      # with no exemption, and this platform gates ITSELF on the same script
      # (ci.yml + validate.yaml, both `--ci`, neither `continue-on-error`).
      # Swallowing it here made one leg of a pair, and the customer's own repo,
      # weaker than the platform holds itself to.
      script:
        - da-tools lint rule-packs/custom/ --ci

    # ── Stage 2: Generate routes + blast radius ──────────────
    generate-routes:
      stage: generate
      image: $DA_TOOLS_IMAGE
      rules:
        - if: $CI_PIPELINE_SOURCE == "merge_request_event"
          changes:
            - conf.d/**/*
      script:
        - mkdir -p .output .output/base/conf.d
        - git archive $CI_MERGE_REQUEST_DIFF_BASE_SHA -- conf.d/ | tar -x -C .output/base/ 2>/dev/null || true
        - da-tools generate-routes --config-dir $CONFIG_DIR -o .output/alertmanager-routes.yaml --validate
        - da-tools config-diff --old-dir .output/base/conf.d --new-dir $CONFIG_DIR --format markdown > .output/blast-radius.md
      artifacts:
        paths:
          - .output/
        expire_in: 7 days
    {apply_stage}
    """).format(
        da_tools_image=da_tools_image,
        namespace=namespace,
        apply_image_var=apply_image_var,
        apply_image_ref=apply_image_ref,
        # Same rule as the GitHub leg: declare the knob only where a script
        # reads it. The argocd branch runs `argocd app sync` and never names a
        # namespace. This leg shipped `MONITORING_NS` in all three branches
        # while every script hardcoded `-n monitoring` — the #1361 class, live,
        # on the generator whose sibling this PR had already fixed.
        monitoring_ns_var=(
            "# (no MONITORING_NS — the argocd branch never names a namespace)"
            if deploy_method == "argocd"
            else f"MONITORING_NS: {namespace}"
        ),
        apply_stage=apply_stage,
    )


# ============================================================
# Configuration File Generators (YAML templates & Kustomize)
# ============================================================

def _gen_kustomize_base(tenants: list[str], namespace: str) -> str:
    """Generate kustomize/base/kustomization.yaml."""

    configmap_files = ['_defaults.yaml'] + [f'{t}.yaml' for t in tenants]
    file_lines = '\n'.join(f'    - {f}' for f in configmap_files)

    return (
        "# kustomization.yaml — Dynamic Alerting ConfigMap generator\n"
        "# Generated by: da-tools init\n"
        "#\n"
        "# Generates threshold-config ConfigMap from conf.d/ files.\n"
        "# Each file becomes a key in the ConfigMap.\n"
        "\n"
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "\n"
        f"namespace: {namespace}\n"
        "\n"
        "configMapGenerator:\n"
        "  - name: threshold-config\n"
        "    behavior: create\n"
        "    files:\n"
        f"{file_lines}\n"
        "\n"
        "generatorOptions:\n"
        "  disableNameSuffixHash: true\n"
    )


def _gen_git_sync_deployment(
    namespace: str, git_repo: str, git_branch: str, git_path: str,
    git_period: int = 60,
) -> str:
    """Generate K8s Deployment patch adding git-sync sidecar to threshold-exporter."""
    return textwrap.dedent(f"""\
    # git-sync-patch.yaml — GitOps Native Mode
    # Generated by: da-tools init --config-source git
    #
    # Architecture:
    #   1. initContainer (git-sync --one-time) — clones repo before exporter starts
    #   2. sidecar (git-sync --period) — keeps config in sync with Git
    #   3. threshold-exporter reads from shared emptyDir via existing Directory Scanner
    #
    # Prerequisites:
    #   kubectl create secret generic git-sync-credentials \\
    #     --from-file=ssh-key=~/.ssh/id_ed25519 \\
    #     -n {namespace}
    #   OR for HTTPS:
    #   kubectl create secret generic git-sync-credentials \\
    #     --from-literal=username=<user> --from-literal=password=<token> \\
    #     -n {namespace}
    #
    # Verify:
    #   da-tools gitops-check sidecar --namespace {namespace}

    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: threshold-exporter
      namespace: {namespace}
    spec:
      template:
        spec:
          # ── Init: one-time clone so exporter never starts with empty config ──
          initContainers:
            - name: git-sync-init
              image: {GIT_SYNC_IMAGE}
              args:
                - "--repo={git_repo}"
                - "--ref={git_branch}"
                - "--root=/data/config"
                - "--link=current"
                - "--one-time"
              volumeMounts:
                - name: git-config
                  mountPath: /data/config
                - name: git-credentials
                  mountPath: /etc/git-secret
                  readOnly: true
              securityContext:
                runAsUser: 65533
                runAsGroup: 65533
          containers:
            - name: threshold-exporter
              args:
                # git-sync --link=current creates: /data/config/current → <checkout>
                - "--config-dir=/data/config/current/{git_path}"
              volumeMounts:
                - name: git-config
                  mountPath: /data/config
                  readOnly: true
            - name: git-sync
              image: {GIT_SYNC_IMAGE}
              args:
                - "--repo={git_repo}"
                - "--ref={git_branch}"
                - "--root=/data/config"
                - "--period={git_period}s"
                - "--link=current"
                - "--max-failures=3"
              volumeMounts:
                - name: git-config
                  mountPath: /data/config
                - name: git-credentials
                  mountPath: /etc/git-secret
                  readOnly: true
              securityContext:
                runAsUser: 65533
                runAsGroup: 65533
              resources:
                requests:
                  cpu: 10m
                  memory: 32Mi
                limits:
                  cpu: 50m
                  memory: 64Mi
          volumes:
            - name: git-config
              emptyDir: {{}}
            - name: git-credentials
              secret:
                secretName: git-sync-credentials
                optional: true
    """)


def _gen_git_sync_kustomization(namespace: str) -> str:
    """Generate kustomization.yaml for git-sync overlay."""
    return textwrap.dedent(f"""\
    # kustomization.yaml — GitOps Native Mode overlay
    # Generated by: da-tools init --config-source git
    #
    # This overlay patches the threshold-exporter Deployment
    # to use git-sync sidecar instead of ConfigMap volume.

    apiVersion: kustomize.config.k8s.io/v1beta1
    kind: Kustomization

    namespace: {namespace}

    resources:
      - ../../base

    patches:
      - path: git-sync-patch.yaml
        target:
          kind: Deployment
          name: threshold-exporter
    """)


def _gen_kustomize_overlay(env_name: str, namespace: str) -> str:
    """Generate kustomize/overlays/<env>/kustomization.yaml."""
    return (
        f"# kustomization.yaml — {env_name} overlay\n"
        "# Generated by: da-tools init\n"
        "\n"
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "\n"
        f"namespace: {namespace}\n"
        "\n"
        "resources:\n"
        "  - ../../base\n"
    )


def _gen_precommit_snippet(da_tools_image: str) -> str:
    """Generate .pre-commit-config.yaml snippet.

    ⛔ Takes the image as an argument. It used to hardcode
    `ghcr.io/vencil/da-tools:latest`, so a customer who ran
    `--da-tools-image registry.internal/da-tools:v2.9.0` still got a snippet
    pointing back at ghcr.io — silently ignoring the flag on the one artifact
    that runs on every developer's laptop (#1337 ④).
    """
    return (
        "# Dynamic Alerting pre-commit hooks\n"
        "# Add this to your existing .pre-commit-config.yaml\n"
        "# Generated by: da-tools init\n"
        "#\n"
        "# Validates tenant YAML on every commit (shift-left).\n"
        "#\n"
        "# NOTE: language is docker_image, not system. pre-commit splits `entry`\n"
        "# with shlex and execs it WITHOUT a shell (pre_commit/lang_base.py), so an\n"
        "# earlier `docker run -v ${PWD}/conf.d:...` form passed the literal string\n"
        "# ${PWD} to docker and failed on every commit with an invalid-volume error.\n"
        "# docker_image makes pre-commit build the `docker run` itself and mount the\n"
        "# repo at /src (pre_commit/languages/docker.py: `-v <cwd>:/src:rw,Z\n"
        "# --workdir /src`), which is why the paths below are /src-relative.\n"
        "# Trade-off, stated: that mount is the whole repo read-WRITE, wider than the\n"
        "# read-only conf.d mount the broken form asked for — but it is pre-commit's\n"
        "# own mechanism, and a hook that cannot run protects nothing.\n"
        "\n"
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: da-validate-config\n"
        "        name: Validate Dynamic Alerting config\n"
        "        entry: >-\n"
        f"          {da_tools_image}\n"
        "          validate-config --config-dir /src/conf.d\n"
        "        language: docker_image\n"
        "        files: ^conf\\.d/.*\\.ya?ml$\n"
        "        pass_filenames: false\n"
        "\n"
        "      - id: da-generate-routes\n"
        "        name: Generate Alertmanager routes (dry-run)\n"
        "        entry: >-\n"
        f"          {da_tools_image}\n"
        "          generate-routes --config-dir /src/conf.d --dry-run --validate\n"
        "        language: docker_image\n"
        "        files: ^conf\\.d/.*\\.ya?ml$\n"
        "        pass_filenames: false\n"
    )


def _gen_da_init_marker(
    ci_platform: str,
    deploy_method: str,
    rule_packs: list[str],
    tenants: list[str],
    config_source: str = 'configmap',
    git_repo: Optional[str] = None,
) -> str:
    """Generate .da-init.yaml marker file."""
    marker = {
        'version': '2.2.0',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'ci_platform': ci_platform,
        'deploy_method': deploy_method,
        'config_source': config_source,
        'rule_packs': rule_packs,
        'tenants': tenants,
    }
    if git_repo:
        marker['git_repo'] = git_repo
    header = textwrap.dedent("""\
    # .da-init.yaml — Dynamic Alerting project marker
    # Do not edit manually. Used by da-tools for upgrade detection.
    """)
    return header + yaml.dump(marker, default_flow_style=False, sort_keys=False)


# ============================================================
# Interactive prompts
# ============================================================

def _prompt_choice(prompt_text: str, choices: list[str], default: str) -> str:
    """Prompt user to choose from a list."""
    while True:
        print(f"\n{prompt_text}")
        for i, c in enumerate(choices, 1):
            marker = ' (default)' if c == default else ''
            print(f"  {i}. {c}{marker}")
        raw = input(f"\n> ").strip()
        if not raw:
            return default
        if raw in choices:
            return raw
        try:
            idx = int(raw)
            if 1 <= idx <= len(choices):
                return choices[idx - 1]
        except ValueError:
            pass
        print(f"  Invalid choice. Please enter 1-{len(choices)} or a value from the list.")


def _selectable_rule_packs() -> list[str]:
    """Return rule packs that users can select (excludes auto-enabled)."""
    return [k for k, v in RULE_PACK_CATALOG.items() if not v.get('auto_enabled')]


def _auto_enabled_rule_packs() -> list[str]:
    """Return rule packs that are always auto-enabled."""
    return [k for k, v in RULE_PACK_CATALOG.items() if v.get('auto_enabled')]


def _prompt_multi(prompt_text: str, choices: list[str], defaults: Optional[list[str]] = None) -> list[str]:
    """Prompt user to select multiple items."""
    is_zh = _LANG == 'zh'
    print(f"\n{prompt_text}")
    for idx, c in enumerate(choices, 1):
        label = RULE_PACK_CATALOG.get(c, {}).get('label', c)
        marker = ' *' if defaults and c in defaults else ''
        print(f"  {idx:2d}. {c:20s} ({label}){marker}")

    # Show auto-enabled packs as info
    auto = _auto_enabled_rule_packs()
    if auto:
        tag = "自動啟用，無需選擇" if is_zh else "auto-enabled, no selection needed"
        print(f"\n  [{tag}]")
        for a in auto:
            label = RULE_PACK_CATALOG[a]['label']
            print(f"   ✓ {a:20s} ({label})")

    if defaults:
        default_str = ','.join(defaults)
        hint = f" (default: {default_str})" if _LANG == 'en' else f" (預設: {default_str})"
    else:
        hint = ""

    raw = input(f"\nEnter numbers or names, comma-separated{hint}\n> ").strip()
    if not raw and defaults:
        return defaults

    selected = []
    for token in raw.split(','):
        token = token.strip()
        if token in choices:
            selected.append(token)
        else:
            try:
                num = int(token)
                if 1 <= num <= len(choices):
                    selected.append(choices[num - 1])
            except ValueError:
                pass
    return selected or (defaults or [])


def _prompt_text(prompt_text: str, default: str = '') -> str:
    """Prompt for free-text input."""
    hint = f" [{default}]" if default else ""
    raw = input(f"\n{prompt_text}{hint}\n> ").strip()
    return raw or default


def _validate_tenant_name(name: str) -> bool:
    """Validate tenant name follows K8s naming conventions."""
    import re
    return bool(re.match(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$', name)) and len(name) <= 63


def _interactive_flow() -> dict:
    """Run interactive prompts and return config dict."""
    is_zh = _LANG == 'zh'

    print("=" * 60)
    print("  da-tools init — Dynamic Alerting 整合初始化" if is_zh
          else "  da-tools init — Dynamic Alerting Integration Setup")
    print("=" * 60)

    ci = _prompt_choice(
        "選擇 CI/CD 平台:" if is_zh else "Select CI/CD platform:",
        _parser_choices('--ci'),
        'both',
    )

    deploy = _prompt_choice(
        "選擇部署方式:" if is_zh else "Select deployment method:",
        _parser_choices('--deploy'),
        'kustomize',
    )

    rule_packs = _prompt_multi(
        "選擇 Rule Packs (你的監控對象):" if is_zh else "Select Rule Packs (what you're monitoring):",
        _selectable_rule_packs(),
        ['mariadb', 'kubernetes'],
    )

    tenant_str = _prompt_text(
        "輸入租戶名稱 (逗號分隔):" if is_zh else "Enter tenant names (comma-separated):",
        'db-a,db-b',
    )
    tenants = [t.strip() for t in tenant_str.split(',') if t.strip()]

    # Validate tenant names
    invalid_names = [t for t in tenants if not _validate_tenant_name(t)]
    if invalid_names:
        warn = "⚠️  以下租戶名稱不符合 K8s 命名規範" if is_zh else "⚠️  Invalid tenant names (K8s convention: lowercase, alphanumeric, hyphens)"
        print(f"\n  {warn}: {', '.join(invalid_names)}")

    namespace = _prompt_text(
        "Kubernetes monitoring namespace:" if is_zh else "Kubernetes monitoring namespace:",
        'monitoring',
    )

    return {
        'ci': ci,
        'deploy': deploy,
        'rule_packs': rule_packs,
        'tenants': tenants,
        'namespace': namespace,
        'da_tools_image': DA_TOOLS_IMAGE,
    }


# ============================================================
# File writer
# ============================================================

def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_file(path: str, content: str, created_files: list[str]) -> None:
    """Write file with SAST-compliant writer, track in list."""
    _ensure_dir(str(Path(path).parent))
    write_text_secure(path, content)
    created_files.append(path)


# ============================================================
# Main orchestration
# ============================================================

def _preview_files(config: dict, output_dir: str) -> list[str]:
    """Return list of file paths that would be created (without writing).

    Paths use POSIX separators (forward-slash) regardless of OS, since
    these strings are shown to the user as "what will be created" preview
    output — cross-platform consistent display matters more than native
    separator. Same rationale as _snapshot_mtimes (PR #319).
    """
    out = Path(output_dir)
    paths: list[str] = []
    ci, deploy = config['ci'], config['deploy']
    tenants = config['tenants']

    def _add(p: Path) -> None:
        paths.append(p.as_posix())

    _add(out / 'conf.d' / '_defaults.yaml')
    for t in tenants:
        _add(out / 'conf.d' / f'{t}.yaml')
    if ci in ('github', 'both'):
        _add(out / '.github' / 'workflows' / 'dynamic-alerting.yaml')
    if ci in ('gitlab', 'both'):
        _add(out / '.gitlab-ci.d' / 'dynamic-alerting.yml')
    if deploy == 'kustomize':
        _add(out / 'kustomize' / 'base' / 'kustomization.yaml')
        _add(out / 'kustomize' / 'base' / 'README.md')
        _add(out / 'kustomize' / 'overlays' / 'dev' / 'kustomization.yaml')
        _add(out / 'kustomize' / 'overlays' / 'prod' / 'kustomization.yaml')
    # GitOps Native Mode writes a gitops overlay regardless of --deploy
    # (run_init step 3b). Omitting it here understated the dry-run by two
    # files, one of them a Deployment patch — the preview promised nothing
    # would touch kustomize/ and then two files appeared there.
    if config.get('config_source') == 'git' and config.get('git_repo'):
        _add(out / 'kustomize' / 'overlays' / 'gitops' / 'kustomization.yaml')
        _add(out / 'kustomize' / 'overlays' / 'gitops' / 'git-sync-patch.yaml')
    _add(out / '.pre-commit-config.da.yaml')
    _add(out / '.da-init.yaml')
    return paths


def run_init(config: dict, output_dir: str) -> list[str]:
    """Generate all files based on config. Returns list of created file paths."""
    created: list[str] = []

    ci = config['ci']
    deploy = config['deploy']
    rule_packs = config['rule_packs']
    tenants = config['tenants']
    namespace = config['namespace']
    da_tools_image = config['da_tools_image']
    config_source = config.get('config_source', 'configmap')
    git_repo = config.get('git_repo', '')
    git_branch = config.get('git_branch', 'main')
    git_path = config.get('git_path', 'conf.d')
    git_period = config.get('git_period', 60)

    # ── 1. conf.d/ ─────────────────────────────────────────
    out = Path(output_dir)
    conf_dir = out / 'conf.d'

    _write_file(
        str(conf_dir / '_defaults.yaml'),
        _gen_defaults_yaml(rule_packs, namespace),
        created,
    )

    for tenant in tenants:
        _write_file(
            str(conf_dir / f'{tenant}.yaml'),
            _gen_tenant_yaml(tenant, rule_packs),
            created,
        )

    # ── 2. CI/CD pipelines ─────────────────────────────────
    if ci in ('github', 'both'):
        _write_file(
            str(out / '.github' / 'workflows' / 'dynamic-alerting.yaml'),
            _gen_github_actions(namespace, da_tools_image, deploy),
            created,
        )

    if ci in ('gitlab', 'both'):
        _write_file(
            str(out / '.gitlab-ci.d' / 'dynamic-alerting.yml'),
            _gen_gitlab_ci(namespace, da_tools_image, deploy),
            created,
        )

    # ── 3. Kustomize overlays ──────────────────────────────
    if deploy == 'kustomize':
        kust_base = out / 'kustomize' / 'base'
        _write_file(
            str(kust_base / 'kustomization.yaml'),
            _gen_kustomize_base(tenants, namespace),
            created,
        )

        # Copy conf.d files into kustomize base (symlink in production)
        # For now, generate a README explaining the setup
        _write_file(
            str(kust_base / 'README.md'),
            textwrap.dedent("""\
            # Kustomize Base

            This directory uses `configMapGenerator` to create the `threshold-config`
            ConfigMap from your `conf.d/` files.

            **Setup:** Create symlinks from `conf.d/` files to this directory:

            ```bash
            ln -s ../../conf.d/_defaults.yaml .
            ln -s ../../conf.d/db-a.yaml .
            # ... for each tenant
            ```

            Or copy files during CI (see the generated workflow).
            """),
            created,
        )

        for env_name in ('dev', 'prod'):
            _write_file(
                str(out / 'kustomize' / 'overlays' / env_name / 'kustomization.yaml'),
                _gen_kustomize_overlay(env_name, namespace),
                created,
            )

    # ── 3b. GitOps Native Mode (git-sync sidecar) ──────────
    if config_source == 'git' and git_repo:
        gitsync_dir = out / 'kustomize' / 'overlays' / 'gitops'
        _write_file(
            str(gitsync_dir / 'kustomization.yaml'),
            _gen_git_sync_kustomization(namespace),
            created,
        )
        _write_file(
            str(gitsync_dir / 'git-sync-patch.yaml'),
            _gen_git_sync_deployment(
                namespace, git_repo, git_branch, git_path, git_period,
            ),
            created,
        )

    # ── 4. Pre-commit config ───────────────────────────────
    _write_file(
        str(out / '.pre-commit-config.da.yaml'),
        _gen_precommit_snippet(da_tools_image),
        created,
    )

    # ── 5. Marker file ─────────────────────────────────────
    _write_file(
        str(out / '.da-init.yaml'),
        _gen_da_init_marker(ci, deploy, rule_packs, tenants, config_source, git_repo),
        created,
    )

    return created


def _print_summary(created: list[str], output_dir: str, config: dict) -> None:
    """Print post-init summary."""
    is_zh = _LANG == 'zh'

    print()
    print("=" * 60)
    print("  初始化完成！" if is_zh else "  Initialization complete!")
    print("=" * 60)
    print()
    print(f"  {'產生的檔案' if is_zh else 'Generated files'}: {len(created)}")
    print(f"  {'輸出目錄' if is_zh else 'Output directory'}: {output_dir}")
    print()

    for f in created:
        rel = str(Path(f).relative_to(output_dir))
        print(f"  ✓ {rel}")

    # Show auto-enabled packs
    auto = _auto_enabled_rule_packs()
    if auto:
        print()
        tag = "自動啟用的 Rule Pack" if is_zh else "Auto-enabled Rule Packs"
        print(f"  {tag}: {', '.join(auto)}")

    print()
    print("─" * 60)
    print("  " + ("下一步：" if is_zh else "Next steps:"))
    print()

    step = 1
    if config['deploy'] == 'kustomize':
        if is_zh:
            print(f"  {step}. 建立 conf.d/ 到 kustomize/base/ 的符號連結")
        else:
            print(f"  {step}. Create symlinks from conf.d/ to kustomize/base/")
        step += 1

    if is_zh:
        print(f"  {step}. 編輯 conf.d/_defaults.yaml — 調整平台預設閾值")
    else:
        print(f"  {step}. Edit conf.d/_defaults.yaml — adjust platform default thresholds")
    step += 1

    for t in config['tenants']:
        if is_zh:
            print(f"  {step}. 編輯 conf.d/{t}.yaml — 設定租戶覆寫閾值與路由")
        else:
            print(f"  {step}. Edit conf.d/{t}.yaml — set tenant override thresholds and routing")
        step += 1

    if is_zh:
        print(f"  {step}. 合併 .pre-commit-config.da.yaml 到你的 .pre-commit-config.yaml")
    else:
        print(f"  {step}. Merge .pre-commit-config.da.yaml into your .pre-commit-config.yaml")
    step += 1

    if is_zh:
        print(f"  {step}. 提交並推送 — CI 會自動驗證你的配置")
    else:
        print(f"  {step}. Commit and push — CI will automatically validate your config")
    step += 1

    print()
    if is_zh:
        print("  📖 完整指南: https://vencil.github.io/Dynamic-Alerting-Integrations/scenarios/gitops-ci-integration/")
        print("  🛠️  驗證: da-tools validate-config --config-dir conf.d/")
    else:
        print("  📖 Full guide: https://vencil.github.io/Dynamic-Alerting-Integrations/scenarios/gitops-ci-integration/")
        print("  🛠️  Validate: da-tools validate-config --config-dir conf.d/")
    print()


# ============================================================
# CLI entry point
# ============================================================

# ============================================================
# Initialization & CLI Validation
# ============================================================

def _check_existing_init(output_dir: str, force: bool, parser: argparse.ArgumentParser) -> None:
    """Check if directory is already initialized."""
    marker_path = str(Path(output_dir) / '.da-init.yaml')
    if Path(marker_path).is_file() and not force:
        if _LANG == 'zh':
            print(f"⚠️  此目錄已初始化 ({marker_path})。", file=sys.stderr)
            print("   使用 --force 覆寫或手動刪除 .da-init.yaml。", file=sys.stderr)
        else:
            print(f"⚠️  This directory is already initialized ({marker_path}).", file=sys.stderr)
            print("   Use --force to overwrite or remove .da-init.yaml manually.", file=sys.stderr)
        sys.exit(EXIT_VIOLATION)


def _build_config_from_args(args, parser: argparse.ArgumentParser) -> dict:
    """Build configuration from CLI args or interactive flow."""
    if args.config_source == 'git' and not args.git_repo:
        parser.error("--config-source git requires --git-repo <url>")

    has_cli_args = args.ci or args.tenants or args.rule_packs or args.deploy
    if args.non_interactive or has_cli_args:
        if args.non_interactive and not args.tenants:
            parser.error("--non-interactive requires --tenants")
        return {
            'ci': args.ci or 'both',
            'deploy': args.deploy or 'kustomize',
            'rule_packs': [r.strip() for r in (args.rule_packs or 'mariadb,kubernetes').split(',')],
            'tenants': [t.strip() for t in (args.tenants or 'db-a,db-b').split(',')],
            'namespace': args.namespace,
            'da_tools_image': args.da_tools_image,
            'config_source': args.config_source,
            'git_repo': args.git_repo,
            'git_branch': args.git_branch,
            'git_path': args.git_path,
            'git_period': args.git_period,
        }
    else:
        config = _interactive_flow()
        config['da_tools_image'] = args.da_tools_image
        config.setdefault('config_source', args.config_source)
        config.setdefault('git_repo', args.git_repo)
        config.setdefault('git_branch', args.git_branch)
        config.setdefault('git_path', args.git_path)
        config.setdefault('git_period', args.git_period)
        return config


def _validate_config(config: dict) -> None:
    """Validate tenant names and rule packs in config."""
    # Validate tenant names (K8s naming conventions)
    invalid_tenants = [t for t in config['tenants']
                       if not _validate_tenant_name(t)]
    if invalid_tenants:
        if _LANG == 'zh':
            print(f"⚠️  以下租戶名稱不符合 K8s 命名規範: "
                  f"{', '.join(invalid_tenants)}", file=sys.stderr)
            print("   規則: 小寫英數 + 連字號, 最長 63 字元", file=sys.stderr)
        else:
            print(f"⚠️  Invalid tenant names (K8s convention): "
                  f"{', '.join(invalid_tenants)}", file=sys.stderr)
            print("   Rules: lowercase alphanumeric + hyphens, max 63 chars",
                  file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # Reject empty tenant list
    if not config['tenants']:
        if _LANG == 'zh':
            print("⚠️  至少需要一個租戶名稱", file=sys.stderr)
        else:
            print("⚠️  At least one tenant name is required", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # Filter out auto-enabled packs and validate remaining ones
    selectable = set(_selectable_rule_packs())
    auto = set(_auto_enabled_rule_packs())
    config['rule_packs'] = [r for r in config['rule_packs'] if r not in auto]

    invalid = [r for r in config['rule_packs'] if r not in selectable]
    if invalid:
        if _LANG == 'zh':
            print(f"⚠️  未知的 Rule Pack: {', '.join(invalid)}", file=sys.stderr)
            print(f"   可用的: {', '.join(sorted(selectable))}", file=sys.stderr)
        else:
            print(f"⚠️  Unknown Rule Packs: {', '.join(invalid)}", file=sys.stderr)
            print(f"   Available: {', '.join(sorted(selectable))}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)


def _handle_dry_run(config: dict, output_dir: str) -> None:
    """Handle --dry-run mode: preview files without writing."""
    is_zh = _LANG == 'zh'
    print("DRY RUN — " + ("以下檔案會被產生：" if is_zh else "The following files would be created:"))
    print()
    files = _preview_files(config, output_dir)
    for f in files:
        print(f"  {Path(f).relative_to(output_dir)}")
    print(f"\n  {'總計' if is_zh else 'Total'}: {len(files)}")
    sys.exit(EXIT_OK)



def _parser_choices(flag: str) -> list[str]:
    """The CLI's own `choices` for a flag, so the interactive path cannot drift.

    ⛔ These lists used to be a second, unbound copy. Nothing tied them to
    `_build_parser()`, and both `_build_*_apply_stage` end in `else:  # argocd`
    — so a deploy method added interactively-only would silently ship argocd
    YAML and never appear in the validated `--ci` x `--deploy` matrix, while one
    added to `--deploy` only would never be offered on the path this tool calls
    its default. Reading the parser makes "validated the moment it is added to
    the CLI" true instead of aspirational.
    """
    for action in _build_parser()._actions:
        if flag in (action.option_strings or []):
            return list(action.choices or [])
    raise SystemExit(f"init_project: no parser choices for {flag}")


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Split out of ``main()`` so the `--ci` × `--deploy` choice sets are
    readable as DATA. tests/ops/test_generated_ci_artifacts.py derives the
    combination matrix from these `choices` instead of hand-listing it, so a
    fourth deploy method is structurally validated the moment it is added
    rather than whenever someone remembers to extend a test list.
    """
    parser = argparse.ArgumentParser(
        description=_h('description'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_h('epilog'),
    )
    parser.add_argument('--ci', choices=['github', 'gitlab', 'both'],
                        default=None, help=_h('ci'))
    parser.add_argument('--tenants', default=None, help=_h('tenants'))
    parser.add_argument('--rule-packs', default=None, help=_h('rule_packs'))
    parser.add_argument('--deploy', choices=['kustomize', 'helm', 'argocd'],
                        default=None, help=_h('deploy'))
    parser.add_argument('-o', '--output-dir', default='.', help=_h('output_dir'))
    parser.add_argument('--non-interactive', action='store_true',
                        help=_h('non_interactive'))
    # ⛔ The help text used to say only ".da-init.yaml", which is the marker
    # file this flag lets you past — not the blast radius. `_write_file` has no
    # existence check, so a forced re-run rewrites conf.d/_defaults.yaml AND
    # every conf.d/<tenant>.yaml, discarding hand-tuned thresholds. That
    # understatement got worse with #1218: re-running is now the only in-tool
    # route to the corrected `_defaults.yaml`, so someone WILL reach for it.
    parser.add_argument('--force', action='store_true',
                        help='Re-run in an initialised directory: REWRITES every '
                             'generated file, including conf.d/_defaults.yaml and '
                             'each conf.d/<tenant>.yaml (hand edits are lost)'
                        if _LANG == 'en'
                        # ⚠️ 大寫「重寫所有產生的檔案」而非 markdown `**`：這是
                        # argparse help，會**原樣**印到終端機（實測 `--help` 輸出
                        # 帶著星號）。en 分支用大寫 REWRITES 正是為此，zh 分支卻
                        # 用了 markdown 語法——全 repo 唯一一處，不是慣例。
                        else '在已初始化的目錄重跑：會「重寫所有產生的檔案」，'
                             '含 conf.d/_defaults.yaml 與每一份 conf.d/<tenant>.yaml'
                             '（手動調整會遺失）')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what files would be created without writing'
                        if _LANG == 'en' else '顯示會產生的檔案但不寫入')
    parser.add_argument('--namespace', default='monitoring', help=_h('namespace'))
    parser.add_argument('--da-tools-image',
                        default=DA_TOOLS_IMAGE,
                        help=_h('da_tools_image'))
    parser.add_argument('--config-source',
                        choices=['configmap', 'git'], default='configmap',
                        help=_h('config_source'))
    parser.add_argument('--git-repo', default=None, help=_h('git_repo'))
    parser.add_argument('--git-branch', default='main', help=_h('git_branch'))
    parser.add_argument('--git-path', default='conf.d', help=_h('git_path'))
    parser.add_argument('--git-period', type=int, default=60,
                        help=_h('git_period'))

    return parser


def main():
    try_utf8_stdout()
    parser = _build_parser()

    args = parser.parse_args()

    _check_existing_init(args.output_dir, args.force, parser)
    config = _build_config_from_args(args, parser)
    _validate_config(config)

    output_dir = str(Path(args.output_dir).resolve())

    if args.dry_run:
        _handle_dry_run(config, output_dir)

    created = run_init(config, output_dir)
    _print_summary(created, output_dir, config)


if __name__ == "__main__":
    main()
