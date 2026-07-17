"""Shape signature + recipe_id slug + validation + safe selector assembly.

ADR-024 Capability B (#741). The recipe_id is the identity of a generated rule:
it is the dedup key, the recording-rule name component, the alertname suffix, AND
a label on the data-plane `user_threshold` series. Because the same recipe_id is
computed independently by this Python compiler AND (in S3) by the Go exporter,
**the slug algorithm is a cross-language contract** — it must be deterministic,
locale-free, and map-order-independent. A drift silently breaks every
`on(tenant) group_left` join.

The readable slug is a pure string assembly, but it is NOT injective over the shape
identity: _sanitise is lossy, and the `s_{key}_{value}`/`__`-join is ambiguous even
with no lossy char ({region_x:1} and {region:x_1} both → `s_region_x_1`) — #1008 / F3.
So a recipe with >=1 selector (the only tenant-controlled free-form slug field) carries
a trailing `__x{16-hex}` = SHA-256 over a length-prefixed canonical of the STRUCTURED
shape identity (see _shape_hash / _needs_shape_hash). window/quantile/metric/for/group_by
are charset- or enum-bounded, so they cannot alias.

CONTRACT INVARIANT (load-bearing): the schema `type: string` on every slug-bearing
free-form field is what guarantees Go and Python read the SAME value text. Since #1017
`quantile` is string-only too (was `type: ["string","number"]`). NB the bare-number
hazard is subtler than "Go keeps raw text, Python str()s a float": the exporter's
production path RE-CANONICALISES a bare scalar (parse.go ScheduledValue passthrough,
Decode→Marshal) before flexStr ever sees it, so plain decimals like `0.990` converge
to "0.99" on BOTH sides. The real divergence class is YAML-dialect disagreement —
PyYAML (YAML 1.1) reads a dotless exponent like `95e-2` as a STRING while yaml.v3
reads a FLOAT (Go q0_95 vs Python q95e_2 → silent join loss) — and even the
convergent cases rest on a double coincidence (Go shortest-repr == CPython repr;
the two resolvers agreeing). `type: string` removes the whole class structurally:
both languages read identical authored text, no resolver in the loop.

The contract is pinned by tests/dx/fixtures/recipe_id_vectors.json (a shared
golden vector both implementations assert against).

recipe_id grammar (parts joined by `__`, each part sanitised to [a-z0-9_]):
    {recipe}__{metric}[__{sorted selector parts}]__{op_slug}__w{window}
             [__q{quantile}][__den_{denominator_metric}]__for{for}
  slo_burn_rate (ADR-031) has NO window slot (the burn windows 1h/5m/6h/30m are
  recipe semantics, not tenant params) and a fixed op (gt); its den slot is
  followed by a `minev{N}` slot (min_events, ALWAYS emitted — default 10 is
  materialised into the slug; N is a charset-bounded positive integer, so it
  needs no _shape_hash field):
    slo_burn_rate__{metric}[__{sorted selector parts}]__gt
             __den_{denominator_metric}__minev{min_events}__for{for}
  (`objective` and `slo_period` are NOT shape components: they ride the data
  plane / only scale the exporter-computed threshold VALUE — never in the slug,
  the hash, or the rule text. Changing them never re-slugs.)
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
the slug valid as a Prometheus recording-rule name component). A selector-bearing slug
additionally carries a trailing `__x{16-hex}` suffix (#1008 / F3) for injectivity.
"""
from __future__ import annotations

import hashlib
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

# `window` (and, for p99_latency, `quantile`) are BARE PromQL tokens interpolated
# raw into the compiled rule (rate(m[<window>]) / histogram_quantile(<quantile>,…)).
# Unlike a selector VALUE they cannot be quote-escaped — a bare token IS syntax — so
# they must be allowlisted. `window` is the Go-duration charset (the schema pattern,
# now enforced imperatively both sides). MUST match custom_alert.go::customAlertWindowRe.
_WINDOW_RE = re.compile(r"^([0-9]+(ns|us|µs|ms|s|m|h))+$")

