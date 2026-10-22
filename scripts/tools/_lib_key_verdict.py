"""The one verdict on "does any reader read this key" (ADR-038 decisions 4–7).

Every tool that asks whether a tenant's key is read — validate-config, the
route generator's `--validate`, tenant-api's write check, `explain-keys` —
calls `judge_tree` here instead of judging key names itself (decision 3).

It joins three readings, none of them its own:

* da-guard served-values (`_lib_tenant_values.load_served_tree`, with
  `disable_effects`): whether the exporter reads each key of a tenant's
  merged config, why not, the readers the reserved-key registry declares for
  a `_` key, and what each `disable` does (decision 1, 8);
* da-guard effective (`load_effective_tree`): which layer and file each key
  of a tenant's effective config comes from (`key_sources`), so a declared
  reader counts only in a layer the registry says it reads;
* the Policy-as-Code rules the tree carries (root `_defaults.yaml`
  `_policies`) plus any the caller passes: a key a rule that applies to the
  tenant names as `target` (or `when.target`) is read by `policy_engine`
  (decision 3) — when Policy-as-Code's view holds it (a `_` key only if the
  registry lists it). A wildcard target names no key.

A key is `read` when some reader reads it (`annotation` counts), `unread`
when none does, and `undecidable` when no root platform file was read (a
threshold key's exporter half cannot be judged; `_` keys are judged as
usual, decision 7). A `_` key the registry does not list is read by no
reader — the exporter serves it in no layer (#3045) — so it is `unread`
like any other, reason `unregistered`: an error in the tenant's own file, a
notice on the platform file or profile it comes from (decision 4).
Severities are those decision 4 gives the gates once they block; callers
that only report print them as they are.

A `disable` is read when it switches something off (`effective` /
`partial`); a `noop` one is unread at `warning` (decision 4) — except on a
`_` key the registry does not list, which is a misspelt reserved key and
unread at the severity of any other unread key. A `partial`
one also yields a `disable_partial` warning. Without disable effects
(`disable_effects=False`) a switched-off key is taken as read: its effect
is not known.

The same unread key written in a platform file or a profile is ONE finding
naming the file, with the tenants it reaches (decision 4: deduplicated by
file and key).

A `_` key written in a profile is judged on its own, from the registry's
`profile` layer, whether or not a tenant binds the profile or gets its value
(a tenant's own value shadows it; `_profile` in a profile always is): a key
no reader reads in a profile is ONE notice on that file, with this judgment's
reason and the tenants that bind the profile (none for an unbound one) — it
replaces the finding the tenants' keys would give, so the reason does not
depend on who binds the profile (#3042 — the judgment validate-config's
`profiles` row made by key name before). Policy-as-Code is not counted here:
it reads a tenant's view, and a tenant that gets the value is judged above.

Exceptions are those of `_lib_tenant_values`; a caller that must not block
on them (decision 5: da-guard unavailable or failing is a notice, never a
refusal) catches `_lib_tenant_values.DaGuardError` and its kin.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterable, NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _lib_confd  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402
from _lib_constants import (  # noqa: E402
    RESERVED_KEY_READERS, RESERVED_PREFIX_READERS, RETIRED_RESERVED_KEYS)

#: da-guard effective's KeySource.layer → the reserved-key registry's layer.
#: The root `_defaults.yaml`'s `defaults:` (level 0) is no registry layer: a
#: `_` key there is read by the exporter or by nothing.
_LAYERS = {"tenant": "tenant", "platform": "platform_tenants", "profile": "profile"}

READ, UNREAD, UNDECIDABLE = "read", "unread", "undecidable"
ERROR, WARNING, NOTICE = "error", "warning", "notice"


class KeyVerdict(NamedTuple):
    tenant: str
    key: str                       # as the tenant's config spells it
    layer: str | None              # da-guard effective's layer, None when it has no source
    file: str | None               # the file whose value the tenant gets
    read_by: tuple[str, ...]       # sorted reader names; () for none
    verdict: str                   # READ / UNREAD / UNDECIDABLE
    severity: str | None           # ERROR / WARNING / NOTICE; None for READ
    reason: str | None             # served-values' reason, "disable_noop", or None
    disable: str | None            # "effective" / "partial" / "noop" / None
    served_family: tuple[str, ...]
    value: Any                     # the effective config's value (as written); None when absent
    served: Any                    # what /metrics serves for it (served-values `values`); None when nothing


class Finding(NamedTuple):
    """One thing to report: a tenant's own key, or a platform file / profile
    key reaching `tenants`."""
    severity: str
    kind: str                      # "unread" | "disable_noop" | "disable_partial"
    key: str
    file: str | None
    tenants: tuple[str, ...]
    reason: str | None
    served_family: tuple[str, ...] = ()
    served_from: tuple[str, ...] = ()   # each of served_family's files (", "-joined when the tenants differ; "?" unknown)


class TreeVerdict(NamedTuple):
    keys: dict[str, list[KeyVerdict]]   # tenant → its keys, sorted by key
    findings: list[Finding]
    undecidable: bool                   # no root platform file was read


def registered_readers(key: str) -> tuple[dict[str, tuple[str, ...]], bool]:
    """What the reserved-key registry declares for `key`: reader → layers,
    from the key's own entry, else from the first registered prefix it
    carries; and whether it is registered at all (a retired key is not).
    Go's config.RegisteredReaders over the same generated tables."""
    if key in RETIRED_RESERVED_KEYS:
        return {}, False
    if key in RESERVED_KEY_READERS:
        return dict(RESERVED_KEY_READERS[key]), True
    for prefix, read_by in RESERVED_PREFIX_READERS:
        if key.startswith(prefix):
            return dict(read_by), True
    return {}, False


