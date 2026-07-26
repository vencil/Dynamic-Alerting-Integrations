#!/usr/bin/env python3
"""Threshold unit-sanity gate — the value-range validation #992 promised and nobody built.

WHY: #944 recalibrated `mysql_cpu` from a host-CPU% misnomer to
`threads_running` saturation (80→30). #992 (`dcfa129e`) accepted keeping the
config KEY as-is, and wrote down the residual risk plus its cheaper mitigation
verbatim:

    "The residual footgun (a tenant setting mysql_cpu to a percentage-shaped
     number) is cheaper to address with value-range validation than a rename,
     if it ever materialises."

It materialised. #1217 cleaned eleven user-visible surfaces still teaching the
old magnitude — and the mitigation was still zero lines: both
`docs/schemas/platform-defaults.schema.json` ("values left loose") and
`tenant-config.schema.json`'s `thresholdScalar` (bare `type: string`) decline
to constrain threshold VALUES. This file is that mitigation.

It is deliberately NOT `mysql_cpu`-shaped. Two independent adversarial reviews
converged on the same refutation of "the key name is the disease": the identical
and WORSE bug lives on `pg_connections`, whose name is perfectly correct —
`recommendation-data.json` recommends 300 "count" while the registry declares
`% of max_connections` and `rule-pack-postgresql.yaml` compares against
`tenant:pg_connection_usage:ratio` = `100 * used / max` (a 0-100 quantity). A
tenant clicking the portal's "Apply Recommended Values" gets `pg_connections:
300`, which `PostgresHighConnections` can never cross. One button, one silently
disabled shipped alert. The disease is semantics hand-copied across surfaces
with no generator and no gate; the cure is to make the semantics checkable.

SoT: `rule-packs/threshold-registry.yaml` (#1200 D2) already carries `unit` per
key. This gate types that free-text unit into a DIMENSION and enforces two
invariants across every structured artifact that carries threshold values:

  1. UNIT-DRIFT — an asset that *declares* a unit for a registry key must
     declare the registry's unit. (Catches the `mysql_cpu: unit "%"` and
     `pg_connections: unit "count"` class at the source.)
  2. OUT-OF-DOMAIN — a value must lie in its dimension's domain: a `percent`
     threshold above 100 can never be crossed; a `ratio` threshold above 1 the
     same; a `bytes` threshold in the single digits is a unit slip. These are
     the failure modes that are SILENT at runtime — the alert loads, evaluates
     forever, and never fires.

Unbounded dimensions (count / duration / rate) get only a `> 0` floor: their
right magnitude is workload-dependent and no static rule can know it. Saying so
is the point — see `docs/internal/dev-rules.md` on fail-loud over fail-quiet.

UNKNOWN UNIT IS A VIOLATION, not a skip. The registry's unit vocabulary is
small (18 strings / 65 keys today) and closed in practice; a new unit string
must be classified here deliberately, otherwise the gate would silently stop
covering whatever pack introduced it — the exact fail-quiet shape this file
exists to remove.

Exit codes (_lib_exitcodes): 0 clean / 1 violation (--ci) / 2 caller error.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

import yaml

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)

_OPS = os.path.join(PROJECT_ROOT, "scripts", "tools", "ops")
sys.path.insert(0, _OPS)
import _registry_lib as registry_lib  # noqa: E402

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

# ---------------------------------------------------------------- dimensions

# registry `unit` string → dimension. Extend deliberately: an unclassified unit
# is a hard error (see module docstring), never a silent skip.
UNIT_DIMENSION: dict[str, str] = {
    "%": "percent",
    "% of max_connections": "percent",
    "ratio": "ratio",
    "0=green,1=yellow,2=red": "enum",
    "bytes (4GB)": "bytes",
    "bytes (8GB)": "bytes",
    "count": "count",
    "threads": "count",
    "messages": "count",
    "seconds": "duration",
    "seconds/5m": "duration",
    "ms": "duration",  # es_search_latency_ms (#1196 D) — millisecond latency threshold
    "msg/s": "rate",
    "req/s": "rate",
    "keys/s": "rate",
    "ops/s": "rate",
    "s/s": "rate",
    "count/s": "rate",
    "qps": "rate",
}

# A bytes-dimension threshold below this is a unit slip (someone wrote "4"
# meaning 4 GB). 1 MiB is far under any real memory/disk threshold we ship.
_BYTES_FLOOR = 1024 * 1024

_ENUM_RE = re.compile(r"(\d+)\s*=")


# Dimensions where a zero threshold is meaningless: `container_cpu: 0` or
# `redis_memory_used_bytes: 0` fires permanently. Deliberately NOT count/rate —
# there, zero is the idiomatic "alert if ANY" threshold and the platform ships
# it on purpose (`kafka_under_replicated_partitions: 0`, i.e. `> 0`). An earlier
# blanket `> 0` rule flagged that shipped-and-correct value; the gate must know
# the difference between "impossible" and "strict".
_ZERO_IS_MEANINGLESS = {"percent", "ratio", "bytes"}


def _domain_error(dimension: str, unit: str, value: float) -> str | None:
    """Return a violation message if `value` is outside `dimension`, else None."""
    if value < 0:
        return f"a negative threshold is never crossable, got {value:g}"
    if value == 0 and dimension in _ZERO_IS_MEANINGLESS:
        return (
            f"a zero {dimension} threshold fires permanently "
            f"(unit {unit!r}), got {value:g}"
        )
    if dimension == "percent" and value > 100:
        return (
            f"unit {unit!r} is a 0-100 quantity, but the value is {value:g} — "
            "the alert compares against a percentage and can NEVER cross it"
        )
    if dimension == "ratio" and value > 1:
        return (
            f"unit 'ratio' is a 0-1 quantity, but the value is {value:g} — "
            "looks like a percentage written where a ratio is expected "
            "(the alert can never cross it)"
        )
    if dimension == "bytes" and value < _BYTES_FLOOR:
        return (
            f"unit {unit!r} is in BYTES, but the value is {value:g} (< 1 MiB) — "
            "looks like a unit slip (e.g. '4' meaning 4 GB)"
        )
    if dimension == "enum":
        allowed = {float(m) for m in _ENUM_RE.findall(unit)}
        if allowed and value not in allowed:
            return f"unit {unit!r} enumerates {sorted(allowed)}, got {value:g}"
    return None


def _as_number(raw) -> float | None:
    """Coerce a threshold literal to a number, or None if it isn't one.

    conf.d thresholds are strings, may carry the inline `"70:critical"` severity
    suffix, and may be the sentinel `"disable"`. Anything non-numeric (dates in
    an `expires:` block, `type: scheduled`, label lists) is not a threshold.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str):
        return None
    text = raw.split(":", 1)[0].strip()
    try:
        return float(text)
    except ValueError:
        return None


