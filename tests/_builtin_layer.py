"""The built-in layer (#3832) as tests see it.

The readers of conf.d (exporter, da-guard, tenant-api) are compiled with a
table of the thresholds the platform ships armed, generated from the
threshold registry into
components/threshold-exporter/app/pkg/config/builtin_defaults_gen.go. Every
tenant's effective config and served values hold those keys under the root
`defaults:` unless the tree overrides them, so a test whose table names only
the tree's own keys lays these under its expectation (with_builtin) or takes
them out of the answer (without_builtin) — read from the generated table, not
written down, as the table keeps growing.
"""
from __future__ import annotations

import re
from pathlib import Path

_GEN = (Path(__file__).resolve().parents[1] / "components" / "threshold-exporter"
        / "app" / "pkg" / "config" / "builtin_defaults_gen.go")


def _load() -> dict[str, float]:
    text = _GEN.read_text(encoding="utf-8")
    out = {k: float(v) for k, v in re.findall(r'\{"([^"]+)", ([-0-9.eE+]+)\}', text)}
    if not out:
        raise AssertionError(f"no built-in entries parsed from {_GEN}")
    return out


#: key -> the platform's value (float; an integral value compares equal to
#: the int da-guard's JSON decodes to).
BUILTIN_DEFAULTS: dict[str, float] = _load()

_OFF_GEN = _GEN.with_name("unset_threshold_keys_gen.go")


def _load_off() -> dict[str, str]:
    text = _OFF_GEN.read_text(encoding="utf-8")
    out = dict(re.findall(r'\{"([^"]+)", "([^"]+)"\}', text))
    if not out:
        raise AssertionError(f"no off-table entries parsed from {_OFF_GEN}")
    return out


#: The layer's off table (#4163): key -> the rule pack that reads it. These
#: keys hold no number in any reader's answer (served values, effective
#: config) unless the tree writes one; a tenant value for one is read.
BUILTIN_OFF: dict[str, str] = _load_off()


def with_builtin(cfg: dict) -> dict:
    """*cfg* with every built-in key it does not hold laid under it."""
    out = dict(BUILTIN_DEFAULTS)
    out.update(cfg)
    return out


def without_builtin(cfg: dict, keep=()) -> dict:
    """*cfg* without the built-in keys, except those named in *keep* (the
    keys the fixture itself writes)."""
    keep = set(keep)
    return {k: v for k, v in cfg.items() if k not in BUILTIN_DEFAULTS or k in keep}
