"""Routing-config merging + tenant substitution + receiver building.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  _apply_timing_params(...)         → group_wait/interval/repeat with guardrails
  _substitute_tenant(obj, name)     → recursive {{tenant}} → name replacement
  _contains_tenant_placeholder(obj) → True if any string contains {{tenant}}
  merge_routing_with_defaults(...)  → shallow merge of defaults + tenant routing
  build_receiver_config(...)        → structured receiver dict → AM config dict
  skipped_entry_warning(line)       → mark a dropped-entry WARN line (#2489)
  replaced_value_warning(line)      → mark a replaced-value WARN line (#2490)
  Finding / FINDING_KINDS           → structured warning-stream lines (#2766)
  unclassified(line, blocks=...)    → a stream line with no kind yet (#2766)
"""
from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    PLATFORM_DEFAULTS,
    validate_and_clamp,
    receiver_exactly_one_problem,
    receiver_optional_problem,
    receiver_bool_fields,
    coerce_yaml_bool,
    receiver_required_problem,
    RECEIVER_TYPES,
)
from _lib_validation import INVALID_DURATION_HINT, am_duration_seconds  # noqa: E402
from _lib_validation import (  # noqa: E402  (#2766 finding kinds)
    FIELD_NOT_STRING, FIELD_SET, receiver_field_state,
)


# ── #2766 (ADR-036 §3): every warning-stream line is a structured finding ──
# The generator's verdict used to exist only as text, so a reader that needed
# "which tenant / which file / does it block" had to compare strings — and an
# already-violating tenant rewriting its config produces the SAME line as the
# base, which a diff of lines lets through. A ``Finding`` IS the line (a
# ``str`` whose value is exactly the text printed before #2766) plus the
# fields below; ``generate_alertmanager_routes --findings-json`` writes them.
#
# * ``kind`` — a stable snake_case id from ``FINDING_KINDS`` (da-guard's
#   ``FindingKind`` spelling where the meaning is the same). ``unclassified``
#   is a line nobody has given a kind yet: its TEXT is its only contract, and
#   the producer sites still emitting one are listed in the shrink-only
#   ``tests/ops/grar_unclassified_findings_baseline.json``.
# * ``severity`` — the SARIF level: ``error`` / ``warning`` / ``note``.
# * ``blocks`` — when it makes the generator fail: ``always`` (every mode),
#   ``strict`` (under ``--strict``), ``validate`` (under ``--validate``; the
#   same lines validate-config's schema / routes rows fail on), ``never``.
#   It restates the rule the predicates already apply (``_policy_errors``,
#   ``blocking_generation_errors``, the refusals) — it decides nothing: those
#   predicates still read the type and the prefix, unchanged.
# * ``tenant`` / ``policy`` / ``file`` / ``field`` — what the producer had as
#   a variable, else None. ⛔ Never parsed back out of a context string.
#   ``file`` is relative to ``--config-dir``.
#
# ⛔ The same rule as ``SkippedEntryWarning`` below, for every field: the
# attributes live on the str OBJECT, and any re-formatting (an f-string,
# ``"  " + w``, ``.strip()``, ``safe_label(w)``) returns a plain ``str``
# without them. Build the final text first, then wrap it.
FINDING_SEVERITIES = ("error", "warning", "note")
FINDING_BLOCKS = ("always", "strict", "validate", "never")
UNCLASSIFIED = "unclassified"

