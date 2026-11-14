#!/usr/bin/env python3
"""_registry_lib.py — threshold-registry SoT loader / validator / query lib (TRK-339 WS1a / #1200).

Epic #1200 D2 (LOCKED, 2026-07-23): the threshold contract gets a standalone
YAML registry as its Source of Truth — language-neutral (helm/docs/portal can
read it directly, no Python import), JSON-Schema validatable (confd-schema
family gate). ``rule-packs/threshold-registry.yaml`` is that registry;
``docs/schemas/threshold-registry.schema.json`` is its shape contract.

GENERATED SURFACES: previously hand-copied surfaces are generated from the
registry inside delimited blocks (everything outside the delimiters stays
hand-written):

  1. rule-pack header threshold sections (the "對應的 threshold-exporter
     defaults" block of every threshold pack, defaults tier + up to three
     optional_overrides sub-sections, each under its own warning — see
     ``optional_warning_groups`` / ``render_pack_header_lines``);
  2. ``helm/threshold-exporter/values.yaml`` ``thresholdConfig.defaults``
     (the ``arming: default`` key set);
  3. ``components/threshold-exporter/config/conf.d/_defaults.yaml``
     ``defaults`` section (same ``arming: default`` set);
  4. the ``optional_overrides:`` LIST on both of those runtime surfaces
     (#1310) — the declared-without-value key names the exporter recognises
     but asserts no value for. A LIST, not a mapping: see
     ``render_optional_overrides_lines``;
  5. the comment block under that ``thresholdConfig.defaults`` listing the
     defaults-tier keys the chart does NOT ship, each with its arming state
     and reason (``render_unarmed_lines``, D-A A1).

Since D-A PR-3 the registry FILE is authored by hand and is the only place a
threshold is written. ``scaffold_tenant.RULE_PACKS``'s threshold half is a
GENERATED module (``_rule_packs_generated.py``, ``render_rule_packs_module``)
— generated rather than read at runtime because the da-tools image ships a
flat tool layout with no repo tree to find the YAML in. The extraction
(``build_registry_doc``) survives as the registry's self-consistency check:
``fixed_point_problems`` projects the registry back to the RULE_PACKS shape,
extracts it again and requires the same document, so every rule the
extraction enforces (``critical_of`` exactly where it derives,
every ``arming_problems`` rule, a ``metric_class`` on every key,
aliases onto live keys only) holds for the authored file too. All of it is
enforced by ``scripts/tools/lint/check_threshold_registry.py`` (schema +
fixed point + surface freshness + header-prose key membership), wired as a
pre-commit gate. The fix on a red gate: edit the registry, then run
``check_threshold_registry.py --regen`` to refresh every generated surface,
the RULE_PACKS module included, in one shot.

SCOPE (WS1a): threshold identity — ``defaults`` + ``optional_overrides`` tiers
({pack, tier, value, unit, desc, metric_class, arming, critical
variant}). ``state_filters`` / ``dimensional_example`` stay scaffold-owned
(they are state/config surface, not threshold identity; absorb later if a
consumer needs them). Extraction is FULL (all packs, all keys — mechanical and
cheap); the enforcement path this registry exists to serve first is the 18-key
repair line (#1196 / TRK-337).
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
import textwrap
from typing import Any, Callable, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - environments without pyyaml
    yaml = None  # type: ignore

try:
    import jsonschema
except ImportError:  # pragma: no cover - environments without jsonschema
    jsonschema = None  # type: ignore

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # repo layout: _lib_compat
from _lib_compat import find_project_root  # noqa: E402
import _lib_consumer_read  # noqa: E402  (#3206: spliced surfaces read back by their consumer)

# ⛔ Found, not counted (#1501). This used to be `_THIS_DIR` + three `".."`,
# which is the repo root in a checkout and `/` in the da-tools image (every
# tool flattened into `/opt/da-tools/`). The image ships this module for its
# pure predicates (scaffold_tenant / init_project / onboard_platform import
# them) and carries none of the repo files below, so None is the correct
# answer there — and every function that needs one of those files goes
# through `_repo_path_or_raise`, which says so instead of opening `/rule-packs/…`.
_ROOT = find_project_root(_THIS_DIR)
_REPO_ROOT: Optional[str] = str(_ROOT) if _ROOT is not None else None


class RepoTreeNotFoundError(FileNotFoundError):
    """A repo-only function ran where no project root is reachable (#1501)."""


def _in_repo(*parts: str) -> Optional[str]:
    return os.path.join(_REPO_ROOT, *parts) if _REPO_ROOT is not None else None


def _repo_path_or_raise(path: Optional[str], what: str) -> str:
    if path is None:
        raise RepoTreeNotFoundError(
            f"{what} lives in the repository tree, and {_THIS_DIR} is not "
            f"inside a scripts/tools tree — this function is repo-only (the "
            f"da-tools image carries no repo tree); pass an explicit path "
            f"instead"
        )
    return path


REGISTRY_PATH = _in_repo("rule-packs", "threshold-registry.yaml")
SCHEMA_PATH = _in_repo("docs", "schemas", "threshold-registry.schema.json")

CRITICAL_SUFFIX = "_critical"

# ---------------------------------------------------------------------------
# arming (D-A A1, made orthogonal by A1.5 / #3773): three independent facts
# about every key that is not a `<base>_critical` (those follow their base:
# `resolveCriticalRows` only emits one when `defaults[base]` has a value).
#
#   arming       default | operator — does the chart ship this key's value,
#                or must the operator supply it (not `off`: YAML 1.1 reads
#                an unquoted `off` as the boolean false).
#   portability  portable | ratio_pending | site_specific — does ONE value
#                carry across deployments. Required on shipped keys too:
#                that is what lets the gate catch a shipped absolute count.
#   blocker      optional {kind, tracking, reason, reason_zh} — something
#                other than validation stops the key from shipping (no data
#                source, a broken aggregation, never measured, a disputed
#                value), with the issue that tracks it.
#
# ⛔ Nothing here is a hand-kept list. "Read by a rule pack, not shipped by
# the chart" (`not_chart_armed_keys`) and "portable, unshipped, unblocked —
# what A2 validates next" (`validation_candidates`) are both DERIVED.
# The one-dimensional enum A1 shipped had grown a state per blocker and
# could not say "shipped, but not portable"; that is why it was split.
# ---------------------------------------------------------------------------
ARMING_DEFAULT = "default"
ARMING_OFF = "operator"
ARMING_STATES = (ARMING_DEFAULT, ARMING_OFF)
PORTABLE = "portable"
PORTABILITY_STATES = (PORTABLE, "ratio_pending", "site_specific")
BLOCKER_KINDS = ("unwired", "aggregation_bug", "unmeasured", "value_disputed")
#: The authored fields that make up one key's arming.
ARMING_FIELDS = ("arming", "portability", "portability_reason",
                 "portability_reason_zh", "blocker", "default_exception",
                 "validation")
#: `validation.basis` (D-A A2, #1200 ruling B): what a shipped value rests on.
#: vendor = one source that uses this very value; consensus = two or more
#: sources from different organisations whose values bracket it.
VALIDATION_BASES = ("vendor", "consensus")
_VALIDATION_FIELDS = ("basis", "sources")
_SOURCE_REQUIRED = ("org", "url", "quote", "value")
_SOURCE_FIELDS = _SOURCE_REQUIRED + ("tier",)
SOURCE_TIERS = ("info", "warning", "critical")
#: Required iff portability is not `portable`: why one value does not carry.
PORTABILITY_REASON_FIELDS = ("portability_reason", "portability_reason_zh")
_BLOCKER_FIELDS = ("kind", "tracking", "reason", "reason_zh")
#: How each state reads in a generated Chinese surface.
PORTABILITY_LABEL_ZH = {
    "ratio_pending": "待改比值",
    "site_specific": "依部署而定",
}
BLOCKER_LABEL_ZH = {
    "unwired": "沒有資料源",
    "aggregation_bug": "聚合錯誤",
    "unmeasured": "尚未量測",
    "value_disputed": "值有爭議",
}
CANDIDATE_LABEL_ZH = "可攜，待驗證"
CANDIDATE_NOTE_ZH = ("這個值可跨部署沿用，但還沒通過出貨驗證"
                     "（綁定 promtool 測試、登記廠商或共識依據），通過後才出貨")
#: The optional tier is not a candidate: `default` is defaults-tier only.
DECLARED_ONLY_LABEL_ZH = "可攜，僅宣告"
DECLARED_ONLY_NOTE_ZH = ("這個值可跨部署沿用，但鍵在 optional_overrides 層、平台不給值；"
                         "要出貨得先升到 defaults 層，再通過出貨驗證")

# The two threshold tiers carried by the registry. Order matters: it is the
# extraction order, and a same-name key in both tiers of one pack would be a
# contract bug (build_registry_doc fails loudly on it).
TIERS = ("defaults", "optional_overrides")

# ---------------------------------------------------------------------------
# Since D-A PR-3 the registry FILE is the authored source of everything this
# module used to merge in from side tables: the chart set (now `arming`), every key's
# `metric_class` (the old METRIC_CLASS_BACKFILL) and the `deprecated_aliases`
# section (the old DEPRECATED_KEY_ALIASES). Edit rule-packs/threshold-registry.yaml
# and run `check_threshold_registry.py --regen`.
#
# deprecated_aliases is still the alias SSOT. Its one runtime mirror, Go's
# components/threshold-exporter/app/pkg/config/aliases.go, is PINNED to the
# registry's section by pkg/config/aliases_registry_test.go. ⛔ No Python
# reader applies the table to a key: the rule that does is Go's alone, asked
# through `da-guard key-spellings` (#3556); tests/shared/test_alias_rule_lives_in_go.py
# keeps a reader of it from coming back. `DEPRECATED_KEY_ALIASES` below is
# the registry section, read on access (the da-tools image carries no repo
# tree, so it must not be read at import).
# ---------------------------------------------------------------------------


def __getattr__(name: str):
    """`DEPRECATED_KEY_ALIASES`: the committed registry's alias section."""
    if name == "DEPRECATED_KEY_ALIASES":
        return dict(load_registry().get("deprecated_aliases") or {})
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Canonical per-key field order for the emitted registry document.
_FIELD_ORDER = (
    "pack", "tier", "value", "unit", "desc", "metric_class", "arming",
    "portability", "portability_reason", "portability_reason_zh", "blocker",
    "default_exception", "validation", "critical_of", "value_counterexample",
    "dimensions",
)

def _load_scaffold():
    """Lazy import of scaffold_tenant (same directory)."""
    if _THIS_DIR not in sys.path:
        sys.path.insert(0, _THIS_DIR)
    import scaffold_tenant  # noqa: E402
    return scaffold_tenant


# ---------------------------------------------------------------------------
# Extraction (scaffold RULE_PACKS -> registry doc) — mechanical, never hand-copy
# ---------------------------------------------------------------------------

def build_registry_doc(
    rule_packs: Optional[dict] = None,
    *,
    arming: Optional[dict[str, dict]] = None,
    strict_metric_class: Optional[bool] = None,
    strict_arming: Optional[bool] = None,
    deprecated_aliases: Optional[dict[str, str]] = None,
    retired_keys: Optional[dict[str, dict]] = None,
) -> dict:
    """The registry document of *rule_packs* — or, with none, the committed one.

    With ``rule_packs`` (packs shaped like scaffold's RULE_PACKS): extract.
    Per pack: {display, exporter, default_on, rule_pack_file}.
    Per key : {pack, tier, value, unit, desc[, metric_class][, arming,
    portability[, portability_reason, portability_reason_zh][, blocker]
    [, default_exception]][, critical_of]}. ``critical_of`` is
    derived (never authored): key ends with ``_critical`` AND its base key
    exists in the same pack -> the base key name. ``arming`` maps a key to its
    authored arming fields (see `arming_problems` for the rules); with
    ``strict_arming`` every key that is not a ``<base>_critical`` must carry
    one. ``metric_class`` passes through and a ``_critical`` key without one
    inherits its base key's class; with
    ``strict_metric_class`` a key left unclassified is a hard error.

    Without ``rule_packs``: the committed rule-packs/threshold-registry.yaml,
    which since D-A PR-3 IS the source (`committed_registry_doc`). It must be
    a fixed point of `project_rule_packs` -> extract, so every rule above
    still binds the authored file.

    Fails loudly on identity collisions (same key in two tiers of one pack, or
    in two packs) — a collision would silently shadow a contract entry — and
    on an arming entry that names an unknown key or breaks `arming_problems`
    (a typo would silently un-ship a chart key).

    ``deprecated_aliases`` (#1231) rides along as a top-level registry section
    (old spelling -> canonical key). Fail-loud contract: the old spelling must
    NOT be an active key (it is retired), the target MUST be an active key
    (an alias onto nothing would silently accept configs that resolve to no
    threshold), and old != new.

    ``retired_keys`` (#3829) rides along the same way: a threshold key no rule
    pack reads any more -> ``{replaced_by, tracking, hint}``. Fail-loud
    contract (`retired_key_problems`): the retired key is not an active key and
    is on neither side of ``deprecated_aliases``, ``replaced_by`` is null or an
    active key, ``tracking`` is an issue number, ``hint`` one line.
    """
    if rule_packs is None:
        # The real registry: since D-A PR-3 the committed file IS the source,
        # so the "real extraction" is that file, checked to be a fixed point
        # of project -> extract (see `fixed_point_problems`).
        return committed_registry_doc()
    arming = arming or {}
    strict_metric_class = bool(strict_metric_class)
    deprecated_aliases = deprecated_aliases or {}
    retired_keys = retired_keys or {}

    packs: dict[str, Any] = {}
    keys: dict[str, Any] = {}
    for pack_name, pack in rule_packs.items():
        packs[pack_name] = {
            "display": pack["display"],
            "exporter": pack["exporter"],
            "default_on": bool(pack.get("default_on", False)),
            "rule_pack_file": pack["rule_pack_file"],
        }
        combined: dict[str, tuple[str, dict]] = {}
        for tier in TIERS:
            for key, info in (pack.get(tier) or {}).items():
                if key in combined:
                    raise ValueError(
                        f"threshold key {key!r} appears in BOTH tiers of pack "
                        f"{pack_name!r} — ambiguous contract identity"
                    )
                combined[key] = (tier, info)
        for key, (tier, info) in combined.items():
            if key in keys:
                raise ValueError(
                    f"threshold key {key!r} appears in packs "
                    f"{keys[key]['pack']!r} AND {pack_name!r} — ambiguous "
                    "contract identity"
                )
            entry: dict[str, Any] = {
                "pack": pack_name,
                "tier": tier,
                "value": info["value"],
                "unit": info["unit"],
                "desc": info["desc"],
            }
            if "metric_class" in info:
                entry["metric_class"] = info["metric_class"]
            if "dimensions" in info:
                # #3475: the labels a rule pack reads this key per value of
                # (ADR-024's `version`). Authored, never derived; a test pins
                # it both ways to the packs' normalisation grouping, and it is
                # rendered into the runtime `dimensions:` declaration.
                entry["dimensions"] = sorted(info["dimensions"])
            if "value_counterexample" in info:
                # Passed through verbatim, never derived: it records that the
                # ADR-030 reference library produced a case THIS value gets
                # wrong. Deliberately not a verdict — that library is a
                # sampling probe, not a spec (its README's anti-Goodhart
                # clause), so the registry states the measurement and lets the
                # reader judge. #1176.
                entry["value_counterexample"] = dict(info["value_counterexample"])
            for f, v in (arming.get(key) or {}).items():
                if f not in ARMING_FIELDS:
                    raise ValueError(
                        f"arming of {key!r} carries {f!r} — only "
                        f"{list(ARMING_FIELDS)} are arming fields"
                    )
                entry[f] = v
            if key.endswith(CRITICAL_SUFFIX):
                base = key[: -len(CRITICAL_SUFFIX)]
                if base in combined:
                    entry["critical_of"] = base
            keys[key] = entry

    missing_arming = set(arming) - set(keys)
    if missing_arming:
        raise ValueError(
            "arming given for keys not found in the packs: "
            f"{sorted(missing_arming)} — a typo here would silently un-ship a "
            "chart key"
        )
    problems = arming_problems(keys, strict=bool(strict_arming))
    if problems:
        raise ValueError("; ".join(problems))

    # metric_class: _critical inheritance -> strict completeness.
    for key, entry in keys.items():
        if "critical_of" not in entry:
            continue
        base = keys.get(entry["critical_of"], {})
        if "metric_class" not in base:
            continue
        if "metric_class" not in entry:
            entry["metric_class"] = base["metric_class"]
        elif entry["metric_class"] != base["metric_class"]:
            # A critical tier is the same metric as its base, one severity
            # up; a different class would e.g. hide a saturation base's
            # `_critical` from the educational hint (or invent one).
            raise ValueError(
                f"{key!r} is classed {entry['metric_class']!r} but its base "
                f"{entry['critical_of']!r} is {base['metric_class']!r} — a "
                "critical tier carries its base's metric_class"
            )
    if strict_metric_class:
        unclassified = sorted(
            k for k, e in keys.items() if "metric_class" not in e
        )
        if unclassified:
            raise ValueError(
                f"unclassified threshold keys {unclassified} — author "
                "metric_class in rule-packs/threshold-registry.yaml "
                "(schema requires it on every key)"
            )

    # deprecated_aliases (#1231): retired spelling -> canonical key. Fail-loud
    # so a stale table entry cannot ship a half-renamed contract.
    for old, new in deprecated_aliases.items():
        if old == new:
            raise ValueError(
                f"deprecated alias {old!r} maps to itself — remove the entry"
            )
        if old in keys:
            raise ValueError(
                f"deprecated alias {old!r} is still an ACTIVE registry key — "
                "an alias entry means the old spelling is retired; finish the "
                "rename (or drop the alias)"
            )
        if new not in keys:
            raise ValueError(
                f"deprecated alias {old!r} targets {new!r} which is not a "
                "registry key — the alias would resolve to no threshold"
            )

    problems = retired_key_problems(retired_keys, keys, deprecated_aliases)
    if problems:
        raise ValueError("; ".join(problems))

    # canonical field order (stable YAML emission).
    keys = {
        k: {f: e[f] for f in _FIELD_ORDER if f in e} for k, e in keys.items()
    }
    doc: dict[str, Any] = {"version": 1, "packs": packs, "keys": keys}
    if deprecated_aliases:
        doc["deprecated_aliases"] = dict(sorted(deprecated_aliases.items()))
    if retired_keys:
        doc["retired_keys"] = {
            k: {f: e[f] for f in RETIRED_KEY_FIELDS if f in e}
            for k, e in sorted(retired_keys.items())}
    return doc


#: The fields of one `retired_keys:` entry, in emission order.
RETIRED_KEY_FIELDS = ("replaced_by", "tracking", "hint")


def retired_key_problems(retired: dict, keys: dict, aliases: dict[str, str]) -> list[str]:
    """What breaks the `retired_keys:` contract (#3829), one message per rule.

    A retired key must have left the contract (not an active key), must not
    be an alias spelling either way (an alias is a key still honored — the
    two would say opposite things about one spelling; when an alias window
    closes, its old spelling moves here and leaves `deprecated_aliases`), its
    ``replaced_by`` is null or an active key (a hint naming no key would send
    the operator nowhere), and it names the issue that retired it. Its
    `_critical` tier is retired with it (the Go lookup derives
    `<old>_critical` -> `<replaced_by>_critical`, as an alias does), so that
    tier must not be an active key either."""
    problems: list[str] = []
    alias_sides = set(aliases) | set(aliases.values())
    for old, entry in retired.items():
        if not isinstance(entry, dict):
            problems.append(f"retired key {old!r} must be a mapping of {list(RETIRED_KEY_FIELDS)}")
            continue
        if old in keys:
            problems.append(
                f"retired key {old!r} is still an ACTIVE registry key — retire it "
                "by removing it from `keys:`, or drop the retired entry")
        if old in alias_sides:
            problems.append(
                f"retired key {old!r} is also in deprecated_aliases — a spelling "
                "is either still honored (alias) or retired, not both")
        if old + CRITICAL_SUFFIX in keys:
            # The lookup (pkg/config/retired_threshold_keys.go) retires
            # `<old>_critical` with its base, as aliasHead canonicalizes it.
            problems.append(
                f"retired key {old!r} still has an ACTIVE critical tier "
                f"{old + CRITICAL_SUFFIX!r} — the critical tier is retired with "
                "its base; retire both")
        repl = entry.get("replaced_by")
        if repl is not None and repl not in keys:
            problems.append(
                f"retired key {old!r} is replaced_by {repl!r}, which is not an "
                "active registry key — name the live key, or null for none")
        elif (repl is not None and repl + CRITICAL_SUFFIX not in keys
              and old + CRITICAL_SUFFIX not in retired):
            # The lookup derives `<old>_critical` -> `<repl>_critical`; when
            # repl has no critical tier that hint would name a key that does
            # not exist, so the critical spelling needs its own entry.
            problems.append(
                f"retired key {old!r} is replaced_by {repl!r}, which has no critical "
                f"tier, so the derived hint for {old + CRITICAL_SUFFIX!r} would name "
                f"{repl + CRITICAL_SUFFIX!r}, not a registry key — add a retired entry "
                f"for {old + CRITICAL_SUFFIX!r} (replaced_by: null)")
        if "tracking" not in entry:
            problems.append(f"retired key {old!r} needs tracking: <issue>")
        else:
            msg = _tracking_problem(old, "retired_keys", entry["tracking"])
            if msg:
                problems.append(msg)
        msg = _one_line_problem(old, "hint", entry.get("hint"))
        if msg:
            problems.append(f"retired key {msg}")
    return problems


def _one_line_problem(key: str, field: str, value: Any) -> Optional[str]:
    """Why *value* is not a non-blank single line, else None. The reasons are
    spliced into ``#`` comments, where a newline puts the tail at column 0."""
    if not isinstance(value, str) or not value.strip():
        return f"{key!r} needs a non-blank {field}"
    if value != value.strip() or "\n" in value or "\r" in value:
        return f"{key!r} {field} must be one line with no surrounding whitespace"
    return None


def _tracking_problem(key: str, where: str, value: Any) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return f"{key!r} {where}.tracking must be an issue number"
    return None


def arming_problems(keys: dict[str, dict], *, strict: bool) -> list[str]:
    """What is wrong with the arming fields of *keys* (registry key entries).

    - a ``<base>_critical`` (``critical_of`` set) carries none of them;
    - ``arming`` is ``default`` or ``operator``; ``portability`` is one of
      `PORTABILITY_STATES`; with *strict* both are present on every other key;
    - both portability reasons are present iff portability is not
      ``portable`` (a portable key has nothing to explain);
    - ``blocker`` is ``{kind, tracking, reason, reason_zh}`` exactly, with a
      known kind and an issue number;
    - ``default`` sits on a ``defaults``-tier key, has no blocker, and is
      portable — unless ``default_exception: {tracking}`` records why a
      non-portable value ships anyway; that field appears nowhere else;
    - every reason is one non-blank line;
    - ``default`` carries ``validation`` (`validation_problems`) and no
      ``value_counterexample``: a value the reference library measured
      wrong does not ship. An operator key may carry ``validation`` ahead
      of its promotion; whatever carries it is held to the same rules.
    """
    out: list[str] = []
    for key, entry in keys.items():
        present = [f for f in ARMING_FIELDS if f in entry]
        if entry.get("critical_of"):
            if present:
                out.append(
                    f"{key!r} is a critical tier of {entry['critical_of']!r}: it "
                    f"follows its base — remove {present}")
            continue
        state, port = entry.get("arming"), entry.get("portability")
        if state is None or port is None:
            missing = [f for f, v in (("arming", state), ("portability", port))
                       if v is None]
            if present or strict:
                out.append(f"{key!r} has no {' / '.join(missing)} — every key "
                           "that is not a <base>_critical states both")
            continue
        if state not in ARMING_STATES:
            out.append(f"{key!r} arming {state!r} is not one of "
                       f"{list(ARMING_STATES)}")
            continue
        if port not in PORTABILITY_STATES:
            out.append(f"{key!r} portability {port!r} is not one of "
                       f"{list(PORTABILITY_STATES)}")
            continue
        reasons = [f for f in PORTABILITY_REASON_FIELDS if f in entry]
        if port == PORTABLE:
            if reasons:
                out.append(f"{key!r} is portable — remove {reasons} (only a "
                           "non-portable key has something to explain)")
        else:
            for f in PORTABILITY_REASON_FIELDS:
                p = _one_line_problem(key, f, entry.get(f))
                if p:
                    out.append(p)
        blocker = entry.get("blocker")
        if blocker is not None:
            if not isinstance(blocker, dict) or set(blocker) != set(_BLOCKER_FIELDS):
                out.append(f"{key!r} blocker must be exactly "
                           f"{list(_BLOCKER_FIELDS)}")
            else:
                if blocker["kind"] not in BLOCKER_KINDS:
                    out.append(f"{key!r} blocker.kind {blocker['kind']!r} is not "
                               f"one of {list(BLOCKER_KINDS)}")
                p = _tracking_problem(key, "blocker", blocker["tracking"])
                if p:
                    out.append(p)
                for f in ("reason", "reason_zh"):
                    p = _one_line_problem(key, f"blocker.{f}", blocker[f])
                    if p:
                        out.append(p)
        exception = entry.get("default_exception")
        if state == ARMING_DEFAULT:
            if entry.get("tier") != "defaults":
                out.append(
                    f"{key!r} is arming: default but sits in tier "
                    f"{entry.get('tier')!r} — only defaults-tier keys are "
                    "chart-shippable")
            if blocker is not None:
                out.append(f"{key!r} is arming: default but has a blocker — a "
                           "blocked key cannot ship")
            if "value_counterexample" in entry:
                out.append(
                    f"{key!r} is arming: default but carries a "
                    "value_counterexample — a value the reference library "
                    "measured wrong cannot ship")
            if "validation" not in entry:
                out.append(
                    f"{key!r} is arming: default but has no validation — a "
                    "shipped value states what it rests on (basis: vendor | "
                    "consensus, with its sources)")
            if port == PORTABLE and exception is not None:
                out.append(f"{key!r} is portable — remove default_exception")
            elif port != PORTABLE:
                if not isinstance(exception, dict) or set(exception) != {"tracking"}:
                    out.append(
                        f"{key!r} ships (arming: default) but is {port}: a "
                        "shipped value must be portable, or record "
                        "default_exception: {tracking: <issue>}")
                else:
                    p = _tracking_problem(key, "default_exception",
                                          exception["tracking"])
                    if p:
                        out.append(p)
        elif exception is not None:
            out.append(f"{key!r} is arming: operator — default_exception only "
                       "applies to a shipped key")
        if "validation" in entry:
            out += validation_problems(key, entry)
    return out


def _number(v: Any) -> bool:
    """A finite number: not a bool, not NaN, not an infinity."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and abs(v) != float("inf"))


_URL_RE = re.compile(r"https://\S+")
_NUMBER_TOKEN_RE = re.compile(r"\d*\.?\d+")


def _quote_states(value: float, quote: str) -> bool:
    """Whether *quote* carries *value*: the number itself, or — for a source
    that writes a percentage as a ratio (`> 0.8`, `> .95`) — a hundredth of it."""
    numbers = {float(t) for t in _NUMBER_TOKEN_RE.findall(quote)}
    return any(abs(n - c) < 1e-9 for n in numbers for c in (value, value / 100))


def _same_page(url: str) -> str:
    """A url reduced to what tells two pages apart: no fragment, no trailing
    slash, case-insensitive."""
    return url.split("#", 1)[0].rstrip("/").lower()


def validation_problems(key: str, entry: dict) -> list[str]:
    """What is wrong with *entry*'s ``validation`` (D-A A2, #1200 ruling B).

    - exactly ``{basis, sources}``; ``basis`` one of `VALIDATION_BASES`;
    - every source is ``{org, url, quote, value}`` (+ optional ``tier``, the
      severity the source alerts at): an https url, a one-line quote, a
      finite number in this key's unit, and the quote carries that number
      (as written, or as a ratio: `> 0.8` for 80);
    - ``vendor``: one source, and its value IS the registry value;
    - ``consensus``: two or more sources, no organisation or url twice, and
      the registry value lies between the smallest and the largest source
      value (ends included).

    No relation is authored: whether a source is equal, stricter or looser is
    its value against the registry's, so it cannot go stale when the value
    changes — the rule above simply stops holding. Whether two organisations
    are independent of each other stays a reviewer's call."""
    v = entry.get("validation")
    if not isinstance(v, dict) or set(v) != set(_VALIDATION_FIELDS):
        return [f"{key!r} validation must be exactly {list(_VALIDATION_FIELDS)}"]
    out: list[str] = []
    basis, sources = v["basis"], v["sources"]
    if basis not in VALIDATION_BASES:
        out.append(f"{key!r} validation.basis {basis!r} is not one of "
                   f"{list(VALIDATION_BASES)}")
    if not isinstance(sources, list) or not sources:
        return out + [f"{key!r} validation.sources must be a non-empty list"]
    values: list[float] = []
    for i, s in enumerate(sources):
        where = f"validation.sources[{i}]"
        if (not isinstance(s, dict) or not set(_SOURCE_REQUIRED) <= set(s)
                or not set(s) <= set(_SOURCE_FIELDS)):
            out.append(f"{key!r} {where} must carry {list(_SOURCE_REQUIRED)} "
                       "(and optionally 'tier')")
            continue
        for f in ("org", "quote"):
            p = _one_line_problem(key, f"{where}.{f}", s[f])
            if p:
                out.append(p)
        if not (isinstance(s["url"], str) and _URL_RE.fullmatch(s["url"])):
            out.append(f"{key!r} {where}.url must be an https:// URL")
        if "tier" in s and s["tier"] not in SOURCE_TIERS:
            out.append(f"{key!r} {where}.tier {s['tier']!r} is not one of "
                       f"{list(SOURCE_TIERS)}")
        if not _number(s["value"]):
            out.append(f"{key!r} {where}.value must be a finite number in the "
                       "key's unit")
        else:
            values.append(float(s["value"]))
            if isinstance(s["quote"], str) and not _quote_states(float(s["value"]), s["quote"]):
                out.append(
                    f"{key!r} {where}.quote does not carry its value "
                    f"{s['value']} — quote the line that states the number")
    if out or len(values) != len(sources):
        return out
    value = entry.get("value")
    if not _number(value):
        return out + [f"{key!r} has a validation but its value {value!r} is not "
                      "a number to hold the sources against"]
    if basis == "vendor":
        if len(sources) != 1:
            out.append(f"{key!r} validation.basis vendor takes exactly one "
                       f"source (found {len(sources)}); several sources are a "
                       "consensus")
        elif values[0] != value:
            out.append(
                f"{key!r} ships {value} but its vendor source uses "
                f"{sources[0]['value']} — a vendor basis is the vendor's own value")
    else:
        if len(sources) < 2:
            out.append(f"{key!r} validation.basis consensus needs two or more "
                       "sources")
        for f in ("org", "url"):
            seen = [_same_page(s[f]) if f == "url" else str(s[f]).strip().lower()
                    for s in sources]
            dup = sorted({x for x in seen if seen.count(x) > 1})
            if dup:
                out.append(f"{key!r} consensus sources repeat {f} {dup} — each "
                           "source is a different organisation's page")
        if not min(values) <= value <= max(values):
            out.append(
                f"{key!r} ships {value}, outside what its consensus sources "
                f"use ({min(values):g}–{max(values):g})")
    return out


def is_chart_armed(entry: dict) -> bool:
    """Whether the chart ships this registry key's value (``arming: default``)."""
    return entry.get("arming") == ARMING_DEFAULT


def _valid_blocker(entry: dict) -> Optional[dict]:
    b = entry.get("blocker")
    if (isinstance(b, dict) and b.get("kind") in BLOCKER_KINDS
            and isinstance(b.get("reason_zh"), str)):
        return b
    return None


def is_validation_candidate(entry: dict) -> bool:
    """Portable, not shipped, not blocked, in the tier that can ship: what
    A2 validates next. An ``optional_overrides`` key is not one — the
    shipping gate (`arming_problems`) refuses ``default`` outside the
    defaults tier, so listing it here promised a promotion the gate blocks."""
    return (entry.get("arming") == ARMING_OFF
            and entry.get("portability") == PORTABLE
            and entry.get("tier") == "defaults"
            and "blocker" not in entry and not entry.get("critical_of"))


def validation_candidates(doc: dict) -> frozenset[str]:
    """Every key `is_validation_candidate` admits — DERIVED, never authored."""
    return frozenset(k for k, e in registry_keys(doc).items()
                     if is_validation_candidate(e))


def arming_notes_zh(entry: dict) -> list[str]:
    """One Chinese sentence per fact a reader of *entry* needs: why it is not
    shipped (blocker, non-portability, or awaiting validation), or why a
    non-portable value ships anyway. Empty for a portable shipped key and for
    a ``<base>_critical``.

    Total over malformed input: the renderers run even when the registry
    fails validation (the gate reports every violation in one pass), so a
    malformed field renders nothing instead of raising and hiding the
    violation behind a crash."""
    if entry.get("critical_of"):
        return []
    state, port = entry.get("arming"), entry.get("portability")
    port_label = PORTABILITY_LABEL_ZH.get(port) if isinstance(port, str) else None
    port_reason = entry.get("portability_reason_zh")
    notes: list[str] = []
    if state == ARMING_OFF:
        b = _valid_blocker(entry)
        if b is not None:
            notes.append(f"未啟用（{BLOCKER_LABEL_ZH[b['kind']]}，"
                         f"#{b.get('tracking')}）：{b['reason_zh']}")
        if port_label and isinstance(port_reason, str):
            notes.append(f"未啟用（{port_label}）：{port_reason}")
        elif port == PORTABLE and b is None:
            if is_validation_candidate(entry):
                notes.append(f"未啟用（{CANDIDATE_LABEL_ZH}）：{CANDIDATE_NOTE_ZH}")
            else:
                notes.append(f"未啟用（{DECLARED_ONLY_LABEL_ZH}）：{DECLARED_ONLY_NOTE_ZH}")
    elif state == ARMING_DEFAULT and port_label and isinstance(port_reason, str):
        exc = entry.get("default_exception")
        t = exc.get("tracking") if isinstance(exc, dict) else None
        notes.append(f"出貨例外（{port_label}，#{t}）：{port_reason}")
    return notes


def not_chart_armed_keys(doc: dict) -> frozenset[str]:
    """The keys the scaffold path supplies but the chart does not ship.

    A ``defaults``-tier key whose arming is not ``default``, plus every
    ``<base>_critical`` whose base is such a key (the critical row needs
    ``defaults[base]``, which the chart then does not carry). This is what the
    reachability gate's chart face must measure in the shipped values.yaml —
    derived here so there is no hand-kept list to drift from the registry.
    ``optional_overrides`` keys are not in it: the scaffold path supplies no
    value for them either, so the gate's unreachable face owns them.
    """
    keys = registry_keys(doc)
    out: set[str] = set()
    for key, entry in keys.items():
        base = entry.get("critical_of")
        owner = keys.get(base) if base else entry
        if owner and owner.get("tier") == "defaults" and not is_chart_armed(owner):
            out.add(key)
    return frozenset(out)


# ---------------------------------------------------------------------------
# The registry as source (D-A PR-3): project -> RULE_PACKS shape, fixed point,
# and the generated Python module scaffold_tenant reads
# ---------------------------------------------------------------------------

#: Per-pack fields of scaffold's RULE_PACKS that come from the registry.
_PACK_FIELDS = ("display", "exporter", "default_on", "rule_pack_file")
#: Per-key fields of scaffold's RULE_PACKS that come from the registry.
_KEY_FIELDS = ("value", "unit", "desc", "metric_class", "dimensions",
               "value_counterexample")

#: The generated module: a sibling of this file, so it ships in the da-tools
#: image beside scaffold_tenant (which has no repo tree to read the YAML from).
RULE_PACKS_MODULE_PATH = os.path.join(_THIS_DIR, "_rule_packs_generated.py")
RULE_PACKS_MODULE_ID = "rule-packs-module"

_RULE_PACKS_MODULE_HEADER = (
    '"""GENERATED from rule-packs/threshold-registry.yaml — DO NOT EDIT.\n'
    "\n"
    "The threshold half of scaffold_tenant.RULE_PACKS (D-A PR-3): per pack its\n"
    "display/exporter/default_on/rule_pack_file and the `defaults` /\n"
    "`optional_overrides` tiers. Edit the registry, then run\n"
    "  python3 scripts/tools/lint/check_threshold_registry.py --regen\n"
    "`--ci` (pre-commit, CI) fails while this file is stale.\n"
    '"""\n'
)


def project_rule_packs(doc: dict) -> dict:
    """The threshold part of RULE_PACKS, as the registry *doc* states it.

    Per pack: the `_PACK_FIELDS`, then the tiers in key order; per key the
    `_KEY_FIELDS` it carries. Derived fields (`critical_of`) and the
    registry-only arming fields are not part of RULE_PACKS."""
    packs: dict[str, dict] = {}
    for name, meta in (doc.get("packs") or {}).items():
        packs[name] = {f: meta[f] for f in _PACK_FIELDS if f in meta}
    for key, entry in registry_keys(doc).items():
        info: dict[str, Any] = {}
        for f in _KEY_FIELDS:
            if f not in entry:
                continue
            v = entry[f]
            info[f] = list(v) if isinstance(v, list) else (
                dict(v) if isinstance(v, dict) else v)
        packs[entry["pack"]].setdefault(entry["tier"], {})[key] = info
    return packs


def fixed_point_problems(doc: dict) -> list[str]:
    """What keeps *doc* from being a fixed point of project -> extract.

    Extracting `project_rule_packs(doc)` again — with its own arming, aliases
    and classes, strict classification and strict arming — must
    reproduce *doc*. That is how the authored file is held to every rule
    `build_registry_doc` enforces: collisions, `critical_of` exactly where it
    derives, every `arming_problems` rule, a class on every key, aliases onto
    live keys. Empty == holds."""
    try:
        fresh = build_registry_doc(
            project_rule_packs(doc),
            arming={k: {f: e[f] for f in ARMING_FIELDS if f in e}
                    for k, e in registry_keys(doc).items()
                    if any(f in e for f in ARMING_FIELDS)},
            strict_metric_class=True,
            strict_arming=True,
            deprecated_aliases=dict(doc.get("deprecated_aliases") or {}),
            retired_keys=dict(doc.get("retired_keys") or {}),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        # The file is hand-authored: an entry that is not a mapping (or a
        # `packs.<p>: null`) is a contract error to report next to the schema
        # errors, not a crash that hides them.
        return [f"not a valid contract: {type(exc).__name__}: {exc}"]
    return diff_docs(doc, fresh)


def committed_registry_doc(path: Optional[str] = None) -> dict:
    """The committed registry, refused (ValueError) unless it is a fixed point."""
    doc = load_registry(path)
    problems = fixed_point_problems(doc)
    if problems:
        raise ValueError(
            "rule-packs/threshold-registry.yaml breaks its own contract: "
            + "; ".join(problems))
    return doc


def render_rule_packs_module(doc: dict) -> str:
    """The exact text of the generated module for *doc*."""
    import pprint

    body = pprint.pformat(project_rule_packs(doc), indent=1, width=100,
                          sort_dicts=False)
    return _RULE_PACKS_MODULE_HEADER + "\nTHRESHOLD_PACKS = " + body + "\n"


def packaged_registry_doc() -> dict:
    """The registry as the generated RULE_PACKS module carries it.

    For a reader that runs where no repo tree is (the da-tools image ships
    the module, not the YAML): every key's pack, tier, value, unit, desc,
    metric_class, dimensions and derived `critical_of` — but NOT
    the arming fields or `deprecated_aliases`, which RULE_PACKS does not
    carry. In a checkout it equals the committed registry minus those two
    (pinned by tests/ops/test_registry_lib.py)."""
    import runpy  # noqa: PLC0415  (a data module: no import-system entry needed)
    packs = runpy.run_path(RULE_PACKS_MODULE_PATH)["THRESHOLD_PACKS"]
    return build_registry_doc(packs, strict_metric_class=True)


def check_rule_packs_module(doc: dict, path: Optional[str] = None) -> Optional[str]:
    """None when the generated module is fresh, else what is wrong with it."""
    p = path or RULE_PACKS_MODULE_PATH
    if not os.path.isfile(p):
        return f"{p} (surface {RULE_PACKS_MODULE_ID!r}) does not exist"
    with open(p, encoding="utf-8") as fh:
        if fh.read() != render_rule_packs_module(doc):
            return (f"{p} (surface {RULE_PACKS_MODULE_ID!r}) is not what the "
                    "registry generates")
    return None


#: The Go table of `retired_keys:` (#3829), generated whole: the key verdict
#: (pkg/keyverdict), served-values' hint and tenant-api's batch `unset` read
#: it through pkg/config/retired_threshold_keys.go.
RETIRED_KEYS_GO_PATH = _in_repo(
    "components", "threshold-exporter", "app", "pkg", "config",
    "retired_threshold_keys_gen.go")
RETIRED_KEYS_GO_ID = "retired-keys-go"


def _go_str(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)


def render_retired_keys_go(doc: dict) -> str:
    """The exact text of the generated Go table for *doc*'s `retired_keys:`.

    Rendered for the freshness check even when the section breaks its
    contract (reported beside it by the schema and fixed-point checks), so a
    malformed entry renders as best it can instead of raising."""
    def field(e: Any, name: str) -> Any:
        return e.get(name) if isinstance(e, dict) else None

    def tracking(e: Any) -> int:
        t = field(e, "tracking")
        return t if isinstance(t, int) and not isinstance(t, bool) else 0

    entries = "".join(
        f"\t{{{_go_str(str(k))}, {_go_str(str(field(e, 'replaced_by') or ''))}, "
        f"{tracking(e)}, {_go_str(str(field(e, 'hint') or ''))}}},\n"
        for k, e in sorted((doc.get("retired_keys") or {}).items(), key=lambda kv: str(kv[0])))
    return (
        "// Code generated by scripts/tools/lint/check_threshold_registry.py --regen; DO NOT EDIT.\n"
        "// Source: rule-packs/threshold-registry.yaml `retired_keys:` (#3829).\n"
        "\n"
        "package config\n"
        "\n"
        "// retiredThresholdKeys are the threshold keys no rule pack reads any\n"
        "// more: key, the key that replaces it (\"\" for none), the issue that\n"
        "// retired it and the hint. Looked up by retired_threshold_keys.go.\n"
        "var retiredThresholdKeys = []retiredThresholdKey{"
        + (f"\n{entries}" if entries else "") + "}\n")


def check_retired_keys_go(doc: dict, path: Optional[str] = None) -> Optional[str]:
    """None when the generated Go table is fresh, else what is wrong with it."""
    p = path or RETIRED_KEYS_GO_PATH
    if p is None or not os.path.isfile(p):
        return f"{p} (surface {RETIRED_KEYS_GO_ID!r}) does not exist"
    with open(p, encoding="utf-8") as fh:
        if fh.read() != render_retired_keys_go(doc):
            return (f"{p} (surface {RETIRED_KEYS_GO_ID!r}) is not what the "
                    "registry generates")
    return None


def regen(path: Optional[str] = None) -> tuple[dict, list[str]]:
    """``--regen``: every generated surface from the committed registry.

    The registry itself is authored (D-A PR-3) and never written. It is
    refused unless it is a fixed point (`committed_registry_doc`); then the
    surfaces are planned and every conf.d surface is judged by a ``da-guard
    write`` dry run of the batches that will write it (`plan_surfaces`) —
    all before anything is written. Only then are the changed surfaces
    written (the conf.d ones first, through ``da-guard write``) and the
    generated RULE_PACKS module and the retired-keys Go table (#3829)
    last; a refusal (ConfdWriteRefused,
    ValueError, `_lib_parse_tree.ParseTreeError`) leaves every file as it
    was; a write that stops part-way is SurfaceWriteStopped
    (ConfdWriteStopped for a conf.d batch), naming the files already
    written, and nothing after it is written.
    Returns ({path, packs, keys}, the touched surface paths)."""
    sys.path.insert(0, _THIS_DIR)
    sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
    from _lib_python import write_text_secure  # noqa: E402

    src = path or _repo_path_or_raise(REGISTRY_PATH, "the threshold registry")
    doc = committed_registry_doc(src)
    planned = plan_surfaces(doc)
    module_text = render_rule_packs_module(doc)
    go_text = render_retired_keys_go(doc)
    go_path = _repo_path_or_raise(RETIRED_KEYS_GO_PATH, "the retired-keys Go table")
    touched = write_planned(planned)
    if check_rule_packs_module(doc) is not None:
        _write_or_stop(RULE_PACKS_MODULE_PATH, module_text, touched, write_text_secure)
        touched.append(RULE_PACKS_MODULE_PATH)
    if check_retired_keys_go(doc, go_path) is not None:
        _write_or_stop(go_path, go_text, touched, write_text_secure)
        touched.append(go_path)
    return {"path": src, "packs": len(doc["packs"]), "keys": len(doc["keys"])}, touched


# ---------------------------------------------------------------------------
# Loading + schema validation
# ---------------------------------------------------------------------------

def load_registry(path: Optional[str] = None) -> dict:
    """Load the committed registry document (the whole doc, not just keys)."""
    if yaml is None:
        raise RuntimeError("pyyaml required")
    p = path or _repo_path_or_raise(REGISTRY_PATH, "the threshold registry")
    # Strict: since D-A PR-3 the file is authored by hand, and a key written
    # twice (`pg_connections:` under two packs) would otherwise keep the last
    # one silently — the extraction's two-packs collision check never sees it.
    from _lib_io import strict_safe_load  # noqa: PLC0415  (scripts/tools, on the path above)
    with open(p, encoding="utf-8") as fh:
        return strict_safe_load(fh) or {}


def validate_registry(
    doc: dict, schema_path: Optional[str] = None
) -> list[str]:
    """Validate a registry doc against the JSON Schema. Returns error strings.

    Empty list == valid. Uses Draft-07 (the docs/schemas/ family convention).
    """
    if jsonschema is None:
        raise RuntimeError("jsonschema required")
    import json

    sp = schema_path or _repo_path_or_raise(
        SCHEMA_PATH, "the threshold-registry schema")
    with open(sp, encoding="utf-8") as fh:
        schema = json.load(fh)
    validator = jsonschema.Draft7Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"schema: {where}: {err.message}")
    return errors


# ---------------------------------------------------------------------------
# Convenience queries
# ---------------------------------------------------------------------------

def registry_keys(doc: dict) -> dict[str, dict]:
    """The flat ``keys`` mapping of a registry doc (or {})."""
    return doc.get("keys", {}) or {}


def keys_by_pack(doc: dict) -> dict[str, dict[str, dict]]:
    """Group the flat keys map by pack: {pack: {key: entry}}."""
    out: dict[str, dict[str, dict]] = {}
    for key, entry in registry_keys(doc).items():
        out.setdefault(entry.get("pack", "?"), {})[key] = entry
    return out


def keys_in_tier(doc: dict, tier: str) -> dict[str, dict]:
    """All keys of one tier ('defaults' / 'optional_overrides')."""
    return {
        k: e for k, e in registry_keys(doc).items() if e.get("tier") == tier
    }


def default_value(doc: dict, key: str):
    """The registry default value for ``key`` (None if absent)."""
    entry = registry_keys(doc).get(key)
    return None if entry is None else entry.get("value")


# ---------------------------------------------------------------------------
# Field-by-field diff of two registry documents
# ---------------------------------------------------------------------------

def diff_docs(committed: dict, fresh: dict) -> list[str]:
    """Semantic diff of a committed registry vs its fresh re-derivation.

    Returns one message per divergence (empty == semantically equal). Messages
    are per-field so the gate output is directly actionable.
    """
    diffs: list[str] = []

    c_packs = committed.get("packs", {}) or {}
    f_packs = fresh.get("packs", {}) or {}
    for name in sorted(set(f_packs) - set(c_packs)):
        diffs.append(f"pack {name!r}: derived but missing from registry")
    for name in sorted(set(c_packs) - set(f_packs)):
        diffs.append(f"pack {name!r}: in registry but not derived")
    for name in sorted(set(c_packs) & set(f_packs)):
        for field in sorted(set(c_packs[name]) | set(f_packs[name])):
            cv, fv = c_packs[name].get(field), f_packs[name].get(field)
            if cv != fv:
                diffs.append(
                    f"pack {name!r}.{field}: registry={cv!r} vs derived={fv!r}"
                )

    c_keys = committed.get("keys", {}) or {}
    f_keys = fresh.get("keys", {}) or {}
    for key in sorted(set(f_keys) - set(c_keys)):
        diffs.append(f"key {key!r}: derived but missing from registry")
    for key in sorted(set(c_keys) - set(f_keys)):
        diffs.append(f"key {key!r}: in registry but not derived")
    for key in sorted(set(c_keys) & set(f_keys)):
        for field in sorted(set(c_keys[key]) | set(f_keys[key])):
            cv, fv = c_keys[key].get(field), f_keys[key].get(field)
            if cv != fv:
                diffs.append(
                    f"key {key!r}.{field}: registry={cv!r} vs derived={fv!r}"
                )

    # deprecated_aliases (#1231) — same anti-dual-SoT treatment as keys: any
    # divergence in either direction is a drift (a missing section counts as
    # an empty table).
    c_alias = committed.get("deprecated_aliases", {}) or {}
    f_alias = fresh.get("deprecated_aliases", {}) or {}
    for old in sorted(set(f_alias) - set(c_alias)):
        diffs.append(
            f"deprecated_alias {old!r}: derived but missing from registry"
        )
    for old in sorted(set(c_alias) - set(f_alias)):
        diffs.append(
            f"deprecated_alias {old!r}: in registry but not derived"
        )
    for old in sorted(set(c_alias) & set(f_alias)):
        if c_alias[old] != f_alias[old]:
            diffs.append(
                f"deprecated_alias {old!r}: registry={c_alias[old]!r} vs "
                f"derived={f_alias[old]!r}"
            )

    # retired_keys (#3829): same treatment, a missing section is empty.
    c_ret = committed.get("retired_keys", {}) or {}
    f_ret = fresh.get("retired_keys", {}) or {}
    for old in sorted(set(c_ret) ^ set(f_ret)):
        diffs.append(f"retired_key {old!r}: in only one of registry / derived")
    for old in sorted(set(c_ret) & set(f_ret)):
        if c_ret[old] != f_ret[old]:
            diffs.append(
                f"retired_key {old!r}: registry={c_ret[old]!r} vs "
                f"derived={f_ret[old]!r}")

    if committed.get("version") != fresh.get("version"):
        diffs.append(
            f"version: registry={committed.get('version')!r} vs "
            f"expected={fresh.get('version')!r}"
        )
    return diffs


# ---------------------------------------------------------------------------
# Generated surfaces (PR-2 rewire) — renderers, delimiter splice, freshness
# ---------------------------------------------------------------------------
# Previously hand-copied surfaces are generated from the registry inside
# delimited blocks (a file may host more than one — helm values and the dev
# template each carry both a `defaults` mapping and an `optional_overrides`
# list). The delimiters are load-bearing: --check compares the
# whole block (markers included) against a fresh render, --regen splices a
# fresh render between them. Hand-written prose OUTSIDE the block is never
# touched (and is separately covered by the header-prose membership lint).

HELM_VALUES_PATH = _in_repo("helm", "threshold-exporter", "values.yaml")
DEV_DEFAULTS_PATH = _in_repo(
    "components", "threshold-exporter", "config", "conf.d", "_defaults.yaml",
)
# ⛔ The try-local seed used to be the THIRD copy of the declared list,
# hand-maintained by a comment that told the reader "if the generated copies
# diverge, THEY are right and this copy needs a hand edit" — i.e. a documented
# manual sync step. Its only gate compared the PARSED list, so anything
# comment-shaped (which is where the counter-example caveat lives) was
# invisible to it and the seed silently became the one shipped conf.d without
# the caveat (blind review, #1344). Generating it deletes the manual step.
TRY_LOCAL_DEFAULTS_PATH = _in_repo(
    "try-local", "seed", "conf.d", "_defaults.yaml",
)

_MARKER_STEM = "GENERATED:threshold-registry:"


def begin_marker(surface_id: str, indent: str = "") -> str:
    return (
        f"{indent}# >>> {_MARKER_STEM}{surface_id} — generated block, DO NOT "
        "EDIT（改 scaffold_tenant.RULE_PACKS 後跑 check_threshold_registry.py "
        "--regen）"
    )


def end_marker(surface_id: str, indent: str = "") -> str:
    return f"{indent}# <<< {_MARKER_STEM}{surface_id}"


def _fmt_value(v: Any) -> str:
    """Deterministic YAML-comment-safe scalar rendering (int stays int)."""
    if isinstance(v, bool):  # bool is an int subclass — guard first
        return "true" if v else "false"
    return repr(v) if isinstance(v, float) else str(v)


def _entry_lines(
    key: str, entry: dict, prefix: str = "#   ", cont: str = "#       "
) -> list[str]:
    """One registry key as pack-header comment lines (inline when short)."""
    tag = "   [chart-default]" if is_chart_armed(entry) else ""
    keyline = f"{prefix}{key}: {_fmt_value(entry['value'])}{tag}"
    meta = f"{entry['unit']} — {entry['desc']}"
    inline = f"{keyline}   # {meta}"
    lines = [inline] if len(inline) <= _COMMENT_WIDTH else [keyline] + [
        f"{cont}{seg}" for seg in _wrap_comment(meta, _COMMENT_WIDTH - len(cont))
    ]
    return lines + _counterexample_lines(entry, cont) + _arming_note_lines(
        entry, cont)


def _arming_note_lines(entry: dict, cont: str) -> list[str]:
    """`arming_notes_zh` as ``#`` comment lines under a pack-header key."""
    return [f"{cont}{seg}" for note in arming_notes_zh(entry)
            for seg in _wrap_comment(note, _COMMENT_WIDTH - len(cont))]


# The two directions are NOT interchangeable, and a single blanket caveat would
# be false for one of them: over_fire means a benign scenario crossed the
# threshold, under_fire means a real fault stayed under it. #1176 has both.
#
# ⛔ ONE source for the verdict wording, in both languages, for every surface —
# Python and JS alike (the JS copy lives in `_common/data/rule-packs.js` and is
# imported by both portal callers). What is deliberately NOT unified is the
# SENTENCE each medium composes around it: a YAML comment, a CLI prompt line
# and a React element cannot share a string. The facts (issue / observed /
# direction verdict / the mark) have one source; the framing is per-medium.
# Saying more than that is what the previous docstring here did, and it was
# false the moment the second composer appeared (blind review, #1344).
COUNTEREXAMPLE_DIRECTION = {
    "zh": {
        "over_fire": "此值會對正常運作誤警",
        "under_fire": "此值接不到該抓的故障",
    },
    "en": {
        "over_fire": "fires on benign load",
        "under_fire": "misses the fault it should catch",
    },
}

# The phrase every rendered counter-example carries, exported so the gates can
# key off ONE string. Three test files had each hand-copied a fragment of it —
# the same duplication this whole line of work exists to delete, reproduced
# inside the tests that guard against it (CodeRabbit, #1344).
COUNTEREXAMPLE_MARK = "實測反例"
COUNTEREXAMPLE_MARKS = {"zh": COUNTEREXAMPLE_MARK, "en": "measured counter-example"}


def counterexample_verdict(ce: dict, lang: str = "zh") -> str:
    """What the measured counter-example says about the value, in `lang`.

    Lookup, never a ternary: an unknown ``direction`` must be a hard error and
    not silently render as the OTHER direction's wording. The portal copy of
    this table fails the same way by design (`rule-packs.js`) — telling an
    operator "this value fires on benign load" when the measurement said the
    opposite points them at the wrong knob.

    ⛔ Fail loud, but say WHAT is wrong. A bare ``KeyError: 'over-fire'`` out of
    a dict lookup surfaces to an operator running `scaffold_tenant` as a
    traceback with no hint that the typo is in a registry field (blind review,
    #1344) — and `scaffold_tenant` has no except around the write path.
    """
    require_counterexample(ce)
    return COUNTEREXAMPLE_DIRECTION[lang][ce["direction"]]


def require_counterexample(ce: dict) -> dict:
    """Validate a `value_counterexample` and say what is wrong if it is not.

    ⛔ ALL THREE fields, in one place. An earlier version guarded only
    ``direction``, so a missing ``issue`` or ``observed`` still reached an
    operator as a bare ``KeyError: 'observed'`` from whichever composer got
    there first — the cited instance fixed, the class left open (blind review,
    #1344).

    ⛔ ``observed`` must be one non-blank line. The schema's ``pattern`` catches
    an EMBEDDED newline but not a trailing one: jsonschema matches with
    ``re.search`` and Python's ``$`` also matches just before a final newline,
    so ``observed: |`` (the natural YAML way to write a long clause) produces a
    string the schema accepts. Every composer collapses whitespace defensively,
    but the useful place to refuse it is here, where the message can name the
    field — not silently, five surfaces downstream.
    """
    missing = [f for f in ("issue", "direction", "observed") if f not in ce]
    if missing:
        raise ValueError(
            f"value_counterexample is missing {missing} (needs issue / "
            f"direction / observed; see docs/schemas/threshold-registry.schema.json). "
            f"Got keys {sorted(ce)}.")
    if ce["direction"] not in COUNTEREXAMPLE_DIRECTION["zh"]:
        raise ValueError(
            f"value_counterexample.direction={ce['direction']!r} is not one of "
            f"{sorted(COUNTEREXAMPLE_DIRECTION['zh'])} (key metadata in "
            f"scaffold_tenant.RULE_PACKS / threshold-registry.yaml). The two "
            f"directions mean opposite things, so there is no safe default.")
    for field in ("observed", "observed_zh"):
        if field not in ce:
            continue  # `observed_zh` is optional in the schema; see below
        observed = ce[field]
        if not isinstance(observed, str) or not observed.strip():
            raise ValueError(
                f"value_counterexample.{field}={observed!r} is blank — an empty "
                "clause renders as a warning that says nothing, which reads as "
                "'measured and fine'.")
        if observed != " ".join(observed.split()):
            raise ValueError(
                f"value_counterexample.{field}={observed!r} contains a newline "
                "or padding. It is spliced into single-line `#` comments on "
                "several surfaces; write it as one clause.")
    return ce


def counterexample_observed(ce: dict, lang: str = "zh") -> str:
    """The measured clause in `lang`.

    ⛔ Falls back to `observed` (English) rather than rendering nothing. A
    missing translation must not make the warning disappear: absence of the
    caveat is the repo's signal for "nothing was measured", and borrowing it
    for "nothing was translated" would turn a data-entry gap into a false
    all-clear. `tests/lint/test_counterexample_coverage.py` requires every
    shipped counter-example to carry `observed_zh`, so the fallback is a
    safety net rather than a path anything actually takes (owner decision on
    the mixed-language ZH surfaces, #1344).
    """
    if lang == "zh":
        return ce.get("observed_zh") or ce["observed"]
    return ce["observed"]


def counterexample_for_key(key: str, rule_packs: Optional[dict] = None) -> Optional[dict]:
    """The measured counter-example for `key`, or None. Looks across both tiers."""
    if rule_packs is None:
        rule_packs = _load_scaffold().RULE_PACKS
    for pack in (rule_packs or {}).values():
        for tier in ("defaults", "optional_overrides"):
            info = (pack.get(tier) or {}).get(key)
            if isinstance(info, dict) and "value_counterexample" in info:
                return info["value_counterexample"]
    return None


def counterexample_sentence(ce: dict, lang: str = "zh") -> str:
    """The one-line comment framing used by the two `_defaults.yaml` writers."""
    mark = COUNTEREXAMPLE_MARKS[lang]
    verdict = counterexample_verdict(ce, lang)
    if lang == "zh":
        return f"#{ce['issue']} {mark}：{counterexample_observed(ce, 'zh')}（{verdict}）"
    return f"#{ce['issue']} {mark}: {counterexample_observed(ce, 'en')} ({verdict})"


def defaults_counterexample_head(at: tuple, value: Any,
                                 rule_packs: Optional[dict] = None,
                                 lang: str = "zh") -> Optional[str]:
    """The ⚠ comment above an emitted `_defaults.yaml` key that has one: a
    `_lib_confedit.data_value` *head* (key path, value) → comment text, or
    None.

    ⛔ Why this exists at all: the interactive prompt only reaches a human who
    is ANSWERING PROMPTS. `generate_defaults` → `write_outputs` is the path
    taken by `--non-interactive`, by `--from-onboard` batch onboarding, by the
    interactive run whose operator declines to customise, and by
    `da-tools init` — and it writes the platform-asserted numbers straight into
    `_defaults.yaml` with nobody prompted.

    ⛔ It lives HERE, not in `scaffold_tenant`, because there are TWO producers
    of that file: `scaffold_tenant.write_outputs` and
    `init_project._defaults_file`. It was in `scaffold_tenant`, so the
    second producer wrote the same declared keys bare — same directory, same
    filename, one annotated and one not (blind review, #1344).

    A comment da-guard writes (ADR-036 §2, #3206 10c-3): Go writes the file,
    this says which comment goes above which node. It is asked about every
    node `da-guard write` will emit, so it can only annotate a key this run
    actually writes.

    ⛔ BOTH shapes, not just `key: value`. `optional_overrides:` is a plain
    string LIST (`- oracle_process_count`), so a mapping-only rule skipped the
    entire declared tier — the tier #1320 D1 says a tenant can fill in today.
    A sequence item (its path ends `[i]`) is annotated by its string value; a
    key whose value is a non-empty mapping or sequence (a section) is not.

    ⛔ WRAPPED, like every other composer, at the width left at the node's
    indent (yaml.v3's: two columns a level, a sequence item indented under
    its key). This one used to interpolate the sentence raw, so an `observed`
    containing a newline emitted its tail at column 0 and the written
    `_defaults.yaml` stopped being valid YAML. The schema forbids the newline
    at the source; da-guard writes every comment line as `# <line>` besides.
    """
    if not at:
        return None
    last = at[-1]
    if last.startswith("[") and last.endswith("]"):
        if not isinstance(value, str):
            return None
        key = value
    elif isinstance(value, (dict, list, tuple)) and value:
        return None
    else:
        key = last
    ce = counterexample_for_key(key, rule_packs)
    if not ce:
        return None
    indent = 2 * (len(at) - 1)
    body = "⚠ " + " ".join(counterexample_sentence(ce, lang).split())
    return "\n".join(_wrap_comment(body, _COMMENT_WIDTH - indent - len("# ")))


def _counterexample_lines(entry: dict, cont: str = "#       ") -> list[str]:
    """The ⚠ line under a key whose value has a measured counter-example.

    Rendered FROM the registry field, never authored per pack — the same
    discipline as the tier warnings next door: a header must not be able to
    claim something the registry contradicts. Absence of the field renders
    nothing at all, so "no line" means "no counter-example measured" and never
    "validated".
    """
    ce = entry.get("value_counterexample")
    if not ce:
        return []
    verdict = counterexample_verdict(ce)
    body = (f"⚠ 參考庫實測反例（#{ce['issue']}）："
            f"{counterexample_observed(ce, 'zh')} —— {verdict}；"
            f"這個數字是起點，不是平台背書的值")
    # `_wrap_comment` owns the no-split rule (both flags). The note that used to
    # sit here named only the hyphen half.
    return [f"{cont}{seg}"
            for seg in _wrap_comment(body, _COMMENT_WIDTH - len(cont))]


# ---------------------------------------------------------------------------
# optional_overrides — ONE tier, TWO populations, two different truths (#1310)
# ---------------------------------------------------------------------------
# Until PR-C (#1314) the whole tier got a SINGLE warning stating that a
# tenant-set key "永遠不會生效 ... 永久無法啟用". That sentence is now FALSE for
# half the tier and still TRUE for the other half:
#
#   * FLAT keys (today oracle_* / db2_* / clickhouse_*) — ``resolveDeclaredRows``
#     walks the DECLARED set and emits a row as soon as the TENANT supplies a
#     value, and #1310 ships that declared list itself in ``_defaults.yaml`` /
#     Helm values. Tenant fills one in → it fires. No platform fallback, by
#     design: the platform asserts no value.
#   * ``<base>_critical`` keys — never on the list. ``resolveDeclaredRows``
#     skips the critical suffix outright and ``resolveCriticalRows`` keys off
#     ``defaults[base]``, so they are deliberately EXCLUDED (#1311): listing all
#     25 tier keys would make 16 of them decoration.
#
# ⛔ But "off the list" is NOT "dormant", and conflating the two is the second
# mistake this block has now made in both directions. ``resolveCriticalRows``
# needs ``defaults[base]``, and for 5 of the 16 (the postgresql / mariadb ones)
# that base IS a shipped chart default — so a tenant filling one in today gets a
# real critical row. Only the other 11, whose base is not shipped either, are
# actually dormant.
#
# So the warning is chosen PER KEY by TWO derived predicates —
# ``is_shipped_optional_key`` (the SAME one that decides list membership, so
# header claim and shipped list cannot drift apart) and ``has_shipped_chart_base``
# (which asks the resolver's real entry condition). See
# ``optional_warning_groups``.


def is_shipped_optional_key(key: str) -> bool:
    """Whether an ``optional_overrides``-tier key belongs in the SHIPPED list.

    Flat keys only. Identical predicate to the one ``scaffold_tenant`` uses for
    the same split (``not key.endswith("_critical") and "{" not in key``), which
    in turn mirrors the exporter's own refusals in
    ``components/threshold-exporter/app/pkg/config/resolve.go``:
    ``resolveDeclaredRows`` skips both the ``_critical`` suffix (that tier is
    ``resolveCriticalRows``' business, and it needs ``defaults[base]``) and
    dimensional ``key{...}`` tokens (already emitted by
    ``resolveDimensionalRows``).

    NOTE the registry models no dimensional keys today (they are scaffold's
    ``dimensional_example`` domain, see this module's SCOPE note), so the
    ``{`` arm is defence, not a live branch.
    """
    return not key.endswith(CRITICAL_SUFFIX) and "{" not in key


def shipped_optional_keys_for_packs(
    pack_names, rule_packs: Optional[dict] = None
) -> list[str]:
    """The shipped ``optional_overrides:`` key names for a SELECTION of packs.

    The onboarding generators (``scaffold_tenant.generate_defaults`` and
    ``init_project._gen_defaults_yaml``) write a customer-side ``_defaults.yaml``
    for the packs that customer selected, and that file is the ONLY declared-key
    source the tenant-api write path ever reads
    (``config.mergeTenantConfig`` reads the root carrier the exporter's chain
    selects — ``_defaults.yaml`` for these generators; #1674). They must
    ship the same names the chart does, or a tenant on the GitOps topology gets
    a 400 for a key the platform believes it declared.

    ⛔ Both call this instead of re-spelling ``not endswith("_critical")``.
    Since #1196 ``init_project`` also takes its ``defaults:`` VALUES from
    ``scaffold_tenant.RULE_PACKS`` (its former own copy spelled keys the
    exporter never read); this function stays the declared-key half of that
    sharing — it unifies only the declared-key
    derivation, so the two generators cannot disagree about which keys a tenant
    may set. Splitting that derivation in two would just have created a fourth
    hand-copy of the predicate (#1189).

    Order is RULE_PACKS order (== registry pack order), not the caller's
    selection order, so a full selection renders byte-identical to
    ``optional_override_list_keys``.
    """
    if rule_packs is None:
        rule_packs = _load_scaffold().RULE_PACKS
    selected = set(pack_names)
    return [
        key
        for pack, meta in rule_packs.items()
        if pack in selected
        for key in (meta or {}).get("optional_overrides", {})
        if is_shipped_optional_key(key)
    ]


def shipped_dimensions_for_packs(
    pack_names, rule_packs: Optional[dict] = None
) -> "dict[str, list[str]]":
    """The ``dimensions:`` declaration for a SELECTION of packs (#3475).

    The same reason as ``shipped_optional_keys_for_packs``: the onboarding
    generators write the customer-side root ``_defaults.yaml``, and on the
    GitOps topology (tenant-api reading the customer repo; an
    ``existingConfigMap`` / kustomize deployment) that file is the only
    ``dimensions:`` the exporter and the write gate see. Without it a
    ``container_cpu{version="v2"}`` the chart serves is unread there.

    Derived from each key's ``dimensions`` in ``scaffold_tenant.RULE_PACKS``,
    the field the registry's (and so the chart's) declaration is generated
    from; a full selection equals ``dimension_entries`` of the registry.
    Order is RULE_PACKS order."""
    if rule_packs is None:
        rule_packs = _load_scaffold().RULE_PACKS
    selected = set(pack_names)
    out: "dict[str, list[str]]" = {}
    for pack, meta in rule_packs.items():
        if pack not in selected:
            continue
        for tier in ("defaults", "optional_overrides"):
            for key, info in ((meta or {}).get(tier) or {}).items():
                if (info or {}).get("dimensions"):
                    # sorted, as build_registry_doc stores it: the generated
                    # file must match the registry surfaces label for label.
                    out[key] = sorted(info["dimensions"])
    return out


def has_shipped_chart_base(doc: dict, key: str) -> bool:
    """Whether an UNSHIPPED optional key's base is itself a shipped chart default.

    Only ``<base>_critical`` keys have a base at all (``critical_of`` is derived,
    never authored — see ``build_registry_doc``). ``resolveCriticalRows`` emits a
    row the moment ``defaults[base]`` carries a value, so a ``_critical`` whose
    base is a shipped chart default is settable by a TENANT TODAY even though the
    key itself is (correctly) off the declared list — it never needed to be on
    it. (Shipped = the base's ``arming`` is ``default``.) A ``_critical`` whose base is NOT shipped is the genuinely dormant case:
    the deployment must supply the base first.

    ⛔ This distinction is why the pack-header warning is three-way and not two.
    The pre-#1310 single warning, and the two-way split that first replaced it,
    both told postgresql/mariadb readers their ``_critical`` keys "並不會發射任何
    user_threshold" — false for all five of them, and false in the direction that
    talks a tenant out of protection they already have.
    """
    return "helm" in base_topologies(doc, key)


def base_topologies(doc: dict, key: str) -> frozenset[str]:
    """The topologies (``TOPOLOGY_DEFAULTS_KEEPS``) whose generated defaults
    value the base of ``key`` — where ``resolveCriticalRows`` has the
    ``defaults[base]`` it admits a ``<base>_critical`` on. Empty for a key
    with no base (no ``critical_of``)."""
    keys = doc.get("keys", {})
    base = (keys.get(key) or {}).get("critical_of")
    if not base:
        return frozenset()
    entry = keys.get(base) or {}
    pack = entry.get("pack", "?")
    return frozenset(
        topo for topo, keeps in TOPOLOGY_DEFAULTS_KEEPS.items()
        if any(keep(pack, entry) for keep in keeps))


# Warning A — the keys the platform SHIPS in the optional_overrides: list.
_OPTIONAL_SHIPPED_WARNING_LINES = (
    "#",
    "# optional_overrides（已出貨在清單裡——平台認得這個 key，但不主張它的值）：",
    "# 下列 key 由平台出貨在 _defaults.yaml / Helm values 的 `optional_overrides:`",
    "# 清單中，租戶可直接在自己的 conf.d 填值並生效（resolveDeclaredRows 走宣告集，",
    "# 只在租戶給了值時發射；components/threshold-exporter/app/pkg/config/resolve.go）。",
    "# 平台不主張值 ⇒ 沒有平台回退：租戶不填就是靜默，那正是這一格的用意。",
    "# ⚠️ 下列數字是「參考起點」不是背書——正式採用前必須以該租戶實際 baseline 校準。",
    "# 已測到反例的那幾個，反例逐條標在該 key 下（registry `value_counterexample`）。",
)
# ⛔ The three counter-examples used to be spelled out HERE, as prose, inside a
# GENERATED block — and the Oracle header enumerated DB2's case, so a pack
# header asserted a fact about a pack it does not own. That is the hand-copied
# contract this whole registry exists to delete: re-tuning db2_deadlock_rate
# would have left two other packs quietly claiming the old number. They are now
# rendered per key from `value_counterexample`, which also means a pack with no
# measured counter-example silently gets no such claim (#1176).

# Warning B — off the shipped list, but the BASE is a shipped chart default, so
# `resolveCriticalRows` already has everything it needs. These keys are settable
# by a tenant TODAY. Saying otherwise (as the single pre-#1310 warning and its
# first two-way replacement both did) is the expensive direction of wrong: it
# talks a reader out of protection that is already available.
_OPTIONAL_CRITICAL_BASE_SHIPPED_WARNING_LINES = (
    "#",
    "# optional_overrides（不在出貨清單裡，但 base 已出貨——租戶今天就設得動）：",
    "# 下列 `<base>_critical` 不在平台出貨的 `optional_overrides:` 清單裡，也不需要列進去。",
    "# 它們的解析走 resolveCriticalRows，入場條件是 `defaults[base]` 有值",
    "# （components/threshold-exporter/app/pkg/config/resolve.go），",
    "# 而下列每個 key 的 base 都標了",
    "# [chart-default]＝出貨的 Helm values / dev conf.d 範本就供給它。⇒ 租戶在自己的",
    "# conf.d 填一個，ValidateTenantKeys 放行、resolveCriticalRows 產出一條真的",
    "# critical row。清單刻意不收 _critical 也是同一個理由：它們用不到宣告集（#1311）。",
    "# ⚠️ 下列數字是「參考起點」不是背書——正式採用前必須以該租戶實際 baseline 校準。",
)

# Warning B2 — off the shipped list; the base is valued by the repo's dev
# conf.d template (its `dev-example-defaults` block) but not by the chart. True
# in one topology and false in the other, so the header says both (#3730).
_OPTIONAL_CRITICAL_BASE_DEV_ONLY_WARNING_LINES = (
    "#",
    "# optional_overrides（不在出貨清單裡；base 只在 repo 的 dev conf.d 範本供給，Helm chart 不出貨）：",
    "# 下列 `<base>_critical` 的解析走 resolveCriticalRows，入場條件是 `defaults[base]` 有值",
    "# （components/threshold-exporter/app/pkg/config/resolve.go）。",
    "# - repo 的 dev conf.d（components/threshold-exporter/config/conf.d/_defaults.yaml 的",
    "#   dev-example-defaults 段）供給下列每個 key 的 base：在這棵樹裡租戶填一個，",
    "#   resolveCriticalRows 就產出一條真的 critical row。",
    "# - 出貨的 Helm values 不供給這些 base：用 chart 部署時，租戶填一個並不會產出任何 row，",
    "#   要先由部署面供給 base（scaffold_tenant 針對選到的 pack 產生的 _defaults.yaml",
    "#   就含 base，operator 也可自行填 Helm values）。",
    "# ⚠️ 下列數字是「參考起點」不是背書——正式採用前必須以該租戶實際 baseline 校準。",
)

# Warning C — off the shipped list AND the base is valued in neither topology.
# This is the only population the original "documented-but-dormant" wording is
# still true for. Worded by the property that actually gates them
# (`defaults[base]`), not by the `_critical` spelling, so it stays true if the
# populations shift.
_OPTIONAL_UNSHIPPED_WARNING_LINES = (
    "#",
    "# optional_overrides（不在出貨清單裡，base 也未出貨——documented-but-dormant）：",
    "# ⛔ 啟用前提（別跳過）：下列 key **不在**平台出貨的 `optional_overrides:` 清單，",
    "# 所以走不到 resolveDeclaredRows；`<base>_critical` 的解析走 resolveCriticalRows，",
    "# 它讀的是 defaults[base]，base 沒有值就整列丟棄",
    "# （components/threshold-exporter/app/pkg/config/resolve.go）。",
    "# 而下列每個 key 的 base，出貨的 Helm values 與 repo 的 dev conf.d 範本都沒有供給：",
    "# 租戶在這兩種拓撲下填一個並不會發射任何 user_threshold。",
    "# 要能用，部署面必須先供給 base（scaffold_tenant 針對選到",
    "# 的 pack 產生的 _defaults.yaml 就含 base，operator 也可自行填 Helm values），",
    "# critical 覆寫才有東西可加嚴。清單刻意排除 _critical 正是因為如此：放進去只會是",
    "# 一格裝飾（#1311）。",
)


# Warning D — off the shipped list AND the base itself sits in the
# optional_overrides tier (#3778: mysql_threads_running went operator-armed).
# Warning C's remedy — "scaffold_tenant's _defaults.yaml carries the base" —
# is false here: generators list an optional-tier base under
# `optional_overrides:`, never under `defaults:`, and a tenant-level value of
# the base is not `defaults[base]` — nor is a subtree `_defaults.yaml` or a
# profile, which merge into the tenant's overrides (resolve.go
# criticalRowRead reads the ROOT defaults). The only remedy is the deployer
# writing the base under the ROOT `defaults:` by hand.
_OPTIONAL_CRITICAL_BASE_OPTIONAL_WARNING_LINES = (
    "#",
    "# optional_overrides（不在出貨清單裡；base 本身也是 optional_overrides 層）：",
    "# 下列 `<base>_critical` 的解析走 resolveCriticalRows，入場條件是 `defaults[base]` 有值",
    "# （components/threshold-exporter/app/pkg/config/resolve.go）。",
    "# 而 base 屬 optional_overrides 層：沒有任何產生器把它寫進 `defaults:`",
    "# （出貨的 Helm values、repo 的 dev conf.d 範本、scaffold_tenant 都只把它列進",
    "# `optional_overrides:` 清單），租戶在自己的 conf.d 填 base 也不算 `defaults[base]`。",
    "# ⇒ 要讓 critical 覆寫產出 row，部署面必須自己在根層 `defaults:`（根目錄的",
    "# _defaults.yaml 或 Helm values）寫 base 的值；子樹 _defaults.yaml、profile、",
    "# CRD 的 spec.defaults（渲染成 <ns>/_defaults.yaml，屬子樹層）都不算 `defaults[base]`。",
    "# ⚠️ 下列數字是「參考起點」不是背書——正式採用前必須以該租戶實際 baseline 校準。",
)


def optional_warning_groups(doc: dict, optional: dict) -> list[tuple[tuple, dict]]:
    """[(warning_lines, {key: entry}), …] — the optional tier split by TRUTH.

    Five populations, five different truths, and the split is DERIVED
    (``is_shipped_optional_key`` for membership, ``base_topologies`` for the
    rest: base valued by the chart — and so by the dev template too —, by the
    dev template only, by neither because the base is itself optional-tier
    (#3778), or by neither) rather than spelled out per pack — the
    whole point is that a header can never claim something the resolver
    contradicts in either topology it speaks for (#3730).

    Empty groups are dropped, so a pack with no optional tier gets no warning
    line at all and a single-population pack gets exactly one.
    """
    keys = doc.get("keys", {})
    shipped, base_shipped, dev_only, base_optional, dormant = {}, {}, {}, {}, {}
    for key, entry in optional.items():
        topologies = base_topologies(doc, key)
        base = (keys.get(key) or {}).get("critical_of")
        if is_shipped_optional_key(key):
            shipped[key] = entry
        elif "helm" in topologies:
            base_shipped[key] = entry
        elif "dev" in topologies:
            dev_only[key] = entry
        elif base and (keys.get(base) or {}).get("tier") == "optional_overrides":
            base_optional[key] = entry
        else:
            dormant[key] = entry
    groups = [
        (_OPTIONAL_SHIPPED_WARNING_LINES, shipped),
        (_OPTIONAL_CRITICAL_BASE_SHIPPED_WARNING_LINES, base_shipped),
        (_OPTIONAL_CRITICAL_BASE_DEV_ONLY_WARNING_LINES, dev_only),
        (_OPTIONAL_CRITICAL_BASE_OPTIONAL_WARNING_LINES, base_optional),
        (_OPTIONAL_UNSHIPPED_WARNING_LINES, dormant),
    ]
    return [(w, keys) for w, keys in groups if keys]


def render_pack_header_lines(doc: dict, pack_name: str) -> list[str]:
    """The generated threshold section of one rule-pack header (body only).

    The optional tier renders as up to FOUR sub-sections, each under its own
    warning (see ``optional_warning_groups``): the keys the platform ships on
    the ``optional_overrides:`` list, the ``_critical`` keys whose base is a
    shipped chart default (settable today), the ones whose base only the
    repo's dev conf.d template values, and the ones whose base neither does
    (genuinely dormant). A pack gets exactly the sub-sections its keys populate,
    in that order; a pack with no optional-tier key gets no warning whatsoever.
    """
    by_pack = keys_by_pack(doc).get(pack_name, {})
    defaults = {k: e for k, e in by_pack.items() if e.get("tier") == "defaults"}
    optional = {
        k: e for k, e in by_pack.items() if e.get("tier") == "optional_overrides"
    }
    lines = [
        "# 對應的 threshold-exporter defaults"
        "（defaults tier——scaffold onboarding 供給；[chart-default]=chart 出貨即啟用；"
        "未標者 chart 不出貨，「未啟用」一行寫原因，需部署面顯式供給後才發射可達）:",
    ]
    for key, entry in defaults.items():
        lines += _entry_lines(key, entry)
    for warning, keys in optional_warning_groups(doc, optional):
        lines += list(warning)
        for key, entry in keys.items():
            lines += _entry_lines(key, entry)
    return lines


def _critical_sibling(doc: dict, key: str) -> Optional[tuple[str, dict]]:
    crit = registry_keys(doc).get(key + CRITICAL_SUFFIX)
    if crit is not None and crit.get("critical_of") == key:
        return key + CRITICAL_SUFFIX, crit
    return None


def render_chart_defaults_lines(doc: dict, indent: int) -> list[str]:
    """The chart-shipped ``defaults`` mapping body (helm values / dev template).

    Only ``arming: default`` keys, grouped by pack in registry order.
    unit+desc render as comment lines above each key; when the registry
    carries a ``<key>_critical`` sibling, an opt-in hint (with the registry's
    suggested value) is appended so the hand-written hints the old copies
    carried are reproduced mechanically.
    """
    return _render_defaults_lines(doc, indent, _chart_default_keep)


def render_unarmed_lines(doc: dict, indent: int) -> list[str]:
    """The comment block under the chart's ``defaults``: every defaults-tier
    key a rule pack reads but the chart does not ship, with why (D-A A1).

    Comments only, so the mapping it sits in is unchanged. An operator who
    only ran ``helm install`` reads here which alerts stay silent until they
    supply a value, without opening the registry. Empty when every
    defaults-tier key is chart-armed."""
    ind = " " * indent
    width = _COMMENT_WIDTH - indent - 2
    groups = _defaults_by_pack(
        doc, lambda _p, e: (e.get("tier") == "defaults"
                            and e.get("arming") == ARMING_OFF
                            and bool(arming_notes_zh(e))))
    if not groups:
        return []
    lines = [f"{ind}# {seg}" for seg in _wrap_comment(
        "下列 key 規則包會讀，但 chart 預設不出貨值：helm install 後對應的告警不會響。"
        "用 chart 自建的 ConfigMap 時，在上面的 defaults 補值；設了 "
        "thresholdConfig.existingConfigMap 時 chart 不讀這裡，要在那個 ConfigMap 的來源補值"
        "（其 <key>_critical 也要先有 base 值）。"
        "原因取自 threshold-registry 的 arming、portability 與 blocker 欄位。", width)]
    for pack_name, _meta, pairs in groups:
        lines.append(f"{ind}# ── {pack_name} ──")
        for key, entry in pairs:
            # Every note names its key: a second reason indented like a
            # wrapped continuation read as part of the first.
            for note in arming_notes_zh(entry):
                segs = _wrap_comment(f"{key} {note}", width - 2)
                lines.append(f"{ind}#   {segs[0]}")
                lines.extend(f"{ind}#     {seg}" for seg in segs[1:])
            # Same discipline as every other face that hands a value over
            # (test_counterexample_coverage): a key with a measured
            # counter-example carries it here too.
            lines.extend(_counterexample_lines(entry, f"{ind}#     "))
    return lines


# The packs whose `defaults`-tier keys the dev template's SECOND defaults block
# arms (#3014). They are exactly the packs the shipped `conf.d/examples/`
# tenants are written against. ⛔ Not "every non-chart pack": the dev tree is
# what every doc points at, and arming kafka/oracle/... for db-a would add
# series nothing in the tree asks for. ⛔ Not a subtree `_defaults.yaml` either
# — that is where these keys used to sit (as `_defaults-multidb.yaml`, a name
# the loader skips), and moving them to `examples/_defaults.yaml` does not work
# today: a key that only a SUBTREE carrier declares never reaches /metrics
# (#1976). The guard that this tuple is wide enough is
# `test_shipped_example_tree_serves_every_tenant_key`, which fails the day an
# example tenant uses a pack listed nowhere here.
DEV_EXAMPLE_PACKS: tuple[str, ...] = ("redis", "elasticsearch", "mongodb")


def render_dev_example_defaults_lines(doc: dict, indent: int) -> list[str]:
    """The dev template's ``defaults`` keys for the example tenants' packs.

    Every ``defaults``-tier key of ``DEV_EXAMPLE_PACKS`` that is not already a
    chart default (those render in the block above). Same layout as
    ``render_chart_defaults_lines``, so the counter-example and ``_critical``
    hints come out of one code path.
    """
    return _render_defaults_lines(doc, indent, _dev_example_keep)


def _chart_default_keep(pack: str, entry: dict) -> bool:
    return is_chart_armed(entry)


def _dev_example_keep(pack: str, entry: dict) -> bool:
    return (pack in DEV_EXAMPLE_PACKS and entry.get("tier") == "defaults"
            and not is_chart_armed(entry))


# The two topologies a pack header speaks for, and the defaults blocks each
# one's generated `defaults:` mapping carries — by the very keep predicates
# `regen_surfaces` renders those blocks with (helm values: the chart-default
# block; the repo's dev conf.d `_defaults.yaml`: the same block plus the
# example tenants' `dev-example-defaults` block). A header never asks one
# topology and speaks for the other (#3730);
# `test_topology_keeps_are_what_the_surfaces_carry` holds this table to the
# generated files.
TOPOLOGY_DEFAULTS_KEEPS: dict[str, tuple] = {
    "helm": (_chart_default_keep,),
    "dev": (_chart_default_keep, _dev_example_keep),
}


def _defaults_by_pack(doc: dict, keep) -> list[tuple[str, dict, list]]:
    """[(pack_name, pack_meta, [(key, entry), …]), …] — the keys
    ``keep(pack, entry)`` admits, grouped by pack in registry order; packs
    with none are omitted. The one traversal behind both the rendered block
    and the entries ``regen_surfaces`` expects it to hold."""
    grouped = keys_by_pack(doc)
    out: list[tuple[str, dict, list]] = []
    for pack_name, pack_meta in (doc.get("packs", {}) or {}).items():
        pairs = [(k, e) for k, e in grouped.get(pack_name, {}).items()
                 if keep(pack_name, e)]
        if pairs:
            out.append((pack_name, pack_meta, pairs))
    return out


def _render_defaults_lines(doc: dict, indent: int, keep) -> list[str]:
    """A ``defaults`` mapping body: the keys ``keep(pack, entry)`` admits,
    grouped by pack in registry order."""
    ind = " " * indent
    lines: list[str] = []
    for pack_name, pack_meta, chart_keys in _defaults_by_pack(doc, keep):
        lines.append(f"{ind}# ── {pack_name}：{pack_meta['display']} ──")
        for key, entry in chart_keys:
            meta = f"{entry['unit']} — {entry['desc']}"
            sibling = _critical_sibling(doc, key)
            if sibling is not None:
                crit_key, crit = sibling
                meta += (
                    f"（critical 加嚴 opt-in：{crit_key}，"
                    f"registry 建議 {_fmt_value(crit['value'])}）"
                )
            for seg in _wrap_comment(meta, _COMMENT_WIDTH - indent - 2):
                lines.append(f"{ind}# {seg}")
            # Wired now, not "when a chart-default key gets one": no shipped
            # chart default carries a counter-example today, so this emits
            # nothing — but the alternative is a face that silently ships a
            # bare number the day the registry learns something about it.
            lines.extend(_counterexample_lines(entry, f"{ind}# "))
            # A shipped value the registry records as non-portable says so
            # here too, where the operator reads it (#3773 review F5). Only
            # for chart defaults: the dev template's example block ships keys
            # the chart does not, and "not shipped" would contradict it.
            if is_chart_armed(entry):
                lines.extend(_arming_note_lines(entry, f"{ind}# "))
            lines.append(f"{ind}{key}: {_fmt_value(entry['value'])}")
    return lines


_COMMENT_WIDTH = 100


def _wrap_comment(body: str, width: int) -> list[str]:
    """Wrap prose for a generated comment block WITHOUT ever splitting a token.

    ⛔ BOTH flags, and neither is style. `textwrap` defaults to breaking on
    hyphens AND to breaking long words, so either one can cut an identifier or a
    path in half across the two comment lines it emits — and a reference split
    that way is invisible to `git grep`, which is the #1373 accident shape that
    `tests/ops/test_wrapped_path_references.py` exists to catch.

    ⛔ THE POINT OF THIS FUNCTION IS THAT THERE IS ONLY ONE. The hyphen half was
    fixed at three of the six call sites in an earlier change (its comment names
    `negative-db2.yaml` being cut after the hyphen); the long-word half was
    fixed at NONE of them, and the other three never got the hyphen fix either.
    Six call sites meant six chances to get half of it. Deciding here means a
    caller cannot (#1453).

    ⚠️ THAT SENTENCE WAS TRUE OF THE FLAGS AND FALSE OF THE WIDTH when it was
    written: three call sites still passed a bare `100` while three used
    `_COMMENT_WIDTH`, so the base column budget was exactly the kind of
    half-applied decision this function exists to end — the same defect, one
    parameter over. All of them route through `_COMMENT_WIDTH` now, and nothing
    asserts that, so re-read it if you add a call site. What legitimately stays
    per-call-site is the SUBTRACTION (`- len(cont)` versus `- indent - 2`):
    different renderers spend different amounts of the line on their prefix.
    """
    return textwrap.wrap(body, width, break_on_hyphens=False,
                         break_long_words=False)


def _append_wrapped_comment(lines: list[str], item: str, meta: str, cont: str,
                            width: int = _COMMENT_WIDTH) -> list[str]:
    """Append ``item`` carrying ``meta`` as a trailing comment; wrap past ``width``.

    Both list-shaped renderers below (the shipped ``optional_overrides:`` list
    and the tenant stub's declared block) need the identical rule: keep the
    comment INLINE while the whole line fits in ``width`` columns, otherwise
    emit the item alone and continue ``meta`` on comment-only lines indented to
    ``cont``. Having written that rule twice is how the two blocks would
    eventually stop looking alike — they render the SAME declared set to two
    audiences, so a divergence in wrapping is a divergence nobody chose.

    ⚠️ THAT RULE IS NO LONGER "whatever each renderer did inline", and this
    paragraph used to say it was. The layout is unchanged — three spaces before
    the ``#``, continuation indented to ``cont`` — but the wrapping goes through
    ``_wrap_comment``, whose entire purpose is that ``textwrap``'s two splitting
    defaults are OFF (#1453). For any ``meta`` carrying a hyphenated or
    over-width token the output therefore DIFFERS from the old inline call, on
    purpose. ⚠️ No ``meta`` reaching THIS renderer carries such a token today, so
    reverting just this call site regenerates the surfaces byte-for-byte —
    measured. The regen diff #1453 landed came from a different renderer.

    Mutates and returns ``lines`` (the callers accumulate into one list).
    """
    inline = f"{item}   # {meta}"
    if len(inline) <= width:
        lines.append(inline)
        return lines
    lines.append(item)
    lines += [f"{cont}{seg}" for seg in _wrap_comment(meta, width - len(cont))]
    return lines


def _shipped_optional_by_pack(doc: dict) -> list[tuple[str, dict, list]]:
    """[(pack_name, pack_meta, [(key, entry), …]), …] — shipped optional keys.

    Deterministic order, and the ONLY traversal used by both the renderer and
    ``optional_override_list_keys``: registry pack order (``doc["packs"]``),
    then registry key order within each pack. Packs with no shipped optional
    key are omitted entirely.
    """
    grouped = keys_by_pack(doc)
    out: list[tuple[str, dict, list]] = []
    for pack_name, pack_meta in (doc.get("packs", {}) or {}).items():
        pack_keys = [
            (k, e)
            for k, e in grouped.get(pack_name, {}).items()
            if e.get("tier") == "optional_overrides" and is_shipped_optional_key(k)
        ]
        if pack_keys:
            out.append((pack_name, pack_meta, pack_keys))
    return out


def optional_override_list_keys(doc: dict) -> list[str]:
    """The key names shipped in the runtime ``optional_overrides:`` list.

    Same traversal + predicate as ``render_optional_overrides_lines``, exposed
    so callers can assert membership without re-parsing rendered comments.
    """
    return [k for _n, _m, pairs in _shipped_optional_by_pack(doc) for k, _e in pairs]


def render_optional_overrides_lines(doc: dict, indent: int) -> list[str]:
    """The shipped ``optional_overrides:`` LIST body (helm values / dev template).

    A YAML **array of strings**, not a ``key: value`` mapping — the platform
    RECOGNISES these keys and asserts NO value for them
    (``docs/schemas/platform-defaults.schema.json`` types ``optional_overrides``
    as ``array of string``; a value here would arm the key for every tenant,
    the exact opposite of the tier's meaning).

    ORDER: registry pack order, then registry key order within the pack (see
    ``_shipped_optional_by_pack``) — the same traversal
    ``render_chart_defaults_lines`` uses, so the two blocks read alike.

    Each item carries the registry's ``unit — desc`` as an INLINE comment,
    wrapped onto comment-only continuation lines when it would pass 100
    columns (``_entry_lines`` house style — the wrapping itself is
    ``_append_wrapped_comment``, shared with the tenant stub renderer).
    Everything added is a comment, so the block still parses as a plain list of
    key names — ``regen_surfaces`` reads each spliced conf.d file back through
    ``da-guard parse-tree`` before writing and refuses one that does not read
    as exactly these names, which would catch a regression here.
    """
    ind = " " * indent
    cont = f"{ind}  # "
    lines: list[str] = []
    for pack_name, pack_meta, pack_keys in _shipped_optional_by_pack(doc):
        lines.append(f"{ind}# ── {pack_name}：{pack_meta['display']} ──")
        for key, entry in pack_keys:
            _append_wrapped_comment(
                lines, f"{ind}- {key}", f"{entry['unit']} — {entry['desc']}", cont)
            # The tenant-facing copy of this same declared set already carries
            # the caveat per key; an operator reading the chart got less than a
            # tenant reading their own file. "Names a key" is the trigger, not
            # "hands over a number" — a reader who goes and looks the key up is
            # exactly the reader about to copy the reference figure (#1344).
            #
            # ⚠️ BELOW the item and indented, whereas
            # `defaults_counterexample_head` puts it ABOVE the key. Not an
            # oversight: this block is a YAML LIST whose items already carry a
            # trailing `# unit — desc` comment that continues on indented
            # comment lines (`_append_wrapped_comment`), so the caveat is the
            # next continuation of the item it belongs to. A mapping key has no
            # such continuation, and a comment above it is the only position
            # that reads as a preamble rather than as a note on the PREVIOUS
            # key. Same composer, two host grammars.
            lines.extend(_counterexample_lines(entry, cont))
    return lines


# ---------------------------------------------------------------------------
# The SAME declared set, told to the OTHER audience (#1321)
# ---------------------------------------------------------------------------
# Everything above renders the declared tier for a reader who owns the PLATFORM
# side: rule-pack headers, helm values, the dev conf.d template. None of those
# files is one a tenant ever opens. The single file a tenant does open is its
# own ``<tenant>.yaml``, and until #1321 that file's generated header said the
# opposite of the truth ("Omitted keys inherit from _defaults.yaml" /
# "省略=Default") — true for every key with a platform default, FALSE for the
# declared tier, which has no value to inherit at all.
#
# So the tenant stub gets its own rendering of the declared set. Deliberately
# NOT a second derivation: both generators hand this renderer a key list that
# came out of ``shipped_optional_keys_for_packs`` (``init_project`` calls it
# directly; ``scaffold_tenant`` passes the very list ``generate_defaults``
# already wrote into the sibling ``_defaults.yaml``, so the stub cannot
# advertise a key that file does not declare — that mismatch is exactly what
# earns a tenant an HTTP 400 from the only supported writer).
#
# ⛔ The values render as REFERENCE-ONLY comment text and every emitted key line
# is itself a comment carrying a ``<your value>`` placeholder. Filling a real
# number in for the tenant would re-arm, on the generated-artifact surface, the
# exact keystroke #1310/PR-C removed from the interactive prompt (see
# ``scaffold_tenant.prompt_value``): ADR-030's blind-write reference library has
# measured counter-examples for these very numbers.

TENANT_STUB_DECLARED_HEADING = {
    "zh": "# ── 平台已宣告、但不主張值的 key（_defaults.yaml 的 optional_overrides 清單）──",
    "en": "# -- Declared keys: the platform recognises these but asserts no value --",
}

_TENANT_STUB_PLACEHOLDER = {"zh": '"<你的值>"', "en": '"<your value>"'}

_TENANT_STUB_PROSE = {
    "zh": (
        "#",
        "# 平台「認得」下列 key：你在上面 tenants: 底下自己填值就會生效，不必再找",
        "# operator 開通。但平台刻意不給它們預設值 ⇒ 不填＝沒有值＝不產生 series，",
        "# 不會有告警，也不會有任何錯誤訊息。那是設計，不是漏掉的預設——所以本檔開頭",
        "# 的三態在這一格只有兩態：填值，或靜默（沒有「省略＝用預設」可用）。",
        "# ⚠️ 下列數字是「參考起點」不是背書：這種閾值只有你自己的 baseline 校準得",
        "#    出來。平台自家的盲寫參考庫（ADR-030）已經對其中一些值量到反例，逐條",
        "#    標在該 key 自己底下——沒有標的就是沒測過，永遠不讀作「已驗證」。",
        "# 用法：整行複製到上面自己那個租戶底下、去掉開頭的「# 」、把佔位字串換成你",
        "#    校準出來的數字（縮排已對齊）。若上面該租戶目前是 `<tenant>: {}`（空設",
        "#    定），先把 ` {}` 刪掉再貼——flow mapping 底下接縮排行不是合法 YAML。",
    ),
    "en": (
        "#",
        "# The platform RECOGNISES these keys: set one under `tenants:` above and",
        "# it takes effect — no operator action needed. But the platform ships no",
        "# default for them, so leaving one out means NO value at all: no series,",
        "# no alert, and no error message either. That silence is the design, not",
        "# a missing default — the three-state rule at the top of this file has",
        "# only two states here: set a value, or stay silent.",
        "# WARNING: the numbers below are a STARTING REFERENCE, not an endorsement",
        "#    — only your own baseline can calibrate this tier. Where the",
        "#    platform's own blind-write reference library (ADR-030) measured a",
        "#    counter-example it is noted under that key; no note means nothing",
        "#    was measured, and never means 'validated'.",
        "# To use: copy a whole line under your tenant above, drop the leading",
        "#    '# ', and replace the placeholder with a number you calibrated",
        "#    yourself (the indent already lines up). If your tenant currently",
        "#    reads `<tenant>: {}`, delete the ` {}` first — indented lines under",
        "#    a flow mapping are not valid YAML.",
    ),
}

_TENANT_STUB_META = {
    "zh": "{desc} — 參考起點 {value} {unit}（非背書，須依自身 baseline 校準）",
    "en": "{desc} — reference start {value} {unit} (not an endorsement; calibrate)",
}


def stub_counterexample_lines(ce: dict, cont: str, lang: str = "zh") -> list[str]:
    """The ⚠ continuation lines under ONE stub key that has a measured
    counter-example.

    ⛔ Per KEY, deliberately. This block used to carry a single hand-written
    paragraph naming two Oracle cases, printed unconditionally — so a db2-only
    tenant was handed a caveat about two keys its file does not contain, while
    the one key in it that DOES have a measurement (``db2_deadlock_rate``) said
    nothing, and the blanket "both would over-fire" was the exact reversal the
    registry's ``direction`` enum exists to prevent (it is ``under_fire``).
    Two independent blind reviews found this as their top finding (#1344).
    """
    verdict = counterexample_verdict(ce, lang)
    mark = COUNTEREXAMPLE_MARKS[lang]
    if lang == "zh":
        body = (f"⚠ #{ce['issue']} {mark}："
                f"{counterexample_observed(ce, 'zh')}（{verdict}）")
    else:
        body = (f"⚠ #{ce['issue']} {mark}: "
                f"{counterexample_observed(ce, 'en')} ({verdict})")
    # No-split rule lives in `_wrap_comment`, for the same reason as the header
    # renderer: the defaults cut `negative-db2.yaml` in half across two lines.
    return [f"{cont}{seg}" for seg in
            _wrap_comment(body, _COMMENT_WIDTH - len(cont))]


def _optional_entry_index(rule_packs: Optional[dict] = None) -> dict[str, dict]:
    """key -> optional_overrides entry, across every pack (meta lookup only)."""
    if rule_packs is None:
        rule_packs = _load_scaffold().RULE_PACKS
    return {
        key: entry
        for meta in (rule_packs or {}).values()
        for key, entry in ((meta or {}).get("optional_overrides") or {}).items()
    }


def render_tenant_declared_stub_lines(
    keys, rule_packs: Optional[dict] = None, lang: str = "zh", indent: int = 4
) -> list[str]:
    """The tenant-stub comment block for ``keys`` (all lines are comments).

    ``keys`` is the caller's already-derived declared list (see the section
    note above — this function never re-derives it, so it cannot disagree with
    the ``_defaults.yaml`` written alongside). Empty list → empty block.

    Every key renders as ``# <indent>key: "<placeholder>"``, i.e. commented out
    AND valueless: dropping the leading ``"# "`` yields a line already at the
    right indent for ``tenants: <tenant>:``.

    ⚠️ Be precise about what the placeholder buys, because the natural claim —
    "an unsubstituted placeholder gets caught by validation" — is FALSE and was
    written here before it was measured. ``thresholdScalar`` in
    ``docs/schemas/tenant-config.schema.json`` is ``{"type": "string"}`` whose
    only pattern checks the ``:severity`` suffix (#3071), so
    ``key: "<your value>"`` (no ``:``) is a schema-valid threshold: measured,
    ``validate_config.py`` reports 5 checks / 5 pass / 0 warn on exactly that
    file. What the placeholder DOES buy is that no platform-chosen number is
    silently armed — the failure mode it removes is a wrong threshold quietly
    alerting, not a wrong threshold being rejected. The only signal a
    substituted-nothing key produces today is exporter-side and
    tenant-invisible: ``resolveDeclaredRows`` cannot parse the value, logs
    ``WARN: invalid declared threshold`` and emits no row (``resolve.go``), so
    the tenant sees silence — the same thing it would see had it never set the
    key at all. Tightening the schema is deliberately NOT done here: that
    validator is on the path of every tenant threshold, so it is its own change.
    """
    if not keys:
        return []
    entries = _optional_entry_index(rule_packs)
    prefix = f"# {' ' * indent}"
    cont = f"{prefix}  # "
    lines = [TENANT_STUB_DECLARED_HEADING[lang], *_TENANT_STUB_PROSE[lang]]
    for key in keys:
        entry = entries.get(key) or {}
        keyline = f"{prefix}{key}: {_TENANT_STUB_PLACEHOLDER[lang]}"
        if not entry:
            lines.append(keyline)
            continue
        meta = _TENANT_STUB_META[lang].format(
            desc=entry.get("desc", ""),
            value=_fmt_value(entry.get("value")),
            unit=entry.get("unit", ""),
        )
        # Same wrapping rule as the shipped `optional_overrides:` list — one
        # implementation, because the two blocks render the same declared set.
        _append_wrapped_comment(lines, keyline, meta, cont)
        ce = entry.get("value_counterexample")
        if ce:
            lines.extend(stub_counterexample_lines(ce, cont, lang))
    return lines


# ---------------------------------------------------------------------------
# The THIRD population — and its truth is per-GENERATOR, not per-repo (#1321)
# ---------------------------------------------------------------------------
# The tenant header names three groups: keys `_defaults.yaml` gives a value to,
# keys it only DECLARES, and `<base>_critical`. The third sentence was first
# written as a static claim ("in neither block; it fires once `<base>` has a
# value") — measured true for what ``scaffold_tenant`` writes and FALSE for what
# ``init_project`` writes in the very same command:
#
#   * ``scaffold_tenant.RULE_PACKS`` puts ZERO ``_critical`` in its ``defaults``
#     tier, and the shipped ``helm/threshold-exporter/values.yaml``
#     ``thresholdConfig.defaults`` carries zero as well.
#   * ``init_project.RULE_PACK_CATALOG`` USED TO put sixteen of them straight
#     into ``defaults`` (11 of its 15 packs — e.g. mariadb's
#     ``mysql_connections_critical: 150``), so ``da-tools init --rule-packs
#     mariadb`` wrote a ``_defaults.yaml`` whose ``defaults:`` armed nothing at
#     all for that key, while the ``<tenant>.yaml`` written by the SAME run
#     told the tenant it was already covered.
#
# ⛔ PAST TENSE from here down, and the tense is the point: #1218 moved those
# sixteen into the catalog's ``critical_overrides`` tier and
# ``check_threshold_reachability``'s defaults-tier face now fails CI on any
# producer that reintroduces the shape, so BOTH generators are in the
# ``_critical``-free regime today. The derivation below is kept exactly because
# that was not always true and need not stay true: a sentence pinned to either
# regime goes quietly false the next time a generator moves, which is the whole
# defect #1321 fixed and #1218 found the other half of. What IS fixed here is
# the header lying about it: the claim is DERIVED from the ``defaults:`` mapping
# the generator is about to write, so whichever regime a generator is in, the
# file says the true one.

def defaults_critical_keys(defaults) -> list[str]:
    """The ``<base>_critical`` names a ``defaults:`` mapping assigns a value to.

    ``defaults`` is the mapping the caller is ABOUT TO WRITE into its
    ``_defaults.yaml`` — not a pack roster, not a registry query. Both
    generators already hold it (``scaffold_tenant`` as
    ``defaults_data["defaults"]``, ``init_project`` as ``_catalog_defaults()``),
    so nothing new is derived and the header cannot drift from its sibling file.
    """
    return [k for k in (defaults or {}) if k.endswith(CRITICAL_SUFFIX)]


# Two regimes, two truths. Keyed (lang, defaults-ships-critical) so a generator
# in either regime gets prose that is true for the file it just wrote.
#
# ⛔ The True regime is a MISPLACEMENT REPORT, not the good case, and saying so
# is the whole point of #1218 / TRK-344. It first shipped as reassurance — "the
# critical row fires without you doing anything" — written from reading
# `resolveCriticalRows` rather than running it. Measured (pkg/config/
# critical_tier_placement_test.go): a `<base>_critical` under `defaults:` is
# walked by `resolveBaseRows`, `parseMetricKey` splits on the FIRST underscore,
# and the row that comes out is
# `{component="<prefix>", metric="<rest>_critical", severity="warning"}`
# — a series no recording rule joins — with no critical row anywhere. So the one
# branch that existed to describe a defective file was telling its reader the
# file was fine.
#
# No producer reaches this branch today (`check_threshold_reachability`'s
# defaults-tier face fails CI first), and it is kept rather than deleted for
# that reason: the alternative to a truthful branch is a KeyError inside a CLI
# a customer is running.
_TENANT_STUB_CRITICAL_NOTE = {
    ("zh", False): (
        "# 第三類 <base>_critical：本次一起產出的 _defaults.yaml 的 defaults: 一個都沒有列，",
        "# 平台不供給 critical 值 ⇒ 你在這裡填一個，只要 <base> 在 defaults: 有值，就會",
        "# 產生一條真的 critical 閾值（不填則無此列，base 的 warning 列不受影響）。",
    ),
    ("zh", True): (
        "# ⚠️ 第三類 <base>_critical：本次一起產出的 _defaults.yaml 把其中 {n} 個放進了",
        "# defaults:，那是放錯區塊、不是可繼承的平台值 —— critical 層只認租戶覆寫，那 {n} 個",
        "# 只會各發一條 warning 級的 <base>_critical series（無人消費），critical 列並不存在。",
        "# 要 critical 列請在本檔填 <base>_critical，只要 <base> 在 defaults: 有值就會生效。",
    ),
    ("en", False): (
        "# A third group is `<base>_critical`. The _defaults.yaml generated next to",
        "# this file lists none of them under `defaults:`, so the platform supplies",
        "# no critical value: set one here and it produces a real critical-severity",
        "# threshold as long as `<base>` has a value under `defaults:`; leave it out",
        "# and there simply is no critical row.",
    ),
    ("en", True): (
        "# ⚠️ A third group is `<base>_critical`. The _defaults.yaml generated next",
        "# to this file puts {n} of them under `defaults:` — the wrong section, not a",
        "# platform value you can inherit: the critical tier reads tenant overrides",
        "# only, so each of those {n} emits a warning-severity `<base>_critical`",
        "# series that nothing consumes, and no critical row exists. Set",
        "# `<base>_critical` HERE to get one; it takes effect while `<base>` has a",
        "# value under `defaults:`.",
    ),
}


def render_tenant_critical_note_lines(defaults, lang: str = "zh") -> list[str]:
    """The tenant header's ``<base>_critical`` paragraph, true for ``defaults``.

    ⛔ Derived from the artifact, never asserted statically. Both generators
    ship the False regime today, and that is precisely why the derivation has
    to stay: they did NOT always agree (``init_project`` shipped 16 ``_critical``
    keys under ``defaults:`` until #1218), and a sentence fixed to either regime
    is a sentence that goes quietly false the next time one of them moves. The
    count is rendered too — it is what makes the claim checkable against the
    sibling ``_defaults.yaml`` rather than merely plausible.
    """
    shipped = defaults_critical_keys(defaults)
    template = _TENANT_STUB_CRITICAL_NOTE[(lang, bool(shipped))]
    return [line.format(n=len(shipped)) for line in template]


# The header sentence that POINTS AT the block above — and the pointer is the
# half that was still asserted rather than derived (#1321 review). The block is
# emitted only when the declared list is non-empty (``tenant_declared_foot``
# returns None for an empty list), while both generators' headers
# said "the list is at the end of this file" unconditionally. Measured: the
# optional tier of mariadb / redis / postgresql / kafka / rabbitmq / jvm / nginx
# is `_critical`-only, so `shipped_optional_keys_for_packs` returns [] for every
# one of them — a tenant onboarded onto any of those packs got a header
# pointing at a section that is not in its file.
#
# ⛔ Not fixed by making the sentence conditional prose ("if there are declared
# keys, see the end of the file") — that hands the tenant the judgement call the
# generator already has the data to make. Same discipline as the `_critical`
# paragraph next door: render the claim FROM the artifact this run produces.
_TENANT_STUB_DECLARED_NOTE = {
    ("zh", True): (
        "# 宣告層（_defaults.yaml 的 optional_overrides: 清單）沒有值可繼承 ⇒"
        " 省略＝沒有值＝靜默，",
        "# 不是「用預設」；那一格只有「填值」與「不填」兩態。本次選的 pack 有 {n} 個"
        "這種 key，",
        "# 清單見檔尾。",
    ),
    ("zh", False): (
        "# 宣告層（_defaults.yaml 的 optional_overrides: 清單）沒有值可繼承 ⇒"
        " 省略＝沒有值＝靜默，",
        "# 不是「用預設」；那一格只有「填值」與「不填」兩態。本次選的 pack 一個這種"
        " key 都沒有，",
        "# 所以本檔沒有那一段可看（換 pack 時會有）。",
    ),
    ("en", True): (
        "# Omitting a key _defaults.yaml only DECLARES (its `optional_overrides:`",
        "# list) inherits NOTHING — the platform asserts no value there, so an unset",
        "# declared key stays silent. The {n} such keys for the packs selected here",
        "# are listed at the end of this file.",
    ),
    ("en", False): (
        "# Omitting a key _defaults.yaml only DECLARES (its `optional_overrides:`",
        "# list) inherits NOTHING — the platform asserts no value there, so an unset",
        "# declared key stays silent. The packs selected here declare no such key,",
        "# so this file carries no such list.",
    ),
}


def render_tenant_declared_note_lines(keys, lang: str = "zh") -> list[str]:
    """The tenant header's declared-tier paragraph, true for ``keys``.

    ``keys`` must be the SAME list the caller hands
    ``tenant_declared_foot`` — that is what makes "the list is at the end
    of this file" a fact about the file being written rather than a hope. Both
    generators hold exactly one such list per run (``scaffold_tenant`` from the
    sibling ``_defaults.yaml`` it just built, ``init_project`` from
    ``shipped_optional_keys_for_packs``), so nothing new is derived here.

    The count is rendered for the same reason ``render_tenant_critical_note_lines``
    renders one: it makes the claim checkable against the emitted block instead
    of merely plausible.
    """
    keys = list(keys or [])
    return [line.format(n=len(keys))
            for line in _TENANT_STUB_DECLARED_NOTE[(lang, bool(keys))]]


def tenant_declared_foot(
    keys,
    rule_packs: Optional[dict] = None,
    lang: str = "zh",
    indent: int = 4,
) -> Optional[str]:
    """The declared-key block of a tenant stub as the file's foot comment
    (`_lib_confedit.create_entry`'s *foot_comment*), or None for an empty
    list.

    A file-level reference section AFTER the tenant's YAML (da-guard writes
    it after the last node, set off by a blank line), not inside the
    tenant's own mapping: a stub with no overrides is ``<tenant>: {}`` (flow
    style), so there is no block-mapping context to put it in at all. The
    prose says so — copying a line into a tenant that still reads ``{}``
    needs the ``{}`` removed first, and a hint that quietly produces a YAML
    syntax error would be worse than no hint.
    """
    lines = render_tenant_declared_stub_lines(keys, rule_packs, lang, indent)
    if not lines:
        return None
    import _lib_confedit  # noqa: PLC0415  (scripts/tools, on the path above)
    return _lib_confedit.comment_block("\n".join(lines))


def _confd_mapping(doc: dict, keep) -> dict:
    """``confd`` of a ``defaults`` block: the keys it renders, in order."""
    return {"carrier": "defaults", "shape": "mapping",
            "keys": [k for _n, _m, pairs in _defaults_by_pack(doc, keep) for k, _e in pairs]}


def _confd_sequence(doc: dict) -> dict:
    """``confd`` of an ``optional_overrides`` block: the key names it lists."""
    return {"carrier": "optional_overrides", "shape": "sequence",
            "keys": list(optional_override_list_keys(doc))}


def dimension_entries(doc: dict) -> "list[tuple[str, list[str]]]":
    """(key, labels) of every registry key that declares ``dimensions``, in
    registry key order — the runtime ``dimensions:`` declaration (#3475)."""
    return [(k, list(e["dimensions"])) for k, e in (doc.get("keys") or {}).items()
            if e.get("dimensions")]


def render_dimensions_lines(doc: dict, indent: int) -> list[str]:
    """The shipped ``dimensions:`` MAPPING body: one ``<base>: [labels]`` per
    registry key that declares ``dimensions`` (#3475, ADR-038 §11). The
    exporter serves a dimensional key only when every label of its selector
    is listed here for its base."""
    ind = " " * indent
    return [f"{ind}{k}: [{', '.join(labels)}]" for k, labels in dimension_entries(doc)]


def _confd_dimensions(doc: dict) -> dict:
    """``confd`` of a ``dimensions`` block: the bases it declares."""
    return {"carrier": "dimensions", "shape": "mapping",
            "keys": [k for k, _l in dimension_entries(doc)]}


def render_block(surface_id: str, body_lines: list[str], indent: str = "") -> str:
    """Full generated block text (markers + body), newline-joined."""
    return "\n".join(
        [begin_marker(surface_id, indent), *body_lines, end_marker(surface_id, indent)]
    )


def surface_specs(doc: dict) -> list[dict]:
    """Every generated surface: id, absolute path, indent, rendered block.

    Order: the two ``defaults`` mappings (helm values, dev template), the dev
    template's example-pack ``defaults`` block (#3014), the two
    ``optional_overrides`` lists on the SAME two files (#1310), then one per
    threshold pack (packs without threshold keys —
    liveness/operational/custom-alerts — carry no generated block).

    Two surfaces per file is fine: ``regen_surfaces`` splices a file's
    surfaces one after another on the same text, so the second splice sees
    the first one's result.

    A surface that is a conf.d file (the dev template, the try-local seed)
    also carries ``confd``: the top-level key its block belongs under
    (``carrier``), that key's shape, and the keys the block holds.
    ``regen_surfaces`` hands its spliced text to ``da-guard write`` instead
    of writing it (ADR-036 §2), and refuses a block that does not read as
    sitting under its carrier (`_check_carriers`).
    """
    root = _repo_path_or_raise(_REPO_ROOT, "the generated surfaces")
    specs = [
        {
            "id": "helm-defaults",
            "path": HELM_VALUES_PATH,
            "indent": " " * 4,
            "body": render_chart_defaults_lines(doc, 4),
        },
        {
            # D-A A1: the defaults-tier keys the chart does NOT ship, and
            # why — comments right under the generated defaults mapping.
            "id": "helm-unarmed",
            "path": HELM_VALUES_PATH,
            "indent": " " * 4,
            "body": render_unarmed_lines(doc, 4),
        },
        {
            "id": "dev-defaults",
            "path": DEV_DEFAULTS_PATH,
            "indent": " " * 2,
            "body": render_chart_defaults_lines(doc, 2),
            "confd": _confd_mapping(doc, _chart_default_keep),
        },
        {
            # The example tenants' packs (#3014): a second block in the same
            # `defaults:` mapping, so the chart-default block above stays
            # byte-identical to helm's.
            "id": "dev-example-defaults",
            "path": DEV_DEFAULTS_PATH,
            "indent": " " * 2,
            "body": render_dev_example_defaults_lines(doc, 2),
            "confd": _confd_mapping(doc, _dev_example_keep),
        },
        {
            # thresholdConfig.optional_overrides — key at indent 2, list items
            # (and therefore the markers) at 4.
            "id": "helm-optional",
            "path": HELM_VALUES_PATH,
            "indent": " " * 4,
            "body": render_optional_overrides_lines(doc, 4),
        },
        {
            # top-level optional_overrides — key at indent 0, items at 2.
            "id": "dev-optional",
            "path": DEV_DEFAULTS_PATH,
            "indent": " " * 2,
            "body": render_optional_overrides_lines(doc, 2),
            "confd": _confd_sequence(doc),
        },
        {
            # try-local ships its own conf.d; same list, same shape.
            "id": "try-local-optional",
            "path": TRY_LOCAL_DEFAULTS_PATH,
            "indent": " " * 2,
            "body": render_optional_overrides_lines(doc, 2),
            "confd": _confd_sequence(doc),
        },
        # #3475: the labels a dimensional key may carry, per base — the same
        # three runtime surfaces as the optional_overrides list.
        {
            "id": "helm-dimensions",
            "path": HELM_VALUES_PATH,
            "indent": " " * 4,
            "body": render_dimensions_lines(doc, 4),
        },
        {
            "id": "dev-dimensions",
            "path": DEV_DEFAULTS_PATH,
            "indent": " " * 2,
            "body": render_dimensions_lines(doc, 2),
            "confd": _confd_dimensions(doc),
        },
        {
            "id": "try-local-dimensions",
            "path": TRY_LOCAL_DEFAULTS_PATH,
            "indent": " " * 2,
            "body": render_dimensions_lines(doc, 2),
            "confd": _confd_dimensions(doc),
        },
    ]
    grouped = keys_by_pack(doc)
    for pack_name, pack_meta in (doc.get("packs", {}) or {}).items():
        if not grouped.get(pack_name):
            continue
        specs.append({
            "id": f"pack-{pack_name}",
            "path": os.path.join(root, *pack_meta["rule_pack_file"].split("/")),
            "indent": "",
            "body": render_pack_header_lines(doc, pack_name),
            # a Prometheus rule file: read back by Prometheus' rule loader
            # (`plan_surfaces`), not as a Kubernetes manifest
            "rule_file": True,
        })
    for spec in specs:
        spec["block"] = render_block(spec["id"], spec["body"], spec["indent"])
    return specs


def _find_block(lines: list[str], surface_id: str, indent: str):
    """(begin_idx, end_idx) of the marker lines, or an error string."""
    b, e = begin_marker(surface_id, indent), end_marker(surface_id, indent)
    b_idx = [i for i, ln in enumerate(lines) if ln == b]
    e_idx = [i for i, ln in enumerate(lines) if ln == e]
    if len(b_idx) != 1 or len(e_idx) != 1:
        return (
            f"markers for surface {surface_id!r} not found exactly once "
            f"(begin×{len(b_idx)}, end×{len(e_idx)}) — the delimiters are "
            "load-bearing; restore them (git checkout) or re-carve the block"
        )
    if e_idx[0] <= b_idx[0]:
        return f"markers for surface {surface_id!r} are out of order"
    return b_idx[0], e_idx[0]


def check_surface(text: str, spec: dict) -> Optional[str]:
    """None if the file's generated block is byte-fresh, else an error."""
    lines = text.split("\n")
    found = _find_block(lines, spec["id"], spec["indent"])
    if isinstance(found, str):
        return f"{spec['path']}: {found}"
    b, e = found
    current = "\n".join(lines[b : e + 1])
    if current != spec["block"]:
        return (
            f"{spec['path']}: generated block {spec['id']!r} is STALE vs the "
            "registry — run check_threshold_registry.py --regen"
        )
    return None


def splice_surface(text: str, spec: dict) -> str:
    """Replace the delimited block with a fresh render (idempotent)."""
    lines = text.split("\n")
    found = _find_block(lines, spec["id"], spec["indent"])
    if isinstance(found, str):
        raise ValueError(f"{spec['path']}: {found}")
    b, e = found
    return "\n".join(lines[:b] + spec["block"].split("\n") + lines[e + 1 :])


class ConfdWriteRefused(Exception):
    """``da-guard write`` refuses a generated conf.d surface (ADR-036 §2):
    the message names every refused file and why; nothing was written —
    neither a conf.d file nor any other surface nor the registry."""


class SurfaceWriteStopped(Exception):
    """A `--regen` write stopped after a file was written: the message names
    the file whose write failed and every file already written — they are
    not rolled back — and nothing after it was written."""


class ConfdWriteStopped(SurfaceWriteStopped):
    """A generated conf.d surface's write stopped after the judgement had
    passed (a write error, a file another writer changed in between): the
    message names the files written and the ones that may have been — they
    are not rolled back — and nothing after them was written."""


def _check_carriers(confd: "list[tuple[str, str, list[dict]]]") -> None:
    """Every generated conf.d block of *confd* (``(path, new text, its
    specs)``) sits under the carrier its spec names: the spliced text, read
    by ``da-guard parse-tree`` at its own path, holds each key the block
    renders at ``<carrier>`` (a ``defaults`` mapping's key, an
    ``optional_overrides`` item). A block whose enclosing key was renamed by
    hand (``other_carrier:`` around the markers) would otherwise be written
    and read by nothing. ConfdWriteRefused naming every misplaced block
    (nothing written); `_lib_parse_tree.ParseTreeError` for no usable
    da-guard."""
    import _lib_parse_tree  # noqa: PLC0415
    bad: "list[str]" = []
    for path, text, specs in confd:
        rel = os.path.basename(path)
        node = _lib_parse_tree.load_file(os.path.dirname(path), rel,
                                         text.encode("utf-8")).node(rel)
        for spec in specs:
            carrier, keys = spec["confd"]["carrier"], spec["confd"]["keys"]
            held = node.get(carrier) if isinstance(node, dict) else None
            if spec["confd"]["shape"] == "mapping":
                have = (set(held) if isinstance(held, dict)
                        and not _lib_parse_tree.is_scalar(held) else set())
            else:
                have = ({i["text"] for i in held if _lib_parse_tree.is_scalar(i)}
                        if isinstance(held, list) else set())
            missing = [k for k in keys if k not in have]
            if missing:
                bad.append(f"{path}: the generated block {spec['id']!r} does not read as "
                           f"under {carrier!r} (parse-tree finds {len(missing)} of its "
                           f"{len(keys)} key(s) missing there, e.g. {missing[0]!r}) — the "
                           f"key around its markers is not {carrier!r}")
    if bad:
        raise ConfdWriteRefused("; ".join(bad) + " — nothing was written")


@contextlib.contextmanager
def _confd_batches(confd: "list[tuple[str, str]]"):
    """The `da-guard write` batches of the spliced conf.d files *confd*
    (``(path, new text)``): one per conf.d root (the file's directory), in
    the order the files come, each ``(root, entries)`` with one ``replace``
    entry per file — its whole new text, put in place byte for byte (a
    `_lib_confedit.copy_entry` of the text staged in a private directory
    outside every conf.d; the old file is replaced whole, its mode kept).
    The same texts give the same batches: the dry run that judges a plan
    and the write that carries it out are asked the same thing."""
    import tempfile  # noqa: PLC0415

    import _lib_confedit  # noqa: PLC0415  (scripts/tools, on the path above)
    from _lib_io import write_text_secure  # noqa: PLC0415  (LF kept, 0600)
    by_root: "dict[str, list[dict]]" = {}
    with tempfile.TemporaryDirectory(prefix="da-regen-surface-") as stage:
        for n, (path, text) in enumerate(confd):
            src = os.path.join(stage, str(n), os.path.basename(path))
            os.mkdir(os.path.dirname(src), 0o700)
            write_text_secure(src, text)
            by_root.setdefault(os.path.dirname(path), []).append(
                _lib_confedit.copy_entry(os.path.basename(path), src, replace=True))
        yield list(by_root.items())


def _refusal_lines(root: str, exc: Exception) -> "list[str]":
    """One line per file da-guard refused (or the caller error), each naming
    the file's path."""
    import _lib_confedit  # noqa: PLC0415
    if isinstance(exc, _lib_confedit.EditsRefused):
        return [f"{os.path.join(root, r.path) if r.path else root}: {r.reason}: {r.message}"
                for r in exc.refusals]
    return [f"{root}: {exc}"]


def _judge_confd(confd: "list[tuple[str, str]]") -> None:
    """Every batch of *confd* asked as a dry run (`da-guard write
    --dry-run`), every root judged before the answer: ConfdWriteRefused
    naming each refused file, file by file; `_lib_parse_tree.ParseTreeError`
    when there is no usable da-guard. Nothing is written either way."""
    import _lib_confedit  # noqa: PLC0415
    refused: "list[str]" = []
    with _confd_batches(confd) as batches:
        for root, entries in batches:
            try:
                _lib_confedit.write_all(root, entries, dry_run=True)
            except (_lib_confedit.EditsRefused, _lib_confedit.EditCallerError) as exc:
                refused += _refusal_lines(root, exc)
    if refused:
        raise ConfdWriteRefused(
            f"da-guard write refused {len(refused)} generated conf.d file(s): "
            + "; ".join(refused) + " — nothing was written")


def _write_confd(confd: "list[tuple[str, str]]") -> None:
    """The batches `_judge_confd` judged, written (`da-guard write`), one
    root after another: each all or nothing. Raises ConfdWriteRefused when
    the first batch is refused (nothing written), ConfdWriteStopped when a
    write stops after a file was written."""
    import _lib_confedit  # noqa: PLC0415
    written: "list[str]" = []
    with _confd_batches(confd) as batches:
        for root, entries in batches:
            try:
                results = _lib_confedit.write_all(root, entries)
            except _lib_confedit.PartialWrite as exc:
                done = written + [os.path.join(root, p) for p in exc.written]
                maybe = [os.path.join(root, p) for p in exc.maybe_written]
                raise ConfdWriteStopped(
                    f"the write of the generated conf.d files stopped under {root}: "
                    f"{exc.cause}; written, not rolled back: {', '.join(done) or '(none)'}"
                    + (f"; may have been written: {', '.join(maybe)}" if maybe else "")
                ) from None
            except (_lib_confedit.EditsRefused, _lib_confedit.EditCallerError) as exc:
                why = "; ".join(_refusal_lines(root, exc))
                if not written:
                    raise ConfdWriteRefused(
                        f"da-guard write refused: {why} — nothing was written") from None
                raise ConfdWriteStopped(
                    f"da-guard write refused: {why}; written before it, not rolled "
                    f"back: {', '.join(written)}") from None
            written += [os.path.join(root, r["path"]) for r in results
                        if r["status"] == _lib_confedit.WRITTEN]


def regen_surfaces(doc: Optional[dict] = None) -> list[str]:
    """Splice every generated surface in place (`plan_surfaces`, then
    `write_planned`): all or none. Returns the touched paths."""
    if doc is None:
        doc = load_registry()
    return write_planned(plan_surfaces(doc))


def plan_surfaces(doc: dict) -> "list[tuple[str, str, bool]]":
    """[(path, new text, is conf.d), …] for every generated surface the
    registry *doc* changes, written nowhere. Every surface is spliced in
    memory. The conf.d surfaces (``confd`` in their spec — the dev template,
    the try-local seed) are judged by ``da-guard write --dry-run`` (ADR-036
    §2: only Go writes conf.d): one batch per conf.d root, each spliced file
    a ``replace`` of its whole text (`_confd_batches`), the very batches
    `write_planned` sends; any other YAML file is checked to still be read by
    its consumer (#3206, ADR-036 §12): a rule pack by Prometheus' rule loader
    (`da-read prometheus-rules`, all of them in one run), a Helm values file
    the way Helm reads it (da-crdecode). Raises ConfdWriteRefused, ValueError
    (a spliced file its consumer refuses), `_lib_parse_tree.ParseTreeError`
    when a conf.d file changes and there is no usable da-guard (a run that
    changes no conf.d file needs none), and `_lib_consumer_read.ConsumerReadError`
    when da-read / da-crdecode cannot be had."""
    repo_root = os.path.realpath(
        _repo_path_or_raise(_REPO_ROOT, "the generated surfaces"))
    # path -> [original text, spliced text, [its conf.d specs]]
    planned: dict[str, list] = {}
    rule_files: set = set()
    for spec in surface_specs(doc):
        # Containment: surface paths derive from committed registry fields
        # (e.g. rule_pack_file) that --regen consumes WITHOUT schema
        # validation — a `..`-carrying value must fail loud, not splice a
        # file outside the repo (CodeRabbit #1222 nitpick).
        real = os.path.realpath(spec["path"])
        if not real.startswith(repo_root + os.sep):
            raise ValueError(
                f"generated-surface path escapes repo root: {spec['path']!r}"
            )
        if spec["path"] not in planned:
            with open(spec["path"], encoding="utf-8") as fh:
                old = fh.read()
            planned[spec["path"]] = [old, old, []]
        entry = planned[spec["path"]]
        if spec.get("confd"):
            entry[2].append(spec)
        if spec.get("rule_file"):
            rule_files.add(spec["path"])
        entry[1] = splice_surface(entry[1], spec)
    changed = [(p, new, bool(specs)) for p, (old, new, specs) in planned.items()
               if new != old]
    # A spliced block that breaks the host document must fail loud BEFORE the
    # write: the freshness gate string-compares and would not catch it (burned
    # once — a two-element heading emitted a bare comment continuation line
    # into 13 packs). These files (helm values, rule packs) are not conf.d;
    # each is read back the way its consumer reads it.
    packs = [(path, new) for path, new, confd in changed
             if not confd and path in rule_files]
    for rf in _lib_consumer_read.prometheus_rule_text_results(packs):
        if rf.problem is not None:
            raise ValueError(f"spliced rule pack {rf.path}: Prometheus' rule loader "
                             f"refuses it: {rf.problem}")
    for path, new, confd in changed:
        if not confd and path not in rule_files and path.endswith((".yaml", ".yml")):
            try:
                _lib_consumer_read.any_yaml_text_documents(new)  # Helm values, no manifest (#3426)
            except _lib_consumer_read.InputRefused as exc:
                raise ValueError(f"spliced {path}: Helm's reading refuses it: "
                                 f"{_lib_consumer_read.error_text(exc)}") from None
    confd_files = [(p, new) for p, new, confd in changed if confd]
    if confd_files:
        _judge_confd(confd_files)
        _check_carriers([(p, new, planned[p][2]) for p, new in confd_files])
    return changed


def write_planned(planned: "list[tuple[str, str, bool]]") -> list[str]:
    """Write each ``(path, text, is conf.d)`` of `plan_surfaces`: the conf.d
    files first, through ``da-guard write`` (the batches the plan judged;
    ConfdWriteRefused / ConfdWriteStopped), then every other surface.
    Returns the paths."""
    sys.path.insert(0, _THIS_DIR)
    sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
    from _lib_python import write_text_secure  # noqa: E402

    confd_files = [(p, new) for p, new, confd in planned if confd]
    written: "list[str]" = []
    if confd_files:
        _write_confd(confd_files)
        written = [p for p, _new in confd_files]
    for path, new, confd in planned:
        if not confd:
            _write_or_stop(path, new, written, write_text_secure)
            written.append(path)
    return [p for p, _new, _confd in planned]


def _write_or_stop(path: str, text: str, written: "list[str]", write: Callable) -> None:
    """*write* (`write_text_secure`) *text* to *path*; an OSError is
    SurfaceWriteStopped naming *path* and every file of *written* (already
    written, not rolled back)."""
    try:
        write(path, text)
    except OSError as exc:
        raise SurfaceWriteStopped(
            f"writing {path} failed: {exc}; written before it, not rolled back: "
            f"{', '.join(written) or '(none)'}") from None


# ---------------------------------------------------------------------------
# Header-prose key membership (F6) — hand-written pack-header prose may only
# name conf.d threshold keys that actually exist somewhere in the contract
# ---------------------------------------------------------------------------
# The disease this kills: a pack header hand-claiming keys that exist nowhere
# (the pre-rewire elasticsearch header listed four demand-side names absent
# from the platform-defaults contract). Scanned region: the leading comment
# header of each rule-pack file, MINUS the generated block. Dimensional
# tokens (``key{selector=...}``) are exempt — the dimensional-threshold
# contract is scaffold's dimensional_example domain, not registry identity.

# YAML-ish structure words that legitimately appear in `key:` position inside
# header prose but are not conf.d threshold keys.
STRUCTURAL_PROSE_TOKENS: frozenset[str] = frozenset({
    "state_filters",
    "default_state",
    "group_by",
    "group_wait",
    "group_interval",
    "repeat_interval",
    "rule_pack_file",
    "default_on",
    "metric_class",
    "portability_reason",
    "portability_reason_zh",
    "default_exception",
    "critical_of",
    "optional_overrides",
    "runbook_url",
    "metric_group",
    "alert_threshold",
    "user_threshold",
    # Alertmanager config vocabulary (operational pack header prose)
    "inhibit_rules",
})

# candidate: snake_case token (≥1 underscore) in key position, not glued to a
# preceding identifier char or a recording-rule ':' segment.
_PROSE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_:])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)")


