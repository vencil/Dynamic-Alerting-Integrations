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

import copy
import fnmatch
import os
import re
import sys
from types import MappingProxyType
from typing import Mapping, NamedTuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    PLATFORM_DEFAULTS,
    validate_and_clamp,
    receiver_exactly_one_problem,
    receiver_optional_problem,
    receiver_required_problem,
    RECEIVER_TYPES,
)
from _lib_validation import INVALID_DURATION_HINT, am_duration_seconds  # noqa: E402
from _lib_validation import (  # noqa: E402  (#2766 finding kinds)
    FIELD_NOT_STRING, FIELD_SET, receiver_field_state,
)
from _lib_validation import (  # noqa: E402  (#1560 Secret references)
    CREDENTIAL_REF_SITES, HOST_PORT_DESTINATIONS, CredentialRefSite, RefPath,
    find_credential_refs,
    is_secret_ref, receiver_value_repr, ref_path_text, ref_value_at, shown_value,
    site_path_text,
)
from _lib_validation import shown_file  # noqa: E402  (#3703: conf.d paths into findings)


# ── #2766 (ADR-036 §4): every warning-stream line is a structured finding ──
# The generator's verdict used to exist only as text, so a reader that needed
# "which tenant / which file / does it block" had to compare strings — and an
# already-violating tenant rewriting its config produces the SAME line as the
# base, which a diff of lines lets through. A ``Finding`` IS the line (a
# ``str`` whose value is exactly the text printed before #2766) plus the
# fields below; ``generate_alertmanager_routes --findings-json`` writes them.
#
# * ``kind`` — a stable snake_case id from ``FINDING_KINDS`` (da-guard's
#   finding kind spelling where the meaning is the same). ``unclassified``
#   is a line nobody has given a kind yet: its TEXT is its only contract, and
#   the producer sites still emitting one are listed in the shrink-only
#   ``tests/ops/grar_unclassified_findings_baseline.json``.
# * ``severity`` — the SARIF level: ``error`` / ``warning`` / ``note``.
# * ``blocks`` — when it makes the generator fail: ``always`` (every mode),
#   ``strict`` (under ``--strict``), ``validate`` (under ``--validate``; the
#   same lines validate-config's routes row fails on), ``never``.
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
    "run_failed":
        "The run exited non-zero and no other finding is an error; the "
        "reason is only in the run's stderr. Never printed: added by "
        "--findings-json so a reader of the findings alone cannot pass it.",
    # ADR-007 domain policies
    "domain_policy_violation":
        "A tenant's resolved routing (main route or a sub-route) breaks a "
        "domain policy constraint.",
    "domain_policy_unusable":
        "A domain policy file, policy or constraint cannot be enforced as "
        "written (wrong shape or type, an entry that is no receiver type or "
        "tenant id, an unreadable duration bound, a file that cannot be "
        "used, a domain_policies block in a file other than "
        "_domain_policy.yaml / .yml); the whole tree is refused in every "
        "mode.",
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
    "unselected_defaults_carrier":
        "A second defaults carrier (_defaults.yaml / _defaults.yml spelling) "
        "in one directory; only one is read, the other is ignored whole.",
    "routing_profiles_unusable":
        "A routing_profiles block that is not a mapping; no profile in it is "
        "applied.",
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
        "_routing_enforced.enabled is not a boolean (a string such as "
        "\"true\" included); the whole tree is refused in every mode.",
    "severity_dedup_invalid":
        "A _severity_dedup the exporter does not read, or one written with a "
        "time window or expires (da-guard severity-dedup, #3621); the whole "
        "tree is refused in every mode.",
    "severity_dedup_excluded":
        "A tenant set _severity_dedup: disable, so it is in the tenant!~ list "
        "of the one severity dedup inhibit rule (#3899): both its warning and "
        "critical notifications are sent. A note; never blocks.",
    "unknown_routing_profile":
        "A tenant's _routing_profile names a profile its directory chain "
        "does not define.",
    "routing_profile_invalid":
        "A tenant's _routing_profile is neither a profile name (a string) "
        "nor null — a mapping such as a time-window schedule included; the "
        "whole tree is refused in every mode.",
    "routing_enforced_match_invalid":
        "_routing_enforced.match is written but is not a list of matcher "
        "strings (e.g. a {severity: critical} mapping); no enforced route "
        "is rendered.",
    "routing_value_not_string":
        "A matcher value YAML does not read as a string (quote it).",
    "routing_group_by_invalid":
        "A group_by element that is not a non-empty string, repeats a label "
        "or mixes '...' with labels; or a group_by that is not a list (e.g. "
        "group_by: alertname), which refuses the whole tree in every mode.",
    # receivers and sub-route entries
    "missing_receiver_field":
        "A receiver, or a field it requires, is missing or not an object; "
        "the entry is not rendered.",
    "unknown_receiver_type":
        "A receiver type the generator does not support; the entry is not "
        "rendered.",
    "invalid_receiver_field":
        "A receiver field with a value the generator refuses; the entry is "
        "not rendered. A boolean field that is not a boolean (send_resolved, "
        "require_tls, http_config.proxy_from_environment) refuses the whole "
        "tree in every mode instead.",
    "conflicting_receiver_field":
        "A receiver sets fields of which exactly one is allowed; the entry "
        "is not rendered.",
    "receiver_domain_not_allowed":
        "A receiver host is outside the --policy allowed_domains; the entry "
        "is not rendered.",
    "receiver_host_unparseable":
        "A receiver URL whose host cannot be read, so the --policy domain "
        "check cannot run; the entry is not rendered.",
    # #1560: which Secret a receiver's Secret reference may name.
    # #1560 ⑤-b: credentials written out, judged in the file that writes them.
    "credential_plaintext":
        "A file writes a credential out where only a Secret reference is "
        "taken, a URL carrying userinfo (user[:password]@), or a proxy "
        "credential no receiver may write (proxy_connect_header, a "
        "proxy_url with userinfo); judged on each file's own value, before "
        "any layer is merged. The line names the file, the place and the "
        "category, never the value. Blocks under --validate.",
    "credential_unjudged":
        "A receiver, or a list holding receivers, whose credentials the "
        "plaintext judgement cannot read: a receiver that is not a mapping or "
        "whose type is not a receiver type, an overrides / routes that is "
        "not a list, an item of one that is not a mapping. Blocks under "
        "--validate.",
    "secret_ref_shared_unknown":
        "A Secret reference names a shared alias the deployment policy's "
        "shared_secrets does not list (or no --policy is given); the entry "
        "is not rendered.",
    "secret_ref_shared_field_not_allowed":
        "A Secret reference uses a shared Secret on a field whose full path "
        "(api_url, http_config.basic_auth.password) its shared_secrets entry "
        "does not list under `fields`; the entry is not rendered.",
    "secret_ref_shared_confined":
        "A shared Secret on a field whose credential goes to a destination "
        "the receiver names (email auth_password, the webhook http_config "
        "credentials) on a receiver a tenant owns; only the platform-wide "
        "_routing_enforced (no {{tenant}}) may, under the policy's "
        "allowed_domains, follow_redirects: false and no proxy_url (proxy_from_environment "
        "is the deployer's, allowed); the entry is "
        "not rendered.",
    "secret_ref_destination_unconfined":
        "The tenant's Secret on a field whose credential goes to a "
        "destination the receiver names, but the --policy allowed_domains is "
        "empty or absent, or does not admit that destination's host (or the "
        "destination is missing or itself a Secret reference); the entry is "
        "not rendered.",
    "secret_ref_no_owner":
        "A receiver no tenant owns (the platform-wide _routing_enforced, "
        "without {{tenant}}) references a tenant's own Secret, whose name "
        "derives from the tenant; the entry is not rendered.",
    "secret_ref_proxy_url":
        "A receiver with a shared Secret reference, or a reference whose "
        "destination the allowlist confines, also sets the proxy_url that "
        "request goes through, a proxy the tenant chose that would see it; "
        "the entry is not rendered.",
    "secret_ref_follow_redirects":
        "A credential from the tenant's Secret that Alertmanager sends again "
        "on a redirect (basic_auth, authorization, bearer_token, oauth2, an "
        "http_headers value) on a receiver that does not set "
        "http_config.follow_redirects: false; a redirect could carry it to a "
        "host allowed_domains did not admit; the entry is not rendered.",
    "secret_ref_unsupported_field":
        "A Secret reference is still in the Alertmanager entry after "
        "rendering — on a field, at any depth, that has no *_file form "
        "(email headers, the http_config leaves CREDENTIAL_REF_SITES does not "
        "list); the entry is not rendered.",
    "invalid_override_entry":
        "An overrides value or entry that is not the expected list / "
        "mapping; it is not rendered.",
    "empty_override_matcher":
        "An override with neither alertname nor metric_group; it is not "
        "rendered.",
    "conflicting_override_matcher":
        "An override with both alertname and metric_group; it is not "
        "rendered.",
    "unknown_override_metric_group":
        "An override's metric_group is the group of no shipped rule pack "
        "alert (#3692); it routes nothing unless the tenant's own rules carry "
        "that group. Rendered; advisory.",
    "duplicate_override_matcher":
        "An override with the same matcher as an earlier one; the earlier "
        "one catches those alerts first, so it never takes effect.",
    "redundant_override_receiver":
        "An override whose receiver is identical to the main receiver; it "
        "changes no destination.",
    "invalid_route_entry":
        "A routes value or entry the generator does not render.",
    "parent_receiver_not_rendered":
        "An overrides / routes entry (or the whole value) of a tenant whose "
        "main receiver is not rendered; no child route is rendered without "
        "it.",
    "routing_key_ignored":
        "A key in a routing block, routing profile, overrides entry or "
        "receiver that the generator does not read; the rest is rendered "
        "as if the key were absent.",
    "timing_value_replaced":
        "A timing value Alertmanager cannot read, rendered as the platform "
        "default.",
    # ADR-038 §5: the shared key verdict, under --validate. A finding the
    # verdict says blocks (an error) is `blocks: validate` (#3016).
    "key_unread":
        "A key no reader reads, by the shared key verdict (ADR-038); the line "
        "names the verdict's own severity, and an error fails --validate.",
    "key_disable_noop":
        "A `disable` that switches nothing off (ADR-038 decision 8).",
    "key_disable_cascade":
        "A `<base>_critical` / `<base>{…}` key the tenant's own file writes, "
        "switched off with its base's `disable` (ADR-038 decision 8, #3046).",
    "key_profile_not_judged":
        "A threshold key of a profile no tenant binds, which the shared key "
        "verdict does not judge (ADR-038 decision 4, #3395); a notice.",
    "key_value_unread":
        "A reserved key's value the exporter does not read, in the file that "
        "writes it (ADR-038 decision 10, #3591): an error in a tenant file, "
        "a notice in a profile or a root platform file's `tenants:` entry.",
    "key_dead_entry":
        "A key read whose schedule has an entry /metrics serves at no minute "
        "of the day — a window the windows before it cover, or a `default` the "
        "windows cover (#3502): an error in the file the value comes from.",
    "key_schedule_unread":
        "A reserved key written with a time window or `expires:` / `reason:`, "
        "which its readers ignore: they read its `default` alone (#3269, "
        "#3834). Judged in the file that writes it as key_value_unread is: an "
        "error in a tenant file, a notice in a profile or a root platform "
        "file's `tenants:` entry.",
    "key_undecidable":
        "No root platform file was read, so the threshold keys were not "
        "judged (ADR-038 decision 7); with any such key, --validate fails.",
    "key_version_label":
        "A dimensional key's `version` label the exporter refuses (ADR-024 "
        "OQ-6), as the exporter words it.",
    "key_renamed":
        "A key written in a retired spelling (#1231), as the exporter words "
        "it; advisory.",
    "key_verdict_incomplete":
        "The shared key verdict could not be given (da-guard failing, rules "
        "Policy-as-Code refuses); the keys were not judged and --validate "
        "fails.",
    # #1211: a tenant's served page-mode custom alerts get no delivery child
    # under the component="custom" route and fall to the firehose receiver.
    "custom_alert_undelivered":
        "A tenant's served page-mode custom alerts fall to the custom-alerts "
        "firehose receiver; the line says whether that receiver has an "
        "integration or could not be checked.",
    "watchdog_undelivered":
        "The assembled config's watchdog-heartbeat receiver has no "
        "integration, so the Watchdog heartbeat reaches no dead-man's switch; "
        "an error under --strict only on the operator's own base (#1211).",
    "custom_alert_delivery_unchecked":
        "The tenants' served custom alerts could not be read (da-guard "
        "failing), so their delivery was not checked; the verdict is unchanged.",
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
        # #3703: a conf.d path the author named, through its one exit here,
        # so every `file=` of every producer is covered.
        obj.file = shown_file(file)
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
    ``always`` — fail closed: a line nobody classified may be blocking, and
    the document must never be looser than the run (the guard test refuses
    such a producer)."""
    return line if isinstance(line, Finding) else Finding(
        line, kind=UNCLASSIFIED, blocks="always")


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


# ── #2960: "a key the generator does not read" is a type too ─────────────
# A routing block (a tenant `_routing`, a routing profile, `_routing_defaults`,
# `_routing_enforced`, an `overrides` entry, a receiver) that carries a key
# the generator never reads (`continue`, `mute_time_intervals`, a typo) still
# renders — without that key, exactly as if it were absent. So it is neither a
# `SkippedEntryWarning` (explain-route would list a live route as "not in
# effect") nor a `ReplacedValueWarning`; but what was written is not what is
# rendered, so `--validate` and validate-config refuse it:
# `blocking_generation_errors` counts this type as its own category. Same
# rules as the two types above — the mark lives on the str object.
class IgnoredKeyWarning(Finding):
    """A warning-stream line naming a routing key the generator does not read."""

    __slots__ = ()
    _DEFAULT_BLOCKS = "validate"


def ignored_key_warning(line: str, *, kind: str = UNCLASSIFIED,
                        tenant: str | None = None, file: str | None = None,
                        field: str | None = None) -> IgnoredKeyWarning:
    """Mark *line* (text unchanged) as an ignored-key line — blocking under
    ``--validate`` and validate-config's ``schema`` / ``routes`` rows."""
    return IgnoredKeyWarning(line, kind=kind, tenant=tenant, file=file,
                             field=field)


