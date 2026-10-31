#!/usr/bin/env python3
"""diagnose.py — Quick health check for a tenant's MariaDB and monitoring stack.

Usage:
  # 本地開發 (透過 port-forward)
  kubectl port-forward svc/prometheus 9090:9090 -n monitoring &
  python3 diagnose.py db-a

  # 叢集內執行 (K8s Job / Pod)
  python3 diagnose.py db-a \
    --prometheus http://prometheus.monitoring.svc.cluster.local:9090

  # 多叢集 (Thanos / VictoriaMetrics)
  python3 diagnose.py db-a \
    --prometheus http://thanos-query.monitoring.svc:9090

Returns JSON: {"status": "healthy"|"error", "tenant", ...}

需求:
  - Prometheus Query API 必須可從腳本執行位置存取
    * 叢集內: K8s Service (http://prometheus.monitoring.svc.cluster.local:9090)
    * 叢集外: port-forward 或 Ingress
    * 多叢集: Thanos Query / VictoriaMetrics 等統一查詢端點亦可
"""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import inspect
import json
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple, TextIO

# Add script dir to path for lib imports
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import detect_cli_lang, add_prometheus_arg  # noqa: E402
# #3133: the lib's raising reader (shape checked, samples read once).
from _lib_prometheus import QueryError, prometheus_instant  # noqa: E402
from _lib_python import format_json_report  # noqa: E402
from _lib_exitcodes import (  # noqa: E402
    EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, CallerError, arm_crash_envelope)
from _lib_paths import require_dir  # noqa: E402  (#3276 / #3392)
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_kubectl import KubectlError, kubectl_list, kubectl_logs  # noqa: E402  (#3349)
# #2526 / #2547: the tenant's resolution is threshold-exporter's own
# (`da-guard effective`); see resolve_inheritance_chain.
from _lib_tenant_values import (  # noqa: E402
    EffectiveTree,
    ServedTree,
    ServedValuesError,
    canonical_key,
    exit_on_served_values_error,
    load_effective_tree,
    load_served_tree,
)
from _lib_io import exit_on_yaml_file_error  # noqa: E402  (#1654)
# ADR-036 step 4 (#2906): `optional_overrides` through `da-guard parse-tree`.
import _lib_parse_cache  # noqa: E402
import _lib_parse_tree  # noqa: E402
from _lib_parse_tree import FileUnreadable  # noqa: E402

# Language detection for bilingual help
_LANG = detect_cli_lang()

# Bilingual help strings
_HELP = {
    'description': {
        'zh': '租戶的 MariaDB 和監控堆棧的快速健康檢查',
        'en': "Quick health check for a tenant's MariaDB and monitoring stack"
    },
    'tenant': {
        'zh': '租戶 ID (例如 db-a)',
        'en': 'Tenant ID (e.g. db-a)'
    },
    'prometheus': {
        'zh': 'Prometheus Query API URL (預設: $PROMETHEUS_URL，否則 http://localhost:9090; 叢集內建議用 http://prometheus.monitoring.svc.cluster.local:9090)',
        'en': 'Prometheus Query API URL (default: $PROMETHEUS_URL, else http://localhost:9090; for in-cluster, use http://prometheus.monitoring.svc.cluster.local:9090)'
    },
    'config_dir': {
        'zh': '租戶配置目錄路徑 (conf.d/)，用於設定檔查詢',
        'en': 'Path to tenant config directory (conf.d/) for profile lookup'
    },
    'show_inheritance': {
        'zh': '顯示詳細的三層繼承鏈解析 (需要 --config-dir)',
        'en': 'Show detailed three-layer inheritance chain resolution (requires --config-dir)'
    }
}


def _h(key: str) -> str:
    """Get help text in detected language."""
    return _HELP[key].get(_LANG, _HELP[key]['en'])


