"""Configuration loading + parsing for generate_alertmanager_routes.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  _parse_platform_config(...)   → defaults / _routing_defaults / profiles / policies
  _parse_tenant_overrides(...)  → per-tenant _routing / _severity_dedup / _metadata
  _parse_config_files(dir)      → walk YAML files → raw parsed dict
  _merge_tenant_routing(...)    → 4-layer merge (defaults → profile → tenant)
  load_tenant_tree(dir)         → orchestrate the full pipeline (public entry),
                                  with the file accounting (#1460)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_validation import routing_disabled as _is_disabled  # noqa: E402  (#3141: the generator's own reading)
from _lib_validation import is_valid_tenant_id  # noqa: E402  (#2341 R8)
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping; #3276)
from _lib_paths import classify_path  # noqa: E402  (#3276)
# ADR-036 step 4 (#2906): every conf.d read on this path is `da-guard
# parse-tree`'s, through one reader per run (and, in the generator container,
# its content-addressed cache).
import _lib_parse_cache  # noqa: E402
from _lib_parse_tree import (  # noqa: E402
    RESERVED_CLASSES, FileUnreadable, ParseTreeError, classify_names)
from _lib_confd import (  # noqa: E402
    duplicate_declarations,
    list_config_tree,
    tenant_set_unknown_note,
    overlay_platform_tenants,
    tenant_declarations,
    unusable_reason,
)

from _grar_merge import (  # noqa: E402
    ROOT_LEVEL,
    Finding,
    chain_levels,
    domain_policy_levels,
    merge_routing_with_defaults,
    policy_reaches,
    resolve_routing_defaults,
    skipped_entry_warning,
    unclassified,
    visible_routing_profiles,
)
from _grar_validate import (  # noqa: E402
    FIELD_TYPE_ERROR_PREFIX,
    POLICY_ERROR_PREFIX,
    ROUTING_TREE_ERROR_PREFIX,
    domain_policy_problems,
    receiver_bool_type_errors,
    routing_block_file_reference_errors,
    routing_block_group_by_type_errors,
    value_type_text,
    _validate_profile_refs,
    check_domain_policies,
    check_policy_scope,
    group_by_problem_text,
    ignored_routing_keys,
    invalid_tenant_id_text,
    unknown_routing_keys,
    override_redundancies,
    routing_defaults_not_mapping_text,
    routing_group_by_invalid,
    routing_not_mapping_text,
    routing_not_mapping_warning,
    routing_values_not_string,
    validate_tenant_keys,
    value_not_string_message,
)
# #2503: the strict twin of the enforced routes' group_by render.
from _grar_routes import (  # noqa: E402
    enforced_group_by_problems,
    enforced_match_invalid,
    enforced_match_invalid_text,
    enforced_route_tenants,
)

# ADR-007 --strict fail-open closure: filenames whose content carries the
# domain policies. If such a file is unparseable, or a domain_policies
# block sits in a differently named file, the policies silently vanish —
# strict mode must surface that instead of staying green.
_POLICY_FILENAMES = ("_domain_policy.yaml", "_domain_policy.yml")

# ADR-036 step 4 (#2906): the routing reader's conversion of a conf.d file's
# parse-tree content — tenant ids are keys, so source text; a domain policy's
# `tenants:` list as source text too, so `[010]` meets tenant "010". A
# repeated key, a null key, several documents are parse-tree's `unreadable`.
# One mode for every file this reader opens.
ROUTING_READ = _lib_parse_cache.TENANT_LISTS_AS_TEXT

# What to do about a file parse-tree cannot read, by its kind
# (pkg/parsetree Kinds); any other kind gets the generic remedy.
_UNREADABLE_REMEDY = {
    "syntax_error": "repair the YAML syntax in {f}",
    "invalid_utf8": "re-save {f} as UTF-8",
    "duplicate_key": "write each key once per mapping in {f}",
    "multiple_documents": "keep one YAML document in {f} (no `---` after the first)",
    "null_key": "give every key in {f} a name (no `~:`, `null:` or empty key)",
    "top_level_not_mapping": ("{f}'s top level must be a mapping — the keys this reader "
                              "looks for (tenants, defaults, domain_policies, _routing_*) "
                              "all live at its top level"),
}


def unreadable_reason(e: FileUnreadable) -> str:
    """The reason clause of a file parse-tree could not read: its kind (the
    closed-list code, ADR-036 §2) and parse-tree's message."""
    return f"could not be read ({e.kind}): {' '.join(e.message.split())}"


def unreadable_remedy(e: FileUnreadable, fname: str) -> str:
    """What the author does about it, by kind."""
    return _UNREADABLE_REMEDY.get(
        e.kind, "rewrite {f} so da-guard parse-tree can read it").format(f=fname)


def parse_tree_failure_exit(e: ParseTreeError, config_dir: str) -> "None":
    """No usable `da-guard parse-tree` (missing, failing, a document this
    tool cannot read): nothing of the tree was read, so exit 2 — the caller
    error every tool gives a missing or failing da-guard."""
    print(f"ERROR: {safe_label(config_dir)}: cannot read the conf.d through "
          f"`da-guard parse-tree` (ADR-036): {' '.join(str(e).split())}", file=sys.stderr)
    sys.exit(EXIT_CALLER_ERROR)


# #2981 (ADR-036 §3): the conf.d entries the generator service took from
# git objects whose tree mode is not a regular file's (100644 / 100755) — a
# symlink (120000), a gitlink (160000) or anything else — as {path relative
# to the conf.d root: mode}. The service does not write them into the tree
# it judges and sets them here instead, so each becomes a dropped file named
# for what it is (not_regular_file), never whatever its link text happens to
# parse as. Empty for every read of a real directory: a filesystem reader
# follows a file symlink (a Kubernetes ConfigMap mount is made of them).
_GIT_IRREGULAR: dict[str, str] = {}

_GIT_MODE_NAMES = {"120000": "a symlink", "160000": "a gitlink (submodule)"}


def set_git_irregular_entries(entries: "dict[str, str] | None") -> None:
    """Install (or, with None, clear) the git entries described above."""
    _GIT_IRREGULAR.clear()
    if entries:
        _GIT_IRREGULAR.update(entries)


def git_irregular_reason(mode: str) -> str:
    """The reason clause of a not-regular git entry (one wording, here)."""
    what = _GIT_MODE_NAMES.get(mode, "not a regular file")
    return (f"is not a regular file (not_regular_file): git tree mode {mode}, "
            f"{what} — read from git objects, conf.d entries must be "
            f"100644 / 100755 files")


def _drop_unusable_policy(fname: str, reason: str, remedy: str, result: dict,
                          warning: str | None = None,
                          field: str | None = None) -> None:
    """Record content this reader had to drop, then warn — in one place.

    ⛔ There is more than one way for the domain policies to fall on the
    floor: the file does not decode, it does not parse, it parses into
    something that is not a mapping, or it is a fine mapping whose
    ``domain_policies:`` value is not one. **Every one of them ends with
    zero policies enforced**, which is precisely what ADR-007 ``--strict``
    exists to refuse to do quietly — so every one of them has to leave a
    record here. Before #1448 only the "does not parse" branch did.
    #3116: the record is a ``domain_policy_unusable`` field-type error, so
    the whole tree is refused in every mode, not under ``--strict`` only.

    Measured on ``generate-routes --validate --strict`` over an otherwise
    clean conf.d, with only ``_domain_policy.yaml`` varying (a routing
    config that the policy would reject; ``cd4dbd05`` is this branch's
    base)::

        _domain_policy.yaml            base rc   this branch
        absent (control)                    0        0   ← rc=0 is reachable
        valid, and violated                 1        1
        syntax error                        1        1   ← the loud sibling
        whole file is a list / scalar   1 (crash)    1
        not UTF-8                       1 (crash)    1
        `domain_policies:` is a list        0 ← !     1
        `domain_policies:` is null          0 ← !     1

    #3116: the null row no longer comes here — the schema allows a null
    ``domain_policies:`` ("no policy configured"), so the reader takes it as
    an empty mapping (rc 0, nothing to enforce).

    ⛔ The two rows marked ← ! printed ``OK: all configs valid``, on the
    base and on every release before it. They are **not** a regression this
    branch introduced; the crash rows are what this branch had to convert,
    and converting a crash into a skip without this bookkeeping is exactly
    how a crash becomes a silent pass. (An abandoned first attempt at
    #1448 did precisely that — it is not in this history, so do not go
    looking for the silent-pass state in ``git log``.)

    ⚠️ One shape is deliberately still not recorded, unchanged from before
    #1448: a policy file that parses to *nothing* — ``None`` (empty file,
    or every entry commented out) or an empty mapping ``{}``. An
    intentionally empty policy file is a legal config today, so closing
    that is a behaviour change rather than a fail-open fix, and it is
    tracked separately — see the follow-up on #1448.

    ⚠️ Two more readers used to disagree with this one; both are closed:

    * **Nested files** — closed by #2326: this loop walks the tree now, so
      ``conf.d/team-a/beta.yaml`` with a list at its top level is booked here
      too (it used to be ``validate-config`` rc=1 against
      ``generate-routes --validate --strict`` rc=0).
    * **The write plane** — closed by #2903: ``tenant-api`` no longer reads
      the policy file itself (its ``internal/policy`` let every write
      through for a null ``domain_policies:``); a write is judged by this
      generator, so this function's ``--strict`` ERROR is its verdict too.

    ⛔ "Nothing" means exactly those two. It did NOT for one round: the
    caller's ``if not data: continue`` ran *before* the shape check, so
    every falsy non-mapping (``[]``, ``0``, ``false``) fell through the gap
    between this ⚠️ and the table above — the table said such a file exits
    1, the ⚠️ said only "nothing" is exempt, and both were wrong for that
    slice. Measured: a body of ``[]`` left ``--strict`` at exit 0 with zero
    policies enforced while ``validate-config`` called the same file a
    non-mapping and exited 1.

    ⛔ *reason* and *remedy* are written by the branch that knows why the
    content dropped, not by the consumer that renders them. The consumer
    used to append one fixed sentence — "fix: repair the YAML syntax" —
    which is the right instruction for exactly one of these causes and
    sends the reader of any other one hunting for a syntax error in a file
    whose syntax is fine.
    """
    # #2326: `fname` is the path relative to the conf.d root (a policy file
    # may sit in a subdirectory), so the NAME is what decides the kind.
    if os.path.basename(fname) in _POLICY_FILENAMES:
        detail = f"{fname}: {reason} — fix: {remedy}"
        # setdefault, matching the sibling below: this helper is also
        # reachable from _parse_platform_config, which tests call with a
        # hand-built result dict.
        result.setdefault("policy_file_errors", []).append(
            detail.replace("\n", " "))
        # #3116: a policy file that cannot be used is `domain_policy_unusable`
        # like any other policy problem — the whole tree is refused in every
        # mode (it used to block under --strict only; a lenient run dropped
        # every policy of the file at rc 0). *field* is `domain_policies`
        # when that block is not a mapping, None when the file is unusable.
        _field_type_error(
            result, "domain_policy_unusable",
            f"domain policy file could not be used, so no policy in it is "
            f"enforced: {detail.replace(chr(10), ' ')}",
            file=fname, field=field)
    # #1538: escape at the PRINT, not in `detail` above — `detail` lands in
    # result["policy_file_errors"] -> schema_warnings -> validate-config
    # --json, whose bytes must not change (json.dumps already escapes).
    print(safe_label(warning or f"  WARN: skip {fname}: {reason}"),
          file=sys.stderr)