# ── #1560 ⑤-b: "a credential is written out" is a type too ───────────────
# A credential a file writes out where the policy refuses it (or a receiver
# whose credentials cannot be judged) is judged in the FILE that writes it,
# before any layer is merged (``_grar_parse.credential_findings``), not on a
# rendered entry: the receiver may still render. Its own type, so
# ``blocking_generation_errors`` refuses it under ``--validate`` and
# validate-config's ``routes`` row; same rules as the types above.
class CredentialRefusal(Finding):
    """A line naming a credential a file writes out where it is refused."""

    __slots__ = ()
    _DEFAULT_BLOCKS = "validate"


def credential_refusal(line: str, *, kind: str, tenant: str | None = None,
                       file: str | None = None,
                       field: str | None = None) -> CredentialRefusal:
    """Mark *line* as a credential refusal — blocking under ``--validate``
    and validate-config's ``routes`` row."""
    return CredentialRefusal(line, kind=kind, tenant=tenant, file=file,
                             field=field)


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
                    f"  WARN: {context_name}: invalid {param} {shown_value(val)} "
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
# ADR-017 Decision 9: `_routing_defaults` comes from the root
# (any root `_` file) and then from the defaults carrier of every directory on
# the tenant's path, each level a SHALLOW merge per top-level key, deeper
# wins; routing profiles are visible to the tenants at their own level or
# below. `level` is a directory relative to the conf.d root, POSIX-spelled,
# "." for the root itself.
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
    the same field states it judged: a non-string value
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


