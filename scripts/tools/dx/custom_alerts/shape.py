"""Shape signature + recipe_id slug + validation + safe selector assembly.

ADR-024 Capability B (#741). The recipe_id is the identity of a generated rule:
it is the dedup key, the recording-rule name component, the alertname suffix, AND
a label on the data-plane `user_threshold` series. Because the same recipe_id is
computed independently by this Python compiler AND (in S3) by the Go exporter,
**the slug algorithm is a cross-language contract** — it must be pure string
assembly with no hashing, no locale, no map-ordering dependence. A drift between
the two implementations silently breaks every `on(tenant) group_left` join.

The contract is pinned by tests/dx/fixtures/recipe_id_vectors.json (a shared
golden vector both implementations assert against).

recipe_id grammar (parts joined by `__`, each part sanitised to [a-z0-9_]):
    {recipe}__{metric}[__{sorted selector parts}]__{op_slug}__w{window}
             [__q{quantile}][__den_{denominator_metric}]
  selector part (exact):  s_{key}_{value}
  selector part (regex):  sre_{key}_{value}
  op_slug: >→gt  >=→ge  <→lt  <=→le  (absence → "absent")
Sanitisation maps every char outside [a-zA-Z0-9_] to '_' (deterministic, and keeps
the slug valid as a Prometheus recording-rule name component).
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

# --- charset contracts ------------------------------------------------------
# Metric / label names: strict Prometheus identifier WITHOUT colon. Forbidding
# the colon stops a tenant from referencing a recording rule (e.g.
# `tenant_version:alert_threshold:...`) and is the first line of the
# PromQL-injection defence (ADR-024 §2d). The second line is _assemble_selector,
# which never interpolates a raw value outside a quoted+escaped string literal.
_METRIC_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Tenants may NOT set these as selectors: they are the vectorisation join keys /
# platform-owned dimensions. Letting a tenant pin `tenant=` or `version=` would
# hijack the cross-tenant isolation or poison the graceful version-join.
RESERVED_LABELS = frozenset(
    {"tenant", "version", "__name__", "severity", "recipe", "recipe_id", "name"}
)

OP_SLUG = {">": "gt", ">=": "ge", "<": "lt", "<=": "le"}

RECIPES = ("threshold", "rate", "ratio", "absence", "p99_latency")


class RecipeError(ValueError):
    """A recipe instance is structurally invalid (rejected at compile time)."""


def _sanitise(s: str) -> str:
    """Map every char outside [a-zA-Z0-9_] to '_'. Deterministic, locale-free."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", s)


def validate_metric_name(metric: str, field: str = "metric") -> None:
    if not isinstance(metric, str) or not _METRIC_RE.fullmatch(metric):
        raise RecipeError(
            f"{field} {metric!r} is not a bare Prometheus metric name "
            f"(^[a-zA-Z_][a-zA-Z0-9_]*$, no colon/braces/operators) — "
            f"label filtering must use the `selectors`/`selectors_re` map "
            f"(ADR-024 §2e), never inline PromQL"
        )


