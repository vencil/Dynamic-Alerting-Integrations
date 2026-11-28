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

  1. rule-pack header threshold sections, two sections keyed by ``arming``
     (keys with a platform value, keys the tenant must value; #3924 — see
     ``render_pack_header_lines``);
  2. the ``dimensions:`` block of the chart values, the dev conf.d root and
     the try-local seed (#3475);
  3. the built-in layer digest in ``rule-pack-platform.yaml``.

Since #3924 no shipped artifact and no generator WRITES a registry key: every
one is built into the readers (``builtin_defaults_gen.go``), so the chart's
``thresholdConfig.defaults`` and ``optional_overrides`` ship empty and the
reachability gate's producer policy keeps them that way.

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

SCOPE (WS1a): threshold identity — one flat key set per pack
({pack, value, unit, desc, metric_class, arming, critical variant}; the
``defaults`` / ``optional_overrides`` tier split left in #4195: ``arming``
says whether a key carries a platform value). ``state_filters`` / ``dimensional_example`` stay scaffold-owned
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
# ⛔ Nothing here is a hand-kept list. "Portable, unshipped, unblocked —
# what A2 validates next" (`validation_candidates`) is DERIVED.
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

# The RULE_PACKS field carrying a pack's thresholds, in registry key order
# (#4195: one mapping; the `defaults` / `optional_overrides` split is gone).
THRESHOLDS = "thresholds"

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
    "pack", "value", "unit", "desc", "metric_class", "arming",
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
    Per key : {pack, value, unit, desc[, metric_class][, arming,
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

    Fails loudly on identity collisions (same key in two packs) — a collision would silently shadow a contract entry — and
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
        if pack.get("queries"):
            # query-exporter config that serves the pack's raw metrics (#3833).
            packs[pack_name]["queries"] = pack["queries"]
        if pack.get("presence"):
            # The selectors whose presence means the pack's exporter answers
            # (#4097): a pack that has them is an `_expected_exporters` kind.
            packs[pack_name]["presence"] = list(pack["presence"])
        if pack.get("metric_prefixes"):
            # The exporter's metric namespace (#4270).
            packs[pack_name]["metric_prefixes"] = list(pack["metric_prefixes"])
        if "undeclared_notice" in pack:
            packs[pack_name]["undeclared_notice"] = pack["undeclared_notice"]
        if pack.get("exporters"):
            # The pinned real exporter(s) the pack's raw selectors are checked
            # against (#3833; `exporters_problems`, check_exporter_capture.py).
            packs[pack_name]["exporters"] = list(pack["exporters"])
        if pack.get("unchecked_alternatives"):
            packs[pack_name]["unchecked_alternatives"] = list(pack["unchecked_alternatives"])
        for key, info in (pack.get(THRESHOLDS) or {}).items():
            if key in keys:
                raise ValueError(
                    f"threshold key {key!r} appears in packs "
                    f"{keys[key]['pack']!r} AND {pack_name!r} — ambiguous "
                    "contract identity"
                )
            entry: dict[str, Any] = {
                "pack": pack_name,
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
                if base in (pack.get(THRESHOLDS) or {}):
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
    - ``default`` has no blocker and is portable — unless ``default_exception: {tracking}`` records why a
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
#: A whole number as a source writes it: its sign (unless the `-` is a minus
#: between two operands, `a-30` / `)-30`) and its exponent stay part of it,
#: so `> -30` is -30 and `3e1` is 30 — neither states a plain 30 by accident.
_NUMBER_TOKEN_RE = re.compile(r"(?<![\w)\].])[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _quote_states(value: float, quote: str, unit: Any = None) -> bool:
    """Whether *quote* carries *value*: the number itself, or — for a
    percentage key only, where a source may write the ratio (`> 0.8`,
    `> .95`) — a hundredth of it. A seconds key is never read as a ratio:
    `lag > 0.3` does not state 30."""
    numbers = {float(t) for t in _NUMBER_TOKEN_RE.findall(quote)}
    wanted = [value]
    if isinstance(unit, str) and unit.strip().startswith("%"):
        wanted.append(value / 100)
    return any(abs(n - c) < 1e-9 for n in numbers for c in wanted)


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
      (as written, or — for a `%` key only — as a ratio: `> 0.8` for 80);
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
            if isinstance(s["quote"], str) and not _quote_states(
                    float(s["value"]), s["quote"], entry.get("unit")):
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
    """Portable, not shipped, not blocked: what A2 validates next (#4195:
    no tier gates a promotion any more)."""
    return (entry.get("arming") == ARMING_OFF
            and entry.get("portability") == PORTABLE
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
        elif port == PORTABLE and b is None and is_validation_candidate(entry):
            notes.append(f"未啟用（{CANDIDATE_LABEL_ZH}）：{CANDIDATE_NOTE_ZH}")
    elif state == ARMING_DEFAULT and port_label and isinstance(port_reason, str):
        exc = entry.get("default_exception")
        t = exc.get("tracking") if isinstance(exc, dict) else None
        notes.append(f"出貨例外（{port_label}，#{t}）：{port_reason}")
    return notes


# ---------------------------------------------------------------------------
# The registry as source (D-A PR-3): project -> RULE_PACKS shape, fixed point,
# and the generated Python module scaffold_tenant reads
# ---------------------------------------------------------------------------

#: Per-pack fields of scaffold's RULE_PACKS that come from the registry.
_PACK_FIELDS = ("display", "exporter", "default_on", "rule_pack_file", "queries",
                "presence", "metric_prefixes", "undeclared_notice",
                "exporters", "unchecked_alternatives")
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
    "display/exporter/default_on/rule_pack_file(/queries/presence/metric_prefixes/\n"
    "undeclared_notice/exporters/unchecked_alternatives)\n"
    "and its `thresholds` (registry key order), BUILTIN_DEFAULT_KEYS: the keys the\n"
    "readers build in with a platform value (`arming: default`, the same derivation\n"
    "as builtin_defaults_gen.go, #3832), and EXPECTED_EXPORTER_KINDS: the packs with\n"
    "`presence:`, the kinds a tenant may name in `_expected_exporters` (#4097).\n"
    "Edit the registry, then run\n"
    "  python3 scripts/tools/lint/check_threshold_registry.py --regen\n"
    "`--ci` (pre-commit, CI) fails while this file is stale.\n"
    '"""\n'
)


def project_rule_packs(doc: dict) -> dict:
    """The threshold part of RULE_PACKS, as the registry *doc* states it.

    Per pack: the `_PACK_FIELDS`, then `thresholds` in key order; per key the
    `_KEY_FIELDS` it carries. Derived fields (`critical_of`) and the
    registry-only arming fields are not part of RULE_PACKS."""
    packs: dict[str, dict] = {}
    for name, meta in (doc.get("packs") or {}).items():
        packs[name] = {f: (list(meta[f]) if isinstance(meta[f], list) else meta[f])
                       for f in _PACK_FIELDS if f in meta}
    for key, entry in registry_keys(doc).items():
        info: dict[str, Any] = {}
        for f in _KEY_FIELDS:
            if f not in entry:
                continue
            v = entry[f]
            info[f] = list(v) if isinstance(v, list) else (
                dict(v) if isinstance(v, dict) else v)
        packs[entry["pack"]].setdefault(THRESHOLDS, {})[key] = info
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
    builtin = ", ".join(repr(k) for k in sorted(builtin_defaults(doc)))
    kinds = ", ".join(repr(k) for k in expected_exporter_kinds(doc))
    return (_RULE_PACKS_MODULE_HEADER + "\nTHRESHOLD_PACKS = " + body + "\n"
            + "\nBUILTIN_DEFAULT_KEYS = frozenset({" + builtin + "})\n"
            + "\nEXPECTED_EXPORTER_KINDS = (" + kinds + (",)\n" if kinds else ")\n"))


def packaged_registry_doc() -> dict:
    """The registry as the generated RULE_PACKS module carries it.

    For a reader that runs where no repo tree is (the da-tools image ships
    the module, not the YAML): every key's pack, value, unit, desc,
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


#: The Go table of the default-armed thresholds and their platform values
#: (#3832), generated whole: pkg/config/builtin_defaults.go hands it to the
#: readers of conf.d, which are compiled from the one source.
BUILTIN_DEFAULTS_GO_PATH = _in_repo(
    "components", "threshold-exporter", "app", "pkg", "config",
    "builtin_defaults_gen.go")
BUILTIN_DEFAULTS_GO_ID = "builtin-defaults-go"


def builtin_default_number(value: Any) -> Optional[str]:
    """*value* as the text the digest is computed over, or None when it is
    not a finite number: plain decimal digits, never an exponent, without a
    fraction when it is integral (``80``) and otherwise the shortest form
    that reads back as the same number (``0.5``) — what Go's
    ``strconv.FormatFloat(v, 'f', -1, 64)`` writes, which the Go side of the
    digest uses. An integer too large for a float is no finite number here."""
    import math
    from decimal import Decimal
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        f = float(value)
    except OverflowError:
        return None
    if not math.isfinite(f):
        return None
    text = format(Decimal(repr(f)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _builtin_go_literal(text: str) -> str:
    """*text* (`builtin_default_number`) as a Go float64 literal. A long run
    of digits gets a fraction: an untyped integer constant past 512 bits does
    not compile, a float constant of the same digits does."""
    return text + ".0" if "." not in text and len(text.lstrip("-")) > 15 else text


def builtin_defaults(doc: dict) -> dict[str, str]:
    """Every default-armed key of *doc* (`is_chart_armed`) -> its value's text
    (`builtin_default_number`). A key whose value is no finite number is left
    out here and reported by the schema check beside this one."""
    out: dict[str, str] = {}
    for key, entry in (doc.get("keys") or {}).items():
        if not isinstance(entry, dict) or not is_chart_armed(entry):
            continue
        text = builtin_default_number(entry.get("value"))
        if text is not None:
            out[str(key)] = text
    return out


def builtin_defaults_digest(table: dict[str, str]) -> str:
    """The digest that names one built-in table: the first 12 hex digits of
    the SHA-256 of its ``key=value`` lines, sorted by key, each ended by a
    newline. Two components are aligned when their digests are equal; a
    version number says nothing here, as each component has a release line
    of its own. The Go test beside the table recomputes it the same way."""
    import hashlib
    lines = "".join(f"{k}={table[k]}\n" for k in sorted(table))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()[:12]


def builtin_layer_digest(doc: dict) -> str:
    """The digest of *doc*'s whole built-in layer (#4163): the table of
    values (`builtin_defaults`) and the off table — every key the platform
    ships no value for (`unset_notice_keys`), one ``key=off`` line each. The
    readers lay both, so two components hold the same layer when this is
    equal; it is the digest the generated Go table and the platform pack
    carry. The two tables never share a key (an arming is one or the other)."""
    table = dict(builtin_defaults(doc))
    for key in unset_notice_keys(doc):
        table[key] = "off"
    return builtin_defaults_digest(table)


def render_builtin_defaults_go(doc: dict) -> str:
    """The exact text of the generated Go table for *doc*'s default-armed keys.

    One `{key, value}` entry a line, as the retired-keys table: gofmt leaves
    such lines alone, where it re-aligns the values of a `key: value` literal
    by rules that depend on how long neighbouring keys are."""
    table = builtin_defaults(doc)
    entries = "".join(
        f"\t{{{_go_str(k)}, {_builtin_go_literal(table[k])}}},\n" for k in sorted(table))
    return (
        "// Code generated by scripts/tools/lint/check_threshold_registry.py --regen; DO NOT EDIT.\n"
        "// Source: rule-packs/threshold-registry.yaml, the keys with `arming: default` (#3832).\n"
        "\n"
        "package config\n"
        "\n"
        "// builtinDefaultEntries are the thresholds the platform ships armed, each\n"
        "// with the platform's value. Read through builtin_defaults.go.\n"
        "var builtinDefaultEntries = []builtinDefaultEntry{"
        + (f"\n{entries}" if entries else "") + "}\n"
        "\n"
        "// builtinDefaultsDigest names the built-in layer: the first 12 hex digits\n"
        "// of the SHA-256 of this table's `key=value` lines and a `key=off` line per\n"
        "// key of unsetThresholdKeys (#4163), sorted by key.\n"
        f"const builtinDefaultsDigest = {_go_str(builtin_layer_digest(doc))}\n")


def check_builtin_defaults_go(doc: dict, path: Optional[str] = None) -> Optional[str]:
    """None when the generated Go table is fresh, else what is wrong with it."""
    p = path or BUILTIN_DEFAULTS_GO_PATH
    if p is None or not os.path.isfile(p):
        return f"{p} (surface {BUILTIN_DEFAULTS_GO_ID!r}) does not exist"
    with open(p, encoding="utf-8") as fh:
        if fh.read() != render_builtin_defaults_go(doc):
            return (f"{p} (surface {BUILTIN_DEFAULTS_GO_ID!r}) is not what the "
                    "registry generates")
    return None


#: The Go table of the operator-supplied threshold keys and their packs
#: (#1200 D-A A3): pkg/config/unset_thresholds.go judges, per tenant, which of
#: them nothing writes, and the exporter serves that as user_threshold_unset.
UNSET_KEYS_GO_PATH = _in_repo(
    "components", "threshold-exporter", "app", "pkg", "config",
    "unset_threshold_keys_gen.go")
UNSET_KEYS_GO_ID = "unset-threshold-keys-go"


def unset_notice_keys(doc: dict) -> dict[str, str]:
    """Every key the platform does not ship a value for -> its pack: the
    operator-supplied keys (`arming: operator`). A `_critical`
    key is not one: the schema gives it no arming (it follows its base)."""
    out: dict[str, str] = {}
    for key, entry in (doc.get("keys") or {}).items():
        if isinstance(entry, dict) and entry.get("arming") == ARMING_OFF:
            out[str(key)] = str(entry.get("pack") or "")
    return out


def render_unset_keys_go(doc: dict) -> str:
    """The exact text of the generated Go table for *doc*'s operator keys,
    one `{key, pack}` entry a line, sorted by key."""
    table = unset_notice_keys(doc)
    entries = "".join(
        f"\t{{{_go_str(k)}, {_go_str(table[k])}}},\n" for k in sorted(table))
    return (
        "// Code generated by scripts/tools/lint/check_threshold_registry.py --regen; DO NOT EDIT.\n"
        "// Source: rule-packs/threshold-registry.yaml, the keys with `arming: operator` (#1200 D-A A3).\n"
        "\n"
        "package config\n"
        "\n"
        "// unsetThresholdKeys are the threshold keys the platform does not ship a\n"
        "// value for, each with the pack whose rules read it. Read through\n"
        "// unset_thresholds.go.\n"
        "var unsetThresholdKeys = []unsetThresholdKey{"
        + (f"\n{entries}" if entries else "") + "}\n")


def check_unset_keys_go(doc: dict, path: Optional[str] = None) -> Optional[str]:
    """None when the generated Go table is fresh, else what is wrong with it."""
    p = path or UNSET_KEYS_GO_PATH
    if p is None or not os.path.isfile(p):
        return f"{p} (surface {UNSET_KEYS_GO_ID!r}) does not exist"
    with open(p, encoding="utf-8") as fh:
        if fh.read() != render_unset_keys_go(doc):
            return (f"{p} (surface {UNSET_KEYS_GO_ID!r}) is not what the "
                    "registry generates")
    return None


# ---------------------------------------------------------------------------
# Exporter kinds (#4097): a pack with `presence:` is a kind a tenant may name
# in `_expected_exporters`
# ---------------------------------------------------------------------------

def pack_presence(doc: dict) -> dict[str, tuple[str, ...]]:
    """kind -> the selectors (no `job` matcher) whose presence under the
    tenant-exporters job means an exporter of that kind is answering: every
    pack of *doc* that carries `presence:`, in the registry's order."""
    out: dict[str, tuple[str, ...]] = {}
    for name, meta in (doc.get("packs") or {}).items():
        if isinstance(meta, dict) and meta.get("presence"):
            out[str(name)] = tuple(str(s) for s in meta["presence"])
    return out


def expected_exporter_kinds(doc: dict) -> list[str]:
    """The kinds `_expected_exporters` may name, sorted: the packs with a
    presence selector. The one list every other list of kinds is generated
    from or pinned to."""
    return sorted(pack_presence(doc))


def undeclared_notice_kinds(doc: dict) -> list[str]:
    """The kinds TenantExporterUndeclared names, in the registry's order:
    every kind but those whose pack says `undeclared_notice: false` (a metric
    many targets expose incidentally)."""
    packs = doc.get("packs") or {}
    return [k for k in pack_presence(doc)
            if packs[k].get("undeclared_notice") is not False]


def generated_packs() -> dict:
    """THRESHOLD_PACKS of the generated module (``_rule_packs_generated``):
    the registry's packs where no repo tree is (the da-tools image ships the
    module beside this file, not the YAML)."""
    if _THIS_DIR not in sys.path:
        sys.path.insert(0, _THIS_DIR)
    import _rule_packs_generated  # noqa: PLC0415  (a data module beside this file)
    return _rule_packs_generated.THRESHOLD_PACKS


def pack_metric_prefixes(packs: "dict | None" = None) -> "dict[str, tuple[str, ...]]":
    """⛔ THE one answer to "whose exporter emits metrics named like this"
    (#4270): pack -> its `metric_prefixes`, every pack that declares them, in
    the registry's order. *packs* defaults to ``generated_packs()``."""
    packs = generated_packs() if packs is None else packs
    return {str(name): tuple(meta["metric_prefixes"]) for name, meta in packs.items()
            if isinstance(meta, dict) and meta.get("metric_prefixes")}


def tenant_exporter_prefixes(packs: "dict | None" = None) -> "tuple[str, ...]":
    """The prefixes of every exporter KIND's metrics (a pack with `presence:`):
    what the scrape gate and the split validator take as a tenant exporter's
    metric. A pack without presence (kubernetes: cAdvisor and
    kube-state-metrics, scraped by the platform) is not one."""
    packs = generated_packs() if packs is None else packs
    return tuple(p for name, prefixes in pack_metric_prefixes(packs).items()
                 if packs[name].get("presence") for p in prefixes)


def pack_of_metric(metric: str, packs: "dict | None" = None, *,
                   ignore_case: bool = False, kinds_only: bool = False) -> "str | None":
    """The pack whose `metric_prefixes` *metric* is under, or None.
    *ignore_case* for the onboarding heuristics, which read a customer's
    metric names as they were written; *kinds_only* to answer with an
    exporter kind (a pack with `presence:`) only. Prefixes do not overlap
    (pinned by tests/rulepacks/test_exporter_metric_prefixes.py), so the
    answer is one."""
    packs = generated_packs() if packs is None else packs
    name = metric.lower() if ignore_case else metric
    for pack, prefixes in pack_metric_prefixes(packs).items():
        if kinds_only and not packs[pack].get("presence"):
            continue
        if name.startswith(tuple(p.lower() if ignore_case else p for p in prefixes)):
            return pack
    return None


def presence_problems(doc: dict) -> list[str]:
    """What breaks the `presence:` contract beyond the schema: a selector must
    not carry its own `job` matcher (the rules add the tenant-exporters job),
    and `undeclared_notice` belongs to a pack that has `presence:`."""
    problems = []
    for name, meta in (doc.get("packs") or {}).items():
        if not isinstance(meta, dict):
            continue
        for sel in meta.get("presence") or []:
            if re.search(r"[{,]\s*job\s*(=|!)", str(sel)):
                problems.append(
                    f"pack {name!r}: presence selector {sel!r} carries a `job` matcher — "
                    "the rules add job=\"tenant-exporters\" themselves")
        if "undeclared_notice" in meta and not meta.get("presence"):
            problems.append(
                f"pack {name!r}: undeclared_notice without presence — only a kind "
                "(a pack with `presence:`) can be noticed")
    return problems


def pack_exporters(doc: dict) -> "dict[str, list[dict]]":
    """pack -> its `exporters:` entries (#3833): the pinned real exporter(s)
    whose /metrics the pack's raw selectors are checked against, each with the
    server it watches, the flags it is started with and the server states
    captured. Only the packs that carry the field."""
    return {str(name): list(meta["exporters"])
            for name, meta in (doc.get("packs") or {}).items()
            if isinstance(meta, dict) and isinstance(meta.get("exporters"), list)
            and meta["exporters"]}


def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _well_formed_exporter(entry) -> bool:
    """*entry* has every field the derivations read, of the type they read it
    as — the schema's shape, asked here so that a malformed registry yields
    the schema's message instead of a crash that replaces every message of
    the run."""
    if not isinstance(entry, dict):
        return False
    server, identity = entry.get("server"), entry.get("identity", {})
    return (all(isinstance(entry.get(k), str) for k in ("name", "image", "tag", "digest"))
            and _is_str_list(entry.get("flags")) and _is_str_list(entry.get("states"))
            and isinstance(server, dict)
            and all(isinstance(server.get(k), str) for k in ("image", "tag", "digest"))
            and isinstance(identity, dict)
            and all(isinstance(v, dict) and isinstance(v.get("metric"), str)
                    and isinstance(v.get("value"), (int, float)) and not isinstance(v.get("value"), bool)
                    for v in identity.values()))


def well_formed_exporters(meta) -> "list[dict] | None":
    """A pack's `exporters:` entries when every one is well formed, else None
    (the schema reports what is wrong; nothing is derived from it)."""
    exporters = meta.get("exporters") if isinstance(meta, dict) else None
    if not isinstance(exporters, list) or not all(_well_formed_exporter(e) for e in exporters):
        return None
    return exporters


def exporter_text(exporters: "list[dict]") -> str:
    """⛔ THE text of a pack's `exporter:` when it has `exporters:` (#3833):
    the entries' names joined by " or ". `exporters_problems` holds the
    authored `exporter:` equal to it, so the human-readable line is not a
    second, hand-kept statement of which exporter the pack is for."""
    return " or ".join(str(e["name"]) for e in exporters)


def image_ref(entry: dict) -> str:
    """`<image>:<tag>@<digest>` of an `exporters:` entry or its `server:`."""
    return f"{entry['image']}:{entry['tag']}@{entry['digest']}"


def exporters_problems(doc: dict) -> list[str]:
    """What breaks the `exporters:` contract beyond the schema (#3833):
    `exporter:` is `exporter_text` of the entries; `unchecked_alternatives`
    belongs to a pack with `exporters:`, names each metric once, and each
    metric is under the pack's `metric_prefixes`."""
    problems = []
    for name, meta in (doc.get("packs") or {}).items():
        if not isinstance(meta, dict):
            continue
        exporters = meta.get("exporters")
        alternatives = meta.get("unchecked_alternatives") or []
        if alternatives and not exporters:
            problems.append(
                f"pack {name!r}: unchecked_alternatives without exporters — there is "
                "no capture for a name to be left out of")
        if not exporters:
            continue
        exporters = well_formed_exporters(meta)
        if exporters is None:
            continue   # the schema names the malformed entry
        want = exporter_text(exporters)
        for i, e in enumerate(exporters):
            ident = set(e.get("identity") or {})
            if ident - set(e["states"]):
                problems.append(
                    f"pack {name!r}: exporters[{i}].identity names {sorted(ident - set(e['states']))}, "
                    f"which are not among its states {e['states']}")
            if len(e["states"]) > 1 and set(e["states"]) - ident:
                problems.append(
                    f"pack {name!r}: exporters[{i}] captures {len(e['states'])} states and gives no "
                    f"identity for {sorted(set(e['states']) - ident)} — without one a scrape of one "
                    "state passes for another")
        if meta.get("exporter") != want:
            problems.append(
                f"pack {name!r}: exporter is {meta.get('exporter')!r}, its exporters: "
                f"entries name {want!r} — write that (the line is derived from them)")
        seen: set[str] = set()
        prefixes = tuple(meta.get("metric_prefixes") or ())
        for alt in alternatives:
            metric = alt.get("metric") if isinstance(alt, dict) else None
            if not isinstance(metric, str):
                continue
            if metric in seen:
                problems.append(f"pack {name!r}: unchecked_alternatives names {metric!r} twice")
            seen.add(metric)
            if not metric.startswith(prefixes or ("\0",)):
                problems.append(
                    f"pack {name!r}: unchecked alternative {metric!r} is under none of "
                    f"the pack's metric_prefixes {list(prefixes)}")
    return problems


EXPECTED_KINDS_GO_PATH = _in_repo(
    "components", "threshold-exporter", "app", "pkg", "config",
    "expected_exporter_kinds_gen.go")
EXPECTED_KINDS_GO_ID = "expected-exporter-kinds-go"


def render_expected_kinds_go(doc: dict) -> str:
    """The exact text of the generated Go table of *doc*'s exporter kinds,
    one a line, sorted."""
    entries = "".join(f"\t{_go_str(k)},\n" for k in expected_exporter_kinds(doc))
    return (
        "// Code generated by scripts/tools/lint/check_threshold_registry.py --regen; DO NOT EDIT.\n"
        "// Source: rule-packs/threshold-registry.yaml, the packs with `presence:` (#4097).\n"
        "\n"
        "package config\n"
        "\n"
        "// expectedExporterKinds are the kinds `_expected_exporters` may name: the\n"
        "// rule packs that have a presence metric, sorted. Read through\n"
        "// expected_exporters.go.\n"
        "var expectedExporterKinds = []string{"
        + (f"\n{entries}" if entries else "") + "}\n")


def check_expected_kinds_go(doc: dict, path: Optional[str] = None) -> Optional[str]:
    """None when the generated Go table is fresh, else what is wrong with it."""
    p = path or EXPECTED_KINDS_GO_PATH
    if p is None or not os.path.isfile(p):
        return f"{p} (surface {EXPECTED_KINDS_GO_ID!r}) does not exist"
    with open(p, encoding="utf-8") as fh:
        if fh.read() != render_expected_kinds_go(doc):
            return (f"{p} (surface {EXPECTED_KINDS_GO_ID!r}) is not what the "
                    "registry generates")
    return None


def platform_scraped_prefixes(packs: "dict | None" = None) -> "tuple[str, ...]":
    """The prefixes of every pack whose metrics the platform scrapes itself
    (a pack with `metric_prefixes` and no `presence:` — kubernetes: cAdvisor,
    the kubelet's volume stats, kube-state-metrics), in the registry's order.
    Their series name the tenant only by `namespace` (#4178): no scrape writes
    `tenant` on them, so whatever needs it derives it from `namespace`."""
    packs = generated_packs() if packs is None else packs
    return tuple(p for name, prefixes in pack_metric_prefixes(packs).items()
                 if not packs[name].get("presence") for p in prefixes)


NAMESPACE_KEYED_GO_PATH = _in_repo(
    "components", "threshold-exporter", "app", "pkg", "config",
    "namespace_keyed_prefixes_gen.go")
NAMESPACE_KEYED_GO_ID = "namespace-keyed-prefixes-go"


def render_namespace_keyed_go(doc: dict) -> str:
    """The exact text of the generated Go table of the metric prefixes whose
    series carry the tenant only as `namespace` (``platform_scraped_prefixes``)."""
    entries = "".join(f"\t{_go_str(p)},\n" for p in platform_scraped_prefixes(doc.get("packs") or {}))
    return (
        "// Code generated by scripts/tools/lint/check_threshold_registry.py --regen; DO NOT EDIT.\n"
        "// Source: rule-packs/threshold-registry.yaml, the `metric_prefixes` of the packs\n"
        "// without `presence:` (#4178).\n"
        "\n"
        "package config\n"
        "\n"
        "// namespaceKeyedPrefixes name the metrics the platform scrapes itself, whose\n"
        "// series carry the tenant only as `namespace`. Read through\n"
        "// custom_alert_compile.go, which derives `tenant` for them.\n"
        "var namespaceKeyedPrefixes = []string{"
        + (f"\n{entries}" if entries else "") + "}\n")


def check_namespace_keyed_go(doc: dict, path: Optional[str] = None) -> Optional[str]:
    """None when the generated Go table is fresh, else what is wrong with it."""
    p = path or NAMESPACE_KEYED_GO_PATH
    if p is None or not os.path.isfile(p):
        return f"{p} (surface {NAMESPACE_KEYED_GO_ID!r}) does not exist"
    with open(p, encoding="utf-8") as fh:
        if fh.read() != render_namespace_keyed_go(doc):
            return (f"{p} (surface {NAMESPACE_KEYED_GO_ID!r}) is not what the "
                    "registry generates")
    return None


PLATFORM_DIGEST_TEST_PATH = _in_repo(
    "tests", "rulepacks", "platform-builtin-defaults-digest_test.yaml")
PLATFORM_DIGEST_TEST_ID = "platform-builtin-digest-test"
_RULEPACK_DIGEST_RE = re.compile(r'rulepack_digest: "([0-9a-f]{12})"')


def _platform_digest_test_digests(text: str) -> set[str]:
    """The pack digests the promtool test of BuiltinDefaultsDigestMismatch
    expects: every ``rulepack_digest: "<digest>"`` it writes."""
    return set(_RULEPACK_DIGEST_RE.findall(text))


def render_platform_digest_test(doc: dict, text: str) -> str:
    """*text* (that promtool test) with the pack digest it expects replaced,
    everywhere in it, by the built-in table's digest of *doc* (#3831). The
    test feeds a component that is aligned with the pack the pack's own
    digest, so the digest appears in input series and annotations too; a
    made-up component digest differs from it and is left alone."""
    digest = builtin_layer_digest(doc)
    for old in _platform_digest_test_digests(text):
        if old != digest:
            text = text.replace(old, digest)
    return text


def check_platform_digest_test(doc: dict, path: Optional[str] = None) -> Optional[str]:
    """None when the promtool test of BuiltinDefaultsDigestMismatch expects
    the built-in table's digest of *doc*, else what is wrong with it."""
    p = path or PLATFORM_DIGEST_TEST_PATH
    if p is None or not os.path.isfile(p):
        return f"{p} (surface {PLATFORM_DIGEST_TEST_ID!r}) does not exist"
    with open(p, encoding="utf-8") as fh:
        text = fh.read()
    digest = builtin_layer_digest(doc)
    found = _platform_digest_test_digests(text)
    if found != {digest}:
        return (f"{p} (surface {PLATFORM_DIGEST_TEST_ID!r}) expects the rule "
                f"pack digest {', '.join(sorted(found)) or 'nowhere'}, the "
                f"registry's built-in table is {digest} — run "
                "check_threshold_registry.py --regen")
    return None


#: Every whole file ``--regen`` writes, in the order it writes them, after the
#: spliced surfaces (`surface_specs`) — the one list of them (#4346): `regen`
#: writes these and nothing else, and the `files:` of the
#: `threshold-registry-check` pre-commit hook must match each
#: (tests/lint/test_check_threshold_registry.py). Each row is (the name of the
#: module constant that holds the path, what the file is, render(doc, its
#: current text), check(doc, path)). The path is read by name when `regen`
#: runs, so a test that points the constant elsewhere moves the write too.
GENERATED_FILES: "tuple[tuple[str, str, Callable[[dict, Optional[str]], str], Callable[..., Optional[str]]], ...]" = (
    ("RULE_PACKS_MODULE_PATH", "the RULE_PACKS module",
     lambda doc, _text: render_rule_packs_module(doc), check_rule_packs_module),
    ("RETIRED_KEYS_GO_PATH", "the retired-keys Go table",
     lambda doc, _text: render_retired_keys_go(doc), check_retired_keys_go),
    ("BUILTIN_DEFAULTS_GO_PATH", "the built-in defaults Go table",
     lambda doc, _text: render_builtin_defaults_go(doc), check_builtin_defaults_go),
    ("UNSET_KEYS_GO_PATH", "the unset-threshold keys Go table",
     lambda doc, _text: render_unset_keys_go(doc), check_unset_keys_go),
    ("EXPECTED_KINDS_GO_PATH", "the exporter-kinds Go table",
     lambda doc, _text: render_expected_kinds_go(doc), check_expected_kinds_go),
    ("NAMESPACE_KEYED_GO_PATH", "the namespace-keyed prefixes Go table",
     lambda doc, _text: render_namespace_keyed_go(doc), check_namespace_keyed_go),
    ("PLATFORM_DIGEST_TEST_PATH", "the promtool test of BuiltinDefaultsDigestMismatch",
     lambda doc, text: render_platform_digest_test(doc, _existing(text, PLATFORM_DIGEST_TEST_PATH)),
     check_platform_digest_test),
)


def _existing(text: Optional[str], path: str) -> str:
    """*text*, which a file rewritten in place (not generated whole) must have."""
    if text is None:
        raise FileNotFoundError(f"{path} does not exist: --regen rewrites it in place")
    return text


def generated_file_paths() -> list[str]:
    """The paths of `GENERATED_FILES`, as the module constants hold them now."""
    return [globals()[attr] for attr, _what, _render, _check in GENERATED_FILES]


def regen(path: Optional[str] = None) -> tuple[dict, list[str]]:
    """``--regen``: every generated surface from the committed registry.

    The registry itself is authored (D-A PR-3) and never written. It is
    refused unless it is a fixed point (`committed_registry_doc`); then the
    surfaces are planned and every conf.d surface is judged by a ``da-guard
    write`` dry run of the batches that will write it (`plan_surfaces`) —
    all before anything is written. Only then are the changed surfaces
    written (the conf.d ones first, through ``da-guard write``) and the
    generated RULE_PACKS module, the retired-keys Go table (#3829) and the
    built-in defaults Go table (#3832), the unset-threshold keys Go table
    (#1200 D-A A3), the exporter-kinds Go table (#4097) and the pack digest the promtool test of
    BuiltinDefaultsDigestMismatch expects (#3831) last; a refusal (ConfdWriteRefused,
    ValueError, `_lib_parse_tree.ParseTreeError`) leaves every file as it
    was; a write that stops part-way is SurfaceWriteStopped
    (ConfdWriteStopped for a conf.d batch), naming the files already
    written, and nothing after it is written.
    Returns ({path, packs, keys}, the touched surface paths)."""
    sys.path.insert(0, _THIS_DIR)
    sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
    from _lib_io import write_repo_artifact  # noqa: E402  (#3107: tracked files)

    src = path or _repo_path_or_raise(REGISTRY_PATH, "the threshold registry")
    doc = committed_registry_doc(src)
    planned = plan_surfaces(doc)
    # Every text is rendered before anything is written: a render that raises
    # leaves every file as it was.
    rendered = []
    for attr, what, render, check in GENERATED_FILES:
        out = _repo_path_or_raise(globals()[attr], what)
        current = None
        if os.path.isfile(out):
            with open(out, encoding="utf-8") as fh:
                current = fh.read()
        rendered.append((out, render(doc, current), check))
    touched = write_planned(planned)
    for out, text, check in rendered:
        if check(doc, out) is not None:
            _write_or_stop(out, text, touched, write_repo_artifact)
            touched.append(out)
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
# threshold, under_fire means a real fault stayed under it. #1176 measured both
# (retired-key-mention: db2_deadlock_rate —
# its one under_fire key, db2_deadlock_rate, was re-derived and retired in #1320).
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
    """The measured counter-example for `key`, or None."""
    if rule_packs is None:
        rule_packs = _load_scaffold().RULE_PACKS
    for pack in (rule_packs or {}).values():
        info = (pack.get(THRESHOLDS) or {}).get(key)
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


def _counterexample_lines(entry: dict, cont: str = "#       ") -> list[str]:
    """The ⚠ line under a key whose value has a measured counter-example.

    Rendered FROM the registry field, never authored per pack — the same
    discipline as the rest of the header: a header must not be able to
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
# Flat keys — the shape the exporter serves on its own row (#1310, #1311)
# ---------------------------------------------------------------------------


def is_flat_key(key: str) -> bool:
    """Whether *key* is a flat threshold key: neither a ``<base>_critical``
    nor a dimensional ``key{...}`` token.

    Mirrors the exporter's own split in
    ``components/threshold-exporter/app/pkg/config/resolve.go``: a
    ``_critical`` key is ``resolveCriticalRows``' business (it is read off its
    base), and a dimensional token is ``resolveDimensionalRows``'. Read by the
    portal's ``declaredKeys`` (generate_platform_data), the tenant-fixture
    tools and ``inject_default_key``.

    NOTE the registry models no dimensional keys today (they are scaffold's
    ``dimensional_example`` domain, see this module's SCOPE note), so the
    ``{`` arm is defence, not a live branch.
    """
    return not key.endswith(CRITICAL_SUFFIX) and "{" not in key


def shipped_dimensions_for_packs(
    pack_names, rule_packs: Optional[dict] = None
) -> "dict[str, list[str]]":
    """The ``dimensions:`` declaration for a SELECTION of packs (#3475).

    The onboarding generators write the customer-side root ``_defaults.yaml``, and on the
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
        for key, info in ((meta or {}).get(THRESHOLDS) or {}).items():
            if (info or {}).get("dimensions"):
                # sorted, as build_registry_doc stores it: the generated
                # file must match the registry surfaces label for label.
                out[key] = sorted(info["dimensions"])
    return out


# The `<base>_critical` keys' one sentence (#3924). Since #4163 every base is
# valued — by the platform's number or by the built-in off — so the critical
# row is served for any of them the moment a tenant, subdirectory or profile
# writes it; nothing depends on the topology any more (the header used to
# split them five ways by where the base was valued: chart, dev template,
# neither…).
_CRITICAL_SECTION_LINES = (
    "#",
    "# <base>_critical（critical 層；沒有平台值）：租戶、子目錄或 profile 寫了就送一條",
    "# severity=critical 的列，根目錄寫的是每個租戶的 critical 預設值（#4173）；",
    "# base 整族關掉（`<base>: \"disable\"`）時不送。",
    "# ⚠️ 下列數字是「參考起點」不是背書——正式採用前必須以該租戶實際 baseline 校準。",
)


def render_exporter_capture_lines(doc: dict, pack_name: str) -> list[str]:
    """The pack-header lines that tell a reader which real exporter the pack's
    raw metrics are checked against (#3833): per `exporters:` entry its name,
    version, pinned image, the non-default flags it is started with, the
    server and the states captured; then the names the pack also reads that
    no capture serves. Empty for a pack without `exporters:`."""
    meta = (doc.get("packs") or {}).get(pack_name) or {}
    # An entry the schema refuses is not rendered: the schema's message is the
    # finding, and a KeyError here would replace every message of the run.
    exporters = well_formed_exporters(meta)
    if not exporters:
        return []
    lines = [
        "# 本 pack 讀的每個原始指標選擇器，CI 都拿下列真實 exporter 的 /metrics 核對（#3833；",
        f"# scripts/ops/exporter_capture_runtime.sh {pack_name}）。其他版本或旗標組合沒有核對過。",
    ]
    for e in exporters:
        flags = " ".join(e["flags"]) if e["flags"] else "（無；全用預設值）"
        server = e["server"]
        lines += [
            f"#   {e['name']} {e['tag']}",
            f"#     映像：{e['image']}@{e['digest']}",
            f"#     啟動旗標（非預設，少了就有規則讀不到資料）：{flags}",
            f"#     核對對象：{server['image']} {server['tag']}；狀態：{'、'.join(e['states'])}",
        ]
    for alt in meta.get("unchecked_alternatives") or []:
        if not (isinstance(alt, dict) and isinstance(alt.get("metric"), str) and "reason" in alt):
            continue   # the schema names it
        lines.append(f"#   另外也讀、但沒有核對的名稱：{alt['metric']}")
        lines += [f"#       {seg}" for seg in _wrap_comment(str(alt["reason"]), _COMMENT_WIDTH - 8)]
    lines.append("#")
    return lines


def render_pack_header_lines(doc: dict, pack_name: str) -> list[str]:
    """The generated threshold section of one rule-pack header (body only).

    Two sections, keyed off ``arming`` and ``critical_of`` (#3924): the pack's
    thresholds — every one built into the readers (#3832, #4163), tagged
    ``[chart-default]`` when the platform ships its value, otherwise with the
    reason it has none — then its ``<base>_critical`` keys under one sentence
    that holds for all of them. A pack with no critical key gets no second
    section.
    """
    by_pack = keys_by_pack(doc).get(pack_name, {})
    base_keys = {k: e for k, e in by_pack.items() if not e.get("critical_of")}
    critical = {k: e for k, e in by_pack.items() if e.get("critical_of")}
    lines = render_exporter_capture_lines(doc, pack_name) + [
        "# 對應的 threshold-exporter 閾值（全部內建在讀取端，conf.d 不必列；"
        "[chart-default]=平台出貨即啟用，省略就用平台值；未標者沒有平台值，"
        "「未啟用」一行寫原因，租戶、子目錄或 profile 給值才發射）:",
    ]
    for key, entry in base_keys.items():
        lines += _entry_lines(key, entry)
    if critical:
        lines += list(_CRITICAL_SECTION_LINES)
        for key, entry in critical.items():
            lines += _entry_lines(key, entry)
    return lines


#: The platform pack's recording rule that carries the digest of the built-in
#: table the packs were generated from (#3831, #3832 decision 5); the alert
#: BuiltinDefaultsDigestMismatch beside it compares it with the digest the
#: threshold-exporter and tenant-api serve.
PLATFORM_DIGEST_RECORD = "rule_pack_builtin_defaults_info"
PLATFORM_DIGEST_SURFACE_ID = "platform-builtin-digest"


def render_platform_digest_lines(doc: dict, indent: int) -> list[str]:
    """The platform pack's digest recording rule (#3831): one series,
    ``rule_pack_builtin_defaults_info{rulepack_digest="<digest>"} 1``, whose
    digest is the built-in table's of *doc* (``builtin_defaults_digest``) —
    the same digest the components are compiled with when they are built
    from the same registry."""
    pad = " " * indent
    digest = builtin_layer_digest(doc)
    return [
        f"{pad}- record: {PLATFORM_DIGEST_RECORD}",
        f"{pad}  expr: vector(1)",
        f"{pad}  labels:",
        f'{pad}    rulepack_digest: "{digest}"',
    ]


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

    Order: the three ``dimensions`` blocks (helm values, dev template,
    try-local seed), the platform pack's built-in digest, then one per
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
    # #3924: no surface carries a threshold key's value or name. Every key
    # of the registry is in the readers' built-in layer (#3832, #4163), so the
    # chart, the dev template and the try-local seed list none (they carried
    # the chart's `defaults` mapping, the unarmed keys' roster, the example
    # packs' values and the `optional_overrides` list).
    specs = [
        # #3475: the labels a dimensional key may carry, per base, on the
        # three runtime surfaces.
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
    # #3831: the platform pack carries the built-in table's digest as a
    # recording rule (a Prometheus rule file, like the pack headers below).
    rule_packs = _lib_consumer_read.RULE_PACKS
    specs.append({
        "id": PLATFORM_DIGEST_SURFACE_ID,
        "path": os.path.join(root, rule_packs.directory, rule_packs.file_name("platform")),
        "indent": " " * 6,
        "body": render_platform_digest_lines(doc, 6),
        "rule_file": True,
    })
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
    by_root: "dict[str, list[dict]]" = {}
    with tempfile.TemporaryDirectory(prefix="da-regen-surface-") as stage:
        for n, (path, text) in enumerate(confd):
            src = os.path.join(stage, str(n), os.path.basename(path))
            os.mkdir(os.path.dirname(src), 0o700)
            # A process-private intermediate (owner 2026-11-15: stays a bare
            # write, under the write gate's temp-directory rule): created
            # here, 0600 and LF, as the retired `write_text_secure` wrote it.
            fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(fd, 0o600)  # the umask does not decide it (da-guard must read it)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
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
    from _lib_io import write_repo_artifact  # noqa: E402  (#3107: tracked files)

    confd_files = [(p, new) for p, new, confd in planned if confd]
    written: "list[str]" = []
    if confd_files:
        _write_confd(confd_files)
        written = [p for p, _new in confd_files]
    for path, new, confd in planned:
        if not confd:
            _write_or_stop(path, new, written, write_repo_artifact)
            written.append(path)
    return [p for p, _new, _confd in planned]


def _write_or_stop(path: str, text: str, written: "list[str]", write: Callable) -> None:
    """*write* (`write_repo_artifact`) *text* to *path*; an OSError is
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
    # registry pack field (#4097): the liveness pack header names it
    "undeclared_notice",
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
