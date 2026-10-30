#!/usr/bin/env python3
"""The one inventory of the YAML fences in docs/**/*.md (ADR-036 §12, #3416).

Every gate that judges a documented YAML example reads it from here, and only
from here: `check_md_yaml_drift.py` (its three checks: fences, schema, crd),
`check_threshold_unit_sanity.py`, and the Go loader test
(components/threshold-exporter/app/pkg/config/docs_defaults_sample_test.go,
which runs this module's CLI). Extracting the fences, cutting a fence into
the files it documents, reading the fence's declaration, the exemption,
deciding which consumer reads a unit, and reading it the way that consumer
reads it happen in this module and nowhere else. Before, three programs had
four answers to "is this a conf.d example", and a block one of them judged
"not conf.d" was read by nobody.

A **unit** is one documented file: a fence cut at each column-0 comment that
names a YAML file (`# conf.d/_defaults.yaml`). For each unit the inventory
gives (path, line, consumer, reading or refusal(kind, detail), declaration,
exemption reason) and the errors found on it.

Declaring a fence (attr_list syntax, which mkdocs' superfences renders as an
ordinary yaml code block):

    ```yaml {.confd}
    ```yaml {.k8s}
    ```yaml {.illustrative reason="what this block shows, and why no gate judges it"}
    ```yaml {.confd .expect-refused=multiple_documents}
    ```yaml {.confd .expect-invalid}

The vocabulary is closed; an attribute or value outside it is an error, as
rustdoc's `invalid_codeblock_attributes`:

* consumer (at most one): `.confd` (da-guard parse-tree), `.k8s` (da-crdecode,
  kubectl's reading), `.helm-values` (da-crdecode: Helm reads values with the
  same sigs.k8s.io/yaml conversion), `.prom-rules` (da-read prometheus-rules),
  `.alertmanager` (da-read alertmanager), `.prometheus` (da-read prometheus).
  Every reading is an entry of `_lib_parse_tree` / `_lib_consumer_read`; this
  module reads no YAML itself.
* `.illustrative reason="…"`: the fence is judged by no gate. The reason is
  mandatory and non-empty, and every exemption is printed by every report.
  It takes no other attribute.
* `.expect-refused=<kind>` (with `.confd`): every unit of the fence must be
  refused by parse-tree with that problem kind; read, or refused with another
  kind, is an error. Only parse-tree names a kind; the other consumers'
  readings give a reason without one, so they take no `.expect-refused`.
* `.expect-invalid` (with `.confd`): a counter-example that reads but is
  wrong on purpose (rustdoc's `compile_fail`). The schema check must find at
  least one violation among the fence's units, and reports none of them;
  finding none is an error. A block that only shows a reading, and meets the
  schema, is `.illustrative`.

⛔ Written without braces (```` ```yaml confd ````) is an error: mkdocs does not
render that as a code block at all. The retired comment marker
(`<!-- md-yaml-drift: … -->`) is an error that names the attribute to write.

Which consumer reads a unit:

* declared: the declared one, whatever the text is (a top level that is not a
  mapping, a syntax error, several documents: all the consumer's to answer).
* undeclared: parse-tree reads it (the conf.d reader is the default reader of
  a ```yaml fence in this repo). Read: by structure — a conf.d example
  (`is_confd_example`) is `confd`; a mapping with a string `apiVersion` and
  `kind` is a Kubernetes object, read again by da-crdecode as `k8s`;
  anything else has no consumer and keeps parse-tree's reading. Refused with
  `top_level_not_mapping`: no conf.d file (a rule list, a shell line), not
  read and not reported. Refused with any other kind: an error that asks the
  author to declare the fence — the refusal says nothing about whether the
  example is right, only that the conf.d reader cannot tell.

CLI (`--json`) prints the inventory for a program that is not Python (the Go
loader test). Exit codes: 0, 2 when a reader could not answer.

Imports only what `_lib_parse_tree` / `_lib_consumer_read` import (their shared
libs pull PyYAML in; nothing here reads YAML with it), so the Go test runs it
with the python3 its CI leg already has.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import _lib_consumer_read  # noqa: E402
import _lib_parse_tree  # noqa: E402

# ── vocabulary ──────────────────────────────────────────────────────────────

#: consumer attribute → (consumer, the reading it names).
CONSUMERS = {
    "confd": "da-guard parse-tree",
    "k8s": "da-crdecode",
    "helm-values": "da-crdecode",
    "prom-rules": "da-read prometheus-rules",
    "alertmanager": "da-read alertmanager",
    "prometheus": "da-read prometheus",
}
ILLUSTRATIVE = "illustrative"
EXPECT_REFUSED = "expect-refused"
EXPECT_INVALID = "expect-invalid"

#: The opening line of a yaml fence (stripped): ```yaml / ```yml, then nothing,
#: or what follows after whitespace or `{`. Mirrored by yamlFenceRe in the Go
#: loader test; the parity test holds the two to the same units.
FENCE_RE = re.compile(r"^```(?:yaml|yml)(?=$|[\s{])(.*)$")
FENCE_CLOSE = "```"

#: A file-header comment naming a YAML file. Anchored at column 0: an indented
#: `#` that mentions a filename in passing is prose, and cutting there would
#: sever a block mid-mapping. Mirrored by fileHeaderRe in the Go loader test.
FILE_HEADER_RE = re.compile(r"^#.*\.ya?ml\b")

#: The retired exemption marker (`<!-- md-yaml-drift: ignore — … -->`).
RETIRED_MARKER_RE = re.compile(r"<!--\s*md-yaml-drift\s*:")

_ATTR_TOKEN_RE = re.compile(
    r'\s*(?:'
    r'\.(?P<cls>[A-Za-z][\w-]*)(?:=(?P<clsval>[^\s"}]*))?'
    r'|(?P<key>[A-Za-z][\w-]*)=(?:"(?P<qval>(?:[^"\\]|\\.)*)"|(?P<val>[^\s"}]+))'
    r')')

# ── what a conf.d example is (the one copy of this judgement) ──────────────

#: A block carrying any of these top-level keys is a PLATFORM file
#: (`_defaults.yaml`), judged by platform-defaults.schema.json — which also
#: permits `tenants:`, so a doc that stacks L0 + a tenant block in one file
#: still routes correctly.
PLATFORM_KEYS = frozenset({
    "defaults", "state_filters", "optional_overrides", "profiles",
    "max_metrics_per_tenant", "_routing_defaults", "_routing_enforced",
})

#: Keys that only appear INSIDE a tenant's body, so a block carrying one is a
#: tenant snippet even without a `tenants:` wrapper.
TENANT_CONFIG_MARKERS = frozenset({
    "_thresholds", "_routing", "_silent_mode", "_state_maintenance",
    "_routing_defaults", "_routing_enforced", "_threshold_mode",
    "_write_mode", "_write_back_mode",
})


def is_confd_example(data: Any) -> bool:
    """Is this reading a Vibe config example (platform file or tenant snippet)?

    A platform key at the top (`defaults:` / `state_filters:` / …), a bare
    top-level `tenants:`, a tenant marker at the top, or a tenant marker one
    level down (a tenant body with `tenants:` omitted in the snippet). Asked
    only of an undeclared unit parse-tree read; a declared `.confd` unit is
    conf.d whatever its keys are.
    """
    if not isinstance(data, dict):
        return False
    if "tenants" in data:
        return True
    for key in data:
        if key in TENANT_CONFIG_MARKERS or key in PLATFORM_KEYS:
            return True
    for value in data.values():
        if isinstance(value, dict) and any(k in TENANT_CONFIG_MARKERS for k in value):
            return True
    return False


def confd_kind(data: Any) -> "str | None":
    """Which contract a conf.d reading is governed by: ``platform`` (a
    platform key at the top: platform-defaults.schema.json), ``tenant-file``
    (`tenants:` at the top), ``fragment`` (a bare tenant body, judged as one
    tenant's). None for a reading that is not a mapping."""
    if not isinstance(data, dict):
        return None
    keys = set(data)
    if keys & PLATFORM_KEYS:
        return "platform"
    if "tenants" in keys:
        return "tenant-file"
    return "fragment"


def _is_k8s_object(data: Any) -> bool:
    return (isinstance(data, dict) and isinstance(data.get("apiVersion"), str)
            and isinstance(data.get("kind"), str))


# ── extraction ──────────────────────────────────────────────────────────────

@dataclass
class Declaration:
    """A fence's `{…}` attributes, read. `errors` holds every problem with
    them; a declaration with errors decides nothing (its units are not read)."""
    raw: str
    consumer: "str | None" = None
    illustrative: "str | None" = None
    expect_refused: "str | None" = None
    expect_invalid: bool = False
    errors: "list[str]" = field(default_factory=list)


@dataclass
class Fence:
    path: str          # repo-relative, '/'
    line: int          # 1-based line of the opening fence
    info: str          # what follows ```yaml on the opening line, stripped
    body: "list[str]"  # the lines inside the fence, as written


@dataclass
class Unit:
    """One documented file of one fence, and what the inventory knows of it."""
    path: str
    line: int                    # first line of the unit (fence line + offset + 1)
    fence_line: int
    text: str
    declaration: "str | None"    # the fence's raw info text, None for a bare fence
    consumer: "str | None" = None
    how: "str | None" = None     # "declared" | "structure" | None
    exemption: "str | None" = None
    expect_refused: "str | None" = None
    expect_invalid: bool = False
    documents: "list[Any] | None" = None   # the consumer's reading, one entry per document
    node: Any = None             # parse-tree's tagged node (conf.d units)
    refusal: "tuple[str | None, str] | None" = None   # (kind, detail)
    errors: "list[str]" = field(default_factory=list)

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"

    @property
    def read(self) -> bool:
        return self.documents is not None

    @property
    def confd_kind(self) -> "str | None":
        if self.consumer != "confd" or not self.read:
            return None
        return confd_kind(self.documents[0])


def parse_declaration(info: str) -> "Declaration | None":
    """The fence's attributes, or None for a bare ```yaml fence."""
    info = info.strip()
    if not info:
        return None
    decl = Declaration(raw=info)
    if not (info.startswith("{") and info.endswith("}")):
        decl.errors.append(
            f"fence info {info!r} is not an attribute list: write ```yaml {{.<attr>}} — "
            "without braces mkdocs does not render the fence as a code block")
        return decl
    inner = info[1:-1]
    pos, reason_given = 0, None
    consumers: "list[str]" = []
    while pos < len(inner):
        if not inner[pos:].strip():
            break
        m = _ATTR_TOKEN_RE.match(inner, pos)
        if not m or m.end() == pos:
            decl.errors.append(f"cannot read attribute text {inner[pos:].strip()!r}")
            break
        pos = m.end()
        cls, clsval = m.group("cls"), m.group("clsval")
        if cls is not None:
            if cls in CONSUMERS and clsval is None:
                consumers.append(cls)
            elif cls == ILLUSTRATIVE and clsval is None:
                decl.illustrative = decl.illustrative or ""
            elif cls == EXPECT_REFUSED:
                if not clsval:
                    decl.errors.append(".expect-refused needs a kind: .expect-refused=<kind>")
                else:
                    decl.expect_refused = clsval
            elif cls == EXPECT_INVALID and clsval is None:
                decl.expect_invalid = True
            else:
                shown = f".{cls}" + (f"={clsval}" if clsval is not None else "")
                decl.errors.append(f"unknown attribute {shown!r} (known: "
                                   + ", ".join(f".{c}" for c in CONSUMERS)
                                   + f", .{ILLUSTRATIVE} reason=\"…\", .{EXPECT_REFUSED}=<kind>, "
                                   f".{EXPECT_INVALID})")
        else:
            key = m.group("key")
            val = m.group("qval") if m.group("qval") is not None else m.group("val")
            if key == "reason":
                reason_given = val
            else:
                decl.errors.append(f"unknown attribute {key}={val!r} (only .{ILLUSTRATIVE} "
                                   "takes a key=value: reason=\"…\")")
    if len(consumers) > 1:
        decl.errors.append(f"more than one consumer: {', '.join('.' + c for c in consumers)}")
    decl.consumer = consumers[0] if consumers else None
    if decl.illustrative is not None:
        if not (reason_given or "").strip():
            decl.errors.append(f".{ILLUSTRATIVE} needs a non-empty reason=\"…\"")
        else:
            decl.illustrative = reason_given.strip()
        if decl.consumer or decl.expect_refused or decl.expect_invalid:
            decl.errors.append(f".{ILLUSTRATIVE} takes no other attribute: no gate judges "
                               "the fence, so a consumer or an expectation means nothing")
    elif reason_given is not None:
        decl.errors.append(f"reason=… is only for .{ILLUSTRATIVE}")
    if decl.expect_refused and decl.consumer != "confd":
        decl.errors.append(
            f".{EXPECT_REFUSED} needs .confd: only parse-tree names the kind of a refusal "
            f"(got {('.' + decl.consumer) if decl.consumer else 'no consumer'})")
    if decl.expect_invalid and decl.consumer != "confd":
        decl.errors.append(f".{EXPECT_INVALID} needs .confd: the schema check judges conf.d "
                           "examples only")
    if decl.expect_invalid and decl.expect_refused:
        decl.errors.append(f".{EXPECT_INVALID} and .{EXPECT_REFUSED} exclude each other: a "
                           "refused file has nothing for the schema to judge")
    return decl


def extract_fences(path: str, content: str) -> "Iterator[Fence]":
    """Every ```yaml / ```yml fence of a Markdown text, in order. Lines are
    split on ``\\n`` only (a ``\\r`` stays in a body line, as Go's
    strings.Split leaves it); the fence lines are compared stripped. A fence
    with no line inside is no fence."""
    in_yaml, start, info, body = False, 0, "", []
    for i, line in enumerate(content.split("\n"), 1):
        stripped = line.strip()
        if not in_yaml:
            m = FENCE_RE.match(stripped)
            if m:
                in_yaml, start, info, body = True, i, m.group(1).strip(), []
        elif stripped == FENCE_CLOSE:
            in_yaml = False
            if body:
                yield Fence(path, start, info, body)
        else:
            body.append(line)


def split_documented_files(block_lines: "list[str]") -> "list[tuple[int, str]]":
    """Cut a fence that illustrates SEVERAL files into one segment per file.

    Several docs teach inheritance by stacking `# L0 _defaults.yaml`,
    `# L1 finance/_defaults.yaml` and a tenant file into ONE fence. Read
    whole, such a block repeats its top-level keys, which says nothing about
    whether the documented config is right.

    Returns (line offset, text) pairs (offset 0 is the fence's first body
    line). Mirrors splitConcatenatedFiles in the Go loader test;
    tests/shared/docs_yaml_split_matrix.json holds both to the same cuts.
    """
    segments: "list[tuple[int, str]]" = []
    current: "list[str]" = []
    start = 0
    for i, line in enumerate(block_lines):
        if FILE_HEADER_RE.match(line) and current:
            segments.append((start, "\n".join(current)))
            current, start = [], i
        current.append(line)
    if current:
        segments.append((start, "\n".join(current)))
    return segments


def iter_markdown(repo_root: Path) -> "Iterator[tuple[str, str]]":
    """(repo path, text) of every docs/**/*.md, sorted."""
    docs = repo_root / "docs"
    for md in sorted(docs.rglob("*.md")):
        rel = md.relative_to(repo_root).as_posix()
        yield rel, md.read_text(encoding="utf-8", errors="replace")


# ── reading ─────────────────────────────────────────────────────────────────

def read_confd_texts(texts: "list[str]") -> "list[tuple[Any, Any, tuple[str, str] | None]]":
    """Each text read as a conf.d file is read: ``(value, node, None)``, or
    ``(None, None, (kind, message))`` for one parse-tree refuses.

    One `da-guard parse-tree --inputs -` run over an empty scratch tree, each
    text as its own tenant file there (ADR-036 §2: Go is the only YAML parser
    of conf.d). Raises `_lib_parse_tree.ParseTreeError` (BinaryMissing
    included) when da-guard cannot answer: the run is then unusable whole.
    """
    if not texts:
        return []
    names = [f"unit{i:06d}.yaml" for i in range(len(texts))]
    with tempfile.TemporaryDirectory(prefix="doc-yaml-inventory-") as root:
        lines = _lib_parse_tree.run_files_raw(
            root, [(n, t.encode("utf-8")) for n, t in zip(names, texts)])
    out: "list[tuple[Any, Any, tuple[str, str] | None]]" = []
    for name, raw in zip(names, lines):
        tree = _lib_parse_tree.tree_of(_lib_parse_tree.file_document(raw))
        try:
            node = tree.node(name)
            out.append((_lib_parse_tree.to_python(node), node, None))
        except _lib_parse_tree.FileUnreadable as e:
            out.append((None, None, (e.kind, e.message)))
    return out


def _refusal_detail(e: _lib_consumer_read.ConsumerReadError) -> str:
    return " | ".join(e.stderr_lines) or str(e)


def _read_other(units: "list[Unit]", consumers: "frozenset[str]") -> None:
    """Read every unit whose consumer is not conf.d with that consumer.
    A consumer outside *consumers* is not read (the CLI's ``--read``)."""
    by: "dict[str, list[Unit]]" = {}
    for u in units:
        if u.consumer and u.consumer != "confd" and not u.errors and u.consumer in consumers:
            by.setdefault(u.consumer, []).append(u)
    k8s = by.get("k8s", []) + by.get("helm-values", [])
    if k8s:
        readings = _lib_consumer_read.kubernetes_text_documents_each(
            [u.text for u in k8s], keep_refusals=True)
        for u, r in zip(k8s, readings):
            if isinstance(r, _lib_consumer_read.ConsumerReadError):
                u.refusal = (None, _refusal_detail(r))
            else:
                u.documents = list(r)
    rules = by.get("prom-rules", [])
    if rules:
        results = _lib_consumer_read.prometheus_rule_text_results(
            [(u.where, u.text) for u in rules])
        for u, r in zip(rules, results):
            if r.problem is not None:
                u.refusal = (None, r.problem)
            else:
                u.documents = list(r.documents)
    for consumer, read in (("alertmanager", _lib_consumer_read.alertmanager_config),
                           ("prometheus", _lib_consumer_read.prometheus_config)):
        for u in by.get(consumer, []):
            try:
                u.documents = [read(u.text)]
            except _lib_consumer_read.InputRefused as e:
                u.refusal = (None, _refusal_detail(e))


def _judge_refusals(u: Unit) -> None:
    """The errors a unit's reading (or refusal) makes, once read."""
    if u.exemption is not None or u.errors:
        return
    if u.how == "declared":
        if u.expect_refused is not None:
            if u.refusal is None:
                u.errors.append(f"declared .{EXPECT_REFUSED}={u.expect_refused}, but "
                                f"{CONSUMERS[u.consumer]} reads it")
            elif u.refusal[0] != u.expect_refused:
                u.errors.append(f"declared .{EXPECT_REFUSED}={u.expect_refused}, but "
                                f"{CONSUMERS[u.consumer]} refuses it as {u.refusal[0]}: "
                                f"{u.refusal[1]}")
        elif u.refusal is not None:
            kind = f" ({u.refusal[0]})" if u.refusal[0] else ""
            u.errors.append(f"declared .{u.consumer}, and {CONSUMERS[u.consumer]} refuses "
                            f"it{kind}: {u.refusal[1]}")
    elif u.consumer == "k8s" and u.refusal is not None:
        u.errors.append("a Kubernetes object (apiVersion / kind) that kubectl's reading "
                        f"(da-crdecode) refuses: {u.refusal[1]}")


def build(repo_root: "Path | str", *,
          consumers: "Iterable[str] | None" = None) -> "Inventory":
    """The inventory of *repo_root*/docs/**/*.md.

    *consumers* limits which non-conf.d consumers are run (default: all); a
    unit of one left out keeps no reading and no refusal. parse-tree always
    runs: it is the reader of every undeclared unit. Raises
    `_lib_parse_tree.ParseTreeError` / `_lib_consumer_read.ConsumerReadError`
    when a reader cannot answer (no binary, a crash): no partial inventory.
    """
    root = Path(repo_root).resolve()
    wanted = frozenset(CONSUMERS if consumers is None else consumers)
    inv = Inventory()
    for rel, content in iter_markdown(root):
        for i, line in enumerate(content.split("\n"), 1):
            if RETIRED_MARKER_RE.search(line):
                inv.file_errors.append((
                    rel, i, "the `md-yaml-drift:` comment marker is retired (#3416): declare "
                    "the fence instead — ```yaml {.illustrative reason=\"…\"}, "
                    "{.confd .expect-refused=<kind>} or {.confd .expect-invalid}"))
        for fence in extract_fences(rel, content):
            decl = parse_declaration(fence.info)
            for offset, text in split_documented_files(fence.body):
                # `fence.line` is the ```yaml line itself; +1 lands on the
                # segment's first real line (the Go test's `b.line + s.offset + 1`).
                u = Unit(path=rel, line=fence.line + offset + 1, fence_line=fence.line,
                         text=text, declaration=decl.raw if decl else None)
                if decl is not None:
                    u.errors.extend(decl.errors)
                    if not decl.errors:
                        u.exemption = decl.illustrative
                        u.consumer = decl.consumer
                        u.how = "declared" if decl.consumer else None
                        u.expect_refused = decl.expect_refused
                        u.expect_invalid = decl.expect_invalid
                inv.units.append(u)

    by_parse_tree = [u for u in inv.units
                     if not u.errors and u.exemption is None and u.consumer in (None, "confd")]
    for u, (value, node, problem) in zip(
            by_parse_tree, read_confd_texts([u.text for u in by_parse_tree])):
        if u.how == "declared":
            if problem is None:
                u.documents, u.node = [value], node
            else:
                u.refusal = problem
            continue
        if problem is not None:
            if problem[0] != "top_level_not_mapping":
                u.refusal = problem
                u.errors.append(
                    f"no consumer declared, and the conf.d reader (parse-tree) refuses it "
                    f"({problem[0]}: {problem[1]}). Fix the text; or declare the fence "
                    "with its consumer (```yaml {.k8s} …), {.confd .expect-refused="
                    f"{problem[0]}}} for a refusal shown on purpose, or "
                    "{.illustrative reason=\"…\"}; or re-tag a fence that is not YAML "
                    "(```text for a directory tree)")
            continue
        if is_confd_example(value):
            u.consumer, u.how, u.documents, u.node = "confd", "structure", [value], node
        elif _is_k8s_object(value):
            u.consumer, u.how = "k8s", "structure"
        else:
            u.documents, u.node = [value], node   # no consumer: parse-tree's reading
    _read_other(inv.units, wanted)
    for u in inv.units:
        _judge_refusals(u)
    return inv


@dataclass
class Inventory:
    units: "list[Unit]" = field(default_factory=list)
    #: (path, line, message): errors on a docs line that is no unit (a retired marker).
    file_errors: "list[tuple[str, int, str]]" = field(default_factory=list)

    def errors(self) -> "list[tuple[str, int, str]]":
        """Every error, (path, line, message), in file order."""
        out = list(self.file_errors)
        for u in self.units:
            out.extend((u.path, u.line, e) for e in u.errors)
        return sorted(out)

    def exemptions(self) -> "list[Unit]":
        return [u for u in self.units if u.exemption is not None]

    def judged(self, consumer: "str | None" = ...) -> "list[Unit]":
        """Units with a reading and no error or exemption — what a gate
        judges; of *consumer* when it is given (None: no consumer)."""
        return [u for u in self.units
                if u.read and not u.errors and u.exemption is None
                and (consumer is ... or u.consumer == consumer)]


def print_exemptions(inv: Inventory) -> None:
    """Name every exempted unit: a gate that drops inputs silently reads as
    'covered everything' when it did not."""
    ex = inv.exemptions()
    if not ex:
        return
    print(f"Units exempted (.{ILLUSTRATIVE}): {len(ex)}")
    for u in ex:
        print(f"    {u.where} — {u.exemption}")


def as_json(inv: Inventory) -> dict:
    """The inventory as JSON (no readings: the consumer, the refusal kind and
    the conf.d contract are what a program outside Python needs)."""
    return {
        "units": [{
            "path": u.path, "line": u.line, "text": u.text,
            "declaration": u.declaration, "consumer": u.consumer, "how": u.how,
            "exemption": u.exemption, "expect_refused": u.expect_refused,
            "expect_invalid": u.expect_invalid, "read": u.read,
            "refusal_kind": u.refusal[0] if u.refusal else None,
            "confd_kind": u.confd_kind, "errors": u.errors,
        } for u in inv.units],
        "file_errors": [{"path": p, "line": ln, "error": e} for p, ln, e in inv.file_errors],
    }


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="The inventory of the ```yaml examples in docs/**/*.md (#3416).")
    parser.add_argument("--repo-root", default=".", help="repository root (default: .)")
    parser.add_argument("--json", action="store_true", help="print the inventory as JSON")
    parser.add_argument(
        "--read", default=",".join(CONSUMERS),
        help="comma-separated consumers to run besides parse-tree (default: all); "
             "a unit of one left out has no reading")
    args = parser.parse_args(argv)
    consumers = [c for c in args.read.split(",") if c]
    unknown = sorted(set(consumers) - set(CONSUMERS))
    if unknown:
        print(f"ERROR: unknown consumer(s) for --read: {', '.join(unknown)}", file=sys.stderr)
        return 2
    try:
        inv = build(args.repo_root, consumers=consumers)
    except (_lib_parse_tree.ParseTreeError, _lib_consumer_read.ConsumerReadError) as e:
        print(f"ERROR: a reader of the documented YAML examples did not answer: {e}",
              file=sys.stderr)
        return 2
    if args.json:
        json.dump(as_json(inv), sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        for path, line, err in inv.errors():
            print(f"{path}:{line}: {err}")
        print_exemptions(inv)
        print(f"{len(inv.units)} unit(s), {len(inv.errors())} error(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