def _drop_unreadable_file(fname: str, reason: str, remedy: str, result: dict,
                          warning: str | None = None) -> None:
    """Book a WHOLE FILE this reader could not use, then drop it.

    #1460: ``_drop_unusable_policy`` above records the failure only when the
    file is a policy file — a tenant file that did not parse left one stderr
    WARN and nothing else, so ``--validate`` went on to print
    ``OK: all configs valid`` at rc=0 over a tree that was one tenant
    smaller. Measured: with ``--strict`` too, and inside the shipped
    pre-commit hook (which prints hook output only on failure) not even the
    WARN reached the screen.

    Every branch that drops a whole file — unreadable path, decode error,
    parse error, any other read failure, a top level that is not a mapping —
    goes through here, so ``files_skipped`` is the complete list and
    ``tenant_file_errors`` is its non-policy subset. The consumer in
    ``generate_alertmanager_routes.main`` derives its "read N, skipped M"
    line and its refusal from these records, never from the WARN text.

    Policy files keep their own treatment through the delegate (#3116: a
    ``domain_policy_unusable`` field-type error, refused in every mode);
    they are booked in ``files_skipped`` (a skipped file is a skipped file)
    but NOT in ``tenant_file_errors``.
    """
    result.setdefault("files_skipped", []).append((fname, reason))
    if os.path.basename(fname) not in _POLICY_FILENAMES:
        result.setdefault("tenant_file_errors", []).append(
            (fname, f"{reason} — fix: {remedy}".replace("\n", " ")))
    _drop_unusable_policy(fname, reason, remedy, result, warning=warning)


def _name_class(fname: str) -> str:
    """Go's class of *fname*'s base name (names mode, `classify_names`): for
    a caller that holds a name and no entry of a tree (#3206)."""
    base = os.path.basename(fname)
    return classify_names([base])[base].cls


def _parse_platform_config(data: dict, fname: str, result: dict, *,
                           cls: "str | None" = None) -> None:
    """Extract platform-level config from a _ prefixed YAML file.

    Handles: defaults keys, _routing_defaults, _routing_enforced,
    routing_profiles (ADR-007), domain_policies (ADR-007).
    Mutates *result* in place.

    ⛔ TWO QUESTIONS, as in the exporter's `applyBoundaryRules` since #1676:
    "is this a platform (`_`-prefixed) file" gates the routing keys, and "is
    this the defaults CARRIER" gates `defaults:` / `optional_overrides:`.
    Before, any `_` file (and, for `defaults:`, any file at all) widened the
    key universe validate_tenant_keys checks against, while the exporter
    serves those keys only from the carrier — so a root `_profiles.yaml`
    carrying `defaults:` passed here and was stripped there.
    Both are Go's class of the file (#3206): *cls* is the entry's (the read
    loop passes it), else the name's in names mode (`classify_names`) — no
    name rule here. Which of SEVERAL carriers is read is the caller's job —
    `_parse_config_files` never hands this function an unselected spelling
    (#1674).
    """
    if cls is None:
        cls = _name_class(fname)
    is_defaults_file = cls in RESERVED_CLASSES   # platform file (routing keys)
    is_carrier = cls == "defaults"
    # #1538: display-only alias. `fname` itself stays raw — it is used for
    # lookups (_POLICY_FILENAMES) and stored into `result`, which feeds --json.
    _f = safe_label(fname)
    if is_defaults_file:
        _record_unknown_routing_keys(result, data, layer="platform", where="top level", file=fname)

    # Collect defaults keys for schema validation — from the carrier only.
    if isinstance(data.get("defaults"), dict):
        if is_carrier:
            result["defaults_keys"].update(data["defaults"].keys())
        elif data["defaults"]:
            print(f"  WARN: defaults in {_f} ignored (not a defaults carrier; "
                  "only _defaults.yaml / _defaults.yml is)", file=sys.stderr)

    # #1189 / TRK-337: keys the platform RECOGNISES but supplies no value for
    # (the runtime half of the registry's `tier: optional_overrides`). They
    # widen the membership universe validate_tenant_keys checks against, so a
    # tenant can finally set them — which is precisely why they are honoured
    # ONLY from _ prefixed files. A tenant naming its own keys here would be
    # self-authorising past this very check; Go strips it from tenant-owned
    # files for the same reason (applyBoundaryRules).
    if "optional_overrides" in data:
        if is_carrier:
            raw = data["optional_overrides"]
            if isinstance(raw, list):
                # ⚠️ Non-string entries are dropped LOUDLY. Go decodes this
                # field straight into []string, so the same bytes that cost
                # Python one list entry cost Go the entire file: a mapping
                # element (a stray trailing colon is enough) is an unmarshal
                # error, and the whole platform block is skipped — taking
                # `defaults:` with it. Dropping quietly here would leave CI
                # green on the config that empties the write plane.
                bad = [k for k in raw if not isinstance(k, str)]
                if bad:
                    print(f"  WARN: optional_overrides in {_f} has "
                          f"{len(bad)} non-string entr{'y' if len(bad) == 1 else 'ies'} "
                          f"({bad!r}) — ignored here, but the Go exporter fails "
                          "the whole file on them", file=sys.stderr)
                result["optional_override_keys"].update(
                    k for k in raw if isinstance(k, str))
            elif raw is not None:
                print(f"  WARN: optional_overrides in {_f} must be a list, "
                      "ignoring", file=sys.stderr)
        elif data["optional_overrides"]:
            print(f"  WARN: optional_overrides in {_f} ignored "
                  "(platform-scoped; only allowed in the defaults carrier, "
                  "_defaults.yaml / _defaults.yml)",
                  file=sys.stderr)

    # Extract _routing_defaults (only from _ prefixed files)
    if "_routing_defaults" in data:
        if is_defaults_file:
            rd = data["_routing_defaults"]
            # #2412: a value that is neither a mapping nor null (a list, a
            # string, a number, a boolean) is named and contributes nothing —
            # the subdirectory reader's verdict (`_parse_nested_config`). It
            # is still stored, as None: a later root file that carries the
            # key replaces an earlier one WHOLE, whatever its value. Before,
            # the value reached `resolve_routing_defaults`' `dict(root)` and
            # the generator died with a traceback at rc 1. Null stays silent.
            # #2341 R5: and that is blocking (`WARN … skipping`; --strict
            # ERROR) — every tenant without its own `_routing` lost its
            # receiver in silence.
            if rd is not None and not isinstance(rd, dict):
                _routing_defaults_not_mapping(fname, rd, result)
                rd = None
            # #2245: ADR-007 `routes` belong to a routing profile or the
            # tenant, never to the platform-wide defaults (every tenant would
            # inherit the escalation). Dropped HERE, before any merge, so
            # neither the generator nor explain_route renders it; recorded
            # as a `WARN … skipping` line (→ schema_warnings, blocking under
            # `--validate`), like a non-boolean `_routing_enforced.enabled`.
            if isinstance(rd, dict) and "routes" in rd:
                _record_file_reference_errors(  # #3257: dropped, still judged
                    result, {"routes": rd["routes"]}, where="_routing_defaults",
                    field_prefix="_routing_defaults.", file=fname)
                rd = {k: v for k, v in rd.items() if k != "routes"}
                result.setdefault("routing_defaults_errors", []).append(
                    skipped_entry_warning(f"  WARN: _routing_defaults in {fname}: 'routes' is not "
                                          "supported here (define it in a routing profile or the "
                                          "tenant's _routing), skipping",
                                          kind="routing_defaults_routes_ignored", file=fname,
                                          field="_routing_defaults.routes"))
            _record_ignored_keys(result, rd, block="routing_defaults",
                                 where="_routing_defaults",
                                 field_prefix="_routing_defaults.", file=fname)
            result["routing_defaults"] = rd
        else:
            print(f"  WARN: _routing_defaults in {_f} ignored "
                  "(only allowed in _ prefixed files)", file=sys.stderr)

    # v1.7.0: Extract _routing_enforced (only from _ prefixed files)
    if "_routing_enforced" in data:
        if is_defaults_file:
            raw = data["_routing_enforced"]
            # ⛔ #2164: `is True`, never truthiness. PyYAML reads an unquoted
            # `n` / `y` as the STRING 'n' / 'y', and `'n'` is truthy, so
            # `enabled: n` used to switch NOC routing ON at rc 0. A missing
            # key stays "disabled".
            # ⛔ #2906: anything that is not a boolean — `"true"` and `"yes"`
            # included, and null — refuses the WHOLE tree in every mode
            # (`field_type_errors`). It used to be a `WARN … skipping` line
            # that render mode printed and then shipped a config with no
            # NOC route at rc 0; once the generator reads yaml.v3 (ADR-036
            # step 4) an unquoted `yes` is such a string too.
            enabled = raw.get("enabled", False) if isinstance(raw, dict) else None
            # #2960: `enabeld: true` leaves NOC routing off — named, as every
            # key the generator does not read, whatever `enabled` says.
            _record_ignored_keys(result, raw, block="routing_enforced",
                                 where="_routing_enforced",
                                 field_prefix="_routing_enforced.", file=fname)
            if isinstance(raw, dict) and enabled is True and enforced_match_invalid(raw):
                # #2989: a `match` that is not a list (`{severity: critical}`)
                # used to be dropped, so the NOC route took every alert. Not
                # enabled, as for a non-boolean `enabled`: an earlier root
                # file's block stays, and --validate fails on the line.
                result.setdefault("enforced_errors", []).append(
                    skipped_entry_warning(
                        "  WARN: " + enforced_match_invalid_text(f" in {fname}", raw["match"]),
                        kind="routing_enforced_match_invalid",
                        file=fname, field="_routing_enforced.match"))
            elif isinstance(raw, dict) and enabled is True:
                result["enforced_routing"] = raw
            elif isinstance(raw, dict) and enabled is False:
                pass  # explicitly disabled → None
            elif isinstance(raw, dict):
                _field_type_error(
                    result, "routing_enforced_enabled_invalid",
                    f"_routing_enforced in {fname}: 'enabled' must be a boolean "
                    f"(true / false, unquoted), got {value_type_text(enabled)}",
                    file=fname, field="_routing_enforced.enabled")
            else:
                print(f"  WARN: _routing_enforced in {_f} must be a dict "
                      "with 'enabled: true', ignoring", file=sys.stderr)
        else:
            print(f"  WARN: _routing_enforced in {_f} ignored "
                  "(only allowed in _ prefixed files)", file=sys.stderr)

    _parse_profiles_and_policies(data, fname, ROOT_LEVEL, result)


def _record_ignored_keys(result: dict, obj: object, **kw) -> None:
    """#2960: name the keys *obj* carries that the generator does not read
    (`ignored_routing_keys`; → schema_warnings, blocking under `--validate`).

    #2906: called for every routing block this reader reads (a tenant's
    `_routing`, `_routing_defaults` at each level, `_routing_enforced`, each
    routing profile), so the receivers' boolean fields are judged here too
    (`_record_receiver_bool_errors`)."""
    result.setdefault("ignored_routing_keys", []).extend(ignored_routing_keys(obj, **kw))
    _record_receiver_bool_errors(result, obj, **kw)


@dataclass(frozen=True)
class FieldTypeError:
    """One #2906 field-type finding: a boolean / enum field of the wrong type
    or with an unknown value. ``kind`` is a ``FINDING_KINDS`` key; ``file``
    is root-relative; ``message`` names the place, the rule and the value."""
    kind: str
    message: str
    file: str | None = None
    tenant: str | None = None
    field: str | None = None
    policy: str | None = None