class CommandTimeoutError(OSError):
    """The caller's deadline had passed before kubectl could be started.

    An environment failure like `KubectlError` (rc 2), not a finding about
    the tenant (#2968): the CLI prints one line, batch_diagnose marks the
    tenant `_environment_error`. Deliberately NOT a FileNotFoundError
    subclass, so batch_diagnose has to name it rather than let it fall into
    its generic OSError branch (rc 1). A kubectl that was started and gave no
    answer in its budget is `KubectlError` (#3349).
    """


# #2968: the time budget a caller (batch_diagnose) puts on everything check()
# runs: `(at, grace)`, `at` a time.monotonic() value. None — the default, and
# what a lone `da-tools diagnose` runs with — leaves each call at its own cap.
#
# ⚠️ A ContextVar rather than a parameter, so check()'s signature and every
# test double of it stay as they are. The price is that it is implicit:
#   - A new thread does NOT inherit it (each thread starts from an empty
#     context on the Pythons this supports), which is why batch_diagnose sets
#     it INSIDE the tenant's own thread. Should check() ever start a thread of
#     its own, run that thread's target under contextvars.copy_context(), or
#     the deadline silently stops applying there.
#   - Only the direct child is killed when its budget runs out
#     (subprocess's own timeout). A process kubectl itself starts — an exec
#     credential plugin such as `aws eks get-token`, gke-gcloud-auth-plugin,
#     kubelogin — is not, and may outlive the run. Killing the whole process
#     group (start_new_session + killpg) is deferred until a plugin orphan is
#     actually reported: detaching the child from the foreground group makes
#     Ctrl-C leave it running instead.
_DEADLINE: contextvars.ContextVar[tuple[float, float] | None] = contextvars.ContextVar(
    "diagnose_deadline", default=None)
# How long before the caller's deadline a subprocess is made to give up, so
# subprocess kills and reaps it while the caller is still waiting on the
# thread. A conservative margin, not a measured threshold: 0 left no orphan
# kubectl in 35 stressed runs either. batch_diagnose shrinks it for a
# --timeout too short to leave the subprocess any time (min(this, timeout/4)).
# ⚠️ No guarantee below that: with a --timeout under about 0.5s, or a heavily
# loaded machine, the shrunken grace can be too short for fork/exec plus the
# kill, and a kubectl may still outlive the run (measured under 8 busy loops
# on 4 cores: --timeout 0.05 left 10 of 80, 0.3 left 3 of 80, 0.5+ none).
DEADLINE_GRACE_S = 0.5
KUBECTL_TIMEOUT_S = 120
PROMETHEUS_TIMEOUT_S = inspect.signature(
    prometheus_instant).parameters["timeout"].default


@contextlib.contextmanager
def deadline(at: float, *, grace: float = DEADLINE_GRACE_S):
    """Bound every kubectl read / query_prometheus in this context (this thread
    only — see _DEADLINE) by `at`, a time.monotonic() value; a subprocess is
    given up to `grace` seconds less."""
    token = _DEADLINE.set((at, grace))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def _time_left(with_grace: bool) -> float | None:
    """Seconds before the deadline (minus its grace when asked), or None."""
    bound = _DEADLINE.get()
    if bound is None:
        return None
    at, grace = bound
    return at - time.monotonic() - (grace if with_grace else 0.0)


def kubectl_budget() -> float:
    """The timeout for the next kubectl read: its own cap, or less under a
    caller's deadline (minus its grace). Raises CommandTimeoutError when the
    deadline has passed: never start a process the caller stopped waiting for."""
    left = _time_left(with_grace=True)
    if left is not None and left <= 0:
        raise CommandTimeoutError("kubectl not run: the deadline has passed")
    return KUBECTL_TIMEOUT_S if left is None else min(KUBECTL_TIMEOUT_S, left)


def pod_phase(tenant: str) -> str | None:
    """The phase of the tenant's first MariaDB Pod, None when kubectl answered
    and lists none. #3349: a read that got no answer (forbidden, unreachable,
    no kubectl, its budget out) is `KubectlError` (rc 2) — never "not found"."""
    pods = kubectl_list("pods", namespace=tenant, selector="app=mariadb",
                        timeout=kubectl_budget())
    return ((pods[0].get("status") or {}).get("phase") or None) if pods else None