# kind -> one-line description. ⛔ The ONE catalog: a SARIF ``rules`` table is
# generated from it, and ``tests/ops/test_grar_findings.py`` refuses a kind
# literal under scripts/tools/ops that is not a key here.
FINDING_KINDS: dict[str, str] = {
    UNCLASSIFIED:
        "A warning-stream line that has no kind yet; only its text is stable.",
    "refusal_summary":
        "The framing line of a refusal (what was refused and why it stops "
        "the run); the itemised findings are separate.",
    # ADR-007 domain policies
    "domain_policy_violation":
        "A tenant's resolved routing (main route or a sub-route) breaks a "
        "domain policy constraint.",
    "domain_policy_unusable":
        "A domain policy file, policy or constraint cannot be read, so it is "
        "not enforced.",
    "domain_policy_out_of_scope":
        "A subtree domain policy names a tenant outside its subtree; that "
        "entry is not enforced.",
    "critical_escalation_missing":
        "require_critical_escalation is set and severity=critical alerts "
        "reach no escalation receiver.",
    "critical_escalation_leak":
        "The tenant escalates, but this other destination still receives "
        "some severity=critical alerts first.",
    # the conf.d tree
    "tenant_file_unreadable":
        "A tenant file could not be read or parsed; every tenant in it is "
        "absent from the run.",
    "duplicate_tenant":
        "One tenant id is declared by more than one tenant file.",
    "invalid_tenant_id":
        "A declared tenant id breaks the tenant-id rule (ADR-035); nothing "
        "is rendered for it.",
    "routing_enforced_below_root":
        "_routing_enforced in a file below the conf.d root.",
    "routing_defaults_null_below_root":
        "A subdirectory's _routing_defaults sets receiver or overrides to "
        "null.",
    "routing_profile_duplicate":
        "One routing-profile name is defined in two files.",
    "routing_in_unread_location":
        "A routing block in a file the generator never reads it from.",
    # routing blocks
    "routing_not_mapping":
        "A tenant's _routing is neither a mapping nor a disabling string; "
        "no route is rendered for it.",
    "routing_defaults_not_mapping":
        "A _routing_defaults that is neither a mapping nor null; that level "
        "contributes nothing.",
    "routing_defaults_routes_ignored":
        "routes under _routing_defaults, which the generator drops.",
    "routing_enforced_enabled_invalid":
        "_routing_enforced.enabled is not a YAML boolean; no enforced route "
        "is rendered.",
    "routing_value_not_string":
        "A matcher value YAML does not read as a string (quote it).",
    "routing_group_by_invalid":
        "A group_by element that is not a non-empty string, repeats a label "
        "or mixes '...' with labels.",
    # receivers and sub-route entries
    "missing_receiver_field":
        "A receiver, or a field it requires, is missing or not an object; "
        "the entry is not rendered.",
    "unknown_receiver_type":
        "A receiver type the generator does not support; the entry is not "
        "rendered.",
    "invalid_receiver_field":
        "A receiver field with a value the generator refuses; the entry is "
        "not rendered.",
    "conflicting_receiver_field":
        "A receiver sets fields of which exactly one is allowed; the entry "
        "is not rendered.",
    "receiver_domain_not_allowed":
        "A receiver host is outside the --policy allowed_domains; the entry "
        "is not rendered.",
    "receiver_host_unparseable":
        "A receiver URL whose host cannot be read, so the --policy domain "
        "check cannot run; the entry is not rendered.",
    "invalid_override_entry":
        "An overrides value or entry that is not the expected list / "
        "mapping; it is not rendered.",
    "empty_override_matcher":
        "An override with neither alertname nor metric_group; it is not "
        "rendered.",
    "conflicting_override_matcher":
        "An override with both alertname and metric_group; it is not "
        "rendered.",
    "invalid_route_entry":
        "A routes value or entry the generator does not render.",
    "timing_value_replaced":
        "A timing value Alertmanager cannot read, rendered as the platform "
        "default.",
}


def _severity_of(text: str) -> str:
    """The SARIF level a line's own level word states (``ERROR…`` → error,
    ``WARN…`` → warning, anything else — ``INFO`` / ``NOTICE`` — note)."""
    head = text.lstrip()
    if head.startswith("ERROR"):
        return "error"
    if head.startswith("WARN"):
        return "warning"
    return "note"


def _rebuild_finding(cls: type, text: str, attrs: dict) -> "Finding":
    """Unpickle / copy helper: ``__new__`` with the attributes it carried."""
    return cls(text, **attrs)