def _field_type_error(result: dict, kind: str, message: str, *,
                      file: str | None = None, tenant: str | None = None,
                      field: str | None = None, policy: str | None = None) -> None:
    """Record one #2906 field-type finding. The generator refuses the WHOLE
    tree on any of them, in every mode (`_refuse_field_type_errors`, rc 1).
    setdefault: tests call the parse helpers with hand-built result dicts."""
    result.setdefault("field_type_errors", []).append(
        FieldTypeError(kind, message, file=file, tenant=tenant, field=field,
                       policy=policy))


def field_type_findings(errors: "list[FieldTypeError]", *,
                        prefix: str = "  ") -> list[Finding]:
    """#2906: the warning-stream line of each field-type finding
    (``FIELD_TYPE_ERROR_PREFIX``, blocking in every mode). *prefix* is the
    indentation: the refusal (``field_type_errors_refusal``) prints the same
    lines under its own framing line."""
    return [Finding(f"{prefix}{FIELD_TYPE_ERROR_PREFIX} {e.message}",
                    kind=e.kind, blocks="always", tenant=e.tenant,
                    policy=e.policy, file=e.file, field=e.field)
            for e in errors]


def _record_receiver_bool_errors(result: dict, obj: object, *, block: str,
                                 where: str, field_prefix: str = "",
                                 tenant: str | None = None,
                                 file: str | None = None) -> None:
    """#2906: a receiver boolean (`send_resolved`, `require_tls`,
    `http_config.proxy_from_environment`) that is not a boolean or null —
    `"yes"` / `"true"` used to be written out as the boolean they spell.
    #3257: and a file-reference key (`*_file` / `*_ref` / `files`) anywhere
    in the routing block — Alertmanager would load it from its own container."""
    for fld, message in [
            *receiver_bool_type_errors(
                obj, block=block, where=where, field_prefix=field_prefix),
            *routing_block_file_reference_errors(
                obj, block=block, where=where, field_prefix=field_prefix)]:
        _field_type_error(result, "invalid_receiver_field",
                          f"{file}: {message}" if file else message,
                          file=file, tenant=tenant, field=fld)
    # #3284: a `group_by` that is not a list (`group_by: alertname`) used to
    # render no group_by at all, rc 0 in every mode; refused the same way.
    for fld, message in routing_block_group_by_type_errors(
            obj, block=block, where=where, field_prefix=field_prefix):
        _field_type_error(result, "routing_group_by_invalid",
                          f"{file}: {message}" if file else message,
                          file=file, tenant=tenant, field=fld)


def _record_file_reference_errors(result: dict, obj: object, *, where: str,
                                  field_prefix: str, file: str) -> None:
    """#3257: the file-reference keys of routing this reader drops or never
    reads (`_routing_defaults.routes`, routing under `defaults:` or a
    threshold profile): rendered nowhere today, refused like everywhere else."""
    for fld, message in routing_block_file_reference_errors(
            obj, block="unread", where=where, field_prefix=field_prefix):
        _field_type_error(result, "invalid_receiver_field", f"{file}: {message}",
                          file=file, field=fld)


def _record_unknown_routing_keys(result: dict, obj: object, **kw) -> None:
    """#2983: name the `_routing`-prefixed keys *obj* carries that the
    generator does not read in that layer (`unknown_routing_keys`; same
    channel and verdict as `_record_ignored_keys`)."""
    result.setdefault("ignored_routing_keys", []).extend(unknown_routing_keys(obj, **kw))


def _routing_defaults_not_mapping(fname: str, value: object,
                                  result: dict) -> None:
    """#2341 R5: a ``_routing_defaults`` (root or a subdirectory's carrier)
    that is neither a mapping nor null. The level contributes nothing (as
    since #2412) and the finding is blocking: a ``WARN … skipping`` line in
    every mode, an ERROR under ``--strict`` (``load_tenant_tree``)."""
    result.setdefault("routing_defaults_errors", []).append(
        skipped_entry_warning(f"  WARN: {routing_defaults_not_mapping_text(fname, value)}, skipping",
                              kind="routing_defaults_not_mapping", file=fname,
                              field="_routing_defaults"))
    result.setdefault("routing_defaults_not_mapping", []).append(
        (fname, value))


def _tree_problem(result: dict, kind: str, fname: str, field: str,
                  message: str) -> None:
    """Record one #2326 routing-tree finding: ``(kind, file, field, message)``.

    ``kind`` is the finding kind the parity matrix
    (tests/shared/routing_policy_parity_matrix.json) spells. setdefault: tests call the parse helpers
    with hand-built result dicts.
    """
    result.setdefault("routing_tree_problems", []).append(
        (kind, fname, field, message))


# #2326 kinds that make the whole tree unusable for the routing plane: the
# generator exits EXIT_CALLER_ERROR on any of them, in every mode, before
# anything is rendered. The subtree-policy finding
# (`domain_policy_out_of_scope`) is not here — it is ERROR under --strict and
# WARN otherwise, like every other domain-policy finding (ADR-007 (d)).
# `duplicate_tenant` is recorded (the parity matrix pins it) but is
# not here either: #2315 owns that refusal — `_refuse_duplicate_tenants`,
# EXIT_VIOLATION, validate-config's `tenant_uniqueness` scan — and it runs
# first, so a second spelling of the same verdict would only disagree on rc.
BLOCKING_TREE_KINDS = frozenset({
    "routing_enforced_below_root",
    "routing_defaults_null_below_root",
    "routing_profile_duplicate",
})


def _record_profiles(fname: str, level: str, profiles: dict,
                     result: dict) -> None:
    """Add one file's routing profiles at directory *level* (#2326 (c)).

    A profile NAME is unique across the whole tree: a second definition —
    another directory, or `.yml` beside `.yaml` in the same one — is a
    blocking finding and does NOT replace the first (before #2326 the later
    root file silently won).
    """
    origin = result.setdefault("routing_profile_origin", {})
    by_dir = result.setdefault("routing_profiles_by_dir", {})
    flat = result.setdefault("routing_profiles", {})
    for name, cfg in profiles.items():
        first = origin.get(name)
        if first is not None:
            _tree_problem(
                result, "routing_profile_duplicate", fname,
                f"routing_profiles.{name}",
                f"routing profile '{name}' is defined in both {first} and "
                f"{fname} — a profile name must be unique across the whole "
                f"conf.d tree (ADR-007 amendment 2026-09-28); rename or "
                f"remove one of them")
            continue
        origin[name] = fname
        by_dir.setdefault(level, {})[name] = cfg
        flat[name] = cfg
        _record_ignored_keys(result, cfg, block="routing",
                             where=f"routing profile '{name}'",
                             field_prefix=f"routing_profiles.{name}.", file=fname)


def _refuse_nested_enforced(fname: str, result: dict) -> None:
    """#2326 (b): `_routing_enforced` in a file below the root is blocking."""
    _tree_problem(
        result, "routing_enforced_below_root", fname, "_routing_enforced",
        f"{fname}: _routing_enforced is read only at the conf.d root — a "
        f"NOC route scoped to one subtree is not supported (ADR-017 "
        f"Decision 9); move it to a root platform file or "
        f"delete it")


def _unselected_carrier(fname: str, chosen: str, level: str,
                        result: dict) -> None:
    """#2906: a defaults carrier the directory's selection passed over
    (da-guard parse-tree: `not_read` reason `unselected_defaults_carrier`;
    *chosen* is its `defaults_carriers` entry for the directory). No plane reads it, so whatever
    it says is not in force: a `WARN … skipping` line, blocking under
    `--validate` like every other line for config that is written and not
    rendered. The file is never opened here — its content cannot change
    the verdict (before #2906 a `_routing_enforced` in one below the root
    was read out of it and refused as `routing_enforced_below_root`)."""
    where = ("the conf.d root" if level == ROOT_LEVEL
             else f"directory {level}/")
    result.setdefault("unselected_carriers", []).append(skipped_entry_warning(
        f"  WARN: {fname} is not read: {where} has more than one defaults "
        f"carrier and only {chosen} is read, on every plane (#1674); merge "
        f"them into one file, skipping",
        kind="unselected_defaults_carrier", file=fname))


# #2904: the routing keys this reader reads at the top level of a defaults
# CARRIER (the root's, and since #2326 a subdirectory's `_routing_defaults`;
# `_routing_enforced` below the root is refused by `_refuse_nested_enforced`).
_CARRIER_TOP_LEVEL_ROUTING_KEYS = frozenset({"_routing_defaults", "_routing_enforced"})


def _is_routing_key(key: object) -> bool:
    """``_routing`` or ``_routing_<…>`` — the keys this reader reads by name
    (Go: ``config.IsRoutingKey``). Narrower than the reserved ``_routing``
    prefix: ``_routingProfile`` is no routing key."""
    return isinstance(key, str) and (key == "_routing" or key.startswith("_routing_"))


def _is_routing_layer_key(key: object) -> bool:
    """``_severity_dedup`` or any key starting with ``_routing`` (Go:
    ``config.IsRoutingLayerKey``; #2914, why: ``routing_in_unread_location``
    in docs/cli-reference.md#guard)."""
    return isinstance(key, str) and (key == "_severity_dedup" or key.startswith("_routing"))


def _unread_routing_fix(key: str) -> str:
    """Where *key* belongs instead (the remedy of one unread-location line)."""
    if key == "_routing":
        return ("move it to `_routing_defaults` at the top level of a conf.d root "
                "platform file (every tenant), a routing profile in "
                "`_routing_profiles.yaml` referenced by `_routing_profile` (a group), "
                "or the tenant's own file (one tenant)")
    if key == "_routing_profile":
        return ("set `_routing_profile` in the tenant's own file (or its root "
                "platform `tenants:` entry)")
    if key == "_severity_dedup":  # only reported in a threshold profile (#2914)
        return ("move `_severity_dedup` into each tenant's own file (or its root "
                "platform `tenants:` entry), then delete it from the profile")
    if key == "_routing_enforced":
        return ("move `_routing_enforced` to the top level of a conf.d ROOT platform "
                "file (not under `defaults:`, not below the root)")
    if key in _CARRIER_TOP_LEVEL_ROUTING_KEYS:
        return f"move `{key}` to the top level of this defaults file (not under `defaults:`)"
    return ("this reader reads no such key here; move the routing to "
            "`_routing_defaults`, `_routing_profiles.yaml`, or the tenant's own `_routing`")