# quantile: DECIMAL-float charset only. Go strconv.ParseFloat accepts Go hex-float
# literals ("0x1p-1"=0.5) and underscores that CPython float() rejects — a SHARED
# regex pins the accept-set so the Go preflight and the Python compiler can never
# diverge (a divergence lands a poison commit that wedges the CI drift gate; Phase C
# hunter finding). MUST match custom_alert.go::customAlertQuantileRe.
_QUANTILE_RE = re.compile(r"^[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?$")

# Go-template metacharacters forbidden in a tenant-controlled selector VALUE. The
# value is safe in the PromQL string-literal (escaped by _escape_value), but it is
# ALSO interpolated into the alert-annotation `description` (recipes.py _alert_rule),
# which Prometheus renders as a Go text/template at fire time — where `{{ query … }}`
# runs arbitrary cross-tenant PromQL. Per the SSTI logic-less principle, untrusted
# data must never be able to BECOME template code. A denylist by necessity (a label
# value is arbitrary UTF-8, not allowlistable like an identifier); the emit-time
# invariant gate (check_custom_alert_annotations) is the backstop. Backtick is the
# Go raw-string delimiter that survives _escape_value. MUST match
# custom_alert.go::customAlertTemplateMetachars.
_TEMPLATE_METACHARS = ("{{", "}}", "`")

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

# Permitted `slo_period` budget windows (ADR-031). NOT a shape component: it
# only scales the burn-rate multipliers the Go exporter derives at resolve time
# (30d→14.4/6, 28d→13.44/5.6) — never enters the slug/hash/rule text, so
# changing the period never re-slugs or forks the rule. Python validates the
# enum per the ADR-029 dual-side validation duty. MUST match the schema enum +
# custom_alert.go (Wave 2).
ALLOWED_SLO_PERIOD = frozenset({"28d", "30d"})

# slo_burn_rate min_events default (ADR-031 OQ-A → 10): the fast-window (5m)
# bad-event absolute floor. The slow tier's floor is compiler-derived as
# min_events×6 (30m = 5m×6) — the linear window scaling lives HERE, not in Go.
SLO_MIN_EVENTS_DEFAULT = 10

# slo_burn_rate burn windows per severity (ADR-031 §2): (long, short, bad-floor
# multiplier). FIXED recipe semantics — severity is decided by the recipe
# (fast→critical, slow→warning), a deliberate departure from the threshold
# value:severity tail, stated in rule-packs/recipes/slo_burn_rate.yaml.
SLO_BURN_WINDOWS = {
    "critical": ("1h", "5m", 1),    # fast burn: 1h & 5m ratio + bad:5m > minev
    "warning": ("6h", "30m", 6),    # slow burn: 6h & 30m ratio + bad:30m > minev*6
}

# Permitted `group_by` dimensions (ADR-024 §Addendum disk recipes). A disk-fill
# alert must fire PER PVC — a 99%-full 10GB volume must not be hidden by a 10%-full
# 500GB one in a `by(tenant)` sum. group_by preserves the named label in the
# metric-side aggregation so each dimension is evaluated separately. Bounded to a
# whitelist to keep cardinality safe (a tenant has few PVCs). Each entry enters the
# recipe_id slug + shape_signature. MUST match the schema enum + custom_alert.go.
ALLOWED_GROUP_BY = frozenset({"persistentvolumeclaim"})

RECIPES = ("threshold", "rate", "ratio", "absence", "p99_latency", "forecast",
           "slo_burn_rate")

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


