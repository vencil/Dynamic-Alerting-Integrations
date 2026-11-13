#!/usr/bin/env python3
"""Critical-tier twin gate — every declared `<base>_critical` tier has exactly one critical alert, and it is the warning alert's twin (#3658).

WHY: a tenant declares `<base>_critical` (scaffold_tenant.RULE_PACKS
`optional_overrides`, extracted into rule-packs/threshold-registry.yaml as
`critical_of`) and expects the platform to page at that value. Nothing tied
that declaration to a rule: a declared key with no critical alert is a
setting that does nothing; a critical alert whose base is undeclared is a
tier no tenant is told it can set (scaffold never offers it);
and a critical alert that drifted from its warning alert (another `for`,
another label set, another expression shape) pages differently from what
the warning tier promised. This gate makes the declaration and the rules
one contract.

DEFINITIONS (all judged on the tokens of each rule's `expr`):
  - a THRESHOLD TOKEN is `tenant:alert_threshold:<name>` or
    `tenant_version:alert_threshold:<name>`;
  - K's WARNING-TIER reader reads the token for K with no
    `severity="critical"` matcher on that selector;
  - K's CRITICAL-TIER reader reads the token for `K_critical`, or the token
    for K with a `severity="critical"` matcher;
  - an alert READS a tier when its expr does, or a recording rule of any
    tenant pack that it references does (transitively).

CHECKS (rule packs = the tenant packs, `_lib_consumer_read.tenant_rule_pack_files`):
  1. declaration -> rules: every K the registry declares a critical tier for
     (`critical_of`) has exactly one alert that reads K's critical tier; it is
     labelled `severity: critical`; and among the `severity: warning` alerts
     that read K's warning tier exactly one is its twin (check 3).
  2. rules -> declaration: every rule (alert or record) that reads K's
     critical tier, and every `tenant(_version)?:alert_threshold:X_critical`
     record, has its K / X declared.
  3. twin equivalence of W (warning) and C (critical):
       - expr: equal token sequences, except (A) W's threshold token for K
         where C has the `K_critical` token, or (B) a recording rule rw that
         W references where C references rc, and rc's expr equals rw's with
         `severity="warning"` -> `severity="critical"` on the selectors that
         read K (at least one);
       - (A) also requires record `<prefix>:alert_threshold:K_critical` to
         equal record `<prefix>:alert_threshold:K` with exactly one
         `severity="warning"` -> `severity="critical"`;
       - labels equal except `severity`; `metric_group` on both, equal to K;
       - `for` equal, unless W is in FOR_EXCEPTIONS (exit-locked);
       - the same annotation keys;
       - C's name is W's name + "Critical"; same file, same group.
  4. an alert that reads K's critical tier with no twin W is an error
     whether or not K is declared (an undeclared K is reported first).
  An alert reading several thresholds' warning tiers (MariaDBSystemBottleneck,
  JVMPerformanceDegraded) is no K's critical reader; as a warning-tier
  candidate it only has to not be the one twin.

FAIL-CLOSED: a threshold reference this gate cannot judge — a selector whose
matchers it cannot parse, a `severity` matcher with an operator other than
`=` or an escape in its value, a string or a name with `alert_threshold:` in another shape — is an
error, never a silent pass.

A threshold record that keeps `severity` as a dimension (no `severity`
matcher in its own expr) carries both tiers; reading it without a `severity`
matcher is unjudged. Records are looked up by name across the tenant packs
(Prometheus record names are global).

KNOWN LIMITATIONS: the (B) form is one level deep (rc's expr is compared to
rw's with the substitution; a record chain under rw/rc is compared as
tokens, so it must be shared). A `severity` swap written directly in an
alert's own expr (not through a record) is not one of the allowed
differences.

Exit codes (_lib_exitcodes): 0 clean / 1 violation (--ci) / 2 caller error.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_OPS = os.path.join(_THIS_DIR, "..", "ops")
sys.path.insert(0, _OPS)
import _registry_lib as registry_lib  # noqa: E402

sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

import _lib_consumer_read  # noqa: E402  (#3206: rule files as Prometheus reads them)

CRITICAL_KEY_SUFFIX = registry_lib.CRITICAL_SUFFIX  # "_critical"
CRITICAL_ALERT_SUFFIX = "Critical"
THRESHOLD_PREFIXES = ("tenant", "tenant_version")
_THRESHOLD_INFIX = "alert_threshold:"
_THRESHOLD_NAME = re.compile(r"^(tenant|tenant_version):alert_threshold:([A-Za-z0-9_]+)$")

# W alert name -> (W's `for`, C's `for`), as Prometheus' rule loader reads
# them (`60s` reads as `1m`). The critical tier of these pairs deliberately
# fires sooner than the warning tier.
# ⛔ EXIT-LOCKED: an entry whose pair's `for` is now equal, whose values do
# not match the pair, or whose pair is gone is an error. Entries can only be
# removed — never add one; align the `for` instead.
FOR_EXCEPTIONS: dict[str, tuple[str, str]] = {
    "JVMHighGCPause": ("5m", "3m"),
    "JVMMemoryPressure": ("5m", "3m"),
    "JVMThreadPoolExhaustion": ("5m", "3m"),
    "NginxHighConnections": ("5m", "3m"),
    "NginxRequestRateSpike": ("5m", "3m"),
    "NginxConnectionBacklog": ("5m", "3m"),
}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

class Rule:
    """One rule of a tenant rule pack, as the rule loader read it. (A plain
    class, not a dataclass: tests load this file by path.)"""

    def __init__(self, file: str, group: str, kind: str, name: str, expr: str,
                 labels: "dict | None" = None, annotations: "dict | None" = None,
                 for_: "str | None" = None):
        self.file = file
        self.group = group
        self.kind = kind  # "alert" | "record"
        self.name = name
        self.expr = expr
        self.labels = dict(labels or {})
        self.annotations = dict(annotations or {})
        self.for_ = for_

    @property
    def where(self) -> str:
        return f"{self.file} group {self.group!r} {self.kind} {self.name!r}"


def rules_of_document(path: str, doc: dict) -> list[Rule]:
    """The rules of one rule-file document (``{"groups": [...]}``)."""
    out: list[Rule] = []
    for g in (doc or {}).get("groups") or []:
        for r in g.get("rules") or []:
            kind = "alert" if "alert" in r else "record"
            out.append(Rule(
                file=path, group=str(g.get("name")), kind=kind,
                name=str(r.get(kind)), expr=str(r.get("expr") or ""),
                labels=dict(r.get("labels") or {}),
                annotations=dict(r.get("annotations") or {}),
                for_=None if r.get("for") is None else str(r.get("for"))))
    return out


def collect_rules(root: Path = PROJECT_ROOT) -> list[Rule]:
    """Every rule of the tenant rule packs, each file read the way
    Prometheus' rule loader reads it (#3206: `da-read prometheus-rules`). A
    file the loader refuses raises ``RuleFilesRefused``, which names it."""
    packs_dir = Path(root, _lib_consumer_read.RULE_PACKS.directory)
    results = _lib_consumer_read.prometheus_rule_file_results(
        _lib_consumer_read.tenant_rule_pack_files(packs_dir))
    _lib_consumer_read.require_read(results)
    rules: list[Rule] = []
    for rf in results:
        try:
            rel = Path(rf.path).resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            rel = str(rf.path)
        rules.extend(rules_of_document(rel, rf.documents[0]))
    return rules


def declared_critical_bases(doc: "dict | None" = None) -> dict[str, str]:
    """{K: K_critical} for every key the registry derives `critical_of` for —
    the one declaration of which bases have a critical tier."""
    if doc is None:
        doc = registry_lib.load_registry()
    out: dict[str, str] = {}
    for key, entry in registry_lib.registry_keys(doc).items():
        base = (entry or {}).get("critical_of")
        if base:
            out[str(base)] = str(key)
    return out


# ---------------------------------------------------------------------------
# Tokens and selectors
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>\#[^\n]*)
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)
  | (?P<num>[0-9][A-Za-z0-9_.]*|\.[0-9][A-Za-z0-9_.]*)
  | (?P<ident>[A-Za-z_:][A-Za-z0-9_:]*)
  | (?P<op>=~|!~|!=|==|>=|<=|[^\s])
""", re.X)


