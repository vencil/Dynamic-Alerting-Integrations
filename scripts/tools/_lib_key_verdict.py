"""The shared verdict on "does any reader read this key" (ADR-038), as
`da-guard key-verdict` gives it.

⛔ PYTHON JUDGES NOTHING HERE. The verdict is Go's (`pkg/keyverdict`, #3489):
da-guard reads the tree — what the exporter serves, which file and layer each
key of a tenant comes from, the reserved-key registry's readers, the
Policy-as-Code rules of the root carrier, the root platform files'
`profiles:` — and judges every key of every tenant. This module runs it and
hands the document to the tools that report it (the route generator's
`--validate`, which is validate-config's `routes` row; `explain-keys`; the
generator container's write check) in one shape, and formats the lines they
print.

A key is `read` when some reader reads it in some part of the UTC day,
`unread` when none does all day (#3835: the verdict is the whole day's, and
`--at` decides only what has expired — `expires:` — never the time of day),
and `undecidable` when no root platform file was read (a threshold key's exporter
half cannot be judged; `_` keys are judged as usual, decision 7). Severities
describe the defect (decision 4); whether a whole-tree gate refuses the
tree is the document's `blocks`, Go's one rule (#3016) — callers read it,
none restates "an error blocks". A finding is a tenant's own key, or a platform
file's or profile's key with the tenants it reaches (one per file and key).

Policy-as-Code's rules are its own to validate: `judge_tree` loads them with
`policy_engine` first, so a tree whose rules policy_engine refuses
(`InvalidPolicyRules`) is judged by no one, as before — da-guard, which takes
any rule's `target` it can read, is not asked.

Exceptions are those of `_lib_tenant_values`' da-guard readers
(`DaGuardNotFoundError`, `KeyVerdictError` — a `DaGuardError` —,
`ParseFailedError` for a file the exporter's load drops) and policy_engine's
`InvalidPolicyRules`; a caller that must not block on them (decision 5:
da-guard unavailable or failing is a notice, never a refusal) catches them.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _lib_confd  # noqa: E402
from _lib_validation import shown_key, shown_value  # noqa: E402  (#1560 ⑤-b: keys and values into messages)
import _lib_tenant_values as tv  # noqa: E402

SUBCOMMAND = "key-verdict"
SCHEMA = "da-guard.key-verdict/v4"
#: Earlier versions of the document: a da-guard writing one is older than
#: this tool (v1: the ERROR layer only; v2: judged at one instant, each row
#: with what /metrics served then — #3835; v3: `key_checks` as sentences
#: that quoted the key — #3703).
OLDER_SCHEMAS = ("da-guard.key-verdict/v1", "da-guard.key-verdict/v2", "da-guard.key-verdict/v3")

READ, UNREAD, UNDECIDABLE = "read", "unread", "undecidable"
ERROR, WARNING, NOTICE = "error", "warning", "notice"


class KeyVerdictError(tv.DaGuardError):
    """da-guard key-verdict failed."""


class KeyVerdict(NamedTuple):
    tenant: str
    key: str                       # as the tenant's config spells it
    layer: str | None              # da-guard effective's layer, None when it has no source
    file: str | None               # the file whose value the tenant gets
    read_by: tuple[str, ...]       # sorted reader names; () for none
    verdict: str                   # READ / UNREAD / UNDECIDABLE
    severity: str | None           # ERROR / WARNING / NOTICE; None for READ
    reason: str | None             # the whole day's reason (never a value's, #3835), "disable_noop", or None
    disable: str | None            # "effective" / "noop" / None
    switched_off_with: str | None  # the base whose `disable` switches it off (#3046), or None
    value: Any                     # the effective config's value (as written); None when absent
    # (v3, #3835: no `served` — what /metrics serves at one instant is
    # served-values'.)
    # #3502: the entries of its schedule /metrics serves at no minute of the day
    dead_entries: "tuple[tv.DeadEntry, ...]" = ()


class Finding(NamedTuple):
    """One thing to report: a tenant's own key, or a platform file / profile
    key reaching `tenants`."""
    severity: str
    kind: str                      # one of _KINDS
    key: str
    file: str | None
    tenants: tuple[str, ...]
    reason: str | None
    blocks: bool = False                # a whole-tree gate refuses it (Go's keyverdict.Blocking)
    base: str | None = None             # disable_cascade: the base whose `disable` switches it off
    dead_entries: "tuple[tv.DeadEntry, ...]" = ()  # dead_entry (#3502): its entries, in the order written
    # #3829: a retired threshold key's hint (reason `retired`), naming the key
    # that replaces it; None for any other finding.
    hint: str | None = None


class TreeVerdict(NamedTuple):
    keys: dict[str, list[KeyVerdict]]   # tenant → its keys, sorted by key
    findings: list[Finding]
    undecidable: bool                   # no root platform file was read
    # ADR-038 §11: per tenant, the exporter's own checks on its keys'
    # `version` labels and retired spellings (served-values' key_checks,
    # `tv.KeyCheck` records), which a CI reader prints with
    # `key_check_line`. Not part of the verdict on whether a key is read.
    key_checks: "dict[str, tuple[tv.KeyCheck, ...]]" = {}
    # A gate that judges the whole tree refuses it: some finding blocks, or
    # some key is undecidable (Go's keyverdict.Blocks, #3016). The one
    # answer validate-config and the route generator's `--validate` read.
    blocks: bool = False
    # Some key is undecidable, and so blocks (Go's Tree.UndecidableBlocks):
    # what the line of undecidable keys says, read, never inferred.
    undecidable_blocks: bool = False


def key_check_lines(verdict: TreeVerdict) -> "list[tuple[str, tv.KeyCheck]]":
    """The exporter's key checks of `verdict`, as (tenant, check) sorted by
    tenant, each tenant's in Go's order (by key, then kind). Every judged
    tenant has them: key-verdict's document always carries `key_checks` (one
    without it is refused as another version's)."""
    return [(tenant, c) for tenant in sorted(verdict.key_checks) for c in verdict.key_checks[tenant]]