def _exactly_one_field(spec: dict, receiver_obj: dict) -> str | None:
    """#2904: the receiver field a ``receiver_exactly_one_problem`` finding is
    about, from the same field states: the first non-string field of the
    group, else the group's first field when none is set, else the LAST field
    set. None when no group has a problem."""
    for group in spec.get("exactly_one_of", []):
        states = [receiver_field_state(receiver_obj, f) for f in group]
        if FIELD_NOT_STRING in states:
            return group[states.index(FIELD_NOT_STRING)]
        set_fields = [f for f, st in zip(group, states) if st == FIELD_SET]
        if len(set_fields) == 1:
            continue
        return set_fields[-1] if set_fields else group[0]
    return None


def receiver_type(receiver_obj: object) -> str:
    """The receiver type every check reads: the receiver's ``type`` when the
    receiver is a mapping and the type a non-empty string, else ``""``.

    #2780: the one read of ``receiver.type``. A type PyYAML builds as
    anything but a string (``{a: 1}``, ``[a]``, ``5``, ``true``, ``~``) names no receiver
    type; a mapping or a list is unhashable and crashed every ``in`` /
    ``dict.get`` a caller did with it. ``build_receiver_config`` refuses such
    a receiver (``missing_receiver_field``).
    """
    if not isinstance(receiver_obj, dict):
        return ""
    rtype = receiver_obj.get("type")
    return rtype if isinstance(rtype, str) else ""


