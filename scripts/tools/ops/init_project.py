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

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import detect_cli_lang, write_text_secure  # noqa: E402

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


def _h(key: str) -> str:
    return _HELP[key].get(_LANG, _HELP[key]['en'])


# ============================================================
# Rule Pack catalog (metric keys per rule pack)
# ============================================================
RULE_PACK_CATALOG = {
    'mariadb': {
        'label': 'MariaDB / MySQL',
        'defaults': {
            'mysql_connections': 80,
            'mysql_connections_critical': 150,
            'mysql_cpu': 80,
            'mysql_slow_queries': 10,
            'mysql_replication_lag': 30,
            'mysql_replication_lag_critical': 120,
            'mysql_aborted_connections': 50,
            'mysql_table_locks_waited': 100,
        },
    },
    'postgresql': {
        'label': 'PostgreSQL',
        'defaults': {
            'pg_connections': 80,
            'pg_connections_critical': 150,
            'pg_replication_lag': 30,
            'pg_replication_lag_critical': 120,
            'pg_cache_hit_ratio': 95,
            'pg_deadlocks': 5,
            'pg_long_queries': 300,
        },
    },
    'redis': {
        'label': 'Redis',
        'defaults': {
            'redis_memory_usage': 80,
            'redis_memory_usage_critical': 95,
            'redis_connected_clients': 500,
            'redis_evicted_keys': 100,
            'redis_keyspace_misses_ratio': 50,
        },
    },
    'mongodb': {
        'label': 'MongoDB',
        'defaults': {
            'mongodb_connections': 80,
            'mongodb_connections_critical': 150,
            'mongodb_replication_lag': 10,
            'mongodb_opcounters': 10000,
            'mongodb_page_faults': 100,
        },
    },
    'elasticsearch': {
        'label': 'Elasticsearch',
        'defaults': {
            'es_heap_usage': 80,
            'es_heap_usage_critical': 90,
            'es_cluster_status': 1,
            'es_pending_tasks': 50,
            'es_query_latency': 500,
            'es_indexing_latency': 200,
        },
    },
    'oracle': {
        'label': 'Oracle',
        'defaults': {
            'oracle_tablespace_used_percent': 85,
            'oracle_tablespace_used_percent_critical': 95,
            'oracle_active_sessions': 100,
            'oracle_blocking_sessions': 5,
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
            'kafka_consumer_lag_critical': 50000,
            'kafka_under_replicated_partitions': 0,
            'kafka_active_controllers': 1,
            'kafka_offline_partitions': 0,
        },
    },
    'rabbitmq': {
        'label': 'RabbitMQ',
        'defaults': {
            'rabbitmq_queue_messages': 10000,
            'rabbitmq_queue_messages_critical': 50000,
            'rabbitmq_consumers': 1,
            'rabbitmq_unacked_messages': 5000,
            'rabbitmq_memory_usage': 80,
        },
    },
    'jvm': {
        'label': 'JVM Applications',
        'defaults': {
            'jvm_heap_usage': 80,
            'jvm_heap_usage_critical': 95,
            'jvm_gc_pause': 500,
            'jvm_threads': 500,
        },
    },
    'nginx': {
        'label': 'Nginx',
        'defaults': {
            'nginx_error_rate': 5,
            'nginx_error_rate_critical': 15,
            'nginx_request_latency_p99': 1000,
            'nginx_active_connections': 1000,
        },
    },
    'kubernetes': {
        'label': 'Kubernetes',
        'defaults': {
            'container_cpu': 80,
            'container_cpu_critical': 95,
            'container_memory': 85,
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


def _gen_defaults_yaml(rule_packs: list[str], namespace: str) -> str:
    """Generate _defaults.yaml with selected rule pack defaults."""
    defaults = {}
    for rp in rule_packs:
        if rp in RULE_PACK_CATALOG:
            defaults.update(RULE_PACK_CATALOG[rp]['defaults'])

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

    config = {
        'defaults': defaults,
        'state_filters': state_filters,
        '_routing_defaults': routing_defaults,
    }

    header = textwrap.dedent("""\
    # _defaults.yaml — Platform global defaults
    # Managed by Platform Team. Tenant files should NOT contain this section.
    #
    # Three-state logic:
    #   - Custom value:  metric_key: 42     → Override platform default
    #   - Omitted:       (not in tenant YAML) → Use this default
    #   - Disable:       metric_key: "disable" → Suppress metric entirely
    #
    # Generated by: da-tools init
    # Rule Packs: {rule_packs}
    """).format(rule_packs=', '.join(rule_packs))

    return header + yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _gen_tenant_yaml(tenant: str, rule_packs: list[str]) -> str:
    """Generate a tenant stub YAML."""
    header = textwrap.dedent("""\
    # {tenant}.yaml — Tenant threshold overrides
    # Only the 'tenants' section is allowed in tenant files.
    # Omitted keys inherit from _defaults.yaml.
    # Set a key to "disable" to suppress that metric.
    #
    # Generated by: da-tools init
    """).format(tenant=tenant)

    tenant_config: dict = {}

    # Add a few example overrides from the first rule pack
    if rule_packs and rule_packs[0] in RULE_PACK_CATALOG:
        rp = RULE_PACK_CATALOG[rule_packs[0]]
        keys = list(rp['defaults'].keys())[:3]
        for k in keys:
            tenant_config[k] = str(rp['defaults'][k])

    # Add routing stub
    tenant_config['_routing'] = {
        'receiver': {
            'type': 'webhook',
            'url': f'https://webhook.{tenant}.example.com/alerts',
        },
    }

    config = {'tenants': {tenant: tenant_config}}
    return header + yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _gen_github_actions(
    namespace: str,
    da_tools_image: str,
    deploy_method: str,
) -> str:
    """Generate GitHub Actions workflow for Dynamic Alerting CI/CD."""

    kustomize_apply = ""
    helm_apply = ""
    argocd_apply = ""

    if deploy_method == 'kustomize':
        kustomize_apply = textwrap.dedent("""\

      # ── Stage 3: Apply (manual trigger only) ──────────────
      apply:
        needs: [validate, generate]
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
              kubectl rollout restart deployment/prometheus -n {namespace}
    """).format(namespace=namespace)
    elif deploy_method == 'helm':
        helm_apply = textwrap.dedent("""\

      # ── Stage 3: Apply via Helm (manual trigger only) ─────
      apply:
        needs: [validate, generate]
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
                -n {namespace} \\
                --wait --timeout 5m
    """).format(namespace=namespace)
    elif deploy_method == 'argocd':
        argocd_apply = textwrap.dedent("""\

      # ── Stage 3: Sync ArgoCD Application ──────────────────
      apply:
        needs: [validate, generate]
        runs-on: ubuntu-latest
        if: github.event_name == 'workflow_dispatch'
        environment: production
        steps:
          - name: Trigger ArgoCD sync
            run: |
              argocd app sync dynamic-alerting --prune --timeout 300
    """)

    apply_stage = kustomize_apply or helm_apply or argocd_apply

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
      push:
        branches: [main]
        paths:
          - 'conf.d/**'
      workflow_dispatch:
        inputs:
          dry_run:
            description: 'Dry-run mode (no actual apply)'
            type: boolean
            default: true

    env:
      DA_TOOLS_IMAGE: {da_tools_image}
      CONFIG_DIR: conf.d
      MONITORING_NS: {namespace}

    jobs:
      # ── Stage 1: Validate ─────────────────────────────────
      validate:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4

          - name: Validate config (schema + routing + policy)
            run: |
              docker run --rm \\
                -v ${{{{ github.workspace }}}}/conf.d:/data/conf.d:ro \\
                ${{{{ env.DA_TOOLS_IMAGE }}}} \\
                validate-config --config-dir /data/conf.d --ci

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
        steps:
          - uses: actions/checkout@v4

          - name: Prepare output directory
            run: mkdir -p .output

          - name: Generate Alertmanager routes
            run: |
              docker run --rm \\
                -v ${{{{ github.workspace }}}}/conf.d:/data/conf.d:ro \\
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
                -v ${{{{ github.workspace }}}}/conf.d:/data/conf.d:ro \\
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
    )


def _gen_gitlab_ci(
    namespace: str,
    da_tools_image: str,
    deploy_method: str,
) -> str:
    """Generate GitLab CI pipeline for Dynamic Alerting CI/CD."""

    apply_stage = ""
    if deploy_method == 'kustomize':
        apply_stage = textwrap.dedent("""\

    # ── Stage 3: Apply ───────────────────────────────────────
    apply:
      stage: apply
      image: bitnami/kubectl:latest
      environment:
        name: production
      rules:
        - when: manual
      script:
        - kustomize build kustomize/overlays/prod > /tmp/manifests.yaml
        - kubectl apply --dry-run=server -f /tmp/manifests.yaml
        - kubectl apply -f /tmp/manifests.yaml
        - kubectl rollout restart deployment/prometheus -n {namespace}
    """).format(namespace=namespace)
    elif deploy_method == 'helm':
        apply_stage = textwrap.dedent("""\

    # ── Stage 3: Apply via Helm ──────────────────────────────
    apply:
      stage: apply
      image: alpine/helm:latest
      environment:
        name: production
      rules:
        - when: manual
      script:
        - |
          helm upgrade --install threshold-exporter \\
            oci://ghcr.io/vencil/charts/threshold-exporter \\
            -f environments/prod/values.yaml \\
            -n {namespace} \\
            --wait --timeout 5m
    """).format(namespace=namespace)
    elif deploy_method == 'argocd':
        apply_stage = textwrap.dedent("""\

    # ── Stage 3: Sync ArgoCD Application ─────────────────────
    apply:
      stage: apply
      image: argoproj/argocd:latest
      environment:
        name: production
      rules:
        - when: manual
      script:
        - argocd app sync dynamic-alerting --prune --timeout 300
    """)

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
      CONFIG_DIR: conf.d
      MONITORING_NS: {namespace}

    # ── Stage 1: Validate ────────────────────────────────────
    validate-config:
      stage: validate
      image: $DA_TOOLS_IMAGE
      rules:
        - changes:
            - conf.d/**/*
            - rule-packs/**/*
      script:
        - da-tools validate-config --config-dir $CONFIG_DIR --ci

    lint-custom-rules:
      stage: validate
      image: $DA_TOOLS_IMAGE
      rules:
        - changes:
            - rule-packs/custom/**/*
          exists:
            - rule-packs/custom/
      script:
        - da-tools lint rule-packs/custom/ --ci
      allow_failure: true

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
        apply_stage=apply_stage,
    )


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
              image: registry.k8s.io/git-sync/git-sync:v4.4.0
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
              image: registry.k8s.io/git-sync/git-sync:v4.4.0
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


def _gen_precommit_snippet() -> str:
    """Generate .pre-commit-config.yaml snippet."""
    return (
        "# Dynamic Alerting pre-commit hooks\n"
        "# Add this to your existing .pre-commit-config.yaml\n"
        "# Generated by: da-tools init\n"
        "#\n"
        "# Validates tenant YAML on every commit (shift-left).\n"
        "\n"
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: da-validate-config\n"
        "        name: Validate Dynamic Alerting config\n"
        "        entry: >-\n"
        "          docker run --rm\n"
        "          -v ${PWD}/conf.d:/data/conf.d:ro\n"
        "          ghcr.io/vencil/da-tools:latest\n"
        "          validate-config --config-dir /data/conf.d --ci\n"
        "        language: system\n"
        "        files: ^conf\\.d/.*\\.ya?ml$\n"
        "        pass_filenames: false\n"
        "\n"
        "      - id: da-generate-routes\n"
        "        name: Generate Alertmanager routes (dry-run)\n"
        "        entry: >-\n"
        "          docker run --rm\n"
        "          -v ${PWD}/conf.d:/data/conf.d:ro\n"
        "          ghcr.io/vencil/da-tools:latest\n"
        "          generate-routes --config-dir /data/conf.d --dry-run --validate\n"
        "        language: system\n"
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
        ['github', 'gitlab', 'both'],
        'both',
    )

    deploy = _prompt_choice(
        "選擇部署方式:" if is_zh else "Select deployment method:",
        ['kustomize', 'helm', 'argocd'],
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
        'da_tools_image': 'ghcr.io/vencil/da-tools:latest',
    }


# ============================================================
# File writer
# ============================================================

def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_file(path: str, content: str, created_files: list[str]) -> None:
    """Write file with SAST-compliant writer, track in list."""
    _ensure_dir(os.path.dirname(path))
    write_text_secure(path, content)
    created_files.append(path)


# ============================================================
# Main orchestration
# ============================================================

def _preview_files(config: dict, output_dir: str) -> list[str]:
    """Return list of file paths that would be created (without writing)."""
    paths: list[str] = []
    ci, deploy = config['ci'], config['deploy']
    tenants = config['tenants']

    paths.append(os.path.join(output_dir, 'conf.d', '_defaults.yaml'))
    for t in tenants:
        paths.append(os.path.join(output_dir, 'conf.d', f'{t}.yaml'))
    if ci in ('github', 'both'):
        paths.append(os.path.join(output_dir, '.github', 'workflows', 'dynamic-alerting.yaml'))
    if ci in ('gitlab', 'both'):
        paths.append(os.path.join(output_dir, '.gitlab-ci.d', 'dynamic-alerting.yml'))
    if deploy == 'kustomize':
        paths.append(os.path.join(output_dir, 'kustomize', 'base', 'kustomization.yaml'))
        paths.append(os.path.join(output_dir, 'kustomize', 'base', 'README.md'))
        paths.append(os.path.join(output_dir, 'kustomize', 'overlays', 'dev', 'kustomization.yaml'))
        paths.append(os.path.join(output_dir, 'kustomize', 'overlays', 'prod', 'kustomization.yaml'))
    paths.append(os.path.join(output_dir, '.pre-commit-config.da.yaml'))
    paths.append(os.path.join(output_dir, '.da-init.yaml'))
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
    conf_dir = os.path.join(output_dir, 'conf.d')

    _write_file(
        os.path.join(conf_dir, '_defaults.yaml'),
        _gen_defaults_yaml(rule_packs, namespace),
        created,
    )

    for tenant in tenants:
        _write_file(
            os.path.join(conf_dir, f'{tenant}.yaml'),
            _gen_tenant_yaml(tenant, rule_packs),
            created,
        )

    # ── 2. CI/CD pipelines ─────────────────────────────────
    if ci in ('github', 'both'):
        _write_file(
            os.path.join(output_dir, '.github', 'workflows', 'dynamic-alerting.yaml'),
            _gen_github_actions(namespace, da_tools_image, deploy),
            created,
        )

    if ci in ('gitlab', 'both'):
        _write_file(
            os.path.join(output_dir, '.gitlab-ci.d', 'dynamic-alerting.yml'),
            _gen_gitlab_ci(namespace, da_tools_image, deploy),
            created,
        )

    # ── 3. Kustomize overlays ──────────────────────────────
    if deploy == 'kustomize':
        kust_base = os.path.join(output_dir, 'kustomize', 'base')
        _write_file(
            os.path.join(kust_base, 'kustomization.yaml'),
            _gen_kustomize_base(tenants, namespace),
            created,
        )

        # Copy conf.d files into kustomize base (symlink in production)
        # For now, generate a README explaining the setup
        _write_file(
            os.path.join(kust_base, 'README.md'),
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
                os.path.join(output_dir, 'kustomize', 'overlays', env_name, 'kustomization.yaml'),
                _gen_kustomize_overlay(env_name, namespace),
                created,
            )

    # ── 3b. GitOps Native Mode (git-sync sidecar) ──────────
    if config_source == 'git' and git_repo:
        gitsync_dir = os.path.join(output_dir, 'kustomize', 'overlays', 'gitops')
        _write_file(
            os.path.join(gitsync_dir, 'kustomization.yaml'),
            _gen_git_sync_kustomization(namespace),
            created,
        )
        _write_file(
            os.path.join(gitsync_dir, 'git-sync-patch.yaml'),
            _gen_git_sync_deployment(
                namespace, git_repo, git_branch, git_path, git_period,
            ),
            created,
        )

    # ── 4. Pre-commit config ───────────────────────────────
    _write_file(
        os.path.join(output_dir, '.pre-commit-config.da.yaml'),
        _gen_precommit_snippet(),
        created,
    )

    # ── 5. Marker file ─────────────────────────────────────
    _write_file(
        os.path.join(output_dir, '.da-init.yaml'),
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
        rel = os.path.relpath(f, output_dir)
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

def main():
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
    parser.add_argument('--force', action='store_true',
                        help='Overwrite existing .da-init.yaml' if _LANG == 'en'
                        else '覆寫既有的 .da-init.yaml')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what files would be created without writing'
                        if _LANG == 'en' else '顯示會產生的檔案但不寫入')
    parser.add_argument('--namespace', default='monitoring', help=_h('namespace'))
    parser.add_argument('--da-tools-image',
                        default='ghcr.io/vencil/da-tools:latest',
                        help=_h('da_tools_image'))
    parser.add_argument('--config-source',
                        choices=['configmap', 'git'], default='configmap',
                        help=_h('config_source'))
    parser.add_argument('--git-repo', default=None, help=_h('git_repo'))
    parser.add_argument('--git-branch', default='main', help=_h('git_branch'))
    parser.add_argument('--git-path', default='conf.d', help=_h('git_path'))
    parser.add_argument('--git-period', type=int, default=60,
                        help=_h('git_period'))

    args = parser.parse_args()

    # Check for existing init
    marker_path = os.path.join(args.output_dir, '.da-init.yaml')
    if os.path.isfile(marker_path) and not args.force:
        if _LANG == 'zh':
            print(f"⚠️  此目錄已初始化 ({marker_path})。", file=sys.stderr)
            print("   使用 --force 覆寫或手動刪除 .da-init.yaml。", file=sys.stderr)
        else:
            print(f"⚠️  This directory is already initialized ({marker_path}).", file=sys.stderr)
            print("   Use --force to overwrite or remove .da-init.yaml manually.", file=sys.stderr)
        sys.exit(1)

    # Determine mode — build config from CLI args or interactive flow
    # Validate --config-source git requirements
    if args.config_source == 'git' and not args.git_repo:
        parser.error("--config-source git requires --git-repo <url>")

    has_cli_args = args.ci or args.tenants or args.rule_packs or args.deploy
    if args.non_interactive or has_cli_args:
        if args.non_interactive and not args.tenants:
            parser.error("--non-interactive requires --tenants")
        config = {
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
        sys.exit(1)

    # Reject empty tenant list
    if not config['tenants']:
        if _LANG == 'zh':
            print("⚠️  至少需要一個租戶名稱", file=sys.stderr)
        else:
            print("⚠️  At least one tenant name is required", file=sys.stderr)
        sys.exit(1)

    # Filter out auto-enabled packs from user selection (they're always included)
    # but don't reject them as invalid if user explicitly typed them
    selectable = set(_selectable_rule_packs())
    auto = set(_auto_enabled_rule_packs())
    config['rule_packs'] = [r for r in config['rule_packs'] if r not in auto]

    # Validate rule packs
    invalid = [r for r in config['rule_packs'] if r not in selectable]
    if invalid:
        if _LANG == 'zh':
            print(f"⚠️  未知的 Rule Pack: {', '.join(invalid)}", file=sys.stderr)
            print(f"   可用的: {', '.join(sorted(selectable))}", file=sys.stderr)
        else:
            print(f"⚠️  Unknown Rule Packs: {', '.join(invalid)}", file=sys.stderr)
            print(f"   Available: {', '.join(sorted(selectable))}", file=sys.stderr)
        sys.exit(1)

    output_dir = os.path.abspath(args.output_dir)

    if args.dry_run:
        is_zh = _LANG == 'zh'
        print("DRY RUN — " + ("以下檔案會被產生：" if is_zh else "The following files would be created:"))
        print()
        # Simulate without writing
        files = _preview_files(config, output_dir)
        for f in files:
            print(f"  {os.path.relpath(f, output_dir)}")
        print(f"\n  {'總計' if is_zh else 'Total'}: {len(files)}")
        sys.exit(0)

    created = run_init(config, output_dir)
    _print_summary(created, output_dir, config)


if __name__ == "__main__":
    main()
