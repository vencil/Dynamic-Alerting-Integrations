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
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

import yaml

# Add script dir to path for lib imports
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import detect_cli_lang, http_get_json, query_prometheus_instant, add_prometheus_arg  # noqa: E402
from _lib_python import format_json_report  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
# #2526 / #2547: the tenant's resolution is threshold-exporter's own
# (`da-guard effective`); see resolve_inheritance_chain.
from _lib_tenant_values import (  # noqa: E402
    ServedValuesError,
    canonical_key,
    exit_on_served_values_error,
    load_effective_tree,
    load_served_tree,
)
from _lib_io import exit_on_yaml_file_error  # noqa: E402  (#1654)

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


class CommandNotFoundError(FileNotFoundError):
    """The external command (kubectl) is not on PATH.

    A FileNotFoundError subclass so batch_diagnose, which catches OSError per
    tenant, keeps recording it as that tenant's issue; the CLI turns it into
    rc=2 plus one line instead of the traceback it used to be (issue 1513).
    """


class CommandTimeoutError(OSError):
    """The external command (kubectl) did not finish within its time budget.

    An environment failure like CommandNotFoundError (rc 2), not a finding
    about the tenant (#2968): the CLI prints one line, batch_diagnose marks
    the tenant `_environment_error`. Deliberately NOT a FileNotFoundError
    subclass, so batch_diagnose has to name it rather than let it fall into
    its generic OSError branch (rc 1).
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
    query_prometheus_instant).parameters["timeout"].default


@contextlib.contextmanager
def deadline(at: float, *, grace: float = DEADLINE_GRACE_S):
    """Bound every run_cmd / query_prometheus in this context (this thread
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


def run_cmd(cmd: list[str]) -> str | None:
    """Execute a command safely using list arguments only (no shell=True).

    Args:
        cmd: Command as a list of strings. String input is rejected
             to prevent potential command injection via shlex parsing.
    """
    if not isinstance(cmd, list):
        raise TypeError(f"run_cmd() requires list argument, got {type(cmd).__name__}")
    left = _time_left(with_grace=True)
    if left is not None and left <= 0:
        # Never start a process the caller has already stopped waiting for.
        raise CommandTimeoutError(f"{cmd[0]} not run: the deadline has passed")
    budget = KUBECTL_TIMEOUT_S if left is None else min(KUBECTL_TIMEOUT_S, left)
    try:
        return subprocess.check_output(cmd, text=True, encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL, timeout=budget).strip()
    except subprocess.CalledProcessError:
        return None
    except FileNotFoundError as exc:
        raise CommandNotFoundError(f"{cmd[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        # check_output has already killed and reaped the child (#2968).
        raise CommandTimeoutError(f"{cmd[0]} timed out after {budget:.3g}s") from exc


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
    """query_prometheus_instant, its timeout bounded by the caller's deadline
    (#2968; see _DEADLINE). No grace: an abandoned query leaves no process
    behind. Past the deadline nothing is sent, and the result is a failed
    query."""
    left = _time_left(with_grace=False)
    if left is None:
        return query_prometheus_instant(prom_url, promql)
    if left <= 0:
        return None, "not queried: the deadline has passed"
    return query_prometheus_instant(prom_url, promql,
                                    timeout=min(PROMETHEUS_TIMEOUT_S, left))


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


def lookup_tenant_profile(tenant: str, config_dir: str | None) -> str | None:
    """The profile `tenant` is bound to under `config_dir`, or None.

    A thin view over `resolve_inheritance_chain` (#1522: one reader, not
    two); raises what it raises when da-guard is missing or fails.
    """
    inheritance = resolve_inheritance_chain(tenant, config_dir)
    return inheritance["profile_name"] if inheritance else None


def _declared_keys(base: Path, defaults_chain: list[str]) -> list[str]:
    """`optional_overrides:` of the root defaults carrier: key NAMES the
    platform recognises but assigns no value to (#1310).

    ⛔ The one thing still read from a file here, because `da-guard
    effective` does not report it. Read as the exporter reads the list
    (`[]string`): each entry's source text, from the composed node tree —
    nothing is CONSTRUCTED, so no value is interpreted (a `2024-02-30`
    elsewhere in the file does not end the run, an unknown tag is ignored).
    Called only after `da-guard effective` loaded the tree, so the file
    decodes for the exporter; a file PyYAML cannot compose still is
    answered with [] and one WARN, never a traceback.

    WHICH file is the exporter's choice too: the root entry of the tenant's
    `defaults_chain` (/effective), not a carrier this file selects — so no
    Python walk of the tree (and no "read FLAT" warning) happens here.
    """
    if not defaults_chain or "/" in defaults_chain[0]:
        return []   # no root defaults carrier on this tenant's path
    path = base / defaults_chain[0]
    try:
        with open(path, encoding="utf-8") as f:
            root = yaml.compose(f, Loader=yaml.SafeLoader)
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"  WARN: {safe_label(path.name)}: optional_overrides not read "
              f"(declared left empty): {e.__class__.__name__}", file=sys.stderr)
        return []
    if not isinstance(root, yaml.MappingNode):
        return []
    for key, value in root.value:
        if isinstance(key, yaml.ScalarNode) and key.value == "optional_overrides":
            if not isinstance(value, yaml.SequenceNode):
                return []
            return [item.value for item in value.value
                    if isinstance(item, yaml.ScalarNode)
                    and item.tag != "tag:yaml.org,2002:null"]
    return []


def resolve_inheritance_chain(tenant: str, config_dir: str | None) -> dict[str, object] | None:
    """Resolve the inheritance chain for a tenant from threshold-exporter's own
    answers (#2526 / #2547): which keys the tenant has, its profile and each
    key's layer from `da-guard effective` (/effective); each value from
    `da-guard served-values` (/metrics).

    Returns None when `config_dir` is not a directory or the tree has no
    tenant `tenant` (a line on stderr says which), else a dict with:
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
    """
    if not config_dir:
        return None
    base = Path(config_dir)
    if not base.is_dir():
        return None

    tree = load_effective_tree(base)
    for s in tree.skipped:
        print(f"  WARN: {safe_label(s.file)}: {safe_label(s.reason)}", file=sys.stderr)
    te = tree.tenants.get(tenant)
    if te is None:
        print(f"  WARN: no tenant '{safe_label(tenant)}' in {safe_label(str(base))}: "
              f"threshold-exporter reads {len(tree.tenants)} tenant(s) there",
              file=sys.stderr)
        return None
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

    served_tree = load_served_tree(base)
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
          *, out: TextIO | None = None) -> dict:
    """Emit one JSON health document for `tenant`.

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
    errors = []
    skipped = []

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
        pod_status = run_cmd(["kubectl", "get", "pods", "-n", tenant, "-l", "app=mariadb",
                              "-o", "jsonpath={.items[0].status.phase}"])
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
    operational_mode = "normal"
    try:
        maint_results, maint_err = query_prometheus(prom_url, f'user_state_filter{{tenant="{tenant}",filter="maintenance"}}')
        if not maint_err and maint_results:
            operational_mode = "maintenance"

        if operational_mode == "normal":
            silent_results, silent_err = query_prometheus(prom_url, f'user_silent_mode{{tenant="{tenant}"}}')
            if not silent_err and silent_results:
                severities = [r.get("metric", {}).get("target_severity", "") for r in silent_results]
                if "warning" in severities and "critical" in severities:
                    operational_mode = "silent:all"
                elif severities:
                    operational_mode = f"silent:{severities[0]}"
    except (OSError, ValueError):
        pass  # Non-fatal: mode query failure doesn't affect health status

    # 4. Profile lookup + inheritance chain (v1.12.0, optional — requires --config-dir)
    # #1522: ONE read of config_dir. The profile is the chain's own
    # `profile_name`. #2526: that read is `da-guard effective`; when it
    # cannot be made, this raises (the CLI exits 2) rather than answer
    # without the chain.
    inheritance = resolve_inheritance_chain(tenant, config_dir) if config_dir else None
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
        # #2968: an extra the findings above do not depend on — its timeout is
        # recorded beside them, never raised over them (that turned a finding,
        # rc 1, into an environment error, rc 2).
        logs_error = None
        try:
            logs = run_cmd(["kubectl", "logs", "-n", tenant, "deploy/mariadb", "-c", "mariadb",
                            "--tail=20"]) if is_mariadb else None
        except CommandTimeoutError as exc:
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

    if args.show_inheritance:
        if not args.config_dir:
            print("ERROR: --show-inheritance requires --config-dir",
                  file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)  # #452: missing required arg
        # #2526: fail closed — da-guard missing or failing, or a file the
        # exporter's load drops, exits 2 through the decorators on main().
        inheritance = resolve_inheritance_chain(args.tenant, args.config_dir)
        if inheritance:
            print(format_json_report(inheritance, default=str))
        else:
            print(json.dumps({"error": "Could not resolve inheritance chain"},
                             indent=2))
        sys.exit(EXIT_OK)

    try:
        result = check(args.tenant, args.prometheus, config_dir=args.config_dir)
    except CommandNotFoundError as exc:
        # issue 1513: this was a traceback at rc=1. The Pod check needs kubectl
        # and a cluster context; the da-tools image ships neither.
        print(f"ERROR: {exc}; the MariaDB Pod check needs kubectl and cluster "
              "access", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    except CommandTimeoutError as exc:
        # #2968: a kubectl that does not answer is the environment, like a
        # missing one: rc 2 and one line, not a traceback at rc 1.
        print(f"ERROR: {exc}; the MariaDB Pod check needs kubectl and a cluster "
              "that answers", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    # issue 1513: `status: error` used to exit 0, so `da-tools diagnose t && …`
    # passed on an unhealthy tenant.
    sys.exit(exit_code(result))


if __name__ == "__main__":
    main()