def key_check_line(tenant: str, c: "tv.KeyCheck") -> str:
    """The one line a report prints for key check *c* of *tenant* (#3703):
    a constant sentence around the record's fields — the level (WARN for a
    `version` label the exporter refuses, NOTICE for a retired spelling), the
    tenant, the kind, the metric name, the key and the replacing metric name
    each through `shown_key` (a key with labels is never name-like, so it is
    its stand-in), and Go's constant `reason`. Nothing the author wrote is
    printed as it is unless `shown_key` lets it."""
    renamed = c.kind in tv.KEY_CHECK_RENAMED_KINDS
    replacement = (f", replacement metric {shown_key(c.replacement_head)}"
                   if c.replacement_head is not None else "")
    return (f"{'NOTICE' if renamed else 'WARN'}: tenant={shown_key(tenant)}: key check {c.kind}: "
            f"metric {shown_key(c.head)}, key {shown_key(c.key)}{replacement} — {c.reason}")


def _reader_for(conf_d: str | os.PathLike[str], binary: str | None) -> Any:
    """The reader the rules are read through: the run's, or one with the
    caller's da-guard."""
    import _lib_parse_cache
    return (_lib_parse_cache.ConfdReader(conf_d, binary=binary) if binary
            else _lib_parse_cache.reader_for(conf_d))


def _ops_on_path() -> None:
    ops = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops")
    if ops not in sys.path:
        sys.path.insert(0, ops)


def check_tree_rules(conf_d: str | os.PathLike[str], binary: str | None = None,
                     reader: Any = None) -> None:
    """Load the root carrier's `_policies` as Policy-as-Code does — through
    `da-guard parse-tree` (ADR-036 step 4), the carrier parse-tree says the
    root's chain selects — and raise its `InvalidPolicyRules` for rules it
    cannot read. Nothing is returned: the verdict takes the rules from
    da-guard's own read of the same carrier."""
    _ops_on_path()
    import policy_engine as pe  # the engine's own loader
    reader = reader if reader is not None else _reader_for(conf_d, binary)
    path = reader.carrier(conf_d)
    if path is not None:
        pe.load_policies(str(path), config_dir=str(conf_d), reader=reader)


def note_root_only_policies(conf_d: str | os.PathLike[str], reader: Any = None) -> None:
    """Print, once, Policy-as-Code's own note naming the subdirectory
    `_defaults.yaml` files whose `_policies` it does not read — and so this
    verdict does not count — for a caller with no Policy-as-Code row of its
    own to say it (explain-keys). Nothing when there is none. *reader*: the
    caller's reader of `conf_d` (#3206)."""
    _ops_on_path()
    import policy_engine as pe
    _lib_confd.resolve_defaults_file(Path(conf_d), skipped_note=pe.POLICIES_SKIPPED_NOTE,
                                     reader=reader if reader is not None
                                     else _reader_for(conf_d, None))


_VERDICTS = frozenset({READ, UNREAD, UNDECIDABLE})
_SEVERITIES = frozenset({ERROR, WARNING, NOTICE})
_KINDS = frozenset({"unread", "disable_noop", "disable_cascade", "profile_not_judged", "value_unread",
                    "dead_entry", "schedule_unread"})
_DISABLES = frozenset({"effective", "noop"})


def _str(v: Any, what: str) -> str:
    if not isinstance(v, str):
        raise TypeError(f"{what}: not a string: {v!r}")
    return v