def _unread_routing(data: dict, fname: str, *, is_carrier: bool,
                    is_root_platform: bool, result: dict) -> None:
    """#2904 (#2291): routing keys written where this reader never reads them.

    The exporter merges such a key into every tenant's effective config, so a
    reader of the effective config sees routing no route is rendered from.
    Two places, reported as ``routing_in_unread_location``:

    1. inside the ``defaults:`` mapping of the directory's selected defaults
       carrier (root or below) — field ``defaults.<key>``;
    2. inside a threshold profile of a root platform file's ``profiles:``
       mapping (``_profile``; routing profiles are ``routing_profiles:``) —
       field ``profiles.<name>.<key>``, every key ``_is_routing_layer_key``
       accepts; a profile that is not a mapping carries none.

    Each is a ``WARN … skipping`` line (blocking under ``--validate``, as the
    #2326 F3 shape of this kind is) and a routing-tree record for the parity
    matrix. Keys in name order, profiles in name order.
    """
    # A `_routing*` key at the TOP level of a `_` file — a carrier with no
    # `defaults:` mapping included — is routing_key_ignored instead (#2983,
    # _record_unknown_routing_keys), so each key is named once.
    found: list[tuple[str, str, str]] = []   # (field, key, why)
    if is_carrier:
        block = data.get("defaults")
        if isinstance(block, dict):
            found.extend(
                (f"defaults.{k}", k,
                 "a defaults block is merged into every tenant's effective "
                 "config, but the route generator never reads routing from it")
                for k in sorted(k for k in block if _is_routing_key(k)))
    profiles = data.get("profiles") if is_root_platform else None
    if isinstance(profiles, dict):
        for name in sorted((n for n in profiles if isinstance(n, str))):
            body = profiles[name]
            if not isinstance(body, dict):
                continue
            found.extend(
                (f"profiles.{name}.{k}", k,
                 f"a threshold profile fills its keys into the tenants on "
                 f"`_profile: {name}`, but the route generator never reads "
                 f"routing or severity dedup from it, and the exporter does "
                 f"not apply it either (#2914)")
                for k in sorted(k for k in body if _is_routing_layer_key(k)))
    # #3257: routing written here is rendered nowhere, but a file-reference
    # key in it refuses the tree as it does in a routing block that is read.
    unread = []
    if is_carrier and isinstance(data.get("defaults"), dict):
        unread.append(("defaults.", data["defaults"], _is_routing_key))
    if isinstance(profiles, dict):
        unread.extend((f"profiles.{n}.", profiles[n], _is_routing_layer_key)
                      for n in sorted(n for n in profiles if isinstance(n, str))
                      if isinstance(profiles[n], dict))
    for prefix, holder, is_key in unread:
        for k in sorted(k for k in holder if is_key(k)):
            _record_file_reference_errors(result, holder[k], where=f"{prefix}{k}",
                                          field_prefix=f"{prefix}{k}.", file=fname)
    for fld, key, why in found:
        text = f"{fname}: {fld} is not rendered — {why}; {_unread_routing_fix(key)}"
        result.setdefault("routing_defaults_errors", []).append(
            skipped_entry_warning(f"  WARN: {text}, skipping",
                                  kind="routing_in_unread_location", file=fname,
                                  field=fld))
        _tree_problem(result, "routing_in_unread_location", fname, fld, text)


def _parse_nested_config(data: dict, fname: str, level: str,
                         is_level_carrier: bool, result: dict, *,
                         reserved: "bool | None" = None) -> None:
    """The routing-plane keys of one file BELOW the conf.d root (#2326).

    ADR-017 Decision 9:

    * `_routing_defaults` is read from the directory's defaults CARRIER
      only (the one `_defaults.yaml` / `.yml` the threshold chain reads), at
      the top level of the document, and joins the chain of the tenants at
      and below that directory. `receiver` / `overrides` written as null
      there is blocking: every tenant below without its own would lose it.
    * `_routing_enforced` in ANY file below the root is blocking.
    * routing profiles and domain policies below the root:
      `_parse_profiles_and_policies`.
    * the carrier's `defaults:` widens the key universe of the tenants below
      it (validate_tenant_keys), as the root carrier's does for every tenant.

    *reserved*: whether the file is a platform (`_`) file — the entry's Go
    class (the read loop passes it), else the name's (`classify_names`).
    """
    _f = safe_label(fname)
    if reserved is None:
        reserved = _name_class(fname) in RESERVED_CLASSES
    if reserved:
        _record_unknown_routing_keys(result, data, layer="platform", where="top level", file=fname)

    if "_routing_enforced" in data:
        _refuse_nested_enforced(fname, result)

    if "_routing_defaults" in data:
        rd = data["_routing_defaults"]
        if not is_level_carrier and reserved:
            # #2326 review F3: at the root this very file WOULD be read (any
            # root `_` file carries `_routing_defaults`), so moving it into a
            # subdirectory silently drops it — the #2291 "routing where
            # nothing reads it" class, reported as
            # `routing_in_unread_location`: a `WARN … skipping`
            # line, blocking under `--validate` like `_routing_defaults.routes`.
            result.setdefault("routing_defaults_errors", []).append(
                skipped_entry_warning(f"  WARN: _routing_defaults in {fname} is not read: below the "
                                      f"conf.d root only the directory's defaults carrier "
                                      f"(_defaults.yaml / _defaults.yml) carries it — move it there, "
                                      f"skipping",
                                      kind="routing_in_unread_location", file=fname,
                                      field="_routing_defaults"))
            _tree_problem(
                result, "routing_in_unread_location", fname,
                "_routing_defaults",
                f"{fname}: _routing_defaults is not read below the root "
                f"outside the directory's defaults carrier")
        elif not is_level_carrier:
            print(f"  WARN: _routing_defaults in {_f} ignored (below the "
                  "conf.d root only the directory's defaults carrier, "
                  "_defaults.yaml / _defaults.yml, is read)", file=sys.stderr)
        elif rd is None:
            pass   # an empty key: this level contributes nothing
        elif not isinstance(rd, dict):
            _routing_defaults_not_mapping(fname, rd, result)   # #2341 R5
        else:
            # #2245, as at the root: `routes` never belong to the defaults.
            if "routes" in rd:
                _record_file_reference_errors(  # #3257: dropped, still judged
                    result, {"routes": rd["routes"]}, where="_routing_defaults",
                    field_prefix="_routing_defaults.", file=fname)
                rd = {k: v for k, v in rd.items() if k != "routes"}
                result.setdefault("routing_defaults_errors", []).append(
                    skipped_entry_warning(f"  WARN: _routing_defaults in {fname}: 'routes' is not "
                                          "supported here (define it in a routing profile or the "
                                          "tenant's _routing), skipping",
                                          kind="routing_defaults_routes_ignored", file=fname,
                                          field="_routing_defaults.routes"))
            _record_ignored_keys(result, rd, block="routing_defaults",
                                 where="_routing_defaults",
                                 field_prefix="_routing_defaults.", file=fname)
            for key in ("receiver", "overrides"):
                if key in rd and rd[key] is None:
                    _tree_problem(
                        result, "routing_defaults_null_below_root", fname,
                        f"_routing_defaults.{key}",
                        f"{fname}: _routing_defaults.{key} is null — below "
                        f"the conf.d root that takes the {key} away from "
                        f"every tenant under {level}/ that does not set its "
                        f"own, and their alerts fall silently to the "
                        f"catch-all (ADR-017 Decision 9); set "
                        f"a value or remove the key")
            result.setdefault("routing_defaults_levels", {})[level] = rd

    if isinstance(data.get("defaults"), dict):
        if is_level_carrier:
            result.setdefault("defaults_keys_by_dir", {})[level] = set(
                data["defaults"].keys())
        elif data["defaults"]:
            print(f"  WARN: defaults in {_f} ignored (not a defaults carrier; "
                  "only _defaults.yaml / _defaults.yml is)", file=sys.stderr)

    _parse_profiles_and_policies(data, fname, level, result)


def _record_policy_problems(fname: str, policies: dict, result: dict) -> None:
    """#3116: every reason a domain policy of *fname* cannot be enforced as
    written (``domain_policy_problems``: the policy, `tenants` or
    `constraints` of the wrong shape, a list constraint that is not a list,
    an entry that is not a receiver type / tenant id, an unreadable duration
    bound, a `require_critical_escalation` that is not a boolean).

    Each is a ``domain_policy_unusable`` field-type error: the generator
    refuses the WHOLE tree on any of them, in every mode. Most used to be
    reported under `--strict` only (#2906 began with
    `require_critical_escalation`), so a lenient run switched the policy or
    the constraint off at rc 0.
    """
    for name, policy in policies.items():
        for fld, message in domain_policy_problems(policy):
            _field_type_error(
                result, "domain_policy_unusable",
                f"{fname}: domain_policy '{name}': {message}",
                file=fname, policy=str(name), field=fld)


def _parse_profiles_and_policies(data: dict, fname: str, level: str,
                                 result: dict) -> None:
    """`routing_profiles` / `domain_policies` of one file at directory *level*.

    Same file-name rules at every level (ADR-007): profiles only from
    `_routing_profiles.yaml` / `.yml`, policies only from
    `_domain_policy.yaml` / `.yml`. Below the root they are scoped to the
    subtree they sit in (#2326 (c), (d)).
    """
    _f = safe_label(fname)
    base = os.path.basename(fname)

    # v2.1.0 ADR-007: Extract routing_profiles (only from _routing_profiles.yaml)
    if "routing_profiles" in data:
        if base in ("_routing_profiles.yaml", "_routing_profiles.yml"):
            profiles = data["routing_profiles"]
            if isinstance(profiles, dict) and level == ROOT_LEVEL:
                _record_profiles(fname, level, profiles, result)
            elif isinstance(profiles, dict):
                # Recorded after every root file (`_parse_config_files`):
                # which definition of a duplicated name is "first" must not
                # depend on whether a directory's name sorts before `_` —
                # root files first, then the tree in name order.
                result.setdefault("_nested_profile_defs", []).append(
                    (fname, level, profiles))
            else:
                # #2904: a finding (routing_profiles_unusable, warn), not a
                # bare stderr print. Never blocking.
                result.setdefault("routing_profiles_errors", []).append(Finding(
                    f"  WARN: routing_profiles in {fname} must be a mapping of "
                    f"profile name to routing, got {type(profiles).__name__} — "
                    f"no routing profile in this file is applied, ignoring",
                    kind="routing_profiles_unusable", blocks="never",
                    file=fname, field="routing_profiles"))
                _tree_problem(
                    result, "routing_profiles_unusable", fname,
                    "routing_profiles",
                    f"{fname}: routing_profiles must be a mapping, got "
                    f"{type(profiles).__name__}; no profile in it is applied")
        else:
            print(f"  WARN: routing_profiles in {_f} ignored "
                  "(only allowed in _routing_profiles.yaml)", file=sys.stderr)

    # v2.1.0 ADR-007: Extract domain_policies (only from _domain_policy.yaml)
    if "domain_policies" in data:
        dp_fname = fname
        if base in _POLICY_FILENAMES:
            policies = data["domain_policies"]
            if isinstance(policies, dict):
                _record_policy_problems(fname, policies, result)
            if isinstance(policies, dict) and level == ROOT_LEVEL:
                result["domain_policies"].update(policies)
            elif isinstance(policies, dict):
                # #2326 (d): scoped to the subtree, judged apart from the
                # root's (additively) in load_tenant_tree.
                result.setdefault("domain_policies_by_dir", {}).setdefault(
                    level, {}).update(policies)
                origin = result.setdefault("domain_policy_origin", {})
                for name in policies:
                    origin[(level, name)] = fname
            elif policies is None:
                # #3116: `domain_policies:` with no value is legal in the
                # schema ("an empty or null map is legal"): no policy, as
                # an empty mapping. It used to be dropped as unusable.
                pass
            else:
                # #1448: the file decodes, parses, and is a mapping — and
                # every domain policy still ends up on the floor. Same
                # consequence as an unparseable file, so it goes through the
                # same bookkeeping; before this it printed a WARN nobody
                # reads and `--strict` exited 0 with `OK: all configs valid`.
                _drop_unusable_policy(
                    dp_fname,
                    f"'domain_policies:' must be a mapping of domain name to "
                    f"policy, got {type(policies).__name__} — the whole block "
                    f"is ignored",
                    f"indent each domain under `domain_policies:` in "
                    f"{dp_fname}",
                    result,
                    warning=f"  WARN: domain_policies in {_f} must be a "
                            f"dict, ignoring",
                    field="domain_policies")
        else:
            # #3126: a block in a file policies are not read from is as
            # unenforceable as one the generator cannot parse, so it is the
            # same `domain_policy_unusable` field-type error — the whole tree
            # is refused in every mode. It used to be a stderr WARN plus a
            # --strict-only ERROR, so a lenient run rendered at rc 0 with the
            # policy silently off. The KEY decides, not its value: nothing in
            # this file is ever read as a policy, an empty block included.
            target = str(PurePosixPath(fname).parent / "_domain_policy.yaml")
            _field_type_error(
                result, "domain_policy_unusable",
                f"{fname}: a domain_policies block is not read from this "
                f"file — domain policies are read only from "
                f"_domain_policy.yaml / _domain_policy.yml (the conf.d root, "
                f"or a subdirectory for its subtree), so no policy in it is "
                f"enforced — fix: move the domain_policies block into "
                f"{target}",
                file=fname, field="domain_policies")