# ------------------------------------------------------------------ scanning


def _walk_values(node, key_of_interest: str | None = None):
    """Yield (registry_key, number) for every threshold literal reachable in `node`.

    A registry key's value may be a scalar (`mysql_threads_running: "30"`) or a
    nested block (the scheduled form
    `mysql_threads_running: {default: {during_business_hours: "30"}}`).
    #1217 burned on exactly the nested shape: a line-oriented scan is
    structurally blind to it, so once we are inside a known key we collect EVERY
    numeric leaf beneath it.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if key_of_interest is None and isinstance(k, str):
                base = k.split("{", 1)[0]  # dimensional key: container_cpu{version="v2"}
                if base in _REGISTRY_KEYS:
                    yield from _walk_values(v, base)
                    continue
            if key_of_interest is not None:
                num = _as_number(v)
                if num is not None:
                    yield key_of_interest, num
                else:
                    yield from _walk_values(v, key_of_interest)
            else:
                yield from _walk_values(v, None)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_values(item, key_of_interest)
    elif key_of_interest is not None:
        num = _as_number(node)
        if num is not None:
            yield key_of_interest, num


_REGISTRY_KEYS: set[str] = set()

_MD_YAML_RE = re.compile(r"```ya?ml\n(.*?)```", re.S)

# Structured artifacts that carry threshold values. Markdown docs are scanned
# separately (best-effort fenced-YAML) because a fragment may not parse.
_YAML_GLOBS = [
    "rule-packs/threshold-registry.yaml",
    "helm/threshold-exporter/values.yaml",
    "environments/local/threshold-exporter.yaml",
    "environments/ci/threshold-exporter.yaml",
    "k8s/crd/examples/example-thresholdconfig.yaml",
]
_YAML_DIRS = [
    "components/threshold-exporter/config/conf.d",
    "try-local/seed/conf.d",
]
_DOC_DIRS = ["docs"]


def _iter_yaml_files(root: str):
    for rel in _YAML_GLOBS:
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            yield rel, p
    for d in _YAML_DIRS:
        base = os.path.join(root, d)
        for dirpath, _dirs, files in os.walk(base):
            for f in sorted(files):
                if f.endswith((".yaml", ".yml")):
                    p = os.path.join(dirpath, f)
                    yield os.path.relpath(p, root).replace(os.sep, "/"), p


def _iter_doc_files(root: str):
    for d in _DOC_DIRS:
        base = os.path.join(root, d)
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if x not in ("assets", "site")]
            for f in sorted(files):
                if f.endswith(".md"):
                    p = os.path.join(dirpath, f)
                    yield os.path.relpath(p, root).replace(os.sep, "/"), p


def _read(path: str) -> str:
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def _collect(root: str) -> list[tuple[str, str, float, bool]]:
    """Return [(surface, key, value, is_threshold)] over every scannable artifact.

    `is_threshold` is False for OBSERVED values (the recommendation file's
    percentiles): they share the key's unit, so the domain check applies, but
    they are not a configured threshold and must not feed tier ordering.
    """
    found: list[tuple[str, str, float, bool]] = []

    for rel, path in _iter_yaml_files(root):
        try:
            doc = yaml.safe_load(_read(path))
        except Exception:
            continue  # unparseable YAML is another gate's job
        for key, val in _walk_values(doc):
            found.append((rel, key, val, True))

    # docs/assets JSON: platform-data (value+unit), recommendation-data
    # (percentiles + recommended + unit), template-data (embedded tenant YAML).
    pd = os.path.join(root, "docs/assets/platform-data.json")
    if os.path.isfile(pd):
        for pack in (json.loads(_read(pd)).get("rulePacks") or {}).values():
            for key, meta in (pack.get("defaults") or {}).items():
                if key in _REGISTRY_KEYS and isinstance(meta, dict):
                    num = _as_number(meta.get("value"))
                    if num is not None:
                        found.append(("docs/assets/platform-data.json", key, num, True))

    rd = os.path.join(root, "docs/assets/recommendation-data.json")
    if os.path.isfile(rd):
        for key, meta in (json.loads(_read(rd)).get("recommendations") or {}).items():
            if key not in _REGISTRY_KEYS or not isinstance(meta, dict):
                continue
            for field in ("p50", "p95", "p99", "recommended"):
                num = _as_number(meta.get(field))
                if num is not None:
                    # Percentiles are OBSERVATIONS of the metric, not thresholds:
                    # they share the same unit (so the domain check applies) but a
                    # `_critical` key legitimately carries the same observed
                    # distribution as its base, so they must not feed tier ordering.
                    found.append((f"docs/assets/recommendation-data.json[{field}]",
                                  key, num, field == "recommended"))

    td = os.path.join(root, "docs/assets/template-data.json")
    if os.path.isfile(td):
        for tpl in json.loads(_read(td)).get("templates") or []:
            try:
                doc = yaml.safe_load(tpl.get("yaml") or "")
            except Exception:
                continue
            for key, val in _walk_values(doc):
                found.append((f"docs/assets/template-data.json[{tpl.get('id')}]", key, val, True))

    for rel, path in _iter_doc_files(root):
        for block in _MD_YAML_RE.findall(_read(path)):
            try:
                doc = yaml.safe_load(block)
            except Exception:
                continue  # doc fragments legitimately aren't standalone YAML
            for key, val in _walk_values(doc):
                found.append((rel, key, val, True))

    return found


def _declared_units(root: str) -> list[tuple[str, str, str]]:
    """Return [(surface, key, declared_unit)] for assets that carry a unit field."""
    out: list[tuple[str, str, str]] = []
    pd = os.path.join(root, "docs/assets/platform-data.json")
    if os.path.isfile(pd):
        for pack in (json.loads(_read(pd)).get("rulePacks") or {}).values():
            for key, meta in (pack.get("defaults") or {}).items():
                if key in _REGISTRY_KEYS and isinstance(meta, dict) and meta.get("unit"):
                    out.append(("docs/assets/platform-data.json", key, meta["unit"]))
    rd = os.path.join(root, "docs/assets/recommendation-data.json")
    if os.path.isfile(rd):
        for key, meta in (json.loads(_read(rd)).get("recommendations") or {}).items():
            if key in _REGISTRY_KEYS and isinstance(meta, dict) and meta.get("unit"):
                out.append(("docs/assets/recommendation-data.json", key, meta["unit"]))
    return out


# -------------------------------------------------------------------- checks


def run_check(
    registry: dict | None = None,
    root: str | None = None,
) -> dict[str, list[str]]:
    """Return {errors}. Hermetic tests inject a synthetic registry + root."""
    global _REGISTRY_KEYS

    root = root or PROJECT_ROOT
    doc = registry if registry is not None else registry_lib.load_registry()

    spec: dict[str, dict] = {}

    def _walk_registry(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, dict) and "value" in v and "tier" in v:
                    spec[k] = v
                else:
                    _walk_registry(v)

    _walk_registry(doc)
    _REGISTRY_KEYS = set(spec)

    errors: list[str] = []

    # 0. Every registry unit must be classified — an unknown unit silently
    #    removes a whole pack from this gate's coverage.
    for key, meta in sorted(spec.items()):
        unit = meta.get("unit")
        if unit not in UNIT_DIMENSION:
            errors.append(
                f"UNCLASSIFIED-UNIT: {key!r} declares unit {unit!r}, which is not in "
                "UNIT_DIMENSION. Classify it in check_threshold_unit_sanity.py — an "
                "unclassified unit would silently drop this key from the gate."
            )
    if errors:
        return {"errors": errors}  # classification is a precondition for the rest

    # 1. Declared-unit agreement.
    for surface, key, declared in _declared_units(root):
        expected = spec[key]["unit"]
        if declared != expected:
            errors.append(
                f"UNIT-DRIFT: {surface} declares {key!r} as {declared!r}, but "
                f"rule-packs/threshold-registry.yaml declares {expected!r}. The asset "
                "is teaching a different quantity than the platform measures."
            )

    # 2. Value-in-domain.
    per_surface: dict[str, dict[str, float]] = {}
    for surface, key, value, is_threshold in _collect(root):
        meta = spec[key]
        unit = meta.get("unit")
        dimension = UNIT_DIMENSION[unit]
        msg = _domain_error(dimension, unit, value)
        if msg:
            errors.append(f"OUT-OF-DOMAIN: {surface}: {key} = {value:g} — {msg}")
        if is_threshold:
            per_surface.setdefault(surface, {})[key] = value

    # 3. Tier ordering. The registry knows which key is the critical tier of
    #    which (`critical_of`), so an inverted pair is mechanically detectable.
    #    It matters because both tiers are live at once: with critical <=
    #    warning the critical arm fires first or together, and ADR-001's
    #    severity-dedup (shared `metric_group`) then suppresses the warning
    #    permanently — the warning tier becomes dead config that still reads
    #    like protection.
    for surface, values in sorted(per_surface.items()):
        for key, value in sorted(values.items()):
            base = spec[key].get("critical_of")
            if not base or base not in values:
                continue
            if value <= values[base]:
                errors.append(
                    f"TIER-INVERSION: {surface}: {key} = {value:g} is not above its "
                    f"warning tier {base} = {values[base]:g} — the critical arm fires "
                    "first or together, and severity-dedup then suppresses the "
                    "warning permanently."
                )

    return {"errors": sorted(set(errors))}


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "閾值 unit 合理性 gate：以 threshold-registry 的 unit 為 SoT，驗宣告一致與值域"
            "（#992 承諾但未建的 value-range validation）",
            "Threshold unit-sanity gate: registry `unit` as SoT — declared-unit "
            "agreement + value-in-domain (the value-range validation #992 promised)"))
    parser.add_argument(
        "--ci", action="store_true",
        help=i18n_text("任何 unit 漂移／值域違規即 exit 1",
                       "exit 1 on any unit drift or out-of-domain value"))
    parser.add_argument(
        "--json", action="store_true",
        help=i18n_text("以 JSON 輸出（stdout 恰好一份文件）",
                       "emit one JSON document on stdout"))
    args = parser.parse_args(argv)

    try:
        result = run_check()
    except Exception as exc:  # noqa: BLE001 — caller error, not a violation
        print(f"ERROR: unit-sanity check crashed: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    errors = result["errors"]

    if args.json:
        print(json.dumps({"status": "violation" if errors else "ok",
                          "errors": errors}, ensure_ascii=False, indent=2))
    else:
        for msg in errors:
            print(f"❌ {msg}", file=sys.stderr)
        if errors:
            print(
                f"\n{len(errors)} threshold unit-sanity violation(s).\n"
                "Fix the asset, or — if the registry is the one that is wrong — fix "
                "rule-packs/threshold-registry.yaml (via scaffold_tenant.RULE_PACKS "
                "+ `check_threshold_registry.py --regen`).\n"
                "See scripts/tools/lint/check_threshold_unit_sanity.py.",
                file=sys.stderr)
        else:
            print("✅ threshold unit sanity OK — declared units agree with the "
                  "registry and every value lies in its dimension.", file=sys.stderr)

    if errors:
        return EXIT_VIOLATION if args.ci else EXIT_OK
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