def _lp(s: str) -> bytes:
    r"""Length-prefixed UTF-8 encoding of one field: b"<byte-len>:<utf-8>". Concatenating
    length-prefixed fields is injective (NIST SP 800-185 TupleHash style): no two distinct
    field sequences share an encoding, which closes the delimiter-aliasing collision class
    (a selector key/value straddling the `_` separator — #1008 / F3). MUST stay
    byte-identical to custom_alert.go::caHashField."""
    b = s.encode("utf-8")
    return str(len(b)).encode("ascii") + b":" + b


def _shape_hash(inst: dict, nhex: int = 16) -> str:
    """Disambiguation suffix for a selector-bearing recipe_id (#1008 / F3): the first
    `nhex` hex (64-bit) of SHA-256 over a length-prefixed canonical of the STRUCTURED
    shape identity. Fields are emitted in a FIXED order, each length-prefixed, with an
    explicit "" for a field not applicable to the recipe (no None → Go reproduces it
    trivially). 64-bit puts an adversarial suffix-forcing search at a 2^64 birthday bound;
    the loader.py collision guard is the last-resort backstop for the residual. MUST be
    byte-identical to custom_alert.go::shapeHashSuffix (golden-vector pinned)."""
    recipe = inst["recipe"]
    is_forecast = recipe == "forecast"
    den = (inst.get("capacity_metric") if is_forecast else inst.get("denominator_metric")) or ""
    fields = [
        recipe,
        str(inst["metric"]),
        "" if recipe == "absence" else str(inst.get("op", ">")),
        # slo_burn_rate has NO window slot (fixed burn windows are recipe
        # semantics; a stray authored `window` is ignored — like forecast — so
        # it must not fork the hash). Existing recipes' field list/order is
        # UNCHANGED. min_events needs no field here: it is charset-bounded and
        # always visible in the readable slug (`minev{N}`), so it cannot alias.
        "" if recipe in ("forecast", "slo_burn_rate") else str(inst.get("window", "")),
        str(inst.get("quantile", "0.99")) if recipe == "p99_latency" else "",
        _normalize_horizon(inst) if is_forecast else "",
        str(den),
        _normalize_for(inst),
    ]
    buf = bytearray()
    for f in fields:
        buf += _lp(f)
    items = _selector_items(inst)
    buf += _lp("sel")
    buf += _lp(str(len(items)))
    for op, key, value in items:
        buf += _lp(op)
        buf += _lp(key)
        buf += _lp(str(value))
    gb = _normalize_group_by(inst)
    buf += _lp("gb")
    buf += _lp(str(len(gb)))
    for g in gb:
        buf += _lp(g)
    return hashlib.sha256(bytes(buf)).hexdigest()[:nhex]


def _needs_shape_hash(inst: dict) -> bool:
    """True when recipe_id must carry the injective suffix (#1008 / F3): iff the recipe has
    >=1 selector — the `s_{key}_{value}` join is ambiguous even with no lossy char (S1/S2),
    and selector values are the only tenant-controlled free-form slug field. All other slug
    fields (window/quantile/metric/for/group_by) are charset- or enum-bounded after
    validation, so they cannot alias. A no-selector recipe keeps a byte-identical slug
    (zero data-plane migration). MUST match custom_alert.go::RecipeID's inline condition."""
    return bool(inst.get("selectors") or inst.get("selectors_re"))


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