class Unjudged(Exception):
    """A threshold reference this gate cannot judge (fail-closed)."""


def tokenize(expr: str) -> list[tuple[str, str]]:
    """The expr as (kind, text) tokens; whitespace and comments dropped. A
    string token's text is its content without the quotes."""
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if m is None:  # pragma: no cover — the op branch matches any char
            raise Unjudged(f"cannot tokenize expr at offset {pos}")
        pos = m.end()
        kind = m.lastgroup
        if kind in ("ws", "comment"):
            continue
        text = m.group()
        if kind == "str":
            text = text[1:-1]
        out.append((kind, text))
    return out


class Read:
    """One threshold selector in an expr: its prefix (tenant |
    tenant_version), base K, tier ("warning" | "critical"), the index of its
    name token and the indexes of its `severity` matcher values."""

    def __init__(self, prefix: str, base: str, tier: str, token_index: int,
                 severity_value_indexes: "list[int]"):
        self.prefix = prefix
        self.base = base
        self.tier = tier
        self.token_index = token_index
        self.severity_value_indexes = severity_value_indexes


def threshold_reads(tokens: list[tuple[str, str]]) -> list[Read]:
    """Every threshold selector in *tokens*. Raises :class:`Unjudged` for a
    reference whose shape this gate cannot judge."""
    reads: list[Read] = []
    i = 0
    while i < len(tokens):
        kind, text = tokens[i]
        if _THRESHOLD_INFIX in text and kind != "ident":
            raise Unjudged(f"{text!r} names a threshold series inside a {kind} token")
        if kind != "ident" or _THRESHOLD_INFIX not in text:
            i += 1
            continue
        m = _THRESHOLD_NAME.match(text)
        if m is None:
            raise Unjudged(f"{text!r} is not a `tenant(_version)?:alert_threshold:<key>` name")
        prefix, name = m.group(1), m.group(2)
        sev_crit = False
        sev_idx: list[int] = []
        j = i + 1
        if j < len(tokens) and tokens[j] == ("op", "{"):
            j += 1
            while True:
                if j < len(tokens) and tokens[j] == ("op", "}"):
                    j += 1
                    break
                if j + 2 >= len(tokens) or tokens[j][0] != "ident" \
                        or tokens[j + 1][0] != "op" or tokens[j + 2][0] != "str":
                    raise Unjudged(f"cannot parse the matchers of {text!r}")
                label, op, value = tokens[j][1], tokens[j + 1][1], tokens[j + 2][1]
                if op not in ("=", "!=", "=~", "!~"):
                    raise Unjudged(f"cannot parse the matchers of {text!r}")
                if label == "severity":
                    if "\\" in value:
                        raise Unjudged(
                            f"{text!r} has a severity matcher with an escape "
                            f"({value!r}) — PromQL decodes it; write the value plainly")
                    if op != "=":
                        raise Unjudged(
                            f"{text!r} has a severity matcher with {op!r} — only "
                            "`severity=\"...\"` is judged")
                    sev_idx.append(j + 2)
                    if value == "critical":
                        sev_crit = True
                j += 3
                if j < len(tokens) and tokens[j] == ("op", ","):
                    j += 1
        if name.endswith(CRITICAL_KEY_SUFFIX):
            reads.append(Read(prefix, name[: -len(CRITICAL_KEY_SUFFIX)], "critical", i, sev_idx))
        else:
            reads.append(Read(prefix, name, "critical" if sev_crit else "warning", i, sev_idx))
        i = j if j > i + 1 else i + 1
    return reads


