#!/usr/bin/env python3
"""generate_platform_data.py — 共用平台資料產生器

從 rule-packs/*.yaml（權威 recording/alert 計數）、scaffold_tenant.py（metadata）
萃取出 docs/assets/platform-data.json，供所有 JSX 互動工具共用。

消除 JSX 工具間的硬編碼數據不一致問題。

Usage:
    python3 scripts/tools/dx/generate_platform_data.py              # 產生 JSON
    python3 scripts/tools/dx/generate_platform_data.py --check      # CI 模式（drift 偵測）
    python3 scripts/tools/dx/generate_platform_data.py --dry-run    # 只印出 JSON 不寫檔
"""
import argparse
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import yaml

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
import _lib_consumer_read  # noqa: E402  (#3206: rule files as Prometheus reads them)
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_VIOLATION, OutputNotSerializable, Report, encode_json  # noqa: E402
import _lib_consumer_read  # noqa: E402  (ADR-036 §12: Chart.yaml, read Helm's way)
from _lib_tenant_values import (  # noqa: E402  (#2115 0-B/B4)
    DaGuardError,
    DaGuardNotFoundError,
    ParseFailedError,
    now_at,
    print_load_error,
    recorded_at,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

# #3471: where the rule packs and the rules ConfigMaps sit, and which files
# there are rule files, is the one declaration (`_lib_consumer_read.RULE_SOURCES`).
RULE_PACKS_DIR = REPO_ROOT / _lib_consumer_read.RULE_PACKS.directory
K8S_RULES_DIR = REPO_ROOT / _lib_consumer_read.CONFIGMAP_RULES.directory
OUTPUT_PATH = REPO_ROOT / "docs" / "assets" / "platform-data.json"

# ⛔ The portal's OFFLINE fallback, generated from the same build (#1226 §2).
# It used to be a hand-typed object literal inside `rule-packs.js` mirroring the
# `rulePacks` section of the file above, and nothing regenerated it: #1215 added
# `mysql_replication_lag` to the MariaDB defaults and the mirror never got it,
# which shipped a portal that showed one set of thresholds online and another
# offline. `dev-rules.md`'s "add / change a Rule Pack" SOP never mentioned the
# mirror either, so following the SOP could not have caught it.
#
# ⚠️ The file is JSON rather than a `.js` module on purpose — the same shape
# `recipe-status.json` next to it already uses, so there is one convention for
# "generated data the portal imports", and `check_jsx_loader_compat.py` passes
# relative imports without an allowlist entry.
FALLBACK_PATH = (
    REPO_ROOT / "tools" / "portal" / "src" / "interactive" / "tools"
    / "_common" / "data" / "rule-packs-fallback.json"
)

# The fields the offline fallback carries, and the ONLY place that list lives on
# the Python side. ⛔ It must stay equal to `carried()` in
# `tools/portal/tests/rule-packs-fallback-drift.test.ts`, which is what compares
# the two files in the portal's own suite; `test_the_fallback_projection_matches_
# the_drift_gates_field_list` reads that test file and holds the two to each
# other, so adding a field here without teaching that gate is a red, not a
# silent one-sided widening.
#
# ⚠️ `display` / `exporterFull` / `defaultOn` are deliberately absent: no
# in-browser tool reads them, and carrying them would make the offline bundle
# larger for nothing.
_FALLBACK_FIELDS: tuple[tuple[str, object], ...] = (
    ("label", None),
    ("category", None),
    ("exporter", None),
    ("configMap", None),
    ("recordingRules", None),
    ("alertRules", None),
    ("required", False),
    ("defaults", {}),
    ("metrics", []),
    ("dependencies", None),
)


def _display_path(path: Path) -> str:
    """Repo-relative when it can be, absolute otherwise.

    ⚠️ `Path.relative_to` RAISES for a path outside the repository, and the
    raise lands inside an error message — so the tool would die with a
    ValueError while trying to tell the operator what was wrong. Measured: a
    test that points `FALLBACK_PATH` at a temp file (the only honest way to
    drive the `--check` failure branch) hit exactly that.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def build_fallback(data: dict) -> dict:
    """Project `platform-data.json` down to what the portal needs offline.

    ⛔ Pack ORDER is preserved from `packOrder`, and the per-pack `defaults` key
    order is preserved as the source has it: `getAllMetricKeys` iterates
    `Object.entries(pack.defaults)`, so that order is user-visible in
    autocomplete and validation lists. A dict comprehension over `sorted(...)`
    here would have reordered every pack's keys with nothing red to say so.
    """
    packs = data["rulePacks"]
    order = data["packOrder"]
    missing = [p for p in order if p not in packs]
    assert not missing, f"packOrder names packs that do not exist: {missing}"
    assert set(order) == set(packs), (
        f"packOrder and rulePacks disagree: {sorted(set(packs) ^ set(order))}"
    )
    return {
        "_generated": (
            "GENERATED from docs/assets/platform-data.json by "
            "generate_platform_data.py — DO NOT EDIT. Run `make platform-data`."
        ),
        "packOrder": list(order),
        "rulePacks": {
            pack_id: {
                field: (packs[pack_id].get(field)
                        if packs[pack_id].get(field) is not None else default)
                for field, default in _FALLBACK_FIELDS
            }
            for pack_id in order
        },
    }
SCAFFOLD_PATH = SCRIPT_DIR.parent / "ops" / "scaffold_tenant.py"


# ---------------------------------------------------------------------------
# Load scaffold_tenant.py RULE_PACKS without importing the whole module
# ---------------------------------------------------------------------------
def load_scaffold_rule_packs() -> dict:
    """Import RULE_PACKS from scaffold_tenant.py."""
    spec = importlib.util.spec_from_file_location("scaffold_tenant", str(SCAFFOLD_PATH))
    if spec is None or spec.loader is None:
        print(f"WARNING: Cannot load {SCAFFOLD_PATH}", file=sys.stderr)
        return {}
    mod = importlib.util.module_from_spec(spec)
    # Prevent actual execution of main logic
    sys.modules["scaffold_tenant"] = mod
    try:
        spec.loader.exec_module(mod)
    except (ImportError, AttributeError) as e:
        print(f"WARNING: Error loading scaffold_tenant: {e}", file=sys.stderr)
        return {}
    return getattr(mod, "RULE_PACKS", {})


# ---------------------------------------------------------------------------
# Count rules from YAML source files (authoritative)
# ---------------------------------------------------------------------------
def _count(groups: list) -> tuple:
    """(recording, alert) rules in *groups*, as Prometheus loads them."""
    rec = alert = 0
    for g in groups:
        for r in g.get("rules") or []:
            if "alert" in r:
                alert += 1
            elif "record" in r:
                rec += 1
    return rec, alert


def gather_rule_counts() -> dict:
    """Gather authoritative rule counts from the rule packs and the rules
    ConfigMaps, each read the way its consumer reads it (#3206): one
    `da-read` run for the packs, one for the ConfigMaps. Any rule file
    Prometheus would refuse raises `RuleFilesRefused` — a generator does not
    count around it."""
    packs = {}

    # rule-packs/ directory
    pack_files = _lib_consumer_read.RULE_PACKS.files_in(RULE_PACKS_DIR)
    for f, rf in zip(pack_files, _lib_consumer_read.prometheus_rule_files(pack_files)):
        name = _lib_consumer_read.RULE_PACKS.member(f)
        rec, alert = _count(rf.groups)
        packs[name] = {"recording": rec, "alert": alert}

    # k8s ConfigMaps (may have additional rules, e.g. platform)
    cm_files = _lib_consumer_read.CONFIGMAP_RULES.files_in(K8S_RULES_DIR)
    per_cm: dict = {str(f): [0, 0] for f in cm_files}
    for rf in _lib_consumer_read.require_read(
            _lib_consumer_read.manifest_rule_file_results(cm_files)):
        if rf.kind == "ConfigMap":
            r, a = _count(rf.groups)
            per_cm[rf.manifest][0] += r
            per_cm[rf.manifest][1] += a
    for f in cm_files:
        name = _lib_consumer_read.CONFIGMAP_RULES.member(f)
        rec, alert = per_cm[str(f)]
        if name not in packs:
            packs[name] = {"recording": rec, "alert": alert}
        else:
            packs[name]["recording"] = max(packs[name]["recording"], rec)
            packs[name]["alert"] = max(packs[name]["alert"], alert)

    return packs


# ---------------------------------------------------------------------------
# Category & dependency metadata
# ---------------------------------------------------------------------------
CATEGORIES = {
    "database": {"en": "Databases", "zh": "資料庫"},
    "messaging": {"en": "Messaging", "zh": "訊息佇列"},
    "runtime": {"en": "Runtime Environments", "zh": "運行環境"},
    "webserver": {"en": "Web Servers", "zh": "網頁伺服器"},
    "infrastructure": {"en": "Infrastructure", "zh": "基礎設施"},
}

PACK_CATEGORY = {
    "mariadb": "database",
    "postgresql": "database",
    "redis": "database",
    "mongodb": "database",
    "elasticsearch": "database",
    "oracle": "database",
    "db2": "database",
    "clickhouse": "database",
    "kafka": "messaging",
    "rabbitmq": "messaging",
    "jvm": "runtime",
    "nginx": "webserver",
    "kubernetes": "infrastructure",
    "liveness": "infrastructure",
    "operational": "infrastructure",
    "platform": "infrastructure",
}

# Short exporter name for UI display (label column in matrix)
EXPORTER_SHORT = {
    "mariadb": "mysqld_exporter",
    "postgresql": "postgres_exporter",
    "redis": "redis_exporter",
    "mongodb": "mongodb_exporter",
    "elasticsearch": "elasticsearch_exporter",
    "oracle": "oracledb_exporter",
    "db2": "db2_exporter",
    "clickhouse": "built-in /metrics",
    "kafka": "kafka_exporter",
    "rabbitmq": "rabbitmq_exporter",
    "jvm": "jmx_exporter",
    "nginx": "nginx-prometheus-exporter",
    "kubernetes": "cAdvisor + kube-state-metrics",
    "liveness": "threshold-exporter",
    "operational": "threshold-exporter",
    "platform": "threshold-exporter",
}

# Display labels
PACK_LABELS = {
    "mariadb": "MariaDB/MySQL",
    "postgresql": "PostgreSQL",
    "redis": "Redis",
    "mongodb": "MongoDB",
    "elasticsearch": "Elasticsearch",
    "oracle": "Oracle",
    "db2": "DB2",
    "clickhouse": "ClickHouse",
    "kafka": "Kafka",
    "rabbitmq": "RabbitMQ",
    "jvm": "JVM",
    "nginx": "Nginx",
    "kubernetes": "Kubernetes",
    "liveness": "Exporter Liveness",
    "operational": "Operational",
    "platform": "Platform",
}

# Key metrics covered (for matrix display)
METRICS_COVERAGE = {
    # mariadb: "cpu" + "threads_running" coexist during the #1231 2-release
    # alias window (the exporter dual-emits user_threshold{metric="cpu"} next
    # to {metric="threads_running"}); drop "cpu" when the window closes.
    "mariadb": ["connections", "cpu", "threads_running", "memory", "slow_queries", "replication_lag", "query_errors"],
    "postgresql": ["connections", "cache_hit", "query_time", "disk_usage", "replication_lag"],
    "redis": ["memory", "evictions", "connected_clients", "keyspace_hits"],
    "mongodb": ["connections", "memory", "page_faults", "replication"],
    "elasticsearch": ["heap", "unassigned_shards", "cluster_health", "indexing_rate"],
    "oracle": ["sessions", "tablespace", "wait_events", "redo_log"],
    "db2": ["connections", "bufferpool", "tablespace", "lock_waits"],
    "clickhouse": ["queries", "merges", "replicated_lag", "memory"],
    "kafka": ["consumer_lag", "broker_active", "controller", "isr_shrink", "under_replicated"],
    "rabbitmq": ["queue_depth", "consumers", "memory", "disk_free", "connections"],
    "jvm": ["gc_pause", "heap_usage", "thread_pool", "class_loading"],
    "nginx": ["active_connections", "request_rate", "connection_backlog"],
    "kubernetes": ["pod_restart", "cpu_limit", "memory_limit", "pvc_usage"],
    "liveness": ["expected_exporter", "exporter_up"],
    "operational": ["exporter_health", "config_reload"],
    "platform": ["threshold_metric_count", "recording_rule_health", "scrape_success"],
}

# Dependency suggestions (for rule-pack-selector)
DEPENDENCIES = {
    "mariadb": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "postgresql": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "redis": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "mongodb": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "elasticsearch": {"suggests": ["kubernetes", "jvm"], "reason": {"en": "ES runs on JVM; K8s monitors container resources", "zh": "ES 運行在 JVM 上；K8s 監控容器資源"}},
    "oracle": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "db2": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "clickhouse": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement DB monitoring", "zh": "容器資源告警補充 DB 監控"}},
    "kafka": {"suggests": ["kubernetes", "jvm"], "reason": {"en": "Kafka brokers run on JVM; K8s monitors pods", "zh": "Kafka broker 運行在 JVM 上；K8s 監控 Pod"}},
    "rabbitmq": {"suggests": ["kubernetes"], "reason": {"en": "Container resource alerts complement MQ monitoring", "zh": "容器資源告警補充 MQ 監控"}},
    "jvm": {"suggests": ["kubernetes"], "reason": {"en": "JVM apps typically run in K8s pods", "zh": "JVM 應用通常運行在 K8s Pod 中"}},
    "nginx": {"suggests": ["kubernetes"], "reason": {"en": "Ingress/proxy pods benefit from K8s resource alerts", "zh": "Ingress/proxy Pod 受益於 K8s 資源告警"}},
}

# Required packs (always included, cannot be deselected). Every declared
# platform pack is required (#1595 D1a: `PLATFORM_PACKS` is the one place that
# names them); operational and liveness are required TENANT packs — required
# is not platform.
REQUIRED_PACKS = {"operational", "liveness"} | _lib_consumer_read.PLATFORM_PACKS

# Default-on packs (selected by default for new tenants)
DEFAULT_ON_PACKS = {"kubernetes", "mariadb"}

# Canonical order for display
PACK_ORDER = [
    "mariadb", "postgresql", "redis", "mongodb",
    "elasticsearch", "oracle", "db2", "clickhouse",
    "kafka", "rabbitmq", "jvm", "nginx",
    "kubernetes", "liveness", "operational", "platform",
]


# ---------------------------------------------------------------------------
# Tenant metadata integration (v2.3.0)
# ---------------------------------------------------------------------------
class TenantMetadataError(RuntimeError):
    """Tenant metadata 真正載入/建構失敗（有別於「刻意缺席」的 fallback）。

    租戶清單與值來自 da-guard served-values（#2115 0-B／B4）：da-guard 不在、
    失敗、或 exporter 丟掉某個檔，都是這一類——`__cause__` 是
    `_lib_tenant_values` 的例外，main 以 rc 2 印出 da-guard 的 stderr。"""


def _load_tenant_metadata(at: str | None = None) -> tuple:
    """Load tenant metadata via generate_tenant_metadata module.

    Returns (tenant_groups, tenant_metadata) dicts.

    **刻意缺席** → 回傳空 dict，不視為錯誤（保留既有寬容行為）：
      - 沒有 conf.d 目錄（例如只 checkout 文件子集）
      - generate_tenant_metadata.py 不存在或取不到 loader
      - 該 module 沒有 build_tenant_metadata()

    **真正的失敗**（import error、build_tenant_metadata 拋例外，含 da-guard
    缺席／失敗／exporter 丟檔，#2115 0-B／B4）→ 丟 TenantMetadataError。
    ⛔ da-guard 缺席**不是**刻意缺席：conf.d 在、卻讀不到 exporter 的答案時
    回空 dict，就是靜默產出缺 tenant metadata 的 platform-data.json。dev-rules #5 fail-loud：不可靜默吞掉，否則
    build_platform_data() 會在 `if tenant_groups or tenant_metadata:` 下
    整個略過這兩個 key，產出「缺 tenant metadata 卻 exit 0」的
    platform-data.json——一旦這種產物被 commit 進來，drift gate 比對的
    是同樣殘缺的輸出，就再也擋不住了。
    """
    meta_path = SCRIPT_DIR / "generate_tenant_metadata.py"
    if not meta_path.exists():
        return {}, {}
    spec = importlib.util.spec_from_file_location("generate_tenant_metadata", str(meta_path))
    if spec is None or spec.loader is None:
        return {}, {}

    # conf.d 缺席是刻意 fallback：在載入 module 前就先判掉。
    config_dir = REPO_ROOT / "components" / "threshold-exporter" / "config" / "conf.d"
    if not config_dir.is_dir():
        return {}, {}

    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        build_fn = getattr(mod, "build_tenant_metadata", None)
        if build_fn is None:
            return {}, {}

        data = build_fn(config_dir, at=at)
    except Exception as exc:  # noqa: BLE001
        raise TenantMetadataError(
            f"tenant metadata generation failed: {exc}") from exc

    return data.get("tenant_groups", {}), data.get("tenant_metadata", {})


# ---------------------------------------------------------------------------
# Build platform-data.json
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Container images the Deployment Wizard emits into a customer's values.yaml
# ---------------------------------------------------------------------------
# WHY THIS IS GENERATED (#1245): the wizard used to carry these as literals and
# they rotted to a v2.7.0-era snapshot nobody noticed — prom/prometheus v2.52.0
# against a shipped v3.13.1 (a whole MAJOR, and 2.x has the pre-3.0 left-CLOSED
# lookback our rule-pack fixtures are calibrated against), alertmanager v0.27.0
# vs v0.33.1, `jimmidyson/configmap-reload` after that component was replaced
# outright, and `oauth2-proxy/oauth2-proxy:v7.6.0` which does not even EXIST on
# Docker Hub (the ref is missing its `quay.io/` registry, so the emitted YAML
# ImagePullBackOffs). Same failure shape as the four hand-maintained rule-pack
# count tables that PR-3 converged (#1224) — a duplicate view with no gate.
#
# TAG ONLY, never a digest: this is a customer's STARTING values.yaml. A digest
# would freeze it at the instant we generated it, and — worse — silently defeat
# the customer's own `--set image.tag=` bump, because the digest wins. That is
# the exact footgun helm/*/values.yaml carries a ⛔ warning about.
#
# Third-party tags come from the deploy SSOT (check_image_refs_resolve.py reads
# the real helm values / k8s manifests); first-party tags from each chart's
# appVersion. A mapped repo that vanishes from the SSOT is a HARD ERROR, not a
# silently dropped key — that is precisely how the configmap-reload replacement
# would have slipped through again.
IMAGE_REFS_EXTRACTOR = SCRIPT_DIR.parent.parent / "ops" / "check_image_refs_resolve.py"

WIZARD_THIRD_PARTY = {
    "prometheus": "prom/prometheus",
    "alertmanager": "prom/alertmanager",
    "configReloader": "quay.io/prometheus-operator/prometheus-config-reloader",
    "oauth2Proxy": "quay.io/oauth2-proxy/oauth2-proxy",
}

WIZARD_FIRST_PARTY = {
    "thresholdExporter": ("ghcr.io/vencil/threshold-exporter", "threshold-exporter"),
    "daPortal": ("ghcr.io/vencil/da-portal", "da-portal"),
    "tenantApi": ("ghcr.io/vencil/tenant-api", "tenant-api"),
}


class WizardImagesInconsistent(Exception):
    """The repo's own sources disagree about a wizard image (two tags for one
    repo, a mapping the SSOT no longer holds, an appVersion off-convention): a
    finding in the repo — main() exits 1 (scripts/tools/_lib_exitcodes.py)."""


class WizardImagesUnreadable(Exception):
    """A wizard image's source could not be read (chart gone, or not readable
    the way Helm reads it): no verdict — main() exits 2 (#3577, #3611)."""


def _deployed_third_party_tags() -> dict:
    """repo -> tag, from the deploy SSOT (digest stripped)."""
    spec = importlib.util.spec_from_file_location(
        "_check_image_refs_resolve", IMAGE_REFS_EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = {}
    for ref in sorted(mod.discover_refs(REPO_ROOT)):
        without_digest = ref.split("@", 1)[0]
        repo, _, tag = without_digest.rpartition(":")
        if not (repo and tag):
            continue
        # discover_refs returns a SET, so iteration order is not stable. If the
        # same repo were pinned at two tags (mid-migration, or one manifest left
        # behind), a plain `out[repo] = tag` would let whichever entry came last
        # win — silently, and differently between runs. That is the exact silent
        # drift this module exists to prevent, so make it a hard error instead.
        if repo in out and out[repo] != tag:
            raise WizardImagesInconsistent(
                f"ERROR: {repo!r} is pinned at two different tags in the deploy "
                f"SSOT: {out[repo]!r} and {tag!r}. The wizard cannot pick one for "
                f"the customer — reconcile the manifests/values first."
            )
        out[repo] = tag
    return out


def _chart_yaml(chart_dir: str) -> tuple[Path, dict]:
    """helm/<chart_dir>/Chart.yaml as Helm reads it: (path, the one mapping)."""
    chart = REPO_ROOT / "helm" / chart_dir / "Chart.yaml"
    # Same fail-loud contract as the third-party path above: a renamed/removed
    # chart must say so, not surface as a bare FileNotFoundError traceback.
    if not chart.is_file():
        raise WizardImagesUnreadable(
            f"ERROR: {chart} not found — the wizard maps an image or a pinned "
            f"release to chart {chart_dir!r}. Update WIZARD_FIRST_PARTY / "
            f"WIZARD_CHARTS if it was renamed."
        )
    # ADR-036 §12 (#3206): Chart.yaml as Helm reads it (sigs.k8s.io/yaml,
    # da-crdecode). An unquoted `appVersion: 1.10` is the number 1.1 there,
    # as it is to Helm; the convention check below sees what Helm sees.
    try:
        docs = _lib_consumer_read.any_yaml_documents(str(chart))  # no manifest (#3426)
    except _lib_consumer_read.ConsumerReadError as e:
        detail = e.detail()
        raise WizardImagesUnreadable(f"ERROR: cannot read {chart} the way Helm does: {detail}") from e
    data = docs[0] if len(docs) == 1 and isinstance(docs[0], dict) else None
    if data is None:
        raise WizardImagesInconsistent(f"ERROR: {chart} is not one mapping in Helm's reading")
    return chart, data


def _chart_app_version(chart_dir: str) -> str:
    _chart, data = _chart_yaml(chart_dir)
    app = data.get("appVersion", "")
    if not isinstance(app, str):
        raise WizardImagesInconsistent(
            f"ERROR: helm/{chart_dir}/Chart.yaml appVersion is {app!r} in Helm's reading, "
            f"not a string; quote it (appVersion: \"{app}\").")
    app = app.strip()
    if not app:
        raise WizardImagesInconsistent(f"ERROR: helm/{chart_dir}/Chart.yaml has no appVersion")
    # The caller prefixes "v". A chart that already carries one would yield
    # "vv2.9.0" — reject it rather than silently lstrip()-ing, because the repo
    # convention is a bare appVersion and a stray "v" is worth surfacing.
    if app.startswith("v"):
        raise WizardImagesInconsistent(
            f"ERROR: helm/{chart_dir}/Chart.yaml appVersion is {app!r}; the repo "
            f"convention is a bare version (the 'v' is added when composing the "
            f"image tag). Drop the leading 'v' or adjust build_wizard_images()."
        )
    return app


def build_wizard_images() -> dict:
    """Image refs (repo:tag, no digest) for the Deployment Wizard."""
    deployed = _deployed_third_party_tags()
    images = {}

    for key, repo in WIZARD_THIRD_PARTY.items():
        tag = deployed.get(repo)
        if tag is None:
            raise WizardImagesInconsistent(
                f"ERROR: wizard image {key!r} maps to {repo!r}, which the deploy "
                f"SSOT no longer contains. The image was probably replaced or "
                f"renamed — update WIZARD_THIRD_PARTY (and the wizard's offline "
                f"mirror) instead of letting the wizard emit a dead ref.\n"
                f"       SSOT currently holds: {sorted(deployed)}"
            )
        images[key] = f"{repo}:{tag}"

    for key, (repo, chart_dir) in WIZARD_FIRST_PARTY.items():
        images[key] = f"{repo}:v{_chart_app_version(chart_dir)}"

    return images


# ---------------------------------------------------------------------------
# Chart versions the Deployment Wizard pins in its `helm upgrade --install`
# ---------------------------------------------------------------------------
# WHY (#2947): the wizard's install command carried no `--version`, so a
# customer copying it installed whatever chart the registry held as newest on
# that day. The wizard's offline mirror (`_common/data/charts.js`) is held to
# this block by `charts-fallback-drift.test.ts`.
#
# ⛔ NOT derived from Chart.yaml on every run, unlike `images`. A chart's
# `version:` moves with feature PRs between releases (tenant-api: 2.9.25 in
# the tree while the registry held 2.9.8), and `helm … --version <unpublished>`
# does not degrade — it fails. So an ordinary run CARRIES the block already in
# platform-data.json, and only the release wrap-up rewrites it from Chart.yaml
# (`make platform-data RELEASE_CHARTS=1`), when those versions are the ones
# about to be published. `make pre-tag` runs `--check --release-charts`, which
# refuses a tag whose pins are not the charts being tagged.
#
# One entry per chart the wizard emits a release for. A chart added to the
# wizard without an entry here throws in the browser (`chartVersion()` refuses
# an unknown chart) instead of emitting a floating install.
WIZARD_CHARTS = ("threshold-exporter", "tenant-api")

# chart -> the release line whose tag (`<line>/v<version>`) publishes it
# (.github/workflows/release.yaml).
WIZARD_CHART_TAG_LINE = {"threshold-exporter": "exporter", "tenant-api": "tenant-api"}

# Exactly what `helm … --version` takes as ONE release: no range, no leading
# "v". Same grammar the init pin guard accepts (tests/ops/test_init_project.py).
_EXACT_CHART_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?")


def _chart_version(chart_dir: str) -> str:
    chart, data = _chart_yaml(chart_dir)
    version = data.get("version", "")
    if not isinstance(version, str):
        raise WizardImagesInconsistent(
            f"ERROR: helm/{chart_dir}/Chart.yaml version is {version!r} in Helm's reading, "
            f"not a string; quote it (version: \"{version}\").")
    if not _EXACT_CHART_VERSION.fullmatch(version):
        raise WizardImagesInconsistent(
            f"ERROR: helm/{chart_dir}/Chart.yaml version is {version!r}; the wizard "
            f"pins it with `--version`, which needs one exact X.Y.Z release.")
    return version


def build_wizard_charts() -> dict:
    """chart name -> the chart's own version: what a release publishes."""
    return {name: _chart_version(name) for name in WIZARD_CHARTS}


def _tag_exists(tag: str) -> bool:
    """Is `tag` in this checkout? A git that cannot answer is "no": the caller
    then refuses, which is the safe side for a pin nobody could confirm."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
            capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def unpublishable_chart_pins(pinned: dict, tree: dict) -> tuple[list[str], list[str]]:
    """(violations, notes) for the pins a tag is about to ship.

    A pin is fine when it is the chart in the tree (this release publishes
    it) or when its tag exists (an earlier release did). Anything else names a
    chart no registry will hold. A pin behind the tree is reported as a note:
    right when that line is not part of this release, stale when it is.
    """
    bad, notes = [], []
    for name in WIZARD_CHARTS:
        pin, cur = pinned.get(name), tree[name]
        if pin == cur:
            continue
        tag = f"{WIZARD_CHART_TAG_LINE[name]}/v{pin}"
        if isinstance(pin, str) and _EXACT_CHART_VERSION.fullmatch(pin) and _tag_exists(tag):
            notes.append(f"{name}: pinned {pin} (tag {tag}), tree has {cur} — correct only "
                         f"if this release does not publish {name}")
        else:
            bad.append(f"{name}: pinned {pin!r}, tree has {cur}, and tag {tag} is not in "
                       f"this checkout (fetch tags if it was published)")
    return bad, notes


def carried_wizard_charts() -> dict:
    """The `charts` block platform-data.json already carries, unchanged.

    It must name exactly WIZARD_CHARTS, each at one exact version: a block
    that lost a chart or holds a range would reach the customer as a missing
    or floating `--version`.
    """
    how = ("Write it from Chart.yaml with `make platform-data RELEASE_CHARTS=1` "
           "(release wrap-up), or restore the published versions by hand.")
    try:
        existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise WizardImagesInconsistent(
            f"ERROR: {_display_path(OUTPUT_PATH)} does not exist, so there are no "
            f"released chart versions to carry. {how}") from None
    except (OSError, ValueError) as e:
        raise WizardImagesUnreadable(
            f"ERROR: cannot read {_display_path(OUTPUT_PATH)} for its `charts` block: {e}") from e
    charts = existing.get("charts") if isinstance(existing, dict) else None
    if not isinstance(charts, dict) or sorted(charts) != sorted(WIZARD_CHARTS):
        raise WizardImagesInconsistent(
            f"ERROR: {_display_path(OUTPUT_PATH)} `charts` is {charts!r}; it must name "
            f"exactly {sorted(WIZARD_CHARTS)}. {how}")
    for name, version in charts.items():
        if not isinstance(version, str) or not _EXACT_CHART_VERSION.fullmatch(version):
            raise WizardImagesInconsistent(
                f"ERROR: {_display_path(OUTPUT_PATH)} `charts.{name}` is {version!r}, "
                f"not one exact X.Y.Z release. {how}")
    return {name: charts[name] for name in WIZARD_CHARTS}


def build_declared_keys(scaffold_packs: dict) -> dict:
    """Top-level `declaredKeys`: keys the platform RECOGNISES but gives no value.

    Registry tier `optional_overrides`, flat spellings only — membership comes
    from `_registry_lib.is_shipped_optional_key`, the SAME predicate that builds
    the shipped `optional_overrides:` list and the two onboarding generators'
    output. No second spelling of the rule, no hand-copied key list.

    ⛔ Deliberately NOT nested under `rulePacks[*]` — but not for the reason it
    first looks like. `rule-packs-fallback-drift.test.ts` does NOT gate a new
    per-pack field: its `carried()` is a ten-field WHITELIST, so an extra field
    is dropped on both sides and never compared. That IS the problem. A
    per-pack field would still need hand-copying into the inline fallback in
    `rule-packs.js` for the offline path to work, with nothing to catch it if
    you forgot — a fresh, ungated hand-maintained copy of a generated contract,
    in the very line of work that exists to delete them (and #1226 has not yet
    settled how that mirror should be generated). A top-level field carries its
    own drift gate instead (`declared-keys.test.ts`), the same shape `images.js`
    already uses.

    ⛔ `value` is a REFERENCE number for display only. The platform does not
    assert it: ADR-030's blind-write reference library measured several of these
    numbers false-alarming on benign load (#1176), which is precisely why the
    keys carry no platform value. Never pre-fill one for a tenant.
    """
    sys.path.insert(0, str(SCAFFOLD_PATH.parent))
    from _registry_lib import is_shipped_optional_key  # noqa: E402

    declared: dict = {}
    for pack_id in PACK_ORDER:
        entries = (scaffold_packs.get(pack_id) or {}).get("optional_overrides") or {}
        rows = [
            {
                "key": k,
                "value": v["value"],
                "unit": v.get("unit", ""),
                "desc": v.get("desc", ""),
                # ⛔ This tier needs the counter-example MORE than defaults does,
                # not less: these are the keys #1320 D1 says a tenant can fill in
                # today, so the reference number here is the one most likely to be
                # copied. Carrying it only on `defaults` left the three
                # most-likely-to-be-used numbers bare on every portal surface.
                **({"valueCounterexample": v["value_counterexample"]}
                   if "value_counterexample" in v else {}),
            }
            for k, v in entries.items()
            if is_shipped_optional_key(k)
        ]
        if rows:
            declared[pack_id] = rows
    return declared


def build_platform_data(at: str | None = None, release_charts: bool = False) -> dict:
    """Build the complete platform-data.json structure."""
    rule_counts = gather_rule_counts()
    scaffold_packs = load_scaffold_rule_packs()

    rule_packs = {}
    for key in PACK_ORDER:
        counts = rule_counts.get(key, {"recording": 0, "alert": 0})
        scaffold = scaffold_packs.get(key, {})

        pack_data = {
            "label": PACK_LABELS.get(key, key),
            "category": PACK_CATEGORY.get(key, "unknown"),
            "exporter": EXPORTER_SHORT.get(key, key),
            "configMap": f"prometheus-rules-{key}",
            "recordingRules": counts["recording"],
            "alertRules": counts["alert"],
            "required": key in REQUIRED_PACKS,
            "defaultOn": key in DEFAULT_ON_PACKS,
            "metrics": METRICS_COVERAGE.get(key, []),
        }

        # Enrich from scaffold_tenant.py if available
        if scaffold:
            pack_data["display"] = scaffold.get("display", pack_data["label"])
            pack_data["exporterFull"] = scaffold.get("exporter", pack_data["exporter"])

            # Extract default threshold keys
            defaults = scaffold.get("defaults", {})
            if defaults:
                pack_data["defaults"] = {
                    k: {
                        "value": v["value"],
                        "unit": v.get("unit", ""),
                        "desc": v.get("desc", ""),
                        # metric_class（如 saturation）僅 present 時透傳，
                        # 供 portal 顯示 _critical 教育提示
                        **({"metricClass": v["metric_class"]}
                           if "metric_class" in v else {}),
                        # value_counterexample（#1176）同樣僅 present 時透傳。
                        # ⛔ 這一格是 defaults tier——portal 的 generateSampleYaml
                        # 會把它當「活值」寫進使用者可複製的起手 YAML，所以它是
                        # 全平台唯一「把已測到反例的數字直接交到人手上」的面。
                        **({"valueCounterexample": v["value_counterexample"]}
                           if "value_counterexample" in v else {}),
                    }
                    for k, v in defaults.items()
                }

        # Dependencies
        if key in DEPENDENCIES:
            pack_data["dependencies"] = DEPENDENCIES[key]

        rule_packs[key] = pack_data

    # Totals
    total_rec = sum(rule_counts.get(k, {}).get("recording", 0) for k in PACK_ORDER)
    total_alert = sum(rule_counts.get(k, {}).get("alert", 0) for k in PACK_ORDER)

    # v2.3.0: Integrate tenant metadata if conf.d/ exists
    # The tenants are the exporter's answer at `at` (#2115 B4); `generated`
    # records it so --check re-evaluates at the same instant.
    if at is None:
        at = now_at()
    tenant_groups, tenant_metadata = _load_tenant_metadata(at)

    result = {
        "_comment": "Auto-generated by generate_platform_data.py — DO NOT EDIT",
        "generated": at,
        "generator": "scripts/tools/generate_platform_data.py",
        "packOrder": PACK_ORDER,
        "images": build_wizard_images(),
        "charts": build_wizard_charts() if release_charts else carried_wizard_charts(),
        "rulePacks": rule_packs,
        "declaredKeys": build_declared_keys(scaffold_packs),
        "categories": CATEGORIES,
        "totals": {
            "packs": len(PACK_ORDER),
            "recordingRules": total_rec,
            "alertRules": total_alert,
            "total": total_rec + total_alert,
        },
    }

    if tenant_groups or tenant_metadata:
        result["tenant_groups"] = tenant_groups
        result["tenant_metadata"] = tenant_metadata

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    """CLI entry point: 共用平台資料產生器."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Generate docs/assets/platform-data.json from source YAML",
    )
    parser.add_argument("--check", action="store_true",
                        help="CI mode: exit 1 if output is outdated")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print JSON to stdout without writing file")
    parser.add_argument("--release-charts", action="store_true",
                        help="Take `charts` (the wizard's helm --version pins) from "
                             "each Chart.yaml instead of carrying the block already in "
                             "the file. Release wrap-up only; with --check, exit 1 "
                             "unless the pins are the charts in the tree (make pre-tag)")
    args = parser.parse_args()

    at = None
    if args.check and OUTPUT_PATH.exists():
        try:
            at = recorded_at(json.loads(OUTPUT_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            at = None  # the comparison below reports the file itself
    try:
        data = (build_platform_data(at=at, release_charts=True) if args.release_charts
                else build_platform_data(at=at))
    except WizardImagesInconsistent as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(EXIT_VIOLATION)
    except WizardImagesUnreadable as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    except _lib_consumer_read.ConsumerReadError as exc:
        # #3206: the rule files are read the way Prometheus reads them; a
        # missing da-read / da-crdecode or a refused rule file stops the
        # generator — it does not count around it.
        print(f"ERROR: rule files: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    except TenantMetadataError as exc:
        # fail-loud（dev-rules #5）：寧可讓 target / gate 紅，也不要靜默
        # 產出缺 tenant metadata 的 platform-data.json。
        cause = exc.__cause__
        if isinstance(cause, (DaGuardNotFoundError, ParseFailedError, DaGuardError)):
            # #2115 0-B/B4: da-guard missing or failing is rc 2 with its
            # stderr whole, as generate_tenant_metadata itself exits.
            print("ERROR: tenant metadata generation failed: "
                  "the tenants are read through da-guard served-values",
                  file=sys.stderr)
            print_load_error(cause)
            sys.exit(EXIT_CALLER_ERROR)
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_VIOLATION)

    def strip_volatile(d):
        """Remove fields that change between runs (timestamp, git commit)."""
        import copy
        d = copy.deepcopy(d)
        d.pop("generated", None)
        # tenant_metadata.*.last_config_commit changes with every commit
        for tenant in d.get("tenant_metadata", {}).values():
            tenant.pop("last_config_commit", None)
        return d

    content = encode_json(data) + "\n"
    fallback_content = (
        encode_json(build_fallback(data)) + "\n"
    )

    if args.dry_run:
        return Report(data)  # #3216: run() encodes it — the same text as the file

    if args.check:
        if not OUTPUT_PATH.exists():
            print(f"❌ {OUTPUT_PATH.relative_to(REPO_ROOT)} does not exist. "
                  f"Run without --check first.")
            sys.exit(EXIT_VIOLATION)
        existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        new_stable = encode_json(strip_volatile(data)) + "\n"
        try:
            existing_stable = encode_json(strip_volatile(existing)) + "\n"
        except OutputNotSerializable:
            # A hand edit JSON reads but cannot carry (NaN / Infinity): the
            # file is not what the generator writes — outdated, not a crash.
            existing_stable = None
        if args.release_charts:
            pinned = existing.get("charts") if isinstance(existing, dict) else None
            bad, notes = unpublishable_chart_pins(
                pinned if isinstance(pinned, dict) else {}, data["charts"])
            for note in notes:
                print(f"NOTE: wizard chart pin: {note}")
            if bad:
                print(f"❌ {OUTPUT_PATH.relative_to(REPO_ROOT)} `charts`: the Deployment "
                      f"Wizard would pin a chart nothing publishes:\n  "
                      + "\n  ".join(bad)
                      + "\nRun `make platform-data RELEASE_CHARTS=1` and copy the versions "
                        "into CHART_MIRROR in "
                        "tools/portal/src/interactive/tools/_common/data/charts.js.")
                sys.exit(EXIT_VIOLATION)
            # The rest of the file is compared with the block as it stands.
            data["charts"] = pinned
            new_stable = encode_json(strip_volatile(data)) + "\n"
        if existing_stable != new_stable:
            print(f"❌ {OUTPUT_PATH.relative_to(REPO_ROOT)} is outdated. "
                  f"Run `make platform-data` to update.")
            sys.exit(EXIT_VIOLATION)
        # ⛔ The fallback is checked SEPARATELY and by exact bytes. It carries no
        # volatile field, so there is nothing to strip — and an edit to it alone
        # (the shape #1226 is about: somebody "fixes" the offline catalog by
        # hand) leaves the file above untouched, so the comparison up there
        # cannot see it.
        if not FALLBACK_PATH.exists():
            print(f"❌ {_display_path(FALLBACK_PATH)} does not exist. "
                  f"Run `make platform-data` first.")
            sys.exit(EXIT_VIOLATION)
        if FALLBACK_PATH.read_text(encoding="utf-8") != fallback_content:
            print(f"❌ {_display_path(FALLBACK_PATH)} is outdated or was "
                  f"hand-edited. It is GENERATED from "
                  f"{OUTPUT_PATH.relative_to(REPO_ROOT)}; run `make "
                  f"platform-data` to regenerate. (#1226)")
            sys.exit(EXIT_VIOLATION)
        print(f"✅ {OUTPUT_PATH.relative_to(REPO_ROOT)} is up to date.")
        print(f"✅ {_display_path(FALLBACK_PATH)} is up to date.")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(content, encoding="utf-8", newline="\n")
    os.chmod(OUTPUT_PATH,
             stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    FALLBACK_PATH.write_text(fallback_content, encoding="utf-8", newline="\n")
    os.chmod(FALLBACK_PATH,
             stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    print(f"✅ Generated {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    print(f"✅ Generated {_display_path(FALLBACK_PATH)} "
          f"(the portal's offline fallback)")
    print(f"   {data['totals']['packs']} packs, "
          f"{data['totals']['recordingRules']} recording rules, "
          f"{data['totals']['alertRules']} alert rules")


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)  # #3216: run() encodes the --json document