def _validate_objective(inst: dict) -> str:
    """Validate the slo_burn_rate `objective` (ADR-031). REQUIRED — the SLO target
    percentage in the OPEN interval (0,100): =100 → threshold 0 → always-fire, =0
    → never-fire, both rejected loudly. "disable" is the existing tri-state
    opt-out (the declaration still compiles; the exporter simply never emits the
    user_threshold series). NOT a shape component (never enters slug/hash/rule
    text — the exporter derives the burn thresholds from it at resolve time), but
    validated on BOTH sides per ADR-029. Reuses the decimal-only charset shared
    with quantile so the Go ParseFloat accept-set can never diverge (hex-float /
    underscore poison-commit class). MUST match custom_alert.go (Wave 2)."""
    value = inst.get("objective")
    if value in (None, ""):
        raise RecipeError(
            'slo_burn_rate recipe requires `objective` (SLO target percentage in '
            'the open interval (0,100), e.g. "99.9"; "disable" opts out tri-state)'
        )
    if not isinstance(value, str):
        # STRING-ONLY, enforced by TYPE (never str()-coerced): a bare YAML/JSON
        # number has already lost its authored text on this side of the
        # cross-language contract, and str(99.9) would sail through the charset
        # gate below while the Go side sees dialect-dependent text (the #1017
        # quantile class). Mirrors custom_alert.go::validateSloObjective's tag check.
        raise RecipeError(
            f'objective {value!r} must be a quoted YAML string (e.g. "99.9") — a bare '
            f"value is YAML-dialect-ambiguous and breaks the Go/Python lockstep "
            f"(#1017 quantile class)"
        )
    v = value
    if v == "disable":
        return v
    if not _QUANTILE_RE.fullmatch(v):
        raise RecipeError(
            f'objective {v!r} must be a decimal number in the open interval (0,100) '
            f'or "disable" (decimal charset only — keeps Go/Python accept-sets in lockstep)'
        )
    o = float(v)
    if not (0.0 < o < 100.0):
        raise RecipeError(
            f"objective {v!r} must be in the OPEN interval (0,100): 100 makes the "
            f"error budget 0 (threshold 0 → always fires), 0 makes it never fire"
        )
    return v


def _normalize_slo_period(inst: dict) -> str:
    """Validate + normalize the slo_burn_rate `slo_period` (budget window). Falsy
    (missing / null / empty) → default "30d". NOT a shape component — it only
    scales the exporter-computed threshold values (30d→14.4/6, 28d→13.44/5.6), so
    it never enters the slug/hash/rule text and changing it never re-slugs.
    Python's duty is the enum check only (ADR-029 dual-side validation)."""
    value = inst.get("slo_period", "30d")
    value = "30d" if value in (None, "") else str(value)
    if value not in ALLOWED_SLO_PERIOD:
        raise RecipeError(
            f"slo_period {value!r} must be one of {sorted(ALLOWED_SLO_PERIOD)}"
        )
    return value


def _normalize_min_events(inst: dict) -> int:
    """Validate + normalize the slo_burn_rate `min_events` (fast-window bad-event
    absolute floor, low-traffic false-positive guard). Missing / null → default
    10 (SLO_MIN_EVENTS_DEFAULT), which IS materialised into the slug (`minev10`)
    — so a later default change can never silently re-slug existing shapes.
    Must be a YAML INTEGER >= 1 (schema `type: integer`; integers are not
    YAML-dialect-ambiguous, unlike floats — see the quantile #1017 note). bool is
    an int subclass in Python; reject it explicitly (YAML `true` must not slug as
    minev1). MUST match custom_alert.go (Wave 2)."""
    value = inst.get("min_events", SLO_MIN_EVENTS_DEFAULT)
    if value is None:
        value = SLO_MIN_EVENTS_DEFAULT
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecipeError(
            f"min_events {value!r} must be a positive integer (a bare YAML int, "
            f"not a string/float/bool) — it enters the recipe_id slug as minev{{N}}"
        )
    if value < 1:
        raise RecipeError(
            f"min_events {value} must be >= 1 (0 would disable the low-traffic "
            f"guard entirely; use `objective: \"disable\"` to opt out instead)"
        )
    if value > 1_000_000:
        # mirrors the schema `maximum` + custom_alert.go sloMinEventsMax: an
        # absurd floor silently disables the alert while looking configured,
        # and the literal enters the rule text.
        raise RecipeError(
            f"min_events {value} exceeds the maximum 1000000 (schema maximum; "
            f"an absurd floor silently disables the alert while looking configured)"
        )
    return value