def mariadb_logs(tenant: str) -> str:
    """The last 20 lines of the MariaDB container's log, or `KubectlError`."""
    return kubectl_logs(tenant, "deploy/mariadb", container="mariadb", tail=20,
                        timeout=kubectl_budget())


# The Pod and exporter checks below are written for MariaDB (`app=mariadb`,
# `mysql_up`). Every other tenant used to get `Pod not found`, so
# batch-diagnose reported all of them as errors (issue 1513, 本輪決策 N2).
MARIADB_DB_TYPE = "mariadb"
PROMETHEUS_FAILURE = "Prometheus query failed"


def tenant_db_type(tenant: str, prom_url: str) -> tuple[str | None, str | None]:
    """Return (db_type, error) from `tenant_expected_exporter{tenant=...}`.

    Read from Prometheus rather than conf.d: batch-diagnose calls check()
    without a config dir. The exporter emits the series only for a tenant that
    declares `_metadata.db_type` (#869), so an empty result means "not
    declared", which is distinct from a failed query.
    """
    results, err = query_prometheus(prom_url, f'tenant_expected_exporter{{tenant="{tenant}"}}')
    if err:
        return None, err
    for item in results or []:
        db_type = item.get("metric", {}).get("db_type")
        if db_type:
            return db_type, None
    return None, None


def exit_code(result: dict) -> int:
    """0 healthy or unchecked; 2 when a Prometheus query failed (the caller's
    environment, as validate / shadow-verify classify it); 1 for any other issue."""
    if result.get("status") != "error":
        return EXIT_OK
    if any(str(i).startswith(PROMETHEUS_FAILURE) for i in result.get("issues", [])):
        return EXIT_CALLER_ERROR
    return EXIT_VIOLATION


def query_prometheus(prom_url: str, promql: str):
    """``(rows, None)`` from ``prometheus_instant``, or ``(None, error)`` when
    the query was not answered (#3133: the lib's QueryError — a refused
    connection, an error status or a malformed answer alike; every caller
    below turns it into PROMETHEUS_FAILURE). Its timeout is bounded by the
    caller's deadline (#2968; see _DEADLINE). No grace: an abandoned query
    leaves no process behind. Past the deadline nothing is sent, and the
    result is a failed query."""
    left = _time_left(with_grace=False)
    if left is not None and left <= 0:
        return None, "not queried: the deadline has passed"
    try:
        if left is None:
            return prometheus_instant(prom_url, promql), None
        return prometheus_instant(prom_url, promql,
                                  timeout=min(PROMETHEUS_TIMEOUT_S, left)), None
    except QueryError as exc:
        return None, str(exc)


# #2526 / #2547 (step 0): the tenant's resolution — which tenants exist, the
# profile each is bound to, the layer that supplied each key — is Go's answer
# (`da-guard effective`, tenant-api's /effective), and every value is what
# /metrics serves (`da-guard served-values`), never a Python reading of the
# YAML. This file used to re-read conf.d with its own PyYAML
# loaders and so diverged from the exporter wherever PyYAML and yaml.v3
# disagree: `2024-02-30` ended the run with a traceback, `!foo 70` dropped
# the whole tenant file, `tenants: !!set {tx}` declared no tenant.
#
# The order in which `chain` lists the layers, lowest first.
_LAYER_ORDER = ("defaults", "platform", "profile", "tenant")


class TenantNotFoundError(CallerError):
    """#1524: the tree at `--config-dir` has no such tenant — the question
    names something that is not there, as tenant-api's `/effective` answers
    404 `tenant not found`; not a finding about the tenant (rc 1)."""

    reason = "tenant_not_found"


def require_config_dir(config_dir: str) -> None:
    """`--config-dir` as given must be a directory (`_lib_paths`, #3276):
    else `CallerError`, rc 2. The one check, so `main()` can make it before
    any query and `resolve_inheritance_chain` makes the same one."""
    require_dir(config_dir, lang="en", flag="--config-dir")