class Finding(str):
    """One warning-stream (or refusal) line, with its structured fields.

    The value is the line's exact text; see the #2766 block above for the
    fields. ``blocks`` is required here; the two marked subclasses below
    default it (they are blocking under ``--validate`` by definition).
    """

    __slots__ = ("kind", "severity", "blocks", "tenant", "policy", "file",
                 "field")
    _DEFAULT_BLOCKS: str | None = None

    def __new__(cls, text: str, *, kind: str = UNCLASSIFIED,
                severity: str | None = None, blocks: str | None = None,
                tenant: str | None = None, policy: str | None = None,
                file: str | None = None, field: str | None = None):
        if blocks is None:
            blocks = cls._DEFAULT_BLOCKS
        if blocks not in FINDING_BLOCKS:
            raise TypeError(f"Finding needs blocks= one of {FINDING_BLOCKS}, "
                            f"got {blocks!r}")
        obj = super().__new__(cls, text)
        obj.kind = kind
        obj.severity = severity if severity is not None else _severity_of(text)
        obj.blocks = blocks
        obj.tenant = tenant
        obj.policy = policy
        obj.file = file
        obj.field = field
        return obj

    def _attrs(self) -> dict:
        # Finding's own slots: a subclass's ``__slots__`` is its own ``()``.
        return {name: getattr(self, name) for name in Finding.__slots__}

    def __reduce__(self):
        return (_rebuild_finding, (type(self), str(self), self._attrs()))

    def with_text(self, text: str) -> "Finding":
        """The same finding with *text* as its line — for the one deliberate
        re-format, an output-layer escape (``safe_label``) of the line."""
        return type(self)(text, **self._attrs())

    def as_record(self) -> dict:
        """The ``--findings-json`` record: the fields plus ``message``, the
        text with its leading indentation stripped (only here — the stream
        keeps the text as printed)."""
        return {**self._attrs(), "message": str(self).lstrip()}


def unclassified(line: str, *, blocks: str) -> Finding:
    """Wrap a stream line no producer has classified yet (#2766).

    *blocks* states today's rule for the line. ⛔ Every call site is a row
    of ``tests/ops/grar_unclassified_findings_baseline.json`` (shrink-only):
    classify the line instead of adding a call.
    """
    if isinstance(line, Finding):
        return line
    return Finding(line, kind=UNCLASSIFIED, blocks=blocks)


def as_finding(line: str) -> Finding:
    """*line* as a ``Finding`` for a writer that must not fail: a plain str
    that slipped past every producer is reported as ``unclassified`` with
    ``never`` (the guard test refuses such a producer)."""
    return line if isinstance(line, Finding) else Finding(
        line, kind=UNCLASSIFIED, blocks="never")


# ── #2489: "a config entry was dropped as unusable" is a TYPE, not a word ──
# The generation warning stream stays ``list[str]`` and every line reads
# exactly as before; what marks a line blocking under ``--validate`` is that
# it was built by ``skipped_entry_warning``. ``blocking_generation_errors``
# (``_grar_validate``) tests ``isinstance``, never the text: the text carries
# the operator's values, and the substring test it replaces blocked a plain
# clamp WARN whose value happened to be ``skipping``.
# Defined here, not in ``_grar_validate``, because this module is the leaf
# every producer can import (``_grar_validate`` imports this one).
# ⛔ The mark lives on the str OBJECT: any re-formatting between the producer
# and the predicate (an f-string, ``"  " + w``, ``.strip()``, a join, a
# round-trip through text) returns a plain ``str`` and the line silently
# stops blocking. Append / extend / list concatenation keep it.
# ``tests/ops/test_grar_skipped_entry_warning.py`` refuses a ``WARN …,
# skipping`` literal under scripts/tools/ops/ that is not built here.
# #2766: a ``Finding`` like every stream line; the type still decides.
class SkippedEntryWarning(Finding):
    """A warning-stream line saying a config entry was dropped as unusable."""

    __slots__ = ()
    _DEFAULT_BLOCKS = "validate"


def skipped_entry_warning(line: str, *, kind: str = UNCLASSIFIED,
                          tenant: str | None = None, file: str | None = None,
                          field: str | None = None) -> SkippedEntryWarning:
    """Mark *line* (text unchanged) as a dropped-entry line — blocking under
    ``--validate`` and validate-config's ``schema`` / ``routes`` rows.

    #2766: *kind* / *tenant* / *file* / *field* are its ``Finding`` fields."""
    return SkippedEntryWarning(line, kind=kind, tenant=tenant, file=file,
                               field=field)


# ── #2490: "a config value was replaced as unusable" is a type too ────────
# A timing value Alertmanager cannot read (``1.5h``, ``30m1h``, ``1ns``) is
# not dropped with its entry — the route still renders, with the platform
# default in its place — so it is not a ``SkippedEntryWarning``: explain-route
# lists those as sub-routes "not in effect", and this route IS in effect.
# But the config as written cannot be used either, so ``--validate`` and
# validate-config refuse it: ``blocking_generation_errors`` counts this type
# as its own category. Same rules as ``SkippedEntryWarning`` — the mark lives
# on the str object and is lost by any re-formatting.
class ReplacedValueWarning(Finding):
    """A warning-stream line saying a config value was replaced as unusable."""

    __slots__ = ()
    _DEFAULT_BLOCKS = "validate"


