#!/usr/bin/env python3
"""check_cluster_identity.py — rules keep `da_cluster` wherever they keep an identity label (#4097).

ADR-006 "one tenant in several clusters": every series from a tenant
exporter (and from the Kubernetes state / container jobs) carries the
platform's cluster label `da_cluster`, and a rule that reads such series
aggregates and joins per cluster. Two shapes break that without any test
going red on a single-cluster fixture:

  by-drops-cluster     `sum by (tenant, instance) (x)` over cluster-scoped
                       series — the two clusters' series merge.
  on-drops-cluster     `a unless on (tenant, instance) b`, both sides
                       cluster-scoped — one cluster's series cancels the
                       other's (the masking #4097 was opened for).
  without-cluster      `sum without (da_cluster) (x)`.
  ignoring-cluster     `a and ignoring (da_cluster) b`, both sides
                       cluster-scoped.
  one-side-needs-group `x > on (tenant) threshold` with x per cluster and no
                       `group_left`: with two clusters Prometheus refuses the
                       match ("many-to-one matching must be explicit") and the
                       rule yields nothing; with one, the result silently
                       loses `da_cluster`.

  on-cluster-with-flat-side
                       `x > on (tenant, da_cluster) group_left threshold`:
                       the threshold has no `da_cluster`, so once x carries
                       one the two never match — the alert goes silent (and
                       an `unless on (tenant, da_cluster) maintenance` stops
                       muting). Per-tenant series are matched `on (tenant)`.

What is judged, and how it is read
----------------------------------
Each rule's expression is read as Prometheus' parser builds it
(`da-read promql-shape`), never off its text. Every node gets a SCOPE:

  cluster   its series carry `da_cluster`
  flat      they do not: series threshold-exporter and the other platform
            components serve (thresholds, maintenance state, tenant
            metadata — one per tenant, joined with `on(tenant) group_left`),
            and whatever an aggregation or a join left without the label

A selector's scope is its metric's source: a platform family
(`_lib_metric_sources.resolve_family`) is flat unless it is one of the
Kubernetes jobs ADR-006 names; a name a rule of the same tree records has
the scope of that rule's expression; anything else is an exporter's series
and is cluster — so a metric nobody classified is held to the contract, not
excused from it. `up` and `scrape_*` are every target's: they are flat
only under a `job` this file lists as a platform job, and a `job` it does
not know is a failure of the shipped-tree test, not a guess.

Not judged (say so rather than look covered)
--------------------------------------------
  * "expected but absent" per cluster (ADR-006 contract 6): `expected unless
    on (tenant, db_type) present` has a flat left side, so one reporting
    cluster hides a silent one and nothing here says so. That is closed by
    declaring the expected clusters, not by a grouping.
  * `label_replace` / `label_join` that write `da_cluster`.
  * a tenant's own metric whose name starts with a platform family's prefix
    (`user_…`, `da_…`): `resolve_family` is the repo's one table of whose
    metric a name is, and it is read as is.
  * only the FIRST place the label is dropped is reported; fixing an inner
    grouping can surface the one around it.
  * a flat left side: `expected unless on (tenant) count by (tenant, da_cluster) (seen)`
    is the same hiding as the first point (CustomRecipeDiskInert,
    VersionAwareThresholdInert) — the left side has no cluster to keep.
  * a per-tenant series compared with NO `on (...)` at all, or pulled in with
    `group_left (da_cluster)`: only `on (..., da_cluster)` against a flat side
    is reported.
  * rules compiled from a tenant's own recipes at deploy time are not in
    this tree.

A `by (...)` / `on (...)` is judged only when it names an identity label
(tenant, instance, pod, namespace, node): `count(x)` or `sum by (db_type)`
is a deliberate fleet-wide figure and says so by naming none.

A deliberate tenant-wide read
-----------------------------
A rule that MEANS to look across a tenant's clusters says so where the rule
is, in the comment block directly above it:

    # da_cluster: tenant-wide by-drops-cluster (tenant) — <why>
    - alert: MariaDBNoPrimary

The declaration names the exact finding it stands for (kind and labels) and
carries a reason; that finding is then not reported for that rule — once per
declaration, and nothing else in the rule is excused. A declaration with no
reason, or one the rule no longer has a finding for, is itself a failure, so
it cannot outlive the grouping it was written for. There is no list of
exempt rules anywhere else.

The baseline
------------
The rule packs do not yet carry `da_cluster`; today's findings are listed
in tests/rulepacks/cluster_identity_baseline.yaml and the check is a
two-way ratchet on it (as check_vmalert_coverage.py is on its own): a
finding not listed is a failure, and so is a listed finding that is gone —
the list must equal the tree. `--generate` rewrites it. #4097 ends with the
list empty and this ratchet removed.

Exit codes: 0 the tree equals the baseline; 1 it does not; 2 could not run.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))
from _lib_yaml_strict import strict_safe_load  # noqa: E402  (#4111: duplicate keys refused)

import _lib_compat  # noqa: E402,F401  (import-time side effect; see _lib_compat)
import _lib_consumer_read  # noqa: E402
import _lib_io  # noqa: E402
import _lib_metric_sources  # noqa: E402

PROJECT_ROOT = _HERE.parent.parent.parent
BASELINE = PROJECT_ROOT / "tests" / "rulepacks" / "cluster_identity_baseline.yaml"

EXIT_OK, EXIT_VIOLATION, EXIT_CANNOT_RUN = 0, 1, 2

CLUSTER_LABEL = "da_cluster"
#: A grouping or matching that names one of these says "per this thing" — and
#: the thing exists once per cluster.
IDENTITY_LABELS = frozenset({"tenant", "instance", "pod", "namespace", "node"})

CLUSTER, FLAT = "cluster", "flat"

#: The scrape jobs ADR-006 has write `da_cluster`: the tenant-exporter job and
#: the Kubernetes state / container jobs the kubernetes rules read.
CLUSTER_JOBS = frozenset({"tenant-exporters", "kube-state-metrics",
                          "kubelet-cadvisor", "kubelet-volume-stats"})
#: Scrape jobs of the platform's own components: their `up` / `scrape_*` do
#: not carry `da_cluster`. ⛔ Every `job` the shipped rules read `up` under is
#: in this set or in CLUSTER_JOBS (the shipped-tree test holds that), so a new
#: job is classified on purpose.
PLATFORM_JOBS = frozenset({"prometheus", "alertmanager", "threshold-exporter",
                           "tenant-api", "vector-metrics"})
#: Prometheus' own synthetic series about alerts: they carry whatever the
#: alert carried, so they are no evidence either way.
_ALERT_SERIES = frozenset({"ALERTS", "ALERTS_FOR_STATE"})
#: Calls that return a vector without the operand's labels: `absent` keeps
#: only what its equality matchers name, `vector(s)` has none.
_FLAT_CALLS = frozenset({"absent", "absent_over_time", "vector"})
#: Calls that return a scalar.
_SCALAR_CALLS = frozenset({"scalar", "time", "pi"})
#: The date functions take a vector: with an argument that vector, labels
#: kept; with none it is `vector(time())` — one sample with no labels, a
#: vector and not a scalar (`label_replace(month(), …)` parses, of `time()`
#: it does not).
_DATE_CALLS = frozenset({"month", "year", "hour", "minute", "day_of_week",
                         "day_of_month", "day_of_year", "days_in_month"})
#: Aggregations that SELECT series rather than combine them: what comes out
#: keeps every label it came in with, whatever the grouping.
_SELECTING_AGGREGATIONS = frozenset({"topk", "bottomk", "limitk", "limit_ratio"})

BY_DROPS, ON_DROPS = "by-drops-cluster", "on-drops-cluster"
WITHOUT_CLUSTER, IGNORING_CLUSTER = "without-cluster", "ignoring-cluster"
ONE_SIDE = "one-side-needs-group"
ON_FLAT = "on-cluster-with-flat-side"


class CannotRun(Exception):
    pass


def _job_of(node: dict) -> "str | None":
    for m in node["matchers"]:
        if m["name"] == "job" and m["type"] == "=":
            return m["value"]
    return None


def is_per_target(name: str) -> bool:
    """A series every scrape target has (`up`, `scrape_*`): whose it is, is its `job`."""
    return name == "up" or name.startswith("scrape_")


def selector_scope(node: dict, records: "dict[str, str]") -> str:
    """The scope of one selector (see the module docstring)."""
    name = node["name"]
    if not name:   # `{__name__="x", …}`: the parser leaves the name in the matcher
        exact = [m["value"] for m in node["matchers"]
                 if m["name"] == "__name__" and m["type"] == "="]
        name = exact[0] if len(exact) == 1 else ""
    job = _job_of(node)
    if job is not None and job in CLUSTER_JOBS:
        return CLUSTER
    if name in _ALERT_SERIES:
        return FLAT
    if name in records:
        return records[name]
    if is_per_target(name):
        return FLAT if job in PLATFORM_JOBS else CLUSTER
    family = _lib_metric_sources.resolve_family(name) if name else None
    if family is not None:
        _key, _kind, spec = family
        cluster_family = (spec in CLUSTER_JOBS
                          or (isinstance(spec, tuple) and spec[-1] in CLUSTER_JOBS))
        return CLUSTER if cluster_family else FLAT
    return CLUSTER


def judge(node: dict, records: "dict[str, str]", found: "list[tuple[str, str]]") -> "str | None":
    """→ the scope of *node* (None for a scalar or a string), appending
    `(kind, labels)` to *found* for every grouping or matching under it that
    breaks the contract."""
    kind = node["kind"]
    if kind == "selector":
        return selector_scope(node, records)
    if kind in ("number", "string"):
        return None
    if kind in ("paren", "subquery", "unary"):
        return judge(node["expr"], records, found)
    if kind == "call":
        scopes = [judge(a, records, found) for a in node["args"]]
        if node["name"] in _FLAT_CALLS or (node["name"] in _DATE_CALLS and not node["args"]):
            return FLAT
        if node["name"] in _SCALAR_CALLS:
            return None
        # PromQL's functions take one vector; the other arguments are scalars or strings
        return next((sc for sc in scopes if sc is not None), None)
    if kind == "aggregate":
        if node["param"] is not None:
            judge(node["param"], records, found)
        inner = judge(node["expr"], records, found)
        labels = list(node["labels"])
        if inner != CLUSTER:
            return inner
        # A selecting aggregation's grouping says what it ranks WITHIN — a
        # ranking per tenant across the tenant's clusters is the same merge —
        # but the series it returns still carry the label.
        selecting = node["op"] in _SELECTING_AGGREGATIONS
        if node["without"]:
            if CLUSTER_LABEL in labels:
                found.append((WITHOUT_CLUSTER, ",".join(labels)))
                return CLUSTER if selecting else FLAT
            return CLUSTER
        if CLUSTER_LABEL in labels:
            return CLUSTER
        if IDENTITY_LABELS & set(labels):
            found.append((BY_DROPS, ",".join(labels)))
        return CLUSTER if selecting else FLAT
    if kind == "binary":
        lhs = judge(node["lhs"], records, found)
        rhs = judge(node["rhs"], records, found)
        m = node["matching"]
        if m is None:
            return lhs if lhs is not None else rhs
        labels = list(m["labels"])
        both = lhs == CLUSTER and rhs == CLUSTER
        keeps = True
        if m["on"]:
            keeps = CLUSTER_LABEL in labels
            if both and not keeps and IDENTITY_LABELS & set(labels):
                found.append((ON_DROPS, ",".join(labels)))
            if keeps and {lhs, rhs} == {CLUSTER, FLAT} and node["op"] != "or":
                found.append((ON_FLAT, ",".join(labels)))
        elif CLUSTER_LABEL in labels:
            keeps = False
            if both:
                found.append((IGNORING_CLUSTER, ",".join(labels)))
        if node["op"] in ("and", "unless"):
            return lhs
        if m["card"] not in ("one-to-one", "many-to-one", "one-to-many", "many-to-many"):
            raise CannotRun(f"promql-shape printed a matching cardinality {m['card']!r}")
        if (m["on"] and not keeps and node["op"] != "or" and {lhs, rhs} == {CLUSTER, FLAT}
                and IDENTITY_LABELS & set(labels)):
            many_side = {"many-to-one": lhs, "one-to-many": rhs}.get(m["card"])
            if many_side != CLUSTER:
                found.append((ONE_SIDE, ",".join(labels)))
        if node["op"] == "or":
            return CLUSTER if CLUSTER in (lhs, rhs) else FLAT
        many = rhs if m["card"] == "one-to-many" else lhs
        if m["card"] == "one-to-one" and not keeps:
            return FLAT  # the result keeps only the matched labels
        return many
    raise CannotRun(f"promql-shape printed a {kind!r} node this check does not judge")


def _rules(root: Path) -> "list[tuple[str, str, str, str, bool]]":
    """Every rule of the rule-pack tree: (file, group, name, expr, is a
    recording rule), in file order."""
    files = _lib_consumer_read.RULE_PACKS.files_under(root)
    results = _lib_consumer_read.prometheus_rule_file_results(files)
    _lib_consumer_read.require_read(results)
    out = []
    for path, rf in zip(files, results):
        doc = rf.documents[0] or {}
        for g in doc.get("groups") or []:
            for r in g.get("rules") or []:
                name = r.get("alert") or r.get("record")
                out.append((Path(path).name, str(g.get("name")), str(name), str(r.get("expr")),
                            "record" in r))
    return out


def _record_scopes(rules: list, trees: list) -> "dict[str, str]":
    """{recorded name: scope}. A recorded name's scope is its expression's;
    rules read each other, so settle to a fixed point (a name not settled yet
    reads as an exporter's)."""
    recorded = [(r[2], tree) for r, tree in zip(rules, trees) if r[4]]
    records: "dict[str, str]" = {}
    for _round in range(len(recorded) + 1):
        nxt = {}
        for name, tree in recorded:
            scope = judge(tree, records, [])
            # the same name recorded twice with different scopes: hold it to the contract
            nxt[name] = CLUSTER if nxt.get(name, scope) != scope else (scope or FLAT)
        if nxt == records:
            break
        records = nxt
    else:
        raise CannotRun("recorded-series scopes did not settle")
    return records


def rule_scopes(root: "Path | None" = None) -> "tuple[dict[str, str], dict[str, set[str]]]":
    """({recorded name: scope}, {alert name: the scopes of the rules of that
    name}) for the rule-pack tree — what a test needs to know which series
    and which alerts carry `da_cluster`."""
    rules = _rules(root or PROJECT_ROOT)
    trees = _lib_consumer_read.promql_shape([r[3] for r in rules])
    records = _record_scopes(rules, trees)
    alerts: "dict[str, set[str]]" = {}
    for (_f, _g, name, _e, is_record), tree in zip(rules, trees):
        if not is_record:
            alerts.setdefault(name, set()).add(judge(tree, records, []) or FLAT)
    return records, alerts


#: A comment that sets out to be a declaration; one that then does not read
#: as one is reported, not skipped.
_DECLARATION_START = re.compile(r"^\s*#\s*da_cluster\s*:\s*tenant-wide\b")
_DECLARATION = re.compile(
    r"^\s*#\s*da_cluster:\s*tenant-wide\s+(?P<kind>[a-z-]+)\s*\((?P<labels>[^)]*)\)\s*(?:[—-]+\s*(?P<why>.*))?$")
_RULE_LINE = re.compile(r"^\s*-\s+(?:alert|record):\s*(?P<name>\S+)\s*$")
#: What a rule may declare: looking across clusters. `on-cluster-with-flat-side`
#: and `one-side-needs-group` are rules that do not work, not rules that mean
#: something else — there is nothing to declare about them.
DECLARABLE = frozenset({BY_DROPS, ON_DROPS})


def declarations(root: "Path | None" = None) -> "tuple[dict[tuple[str, int], Counter], list[str]]":
    """({(file, n): Counter of (kind, labels)}, [malformed declaration, …]) —
    the `# da_cluster: tenant-wide …` comments of the tree (module docstring),
    each attached to the n-th rule of its file: the rule right below it, and
    not another rule of the same name. Read off the text: a comment is not in
    what the rule loader returns."""
    declared: "dict[tuple[str, int], Counter]" = {}
    malformed: "list[str]" = []
    for path in _lib_consumer_read.RULE_PACKS.files_under(root or PROJECT_ROOT):
        file, nth, pending = Path(path).name, -1, []
        for n, line in enumerate(Path(path).read_text(encoding="utf-8").split("\n"), 1):
            if _DECLARATION_START.match(line):
                m = _DECLARATION.match(line)
                if m is None:
                    malformed.append(f"{file}:{n}: not read as a declaration — write "
                                     "`# da_cluster: tenant-wide <kind> (<labels>) — <why>`")
                elif m["kind"] not in DECLARABLE:
                    malformed.append(f"{file}:{n}: `{m['kind']}` cannot be declared tenant-wide "
                                     f"(only {', '.join(sorted(DECLARABLE))})")
                elif not (m["why"] or "").strip():
                    malformed.append(f"{file}:{n}: a `da_cluster: tenant-wide` declaration "
                                     "needs a reason after the dash")
                else:
                    pending.append((n, m))
                continue
            r = _RULE_LINE.match(line)
            if r:
                nth += 1
                for _at, d in pending:
                    labels = ",".join(x.strip() for x in d["labels"].split(","))
                    declared.setdefault((file, nth), Counter())[(d["kind"], labels)] += 1
            if not line.strip().startswith("#"):
                if pending and not r:
                    malformed += [f"{file}:{at}: a `da_cluster: tenant-wide` declaration must sit "
                                  "in the comment block directly above its rule" for at, _d in pending]
                pending = []
    return declared, malformed


def _judged(root: Path) -> "list[tuple[str, int, str, str, Counter]]":
    """[(file, n, group, rule, Counter of (kind, labels))] — every rule of the
    tree with what it was found to do, declared or not; n is the rule's place
    in its file."""
    rules = _rules(root)
    if not rules:
        raise CannotRun("the rule-pack tree has no rules")
    try:
        trees = _lib_consumer_read.promql_shape([r[3] for r in rules])
    except _lib_consumer_read.InputRefused as e:
        raise CannotRun(f"Prometheus' parser refuses an expression: {e}") from e
    records = _record_scopes(rules, trees)
    out, place = [], Counter()
    for (file, group, name, _expr, _rec), tree in zip(rules, trees):
        found: "list[tuple[str, str]]" = []
        judge(tree, records, found)
        out.append((file, place[file], group, name, Counter(found)))
        place[file] += 1
    return out


def _declared_for(root: Path, judged: list) -> "tuple[dict[tuple[str, int], Counter], list[str]]":
    declared, malformed = declarations(root)
    rules_in = Counter(file for file, *_ in judged)
    for (file, nth) in declared:
        if nth >= rules_in[file]:
            raise CannotRun(f"{file}: the text has more rule lines than the loader read rules — "
                            "a declaration cannot be attached")
    return declared, malformed


def stale_declarations(root: "Path | None" = None) -> "list[str]":
    """Declarations that excuse nothing: malformed, or naming a finding the
    rule below them does not have (any more)."""
    root = root or PROJECT_ROOT
    judged = _judged(root)
    declared, malformed = _declared_for(root, judged)
    out = list(malformed)
    for file, nth, group, name, found in judged:
        for (kind, labels), n in sorted(declared.get((file, nth), Counter()).items()):
            if found[(kind, labels)] < n:
                out.append(f"{file}: {group} / {name}: {kind} ({labels}) is declared tenant-wide "
                           f"{n} time(s) and the rule has {found[(kind, labels)]} such finding(s) "
                           "— drop the declaration")
    return out


def findings(root: "Path | None" = None) -> "dict[str, list[str]]":
    """{file: [finding, …]} — a finding is `<group> / <rule>: <kind> (<labels>)`,
    repeated `×N` when one rule has the same one N times. A finding a rule
    declares as a deliberate tenant-wide read is left out, once per declaration."""
    root = root or PROJECT_ROOT
    judged = _judged(root)
    declared, _malformed = _declared_for(root, judged)
    # Counted per file, not per rule: two rules of one name in one group (a
    # warning and a critical alert) with the same finding are `×2`, so fixing
    # one of them changes the entry instead of hiding behind the other.
    counts: "dict[str, Counter]" = {}
    for file, nth, group, name, found in judged:
        for (kind, labels), n in (found - declared.get((file, nth), Counter())).items():
            counts.setdefault(file, Counter())[f"{group} / {name}: {kind} ({labels})"] += n
    return {f: sorted(t + (f" ×{n}" if n > 1 else "") for t, n in c.items())
            for f, c in sorted(counts.items())}


def per_target_jobs(root: "Path | None" = None) -> "set[str | None]":
    """Every `job` (None: no `job="…"` matcher) the tree's rules read `up` /
    `scrape_*` under."""
    jobs: "set[str | None]" = set()

    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get("kind") == "selector" and is_per_target(node["name"]):
            jobs.add(_job_of(node))
        for v in node.values():
            for child in (v if isinstance(v, list) else [v]):
                walk(child)

    for tree in _lib_consumer_read.promql_shape([r[3] for r in _rules(root or PROJECT_ROOT)]):
        walk(tree)
    return jobs


_BASELINE_HEADER = """\
# Rules that do not yet keep `da_cluster` (#4097, ADR-006 "one tenant in several
# clusters"). Managed by scripts/tools/lint/check_cluster_identity.py — a two-way
# ratchet: a finding not listed here fails, and so does a listed finding that is gone.
# Regenerate with `python3 scripts/tools/lint/check_cluster_identity.py --generate`
# (needs da-read: `make da-read-build`). Being listed is tracked debt, not an
# exemption; #4097 ends with this list empty.
"""


def load_baseline() -> "dict[str, list[str]]":
    if not BASELINE.exists():
        return {}
    data = strict_safe_load(BASELINE.read_text(encoding="utf-8")) or {}
    return {f: sorted(v or []) for f, v in (data.get("findings") or {}).items()}


def _flat(by_file: "dict[str, list[str]]") -> "set[tuple[str, str]]":
    return {(f, x) for f, xs in by_file.items() for x in xs}


def generate() -> int:
    now = findings()
    body = yaml.safe_dump({"findings": now}, allow_unicode=True, sort_keys=True,
                          default_flow_style=False, width=200)
    _lib_io.write_repo_artifact(BASELINE, _BASELINE_HEADER + body)
    print(f"check_cluster_identity: wrote {sum(map(len, now.values()))} finding(s) in "
          f"{len(now)} file(s) -> {BASELINE.relative_to(PROJECT_ROOT)}")
    return EXIT_OK


def check() -> int:
    now, base = _flat(findings()), _flat(load_baseline())
    new, gone = sorted(now - base), sorted(base - now)
    stale = stale_declarations()
    if stale:
        print("check_cluster_identity: `da_cluster: tenant-wide` declaration(s) that excuse nothing:")
        for x in stale:
            print(f"    ! {x}")
        return EXIT_VIOLATION
    if not new and not gone:
        print(f"check_cluster_identity: OK — {len(now)} finding(s), all baselined (== the tree).")
        return EXIT_OK
    rel = BASELINE.relative_to(PROJECT_ROOT)
    if new:
        print("check_cluster_identity: NEW rule(s) that drop the cluster label — with one tenant "
              "in two clusters these merge or mask each other (ADR-006). Add `da_cluster` to the "
              f"grouping / matching named below; do not add the finding to {rel}:")
        for f, x in new:
            print(f"    + {f}: {x}")
    if gone:
        print(f"check_cluster_identity: STALE entry(ies) in {rel} — fixed, renamed or removed. "
              "Drop them so the list equals the tree (--generate):")
        for f, x in gone:
            print(f"    - {f}: {x}")
    return EXIT_VIOLATION


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--generate", action="store_true", help="rewrite the baseline from the tree")
    ap.add_argument("--ci", action="store_true", help="accepted for hook symmetry")
    ap.add_argument("files", nargs="*", help="ignored (pre-commit passes changed files)")
    args = ap.parse_args()
    try:
        return generate() if args.generate else check()
    except (CannotRun, _lib_consumer_read.ConsumerReadError,
            _lib_consumer_read.InputRefused, _lib_io.OutputWriteError) as e:
        print(f"check_cluster_identity: cannot run: {e}", file=sys.stderr)
        return EXIT_CANNOT_RUN


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