def swap_severity(tokens: list[tuple[str, str]], base: str) -> tuple[list[tuple[str, str]], int]:
    """(B): *tokens* with `severity="warning"` -> `severity="critical"` on the
    warning-tier selectors that read *base*, and how many were swapped."""
    out = list(tokens)
    n = 0
    for rd in threshold_reads(tokens):
        if rd.base != base or rd.tier != "warning":
            continue
        for k in rd.severity_value_indexes:
            if out[k] == ("str", "warning"):
                out[k] = ("str", "critical")
                n += 1
    return out, n


# ---------------------------------------------------------------------------
# The rule index
# ---------------------------------------------------------------------------

class Index:
    """Rules with their tokens, threshold reads and record references."""

    def __init__(self, rules: list[Rule]):
        self.rules = rules
        self.problems: list[str] = []
        self.tokens: dict[int, list] = {}
        self.direct: dict[int, list[Read]] = {}
        # Record names are global in Prometheus: a rule reads a record of any
        # pack, so the index is by name alone.
        self.records: dict[str, list[Rule]] = {}
        for r in rules:
            if r.kind == "record":
                self.records.setdefault(r.name, []).append(r)
        for r in rules:
            try:
                toks = tokenize(r.expr)
                self.tokens[id(r)] = toks
                self.direct[id(r)] = threshold_reads(toks)
            except Unjudged as exc:
                self.tokens[id(r)] = []
                self.direct[id(r)] = []
                self.problems.append(
                    f"UNJUDGED-REFERENCE: {r.where}: {exc}. This gate reads threshold "
                    "series only as `tenant(_version)?:alert_threshold:<key>` with "
                    "`label=\"value\"` matchers; write the reference in that shape.")
        self._tiers: dict[int, frozenset] = {}
        self._flag_both_tier_reads()

    def _flag_both_tier_reads(self) -> None:
        """A threshold record that selects no single severity (the
        `tenant_version:` records keep `severity` as a dimension) carries both
        tiers: a selector on it without a `severity` matcher reads both, which
        this gate cannot judge — fail closed."""
        both = set()
        for name, recs in self.records.items():
            if _THRESHOLD_NAME.match(name) is None or len(recs) != 1:
                continue
            toks = self.tokens[id(recs[0])]
            if not any(toks[k] == ("ident", "severity") and toks[k + 1][0] == "op"
                       and toks[k + 1][1] in ("=", "!=", "=~", "!~")
                       for k in range(len(toks) - 1)):
                both.add(name)
        for r in self.rules:
            for rd in self.direct[id(r)]:
                name = self.tokens[id(r)][rd.token_index][1]
                if name in both and not rd.severity_value_indexes:
                    self.problems.append(
                        f"UNJUDGED-REFERENCE: {r.where}: {name!r} carries both tiers "
                        "(its record keeps `severity` as a dimension) and is read without "
                        "a `severity` matcher — add `{severity=\"warning\"}` or "
                        "`{severity=\"critical\"}`.")

    def referenced_records(self, r: Rule) -> list[str]:
        names = {t for k, t in self.tokens[id(r)] if k == "ident"}
        return sorted(n for n in names if n in self.records and n != r.name)

    def tiers(self, r: Rule, _seen: "frozenset | None" = None) -> frozenset:
        """{(base, tier)} that *r* reads, directly or through records (any tenant pack)."""
        if id(r) in self._tiers:  # only complete results are stored
            return self._tiers[id(r)]
        seen = (_seen or frozenset()) | {r.name}
        out = {(rd.base, rd.tier) for rd in self.direct[id(r)]}
        for n in self.referenced_records(r):
            if n in seen:
                continue
            for rec in self.records[n]:
                out |= self.tiers(rec, seen)
        res = frozenset(out)
        if _seen is None:  # a nested result may be cut short by a cycle
            self._tiers[id(r)] = res
        return res

    def alerts(self) -> list[Rule]:
        return [r for r in self.rules if r.kind == "alert"]

    def one_record(self, name: str) -> "Rule | None":
        recs = self.records.get(name) or []
        return recs[0] if len(recs) == 1 else None