def _escape_value(value: str) -> str:
    r"""Escape a selector value for a Prometheus double-quoted string literal.

    Order matters: backslash first, then the double quote. Newlines are escaped
    too so a value can never break out of the quoted literal. promtool is the
    backstop, but this makes injection structurally impossible at emit time.
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _selector_items(inst: dict) -> List[Tuple[str, str, str]]:
    """Return sorted (op, key, value) triples for the instance's selectors.

    op is '=' (from `selectors`) or '=~' (from `selectors_re`). Sorted by
    (key, op) so the assembled selector + recipe_id are order-independent of
    YAML map iteration (cross-language determinism).
    """
    items: List[Tuple[str, str, str]] = []
    for key, value in (inst.get("selectors") or {}).items():
        items.append(("=", str(key), value))
    for key, value in (inst.get("selectors_re") or {}).items():
        items.append(("=~", str(key), value))
    for _op, key, _value in items:
        if not _LABEL_RE.fullmatch(key):
            raise RecipeError(f"selector label {key!r} is not a valid label name")
        if key in RESERVED_LABELS:
            raise RecipeError(
                f"selector label {key!r} is reserved (one of {sorted(RESERVED_LABELS)}) "
                f"— a tenant may not pin a vectorisation/platform label"
            )
    items.sort(key=lambda t: (t[1], t[0]))
    return items


def assemble_selector(inst: dict) -> str:
    """Build the safe `{k="v", k2=~"v2"}` PromQL selector (empty str if none)."""
    items = _selector_items(inst)
    if not items:
        return ""
    parts = [f'{key}{op}"{_escape_value(value)}"' for op, key, value in items]
    return "{" + ", ".join(parts) + "}"


def recipe_id(inst: dict) -> str:
    """Compute the deterministic shape slug (cross-language contract)."""
    recipe = inst["recipe"]
    if recipe not in RECIPES:
        raise RecipeError(f"unknown recipe {recipe!r} (known: {list(RECIPES)})")
    metric = inst["metric"]
    validate_metric_name(metric, "metric")

    parts: List[str] = [recipe, metric]

    for op, key, value in _selector_items(inst):
        prefix = "sre" if op == "=~" else "s"
        parts.append(f"{prefix}_{key}_{value}")

    if recipe == "absence":
        parts.append("absent")
    else:
        op = inst.get("op", ">")
        if op not in OP_SLUG:
            raise RecipeError(f"unknown op {op!r} (known: {list(OP_SLUG)})")
        parts.append(OP_SLUG[op])

    parts.append("w" + str(inst.get("window", "")))

    if recipe == "p99_latency":
        parts.append("q" + str(inst.get("quantile", "0.99")))
    if recipe == "ratio":
        den = inst["denominator_metric"]
        validate_metric_name(den, "denominator_metric")
        parts.append("den_" + den)

    return _sanitise("__".join(parts))


def shape_signature(inst: dict) -> Tuple:
    """Hashable identity used to dedup instances into one rule per shape.

    Two instances with the same signature compile to ONE vectorised rule
    covering all their tenants (O(M)); a difference in any PromQL-shaping
    param yields a distinct rule. NOT keyed by tenant or `name` (those ride
    the data plane) — see ADR-024 §2b.
    """
    return (
        inst["recipe"],
        inst["metric"],
        inst.get("denominator_metric"),
        None if inst["recipe"] == "absence" else inst.get("op", ">"),
        str(inst.get("window", "")),
        str(inst.get("quantile", "0.99")) if inst["recipe"] == "p99_latency" else None,
        tuple(_selector_items(inst)),
    )


# severity is parsed from the threshold's "value:severity" tail (reuses the
# existing thresholdScalar convention). Default severity when omitted: warning.
def parse_threshold(threshold: str) -> Tuple[str, str]:
    """Split a `value[:severity]` threshold into (value, severity).

    Mirrors resolve.go's value:severity convention. Returns (value, severity);
    severity defaults to 'warning'. `value` is returned verbatim for absence
    (where it is a presence flag, not a numeric comparand).
    """
    raw = str(threshold).strip()
    if ":" in raw:
        value, sev = raw.rsplit(":", 1)
        value, sev = value.strip(), sev.strip().lower()
    else:
        value, sev = raw, "warning"
    if sev not in ("warning", "critical"):
        raise RecipeError(
            f"threshold severity {sev!r} must be 'warning' or 'critical' "
            f"(custom alerts are per-severity, ADR-024 §2c)"
        )
    return value, sev


def known_recipes() -> Dict[str, str]:
    """recipe → one-line description (kept in sync with rule-packs/recipes/)."""
    return {
        "threshold": "gauge value crosses a threshold",
        "rate": "per-second rate of a counter crosses a threshold",
        "ratio": "ratio of two counter rates crosses a threshold (div-by-zero safe)",
        "absence": "a metric is absent over a window (per-tenant, self-scoped)",
        "p99_latency": "histogram p-quantile latency crosses a threshold",
    }