# ── #1560: Secret references — which Secret, and the path Alertmanager reads ─
# Owner decision of 2026-11-04 on #1560. A reference never names its Secret:
# `{secret: {key}}` is the owner tenant's own Secret, `da-<tenant>`, and
# `{secret: {shared: <alias>, key}}` is the Secret the deployment policy's
# `shared_secrets[<alias>]` names, for the fields that entry lists. A name a
# tenant cannot write is a name it cannot point at another tenant's Secret.
# The ConfigMap output reads it as `<field>_file: <root>/<secret>/<key>`;
# <root> defaults to Prometheus Operator's `spec.secrets` mount layout. The
# Secrets are created by the operator / ExternalSecrets, and mounting each one
# at <root>/<name>/ is the deployment's contract (BYO integration guide §5:
# docs/integration/byo-alertmanager-integration.md); the generator cannot see them.
DEFAULT_MOUNT_ROOT = "/etc/alertmanager/secrets"
#: the tenant's own Secret is `da-<tenant>`; a shared Secret may not start so.
OWN_NAME_PREFIX = "da-"
# Which proxies the requests carrying a credential go through are each site's
# `proxies` (CREDENTIAL_REF_SITES): http_config.proxy_url, and for oauth2
# oauth2.proxy_url as well (the token request). proxy_from_environment is Alertmanager's own
# environment, not the tenant's to choose, and is never refused.


class SharedSecret(NamedTuple):
    """One ``shared_secrets`` entry of the deployment policy."""

    secret: str
    fields: frozenset[str]


class SecretBinding(NamedTuple):
    """What the generator needs to bind and render Secret references: the
    mount root and the policy's ``shared_secrets`` (empty: no shared
    reference is accepted). The default is no policy."""

    mount_root: str = DEFAULT_MOUNT_ROOT
    shared_secrets: Mapping[str, SharedSecret] = MappingProxyType({})


NO_SECRET_POLICY = SecretBinding()

# `owner_tenant` not passed (distinct from None, "no tenant owns it").
_OWNER_UNSET = object()


def own_secret_name(tenant: str) -> str:
    """The name of tenant *tenant*'s own Secret: ``da-<tenant>``. A tenant id
    is a DNS-1123 label (ADR-035), so this is a valid Secret name."""
    return f"{OWN_NAME_PREFIX}{tenant}"


# ── #1560: which Secrets the emitted config needs mounted, and which keys ────
# The list is read off the EMITTED config (as Alertmanager loads it), never
# re-derived from conf.d: every path Alertmanager will open is in that config,
# the base config's own receivers (watchdog-heartbeat's `url_file`) and its
# `global:` included, and nothing else is. A second derivation from the
# references would miss the base's paths and could disagree with the render.
#: `generate-routes --secret-mounts-out`'s document.
MOUNT_LIST_SCHEMA = "da-tools.secret-mounts/v1"


def _file_paths(node: object, in_headers: bool = False) -> list[str]:
    """Every file path *node* (a config as Alertmanager loads it) names: the
    string value of each ``*_file`` key, at any depth, and the entries of an
    ``http_headers.<name>.files`` list (prometheus/common's header files)."""
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.endswith("_file") and isinstance(v, str):
                found.append(v)
            elif in_headers and k == "files" and isinstance(v, list):
                found.extend(x for x in v if isinstance(x, str))
            else:
                found.extend(_file_paths(v, in_headers or k == "http_headers"))
    elif isinstance(node, list):
        for item in node:
            found.extend(_file_paths(item, in_headers))
    return found


#: The characters Go's ``filepath.Glob`` (Alertmanager's ``templates``)
#: reads as a pattern (``\\`` escapes one).
_GLOB_CHARS = frozenset("*?[\\")