# #2906: the `_severity_dedup` words, as the exporter reads them
# (pkg/config ResolveSeverityDedup: trimmed, case-insensitive). The disabling
# half is the platform's disable vocabulary (`_DISABLED_VALUES`, Go
# `isDisabled`). These are the field's VALUES, not YAML 1.1 boolean words:
# `yes` / `no` are not among them, quoted or not.
SEVERITY_DEDUP_ENABLE_WORDS = ("enable", "enabled", "on", "true")
SEVERITY_DEDUP_DISABLE_WORDS = ("disable", "disabled", "off", "false")
SEVERITY_DEDUP_WORDS = SEVERITY_DEDUP_ENABLE_WORDS + SEVERITY_DEDUP_DISABLE_WORDS


def severity_dedup_mode(value: object) -> str | None:
    """#2906: ``"enable"`` / ``"disable"`` for a tenant's ``_severity_dedup``
    value, or ``None`` when the value is refused.

    * null (the key absent, or written with no value) is the default,
      ``enable``;
    * a boolean: true is ``enable``, false ``disable`` (an unquoted
      ``false`` / ``off`` / ``no`` is the boolean False to PyYAML);
    * a string: one of ``SEVERITY_DEDUP_WORDS`` (trimmed, any case);
    * anything else — another string (``"no"``, ``"yes"``, ``""``), a
      number, a list, a mapping — is refused.
    """
    if value is None:
        return "enable"
    if isinstance(value, bool):
        return "enable" if value else "disable"
    if isinstance(value, str):
        word = value.strip().lower()
        if word in SEVERITY_DEDUP_ENABLE_WORDS:
            return "enable"
        if word in SEVERITY_DEDUP_DISABLE_WORDS:
            return "disable"
    return None


def routing_profile_ref(tenant: str, value: object) -> tuple[str | None, str | None]:
    """#3269: a tenant's ``_routing_profile`` as ``(name, error)``.

    A profile NAME, so a string: ``(value.strip(), None)`` — an empty string
    names nothing, ``(None, None)``, as does null (not set). Any other value —
    a mapping such as ``{default: p1, overrides: [...]}`` (a time window no
    reserved key supports), a number, a boolean, a list — is ``(None,
    message)``: the generator refuses the whole tree on it
    (``routing_profile_invalid``) and check_routing_profiles reports the same
    message. It used to be skipped in silence, the tenant falling back to the
    default route while the file read as if it named a profile.
    """
    if isinstance(value, str):
        return (value.strip() if value else None), None
    if value is None:
        return None, None
    return None, (f"tenant '{tenant}': _routing_profile must be a routing profile "
                  f"name (a string), got {value_type_text(value)} — a reserved key "
                  f"takes no time window or expires (#3269); write the name alone")


def _parse_tenant_overrides(tenant: str, overrides: dict, result: dict) -> None:
    """Extract per-tenant config from a tenant overrides dict.

    Handles: tenant keys, _routing_profile (ADR-007), _severity_dedup,
    _routing (explicit or disabled). `_metadata` is not read (#3043).
    Mutates *result* in place.
    """
    result["all_tenants"].append(tenant)

    # Collect tenant keys for schema validation
    if tenant not in result["tenant_keys"]:
        result["tenant_keys"][tenant] = set()
    result["tenant_keys"][tenant].update(overrides.keys())
    _record_unknown_routing_keys(result, overrides, layer="tenant", where=tenant, tenant=tenant)

    # v2.1.0 ADR-007: Extract _routing_profile reference
    # ⛔ #3269: judged by `routing_profile_ref` (check_routing_profiles reads
    # it too); a value that is not a name refuses the whole tree.
    rp_name, rp_error = routing_profile_ref(tenant, overrides.get("_routing_profile"))
    if rp_name is not None:
        result["tenant_profile_refs"][tenant] = rp_name
    if rp_error is not None:
        _field_type_error(result, "routing_profile_invalid", rp_error,
                          tenant=tenant, field="_routing_profile")

    # Severity dedup: default "enable", explicit "disable" to opt out.
    # ⛔ #2906: judged by type and value (`severity_dedup_mode`); a value it
    # does not know refuses the WHOLE tree. Before, `str(value).lower()`
    # fell through to "enable" for anything not a disabling word, so a
    # quoted "no" (and, once the generator reads yaml.v3, an unquoted `no`)
    # turned dedup ON in silence.
    raw_dedup = overrides.get("_severity_dedup")
    mode = severity_dedup_mode(raw_dedup)
    if mode is None:
        _field_type_error(
            result, "severity_dedup_invalid",
            f"tenant '{tenant}': _severity_dedup must be enable or disable "
            f"(also read: {', '.join(SEVERITY_DEDUP_WORDS[1:4])} / "
            f"{', '.join(SEVERITY_DEDUP_WORDS[5:])}; or true / false, "
            f"unquoted), got {value_type_text(raw_dedup)}",
            tenant=tenant, field="_severity_dedup")
        mode = "enable"
    result["dedup_configs"][tenant] = mode

    # #3043: `_metadata` is not read here — nothing the generator renders
    # uses it (the exporter serves it as tenant_metadata_info).

    # Routing: "disable" string → skip routing
    routing = overrides.get("_routing")
    if isinstance(routing, str) and _is_disabled(routing):
        result["disabled_tenants"].add(tenant)
        return

    if isinstance(routing, dict):
        if routing:
            result["explicit_routing"][tenant] = routing
            _record_ignored_keys(result, routing, block="routing",
                                 where=f"{tenant}: _routing", tenant=tenant)
    elif "_routing" in overrides:
        # #2341 R5: neither a mapping nor a disabling string (`"slack"`, a
        # list, null, an unquoted `false` / `off` / `no`, which PyYAML reads
        # as a boolean). It used to fall through to the defaults route in
        # silence; now the tenant renders NO route (the routing it wrote
        # cannot be read, and guessing "defaults" or "off" is both wrong)
        # and the line below is blocking (`WARN … skipping`).
        result.setdefault("routing_refused", {})[tenant] = routing