def replaced_value_warning(line: str, *, kind: str = UNCLASSIFIED,
                           tenant: str | None = None,
                           field: str | None = None) -> ReplacedValueWarning:
    """Mark *line* (text unchanged) as a replaced-value line — blocking under
    ``--validate`` and validate-config's ``schema`` / ``routes`` rows.

    #2766: *kind* / *tenant* / *field* are its ``Finding`` fields."""
    return ReplacedValueWarning(line, kind=kind, tenant=tenant, field=field)


def _apply_timing_params(source_dict: dict, context_name: str, *,
                         tenant_id: str | None = None,
                         field_prefix: str = "") -> tuple[dict, list[str]]:
    """Apply timing parameters with guardrails to a route dict.

    Reads group_wait, group_interval, repeat_interval from source_dict,
    validates each against GUARDRAILS, and returns applied values + warnings.

    #2490: a value Alertmanager cannot read (``am_duration_seconds`` is None;
    the syntax is tenant-config.schema.json ``definitions.duration``) is
    rendered as the platform default, and its line is a
    ``ReplacedValueWarning``: rendering continues, ``--validate`` and
    validate-config refuse the config.

    #2766: *tenant_id* / *field_prefix* (``"overrides[0]."``) are only the
    ``Finding`` fields of the lines; *context_name* stays the text.

    Returns:
        (timing_dict, warnings_list) — timing_dict has clamped param values.
    """
    timing = {}
    warnings = []
    for param in ("group_wait", "group_interval", "repeat_interval"):
        val = source_dict.get(param)
        if val:
            if am_duration_seconds(str(val)) is None:
                default = PLATFORM_DEFAULTS[param]
                warnings.append(replaced_value_warning(
                    f"  WARN: {context_name}: invalid {param} '{val}' "
                    f"({INVALID_DURATION_HINT}), rendering the platform default "
                    f"{default} in its place; generate-routes --validate and "
                    "validate-config refuse this config",
                    kind="timing_value_replaced", tenant=tenant_id,
                    field=f"{field_prefix}{param}"))
                timing[param] = default
                continue
            clamped, param_warnings = validate_and_clamp(param, str(val), context_name)
            warnings.extend(unclassified(w, blocks="never")
                            for w in param_warnings)
            if clamped:
                timing[param] = clamped
    return timing, warnings


def _substitute_tenant(obj: object, tenant_name: str) -> object:
    """Replace {{tenant}} placeholders in all string values recursively."""
    if isinstance(obj, str):
        return obj.replace("{{tenant}}", tenant_name)
    if isinstance(obj, dict):
        return {k: _substitute_tenant(v, tenant_name) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_tenant(item, tenant_name) for item in obj]
    return obj