def validate_metric_name(metric: str, field: str = "metric") -> None:
    if not isinstance(metric, str) or not _METRIC_RE.fullmatch(metric):
        raise RecipeError(
            f"{field} {metric!r} is not a bare Prometheus metric name "
            f"(^[a-zA-Z_][a-zA-Z0-9_]*$, no colon/braces/operators) — "
            f"label filtering must use the `selectors`/`selectors_re` map "
            f"(ADR-024 §2e), never inline PromQL"
        )


def _validate_quantile(quantile: str) -> None:
    """Reject a p99_latency `quantile` that is not a DECIMAL number in (0,1). It is
    interpolated raw into histogram_quantile(<quantile>, …); a non-numeric value is a
    PromQL injection. The decimal-only regex keeps the Go/Python accept-set in lockstep
    (Go ParseFloat accepts hex-floats/underscores that float() rejects → poison-commit
    drift). MUST match custom_alert.go::validateQuantile."""
    if not _QUANTILE_RE.fullmatch(str(quantile)):
        raise RecipeError(f"quantile {quantile!r} must be a decimal number in (0,1)")
    try:
        q = float(quantile)
    except (TypeError, ValueError):
        raise RecipeError(f"quantile {quantile!r} is not a number in (0,1)")
    if not (0.0 < q < 1.0):
        raise RecipeError(f"quantile {quantile!r} must be in the open interval (0,1)")