def _parse_config_files(config_dir: str, reader: "_lib_parse_cache.ConfdReader | None" = None
                        ) -> dict:
    """Parse all YAML files in config_dir and extract raw data.

    ADR-036 step 4 (#2906): each file's content is `da-guard parse-tree`'s
    (*reader*; the session's reader of *config_dir* when None). A file
    parse-tree cannot read is booked as skipped with its kind; no usable
    da-guard exits 2 (``parse_tree_failure_exit``).

    Returns a dict with keys:
        all_tenants, defaults_keys, routing_defaults, enforced_routing,
        explicit_routing, disabled_tenants, dedup_configs,
        tenant_keys, routing_profiles, domain_policies, tenant_profile_refs.

    Delegates to:
        _parse_platform_config() — platform-level keys from _ prefixed files
        _parse_tenant_overrides() — per-tenant overrides from tenant sections
    """
    result = {
        "all_tenants": [],
        "defaults_keys": set(),
        "optional_override_keys": set(),  # #1189: declared, platform-valueless
        "routing_defaults": {},
        "enforced_routing": None,
        "explicit_routing": {},
        "disabled_tenants": set(),
        "dedup_configs": {},
        "tenant_keys": {},
        "routing_profiles": {},      # v2.1.0 ADR-007: named routing configs
        "domain_policies": {},       # v2.1.0 ADR-007: domain compliance constraints
        "tenant_profile_refs": {},   # v2.1.0 ADR-007: tenant → profile name
        "policy_file_errors": [],    # ADR-007: unusable _domain_policy.yaml (#3116: refused in every mode)
        # #1460: whole-file accounting. `files_read` counts config files this
        # reader parsed (a mapping, or an empty document); `files_skipped`
        # is every (fname, reason) it had to drop; `tenant_file_errors` is
        # the non-policy subset of those, with the remedy attached.
        "files_read": 0,
        "files_skipped": [],
        "tenant_file_errors": [],
        # #2326: the routing plane across directory levels. `tenant_dirs` is
        # each tenant's directory relative to the root ("." = the root);
        # `routing_defaults_levels` / `routing_profiles_by_dir` /
        # `domain_policies_by_dir` hold what each level contributes (the root's
        # `_routing_defaults` and policies stay in the keys above);
        # `routing_tree_problems` is (kind, file, field, message) per finding.
        "tenant_dirs": {},
        "routing_defaults_levels": {},
        "routing_profiles_by_dir": {},
        "routing_profile_origin": {},
        "domain_policies_by_dir": {},
        "domain_policy_origin": {},
        "defaults_keys_by_dir": {},
        "routing_tree_problems": [],
        # #2906: a boolean / enum field of the wrong type or an unknown
        # value, one dict per finding (`_field_type_error`) — refused whole.
        "field_type_errors": [],
        "tenant_declarations": [],   # (file, tenant id) per tenant-file key
        # #2315: {tenant_id: [file, ...]} for every id two tenant files declare.
        "duplicate_tenants": {},
    }

    config_dir_problem = classify_path(config_dir, "dir", flag="--config-dir")  # #3276
    if config_dir_problem is not None:
        print(f"ERROR: {config_dir_problem.message}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # #2326 (ADR-016 "Amendment 2026-09-28", ADR-017 Decision 9): this reader walks the WHOLE
    # tree, with the walker every other reader of the threshold plane uses
    # (`list_config_tree`: hidden directories pruned, directory symlinks
    # reported). Until #2326 it was flat — a tenant in a subdirectory got no
    # route at rc 0 while threshold-exporter served its thresholds. ONE walk
    # answers both "what to read" and "what could not be read" — since #3206
    # the reader's own listing (Go's per-path `role`): the entries of the
    # document every file below is read from, never a second walk.
    if reader is None:
        reader = _lib_parse_cache.reader_for(config_dir)
    try:
        reader.require()
        listing = list_config_tree(config_dir, reader=reader)
    except ParseTreeError as e:
        parse_tree_failure_exit(e, config_dir)

    # #1469: name the config-named things that are not readable files (a
    # DIRECTORY called `beta.yaml`, a broken symlink, an unreadable file)
    # BEFORE the read loop, because the loop below no longer meets them —
    # the walk drops them from `files`, as it should. Dropping them silently
    # would have been one signal fewer than this reader gave before the
    # enumerators were unified, so the record is kept, through the same
    # `_drop_unusable_policy` bookkeeping every other dropped file uses.
    # Since #2326 that includes a directory the walk could not enter and a
    # directory symlink it does not follow: every tenant under either is
    # absent from this run, which is the #1460 refusal's own shape.
    unusable = listing.unusable

    # ⛔ THE ROOT ITSELF, before anything else. `list_config_tree` reports
    # a conf.d root it could not list (a `chmod 111` directory: traversable,
    # not readable) by returning the root — which means this reader saw ZERO
    # files, not "no tenants are configured".
    #
    # Left to fall through, that is a silent green: the loop below prints one
    # stderr WARN, the read loop finds nothing, and `main` reports "No tenants
    # found in config directory." and exits 0. `.github/workflows/validate.yaml`
    # runs `generate-routes --validate --strict` as a REQUIRED check, and
    # GitHub Actions does not fail a step for stderr output — so an unreadable
    # conf.d would turn that gate green with zero routes.
    #
    # ⚠️ Measured, and it is a REGRESSION this change set introduced: before
    # `unusable_config_paths` grew its `except OSError` the same input raised
    # `PermissionError` out of `root.iterdir()` and the process died with a
    # traceback. rc went 1 → 0. Catching the exception was right; letting the
    # caller treat "unreadable" as "empty" was not.
    #
    # EXIT_CALLER_ERROR, matching the `not os.path.isdir` guard a few lines up:
    # both mean "the directory you pointed me at is not usable as input", which
    # is a different statement from "your configuration has a finding".
    root_path = Path(config_dir)
    if any(p == root_path for p in unusable):
        print(f"ERROR: config directory could not be read: {config_dir} — "
              f"{unusable_reason(root_path, listing.listing)}. Nothing was scanned, so an "
              f"empty result here would mean 'unreadable', not 'no tenants'.",
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    def _rel(p: Path) -> str:
        # Root-relative POSIX path: a root file keeps its bare name, so every
        # message about a flat tree reads exactly as it did before #2326.
        return p.relative_to(root_path).as_posix()

    for bad in unusable:
        rel = _rel(bad)
        _drop_unreadable_file(
            rel, unusable_reason(bad, listing.listing),
            f"remove {rel} or replace it with a readable YAML file",
            result)
    # #2981: entries the generator service read from git as not regular files.
    for rel, mode in sorted(_GIT_IRREGULAR.items()):
        _drop_unreadable_file(
            rel, git_irregular_reason(mode),
            f"commit {rel} as a regular file (its content in place of the "
            f"link), or remove it",
            result)

    # #1469: ONE predicate for "what is a config file", shared with
    # `check_yaml_syntax` — the walker's own list.
    # #1674: ONE carrier per directory, chosen by the rule every exporter
    # plane reads. Any other carrier spelling is ignored WHOLE, as the
    # exporter ignores it — never opened — and named once, as its own
    # finding (#2906). ⛔ Which file that is, is parse-tree's verdict
    # (`ConfdReader.not_read` / `carrier`), never a Python selection:
    # a second implementation of the rule that read a file parse-tree lists
    # in `not_read` would die rc 2 on `NotSelected` (#2906 review).
    # #1982: every tenant entry of every file, in read order, applied AFTER
    # the loop — see `_apply_tenant_entries`.
    tenant_entries: list[tuple[str, object, dict]] = []
    # The files of `tenant_entries` Go classes platform (`_`) files (#3206).
    platform_files: set[str] = set()
    listed = listing.files
    try:
        _read, not_read = reader.split_not_read(listed)
        skipped_by_dir: dict[Path, list[Path]] = {}
        for p in listed:  # directories in walk order, as the findings were
            skipped_by_dir.setdefault(p.parent, [])
        for p, _why in not_read:
            skipped_by_dir[p.parent].append(p)
        # #2906: each carrier not selected is a finding, the only report of
        # it on this path, so the one fact is printed once, by the stream.
        for d, skipped_here in skipped_by_dir.items():
            if not skipped_here:
                continue
            chosen = reader.carrier(d)
            for p in sorted(skipped_here, key=_rel):
                _unselected_carrier(_rel(p), _rel(chosen) if chosen is not None
                                    else "another carrier",
                                    PurePosixPath(_rel(p)).parent.as_posix(), result)
    except ParseTreeError as e:
        parse_tree_failure_exit(e, config_dir)
    skipped_set = {p for p, _why in not_read}
    # Under the generator container's cache: every read below that would
    # miss is parsed in ONE parse-tree run, not one run (and one walk of the
    # whole tree) per file — the cache's entries are unchanged
    # (`ConfdReader.prefetch_reads`; ADR-036 §3, #3206): the loop's routing
    # read of each file, and the tenant declarations' read of each tenant
    # file (`tenant_declarations`, `declared_tenants`). Not the skipped
    # ones: they are never read, so they would miss on every judgement.
    try:
        if _lib_parse_cache.active() is not None:
            reader.prefetch_reads(
                [(p, ROUTING_READ) for p in listed if p not in skipped_set]
                + [(rel, _lib_parse_cache.VALUES)
                   for rel in _lib_parse_cache.tenant_scope(reader).files])
    except ParseTreeError as e:
        parse_tree_failure_exit(e, config_dir)
    for path_p in listed:
        fname = _rel(path_p)
        level = PurePosixPath(fname).parent.as_posix()
        if path_p in skipped_set:
            # The unselected spelling: named above (#2906, a finding), never
            # opened. A carrier parse-tree cannot read is not here: the read
            # below books it as skipped.
            continue
        # Not in `not_read`: a defaults-named file here is its directory's
        # selected carrier (or one the read below drops as unreadable).
        # Which file is a carrier or a platform file is Go's class (#3206).
        entry = listing.listing.entry(fname)
        is_carrier = entry.cls == "defaults"
        is_reserved = entry.reserved
        # ADR-036 step 4 (#2906): the content is parse-tree's. A file it
        # cannot read (`unreadable`: a syntax error, a repeated key — the
        # exporter's yaml.v3 rejects that file —, a null key, several
        # documents, a top level that is not a mapping, not UTF-8, …) is
        # dropped WHOLE and booked with its kind. An empty file is {}.
        try:
            data = reader.read(path_p, ROUTING_READ)
        except FileUnreadable as e:
            _drop_unreadable_file(fname, unreadable_reason(e),
                                  unreadable_remedy(e, fname), result)
            continue
        except ParseTreeError as e:
            parse_tree_failure_exit(e, config_dir)
        except ValueError as e:
            # A tag or shape `to_python` does not read: check_shape refuses
            # those first, so this is a da-guard this tool does not match.
            parse_tree_failure_exit(ParseTreeError(str(e)), config_dir)

        # #1460: read — a mapping (an empty file is an empty mapping).
        result["files_read"] += 1

        if not data:
            continue

        # #2904: routing where nothing reads it. `is_carrier`: this
        # directory's selected carrier (an unselected spelling never got here).
        _unread_routing(
            data, fname,
            is_carrier=is_carrier,
            is_root_platform=level == ROOT_LEVEL and is_reserved,
            result=result)

        if level == ROOT_LEVEL:
            _parse_platform_config(data, fname, result, cls=entry.cls)
        else:
            _parse_nested_config(data, fname, level, is_carrier, result,
                                 reserved=is_reserved)

        if "tenants" not in data:
            continue

        # #2326 (f): the `tenants:` block of a platform file BELOW the root is
        # read by no plane (ADR-017 Decision 5); threshold-exporter WARNs about
        # it, and so does this reader now that it opens the file.
        if level != ROOT_LEVEL and is_reserved:
            if data.get("tenants"):
                print(f"  WARN: {safe_label(fname)}: 'tenants:' block ignored "
                      f"— a platform file below the conf.d root supplies no "
                      f"per-tenant values on any plane (only root platform "
                      f"files do); set the keys in the tenant's own file",
                      file=sys.stderr)
            continue

        # `is None`, not the get() default: `tenants:` with every entry
        # commented out parses to None, and the default only fires when the
        # key is absent — the `"tenants" not in data` check above already let
        # that through. #1447: this raised AttributeError and killed the run.
        # (An earlier revision wrote this as `data.get("tenants") or {}` and
        # the comment still said `or {}` after the code stopped doing it.)
        raw_tenants = data.get("tenants")
        if raw_tenants is None:
            # ⛔ Says so rather than passing silently. Commenting a tenant
            # out is a legitimate way to disable it, so this is not an
            # error and the exit code does not change — but "I disabled it"
            # and "I commented out one threshold and took the block with
            # it" produce byte-identical files, and the sibling branch
            # below already warns for a wrong-shaped `tenants:`. Before
            # this, only the null spelling was silent.
            print(f"  WARN: {safe_label(fname)}: 'tenants:' is declared but empty — no "
                  f"tenant is loaded from this file", file=sys.stderr)
            continue
        tenants = raw_tenants
        if not isinstance(tenants, dict):
            # ⚠️ THE ASYMMETRY IS THE POINT, and this reader is the lenient
            # side. Go declares this field
            # `map[string]map[string]ScheduledValue`
            # (`ThresholdConfig.Tenants`, `pkg/config/types.go`), so a sequence here is a
            # `yaml.TypeError`, `parsePartialConfig` returns ok=false
            # (`flat_scanner.go`) and the **entire file is dropped** —
            # taking `defaults:` with it, which this loop has already
            # harvested a few lines above. Same bytes: CI green here, empty
            # platform defaults in the exporter. That is verbatim the hazard
            # the `optional_overrides` branch of this same function warns
            # about, and this branch used to be a crash rather than a skip,
            # so the quiet version is new.
            #
            # #1460 (blind review): a WARN alone left this a whole-file loss
            # that `--validate` still called OK — every tenant in the file
            # vanishes here, and the exporter drops the file outright. Book
            # it like the other whole-file drops; `files_read` was counted
            # at parse time, so take it back the way the non-mapping branch
            # above does.
            result["files_read"] -= 1
            _drop_unreadable_file(
                fname,
                f"'tenants' must be a mapping, got {type(tenants).__name__} "
                f"— no tenant in this file is loaded here, and the Go "
                f"exporter drops the WHOLE file on it (including its "
                f"`defaults:`)",
                f"make `tenants:` in {fname} a mapping of "
                f"<tenant>: {{<key>: <value>}}",
                result)
            continue
        for tenant, overrides in tenants.items():
            # #2326 review F2: a DECLARATION is the key, whatever its value —
            # the exporter walker's rule (`TreeFile.TenantIDs` are the keys of
            # `tenants:`; a null body declares the tenant too). Recorded before
            # the shape check below, which only decides what gets LOADED, so
            # `a/ta.yaml: {tenants: {ta: ~}}` beside `b/ta.yaml` is still the
            # duplicate the exporter refuses.
            if is_reserved:
                platform_files.add(fname)
            else:
                result.setdefault("tenant_declarations", []).append(
                    (fname, tenant))
            if not isinstance(overrides, dict):
                # Per-ENTRY, not per-file: the other tenants in this file
                # are fine, so this stays out of the file booking — but it
                # was silent before, and a tenant whose block is a list or a
                # scalar simply did not exist as far as routing went.
                print(f"  WARN: {safe_label(fname)}: tenant "
                      f"'{safe_label(str(tenant))}' must be a mapping, got "
                      f"{type(overrides).__name__} — that tenant is not "
                      f"loaded", file=sys.stderr)
                continue
            tenant_entries.append((fname, tenant, overrides))

    for nested_def in result.pop("_nested_profile_defs", []):
        _record_profiles(*nested_def, result)
    # #2315: validate-config's `tenant_uniqueness` scan, called — not
    # re-derived — so the two tools agree on every duplicate. It walks the
    # whole tree, as the exporter does, although this reader routes the root.
    try:
        result["duplicate_tenants"] = duplicate_declarations(
            tenant_declarations(config_dir, reader)[0])
    except ParseTreeError as e:
        parse_tree_failure_exit(e, config_dir)
    _apply_tenant_entries(config_dir, tenant_entries, result, reader,
                          platform=platform_files)
    return result


def _declared_ids(config_dir: str, reader: "_lib_parse_cache.ConfdReader | None",
                  unknown: list) -> set:
    """The ids the tree's tenant files declare (`_lib_parse_cache.
    declared_tenants`, both halves of Go's `TenantScope`) through this run's
    reader; what leaves the set open is appended to *unknown*. No usable
    parse-tree exits 2 (`parse_tree_failure_exit`)."""
    if reader is None:
        reader = _lib_parse_cache.reader_for(config_dir)
    try:
        declared, open_ = _lib_parse_cache.declared_tenants(reader)
    except ParseTreeError as e:
        parse_tree_failure_exit(e, config_dir)
        raise  # unreachable: parse_tree_failure_exit exits
    unknown.extend(open_)
    return set(declared)


def _apply_tenant_entries(config_dir: str, entries: list, result: dict,
                          reader: "_lib_parse_cache.ConfdReader | None" = None, *,
                          platform: "set[str]") -> None:
    """Hand each tenant ONE merged block: platform file first, tenant file wins.

    ⛔ THIS USED TO RUN PER FILE, AND THE FILENAME DECIDED WHO WON. A root
    platform file's `tenants:` block is the platform's per-tenant default;
    the tenant's own file overrides it key by key. Called once per file,
    `_parse_tenant_overrides` let whichever file sorted LAST win — and it
    treats a key the file does not write as that key's default, so a tenant
    file silent on `_severity_dedup` RESET the platform's `disable` to
    `enable`. Measured: platform `_severity_dedup: disable`, tenant file
    `tx.yaml` silent → `enable`; the same tenant file named `TX.yaml` or
    `0tx.yaml` (sorting before `_`) → `disable`. And `all_tenants` listed the
    tenant once per file. The exporter's flat plane merges in the same order
    (`sortFlatMergeOrder`), so the two planes agree whatever the file is called.

    A platform entry for a tenant no tenant file declares (anywhere in the
    tree — Go's `TenantScope`, both halves) is dropped and named: a platform
    file cannot create a tenant. *platform*: the files of *entries* Go
    classes platform (`_`) files (the read loop's entries' class).
    """
    unknown: list = []
    merged, orphans = overlay_platform_tenants(
        entries, lambda: _declared_ids(config_dir, reader, unknown), platform=platform)
    for fname, tenant in orphans:
        print(f"  WARN: {safe_label(fname)}: tenants.{safe_label(str(tenant))} "
              f"ignored — no tenant file declares tenant "
              f"'{safe_label(str(tenant))}'{tenant_set_unknown_note(unknown)}; a platform "
              f"file can only provide defaults for a tenant that already exists",
              file=sys.stderr)
    # #2326: where each tenant lives (its tenant file's directory, relative to
    # the root) decides which routing layers and domain policies reach it —
    # and one tenant id declared by two tenant files is blocking (e): the
    # exporter rejects such a tree (DuplicateTenantError), and the per-file
    # merge above would otherwise route a blend of both blocks. The second
    # file in walk order is the one named, as `DuplicateTenantError.PathB`.
    declared: dict = {}
    # Every declaration by a tenant file (F2: including a body that is not a
    # mapping and so is not loaded); a caller that hands `entries` without
    # that record (a hand-built result in a test) falls back to the entries.
    if "tenant_declarations" in result:
        decls = result["tenant_declarations"]
    else:
        decls = [(f, t) for f, t, _o in entries if f not in platform]
    for fname, tenant in decls:
        declared.setdefault(tenant, []).append(fname)
    dirs = result.setdefault("tenant_dirs", {})
    for tenant, files in declared.items():
        dirs.setdefault(tenant, PurePosixPath(files[0]).parent.as_posix())
    for tenant, files in sorted(duplicate_declarations(declared).items(),
                                key=lambda kv: str(kv[0])):
        first, second = declared[tenant][0], declared[tenant][1]
        _tree_problem(
            result, "duplicate_tenant", second, f"tenants.{tenant}",
            f"duplicate tenant id '{tenant}': declared in both {first} and "
            f"{second} — threshold-exporter rejects a tree that declares one "
            f"tenant in two files (DuplicateTenantError), and routes built "
            f"from a blend of both would match neither; keep the tenant in "
            f"one file ({', '.join(files)})")
    # ADR-035 D3: an invalid id is judged on the DECLARATIONS — every key a
    # tenant file puts under `tenants:`, a body that is not a mapping (null,
    # a scalar) included — not on the loaded set: such a body is never
    # loaded, and judging only what loaded let `Team_D:` (null) through to
    # rc 0 in every mode. Shared with validate-config through the
    # `invalid_tenant_ids` record below.
    for tenant in declared:
        if not is_valid_tenant_id(tenant):
            result.setdefault("invalid_tenant_ids", []).append(tenant)
    # First-appearance order, as the per-file loop produced it.
    for tenant in dict.fromkeys(t for _f, t, _o in entries):
        if tenant not in merged:
            continue  # an orphan, named above
        if not is_valid_tenant_id(tenant):
            # #2341 R8: nothing at all is rendered for it — no route, no
            # receiver, no inhibit rule, in every mode (an empty id became
            # `tenant=""`, which Alertmanager matches on alerts WITHOUT a
            # tenant label: platform alerts were routed to `tenant-`).
            result.setdefault("invalid_tenant_ids", []).append(tenant)
            continue
        _parse_tenant_overrides(tenant, merged[tenant], result)


def _merge_tenant_routing(parsed: dict, routing_defaults: dict) -> dict[str, dict]:
    """Merge routing defaults with explicit tenant routing configs.

    v2.1.0 ADR-007: Four-layer merge pipeline:
      1. _routing_defaults → global defaults
      2. routing_profiles[ref] → team/domain shared config (if _routing_profile set)
      3. tenant _routing → per-tenant overrides
      4. _routing_enforced → NOC immutable override (applied later in generate_routes)

    #2326: layer 1 is a CHAIN — the root's `_routing_defaults` (the
    *routing_defaults* argument), then each subdirectory level's on the way
    to the tenant's directory, shallow per top-level key, deeper wins — and
    layer 2 resolves against the profiles visible from that directory
    (`resolve_routing_defaults` / `visible_routing_profiles` in _grar_merge).

    Returns routing_configs dict {tenant: merged_routing}.
    """
    profile_refs = parsed.get("tenant_profile_refs", {})
    tenant_dirs = parsed.get("tenant_dirs", {})

    routing_configs = {}
    seen_tenants = set()
    for tenant in sorted(set(parsed["all_tenants"])):
        if (tenant in parsed["disabled_tenants"] or tenant in seen_tenants
                or tenant in parsed.get("routing_refused", {})):
            continue
        seen_tenants.add(tenant)
        level = tenant_dirs.get(tenant, ROOT_LEVEL)

        # Layer 1: Start with routing defaults (root, then each level down)
        base = resolve_routing_defaults(parsed, level,
                                        routing_defaults or {})
        profiles = visible_routing_profiles(parsed, level)

        # Layer 2: Merge routing profile (if referenced)
        if tenant in profile_refs:
            profile_name = profile_refs[tenant]
            if profile_name in profiles:
                profile_cfg = profiles[profile_name]
                if isinstance(profile_cfg, dict):
                    for k, v in profile_cfg.items():
                        base[k] = v
            # Warning for unknown profile is emitted by _validate_profile_refs()

        # Layer 3: Merge explicit tenant routing overrides
        tenant_routing = parsed["explicit_routing"].get(tenant)

        # Only produce a routing config if there's something to route
        if tenant_routing or base:
            routing_configs[tenant] = merge_routing_with_defaults(
                base, tenant_routing, tenant)

    return routing_configs


@dataclass
class TenantTree:
    """What ``load_tenant_tree`` read, plus what it could NOT read (#1460).

    Callers read the fields by name; there is no positional form (#2906:
    the former 5-tuple's last slot had been a constant ``{}`` since #3043).

    * ``routing_configs`` — {tenant: routing config} for tenants with
      ``_routing`` (the 4-layer merge: defaults → profile → tenant).
    * ``dedup_configs`` — {tenant: "enable"|"disable"} for ALL tenants
      (default "enable"); its keys are the generator's tenant set — what a
      ``{{tenant}}`` ``_routing_enforced`` expands over (#2519) — every
      valid id loaded, even one with no ``_routing``.
    * ``schema_warnings`` — the validation warning/error stream.
    * ``enforced_routing`` — the platform ``_routing_enforced`` or None.

    The accounting fields are the reason this type exists: a consumer that
    only got the first four cannot tell "two tenants" from "three tenants,
    one of which did not parse", and that gap is how a smaller tree was
    reported as a valid one.

    * ``files_read`` — config files parsed (a mapping or an empty document).
    * ``files_skipped`` — ``(fname, reason)`` for every file dropped, policy
      files included.
    * ``tenant_file_errors`` — the non-policy subset of ``files_skipped``,
      ``(fname, reason-with-remedy)``. Non-empty means the tree below is
      INCOMPLETE and no verdict over it should be reported as a success.
    """
    routing_configs: dict[str, dict]
    dedup_configs: dict[str, str]
    schema_warnings: list[str]
    enforced_routing: dict | None
    files_read: int = 0
    files_skipped: list[tuple[str, str]] = field(default_factory=list)
    tenant_file_errors: list[tuple[str, str]] = field(default_factory=list)
    # #2326: every routing-tree finding, ``(kind, file, field, message)`` —
    # the blocking kinds (`BLOCKING_TREE_KINDS`, see ``routing_tree_errors``)
    # and the subtree-policy finding (d). ``file`` is root-relative.
    routing_tree_problems: list[tuple[str, str, str, str]] = field(
        default_factory=list)

    @property
    def routing_tree_errors(self) -> list[tuple[str, str, str, str]]:
        """The #2326 findings that refuse the whole tree (rc 2, every mode)."""
        return [p for p in self.routing_tree_problems
                if p[0] in BLOCKING_TREE_KINDS]
    # #2315: {tenant_id: [file, ...]} — ids two tenant files declare
    # (`_lib_confd.tenant_declarations`, validate-config's own scan).
    duplicate_tenants: dict[str, list[str]] = field(default_factory=dict)
    # ADR-035 D3: declared tenant ids `is_valid_tenant_id` refuses, sorted.
    # Nothing is rendered for them here; the generator refuses the whole
    # tree on them in every mode (`_refuse_invalid_tenant_ids`).
    invalid_tenant_ids: list[str] = field(default_factory=list)
    # #2906: boolean / enum fields of the wrong type or with an unknown
    # value (`_field_type_error` dicts). The generator refuses the whole
    # tree on them in every mode (`_refuse_field_type_errors`).
    field_type_errors: list[FieldTypeError] = field(default_factory=list)
    # #2906: the defaults carriers no plane reads (`unselected_defaults_carrier`
    # Findings, also in `schema_warnings`) — kept apart for
    # `print_reader_warnings`.
    unselected_carriers: list[Finding] = field(default_factory=list)


def policy_level_source(parsed: dict, level: str) -> str:
    """The policy file(s) a subtree *level*'s ``domain_policies`` came from,
    as ``check_policy_scope`` names them (#2326 (d))."""
    origin = parsed.get("domain_policy_origin", {})
    files = sorted({origin[(lv, n)] for (lv, n) in origin if lv == level})
    return " / ".join(files) or f"{level}/"


def load_tenant_tree(
    config_dir: str,
    *,
    strict_policies: bool = False,
    reader: "_lib_parse_cache.ConfdReader | None" = None,
) -> TenantTree:
    """Load and parse every tenant file of *config_dir* into a ``TenantTree``.

    The pipeline:
      1. Parse all .yaml/.yml files in config_dir (``_parse_config_files``)
      2. Merge _routing_defaults with tenant _routing overrides
         (``_merge_tenant_routing``)
      3. Validate tenant keys against defaults (checks for typos)
      4. Resolve routing profile references (ADR-007)
      5. Validate domain policies against resolved routes (ADR-007);
         with strict_policies=True violations are emitted as ERROR
         (blocking for the --strict CLI/CI path) instead of WARN.

    Configuration hierarchy (4 layers): Layer 0 ``_routing_defaults`` from
    ``_``-prefixed files; Layer 1 the ``_routing_profile`` reference; Layer 2
    the profile resolved from ``_routing_profiles.yaml``; Layer 3 explicit
    tenant ``_routing`` overrides.

    Exit 2 on an unusable directory. A file this reader dropped is returned
    as data (``TenantTree.tenant_file_errors`` / ``files_skipped``, #1460),
    not only as a stderr WARN line — a consumer whose verdict depends on the
    tree being complete must look at them. *reader*: as for
    ``_parse_config_files`` (the session's reader of *config_dir* when None).
    """
    parsed = _parse_config_files(config_dir, reader)

    routing_configs = _merge_tenant_routing(
        parsed, parsed["routing_defaults"])

    tenant_dirs = parsed.get("tenant_dirs", {})
    # #2326: the blocking routing-tree findings come FIRST in the stream, so a
    # reader of --validate / validate-config sees why the tree is refused
    # before the findings that follow from it.
    # #2766: each one a Finding with the record's own kind / file / field.
    schema_warnings = [
        Finding(f"  {ROUTING_TREE_ERROR_PREFIX} {msg}", kind=kind,
                blocks="always", file=_f, field=_fld)
        for kind, _f, _fld, msg in parsed.get("routing_tree_problems", [])
        if kind in BLOCKING_TREE_KINDS]
    # #2906: the field-type findings, blocking in every mode as well.
    field_type_errors = list(parsed.get("field_type_errors", []))
    schema_warnings.extend(field_type_findings(field_type_errors))

    # Schema validation: check tenant keys against defaults. #2326: a tenant
    # below the root also sees the `defaults:` keys of every carrier on its
    # directory chain — the keys the exporter merges into it.
    by_dir_keys = parsed.get("defaults_keys_by_dir", {})
    for tenant, keys in sorted(parsed["tenant_keys"].items()):
        universe = set(parsed["defaults_keys"])
        for d in chain_levels(tenant_dirs.get(tenant, ROOT_LEVEL)):
            universe |= by_dir_keys.get(d, set())
        schema_warnings.extend(
            unclassified(w, blocks="never")
            for w in validate_tenant_keys(tenant, keys, universe,
                                          parsed["optional_override_keys"]))

    # v2.1.0 ADR-007: Validate profile references
    schema_warnings.extend(_validate_profile_refs(parsed))

    # #2989: a `_routing_enforced.match` that is not a list. The line carries
    # "WARN … skipping", so `--validate` (and validate-config's schema row)
    # fail on it; render mode prints it and renders without NOC. (#2906: a
    # non-boolean `enabled` is a field-type finding, above — refused whole.)
    schema_warnings.extend(parsed.get("enforced_errors", []))
    # #2245: `_routing_defaults.routes` — dropped at parse, same blocking line.
    # #2904: routing written where nothing reads it (routing_in_unread_location)
    # rides the same list, so it blocks `--validate` the same way.
    schema_warnings.extend(parsed.get("routing_defaults_errors", []))
    # #2960: keys a routing block carries that the generator does not read.
    schema_warnings.extend(parsed.get("ignored_routing_keys", []))
    # #2906: a defaults carrier no plane reads (unselected_defaults_carrier),
    # blocking under `--validate` the same way.
    schema_warnings.extend(parsed.get("unselected_carriers", []))
    # #2904: a `routing_profiles:` block that is not a mapping — no profile in
    # it is applied: routing_profiles_unusable, a warning that never blocks;
    # the tenants that reference one of its
    # profiles get the unknown-profile line besides.
    schema_warnings.extend(parsed.get("routing_profiles_errors", []))
    # #2904: overrides that change nothing (duplicate_override_matcher /
    # redundant_override_receiver): a warning in every mode, never blocking,
    # judged on the RESOLVED routing.
    for tenant, rc in sorted(routing_configs.items()):
        for kind, fld, text in override_redundancies(rc):
            schema_warnings.append(Finding(
                f"  WARN: {tenant}: {text}", kind=kind, blocks="never",
                tenant=tenant, field=fld))
    # #2341 R5: a tenant `_routing` that is neither a mapping nor a disabling
    # string — the tenant renders no route; blocking like the lines above.
    refused = parsed.get("routing_refused", {})
    for tenant in sorted(refused, key=str):
        schema_warnings.append(routing_not_mapping_warning(tenant, refused[tenant]))
    # #2341 R8: a tenant id the routing plane refuses — nothing rendered.
    invalid_ids = sorted(set(parsed.get("invalid_tenant_ids", [])), key=str)
    for tenant in invalid_ids:
        schema_warnings.append(skipped_entry_warning(f"  WARN: {invalid_tenant_id_text(tenant)}, skipping",
                                                     kind="invalid_tenant_id", tenant=tenant))

    # v2.1.0 ADR-007: Validate domain policies against resolved routing.
    # #2326 (d): a policy file below the root applies to the tenants of its
    # own subtree only, judged ADDITIVELY with the root's and every other
    # level's — each level is its own check_domain_policies call over the
    # tenants it reaches, so a tenant meets every policy above it and a
    # subtree can only tighten. An entry naming a tenant outside the subtree
    # is its own finding (ERROR under --strict, WARN otherwise).
    # `domain_policy_levels` / `policy_reaches` are shared with
    # `explain_route --trace` (#2435), so the trace applies the same set.
    tree_problems = list(parsed.get("routing_tree_problems", []))
    origin = parsed.get("domain_policy_origin", {})
    for level, policies in domain_policy_levels(parsed):
        reachable = {t: rc for t, rc in routing_configs.items()
                     if policy_reaches(level, t, tenant_dirs)}
        if level == ROOT_LEVEL:
            # The root reaches every tenant: no scope finding to make.
            schema_warnings.extend(check_domain_policies(
                reachable, policies, strict=strict_policies))
            continue
        source = policy_level_source(parsed, level)
        msgs, rows = check_policy_scope(
            level, policies, tenant_dirs, source=source, strict=strict_policies,
            policy_files={d: origin[(level, d)] for d in policies
                          if (level, d) in origin})
        schema_warnings.extend(msgs)
        for domain, tenant in rows:
            tree_problems.append((
                "domain_policy_out_of_scope", origin.get((level, domain), source),
                f"domain_policies.{domain}.tenants",
                f"domain policy '{domain}' names tenant '{tenant}' outside "
                f"{level}/"))
        schema_warnings.extend(
            check_domain_policies(reachable, policies, strict=strict_policies))

    # (#3116 / #3126: a _domain_policy.yaml that cannot be used, and a
    # domain_policies block in a file policies are not read from, are not
    # --strict ERRORs here: both are field_type_errors, refused in every mode.)
    # #2431: a matcher value (`routes[i].match.<label>`, `overrides[i].
    # alertname` / `metric_group`) of the RESOLVED routing that PyYAML does
    # not read as a string is a blocking ERROR too (tenant-api's writes get
    # this verdict from the generator itself); without --strict the generator keeps its legacy
    # behaviour (routes: WARN + skip; overrides: truthiness + str()).
    # #2503: a bad group_by element (not a string as PyYAML reads it, empty,
    # repeated, `...` mixed with labels) of the resolved routing or of
    # `_routing_enforced` is a blocking ERROR too (kind
    # routing_group_by_invalid); without --strict the
    # generator drops the element with a `WARN … skipping` line instead.
    if strict_policies:
        # #2766: the strict twins of classified lines carry the same kind,
        # so a finding's kind never depends on --strict.
        for tenant in invalid_ids:  # #2341 R8
            schema_warnings.append(Finding(
                f"  {POLICY_ERROR_PREFIX} {invalid_tenant_id_text(tenant)}",
                kind="invalid_tenant_id", blocks="strict", tenant=tenant))
        # #2341 R5: the same two shapes as a blocking ERROR under --strict.
        for tenant in sorted(refused, key=str):
            schema_warnings.append(Finding(
                f"  {POLICY_ERROR_PREFIX} tenant '{tenant}': "
                f"{routing_not_mapping_text(refused[tenant])}",
                kind="routing_not_mapping", blocks="strict", tenant=tenant,
                field="_routing"))
        for fname, value in parsed.get("routing_defaults_not_mapping", []):
            schema_warnings.append(Finding(
                f"  {POLICY_ERROR_PREFIX} "
                f"{routing_defaults_not_mapping_text(fname, value)}",
                kind="routing_defaults_not_mapping", blocks="strict",
                file=fname, field="_routing_defaults"))
        for tenant, rc in sorted(routing_configs.items()):
            for fld, value in routing_values_not_string(rc):
                schema_warnings.append(Finding(
                    f"  {POLICY_ERROR_PREFIX} "
                    f"{value_not_string_message(tenant, fld, value)}",
                    kind="routing_value_not_string", blocks="strict",
                    tenant=tenant, field=fld))
            for fld, kind, value in routing_group_by_invalid(rc):
                schema_warnings.append(Finding(
                    f"  {POLICY_ERROR_PREFIX} tenant '{tenant}': "
                    f"{group_by_problem_text(fld, kind, value)}",
                    kind="routing_group_by_invalid", blocks="strict",
                    tenant=tenant, field=fld))
        # Only the enforced routes the generator renders, the `{{tenant}}`
        # shape per tenant after substitution (#2503 round 2, F1 / F4) — over
        # every tenant it recognises, routed or not (#2519).
        for ctx, idx, kind, value in enforced_group_by_problems(
                parsed["enforced_routing"], enforced_route_tenants(
                    routing_configs, parsed["dedup_configs"])):
            schema_warnings.append(Finding(
                f"  {POLICY_ERROR_PREFIX} {ctx}: "
                f"{group_by_problem_text(f'group_by[{idx}]', kind, value)}",
                kind="routing_group_by_invalid", blocks="strict",
                field=f"_routing_enforced.group_by[{idx}]"))

    return TenantTree(
        routing_configs, parsed["dedup_configs"], schema_warnings,
        parsed["enforced_routing"],
        files_read=parsed.get("files_read", 0),
        files_skipped=list(parsed.get("files_skipped", [])),
        tenant_file_errors=list(parsed.get("tenant_file_errors", [])),
        routing_tree_problems=tree_problems,
        duplicate_tenants=dict(parsed.get("duplicate_tenants", {})),
        invalid_tenant_ids=invalid_ids,
        field_type_errors=field_type_errors,
        unselected_carriers=list(parsed.get("unselected_carriers", [])))


def print_reader_warnings(source: "TenantTree | dict") -> None:
    """Print to stderr the reader findings a tool that does NOT print the
    generator's warning stream must still show its user (#2906).

    *source* is a ``load_tenant_tree`` result or a ``_parse_config_files``
    dict. Today: each `unselected_defaults_carrier` line — it is a Finding
    in the stream, and the reader prints nothing of its own, so a caller
    that prints neither the stream nor this would hide it. ⛔ Not for
    generate-routes / validate-config (they print the stream: twice), and
    not for a caller whose other reader already names it (policy_engine:
    da-guard's own multi-carrier line).
    """
    lines = (source.get("unselected_carriers", []) if isinstance(source, dict)
             else source.unselected_carriers)
    for line in lines:
        print(safe_label(line), file=sys.stderr)