def root_platform_files(conf_d: str | os.PathLike[str], reader: Any = None) -> list[str]:
    """The `_` config files directly in `conf_d` (names, sorted): the
    config files (Go's `role`) of class `defaults` / `platform` at the root
    level of *reader*'s listing — the snapshot it reads the tree from (the
    run's reader of `conf_d` when None; #3206)."""
    import _lib_parse_cache
    reader = reader if reader is not None else _lib_parse_cache.reader_for(conf_d)
    listing = _lib_confd.config_listing(conf_d, reader=reader)
    return [] if listing is None else sorted(
        e.path for e in listing.config_files() if e.depth == 0 and e.reserved)


class PolicyHead(NamedTuple):
    """The first path element of a rule's `target` / `when.target`, and the
    tenants the rule leaves out (`exclude_tenants`)."""
    head: str
    excluded: frozenset[str] = frozenset()


def policy_targets(rules: Iterable[Any]) -> list[PolicyHead]:
    """`PolicyHead` of every rule's `target` and `when.target`."""
    out = []
    for rule in rules:
        targets = [getattr(rule, "target", "")]
        when = getattr(rule, "when", None)
        if when:
            targets.append(str(when.get("target", "")))
        excluded = frozenset(str(t) for t in (getattr(rule, "exclude_tenants", None) or []))
        out += [PolicyHead(t.split(".", 1)[0], excluded) for t in targets if t]
    return out


def _named_by_policy(tenant: str, key: str, heads: list[PolicyHead],
                     aliases: dict[str, str] | None) -> bool:
    """A rule that applies to the tenant names the key itself — in either
    #1231 spelling, as Policy-as-Code compares them (both through the
    exporter's alias table). A wildcard (`mysql_*`) names no key: it would
    credit every misspelling it happens to match."""
    canon = tv.canonical_key(key, aliases)
    return any(tenant not in h.excluded and tv.canonical_key(h.head, aliases) == canon
               for h in heads)


def _reader_for(conf_d: str | os.PathLike[str], binary: str | None) -> Any:
    """The reader `judge_tree` reads `conf_d` through: the run's, or one with
    the caller's da-guard."""
    import _lib_parse_cache
    return (_lib_parse_cache.ConfdReader(conf_d, binary=binary) if binary
            else _lib_parse_cache.reader_for(conf_d))


