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
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

# Add script dir to path for lib imports
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import detect_cli_lang, http_get_json, query_prometheus_instant, add_prometheus_arg  # noqa: E402
from _lib_python import format_json_report  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_CALLER_ERROR  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_confd import (  # noqa: E402
    iter_config_files,
    unusable_config_paths,
    unusable_reason,
    warn_nested,
)

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


def run_cmd(cmd: list[str]) -> str | None:
    """Execute a command safely using list arguments only (no shell=True).

    Args:
        cmd: Command as a list of strings. String input is rejected
             to prevent potential command injection via shlex parsing.
    """
    if not isinstance(cmd, list):
        raise TypeError(f"run_cmd() requires list argument, got {type(cmd).__name__}")
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=120).strip()
    except subprocess.CalledProcessError:
        return None


# Alias for backward-compat within this module
query_prometheus = query_prometheus_instant


def lookup_tenant_profile(tenant: str, config_dir: str | None) -> str | None:
    """Look up the _profile assignment for a tenant from config-dir YAML files.

    Returns profile name string or None.
    """
    if not config_dir:
        return None
    base = Path(config_dir)
    if not base.is_dir():
        return None
    # #1339: flat read — a hierarchical conf.d must not look empty.
    warn_nested(base, tool="diagnose")
    # #1469: the selection predicate is `_lib_confd`'s, not a fourth
    # hand-rolled copy. `iter_config_files` already applies `_is_config`
    # (suffix + not hidden) and, on the `recursive=False` branch, `is_file()`
    # — the three checks that used to sit inline here.
    for entry in iter_config_files(base, recursive=False):
        fname = entry.name
        try:
            with open(entry, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            # ⛔ Still silent, deliberately — see #1522. `check()` calls this
            # AND `resolve_inheritance_chain` over the same directory, so
            # announcing here would print every skip twice. The signal for a
            # `check()` caller comes from the sibling; a caller reaching this
            # function on its own still gets nothing, which is what #1522 is.
            continue
        if not isinstance(raw, dict):
            continue
        tenants = {}
        if "tenants" in raw and isinstance(raw.get("tenants"), dict):
            tenants = raw["tenants"]
        elif not fname.startswith("_"):
            t_name = fname.rsplit(".", 1)[0]
            tenants = {t_name: raw}
        if tenant in tenants and isinstance(tenants[tenant], dict):
            profile = tenants[tenant].get("_profile")
            if profile and isinstance(profile, str):
                return profile.strip()
    return None


def resolve_inheritance_chain(tenant: str, config_dir: str) -> dict[str, object]:
    """Resolve the inheritance chain for a tenant.

    Returns a dict with:
      - chain: list of layers with source and keys
      - resolved: final merged key→value after all layers
      - profile_name: profile name or None
      - declared: key NAMES the platform recognises but assigns no value to
        (`_defaults.yaml` `optional_overrides:`, #1310)

    THREE chain layers — the same three this function has always emitted:
      1. Global Defaults (_defaults.yaml)
      2. Profile Overlay (_profiles.yaml → profile keys fill-in)
      3. Tenant Override (tenant-specific keys)

    ⛔ Do NOT "correct" this back to four. The v1.12.0 model named four
    (`ApplyProfiles` in
    components/threshold-exporter/app/pkg/config/resolve.go), but its layer 2
    is the Rule Pack Baseline, which is
    "embedded in defaults" — the onboarding generator merges it into
    `_defaults.yaml` before this function ever opens the file, so it is not
    separately observable here and was never emitted as a chain entry. The
    docstring said "four" over a list of three for exactly that reason;
    counting what we actually return is the honest repair.

    ⚠️ Separately, this reader is FLAT: it opens `<config-dir>/_defaults.yaml`
    and non-recursively iterates `<config-dir>/*.yaml`, so the exporter's
    directory hierarchy (platform → domain → region → tenant, see
    components/threshold-exporter/README.md §4.2) collapses into layer 1 here.
    A tenant configured through nested `_defaults.yaml` files gets a chain that
    is correct about the merge RESULT of the top level only.

    ⛔ `declared` is deliberately NOT merged into `resolved`, and is not a chain
    layer. The chain answers "what value does this tenant end up with"; a
    declared key has no value until the tenant writes one, so folding it in
    would invent a number the platform explicitly refuses to assert. It is
    reported alongside because this function is what `--show-inheritance`
    prints, and that command is the documented answer to "which metric keys can
    I set?" — an answer that read `defaults:` alone was simply wrong about the
    keys that need it most.
    """
    if not config_dir:
        return None
    base = Path(config_dir)
    if not base.is_dir():
        return None

    # #1468: every `except (OSError, yaml.YAMLError)` below used to `pass` /
    # `continue` in silence, so a `conf.d/` this function could only half
    # read produced a TRUNCATED chain at rc=0 with zero bytes on stderr —
    # measured: `acme.yaml` missing one `]` turned `['defaults','tenant']`
    # into `['defaults']`, and nothing said why. The skips are now recorded
    # (L2, `skipped_unusable_files`, same field name and shape #1448 gave
    # `validate-config`) and announced (L1, the `WARN: skip ...` line
    # `_grar_parse` already prints).
    #
    # ⛔ The exit code is deliberately NOT changed: `batch_diagnose.py`
    # imports `check` in-process and its aggregation semantics would have to
    # be redefined first. Tracked separately — see #1468 L3.
    skipped: list[str] = []
    _said: set[tuple[str, str]] = set()

    def _skip(label: str, reason: str) -> None:
        """Record once per FILE, announce once per (file, reason).

        ⛔ One file can reach here twice. `_profiles.yaml` is the case that
        exists today: the tenant-file loop opens every `*.yaml` in the
        directory — including it — and the Layer 2 profile read opens it
        again by name. Appending blindly put the same path in
        `skipped_unusable_files` twice, and that field is a list of FILES;
        a consumer counting it (batch_diagnose reads this in-process) would
        double-count one broken file. Caught by
        test_diagnose_names_an_unreadable_profiles_file.

        The two dedup keys differ on purpose. The list is per-file, because
        that is what the field means. The WARN is per (file, reason),
        because the second visit can fail for a DIFFERENT reason than the
        first and an operator needs both — suppressing by file alone would
        trade a duplicate line for a lost one.
        """
        if label not in skipped:
            skipped.append(label)
        if (label, reason) not in _said:
            _said.add((label, reason))
            # #1538: `label` is a tenant-controlled FILENAME and `reason` is the
            # exception text derived from it. Escaped at the print, not in
            # `skipped` above — that list is emitted under --json, whose bytes
            # must not change (json.dumps already escapes control chars).
            print(f"  WARN: skip {safe_label(label)}: {safe_label(reason)}",
                  file=sys.stderr)

    # #1469: same selection predicate as `validate-config` and the routing
    # parser — `_lib_confd`, not a fourth hand-rolled copy. The paired
    # `unusable_config_paths` pass names a config-named directory, a broken
    # symlink or an untraversable directory in the SAME words the other two
    # readers use (`unusable_reason`), instead of leaking a raw
    # `IsADirectoryError` through the `except` clauses below. Unifying the
    # predicate without this pass would make those paths silent here — the
    # direction #1469 exists to reverse.
    #
    # ⛔ IT RUNS BEFORE LAYER 1, not before the tenant loop, and that is a
    # fix rather than tidying: `_defaults.yaml` and `_profiles.yaml` are
    # opened BY NAME, so with the pass sitting lower a `_defaults.yaml`
    # that is a DIRECTORY produced two lines for one cause, in two
    # different vocabularies, the first of them a bare errno carrying an
    # absolute path —
    #
    #     WARN: skip _defaults.yaml: IsADirectoryError: [Errno 21] Is a
    #           directory: '/abs/.../conf.d/_defaults.yaml'
    #     WARN: skip _defaults.yaml: is a directory, not a config file
    #
    # — which is the two-answers-for-one-file shape this whole change set
    # is against, produced by the guard against it. `_skip` dedupes on
    # (file, reason) and these are two different reasons, so the dedupe
    # could not collapse them; only the ORDER can. Running first means the
    # shared wording is already recorded when Layer 1's `except` fires, and
    # `_skip`'s own bookkeeping keeps `skipped_unusable_files` at one entry.
    named_by_shared_pass: set[str] = set()
    for bad in unusable_config_paths(base, recursive=False):
        _skip(bad.name, unusable_reason(bad))
        named_by_shared_pass.add(bad.name)

    def _skip_read_failure(label: str, e: BaseException) -> None:
        """Record a read failure UNLESS the shared pass already spoke.

        Ordering alone does not collapse the duplicate — `_skip` dedupes on
        (file, reason) and a bare errno is a different reason from
        `unusable_reason`'s clause, so both would print. This is the half
        that picks ONE vocabulary: if the shared enumerator already named
        the path, its wording stands and the errno is dropped.
        """
        if label in named_by_shared_pass:
            return
        _skip(label, f"{e.__class__.__name__}: {' '.join(str(e).split())}")

    # Layer 1: Global defaults
    defaults_path = base / "_defaults.yaml"
    defaults_raw = {}
    declared = []
    try:
        with open(defaults_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if isinstance(raw, dict):
            defaults_raw = raw.get("defaults", {}) or {}
            listed = raw.get("optional_overrides") or []
            declared = [k for k in listed if isinstance(k, str)]
    except FileNotFoundError:
        # Absent `_defaults.yaml` is a legal config, not a read failure.
        pass
    except (OSError, yaml.YAMLError) as e:
        _skip_read_failure(defaults_path.name, e)

    # Find tenant config
    tenant_overrides = {}
    # #1339: flat read — a hierarchical conf.d must not look empty.
    warn_nested(base, tool="diagnose")
    for entry in iter_config_files(base, recursive=False):
        fname = entry.name
        try:
            with open(entry, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            _skip_read_failure(fname, e)
            continue
        if not isinstance(raw, dict):
            _skip(fname, f"top level must be a mapping, got "
                         f"{type(raw).__name__}")
            continue
        tenants = {}
        if "tenants" in raw and isinstance(raw.get("tenants"), dict):
            tenants = raw["tenants"]
        elif not fname.startswith("_"):
            t_name = fname.rsplit(".", 1)[0]
            tenants = {t_name: raw}
        if tenant in tenants and isinstance(tenants[tenant], dict):
            tenant_overrides = tenants[tenant]
            break

    # Layer 2: Profile overlay
    profile_name = None
    profile_keys = {}
    p_ref = tenant_overrides.get("_profile")
    if p_ref and isinstance(p_ref, str):
        profile_name = p_ref.strip()
        profiles_path = base / "_profiles.yaml"
        try:
            with open(profiles_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            # ⛔ NOT `or {}`. That coerces every FALSY document — `[]`, `0`,
            # `false` — into an empty mapping, so a `_profiles.yaml` whose
            # whole body is `[]` loses the profile layer with zero signal:
            # the exact defect #1468 is about, inside the fix for #1468.
            # Only `None` (an empty document) is legitimately nothing.
            #
            # ⛔ And the `profiles:` VALUE needs its own check. `raw` can be
            # a fine mapping whose `profiles:` is a list, and the old
            # `all_profiles.get(...)` then raised AttributeError — which is
            # NOT in the `except (OSError, yaml.YAMLError)` below, so it
            # escaped and killed the whole call. That is #1447's death
            # ("parses cleanly, is not a mapping, reaches .get(), takes the
            # run with it") reproduced one directory over.
            if raw is None:
                raw = {}
            if not isinstance(raw, dict):
                _skip(profiles_path.name,
                      f"top level must be a mapping, got {type(raw).__name__}")
            else:
                all_profiles = raw.get("profiles")
                if all_profiles is None:
                    all_profiles = {}
                if not isinstance(all_profiles, dict):
                    _skip(profiles_path.name,
                          f"'profiles' must be a mapping, got "
                          f"{type(all_profiles).__name__}")
                else:
                    profile_keys = all_profiles.get(profile_name, {})
        except FileNotFoundError:
            # No `_profiles.yaml` at all: the tenant references a profile
            # this directory does not define. Still a read the chain is
            # missing a layer because of, so it is recorded.
            _skip(profiles_path.name,
                  f"not found, but {tenant} references profile "
                  f"{profile_name}")
        except (OSError, yaml.YAMLError) as e:
            _skip_read_failure(profiles_path.name, e)

    # Layer 3: Tenant-specific (non-reserved metric keys only)
    tenant_metric_keys = {
        k: v for k, v in tenant_overrides.items()
        if not k.startswith("_")
    }

    # Build chain
    chain = []

    # Layer 1: defaults
    default_only = {k: v for k, v in defaults_raw.items() if not k.startswith("_")}
    if default_only:
        chain.append({"layer": "defaults", "source": "_defaults.yaml",
                       "keys": default_only})

    # Layer 2: profile (fill-in — keys NOT in tenant override)
    if profile_name and profile_keys:
        effective_profile = {
            k: v for k, v in profile_keys.items()
            if not k.startswith("_") and k not in tenant_metric_keys
        }
        chain.append({"layer": "profile", "source": f"_profiles.yaml → {profile_name}",
                       "keys": effective_profile})

    # Layer 3: tenant override
    if tenant_metric_keys:
        chain.append({"layer": "tenant", "source": f"{tenant}.yaml",
                       "keys": tenant_metric_keys})

    # Resolved: merge all layers (later layers win)
    resolved = {}
    resolved.update(default_only)
    if profile_keys:
        # Profile fills in only where tenant hasn't overridden
        for k, v in profile_keys.items():
            if not k.startswith("_") and k not in tenant_metric_keys:
                resolved[k] = v
    resolved.update(tenant_metric_keys)

    out: dict[str, object] = {
        "chain": chain,
        "resolved": resolved,
        "profile_name": profile_name,
        # settable, but with no platform value — see the docstring
        "declared": declared,
    }
    # #1468: only when non-empty. An always-present empty list on a healthy
    # run is the shape `validate_config.print_report` deliberately pops off
    # its own rows; a caveat field that appears on clean output stops being
    # read as a caveat.
    if skipped:
        out["skipped_unusable_files"] = skipped
    return out


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
    # #1468: the truncation caveat rides along with the summary, because the
    # summary IS what `check()` puts in its JSON and what
    # `batch_diagnose.py` reads back — a `layers` list short of a layer must
    # not be the only trace that a file could not be read.
    skipped = inheritance.get("skipped_unusable_files") or []
    if skipped:
        summary["skipped_unusable_files"] = list(skipped)
    return summary


def check(tenant: str, prom_url: str, config_dir: str | None = None) -> str:
    errors = []

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
            errors.append(f"Prometheus query failed ({prom_url})")
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
    profile_name = lookup_tenant_profile(tenant, config_dir)
    inheritance = resolve_inheritance_chain(tenant, config_dir) if config_dir else None

    # 5. 輸出結果 (Token Saving 核心：正常時只回傳極簡 JSON)
    if not errors:
        result = {"status": "healthy", "tenant": tenant}
        if operational_mode != "normal":
            result["operational_mode"] = operational_mode
        if profile_name:
            result["profile"] = profile_name
        if inheritance:
            result["inheritance_chain"] = _format_chain_summary(inheritance)
        print(json.dumps(result))
    else:
        # 只有異常時，嘗試抓取最近的 error log
        logs = run_cmd(["kubectl", "logs", "-n", tenant, "deploy/mariadb", "-c", "mariadb", "--tail=20"])
        error_logs = [line for line in (logs or "").split('\n') if 'ERROR' in line]

        result = {
            "status": "error",
            "tenant": tenant,
            "issues": errors,
            "recent_logs": error_logs[:3],  # 只回傳最後 3 行錯誤
        }
        if operational_mode != "normal":
            result["operational_mode"] = operational_mode
        if profile_name:
            result["profile"] = profile_name
        if inheritance:
            result["inheritance_chain"] = _format_chain_summary(inheritance)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
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
        inheritance = resolve_inheritance_chain(args.tenant, args.config_dir)
        if inheritance:
            print(format_json_report(inheritance, default=str))
        else:
            print(json.dumps({"error": "Could not resolve inheritance chain"},
                             indent=2))
        sys.exit(EXIT_OK)

    check(args.tenant, args.prometheus, config_dir=args.config_dir)