# ---------------------------------------------------------------------------
# Twin equivalence
# ---------------------------------------------------------------------------

def _fmt(tok: tuple[str, str]) -> str:
    kind, text = tok
    return f'"{text}"' if kind == "str" else text


def _record_shape_problems(ix: Index, file: str, prefix: str, base: str) -> list[str]:
    """(A): record `<prefix>:alert_threshold:K_critical` must be record
    `<prefix>:alert_threshold:K` with exactly one severity swap."""
    w_name = f"{prefix}:alert_threshold:{base}"
    c_name = w_name + CRITICAL_KEY_SUFFIX
    w_rec, c_rec = ix.one_record(w_name), ix.one_record(c_name)
    if w_rec is None or c_rec is None:
        missing = [n for n, r in ((w_name, w_rec), (c_name, c_rec)) if r is None]
        return [f"record {', '.join(repr(n) for n in missing)} is missing (needed by {file}) "
                "or defined more than once — the (A) form needs exactly one of each; "
                f"define {c_name!r} as {w_name!r} with severity=\"critical\""]
    w_toks = ix.tokens[id(w_rec)]
    hits = [k for k in range(len(w_toks) - 2)
            if w_toks[k] == ("ident", "severity") and w_toks[k + 1] == ("op", "=")
            and w_toks[k + 2] == ("str", "warning")]
    if len(hits) != 1:
        return [f"record {w_name!r} has {len(hits)} `severity=\"warning\"` matcher(s); "
                f"the (A) form needs exactly one so that {c_name!r} is it with "
                "severity=\"critical\""]
    want = list(w_toks)
    want[hits[0] + 2] = ("str", "critical")
    if ix.tokens[id(c_rec)] != want:
        return [f"record {c_name!r} is not record {w_name!r} with severity=\"warning\" -> "
                f"severity=\"critical\" — write its expr as exactly that"]
    if c_rec.labels != w_rec.labels:
        return [f"record {c_name!r} sets labels {c_rec.labels!r}, record {w_name!r} sets "
                f"{w_rec.labels!r} — a record's labels rewrite its series (a `tenant` "
                "there breaks the join); give both the same labels"]
    return []


