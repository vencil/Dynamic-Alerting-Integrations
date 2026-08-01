"""⏳ TEMPORARY TRIPWIRE — delete this file with the emission loop (#1189 / TRK-337).

`optional_overrides:` names threshold keys the platform recognises but supplies
no value for. Validation accepts a tenant override on such a key (Go
`ValidateTenantKeys`, Python `validate_tenant_keys`), but nothing emits one yet:
`resolveBaseRows` iterates `Defaults`, so the key is never visited. A tenant who
set one would get a 200, an empty `validation_warnings`, and no `user_threshold`
row — no error, no notice, not even a scrape-time log line. Strictly less signal
than the `<base>_critical` shape the same validator refuses.

That asymmetry is defensible only because the flat shape is UNREACHABLE: no
supported producer can put a key on the list. This file makes that mechanical
instead of a promise, because "it happens to be empty today" is the kind of
claim that quietly stops being true.

Two directions, both fail-loud:

  1. Nothing may declare a key while the emission loop is absent.
  2. Once the emission loop exists, this tripwire is stale and must go — a
     lingering guard that can no longer fire is the "gate exists but never
     runs" shape this repo keeps finding in its own gates.

Removal: land `resolveDeclaredRows`, delete this file in the same commit.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories with no bearing on what a deployment or a fixture can declare.
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "site", "__pycache__"}

# The Go symbol that would make this tripwire obsolete. Named, not pattern-
# matched, so renaming the loop does not silently retire the guard.
_EMISSION_SYMBOL = "resolveDeclaredRows"
_RESOLVE_GO = REPO_ROOT / "components" / "threshold-exporter" / "app" / "pkg" / "config" / "resolve.go"


def _platform_files() -> list[Path]:
    """Every file a declaration could actually live in.

    Both shapes the exporter honours: any `_`-prefixed platform YAML anywhere in
    a conf.d tree (flat_scanner accepts the whole `_*` class, not just
    `_defaults.yaml`), plus the Helm values that render the shipped one.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if not name.endswith((".yaml", ".yml")):
                continue
            if name.startswith("_") or name == "values.yaml":
                found.append(Path(dirpath) / name)
    return found


def _declared_keys(doc: object) -> list[str]:
    """`optional_overrides` at the top level, or under Helm's thresholdConfig."""
    if not isinstance(doc, dict):
        return []
    out: list[str] = []
    for holder in (doc, doc.get("thresholdConfig")):
        if isinstance(holder, dict):
            raw = holder.get("optional_overrides")
            if isinstance(raw, list):
                out.extend(str(k) for k in raw)
    return out


def test_nothing_declares_optional_overrides_before_emission_exists():
    """⛔ Direction 1: a declaration is unreachable-by-construction until the
    emission loop exists. The moment one appears, a tenant can write a key that
    silently produces nothing."""
    files = _platform_files()
    # An empty corpus would make this pass forever while measuring nothing —
    # the exact failure mode this repo has found in its own gates.
    assert len(files) >= 10, f"file sweep looks broken: {len(files)} candidates"

    offenders: list[str] = []
    for path in files:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError):
            continue  # malformed files are other gates' business
        keys = _declared_keys(doc)
        if keys:
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {keys}")

    assert not offenders, (
        "optional_overrides is declared, but nothing emits a row for such a key yet "
        "(resolveBaseRows iterates Defaults). A tenant setting one of these would be "
        "accepted with no error, no notice and no log line, and their alert would "
        "never fire.\n  " + "\n  ".join(offenders) +
        f"\n\nLand the emission loop ({_EMISSION_SYMBOL}) first, then delete "
        "tests/shared/test_optional_overrides_tripwire.py in that same commit."
    )


def test_tripwire_is_removed_once_the_emission_loop_lands():
    """⛔ Direction 2: this guard must not outlive its purpose.

    Once declared keys emit, direction 1 above would start blocking the very
    thing the feature exists to do — and a guard that can no longer fire for a
    real reason is indistinguishable from one that is simply green.
    """
    if not _RESOLVE_GO.exists():
        pytest.skip(f"{_RESOLVE_GO} not found")
    source = _RESOLVE_GO.read_text(encoding="utf-8")
    assert _EMISSION_SYMBOL not in source, (
        f"{_EMISSION_SYMBOL} exists, so declared keys now emit and this tripwire is "
        "stale: it would block the feature it was written to protect. Delete "
        "tests/shared/test_optional_overrides_tripwire.py."
    )