def lookup_tenant_profile(tenant: str, config_dir: str | None) -> str | None:
    """The profile `tenant` is bound to under `config_dir`, or None.

    A thin view over `resolve_inheritance_chain` (#1522: one reader, not
    two); raises what it raises — an unusable `config_dir` or a tenant the
    tree does not have included (#1524).
    """
    inheritance = resolve_inheritance_chain(tenant, config_dir)
    return inheritance["profile_name"] if inheritance else None


def _declared_keys(base: Path, defaults_chain: list[str]) -> list[str]:
    """`optional_overrides:` of the root defaults carrier: key NAMES the
    platform recognises but assigns no value to (#1310).

    ⛔ The one thing still read from a file here, because `da-guard
    effective` does not report it. Read through `da-guard parse-tree`
    (ADR-036 step 4, #2906) as the exporter reads the list (`[]string`):
    each non-null entry's source text — nothing is interpreted, so `yes`
    is the name "yes" and `010` the name "010". Called only after `da-guard
    effective` loaded the tree, so the file decodes for the exporter; a file
    parse-tree still cannot read is answered with [] and one WARN naming
    why. No usable parse-tree raises ParseTreeError (exit 2 in `main`).

    WHICH file is the exporter's choice too: the root entry of the tenant's
    `defaults_chain` (/effective), not a carrier this file selects — so no
    Python walk of the tree (and no "read FLAT" warning) happens here.
    """
    if not defaults_chain or "/" in defaults_chain[0]:
        return []   # no root defaults carrier on this tenant's path
    try:
        root = _lib_parse_cache.reader_for(base).node(defaults_chain[0])
    except FileUnreadable as e:
        print(f"  WARN: {safe_label(defaults_chain[0])}: optional_overrides not read "
              f"(declared left empty): {safe_label(e.kind)}", file=sys.stderr)
        return []
    value = root.get("optional_overrides")
    if not isinstance(value, list):
        return []
    return [_lib_parse_tree.scalar_text(item) for item in value
            if _lib_parse_tree.is_scalar(item) and item["tag"] != "!!null"]


def resolve_inheritance_chain(tenant: str, config_dir: str | None) -> dict[str, object] | None:
    """Resolve the inheritance chain for a tenant from threshold-exporter's own
    answers (#2526 / #2547): which keys the tenant has, its profile and each
    key's layer from `da-guard effective` (/effective); each value from
    `da-guard served-values` (/metrics).

    Returns None when no `config_dir` was given (None), else a dict with:
      - chain: one entry per layer file that supplies a value the tenant
        ends up with, lowest first — `defaults` (each `_defaults.yaml` on the
        tenant's path, root first), `platform` (a root platform file's
        `tenants:` entry), `profile`, `tenant` (the tenant's own file); each
        `{layer, source, keys}`, `keys` the values as that layer wrote them
        (the winning layer per key)
      - resolved: every threshold (non-`_`) key of the tenant's
        `effective_config`, keyed as written, with the value /metrics serves
        for it; a key /metrics serves no row for (served-values' `unserved`:
        switched off, or not declared at the root) keeps /effective's value,
        and one with neither is left out
      - profile_name: the profile the tenant is bound to, or None
      - declared: key NAMES the platform recognises but assigns no value to
        (`optional_overrides:`, #1310; see `_declared_keys`)

    ⛔ Two answers, side by side, never reconciled here: `resolved` is what
    /metrics serves (#2421 — a value it cannot read as a number, or whose
    `expires:` has passed, is served as the default), `chain` is where each
    value was WRITTEN and what was written (/effective). When they differ the
    output does not say why — `da-guard served-values`' stderr does. Telling
    "60:critical served as 60" from "unservable, default served" would need
    Go's value parsing re-done in Python, which this tool does not do.
    `chain` lists a key once, in the layer that supplies it, so a default
    the tenant overrides is not listed.

    ⛔ `declared` is deliberately NOT merged into `resolved`, and is not a chain
    layer: a declared key has no value until the tenant writes one.

    Raises `DaGuardNotFoundError`, `ParseFailedError` (a file the exporter's
    load drops or cannot read) or `DaGuardError` (`_lib_tenant_values`;
    served-values refusing the tree, or having no entry for a tenant
    effective resolves, included): the chain is never answered
    from a partial read.

    #1524: a `config_dir` that was given is a question, never a no-op. One
    that is not a directory (`''` included) raises `CallerError` with
    `_lib_paths`' reason (`dir_not_found` / `dir_not_accessible`), and a
    tree without `tenant` raises `TenantNotFoundError` — both rc 2 under
    `run()`. Either used to be None, which the CLI printed as an error
    object and exited 0 on, and `check()` answered as if no `--config-dir`
    had been given.
    """
    if config_dir is None:
        return None
    require_config_dir(config_dir)
    base = Path(config_dir)

    tree = _load_effective(base)
    _tenant_entry(tree, tenant, str(base))
    return chain_for(tenant, ChainSource(base, tree, load_served_tree(base)))