def _tree_rules(conf_d: str | os.PathLike[str], binary: str | None = None,
                reader: Any = None) -> list[Any]:
    """The `_policies` of the root `_defaults.yaml`, as Policy-as-Code loads
    them; [] when there is none. *binary*: the da-guard to read it with (None:
    `$DA_GUARD_BINARY`, then `$PATH`)."""
    ops = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops")
    if ops not in sys.path:
        sys.path.insert(0, ops)
    import policy_engine as pe  # the engine's own loader
    # Policy-as-Code's own lookup: the root carrier only, as it reads
    # `_policies`. Not observed as a flat read — every key judged here comes
    # from da-guard's reading of the whole tree, and the root-only
    # `_policies` gap is Policy-as-Code's to report (validate-config's
    # `policy_dsl` row, policy-engine itself).
    # ADR-036 step 4 (#2906): read as the rest of the tree is — through
    # `da-guard parse-tree` (the run's reader of `conf_d`, or one with the
    # caller's binary), not PyYAML; and the carrier is the one parse-tree
    # says the root's chain selects (#2906 review), not a Python selection.
    reader = reader if reader is not None else _reader_for(conf_d, binary)
    path = reader.carrier(conf_d)
    # #3206: no `is_file()` here — it answered False for a carrier it may not
    # stat and so read "no policies". The carrier is parse-tree's selection;
    # one whose read fails fails the read.
    if path is None:
        return []
    return pe.load_policies(str(path), config_dir=str(conf_d), reader=reader)


def note_root_only_policies(conf_d: str | os.PathLike[str], reader: Any = None) -> None:
    """Print, once, Policy-as-Code's own note naming the subdirectory
    `_defaults.yaml` files whose `_policies` it does not read — and so this
    verdict does not count — for a caller with no Policy-as-Code row of its
    own to say it (explain-keys). Nothing when there is none. *reader*: the
    caller's reader of `conf_d` — the one `judge_tree` reads too (#3206)."""
    ops = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops")
    if ops not in sys.path:
        sys.path.insert(0, ops)
    import policy_engine as pe
    _lib_confd.resolve_defaults_file(Path(conf_d), skipped_note=pe.POLICIES_SKIPPED_NOTE,
                                     reader=reader if reader is not None
                                     else _reader_for(conf_d, None))


def judge_tree(conf_d: str | os.PathLike[str], *, at: str | None = None, binary: str | None = None,
               disable_effects: bool = True, rules: Iterable[Any] | None = None,
               timeout: float = tv.DEFAULT_TIMEOUT,
               effective_tree: tv.EffectiveTree | None = None,
               reader: Any = None) -> TreeVerdict:
    """The verdict on every key of every tenant of the tree at `conf_d`.
    `effective_tree`, given, is a `load_effective_tree` of the same tree the
    caller already has (validate-config shares one run among its rows);
    `reader`, given, is the caller's `_lib_parse_cache.ConfdReader` of it
    (#3206: one parse-tree document for the run)."""
    tree = tv.load_served_tree(conf_d, at=at, binary=binary, timeout=timeout,
                               disable_effects=disable_effects)
    effective = (effective_tree or tv.load_effective_tree(conf_d, binary=binary, timeout=timeout)).tenants
    if reader is None:
        reader = _reader_for(conf_d, binary)  # one snapshot for the rules and the root files
    all_rules = list(_tree_rules(conf_d, binary, reader)) + list(rules or [])
    heads = policy_targets(all_rules)
    verdict = judge(tree.tenants, effective, heads, aliases=tree.aliases,
                    undecidable=not root_platform_files(conf_d, reader),
                    disable_effects=disable_effects)
    by_profile = profile_layer_findings(_root_profiles(conf_d, reader), effective)
    return verdict._replace(findings=_merge_profile_findings(verdict, by_profile))


