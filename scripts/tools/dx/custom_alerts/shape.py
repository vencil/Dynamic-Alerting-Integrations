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
             [__q{quantile}][__den_{denominator_metric}]__for{for}
  selector part (exact):  s_{key}_{value}
  selector part (regex):  sre_{key}_{value}
  op_slug: >→gt  >=→ge  <→lt  <=→le  ==→eq  (absence → "absent")
  for: pending-duration; part of rule identity (control-plane static attr,
       TRK-326) — always emitted, enum-bounded in schema. Default "1m".
  group_by part:  gb_{label}  (sorted; ADR-024 §Addendum disk recipes) — a
       preserved aggregation dimension so the alert fires per that dimension
       (e.g. per PVC). Emitted ONLY when present, so a recipe without group_by
       keeps a byte-identical slug. Bounded to ALLOWED_GROUP_BY.
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

OP_SLUG = {">": "gt", ">=": "ge", "<": "lt", "<=": "le", "==": "eq"}

# `==` is threshold-recipe-only (#810): exact match is meaningful for integer
# status/error codes read off a RAW gauge, but float-fragile (and semantically
# hollow) against COMPUTED values — rate()/ratio/histogram_quantile/predict_linear
# all emit floats where equality is an accident of arithmetic. MUST match the
# Go gate in custom_alert.go::RecipeID.
_EQ_RECIPES = frozenset({"threshold"})

# Permitted `for` pending-durations (enum-bounded, TRK-326). `for` is part of the
# recipe_id slug + shape_signature, so bounding it keeps per-base-shape cardinality
# a small constant (no O(M)→O(N)). MUST match docs/schemas/tenant-config.schema.json
# and custom_alert.go::customAlertForValid.
ALLOWED_FOR = frozenset({"0s", "1m", "5m", "15m", "30m", "1h"})

# Permitted forecast horizons (the predict-ahead distance, ADR-024 §Forecast
# Recipe). Enum-bounded for the same cardinality reason as `for`: `horizon` enters
# the recipe_id slug. The platform derives lookback = max(2·horizon, 1h) from this
# (compile-time only — see recipes.py). MUST match the schema + Go customAlertHorizonValid.
ALLOWED_HORIZON = frozenset({"1h", "2h", "4h", "12h", "24h", "48h"})

# Permitted `group_by` dimensions (ADR-024 §Addendum disk recipes). A disk-fill
# alert must fire PER PVC — a 99%-full 10GB volume must not be hidden by a 10%-full
# 500GB one in a `by(tenant)` sum. group_by preserves the named label in the
# metric-side aggregation so each dimension is evaluated separately. Bounded to a
# whitelist to keep cardinality safe (a tenant has few PVCs). Each entry enters the
# recipe_id slug + shape_signature. MUST match the schema enum + custom_alert.go.
ALLOWED_GROUP_BY = frozenset({"persistentvolumeclaim"})

RECIPES = ("threshold", "rate", "ratio", "absence", "p99_latency", "forecast")

# Recipe lifecycle status (ADR-024 §Custom Alerts cost/governance, #741 item #6).
# A recipe is platform-authored; its status governs whether tenants may keep
# declaring it. This is RECIPE versioning — distinct from capability-A APP
# versioning (the `version` label).
#   active     — normal, no restriction.
#   deprecated — still compiles; the compiler emits a non-fatal notice and the
#                portal shows a warning. Signals "migrate away, still works".
#   eol        — existing declarations KEEP compiling (no silent alert loss), but
#                tenant-api preflight rejects any PUT that uses the recipe (forces
#                migration on next edit). SRE clears by flipping status back.
# RECIPE_STATUS is the executable SSOT; the human governance contracts
# rule-packs/recipes/*.yaml mirror a `status:` field (drift-guarded by
# tests/dx/test_recipe_lifecycle.py). Default every shipped recipe to active.
RECIPE_LIFECYCLE = frozenset({"active", "deprecated", "eol"})
RECIPE_STATUS = {r: "active" for r in RECIPES}


def recipe_status(recipe: str) -> str:
    """Lifecycle status of a recipe (one of RECIPE_LIFECYCLE).

    Never raises: an unknown recipe reports "active" (recipe_id() is the
    authority that rejects unknown recipes, so callers can query freely
    without double-validating).
    """
    return RECIPE_STATUS.get(recipe, "active")


class RecipeError(ValueError):
    """A recipe instance is structurally invalid (rejected at compile time)."""