class ChainSource(NamedTuple):
    """One read of a tree for many chains (#1525): `da-guard effective` and
    `served-values` once, then `chain_for` per tenant."""

    base: Path
    effective: EffectiveTree
    served: ServedTree
    where: str = ""
    """How a missing tenant names the tree (default: `base`)."""


def load_chain_source(config_dir: str | Path, *, where: str = "") -> ChainSource:
    """`ChainSource` for `config_dir`: the same checks and reads as
    `resolve_inheritance_chain`, made once — what batch-diagnose resolves
    every tenant from (#1525). Raises as `resolve_inheritance_chain` does."""
    require_config_dir(str(config_dir))
    base = Path(config_dir)
    tree = _load_effective(base)
    return ChainSource(base, tree, load_served_tree(base), where)


def _load_effective(base: Path) -> EffectiveTree:
    tree = load_effective_tree(base)
    for s in tree.skipped:
        print(f"  WARN: {safe_label(s.file)}: {safe_label(s.reason)}", file=sys.stderr)
    return tree


def _tenant_entry(tree: EffectiveTree, tenant: str, where: str):
    te = tree.tenants.get(tenant)
    if te is None:
        raise TenantNotFoundError(
            f"no tenant '{safe_label(tenant)}' in {safe_label(where)}: "
            f"threshold-exporter reads {len(tree.tenants)} tenant(s) there")
    return te


def chain_for(tenant: str, source: ChainSource) -> dict[str, object]:
    """`tenant`'s chain from an already-read tree — the dict
    `resolve_inheritance_chain` documents; `TenantNotFoundError` when the
    tree has no such tenant."""
    base, tree, served_tree = source.base, source.effective, source.served
    te = _tenant_entry(tree, tenant, source.where or str(base))
    for w in te.warnings:
        print(f"  WARN: {safe_label(w)}", file=sys.stderr)
    # `profile_name` is the profile the exporter binds; a reference that
    # binds none (an unknown name, or one the exporter does not read from a
    # defaults file) used to be reported as if it did.
    written = te.effective_config.get("_profile")
    if te.profile is None and isinstance(written, str) and written.strip():
        src = te.key_sources.get("_profile")
        print(f"  WARN: {safe_label(src.file if src else te.source_file)}: _profile "
              f"'{safe_label(written)}' binds no profile for tenant "
              f"'{safe_label(tenant)}' (threshold-exporter applies none)", file=sys.stderr)

    served = served_tree.tenants.get(tenant)
    if served is None:
        # The two Go answers disagree on which tenants exist: no answer.
        raise ServedValuesError(
            f"da-guard served-values has no tenant '{tenant}', which da-guard effective "
            f"reports in {base}", 0, "")
    resolved: dict[str, object] = {}
    groups: dict[tuple[str, str], dict[str, object]] = {}
    # /effective keeps one spelling per threshold (it drops the shadowed
    # one), so each key below maps to its own canonical row.
    for key, src in te.key_sources.items():
        if key.startswith("_"):
            continue
        canon = canonical_key(key, served_tree.aliases)
        written = te.effective_config[key]
        if canon in served.severities:
            value = served.values[canon]
            if isinstance(value, float) and value.is_integer():
                value = int(value)   # the JSON number Go wrote
        elif key in served.unserved and written is not None:
            # No /metrics row (switched off, not declared at the root): the
            # value as /effective decodes it.
            value = written
        else:
            continue
        resolved[key] = value
        groups.setdefault((src.layer, src.file), {})[key] = written

    def _order(item: tuple[tuple[str, str], dict[str, object]]) -> tuple[int, int, str]:
        (layer, file), _ = item
        level = (te.defaults_chain.index(file)
                 if layer == "defaults" and file in te.defaults_chain else 0)
        return (_LAYER_ORDER.index(layer), level, file)

    chain = []
    for (layer, file), keys in sorted(groups.items(), key=_order):
        source = f"{file} → {te.profile}" if layer == "profile" and te.profile else file
        chain.append({"layer": layer, "source": source, "keys": keys})

    return {
        "chain": chain,
        "resolved": resolved,
        "profile_name": te.profile,
        # settable, but with no platform value — see the docstring
        "declared": _declared_keys(base, te.defaults_chain),
    }