def twin_problems(ix: Index, w: Rule, c: Rule, base: str,
                  for_exceptions: dict[str, tuple[str, str]]) -> list[str]:
    """Why C is not W's critical twin for threshold *base* ([] = twins)."""
    probs: list[str] = []
    if c.name != w.name + CRITICAL_ALERT_SUFFIX:
        probs.append(f"name: the critical alert must be named {w.name + CRITICAL_ALERT_SUFFIX!r}")
    if (w.file, w.group) != (c.file, c.group):
        probs.append(f"place: {w.name!r} is in {w.file} group {w.group!r}, {c.name!r} in "
                     f"{c.file} group {c.group!r} — put both in the same group")
    lw = {k: v for k, v in w.labels.items() if k != "severity"}
    lc = {k: v for k, v in c.labels.items() if k != "severity"}
    if lw != lc:
        diff = sorted(k for k in set(lw) | set(lc) if lw.get(k) != lc.get(k))
        probs.append("labels: differ beyond severity on " + ", ".join(
            f"{k} ({lw.get(k)!r} vs {lc.get(k)!r})" for k in diff)
            + " — make them equal")
    for who in (w, c):
        mg = who.labels.get("metric_group")
        if mg != base:
            probs.append(f"metric_group: {who.name!r} carries {mg!r}; it must be the "
                         f"threshold key {base!r}")
    if w.for_ != c.for_:
        exc = for_exceptions.get(w.name)
        if exc is None:
            probs.append(f"for: {w.for_!r} vs {c.for_!r} — make them equal (FOR_EXCEPTIONS "
                         "is exit-locked)")
        elif tuple(exc) != (w.for_, c.for_):
            probs.append(f"for: {w.for_!r} vs {c.for_!r}, but FOR_EXCEPTIONS[{w.name!r}] "
                         f"says {tuple(exc)!r}")
    if set(w.annotations) != set(c.annotations):
        only_w = sorted(set(w.annotations) - set(c.annotations))
        only_c = sorted(set(c.annotations) - set(w.annotations))
        probs.append(f"annotations: keys only on {w.name!r}: {only_w}, only on "
                     f"{c.name!r}: {only_c} — carry the same keys")
    probs.extend(_expr_problems(ix, w, c, base))
    return probs