def _merge_profile_findings(verdict: TreeVerdict, by_profile: list[Finding]) -> list[Finding]:
    """`verdict.findings` with the profile-layer judgment taking the place of
    a tenant-path `unread` finding that only profile-layer keys gave — the
    same (file, key) — and adding the rest. A (file, key) the tenants also
    get from a root file's `tenants:` keeps the tenant-path finding; one some
    tenant's reader reads (a Policy-as-Code rule naming it) is no finding."""
    layers: dict[tuple[str | None, str], set[str | None]] = {}
    read = set()
    for rows in verdict.keys.values():
        for v in rows:
            if v.verdict == UNREAD:
                layers.setdefault((v.file, v.key), set()).add(v.layer)
            elif v.verdict == READ and v.layer == "profile":
                read.add((v.file, v.key))
    by_profile = [f for f in by_profile if (f.file, f.key) not in read]
    profile_only = {fk for fk, ls in layers.items() if ls == {"profile"}}
    ours = {(f.file, f.key) for f in by_profile}
    kept = [f for f in verdict.findings
            if not (f.kind == UNREAD and (f.file, f.key) in profile_only and (f.file, f.key) in ours)]
    theirs = {(f.file, f.key) for f in kept if f.kind == UNREAD}
    return _sorted_findings(kept + [f for f in by_profile if (f.file, f.key) not in theirs])


def _root_profiles(conf_d: str | os.PathLike[str], reader: Any) -> dict[str, dict[str, Any]]:
    """file → the `profiles:` mapping of each root platform file that has
    one (the files the exporter takes profiles from), read through `da-guard
    parse-tree` as the rest of the tree is (ADR-036 step 4) — by *reader*,
    the run's one snapshot (#3206), which also names the files."""
    import _lib_parse_cache
    out = {}
    for name in root_platform_files(conf_d, reader):
        try:
            doc = reader.read(Path(conf_d) / name)
        except _lib_parse_cache.NotSelected:
            continue  # a root defaults spelling parse-tree does not select: never read
        profiles = doc.get("profiles") if isinstance(doc, dict) else None
        if isinstance(profiles, dict):
            out[name] = profiles
    return out


def profile_layer_findings(profiles_by_file: dict[str, dict[str, Any]],
                           effective: dict[str, tv.TenantEffective]) -> list[Finding]:
    """The notices for `_` keys written in a profile that no reader reads in
    the `profile` layer (module docstring)."""
    found: dict[tuple[str, str], tuple[str, set[str]]] = {}
    for file, profiles in sorted(profiles_by_file.items()):
        for name, body in profiles.items():
            if not isinstance(body, dict):
                continue
            bound = {t for t, te in effective.items() if te.profile == str(name)}
            for key in body:
                key = str(key)
                if not key.startswith("_"):
                    continue
                declared, registered = registered_readers(key)
                if any("profile" in layers for layers in declared.values()):
                    continue
                reason = ("retired" if key in RETIRED_RESERVED_KEYS
                          else "not_read_in_profile" if registered else "unregistered")
                found.setdefault((file, key), (reason, set()))[1].update(bound)
    return [Finding(NOTICE, UNREAD, key, file, tuple(sorted(tenants)), reason)
            for (file, key), (reason, tenants) in found.items()]


def _sorted_findings(findings: list[Finding]) -> list[Finding]:
    order = {ERROR: 0, WARNING: 1, NOTICE: 2}
    return sorted(findings, key=lambda f: (order.get(f.severity, 3), f.file or "", f.key, f.kind, f.tenants))