def _format_chain_summary(inheritance):
    """Format inheritance chain for JSON output (token-efficient).

    Returns a compact summary: {layers: [...], resolved_count: N}
    """
    layers = []
    for c in inheritance.get("chain", []):
        layers.append({
            "layer": c["layer"],
            "source": c["source"],
            "key_count": len(c["keys"]),
        })
    summary = {
        "layers": layers,
        "resolved_count": len(inheritance.get("resolved", {})),
        # settable-but-unvalued keys are counted separately, never folded into
        # resolved_count — they carry no value to resolve (#1310)
        "declared_count": len(inheritance.get("declared", []) or []),
        "profile": inheritance.get("profile_name"),
    }
    # #1468's `skipped_unusable_files` is gone (#2526): a file the exporter's
    # load drops or cannot read now fails the run (resolve_inheritance_chain
    # raises), so there is no truncated chain left to caveat.
    return summary


def check(tenant: str, prom_url: str, config_dir: str | None = None,
          *, out: TextIO | None = None, chain: dict | None = None) -> dict:
    """Emit one JSON health document for `tenant`.

    `chain` is the tenant's chain already resolved (`chain_for`), in place of
    `config_dir`: batch-diagnose reads its tree once for every tenant (#1525).
    Giving both is a programming error (`TypeError`).

    ⛔ `out` exists because the caller may need the document WITHOUT touching
    process-global state. `contextlib.redirect_stdout` rebinds `sys.stdout` for
    the whole process, so two threads capturing concurrently can restore each
    other's buffer and leave the real stdout permanently replaced — the process
    then exits 0 having written nothing at all. batch_diagnose.py ran exactly
    that shape; test_check_writes_to_the_given_stream_not_sys_stdout pins the
    seam that replaced it.

    ⚠️ Resolved at CALL time, not as a default argument value: binding
    `sys.stdout` at def time would freeze the stream pytest's capsys swaps in.

    Returns the document it printed, so the CLI can derive its exit code.
    """
    if chain is not None and config_dir is not None:
        raise TypeError("check(): pass config_dir or chain, not both")
    errors = []
    skipped = []
    if config_dir is not None:
        # #1524: an importer gets the path refusal before any query too, not
        # after up to four Prometheus queries and a kubectl call (step 4).
        require_config_dir(config_dir)

    # 0. 資料庫類型：Pod 與 exporter 兩項只為 MariaDB 寫（本輪決策 N2）
    db_type, db_type_err = tenant_db_type(tenant, prom_url)
    if db_type_err:
        errors.append(f"{PROMETHEUS_FAILURE} ({prom_url})")
        reason = "db_type unknown: the Prometheus query for tenant_expected_exporter failed"
    elif db_type is None:
        reason = ("db_type not declared (no tenant_expected_exporter series for this "
                  "tenant); the Pod and exporter checks are written for MariaDB")
    else:
        reason = (f"db_type={db_type}; the Pod and exporter checks are written for "
                  "MariaDB (app=mariadb, mysql_up)")
    is_mariadb = db_type == MARIADB_DB_TYPE and not db_type_err
    if not is_mariadb:
        skipped = [{"check": "pod", "reason": reason},
                   {"check": "exporter", "reason": reason}]

    if is_mariadb:
        # 1. 檢查 Pod 狀態
        pod_status = pod_phase(tenant)
        if not pod_status:
            errors.append("Pod not found")
        elif pod_status != "Running":
            errors.append(f"Pod status is {pod_status}")

        # 2. 檢查 Exporter (透過 Prometheus API)
        try:
            up_results, up_err = query_prometheus(prom_url, f'mysql_up{{instance="{tenant}"}}')
            if up_err:
                errors.append(f"{PROMETHEUS_FAILURE} ({prom_url})")
            elif up_results:
                val = up_results[0].get("value", [None, None])[1]
                if val != "1":
                    errors.append("Exporter reports DOWN (mysql_up!=1)")
            else:
                errors.append("Exporter reports DOWN (mysql_up!=1)")
        except Exception:
            errors.append("Metrics check failed")

    # 3. 查詢運營模式 (Silent Mode / Maintenance)
    # #3133: a mode query not answered is not "normal" — the mode is unknown
    # and the run names the failed query (rc 2), as for the checks above.
    operational_mode = "normal"
    maint_results, maint_err = query_prometheus(prom_url, f'user_state_filter{{tenant="{tenant}",filter="maintenance"}}')
    if maint_err:
        operational_mode = "unknown"
        errors.append(f"{PROMETHEUS_FAILURE} ({prom_url})")
    elif maint_results:
        operational_mode = "maintenance"

    if operational_mode == "normal":
        silent_results, silent_err = query_prometheus(prom_url, f'user_silent_mode{{tenant="{tenant}"}}')
        if silent_err:
            operational_mode = "unknown"
            errors.append(f"{PROMETHEUS_FAILURE} ({prom_url})")
        elif silent_results:
            severities = [r.get("metric", {}).get("target_severity", "") for r in silent_results]
            if "warning" in severities and "critical" in severities:
                operational_mode = "silent:all"
            elif severities:
                operational_mode = f"silent:{severities[0]}"

    # 4. Profile lookup + inheritance chain (v1.12.0, optional — requires --config-dir)
    # #1522: ONE read of config_dir. The profile is the chain's own
    # `profile_name`. #2526: that read is `da-guard effective`; when it
    # cannot be made, this raises (the CLI exits 2) rather than answer
    # without the chain.
    inheritance = chain if chain is not None else resolve_inheritance_chain(tenant, config_dir)
    profile_name = inheritance["profile_name"] if inheritance else None

    # 5. 輸出結果 (Token Saving 核心：正常時只回傳極簡 JSON)
    stream = sys.stdout if out is None else out

    if not errors:
        # Skipped checks are not a pass: a tenant whose Pod and exporter were
        # never looked at must not read as healthy (issue 1513). `unchecked`
        # still exits 0 — nothing was found wrong — and batch-diagnose counts
        # it apart from healthy.
        result = {"status": "unchecked" if skipped else "healthy", "tenant": tenant}
        if operational_mode != "normal":
            result["operational_mode"] = operational_mode
        if profile_name:
            result["profile"] = profile_name
        if inheritance:
            result["inheritance_chain"] = _format_chain_summary(inheritance)
        if skipped:
            result["skipped"] = skipped
        print(json.dumps(result), file=stream)
    else:
        # 只有異常時，嘗試抓取最近的 error log（MariaDB 的 deploy 名稱）
        # #2968: an extra the findings above do not depend on — a log read that
        # got no answer is recorded beside them, never raised over them (that
        # turned a finding, rc 1, into an environment error, rc 2). #3349: any
        # such read, not only a timeout — it used to read as "no log" unnamed.
        logs_error = None
        try:
            logs = mariadb_logs(tenant) if is_mariadb else None
        except (KubectlError, CommandTimeoutError) as exc:
            logs, logs_error = None, str(exc)
        error_logs = [line for line in (logs or "").split('\n') if 'ERROR' in line]

        result = {
            "status": "error",
            "tenant": tenant,
            "issues": errors,
            "recent_logs": error_logs[:3],  # 只回傳最後 3 行錯誤
        }
        if logs_error:
            # Empty recent_logs because they could not be fetched, not
            # because there were none.
            result["recent_logs_error"] = logs_error
        if operational_mode != "normal":
            result["operational_mode"] = operational_mode
        if profile_name:
            result["profile"] = profile_name
        if inheritance:
            result["inheritance_chain"] = _format_chain_summary(inheritance)
        if skipped:
            result["skipped"] = skipped
        print(json.dumps(result, ensure_ascii=False), file=stream)
    return result


