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


def root_platform_files(conf_d: str | os.PathLike[str]) -> list[str]:
    """The `_` config files directly in `conf_d` (names, sorted): the shared
    walk of the whole tree (hidden entries left out as everywhere), kept to
    the root level."""
    root = Path(conf_d)
    return sorted(p.name for p in _lib_confd.iter_config_files(conf_d)
                  if p.parent == root and _lib_confd.is_reserved_name(p.name))


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


def _tree_rules(conf_d: str | os.PathLike[str]) -> list[Any]:
    """The `_policies` of the root `_defaults.yaml`, as Policy-as-Code loads
    them; [] when there is none."""
    ops = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops")
    if ops not in sys.path:
        sys.path.insert(0, ops)
    import policy_engine as pe  # the engine's own loader
    # Policy-as-Code's own lookup: the root carrier only, as it reads
    # `_policies`. Not observed as a flat read — every key judged here comes
    # from da-guard's reading of the whole tree, and the root-only
    # `_policies` gap is Policy-as-Code's to report (validate-config's
    # `policy_dsl` row, policy-engine itself).
    path = _lib_confd.resolve_defaults_file(Path(conf_d), observed=False)
    return pe.load_policies(str(path)) if path.is_file() else []


def note_root_only_policies(conf_d: str | os.PathLike[str]) -> None:
    """Print, once, Policy-as-Code's own note naming the subdirectory
    `_defaults.yaml` files whose `_policies` it does not read — and so this
    verdict does not count — for a caller with no Policy-as-Code row of its
    own to say it (explain-keys). Nothing when there is none."""
    ops = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops")
    if ops not in sys.path:
        sys.path.insert(0, ops)
    import policy_engine as pe
    _lib_confd.resolve_defaults_file(Path(conf_d), skipped_note=pe.POLICIES_SKIPPED_NOTE)


def judge_tree(conf_d: str | os.PathLike[str], *, at: str | None = None, binary: str | None = None,
               disable_effects: bool = True, rules: Iterable[Any] | None = None,
               timeout: float = tv.DEFAULT_TIMEOUT,
               effective_tree: tv.EffectiveTree | None = None) -> TreeVerdict:
    """The verdict on every key of every tenant of the tree at `conf_d`.
    `effective_tree`, given, is a `load_effective_tree` of the same tree the
    caller already has (validate-config shares one run among its rows)."""
    tree = tv.load_served_tree(conf_d, at=at, binary=binary, timeout=timeout,
                               disable_effects=disable_effects)
    effective = (effective_tree or tv.load_effective_tree(conf_d, binary=binary, timeout=timeout)).tenants
    all_rules = list(_tree_rules(conf_d)) + list(rules or [])
    return judge(tree.tenants, effective, policy_targets(all_rules), aliases=tree.aliases,
                 undecidable=not root_platform_files(conf_d), disable_effects=disable_effects)


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
    order = {ERROR: 0, WARNING: 1, NOTICE: 2}
    out.sort(key=lambda f: (order.get(f.severity, 3), f.file or "", f.key, f.kind, f.tenants))
    return out