def required_mounts(config: Mapping, mount_root: str) -> dict:
    """The Secrets *config* (an Alertmanager config as Alertmanager loads it)
    reads under *mount_root*, as the ``--secret-mounts-out`` document.

    A path ``<root>/<name>/<key>`` (exactly two non-empty segments under the
    root: Prometheus Operator mounts Secret ``<name>`` at ``<root>/<name>/``,
    one file per key, and a key holds no ``/``) lists Secret ``<name>`` with
    ``<key>`` under ``mounts``. Every other path a ``*_file`` field names —
    outside the root, or under it in another shape — is not a Secret this
    list can name; it is kept in ``other_files`` so the deployment sees it
    has to provide it some other way, instead of it vanishing.

    The top-level ``templates`` list is read too (CodeRabbit on #3615): its
    entries are globs Alertmanager expands at load. A literal name with a
    literal key lists both; a literal name with a glob key lists the name
    with no key for it (the Secret must be mounted; which keys match is the
    glob's business); a glob in the name segment cannot name a Secret and
    goes to ``other_files``, as does a template outside the root.

    Names and keys are coordinates, never the values. Sorted, so the same
    config gives the same bytes."""
    root = mount_root.rstrip("/")  # "/" → "": the render's own spelling
    prefix = root + "/"
    keys_by_name: dict[str, set[str]] = {}
    other: set[str] = set()

    def segments(path: str) -> list[str]:
        parts = path[len(prefix):].split("/") if path.startswith(prefix) else []
        return parts if len(parts) == 2 and all(parts) else []

    for path in _file_paths(config):
        parts = segments(path)
        if parts:
            keys_by_name.setdefault(parts[0], set()).add(parts[1])
        else:
            other.add(path)
    templates = config.get("templates") if isinstance(config, Mapping) else None
    for path in templates if isinstance(templates, list) else []:
        if not isinstance(path, str) or not path:
            continue  # an empty entry names no file: nothing to mount or provide
        parts = segments(path)
        if not parts or _GLOB_CHARS & set(parts[0]):
            other.add(path)
            continue
        keys = keys_by_name.setdefault(parts[0], set())
        if not _GLOB_CHARS & set(parts[1]):
            keys.add(parts[1])
    return {
        "schema": MOUNT_LIST_SCHEMA,
        "mount_root": mount_root,
        "mounts": [{"name": n, "keys": sorted(keys_by_name[n])}
                   for n in sorted(keys_by_name)],
        "other_files": sorted(other),
    }


def _receiver_secret_refs(receiver_obj: object) -> list[tuple[CredentialRefSite, RefPath]]:
    """``(site, path)`` of every Secret reference of *receiver_obj* that
    stands where ``CREDENTIAL_REF_SITES`` lets one stand (a top-level field
    or an http_config leaf). A receiver of no known type has none."""
    if receiver_type(receiver_obj) not in RECEIVER_TYPES:
        return []
    return find_credential_refs(receiver_obj)


def _is_set(value: object) -> bool:
    """Empty / null counts as not set, as in Alertmanager."""
    return value not in (None, "")


