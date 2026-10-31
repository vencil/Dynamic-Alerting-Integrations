#!/usr/bin/env python3
"""Rule-pack copy drift guard (ADR-024 PR3-pre).

Every rule pack lives in THREE places that must stay semantically identical:

  1. rule-packs/rule-pack-<name>.yaml                 (canonical source)
  2. k8s/03-monitoring/configmap-rules-<name>.yaml    (ConfigMap deploy copy)
  3. operator-manifests/da-rule-pack-<name>.yaml      (PrometheusRule CRD copy)

The three have DIFFERENT serializations (raw `groups:` block scalars vs a
ConfigMap wrapping rules in `data:` keys vs a CRD with machine-serialized
single-line `expr` strings), so a raw `diff` / `sha256sum` ALWAYS differs even
when the rules are identical — a byte comparison is the wrong tool here.

This guard instead compares them SEMANTICALLY: each source is parsed, every
rule is reduced to a canonical identity → content map (expr whitespace
collapsed; all other fields YAML-normalized), and the three maps are compared.
Comparing PARSED structures (not raw text) is what makes the check robust to
serialization / YAML-emitter differences — the fragility that bites a naive
text diff simply does not exist once both sides are valid YAML parsed into
the same shape.

Why this matters: nothing else enforces content parity — the `Rule Pack stats
drift` hook only counts rules, so a hot-fix that edits one copy but forgets
another would ship behavior-divergent PromQL to production (a copy firing on
stale thresholds) and CI would wave it through. This guard makes any such
divergence a hard CI failure.

Exit codes:
    0  All packs' three copies are semantically identical
    1  Drift detected (--ci) — details printed
    2  Error (missing file, YAML parse failure)

Usage:
    python scripts/tools/lint/check_rulepack_sync.py            # report
    python scripts/tools/lint/check_rulepack_sync.py --ci       # exit 1 on drift
    python scripts/tools/lint/check_rulepack_sync.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover - compat shim optional
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, Report  # noqa: E402
from _lib_compat import find_project_root  # noqa: E402  (#2852)


def _repo_root() -> "Path | None":
    """This tree's root, never an enclosing project's (#2852).

    The nearest ancestor with a ``.git`` used to decide it, so a copy without
    one (vendored, ``git archive``) compared the OUTER project's rule packs.
    None outside a ``scripts/tools`` tree, e.g. the recipe-preview image, which
    imports this module's helpers but never runs ``main``.
    """
    return find_project_root(_THIS_DIR)


# One left-to-right alternation over the things whose CONTENT is not code:
# PromQL's three string-literal spellings and its line comment. `finditer`
# takes the earliest match, so whichever opens first wins — which is exactly
# the interleaving rule needed here. `up == 0  # don't` lexes the comment
# (the `#` precedes the apostrophe), while `up{job="a#b"}` lexes the string
# (the quote precedes the hash). Handling either one alone gets the other wrong.
_EXPR_OPAQUE_RE = re.compile(
    r'"(?:[^"\\]|\\.)*"'        # "…", backslash escapes
    r"|'(?:[^'\\]|\\.)*'"       # '…', backslash escapes
    r"|`[^`]*`"                 # `…`, raw — no escapes, per PromQL
    r"|#[^\n]*"                 # line comment, to end of line
)


def _norm_expr(expr: Any) -> str:
    """Canonicalize a PromQL expression for serialization-agnostic comparison.

    Three normalizations so logically-equivalent PromQL compares equal no
    matter how it was serialized (block scalar vs `\\n`-escaped string):
      1. Strip `#` line comments.
      2. Collapse all whitespace runs to a single space.
      3. Remove whitespace adjacent to operator/grouping punctuation so
         `by(tenant)` == `by (tenant)` == `by(tenant, version)` (token-level
         minify, Gemini review) — emitters differ on this spacing.

    ⛔ ALL THREE APPLY TO CODE ONLY. Run blindly over the whole expression they
    rewrite the insides of string literals, and a label matcher's value is not
    formatting — it is the query. Every one of these used to compare EQUAL:

        up{job="a#b"}      vs  up{job="a#c"}      (and the normal form was the
                                                   truncated fragment `up{job="a`)
        up{msg="a  b"}     vs  up{msg="a b"}
        up{path=~"/a, b"}  vs  up{path=~"/a,b"}   — different regexes
        up{re=~"(a )b"}    vs  up{re=~"(a)b"}     — different regexes
        up{msg="a > b"}    vs  up{msg="a>b"}

    …in all three quote styles. Two consumers, and both directions are wrong:
    this module's drift gate stops seeing a real difference between a source
    pack and its deployed copies, and `_rule_tree._alert_names` accepts a
    hand-authored rule as a faithful copy of a pack it merely resembles — which
    is the provenance check that decides whether the platform contracts apply.

    Verified no-op on the tree as it stands: all 930 shipped expressions
    normalise identically before and after (none currently carries a comment or
    a string literal containing any of the characters above).

    An UNTERMINATED quote matches nothing here and stays in the code stream, so
    a `#` after it reads as a comment. That is invalid PromQL either way and
    promtool rejects it upstream; this function does not try to be a parser.
    """
    literals: list[str] = []

    def _stash(m: "re.Match") -> str:
        token = m.group(0)
        if token.startswith("#"):
            # A space, not "", though here the two are EQUIVALENT: a comment
            # always runs to end-of-line, so a newline (or EOF) follows it and
            # the whitespace pass merges either spelling away. Measured — the
            # mutation to "" survives. It is a space because the first draft of
            # this function normalized each code run in isolation, where the
            # difference was real (`sum by(t) (\n  # c\n  x\n)` kept a space the
            # comment-free spelling does not have) and the existing
            # comment-strip test caught it. Keeping the separator costs nothing
            # and removes the trap if the passes are ever split up again.
            return " "
        literals.append(token)
        return f"\x00{len(literals) - 1}\x00"

    # NUL is not a legal character in a YAML scalar (both 1.1 and 1.2 forbid the
    # C0 range apart from tab/LF/CR, and PyYAML refuses to emit or load one), so
    # a NUL-delimited index cannot collide with expression text.
    s = _EXPR_OPAQUE_RE.sub(_stash, str(expr))
    s = re.sub(r"\s+", " ", s)
    # Drop spaces around ( ) , and comparison operators; normalize the space
    # after by/without/on/group_left/group_right to none before '('.
    s = re.sub(r"\s*([(),])\s*", r"\1", s)
    s = re.sub(r"\s*(==|!=|>=|<=|>|<)\s*", r"\1", s)
    s = re.sub(r"\b(by|without|on|ignoring|group_left|group_right)\s+\(", r"\1(", s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: literals[int(m.group(1))], s)
    return s.strip()


def _norm_rule(rule: dict) -> dict:
    """Canonicalize a single rule for comparison: expr whitespace-collapsed,
    every other field left as YAML-parsed (already serialization-normalized)."""
    out = dict(rule)
    if "expr" in out:
        out["expr"] = _norm_expr(out["expr"])
    return out


def _rule_identity(group_name: str, rule: dict) -> str:
    if "record" in rule:
        return f"{group_name}::record::{rule['record']}"
    if "alert" in rule:
        return f"{group_name}::alert::{rule['alert']}"
    return f"{group_name}::unknown::{json.dumps(rule, sort_keys=True)[:40]}"


def _extract(groups: List[dict]) -> Dict[str, str]:
    """Reduce a list of rule groups to {rule_identity: canonical_json}.

    The group's `interval` is folded into each rule's content so an interval
    drift between copies is also caught.
    """
    out: Dict[str, str] = {}
    for g in groups or []:
        gname = g.get("name", "")
        interval = g.get("interval")
        for rule in g.get("rules", []):
            ident = _rule_identity(gname, rule)
            payload = {"interval": interval, "rule": _norm_rule(rule)}
            out[ident] = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return out


def _consumer_read():
    """`_lib_consumer_read`, imported where a file is read (#3206).

    ⛔ Not at module scope: the recipe-preview image copies this module (for
    `_extract` / `_diff_maps`, which read no file) without `_lib_consumer_read`
    or da-read; the readers below are never called there."""
    import _lib_consumer_read  # noqa: PLC0415
    return _lib_consumer_read


def _groups_from_rulepack(path: Path) -> List[dict]:
    """The pack's rule groups as Prometheus' rule loader reads them (#3206,
    ADR-036 §12: `da-read prometheus-rules`). A pack the loader refuses
    raises `RuleFilesRefused`, naming it."""
    cr = _consumer_read()
    return cr.prometheus_rule_files([path])[0].groups


def _manifest_groups(path: Path, kind: str) -> List[dict]:
    cr = _consumer_read()
    groups: List[dict] = []
    for rf in cr.require_read(cr.manifest_rule_file_results([path])):
        if rf.kind == kind:
            groups.extend(rf.groups)
    return groups


def _groups_from_configmap(path: Path) -> List[dict]:
    """A ConfigMap wraps rules in `data:` keys; each value is a rule file with
    its own top-level `groups:`. Merge them all. The manifest is read the way
    kubectl reads it, each value the way Prometheus' rule loader does
    (#3206); a value the loader refuses raises `RuleFilesRefused`."""
    return _manifest_groups(path, "ConfigMap")


def _groups_from_operator(path: Path) -> List[dict]:
    """A PrometheusRule's `spec.groups`, read as the rule file the Prometheus
    Operator writes from it is read by Prometheus' rule loader (#3206)."""
    return _manifest_groups(path, "PrometheusRule")


def _diff_maps(canonical: Dict[str, str], other: Dict[str, str]) -> List[str]:
    """Return human-readable divergences of `other` vs the canonical source."""
    findings: List[str] = []
    ck, ok = set(canonical), set(other)
    for missing in sorted(ck - ok):
        findings.append(f"missing in copy: {missing}")
    for extra in sorted(ok - ck):
        findings.append(f"extra in copy (not in source): {extra}")
    for ident in sorted(ck & ok):
        if canonical[ident] != other[ident]:
            findings.append(f"content differs: {ident}")
    return findings


def copy_paths(repo: Path, name: str) -> Tuple[Path, Path]:
    """(ConfigMap copy, PrometheusRule copy) of pack *name* under *repo*: the
    file names their generators write, both derived from the one declaration
    (#3471: `CONFIGMAP_RULES.file_name`, `OPERATOR_RULES.file_name`)."""
    cr = _consumer_read()
    return (repo / cr.CONFIGMAP_RULES.directory / cr.CONFIGMAP_RULES.file_name(name),
            repo / cr.OPERATOR_RULES.directory / cr.OPERATOR_RULES.file_name(name))


def source_path(repo: Path, name: str) -> Path:
    """The rule pack *name* under *repo* by its declared name (``.yaml``) —
    for a caller that has only the name; `main` passes the file it listed."""
    cr = _consumer_read()
    return repo / cr.RULE_PACKS.directory / cr.RULE_PACKS.file_name(name)


def read_copies(repo: Path, packs: List[Path]) -> Dict[str, Tuple[List[dict], List[dict], List[dict]]]:
    """{pack: (rule-pack groups, configmap groups, operator groups)} for every
    rule pack file of *packs* whose two copies exist, read the way their
    consumers read them (#3206) in two da-read runs for the whole set — not
    three per pack. Raises `RuleFilesRefused` naming any copy Prometheus' rule
    loader refuses."""
    cr = _consumer_read()
    present = [(cr.RULE_PACKS.member(p), p) for p in packs
               if all(c.exists() for c in copy_paths(repo, cr.RULE_PACKS.member(p)))]
    rule_packs = cr.prometheus_rule_files([p for _, p in present])
    manifests = cr.require_read(cr.manifest_rule_file_results(
        [c for n, _ in present for c in copy_paths(repo, n)]))
    out = {}
    for (n, _), rf in zip(present, rule_packs):
        cm, op = (str(c) for c in copy_paths(repo, n))
        out[n] = (rf.groups,
                  [g for m in manifests if m.manifest == cm and m.kind == "ConfigMap"
                   for g in m.groups],
                  [g for m in manifests if m.manifest == op and m.kind == "PrometheusRule"
                   for g in m.groups])
    return out


def check_pack(repo: Path, name: str,
               copies: "Tuple[List[dict], List[dict], List[dict]] | None" = None,
               source: "Path | None" = None,
               ) -> Tuple[bool, List[str]]:
    """Compare the three copies of one pack (*copies* from `read_copies`;
    read here when not given, the source from *source*, by default the
    declared ``.yaml`` name). Returns (ok, findings)."""
    rp = source if source is not None else source_path(repo, name)
    cm, op = copy_paths(repo, name)

    findings: List[str] = []
    if not cm.exists():
        findings.append(f"configmap copy missing: {cm}")
    if not op.exists():
        findings.append(f"operator copy missing: {op}")
    if findings:
        return False, findings

    if copies is None:
        copies = (_groups_from_rulepack(rp), _groups_from_configmap(cm),
                  _groups_from_operator(op))
    canonical = _extract(copies[0])
    cm_map = _extract(copies[1])
    op_map = _extract(copies[2])

    for label, other in (("configmap", cm_map), ("operator", op_map)):
        for f in _diff_maps(canonical, other):
            findings.append(f"[{label}] {f}")

    return (len(findings) == 0), findings


def main() -> "int | Report":
    try_utf8_stdout()
    parser = argparse.ArgumentParser(description="Rule-pack 3-copy semantic drift guard")
    parser.add_argument("--ci", action="store_true", help="exit 1 on any drift")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    repo = _repo_root()
    if repo is None:
        print(f"ERROR: {_THIS_DIR} is not inside a scripts/tools tree, so there "
              f"is no repo whose rule-packs/ to check", file=sys.stderr)
        return EXIT_CALLER_ERROR
    cr = _consumer_read()
    results = {}
    any_drift = False
    try:
        # #3471: the rule packs are the one declaration's (rule-pack-*.yaml|yml;
        # an undeclared YAML beside them is named) — a `.yml` pack is checked
        # too, no longer passed over.
        packs_dir = repo / cr.RULE_PACKS.directory
        pack_files = cr.rule_pack_files(packs_dir) if packs_dir.is_dir() else []
        if not pack_files:
            print(f"ERROR: no rule packs found under {cr.RULE_PACKS.directory}/",
                  file=sys.stderr)
            return EXIT_CALLER_ERROR
        copies = read_copies(repo, pack_files)
        for pf in pack_files:
            name = cr.RULE_PACKS.member(pf)
            ok, findings = check_pack(repo, name, copies.get(name), source=pf)
            results[name] = {"ok": ok, "findings": findings}
            if not ok:
                any_drift = True
    except cr.ConsumerReadError as exc:
        # #3206: a copy Prometheus' rule loader refuses (named, with the
        # loader's reason), or no da-read — the parse-failure exit as before;
        # #3471: an undeclared YAML in rule-packs/ too.
        print(f"ERROR: {cr.error_text(exc)}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if not args.json:
        for name, r in results.items():
            mark = "✅" if r["ok"] else "❌"
            print(f"  {mark} {name}")
            for f in r["findings"]:
                print(f"       {f}")
        total = len(results)
        bad = sum(1 for r in results.values() if not r["ok"])
        print(f"\nRule-pack sync: {total - bad}/{total} packs in sync"
              + (f" — {bad} with drift" if bad else ""))

    if any_drift and args.ci:
        print("\n❌ Rule-pack copy drift detected. The 3 copies "
              "(rule-packs/ ↔ configmap ↔ operator) must match semantically.",
              file=sys.stderr)
    status = EXIT_VIOLATION if any_drift and args.ci else EXIT_OK
    if args.json:
        return Report(results, status)  # #3216: run() encodes it
    return status


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)  # #3216: run() encodes the --json document