def _expr_problems(ix: Index, w: Rule, c: Rule, base: str) -> list[str]:
    tw, tc = ix.tokens[id(w)], ix.tokens[id(c)]
    if len(tw) != len(tc):
        return [f"expr: {len(tw)} tokens vs {len(tc)} — the critical expr must be the "
                "warning expr with only the threshold token (or the record it reads) "
                "swapped"]
    probs: list[str] = []
    a_prefixes: set[str] = set()
    for k, (a, b) in enumerate(zip(tw, tc)):
        if a == b:
            continue
        ma = _THRESHOLD_NAME.match(a[1]) if a[0] == "ident" else None
        mb = _THRESHOLD_NAME.match(b[1]) if b[0] == "ident" else None
        if ma and mb and ma.group(1) == mb.group(1) and ma.group(2) == base \
                and mb.group(2) == base + CRITICAL_KEY_SUFFIX:
            a_prefixes.add(ma.group(1))
            continue
        rw = ix.one_record(a[1]) if a[0] == "ident" else None
        rc = ix.one_record(b[1]) if b[0] == "ident" else None
        if rw is not None and rc is not None:
            want, n = swap_severity(ix.tokens[id(rw)], base)
            if n == 0:
                probs.append(f"expr: record {rw.name!r} (read by {w.name!r}) has no "
                             f"`severity=\"warning\"` on a selector reading {base!r}, so "
                             f"{rc.name!r} cannot be its (B) critical form")
            elif ix.tokens[id(rc)] != want:
                probs.append(f"expr: record {rc.name!r} is not record {rw.name!r} with "
                             f"severity=\"warning\" -> severity=\"critical\" on the "
                             f"selectors reading {base!r} — write it as exactly that")
            elif rc.labels != rw.labels:
                probs.append(f"labels: record {rc.name!r} sets {rc.labels!r}, record "
                             f"{rw.name!r} sets {rw.labels!r} — give both the same labels")
            continue
        probs.append(f"expr: token {k} is {_fmt(a)!s} in {w.name!r} but {_fmt(b)!s} in "
                     f"{c.name!r} — only the threshold token for {base!r} (or a record "
                     "pair differing by severity) may differ")
    if (base, "warning") in ix.tiers(c):
        probs.append(f"expr: {c.name!r} still reads the warning tier of {base!r} — every "
                     f"read of {base!r} in {w.name!r} must become its critical form")
    for prefix in sorted(a_prefixes):
        probs.extend(_record_shape_problems(ix, w.file, prefix, base))
    return probs


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def run_check(
    rules: "list[Rule] | None" = None,
    declared: "dict[str, str] | None" = None,
    for_exceptions: "dict[str, tuple[str, str]] | None" = None,
) -> dict[str, list[str]]:
    """Return {errors}. Inputs default to the real repo (tenant rule packs,
    registry `critical_of`, FOR_EXCEPTIONS); hermetic tests inject them."""
    if rules is None:
        rules = collect_rules()
    if declared is None:
        declared = declared_critical_bases()
    if for_exceptions is None:
        for_exceptions = FOR_EXCEPTIONS

    ix = Index(rules)
    errors: list[str] = list(ix.problems)

    crit_readers: dict[str, list[Rule]] = {}   # K -> rules reading K's critical tier
    for r in rules:
        for base, tier in sorted(ix.tiers(r)):
            if tier == "critical":
                crit_readers.setdefault(base, []).append(r)

    # Threshold records named X_critical: X must be declared (check 2).
    crit_records: dict[str, list[Rule]] = {}
    for r in rules:
        m = _THRESHOLD_NAME.match(r.name) if r.kind == "record" else None
        if m and m.group(2).endswith(CRITICAL_KEY_SUFFIX):
            crit_records.setdefault(m.group(2)[: -len(CRITICAL_KEY_SUFFIX)], []).append(r)

    twins: dict[str, tuple[Rule, Rule]] = {}  # W name -> (W, C)
    for base in sorted(set(declared) | set(crit_readers) | set(crit_records)):
        if base not in declared:
            for r in crit_readers.get(base, []) + crit_records.get(base, []):
                errors.append(
                    f"UNDECLARED-CRITICAL-TIER: {r.where} reads or defines the critical "
                    f"tier of {base!r}, but no `{base}{CRITICAL_KEY_SUFFIX}` is declared. "
                    f"Add `{base}{CRITICAL_KEY_SUFFIX}` to scaffold_tenant.RULE_PACKS"
                    "[<pack>][\"optional_overrides\"] and run "
                    "`python3 scripts/tools/lint/check_threshold_registry.py --regen`, "
                    "or stop reading that tier.")
        cs = [r for r in crit_readers.get(base, []) if r.kind == "alert"]
        if not cs:
            if base in declared:
                errors.append(
                    f"DECLARED-WITHOUT-RULE: {declared[base]!r} is declared (registry "
                    f"critical_of {base!r}) but no alert in the tenant rule packs reads "
                    f"the critical tier of {base!r}. Add the critical twin of {base!r}'s "
                    "warning alert, or remove the declaration from "
                    "scaffold_tenant.RULE_PACKS and run --regen.")
            continue
        if len(cs) > 1:
            errors.append(
                f"AMBIGUOUS-CRITICAL-TIER: {len(cs)} alerts read the critical tier of "
                f"{base!r}: " + ", ".join(r.where for r in cs)
                + ". Exactly one alert may; merge them or make the others read another tier.")
            continue
        c = cs[0]
        if c.labels.get("severity") != "critical":
            errors.append(
                f"CRITICAL-TIER-NOT-CRITICAL: {c.where} reads the critical tier of "
                f"{base!r} but is labelled severity {c.labels.get('severity')!r}. "
                "Label it `severity: critical`.")
            continue
        ws = [r for r in ix.alerts()
              if r.labels.get("severity") == "warning" and (base, "warning") in ix.tiers(r)]
        if not ws:
            errors.append(
                f"NO-WARNING-TWIN: {c.where} reads the critical tier of {base!r}, but no "
                f"`severity: warning` alert reads the warning tier of {base!r}. Add the "
                "warning alert it escalates, as its twin.")
            continue
        verdicts = [(w, twin_problems(ix, w, c, base, for_exceptions)) for w in ws]
        ok = [w for w, p in verdicts if not p]
        if len(ok) == 1:
            twins[ok[0].name] = (ok[0], c)
            continue
        if len(ok) > 1:  # pragma: no cover — the name rule makes the twin unique
            errors.append(f"AMBIGUOUS-TWIN: {c.where} is the twin of "
                          + ", ".join(w.name for w in ok))
            continue
        named = [(w, p) for w, p in verdicts if w.name + CRITICAL_ALERT_SUFFIX == c.name]
        for w, probs in (named or verdicts):
            errors.append(
                f"NOT-A-TWIN: {c.where} (critical tier of {base!r}) is not the twin of "
                f"{w.where}: " + "; ".join(probs))

    # FOR_EXCEPTIONS is exit-locked.
    by_name = {r.name: r for r in ix.alerts()}
    for wname, exc in sorted(for_exceptions.items()):
        w = by_name.get(wname)
        c = by_name.get(wname + CRITICAL_ALERT_SUFFIX)
        if w is None or c is None:
            errors.append(
                f"STALE-FOR-EXCEPTION: FOR_EXCEPTIONS[{wname!r}] names no "
                f"{wname!r} / {wname + CRITICAL_ALERT_SUFFIX!r} alert pair — remove the entry.")
        elif w.for_ == c.for_:
            errors.append(
                f"STALE-FOR-EXCEPTION: {wname!r} and its critical twin now share "
                f"`for: {w.for_}` — remove FOR_EXCEPTIONS[{wname!r}].")
        elif tuple(exc) != (w.for_, c.for_):
            errors.append(
                f"FOR-EXCEPTION-MISMATCH: FOR_EXCEPTIONS[{wname!r}] says {tuple(exc)!r} "
                f"but the pair reads ({w.for_!r}, {c.for_!r}) — the table may only "
                "shrink; align the `for` values or correct the entry to the pair as it was.")

    return {"errors": errors, "twins": sorted(twins)}


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "critical 層孿生 gate：每個宣告的 <base>_critical 恰有一條 critical 告警，且是 warning 告警的孿生（#3658）",
            "Critical-tier twin gate: every declared <base>_critical has exactly "
            "one critical alert, the twin of the warning alert (#3658)"))
    parser.add_argument(
        "--ci", action="store_true",
        help=i18n_text("有任何違規即 exit 1",
                       "exit 1 on any violation"))
    args = parser.parse_args(argv)

    try:
        result = run_check()
    except _lib_consumer_read.ConsumerReadError as exc:
        # #3206: a rule file Prometheus would not load, or no da-read.
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except Exception as exc:  # noqa: BLE001 — caller error, not a violation
        print(f"ERROR: critical-tier twin check crashed: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    errors = result["errors"]
    for msg in errors:
        print(f"❌ {msg}", file=sys.stderr)
    if errors:
        print(
            f"\n{len(errors)} critical-tier twin violation(s) — #3658. "
            "See scripts/tools/lint/check_critical_tier_twins.py.",
            file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK

    print(f"✅ critical-tier twins OK — {len(result['twins'])} warning/critical pair(s), "
          "each the twin of its declared tier.", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