def iter_header_prose_lines(text: str):
    """(lineno, line) for the hand-written leading comment header.

    Stops at the first non-comment, non-blank line; skips the generated block
    (its key lines are registry-owned, not prose claims).
    """
    in_generated = False
    for i, line in enumerate(text.split("\n"), start=1):
        stripped = line.strip()
        if not stripped.startswith("#"):
            if stripped == "":
                continue
            return
        if f"# >>> {_MARKER_STEM}" in line:
            in_generated = True
            continue
        if f"# <<< {_MARKER_STEM}" in line:
            in_generated = False
            continue
        if not in_generated:
            yield i, line


def prose_key_tokens(text: str):
    """(lineno, token) for every conf.d-key-shaped claim in header prose."""
    for lineno, line in iter_header_prose_lines(text):
        for m in _PROSE_TOKEN_RE.finditer(line):
            rest = line[m.end():]
            if rest.startswith("{"):
                continue  # dimensional token — out of registry identity scope
            if re.match(r"[\"']?\s*:", rest) is None:
                continue  # not in key position
            yield lineno, m.group(1)


def membership_universe(
    doc: dict, extra_allowed: Optional[set[str]] = None
) -> set[str]:
    """Every token a pack header may legitimately claim as a conf.d key.

    Registry keys + their derived ``_critical`` opt-ins (the exporter accepts
    ``<base>_critical`` for any shipped base), plus deprecated alias spellings
    (#1231 — still-valid conf.d input during their transition window) and
    their ``_critical`` derivatives, plus caller-supplied extras (the #1196
    KNOWN_UNWIRED pending line, scaffold state-filter names), plus structural
    prose tokens.
    """
    keys = set(registry_keys(doc))
    keys |= set(doc.get("deprecated_aliases", {}) or {})
    uni = keys | {k + CRITICAL_SUFFIX for k in keys}
    if extra_allowed:
        uni |= set(extra_allowed)
        uni |= {k + CRITICAL_SUFFIX for k in extra_allowed}
    return uni | STRUCTURAL_PROSE_TOKENS