def _tuple(v: Any, what: str) -> tuple[str, ...]:
    if not isinstance(v, list):
        raise TypeError(f"{what}: not a list: {v!r}")
    return tuple(_str(x, what) for x in v)


def _optional(v: Any, what: str, allowed: "frozenset[str] | None" = None) -> str | None:
    if v is None:
        return None
    return _one_of(v, what, allowed) if allowed is not None else _str(v, what)


def _one_of(v: Any, what: str, allowed: "frozenset[str]") -> str:
    if v not in allowed:
        raise ValueError(f"{what}: {v!r} is none of {sorted(allowed)}")
    return v


def _bool(v: Any, what: str) -> bool:
    if not isinstance(v, bool):
        raise TypeError(f"{what}: not a boolean: {v!r}")
    return v


def _mapping(v: Any, what: str) -> dict:
    if not isinstance(v, dict):
        raise TypeError(f"{what}: not an object: {v!r}")
    return v


def _rows(v: Any, what: str) -> list:
    if not isinstance(v, list) or not all(isinstance(r, dict) for r in v):
        raise TypeError(f"{what}: not a list of objects: {v!r}")
    return v


def _key_verdict(tenant: str, r: dict) -> KeyVerdict:
    w = f"tenants.{tenant}"
    verdict = _one_of(r["verdict"], f"{w}.verdict", _VERDICTS)
    severity = _optional(r["severity"], f"{w}.severity", _SEVERITIES)
    # A read key has no severity; an unread one has one (undecidable: none).
    if (verdict == UNREAD) != (severity is not None):
        raise ValueError(f"{w}: verdict {verdict!r} with severity {severity!r}")
    return KeyVerdict(tenant, _str(r["key"], f"{w}.key"), _optional(r["layer"], f"{w}.layer"),
                      _optional(r["file"], f"{w}.file"), _tuple(r["read_by"], f"{w}.read_by"), verdict,
                      severity, _optional(r["reason"], f"{w}.reason"),
                      _optional(r["disable"], f"{w}.disable", _DISABLES),
                      _optional(r["switched_off_with"], f"{w}.switched_off_with"), r["value"],
                      tv.dead_entries(r["dead_entries"], f"{w}.dead_entries"))


def _finding(f: dict) -> Finding:
    return Finding(_one_of(f["severity"], "findings.severity", _SEVERITIES),
                   _one_of(f["kind"], "findings.kind", _KINDS), _str(f["key"], "findings.key"),
                   _optional(f["file"], "findings.file"), _tuple(f["tenants"], "findings.tenants"),
                   _optional(f["reason"], "findings.reason"), _bool(f["blocks"], "findings.blocks"),
                   _optional(f["base"], "findings.base"), _finding_dead_entries(f),
                   _optional(f["hint"], "findings.hint"))


def _finding_dead_entries(f: dict) -> "tuple[tv.DeadEntry, ...]":
    """A finding's `dead_entries`: a non-empty list on a dead_entry, null on
    any other kind (#3502)."""
    v = f["dead_entries"]
    if (f["kind"] == "dead_entry") != (v is not None):
        raise ValueError(f"findings: kind {f['kind']!r} with dead_entries {v!r}")
    out = tv.dead_entries(v, "findings.dead_entries") if v is not None else ()
    if f["kind"] == "dead_entry" and not out:
        raise ValueError("findings: a dead_entry without entries")
    return out


def _read(doc: dict[str, Any], requested: "list[str] | None") -> TreeVerdict:
    """The document as a TreeVerdict. A field missing, of another type, or
    with a value outside its closed set is output that is not the JSON it
    should be (KeyError / TypeError / ValueError, which `_run_da_guard`
    reports as such) — never read as some other answer: a verdict this
    module cannot read is not "nothing unread"."""
    tv._subset_absent(doc, requested)
    if not isinstance(doc["undecidable"], bool):
        raise TypeError(f"undecidable: {doc['undecidable']!r}")
    blocks = _bool(doc["blocks"], "blocks")
    undecidable_blocks = _bool(doc["undecidable_blocks"], "undecidable_blocks")
    keys = {_str(t, "tenants"): [_key_verdict(t, r) for r in _rows(rows, f"tenants.{t}")]
            for t, rows in _mapping(doc["tenants"], "tenants").items()}
    findings = [_finding(f) for f in _rows(doc["findings"], "findings")]
    # v4 (#3703): records; the sentences of v3 are named by the schema.
    checks = {_str(t, "key_checks"): tv._key_checks(c, f"key_checks.{t}", older_shape_is_older=False)
              for t, c in _mapping(doc["key_checks"], "key_checks").items()}
    return TreeVerdict(keys, findings, doc["undecidable"], checks, blocks, undecidable_blocks)