def _reject_template_metachars(value: str, key: str) -> None:
    """Reject a selector VALUE that could become Go-template code in the annotation
    sink (see _TEMPLATE_METACHARS). MUST match custom_alert.go::rejectTemplateMetachars."""
    v = str(value)
    for mc in _TEMPLATE_METACHARS:
        if mc in v:
            raise RecipeError(
                f"selector value for {key!r} contains a Go-template metacharacter {mc!r}: "
                f"it would reach the alert-annotation template context where Prometheus "
                f"evaluates {{{{ … }}}} at fire time (cross-tenant PromQL injection). "
                f"Selector values may not contain '{{{{', '}}}}', or backticks."
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
        _reject_template_metachars(_value, key)
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
    # slo_burn_rate fixes op '>' (a burn RATE exceeding its budget threshold is
    # the only meaningful direction). An explicit different op is a semantic
    # error, not a knob — reject loudly rather than silently compile `gt`.
    # MUST match custom_alert.go (Wave 2).
    if recipe == "slo_burn_rate" and op != ">":
        raise RecipeError(
            f"op {op!r} is not settable for slo_burn_rate (the recipe fixes '>' — "
            f"burn rate exceeding the objective-derived threshold); omit `op`"
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
    elif recipe == "slo_burn_rate":
        # ADR-031: the burn windows (1h/5m fast, 6h/30m slow) are FIXED recipe
        # semantics — no `w{window}` slot (a stray authored `window` is ignored,
        # like forecast). `objective` replaces `threshold` (its VALUE rides the
        # data plane via user_threshold; the exporter derives the per-severity
        # burn thresholds from it at resolve time) — so `threshold` present is a
        # declaration error, rejected loudly, and objective/slo_period are
        # validated here (ADR-029 dual-side duty) but NEVER enter the slug.
        if "threshold" in inst:
            raise RecipeError(
                "slo_burn_rate takes `objective` (SLO target percentage), not "
                "`threshold` — severity is fixed by the recipe (fast→critical, "
                "slow→warning), so a value:severity threshold has no meaning here"
            )
        _validate_objective(inst)
        _normalize_slo_period(inst)
        den = inst.get("denominator_metric")
        if not den:
            raise RecipeError(
                "slo_burn_rate recipe requires `denominator_metric` (the "
                "TOTAL-events counter; `metric` is the BAD-events counter)"
            )
        validate_metric_name(den, "denominator_metric")
        parts.append("den_" + str(den))
        # min_events IS a shape component (the compiler writes the literal into
        # the rule text): always emitted, default 10 materialised — see grammar.
        parts.append("minev" + str(_normalize_min_events(inst)))
    else:
        window = str(inst.get("window", ""))
        if not _WINDOW_RE.fullmatch(window):
            raise RecipeError(
                f"window {window!r} is not a valid Go duration "
                f"(^([0-9]+(ns|us|µs|ms|s|m|h))+$); it is interpolated raw into "
                f"rate(…[<window>]) — an invalid value is a PromQL injection"
            )
        parts.append("w" + window)
        if recipe == "p99_latency":
            quantile = str(inst.get("quantile", "0.99"))
            _validate_quantile(quantile)
            parts.append("q" + quantile)
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
    if group_by and (recipe == "absence" or op == "==" or recipe == "slo_burn_rate"):
        what = ("the absence recipe" if recipe == "absence"
                else "the slo_burn_rate recipe (the SLI is a per-tenant aggregate "
                     "by design — a per-dimension SLO is a distinct declaration)"
                if recipe == "slo_burn_rate" else "op '=='")
        raise RecipeError(
            f"group_by (per-dimension eval) is not supported for {what} — it "
            f"applies only to value-crossing recipes (ADR-024 §Addendum)"
        )
    for gb in group_by:
        parts.append("gb_" + gb)

    # F3 (#1008): recipe_id must be INJECTIVE over shape_signature. The readable slug is
    # not — _sanitise is lossy AND the `s_{key}_{value}`/`__`-join is ambiguous even with
    # no lossy char ({region_x:1} and {region:x_1} both → `s_region_x_1`). A selector is
    # the only tenant-controlled free-form slug field (window/quantile/metric/for/group_by
    # are charset/enum-bounded post-validation), so a selector-bearing recipe carries a
    # disambiguation suffix over the STRUCTURED identity; a no-selector recipe stays
    # byte-identical. MUST stay byte-identical to custom_alert.go::RecipeID (golden-pinned).
    slug = _sanitise("__".join(parts))
    if _needs_shape_hash(inst):
        slug += "__x" + _shape_hash(inst)
    return slug


def shape_signature(inst: dict) -> Tuple:
    """Hashable identity used to dedup instances into one rule per shape.

    Two instances with the same signature compile to ONE vectorised rule
    covering all their tenants (O(M)); a difference in any PromQL-shaping
    param yields a distinct rule. NOT keyed by tenant or `name` (those ride
    the data plane) — see ADR-024 §2b.
    """
    is_forecast = inst["recipe"] == "forecast"
    is_slo = inst["recipe"] == "slo_burn_rate"
    return (
        inst["recipe"],
        inst["metric"],
        # den slot: ratio's/slo's denominator_metric OR forecast's capacity_metric.
        inst.get("capacity_metric") if is_forecast else inst.get("denominator_metric"),
        None if inst["recipe"] == "absence" else inst.get("op", ">"),
        # forecast has no window (lookback derives from horizon); slo_burn_rate
        # has none either (fixed burn windows are recipe semantics); others do.
        None if (is_forecast or is_slo) else str(inst.get("window", "")),
        str(inst.get("quantile", "0.99")) if inst["recipe"] == "p99_latency" else None,
        # forecast's horizon is a shaping param (predict-ahead distance).
        _normalize_horizon(inst) if is_forecast else None,
        tuple(_selector_items(inst)),
        # `for` distinguishes shapes (control-plane static attr; see recipe_id).
        _normalize_for(inst),
        # group_by dimensions distinguish per-dimension shapes (ADR-024 §Addendum).
        _normalize_group_by(inst),
        # slo_burn_rate's min_events is a shape component (the compiler writes
        # the literal into the rule text); None for every other recipe, so their
        # signatures stay content-identical (the tuple is in-memory only).
        _normalize_min_events(inst) if is_slo else None,
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
        "slo_burn_rate": "multi-window SLO error-budget burn-rate (fast→critical, slow→warning)",
    }