@exit_on_yaml_file_error  # #1654: a file that does not decode → rc 2, named
@exit_on_served_values_error  # #2526: da-guard missing or failing → rc 2, named
@_lib_parse_cache.exit_on_parse_tree_error  # #2906: no usable parse-tree → rc 2
def main() -> None:
    parser = argparse.ArgumentParser(
        description=_h('description'),
    )
    parser.add_argument("tenant", help=_h('tenant'))
    add_prometheus_arg(parser, help_text=_h('prometheus'))
    parser.add_argument("--config-dir",
                        help=_h('config_dir'))
    parser.add_argument("--show-inheritance", action="store_true",
                        help=_h('show_inheritance'))
    # #452 Track C: diagnose emits JSON by design (consumers like
    # scripts/tools/ops/batch_diagnose.py json.loads its stdout). --json is
    # the default, accepted explicitly so the documented
    # `da-tools diagnose ... --json | jq` idiom works and matches the
    # convention required of new subcommands (see dev-rules.md).
    parser.add_argument("--json", action="store_true", default=True,
                        help="Emit machine-readable JSON to stdout (default).")
    args = parser.parse_args()
    if args.json:  # #3094: a crash in --json mode still prints one envelope
        arm_crash_envelope()

    if args.show_inheritance and args.config_dir is None:
        print("ERROR: --show-inheritance requires --config-dir",
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)  # #452: missing required arg
    if args.config_dir is not None:
        # #1524: before any query — a run told to read a tree it cannot
        # read stops here (rc 2), not after the cluster checks.
        require_config_dir(args.config_dir)

    if args.show_inheritance:
        # #2526: fail closed — da-guard missing or failing, or a file the
        # exporter's load drops, exits 2 through the decorators on main();
        # #1524: so does a tree without the tenant (TenantNotFoundError).
        inheritance = resolve_inheritance_chain(args.tenant, args.config_dir)
        print(format_json_report(inheritance, default=str))
        sys.exit(EXIT_OK)

    try:
        result = check(args.tenant, args.prometheus, config_dir=args.config_dir)
    except KubectlError as exc:
        # issue 1513 / #2968 / #3349: no answer from the cluster — kubectl
        # missing (the da-tools image ships none), forbidden, unreachable or
        # out of time — is the environment: rc 2 and one line, the reason
        # `kubectl_error` under --json; never "Pod not found" (rc 1).
        raise KubectlError(f"{exc}; the MariaDB Pod check needs kubectl and a cluster "
                           "that answers") from None
    except CommandTimeoutError as exc:
        # #2968: the caller's deadline passed before kubectl could start.
        print(f"ERROR: {exc}; the MariaDB Pod check needs kubectl and a cluster "
              "that answers", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    # issue 1513: `status: error` used to exit 0, so `da-tools diagnose t && …`
    # passed on an unhealthy tenant.
    sys.exit(exit_code(result))


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