def judge(served: dict[str, tv.TenantValues], effective: dict[str, tv.TenantEffective],
          heads: list[PolicyHead], *, undecidable: bool, disable_effects: bool = True,
          aliases: dict[str, str] | None = None) -> TreeVerdict:
    """`judge_tree` over readings already in hand."""
    keys: dict[str, list[KeyVerdict]] = {}
    root_defaults: dict[str, str] = {}
    for tenant in sorted(set(served) | set(effective)):
        if tenant in effective and effective[tenant].defaults_chain:
            root_defaults[tenant] = effective[tenant].defaults_chain[0]
        sv, ef = served.get(tenant), effective.get(tenant)
        sk = sv.keys if sv is not None else {}
        cfg = ef.effective_config if ef is not None else {}
        sources = ef.key_sources if ef is not None else {}
        rows = []
        for key in sorted(set(sk) | set(cfg)):
            canon = tv.canonical_key(key, aliases)
            # /metrics rows: a threshold reading (`severities`) or a dropped
            # row. Not `values` alone: it lists every tenant's `_silent_mode`,
            # `_metadata`, … whether or not anything set them.
            has_rows = sv is not None and any(
                k in sv.severities or k in sv.dropped for k in (key, canon))
            served_value = (sv.values.get(key, sv.values.get(canon)) if sv is not None else None)
            rows.append(_judge_key(tenant, key, sk.get(key), sources.get(key), cfg.get(key),
                                   served_value, has_rows, heads, undecidable, disable_effects,
                                   aliases))
        keys[tenant] = rows
    return TreeVerdict(keys, _findings(keys, root_defaults, aliases), undecidable)


def _judge_key(tenant: str, key: str, sk: tv.ServedKey | None, src: tv.KeySource | None, value: Any,
               served_value: Any, has_rows: bool, heads: list[PolicyHead], undecidable: bool,
               disable_effects: bool, aliases: dict[str, str] | None = None) -> KeyVerdict:
    layer = src.layer if src is not None else None
    file = src.file if src is not None else None
    reg_layer = _LAYERS.get(layer or "")
    if layer == "defaults" and src is not None and (src.level or 0) > 0:
        reg_layer = "subtree_defaults"

    read_by: set[str] = set()
    reason = sk.reason if sk is not None else None
    disable = sk.disable if sk is not None else None
    family = sk.served_family if sk is not None else ()
    if sk is not None:
        declared, registered = sk.declared, sk.registered
    else:
        # A key the exporter's build does not take into the tenant's config
        # (no `keys` entry): the registry's own declaration.
        declared, registered = registered_readers(key) if key.startswith("_") else ({}, None)
    misspelt_reserved = key.startswith("_") and registered is False
    if sk is not None and sk.exporter:
        read_by.add("exporter")
    elif sk is None and has_rows:
        # Not a key of the tenant's merged config: a threshold it inherits,
        # which /metrics serves for it.
        read_by.add("exporter")
    elif sk is None and key.startswith("_") and reg_layer in declared.get("exporter", ()) \
            and served_value is not None:
        # A reserved key the tenant inherits from a layer the exporter reads
        # it in (`_state_<f>`, `_severity_dedup` from a subtree
        # `_defaults.yaml`), which the exporter's resolver read.
        read_by.add("exporter")
    elif sk is None and key.startswith("_state_") and served_value is None:
        # Inherited `_state_<filter>` with no reading: the filter is not
        # declared where the exporter reads declarations (decision 7).
        reason = "state_filter_undeclared"
    if sk is not None and reason == "disabled":
        unknown = disable is None and not disable_effects and not misspelt_reserved
        if disable in ("effective", "partial") or unknown:
            read_by.add("exporter")
            reason = None
    for reader, layers in declared.items():
        if reader != "exporter" and reg_layer in layers:
            read_by.add(reader)
    # Policy-as-Code reads what its view holds (policy_engine.load_policy_inputs):
    # every threshold key of the tenant's config (served, or as written) and
    # the reserved keys a tenant may carry. A rule naming a `_` key outside
    # that view finds nothing there.
    in_policy_view = not key.startswith("_") or bool(registered)
    if in_policy_view and _named_by_policy(tenant, key, heads, aliases):
        read_by.add("policy_engine")

    platform_side = layer in ("platform", "profile", "defaults")
    if read_by:
        verdict, severity, reason = READ, None, None
    elif undecidable and not key.startswith("_"):
        verdict, severity = UNDECIDABLE, None
    else:
        verdict = UNREAD
        if misspelt_reserved:
            # A `_` key the registry does not list is a misspelt reserved key
            # (`_routng: disable`), not a switch-off with nothing to switch.
            severity = NOTICE if platform_side else ERROR
            reason = "retired" if reason == "retired" else "unregistered"
        elif disable == "noop":
            severity, reason = WARNING, "disable_noop"
        elif platform_side or reason == "state_filter_undeclared":
            # A `_state_<filter>` the root does not declare: the missing
            # declaration is the platform's (decision 7).
            severity = NOTICE
        else:
            severity = ERROR
        if reason is None and sk is None:
            reason = "not_served"
    return KeyVerdict(tenant, key, layer, file, tuple(sorted(read_by)), verdict, severity, reason,
                      disable, tuple(family), value, served_value)