def secret_binding_findings(owner_tenant: str | None, receiver_obj: dict,
                            binding: SecretBinding, allowed_domains: list[str] | None,
                            *, ctx: str, tenant_id: str | None = None,
                            field: str = "receiver") -> list[Finding]:
    """#1560: the binding problems of the Secret references in one EFFECTIVE
    receiver (after the routing layers are merged and ``{{tenant}}`` is
    substituted) — every one blocks (the entry is not rendered).

    *owner_tenant* is the tenant the receiver delivers for: its own Secret is
    ``da-<owner_tenant>``. None means no tenant owns it (the platform-wide
    ``_routing_enforced``), where only a shared reference can be bound.
    *binding* carries the policy's ``shared_secrets``; *allowed_domains* is
    the policy's allowlist (None or empty: none). The reference shapes have
    already been judged (``receiver_optional_problem``).

    The rules, per ``CREDENTIAL_REF_SITES`` row:

    * a shared alias must be listed, and its entry must list the row's full
      path (``site_path_text``: ``api_url``,
      ``http_config.basic_auth.password``) under ``fields``;
    * a row with ``destinations`` (email auth_password → smarthost; every
      webhook http_config leaf → url, the oauth2 ones → oauth2.token_url and
      url, where the access token they yield is sent): whoever writes the
      receiver writes where the credential goes, so the credential is
      confined by *allowed_domains* — non-empty, and admitting every
      destination's host, written out (a destination that is missing, or
      itself a reference, cannot be checked). On a receiver a tenant owns it
      never takes a shared Secret (the tenant would point the platform's
      credential at a host of its choosing) and takes the tenant's own under
      that allowlist; on the receiver no tenant owns (*owner_tenant* None:
      the platform-wide ``_routing_enforced``, read only from a root ``_``
      file, which tenant-api cannot write) it takes a shared Secret the
      policy lists for the row, under the same allowlist, and the redirect
      and proxy rules below (#1560 ⑤-b, 2-c);
    * a shared reference, or a reference on a row with destinations, refuses
      a set proxy on any of its row's ``proxies`` (a proxy the tenant chose sees a
      plain-http request whole, and an https one whenever the receiver's
      own tls_config — a CA the tenant chose, insecure_skip_verify — lets
      it intercept); an own reference whose content IS the destination does
      not (the tenant may route its own webhook as it likes, as before);
    * a row that ``follows_redirects`` (every http_config credential but the
      TLS client key) needs ``http_config.follow_redirects: false``: upstream
      re-adds Authorization on a redirect to a subdomain of the original
      host, and a custom header on a redirect to any host
      (``CredentialRefSite``).
    """
    out: list[Finding] = []

    def refuse(text: str, where: str, *, kind: str) -> None:
        out.append(skipped_entry_warning(
            f"  WARN: {ctx}: receiver {where} {text}, skipping",
            kind=kind, tenant=tenant_id, field=f"{field}.{where}"))

    # proxy path → the reasons a proxy there is refused (one finding each path)
    proxied: dict[tuple[str, ...], list[str]] = {}
    redirect_paths: list[str] = []
    for site, path in _receiver_secret_refs(receiver_obj):
        where = ref_path_text(path)
        alias = ref_value_at(receiver_obj, path)["secret"].get("shared")
        if alias is None:
            if owner_tenant is None:
                refuse("references a tenant's own Secret (no 'shared'), but "
                       "no tenant owns this receiver; use a shared Secret the "
                       "deployment policy lists, or write {{tenant}} in "
                       "_routing_enforced to render one receiver per tenant",
                       where, kind="secret_ref_no_owner")
            elif site.destinations:
                checked = ", ".join(ref_path_text(d) for d in site.destinations)
                if not allowed_domains:
                    # One sentence for every destination, not one per destination.
                    problems = ["the deployment policy (--policy) has no allowed_domains "
                                f"to confine the {checked} it is sent to"]
                else:
                    problems = [p for d in site.destinations
                                if (p := _destination_problem(receiver_obj, d, allowed_domains))]
                if problems:
                    refuse(f"references a Secret, but {'; '.join(problems)}",
                           where, kind="secret_ref_destination_unconfined")
                for proxy in site.proxies:
                    proxied.setdefault(proxy, []).append(
                        f"{where} is sent through it, past the allowed_domains "
                        f"check on {checked}")
                if site.follows_redirects:
                    redirect_paths.append(where)
            continue
        for proxy in site.proxies:
            proxied.setdefault(proxy, []).append(
                f"the receiver uses the shared Secret '{alias}': the proxy "
                "would see the request to the shared destination")
        field_path = site_path_text(site.path)
        if site.destinations and owner_tenant is not None:
            refuse(f"references the shared Secret '{alias}', which this field "
                   f"may not use on a receiver a tenant owns: the credential goes to "
                   f"{', '.join(ref_path_text(d) for d in site.destinations)}, which this receiver "
                   "names; use the tenant's own Secret ({secret: {key: …}}) (only "
                   "the platform-wide _routing_enforced, without {{tenant}}, may "
                   "use a shared Secret here)",
                   where, kind="secret_ref_shared_confined")
        elif alias not in binding.shared_secrets:
            refuse(f"references the shared Secret '{alias}', which the "
                   "deployment policy's shared_secrets does not list"
                   + ("" if binding.shared_secrets else
                      " (it lists none, or no --policy is given)"),
                   where, kind="secret_ref_shared_unknown")
        elif field_path not in binding.shared_secrets[alias].fields:
            refuse(f"references the shared Secret '{alias}', whose "
                   f"shared_secrets entry does not list {field_path} under "
                   f"fields ({', '.join(sorted(binding.shared_secrets[alias].fields))})",
                   where, kind="secret_ref_shared_field_not_allowed")
        elif site.destinations:
            # #1560 ⑤-b (2-c): the receiver no tenant owns; the credential
            # is confined as a tenant's own would be.
            checked = ", ".join(ref_path_text(d) for d in site.destinations)
            if not allowed_domains:
                problems = ["the deployment policy (--policy) has no allowed_domains "
                            f"to confine the {checked} it is sent to"]
            else:
                problems = [p for d in site.destinations
                            if (p := _destination_problem(receiver_obj, d, allowed_domains))]
            if problems:
                refuse(f"references the shared Secret '{alias}', but {'; '.join(problems)}",
                       where, kind="secret_ref_destination_unconfined")
            if site.follows_redirects:
                redirect_paths.append(where)
    for proxy, reasons in proxied.items():
        if _is_set(ref_value_at(receiver_obj, proxy)):
            refuse(f"sets a proxy the tenant chose, but {'; '.join(dict.fromkeys(reasons))}; "
                   "drop it, or write the value itself",
                   ref_path_text(proxy), kind="secret_ref_proxy_url")
    if redirect_paths and ref_value_at(receiver_obj, ("http_config", "follow_redirects")) is not False:
        refuse(f"is not false, but {', '.join(redirect_paths)} is a credential "
               "from a Secret that Alertmanager sends again on a redirect "
               "(Authorization to a subdomain of the url host, a custom header "
               "to any host), past allowed_domains; set "
               "http_config.follow_redirects: false",
               "http_config.follow_redirects", kind="secret_ref_follow_redirects")
    return out


_URL_SCHEME_RE = re.compile(r"[Hh][Tt][Tt][Pp][Ss]?://")


def _destination_problem(receiver_obj: dict, destination: tuple[str, ...],
                         allowed_domains: list[str] | None) -> str | None:
    """Why a credential sent to *destination* (a receiver path) is not
    confined by *allowed_domains*, or None. The host is read exactly as the
    domain allowlist reads it (``_grar_validate._extract_host``); a URL
    destination (every one but ``HOST_PORT_DESTINATIONS``) must carry an
    http:// or https:// scheme, or that reader takes it for ``host:port``."""
    # Imported here: _grar_validate imports this leaf module.
    from _grar_validate import _extract_host
    dest_text = ref_path_text(destination)
    if not allowed_domains:
        return ("the deployment policy (--policy) has no allowed_domains to "
                f"confine the {dest_text} it is sent to")
    raw = ref_value_at(receiver_obj, destination)
    # #1560 ⑤-b: a destination that is a credential place (webhook url) is
    # never quoted (receiver_value_repr).
    shown = receiver_value_repr(receiver_type(receiver_obj), destination, raw)
    if is_secret_ref(raw):
        return (f"{dest_text}, where it is sent, is itself a Secret reference, "
                "whose host cannot be checked against allowed_domains")
    if not isinstance(raw, str) or raw == "":
        # Missing, null, empty, or not a string: nothing to read a host from.
        return (f"the {dest_text} it is sent to is missing or not a non-empty string (got {shown}), so it "
                "cannot be checked against allowed_domains")
    if destination not in HOST_PORT_DESTINATIONS and _URL_SCHEME_RE.match(raw) is None:
        return (f"{dest_text} {shown}, where it is sent, is not an http:// or "
                "https:// URL, so its host cannot be read for allowed_domains")
    host = _extract_host(raw)
    if host is None:
        return (f"the {dest_text} {shown}, where it is sent, has no host this "
                "generator can read, so it cannot be checked against allowed_domains")
    if not any(fnmatch.fnmatch(host, p) for p in allowed_domains):
        return (f"the {dest_text} host {host!r} is not in the deployment "
                "policy's allowed_domains")
    return None


