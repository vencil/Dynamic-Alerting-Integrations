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

import yaml

# Add script dir to path for lib imports
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import detect_cli_lang, http_get_json, query_prometheus_instant  # noqa: E402

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
        'zh': 'Prometheus Query API URL (預設: http://localhost:9090; 叢集內建議用 http://prometheus.monitoring.svc.cluster.local:9090)',
        'en': 'Prometheus Query API URL (default: http://localhost:9090; for in-cluster, use http://prometheus.monitoring.svc.cluster.local:9090)'
    },
    'config_dir': {
        'zh': '租戶配置目錄路徑 (conf.d/)，用於設定檔查詢',
        'en': 'Path to tenant config directory (conf.d/) for profile lookup'
    },
    'show_inheritance': {
        'zh': '顯示詳細的四層繼承鏈解析 (需要 --config-dir)',
        'en': 'Show detailed four-layer inheritance chain resolution (requires --config-dir)'
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
    if not config_dir or not os.path.isdir(config_dir):
        return None
    for fname in sorted(os.listdir(config_dir)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        if fname.startswith("."):
            continue
        fpath = os.path.join(config_dir, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
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
    """Resolve the four-layer inheritance chain for a tenant.

    Returns a dict with:
      - chain: list of layers with source and keys
      - resolved: final merged key→value after all layers
      - profile_name: profile name or None

    Four-layer inheritance (v1.12.0):
      1. Global Defaults (_defaults.yaml)
      2. Profile Overlay (_profiles.yaml → profile keys fill-in)
      3. Tenant Override (tenant-specific keys)
    """
    if not config_dir or not os.path.isdir(config_dir):
        return None

    # Layer 1: Global defaults
    defaults_path = os.path.join(config_dir, "_defaults.yaml")
    defaults_raw = {}
    try:
        with open(defaults_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        defaults_raw = raw.get("defaults", {}) if isinstance(raw, dict) else {}
    except (OSError, yaml.YAMLError):
        pass

    # Find tenant config
    tenant_overrides = {}
    for fname in sorted(os.listdir(config_dir)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        if fname.startswith("."):
            continue
        fpath = os.path.join(config_dir, fname)
        try:
            with open(fpath, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
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
            tenant_overrides = tenants[tenant]
            break

    # Layer 2: Profile overlay
    profile_name = None
    profile_keys = {}
    p_ref = tenant_overrides.get("_profile")
    if p_ref and isinstance(p_ref, str):
        profile_name = p_ref.strip()
        profiles_path = os.path.join(config_dir, "_profiles.yaml")
        try:
            with open(profiles_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            all_profiles = raw.get("profiles", {}) if isinstance(raw, dict) else {}
            profile_keys = all_profiles.get(profile_name, {})
        except (OSError, yaml.YAMLError):
            pass

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

    return {
        "chain": chain,
        "resolved": resolved,
        "profile_name": profile_name,
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
    return {
        "layers": layers,
        "resolved_count": len(inheritance.get("resolved", {})),
        "profile": inheritance.get("profile_name"),
    }


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
    parser.add_argument("--prometheus", default="http://localhost:9090",
                        help=_h('prometheus'))
    parser.add_argument("--config-dir",
                        help=_h('config_dir'))
    parser.add_argument("--show-inheritance", action="store_true",
                        help=_h('show_inheritance'))
    args = parser.parse_args()

    if args.show_inheritance:
        if not args.config_dir:
            print("ERROR: --show-inheritance requires --config-dir",
                  file=sys.stderr)
            sys.exit(1)
        inheritance = resolve_inheritance_chain(args.tenant, args.config_dir)
        if inheritance:
            print(json.dumps(inheritance, indent=2, ensure_ascii=False,
                             default=str))
        else:
            print(json.dumps({"error": "Could not resolve inheritance chain"},
                             indent=2))
        sys.exit(0)

    check(args.tenant, args.prometheus, config_dir=args.config_dir)