def _findings(keys: dict[str, list[KeyVerdict]], root_defaults: dict[str, str],
              aliases: dict[str, str] | None = None) -> list[Finding]:
    """Deduplicated by (kind, file, key) for platform-side keys; one per
    tenant key for the tenant's own file. An undeclared state filter is the
    platform's: one finding per key, on the root `_defaults.yaml` that
    should declare it, with every tenant it reaches — never charged to a
    tenant's own file (decision 6, 7)."""
    groups: dict[tuple, list[KeyVerdict]] = {}
    # (tenant, key) → {sibling: its file} for the siblings a `partial` reports
    inherited: dict[tuple[str, str], dict[str, str | None]] = {}
    for rows in keys.values():
        # served-values names a family in the exporter's canonical spelling;
        # the tenant's rows keep the spelling each file writes (#1231).
        by_key = {tv.canonical_key(v.key, aliases): v for v in rows}
        for v in rows:
            kinds = []
            if v.verdict == UNREAD:
                kinds.append("disable_noop" if v.reason == "disable_noop" else v.verdict)
            if v.disable == "partial":
                # #3046: a sibling the tenant's own file writes is the
                # tenant's choice (`X: disable` + `X_critical: 90` keeps the
                # critical tier only); one it inherits — a subtree
                # `_defaults.yaml`, a profile, a root platform file, the root
                # defaults' dimensional keys — still serving after the base
                # is switched off is the surprise to report.
                fam = {k: getattr(by_key.get(tv.canonical_key(k, aliases)), "file", None)
                       for k in v.served_family
                       if getattr(by_key.get(tv.canonical_key(k, aliases)), "layer", None) != "tenant"}
                if fam:
                    kinds.append("disable_partial")
                    inherited[(v.tenant, v.key)] = fam
            for kind in kinds:
                if v.reason == "state_filter_undeclared":
                    # The platform's to fix: `state_filters:` is declared in
                    # the root `_defaults.yaml` only, never the tenant's own.
                    gk = (kind, root_defaults.get(v.tenant), v.key, None)
                else:
                    own = v.layer == "tenant"
                    gk = (kind, v.file, v.key, v.tenant if own else None)
                groups.setdefault(gk, []).append(v)
    out = []
    for (kind, file, key, _), vs in groups.items():
        sev = WARNING if kind == "disable_partial" else vs[0].severity or NOTICE
        # One finding per platform file and key: the tenants it reaches may
        # inherit different siblings, or one from different files — every
        # sibling any of them still serves is listed, with every file it
        # comes from, joined.
        fam, src = (), ()
        if kind == "disable_partial":
            per = [inherited[(v.tenant, v.key)] for v in vs]
            fam = tuple(dict.fromkeys(k for p in per for k in p))
            src = tuple(", ".join(sorted({p[k] or "?" for p in per if k in p})) for k in fam)
        out.append(Finding(sev, kind, key, file, tuple(sorted({v.tenant for v in vs})),
                           None if kind == "disable_partial" else vs[0].reason, fam, src))
    return _sorted_findings(out)