def render_secret_refs(am_entry: dict, rtype: str, owner_tenant: str | None,
                       binding: SecretBinding) -> None:
    """#1560: rewrite, in place, every Secret reference of Alertmanager entry
    *am_entry* that stands on a ``CREDENTIAL_REF_SITES`` path: a leaf
    becomes ``<file_key>: <root>/<name>/<key>`` beside it and is dropped; a
    list item (a header's ``secrets[i]``) moves to the sibling list
    ``<file_key>`` (that header's ``files``), in order, and a list left
    empty is dropped. Only for references ``secret_binding_findings``
    passed (a shared alias is listed; an own reference has an owner).
    *am_entry* must not share nested values with the tenant's receiver."""
    root = binding.mount_root.rstrip("/")  # "/" → "", so "/<name>/<key>"
    # (id(holder), key) → (holder, key): each list is filtered once, however
    # many references it held (a dict is unhashable, so its identity is the key).
    emptied: dict[tuple[int, str], tuple[dict, str]] = {}
    for site, path in find_credential_refs(am_entry, rtype):
        inner = ref_value_at(am_entry, path)["secret"]
        alias = inner.get("shared")
        name = (binding.shared_secrets[alias].secret if alias is not None
                else own_secret_name(owner_tenant))
        mounted = f"{root}/{name}/{inner['key']}"
        if isinstance(path[-1], int):
            holder = ref_value_at(am_entry, path[:-2])
            holder.setdefault(site.file_key, []).append(mounted)
            emptied[(id(holder), path[-2])] = (holder, path[-2])
        else:
            holder = ref_value_at(am_entry, path[:-1])
            del holder[path[-1]]
            holder[site.file_key] = mounted
    for holder, key in emptied.values():
        if key in holder:
            kept = [v for v in holder[key] if not is_secret_ref(v)]
            if kept:
                holder[key] = kept
            else:
                del holder[key]