def _contains_tenant_placeholder(obj: object) -> bool:
    """Check if any string value contains {{tenant}} placeholder."""
    if isinstance(obj, str):
        return "{{tenant}}" in obj
    if isinstance(obj, dict):
        return any(_contains_tenant_placeholder(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_tenant_placeholder(item) for item in obj)
    return False


# ── #2326: the routing layer chain across conf.d directory levels ──────
# ADR-017 "Amendment 2026-09-28": `_routing_defaults` comes from the root
# (any root `_` file) and then from the defaults carrier of every directory on
# the tenant's path, each level a SHALLOW merge per top-level key, deeper
# wins; routing profiles are visible to the tenants at their own level or
# below. `level` is a directory relative to the conf.d root, POSIX-spelled,
# "." for the root itself. Go twin: `routingpolicy.Tree.LayersFor`.
ROOT_LEVEL = "."


def chain_levels(level: str) -> list[str]:
    """The directory levels BELOW the root on the way to *level*, root-first.

    `"a/b"` → `["a", "a/b"]`; the root (`"."` or `""`) → `[]`.
    """
    if level in (ROOT_LEVEL, ""):
        return []
    parts = level.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def level_contains(level: str, other: str) -> bool:
    """True when directory *other* is *level* itself or lies below it."""
    if level in (ROOT_LEVEL, ""):
        return True
    return other == level or other.startswith(level + "/")


def domain_policy_levels(parsed: dict) -> list[tuple[str, dict]]:
    """Every level's ``domain_policies``, in the order they are judged.

    #2326 (d): the root's first (``ROOT_LEVEL``), then each subdirectory
    level's in name order. ``load_tenant_tree`` judges them one level at a
    time over the tenants each reaches (``policy_reaches``), and
    ``explain_route --trace`` walks the same list, so the two cannot disagree
    about which policies a tenant meets.
    """
    levels = [(ROOT_LEVEL, parsed.get("domain_policies") or {})]
    levels.extend(sorted((parsed.get("domain_policies_by_dir") or {}).items()))
    return levels


def policy_reaches(level: str, tenant: str, tenant_dirs: dict[str, str]) -> bool:
    """True when a domain policy declared at directory *level* applies to
    *tenant* (#2326 (d)): a root policy reaches every tenant, a subtree
    policy only the tenants whose file sits in its subtree. A tenant no file
    places (absent from *tenant_dirs*) counts as a root tenant."""
    return level_contains(level, tenant_dirs.get(tenant, ROOT_LEVEL))


def resolve_routing_defaults(parsed: dict, level: str,
                             root_defaults: object = None) -> dict:
    """`_routing_defaults` as a tenant in directory *level* sees it.

    The root's value first (*root_defaults*, else ``parsed["routing_defaults"]``),
    then every subdirectory level's on the way down, each top-level key
    replacing the one above it WHOLE — a null included (it is stored; the
    field rules downstream omit or refuse it). A level whose value is not a
    mapping contributes nothing (its reader already said so).
    """
    root = parsed.get("routing_defaults") if root_defaults is None else root_defaults
    base = dict(root) if root else {}
    levels = parsed.get("routing_defaults_levels") or {}
    for d in chain_levels(level):
        rd = levels.get(d)
        if isinstance(rd, dict):
            base.update(rd)
    return base


def visible_routing_profiles(parsed: dict, level: str) -> dict:
    """The routing profiles a tenant in directory *level* can reference:
    those defined at the root, at *level*, or at any level in between.

    A parsed dict built without the per-directory index (a hand-built one in
    a test) falls back to the flat ``routing_profiles`` map.
    """
    by_dir = parsed.get("routing_profiles_by_dir")
    if by_dir is None:
        return parsed.get("routing_profiles", {})
    out: dict = {}
    for d in [ROOT_LEVEL, *chain_levels(level)]:
        out.update(by_dir.get(d, {}))
    return out


def merge_routing_with_defaults(defaults: dict, tenant_routing: dict | None, tenant_name: str) -> dict:
    """Merge _routing_defaults with tenant _routing.

    Rules:
    - Tenant values override defaults (shallow merge)
    - {{tenant}} in string values is replaced with tenant_name
    - Lists (e.g., group_by) are replaced, not concatenated
    """
    merged = dict(defaults)
    if isinstance(tenant_routing, dict):
        for key, value in tenant_routing.items():
            merged[key] = value
    return _substitute_tenant(merged, tenant_name)


def _exactly_one_kind(spec: dict, receiver_obj: dict) -> str:
    """#2766: the kind of a ``receiver_exactly_one_problem`` finding, from
    the same field states it judged (da-guard's split): a non-string value
    → invalid, none set → missing, more than one → conflicting."""
    for group in spec.get("exactly_one_of", []):
        states = [receiver_field_state(receiver_obj, f) for f in group]
        if FIELD_NOT_STRING in states:
            return "invalid_receiver_field"
        n_set = states.count(FIELD_SET)
        if n_set == 1:
            continue
        return "missing_receiver_field" if n_set == 0 else "conflicting_receiver_field"
    return "invalid_receiver_field"


def build_receiver_config(receiver_obj: dict, tenant: str, *,
                          tenant_id: str | None = None,
                          field: str = "receiver") -> tuple[dict | None, list[str]]:
    """Build Alertmanager receiver config from structured receiver object.

    Args:
        receiver_obj: dict with 'type' and type-specific fields.
        tenant: tenant name for error messages.
        tenant_id: #2766 — the tenant, when *tenant* is a context string
            (``alpha-override-0``) or the caller knows it; else None.
        field: #2766 — the receiver's path in the routing (``receiver``,
            ``overrides[0].receiver``, ``_routing_enforced.receiver``).

    Returns:
        (am_config_dict, warnings) where am_config_dict is e.g.
        {"webhook_configs": [{"url": "..."}]} or None on error.
    """
    warnings = []

    def at(suffix: str = "") -> dict:
        """#2766: the Finding fields of a line about *field* + *suffix*."""
        return {"tenant": tenant_id, "field": f"{field}{suffix}"}

    if not isinstance(receiver_obj, dict):
        warnings.append(skipped_entry_warning(
            f"  WARN: {tenant}: 'receiver' must be an object with 'type', skipping",
            kind="missing_receiver_field", **at()))
        return None, warnings

    rtype = receiver_obj.get("type")
    if not rtype or not isinstance(rtype, str):
        warnings.append(skipped_entry_warning(
            f"  WARN: {tenant}: missing required 'receiver.type', skipping",
            kind="missing_receiver_field", **at(".type")))
        return None, warnings

    # Exact match, no case folding or trimming (#2180): the schema (`const`)
    # and the Go guard both reject `Email` / ` email`, so normalising here
    # made the generator the only one of the three to accept them.
    if rtype not in RECEIVER_TYPES:
        supported = ", ".join(sorted(RECEIVER_TYPES.keys()))
        warnings.append(skipped_entry_warning(
            f"  WARN: {tenant}: unknown receiver type '{rtype}' "
            f"(supported: {supported}), skipping",
            kind="unknown_receiver_type", **at(".type")))
        return None, warnings

    spec = RECEIVER_TYPES[rtype]

    # Validate required fields: presence, type and format by the schema (#2180)
    for req in spec["required"]:
        problem = receiver_required_problem(rtype, receiver_obj, req)
        if problem:
            # #2766: absent → missing (da-guard's split), present → invalid.
            warnings.append(skipped_entry_warning(
                f"  WARN: {tenant}: receiver type '{rtype}' {problem}, skipping",
                kind=("missing_receiver_field" if req not in receiver_obj
                      else "invalid_receiver_field"), **at(f".{req}")))
            return None, warnings
    problem = receiver_exactly_one_problem(rtype, receiver_obj)
    if problem:
        warnings.append(skipped_entry_warning(
            f"  WARN: {tenant}: receiver type '{rtype}' {problem}, skipping",
            kind=_exactly_one_kind(spec, receiver_obj), **at()))
        return None, warnings
    # #2295: optional values Alertmanager cannot load (non-boolean
    # send_resolved / require_tls, a malformed http_config) — without this the
    # receiver is written out and only amtool, when it is on PATH, stops it.
    problem = receiver_optional_problem(rtype, receiver_obj)
    if problem:
        warnings.append(skipped_entry_warning(
            f"  WARN: {tenant}: receiver type '{rtype}' {problem}, skipping",
            kind="invalid_receiver_field", **at()))
        return None, warnings

    # Build AM config — include required + present optional fields
    am_entry = {}
    for fld in spec["required"] + spec["optional"]:
        if fld in receiver_obj:
            am_entry[fld] = receiver_obj[fld]

    # Alertmanager's email_config.to is a single STRING (comma-separated for
    # multiple recipients). conf.d authors — and _routing_defaults — conventionally
    # write `to` as a YAML list (one address per line), which is far more readable.
    # Coerce that list into AM's comma-joined string here, at the boundary where we
    # emit AM config: a raw YAML sequence trips Alertmanager's parser with
    # `cannot unmarshal !!seq into string` at config load (amtool check-config),
    # a failure the dict-only Python validation never surfaces.
    # #2295: a YAML 1.1 boolean word that reached us as a string (quoted, or
    # from JSON) is written as the boolean it means: Alertmanager's yaml.v2
    # refuses a quoted 'yes' for a bool field. receiver_optional_problem has
    # already refused every other non-boolean.
    for fld in receiver_bool_fields(rtype):
        if fld in am_entry:
            am_entry[fld] = coerce_yaml_bool(am_entry[fld])
    http_config = am_entry.get("http_config")
    if isinstance(http_config, dict) and "proxy_from_environment" in http_config:
        am_entry["http_config"] = {
            **http_config,
            "proxy_from_environment": coerce_yaml_bool(http_config["proxy_from_environment"]),
        }

    if rtype == "email" and isinstance(am_entry.get("to"), list):
        am_entry["to"] = ", ".join(str(addr) for addr in am_entry["to"])

    return {spec["am_key"]: [am_entry]}, warnings
