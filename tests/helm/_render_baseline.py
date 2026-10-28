"""Render baseline for helm/tenant-api (#2855).

The tenant-api chart fails to render until ``writeBack.mode`` is set. Tests
that render it for another reason pass :func:`baseline_args` right after the
chart path, so their own ``--set`` / ``-f`` arguments still win.
"""
from __future__ import annotations

from pathlib import Path

_TENANT_API_BASELINE = Path(__file__).with_name("tenant_api_render_baseline.yaml")


def baseline_args(chart: str | Path) -> list[str]:
    """``["-f", <baseline>]`` for helm/tenant-api, ``[]`` for any other chart."""
    if Path(chart).parts[-2:] == ("helm", "tenant-api"):
        return ["-f", str(_TENANT_API_BASELINE)]
    return []


# ── tenant-api settings: TA_* env, not args (ADR-036 §12) ──────────────

import re  # noqa: E402

_TENANT_API_GO = Path(__file__).resolve().parents[2] / "components" / "tenant-api"
#: A flag registered with a TA_* variable as its default: `"flag-name",
#: envOrDefault("TA_X"` (also envBool / envNonEmptyOrDefault — every env*
#: helper cmd/server reads a flag's default with — and the duration flags'
#: `parseDurationOrDefault(os.Getenv("TA_X"), …)`).
_FLAG_ENV = re.compile(r'"([a-z][a-z0-9-]*)",\s*(?:env[A-Za-z]*|parseDurationOrDefault\(\s*os\.Getenv)'
                       r'\(\s*"(TA_[A-Z0-9_]+)"')
#: Every TA_* variable the tenant-api build reads — the list its startup check
#: accepts (cmd/server/known_env.go, generated from the reading sites and held
#: to them by TestKnownTAEnv_IsTheSourceList).
_KNOWN_ENV_GO = _TENANT_API_GO / "cmd" / "server" / "known_env.go"


def tenant_api_known_envs() -> frozenset[str]:
    """The TA_* names tenant-api accepts in its environment (known_env.go)."""
    names = frozenset(re.findall(r'"(TA_[A-Z0-9_]+)"', _KNOWN_ENV_GO.read_text(encoding="utf-8")))
    assert len(names) >= 20, f"{_KNOWN_ENV_GO}: only {len(names)} names"
    return names


def tenant_api_flag_envs() -> dict[str, str]:
    """flag name → the TA_* variable tenant-api reads it from, derived from
    the Go source (cmd/ and internal/, tests excluded) — never a hand list."""
    out: dict[str, str] = {}
    for p in sorted(_TENANT_API_GO.rglob("*.go")):
        if p.name.endswith("_test.go"):
            continue
        for flag, env in _FLAG_ENV.findall(p.read_text(encoding="utf-8")):
            assert out.get(flag, env) == env, (flag, env, out[flag])
            out[flag] = env
    return out


def tenant_api_setting(container: dict, flag: str) -> "str | None":
    """The value the tenant-api container gives *flag*: its TA_* env value
    when the flag has one (the chart passes those as env), else `--flag=v`
    in args; None when absent. A bare switch passed as env reads "true"."""
    env = tenant_api_flag_envs().get(flag)
    if env is not None:
        values = [e.get("value") for e in container.get("env") or [] if e.get("name") == env]
    else:
        values = [a.split("=", 1)[1] for a in container.get("args") or []
                  if isinstance(a, str) and a.startswith(f"--{flag}=")]
    assert len(values) <= 1, (flag, values)
    return values[0] if values else None