def _sanitise(s: str) -> str:
    """Map every char outside [a-zA-Z0-9_] to '_'. Deterministic, locale-free."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", s)


def _normalize_for(inst: dict) -> str:
    """Validate + normalize `for` into its canonical slug form (TRK-326).

    `for` enters BOTH recipe_id and shape_signature (the rule identity), so a
    bad value must fail loud here, not silently mint a bogus shape (e.g. `None`
    → "forNone", or a non-enum duration that splits the vectorized rule). Falsy
    (missing / null / empty) → default "1m" — matching custom_alert.go's
    `if forVal == "" { forVal = "1m" }` so the two implementations never diverge
    on the falsy case. Any other value MUST be one of ALLOWED_FOR.
    """
    value = inst.get("for", "1m")
    value = "1m" if value in (None, "") else str(value)
    if value not in ALLOWED_FOR:
        raise RecipeError(
            f"for {value!r} must be one of {sorted(ALLOWED_FOR)} (TRK-326 enum-bounded)"
        )
    return value


def _normalize_horizon(inst: dict) -> str:
    """Validate the forecast `horizon` (predict-ahead distance). REQUIRED for the
    forecast recipe (no default — the tenant must state how far ahead to predict);
    enum-bounded so it can safely enter the recipe_id slug. MUST match the schema
    enum + custom_alert.go::customAlertHorizonValid."""
    value = inst.get("horizon")
    if value in (None, ""):
        raise RecipeError(
            f"forecast recipe requires `horizon` (one of {sorted(ALLOWED_HORIZON)})"
        )
    value = str(value)
    if value not in ALLOWED_HORIZON:
        raise RecipeError(
            f"horizon {value!r} must be one of {sorted(ALLOWED_HORIZON)}"
        )
    return value


def _normalize_group_by(inst: dict) -> Tuple[str, ...]:
    """Validate + canonicalise `group_by` into a sorted, deduped tuple.

    Each entry is a label preserved in the metric-side aggregation so the alert is
    evaluated per that dimension (e.g. per PVC), firing if ANY one crosses. Falsy
    (missing / null / empty) → empty tuple, so a recipe without group_by keeps a
    byte-identical recipe_id (the existing golden vectors stay valid). Bounded to
    ALLOWED_GROUP_BY; sorted for cross-language slug determinism. MUST match
    custom_alert.go::customAlertGroupBy.
    """
    raw = inst.get("group_by")
    if raw in (None, ""):
        return ()
    if not isinstance(raw, (list, tuple)):
        raise RecipeError(
            f"group_by must be a list of labels, got {type(raw).__name__}"
        )
    out = []
    for label in raw:
        label = str(label)
        if label not in ALLOWED_GROUP_BY:
            raise RecipeError(
                f"group_by label {label!r} must be one of {sorted(ALLOWED_GROUP_BY)} "
                f"(bounded whitelist, ADR-024 §Addendum)"
            )
        out.append(label)
    return tuple(sorted(set(out)))


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

    # `==` gate runs BEFORE the absence short-circuit so it also rejects
    # absence + op:"==" (op is meaningless for a presence check). Keeping the
    # gate strict here matches the JSON-schema if/then editor-guard — otherwise
    # an API/GitOps-submitted absence+"==" the imperative gate silently accepted
    # would later fail to render in the Portal form (front/back brain-split).
    op = inst.get("op", ">")
    if op == "==" and recipe not in _EQ_RECIPES:
        raise RecipeError(
            f"op '==' is only allowed for the threshold recipe (exact match on a "
            f"raw-gauge status/error code, #810); {recipe!r} does not support it"
        )
    if recipe == "absence":
        parts.append("absent")
    else:
        if op not in OP_SLUG:
            raise RecipeError(f"unknown op {op!r} (known: {list(OP_SLUG)})")
        parts.append(OP_SLUG[op])

    if recipe == "forecast":
        # forecast derives its lookback from `horizon` (recipes.py:
        # max(2·horizon, 1h)), so the tenant supplies `horizon`, NOT `window` —
        # horizon is the shaping param that enters the slug (`h{horizon}` takes
        # the `w{window}` slot). capacity_metric present → ratio mode (predict a
        # ratio crossing a floor) and reuses the `den_` slug slot; absent → raw
        # mode (predict a gauge crossing an absolute threshold).
        parts.append("h" + _normalize_horizon(inst))
        cap = inst.get("capacity_metric")
        if cap:
            validate_metric_name(cap, "capacity_metric")
            parts.append("den_" + cap)
    else:
        parts.append("w" + str(inst.get("window", "")))
        if recipe == "p99_latency":
            parts.append("q" + str(inst.get("quantile", "0.99")))
        if recipe == "ratio":
            den = inst["denominator_metric"]
            validate_metric_name(den, "denominator_metric")
            parts.append("den_" + den)

    # `for` is part of the rule identity, NOT just a per-instance attribute:
    # Prometheus `for:` is a control-plane STATIC rule attribute (unlike `mode`,
    # which rides the data plane via group_left). Two tenants sharing every other
    # param but a different `for` are genuinely DIFFERENT alert rules — so `for`
    # must enter the slug, else the vectorized rule freezes to one tenant's `for`
    # and silently drops the others (TRK-326 / #751). Always emitted (grammar-
    # consistent with op/window/quantile); enum-bounded in the schema so the
    # cardinality stays a small constant per base-shape (no O(M)→O(N) blow-up).
    # MUST stay byte-identical to custom_alert.go::RecipeID.
    parts.append("for" + _normalize_for(inst))

    # group_by dimensions (ADR-024 §Addendum): each preserved label is a distinct
    # rule from the per-tenant default, so it enters the slug. Appended LAST and
    # only when present → a recipe without group_by keeps a byte-identical slug
    # (existing recipe_id vectors unaffected).
    #   SLUG-ORDER CONTRACT: a NEW slug field added later MUST go in the SAME
    #   position in custom_alert.go::RecipeID (the golden vector enforces parity).
    #   Keep new fields only-when-present like gb_ (an ALWAYS-appended field — like
    #   `for` — re-slugs every existing rule, a deliberate breaking migration).
    # Per-dimension eval only applies to value-crossing recipes, so reject it for
    # absence (a per-tenant presence check) and op '==' (exact code match is not
    # per-PVC) — keeps the eq/absence cores per-tenant.
    #   FORESIGHT: the '==' rejection is safe ONLY because the whitelist is PVC-only
    #   (error codes aren't per-PVC). If a topology dim (e.g. pod) is whitelisted, a
    #   tenant may legitimately want group_by:[pod] + op:'==' ("any pod's errno == X")
    #   — then relax this rejection AND thread group_by into _eq_core_record's
    #   `max by(...)`. MUST match custom_alert.go.
    group_by = _normalize_group_by(inst)
    if group_by and (recipe == "absence" or op == "=="):
        what = "the absence recipe" if recipe == "absence" else "op '=='"
        raise RecipeError(
            f"group_by (per-dimension eval) is not supported for {what} — it "
            f"applies only to value-crossing recipes (ADR-024 §Addendum)"
        )
    for gb in group_by:
        parts.append("gb_" + gb)

    return _sanitise("__".join(parts))


def shape_signature(inst: dict) -> Tuple:
    """Hashable identity used to dedup instances into one rule per shape.

    Two instances with the same signature compile to ONE vectorised rule
    covering all their tenants (O(M)); a difference in any PromQL-shaping
    param yields a distinct rule. NOT keyed by tenant or `name` (those ride
    the data plane) — see ADR-024 §2b.
    """
    is_forecast = inst["recipe"] == "forecast"
    return (
        inst["recipe"],
        inst["metric"],
        # den slot: ratio's denominator_metric OR forecast's capacity_metric.
        inst.get("capacity_metric") if is_forecast else inst.get("denominator_metric"),
        None if inst["recipe"] == "absence" else inst.get("op", ">"),
        # forecast has no window (lookback derives from horizon); others do.
        None if is_forecast else str(inst.get("window", "")),
        str(inst.get("quantile", "0.99")) if inst["recipe"] == "p99_latency" else None,
        # forecast's horizon is a shaping param (predict-ahead distance).
        _normalize_horizon(inst) if is_forecast else None,
        tuple(_selector_items(inst)),
        # `for` distinguishes shapes (control-plane static attr; see recipe_id).
        _normalize_for(inst),
        # group_by dimensions distinguish per-dimension shapes (ADR-024 §Addendum).
        _normalize_group_by(inst),
    )


# forecast ratio-mode current-state band (RATIO MODE ONLY). recipes._forecast_records
# gates a ratio-mode forecast's predicted value on `custom:fcbase < this` — a sanity
# floor that suppresses transient-write-burst / cold-start false positives at high
# headroom (a pure-slope alarm would otherwise page an 80%-empty disk). It lives HERE
# (not in recipes.py) so the emitter AND the write/compile-time validator share ONE
# source of truth; the Go preflight hardcodes the same value (custom_alert.go
# `forecastCurrentBand`) and MUST stay in lockstep.
_FORECAST_CURRENT_BAND = 0.5


def validate_forecast_ratio_threshold(inst: dict, value: str) -> None:
    """Reject a ratio-mode forecast whose threshold floor is not below the band.

    A ratio-mode forecast (capacity_metric set) fires when predicted headroom < the
    tenant threshold AND current headroom < _FORECAST_CURRENT_BAND. If the threshold
    is >= the band, the band silently swallows the lead time between them (the alert
    can only fire once current headroom is already below the band). Enforce the
    constraint LOUDLY here (compile time) — mirrored by the Go preflight (write time).
    Non-forecast / raw-mode recipes are unaffected; a non-numeric value falls through
    (its validity is parse_threshold's / the Go preflight's concern)."""
    if inst.get("recipe") != "forecast" or not inst.get("capacity_metric"):
        return
    try:
        v = float(value)
    except (TypeError, ValueError):
        return
    if v <= 0 or v >= _FORECAST_CURRENT_BAND:
        raise RecipeError(
            f"ratio-mode forecast threshold {v} must be in (0, {_FORECAST_CURRENT_BAND}) "
            f"— the current-state band: a floor >= the band is silently neutered (the "
            f"alert can only fire once current headroom drops below the band)"
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
        "forecast": "predict (linear) a gauge/ratio crossing a threshold within a horizon",
    }