def judge_tree(conf_d: str | os.PathLike[str], *, at: str | None = None, binary: str | None = None,
               timeout: float = tv.DEFAULT_TIMEOUT, tenants: Iterable[str] | None = None,
               reader: Any = None) -> TreeVerdict:
    """The verdict on every key of every tenant of the tree at `conf_d` over
    the whole UTC day, `expires:` read at `at` (RFC3339; None = now) — the
    time of day changes nothing (#3835) — as `da-guard key-verdict` gives it.

    With `tenants` (ADR-038 §5: a write's tenants), the verdict is on those
    tenants' keys only (`--tenant`); the platform files are read whole as
    always. A requested tenant the tree has none for has no key. A profile's
    finding is judged over the requested tenants only.

    *binary* / *timeout*: the da-guard to run (None: `$DA_GUARD_BINARY`, then
    `$PATH`) and how long it may take. *reader*: the caller's
    `_lib_parse_cache.ConfdReader` of `conf_d`, for the rules check (#3206:
    one parse-tree document for the run)."""
    extra, requested = tv._subset_args(tenants)
    check_tree_rules(conf_d, binary, reader)
    if at is not None:
        extra = ["--at", at, *extra]
    # #3835: every earlier shape of this document (no `blocks`, no `hint`, a
    # `partial` disable, …) was written under v1 or v2 and is named older by
    # its schema (OLDER_SCHEMAS). Every da-guard that writes v3 writes every
    # field: a v3 document without one is broken output, never an older
    # binary — so no field-by-field "added since" reading here.
    verdict, _rc, _stderr = tv._run_da_guard(
        SUBCOMMAND, extra, conf_d, binary, timeout, KeyVerdictError, schema=SCHEMA,
        read=lambda doc: _read(doc, requested), reads_unreadable=True, older_schemas=OLDER_SCHEMAS,
        fields_added_since=())
    return verdict


#: A schedule window in the one spelling that can hold nothing but a time
#: of day: digits, `:` and `-`.
_WINDOW_RE = re.compile(r"[0-9]{1,2}:[0-9]{2}-[0-9]{1,2}:[0-9]{2}")


def _shown_window(window: str) -> str:
    """A schedule entry's `window:` as a message names it (#3703): as
    written when it is `HH:MM-HH:MM` in ASCII digits, otherwise
    `shown_value`'s reading of it — a window is the author's text, and the
    exporter reads spellings (`+2:00 - 06:00`) this does not print."""
    return window if _WINDOW_RE.fullmatch(window) else shown_value(window)


def finding_line(f: Finding, limit: int) -> str:
    """The one line a report prints for `f` (the route generator's
    `--validate`, validate-config's `routes` row): the file, the severity, the
    kind, the key, the tenants it reaches (at most `limit` named).

    ⛔ #3703: `reason` and `hint` are da-guard's own sentences — built from
    constants, never from a value or a key of the config (Go:
    `LayerValueProblem.Problem`) — and are printed as they are; no filter of
    prose here. What the author wrote (the key, a dead entry's value and
    window) goes through `shown_key` / `shown_value`. The guard is
    tests/ops/test_reserved_value_canary_matrix.py."""
    who = ", ".join(f.tenants[:limit]) + (
        f" (+{len(f.tenants) - limit} more)" if len(f.tenants) > limit else "")
    why = f" ({f.reason})" if f.reason else ""
    if f.hint:
        # #3829: a retired threshold key — name what replaces it.
        why = f" ({f.reason}: {f.hint})"
    if f.base:
        # #3046: name the base whose `disable` switches the key off.
        why = f" (switched off with its base `{shown_key(f.base)}`)"
    if f.dead_entries:
        # #3502: name each entry that never serves. #3703: its value and its
        # window are the author's text — `entry` (`default`, `overrides[i]`)
        # and `reason` are da-guard's own words.
        why = " (never served: " + "; ".join(
            f"{d.entry}" + (f" `{_shown_window(d.window)}`" if d.window is not None else "")
            + f" → {shown_value(d.value)}: {d.reason}"
            for d in f.dead_entries) + ")"
    # A profile key no tenant binds names none (#3042).
    who = f"tenant(s) {who}" if who else "no tenant"
    # #1560 ⑤-b: a key is the author's text and may be a URL.
    return f"{f.file or '?'}: {f.severity}: {f.kind} `{shown_key(f.key)}`{why} — {who}"


def undecidable_line(verdict: TreeVerdict) -> str | None:
    """The line saying the threshold keys were not judged (decision 7), or
    None when a root platform file was read."""
    if not verdict.undecidable:
        return None
    n = sum(v.verdict == UNDECIDABLE for vs in verdict.keys.values() for v in vs)
    return (f"no root platform file was read: {n} threshold key(s) cannot be judged "
            "(undecidable)")