def secret_ref_paths(value: object, path: str) -> list[str]:
    """The paths (``<path>.<key>``, ``<path>[<i>]``) of every value inside
    *value*, *value* itself included, that ``is_secret_ref``."""
    if is_secret_ref(value):
        return [path]
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            out += secret_ref_paths(item, f"{path}.{key}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            out += secret_ref_paths(item, f"{path}[{idx}]")
    return out


def build_receiver_config(receiver_obj: dict, tenant: str, *,
                          tenant_id: str | None = None,
                          field: str = "receiver",
                          owner_tenant: object = _OWNER_UNSET,
                          secrets: SecretBinding | None = None,
                          allowed_domains: list[str] | None = None,
                          ) -> tuple[dict | None, list[str]]:
    """Build Alertmanager receiver config from structured receiver object.

    Args:
        receiver_obj: dict with 'type' and type-specific fields.
        tenant: tenant name for error messages.
        tenant_id: #2766 — the tenant, when *tenant* is a context string
            (``alpha-override-0``) or the caller knows it; else None.
        field: #2766 — the receiver's path in the routing (``receiver``,
            ``overrides[0].receiver``, ``_routing_enforced.receiver``).
        owner_tenant: #1560 — the tenant this receiver delivers for, whose
            own Secret (``da-<tenant>``) a ``{secret: {key}}`` reference
            names; None when no tenant owns it. ⛔ Passed explicitly by every
            caller, never inferred from *tenant_id* (a Finding field): a
            receiver with a Secret reference and no *owner_tenant* argument
            is a caller bug and raises ``TypeError``.
        secrets: #1560 — mount root and the policy's ``shared_secrets``
            (None: the default root, no shared Secret).
        allowed_domains: #1560 — the --policy allowlist, read only by
            ``secret_binding_findings`` for every ``CREDENTIAL_REF_SITES`` row
            with destinations (email auth_password → smarthost, the webhook
            http_config leaves → url, the oauth2 ones → token_url and url);
            the domain allowlist itself is the caller's
            ``validate_receiver_domains``.

    Returns:
        (am_config_dict, warnings) where am_config_dict is e.g.
        {"webhook_configs": [{"url": "..."}]} or None on error.
    """
    warnings = []
    binding = secrets if secrets is not None else NO_SECRET_POLICY

    def at(suffix: str = "") -> dict:
        """#2766: the Finding fields of a line about *field* + *suffix*."""
        return {"tenant": tenant_id, "field": f"{field}{suffix}"}

    if not isinstance(receiver_obj, dict):
        warnings.append(skipped_entry_warning(
            f"  WARN: {tenant}: 'receiver' must be an object with 'type', skipping",
            kind="missing_receiver_field", **at()))
        return None, warnings

    rtype = receiver_type(receiver_obj)
    if not rtype:
        warnings.append(skipped_entry_warning(
            f"  WARN: {tenant}: missing required 'receiver.type', skipping",
            kind="missing_receiver_field", **at(".type")))
        return None, warnings

    # Exact match, no case folding or trimming (#2180): the schema (`const`)
    # and the exporter (`receiverspec.Known`) both reject `Email` / ` email`,
    # so normalising here made the generator the only one of the three to
    # accept them.
    if rtype not in RECEIVER_TYPES:
        supported = ", ".join(sorted(RECEIVER_TYPES.keys()))
        warnings.append(skipped_entry_warning(
            f"  WARN: {tenant}: unknown receiver type '{shown_value(rtype, bare=True)}' "
            f"(supported: {supported}), skipping",
            kind="unknown_receiver_type", **at(".type")))
        return None, warnings

    spec = RECEIVER_TYPES[rtype]

    # Validate required fields: presence, type and format by the schema (#2180)
    for req in spec["required"]:
        problem = receiver_required_problem(rtype, receiver_obj, req)
        if problem:
            # #2766: absent → missing, present → invalid.
            warnings.append(skipped_entry_warning(
                f"  WARN: {tenant}: receiver type '{rtype}' {problem}, skipping",
                kind=("missing_receiver_field" if req not in receiver_obj
                      else "invalid_receiver_field"), **at(f".{req}")))
            return None, warnings
    problem = receiver_exactly_one_problem(rtype, receiver_obj)
    if problem:
        sub = _exactly_one_field(spec, receiver_obj)
        warnings.append(skipped_entry_warning(
            f"  WARN: {tenant}: receiver type '{rtype}' {problem}, skipping",
            kind=_exactly_one_kind(spec, receiver_obj),
            **at(f".{sub}" if sub else "")))
        return None, warnings
    # #2295: optional values Alertmanager cannot load (non-boolean
    # send_resolved / require_tls, a malformed http_config) — without this the
    # receiver is written out and only Alertmanager's loader stops it.
    problem = receiver_optional_problem(rtype, receiver_obj)
    if problem:
        warnings.append(skipped_entry_warning(
            f"  WARN: {tenant}: receiver type '{rtype}' {problem}, skipping",
            kind="invalid_receiver_field", **at()))
        return None, warnings

    # #1560: which Secret each reference may name, on the effective receiver.
    if _receiver_secret_refs(receiver_obj):
        if owner_tenant is _OWNER_UNSET:
            raise TypeError("build_receiver_config: a receiver with a Secret "
                            "reference needs owner_tenant= (the tenant it "
                            "delivers for, or None)")
        binding_warnings = secret_binding_findings(
            owner_tenant, receiver_obj, binding, allowed_domains,
            ctx=tenant, tenant_id=tenant_id, field=field)
        if binding_warnings:
            warnings.extend(binding_warnings)
            return None, warnings

    # Build AM config — include required + present optional fields. A copy:
    # #1560's rendering rewrites http_config in place, and the tenant's
    # receiver (shared by every caller holding the merged routing) must not
    # change under it.
    am_entry = {}
    for fld in spec["required"] + spec["optional"]:
        if fld in receiver_obj:
            am_entry[fld] = copy.deepcopy(receiver_obj[fld])

    # Alertmanager's email_config.to is a single STRING (comma-separated for
    # multiple recipients). conf.d authors — and _routing_defaults — conventionally
    # write `to` as a YAML list (one address per line), which is far more readable.
    # Coerce that list into AM's comma-joined string here, at the boundary where we
    # emit AM config: a raw YAML sequence trips Alertmanager's parser with
    # `cannot unmarshal !!seq into string` at config load,
    # a failure the dict-only Python validation never surfaces.
    # ⛔ #2906: no boolean is rewritten here. A boolean field that is a string
    # (`"yes"`, `"true"`) was refused above (receiver_optional_problem), and
    # before that the whole tree (`_grar_parse`'s field-type refusal):
    # turning the word into the boolean it means is the YAML 1.1 leniency
    # ADR-036 §2 forbids the semantic layer.
    if rtype == "email" and isinstance(am_entry.get("to"), list):
        am_entry["to"] = ", ".join(str(addr) for addr in am_entry["to"])

    # #1560: a Secret reference becomes the `<field>_file` Alertmanager reads.
    # #3257 does not refuse this: it judges the tenant's input, not this output.
    render_secret_refs(am_entry, rtype, owner_tenant, binding)

    # #1560: whatever is still shaped like a reference after that is on a
    # field with no `*_file` form (an http_config leaf CREDENTIAL_REF_SITES
    # does not list, email headers) and would reach Alertmanager as written. Judged on the OUTPUT, so every
    # receiver slot and every field — today's and one added later — is
    # covered by this one check rather than by a rule per field.
    leftover = secret_ref_paths(am_entry, field)
    if leftover:
        takes = ", ".join(dict.fromkeys(
            ref_path_text(s.path).replace(".*", ".<name>").replace(".[]", "[i]")
            for s in CREDENTIAL_REF_SITES if s.rtype == rtype)) or "none"
        for path in leftover:
            warnings.append(skipped_entry_warning(
                f"  WARN: {tenant}: receiver type '{rtype}' field '{path}' holds a "
                f"Secret reference, which only these fields take: {takes}; skipping",
                kind="secret_ref_unsupported_field", tenant=tenant_id, field=path))
        return None, warnings

    return {spec["am_key"]: [am_entry]}, warnings
