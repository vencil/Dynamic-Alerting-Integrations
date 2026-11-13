#!/usr/bin/env python3
"""The one inventory of the YAML code blocks in docs/**/*.md (ADR-036 §12, #3416).

Every gate that judges a documented YAML example reads it from here, and only
from here: `check_md_yaml_drift.py` (its three checks: fences, schema, crd),
`check_threshold_unit_sanity.py`, and the Go loader test
(components/threshold-exporter/app/pkg/config/docs_defaults_sample_test.go,
which runs this module's CLI). Finding the code blocks, cutting a block into
the files it documents, reading the block's declaration, the exemption,
deciding which consumer reads a unit, and reading it the way that consumer
reads it happen in this module and nowhere else. Before, three programs had
four answers to "is this a conf.d example", and a block one of them judged
"not conf.d" was read by nobody.

Which code blocks exist is the site's renderer's answer, not this module's
(#3814): the gate reads the docs with the reader the site reads them with. Each docs/**/*.md is
rendered with Python-Markdown and mkdocs.yml's `markdown_extensions` (read as
mkdocs reads them, without the theme or the plugins; `Renderer`), and every
fenced block pymdownx.superfences formats is recorded with its language,
classes and attributes. A block is YAML when the highlighter resolves its
language to pygments' YAML lexer (`is_yaml_language`: ```` ```yaml ````,
``~~~yml``, ```` ```YAML ````, ```` ``` {.yaml} ````; not ```` ```yaml+jinja ````,
another lexer, and not an untyped fence or an indented code block). Wherever
the site renders it — in a list item, an admonition, a tab, a blockquote at
any depth — it is a unit; what the site does not render as a code block (a
``yaml confd`` info without braces, a fence never closed) is none. Nested
shapes the hand-written patterns used to find, refuse or miss are the
renderer's to decide, so no gate and the site disagree on them any more.

⚠️ One deliberate difference from the site's build: pymdownx.snippets is not
expanded (`UNEXPANDED_EXTENSIONS`). A page that includes `docs/includes/*.md`
does not carry the included blocks; those files are under docs/ and are
inventoried on their own, once.

Where a block is (`locate_blocks`): the renderer gives a block's text, not
its line, so the text is matched against the file's lines, each without its
container prefix (indentation and `>`). Blocks of identical text share their
positions as a multiset, in file order. A YAML block found nowhere, a text at
more places than the site renders it, or identical text under different
fence info is an error (fail closed), never a guess. The unit's text is the
file's lines as written (a list item's indentation kept, as before #3814),
or, inside a blockquote, the renderer's text (no `>`).

A **unit** is one documented file: a block cut at each column-0 comment that
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
  module reads no YAML itself. A repeated key is the declared consumer's to
  answer: parse-tree refuses it, kubectl / Helm (`.k8s` / `.helm-values`)
  keep the last one. So every document of a `.k8s` unit must be a Kubernetes
  object (a mapping with a string `apiVersion` and `kind`), or `.k8s` would
  hide anything from the conf.d reader.
* `.illustrative reason="…"`: the fence is judged by no gate. The reason is
  mandatory and non-empty, and every exemption is printed by every report.
  It takes no other attribute.
* `.expect-refused=<kind>` (with `.confd`): every unit of the fence must be
  refused by parse-tree with that problem kind; read, or refused with another
  kind, is an error. Only parse-tree names a kind; the other consumers'
  readings give a reason without one, so they take no `.expect-refused`.
* `.expect-invalid` (with `.confd`): a counter-example that reads but is
  wrong on purpose (rustdoc's `compile_fail`). The schema check must find at
  least one violation in EVERY unit of the fence, and reports none of them; a
  unit with none is an error (a correct file shown beside the counter-example
  goes in a fence of its own). `.expect-invalid` absorbs schema errors
  only: the route generator's credential judgement is always reported,
  whatever the fence declares (#3748). A block that only shows a reading, and meets
  the schema, is `.illustrative`. Its values are still read by the
  unit-sanity lint: a reader copies them as they are.

The attributes are the renderer's reading of the `{…}` (pymdownx.superfences
with attr_list): `{.k8s}`, `{.yaml .k8s}` and ``~~~yaml {.k8s}`` declare the
same. An `{#id}` is an error; the highlighter's own options (`title="…"`,
`hl_lines`, `linenums`) are not attributes. The retired comment marker
(`<!-- md-yaml-drift: … -->`) is an error that names the attribute to write.

Which consumer reads a unit:

* declared: the declared one, whatever the text is (a top level that is not a
  mapping, a syntax error, several documents: all the consumer's to answer).
* undeclared: parse-tree reads it (the conf.d reader is the default reader of
  a yaml code block in this repo). Read: by structure — a conf.d example
  (`is_confd_example`) is `confd`; a mapping with a string `apiVersion` and
  `kind` is a Kubernetes object, read again by da-crdecode as `k8s`;
  anything else has no consumer and keeps parse-tree's reading. Refused with
  `top_level_not_mapping`: no conf.d file (a rule list, a shell line), not
  read and not reported. Refused with any other kind: an error that asks the
  author to declare the fence — the refusal says nothing about whether the
  example is right, only that the conf.d reader cannot tell.

A `{.helm-values}` unit's `thresholdConfig` is the conf.d the
threshold-exporter chart renders from it (`helm_values_confd`, held to the
chart's template by a test): the schema check judges those files as conf.d
(#3748).

CLI (`--json`) prints the inventory for a program that is not Python (the Go
loader test). Exit codes: 0, 2 when a reader could not answer.

Besides what `_lib_parse_tree` / `_lib_consumer_read` import (PyYAML; nothing
here reads YAML with it), the renderer needs markdown, pymdown-extensions,
mkdocs and pygments at requirements/ci-constraints.txt's versions: every hook
and CI job that builds the inventory installs them.
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
import _lib_exitcodes  # noqa: E402  (#3216: the one JSON encoder)
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

#: A file-header comment: a column-0 `#` whose comment STARTS with the file's
#: name, then the end of the line or a blank and any note
#: (`# conf.d/db-a.yaml (edge-asia-1 tenant)`, `# _defaults.yaml — Mode A`).
#: A comment that only mentions a file (`# see conf.d/_defaults.yaml`,
#: `# Level 2: conf.d/finance/_defaults.yaml`) is prose: cutting there split
#: one file in two, and a key repeated across the cut was read by nobody
#: (#3416 review). An indented `#` is prose too: anchored at column 0 on
#: purpose (an indented `# … _defaults.yaml 的預設` inside a `tenants:` block
#: would otherwise cut a mapping in two). Pinned by
#: tests/shared/docs_yaml_split_matrix.json.
FILE_HEADER_RE = re.compile(r"^# ?(?P<path>[\w./-]+\.ya?ml)(?:[ \t]+.*)?$", re.ASCII)

#: The retired exemption marker (`<!-- md-yaml-drift: ignore — … -->`).
RETIRED_MARKER_RE = re.compile(r"<!--\s*md-yaml-drift\s*:")

# ── what a conf.d example is (the one copy of this judgement) ──────────────

#: A block carrying any of these top-level keys is a PLATFORM file
#: (`_defaults.yaml`), judged by platform-defaults.schema.json — which also
#: permits `tenants:`, so a doc that stacks L0 + a tenant block in one file
#: still routes correctly.
PLATFORM_KEYS = frozenset({
    "defaults", "state_filters", "optional_overrides", "dimensions", "profiles",
    "max_metrics_per_tenant", "_routing_defaults", "_routing_enforced",
})

#: Keys that only appear INSIDE a tenant's body, so a block carrying one is a
#: tenant snippet even without a `tenants:` wrapper.
TENANT_CONFIG_MARKERS = frozenset({
    "_thresholds", "_routing", "_silent_mode", "_state_maintenance",
    "_routing_defaults", "_routing_enforced", "_threshold_mode",
    "_write_mode", "_write_back_mode",
})


def _is_routing_profiles(data: Any) -> bool:
    """A `_routing_profiles.yaml`: a `routing_profiles:` key at the top. The
    route generator reads that key whatever else the file holds."""
    return isinstance(data, dict) and "routing_profiles" in data


def is_confd_example(data: Any) -> bool:
    """Is this reading a Vibe config example (platform file or tenant snippet)?

    A platform key at the top (`defaults:` / `state_filters:` / …), a bare
    top-level `tenants:`, a tenant marker at the top, or a tenant marker one
    level down (a tenant body with `tenants:` omitted in the snippet), or a
    `_routing_profiles.yaml` (`_is_routing_profiles`). Asked
    only of an undeclared unit parse-tree read; a declared `.confd` unit is
    conf.d whatever its keys are.
    """
    if not isinstance(data, dict):
        return False
    if "tenants" in data or _is_routing_profiles(data):
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
    platform key at the top: platform-defaults.schema.json),
    ``routing-profiles`` (`_is_routing_profiles`), ``tenant-file``
    (`tenants:` at the top), ``fragment`` (a bare tenant body, judged as one
    tenant's). None for a reading that is not a mapping."""
    if not isinstance(data, dict):
        return None
    keys = set(data)
    if keys & PLATFORM_KEYS:
        return "platform"
    if _is_routing_profiles(data):
        return "routing-profiles"
    if "tenants" in keys:
        return "tenant-file"
    return "fragment"


def _is_k8s_object(data: Any) -> bool:
    return (isinstance(data, dict) and isinstance(data.get("apiVersion"), str)
            and isinstance(data.get("kind"), str))


# ── the renderer: which code blocks exist (#3814) ───────────────────────────

#: The site's configuration: mkdocs.yml at the root being inventoried, or, for
#: a root without one (a test's scratch tree), this checkout's.
MKDOCS_YML = "mkdocs.yml"

#: Extensions of mkdocs.yml the inventory does not load — the one deliberate
#: difference from the site's build (module docstring).
UNEXPANDED_EXTENSIONS = frozenset({"pymdownx.snippets"})

#: The class of the inventory's catch-all fence (`name: '*'`): the renderer
#: adds it to every block that fence formats; not an attribute the author wrote.
_COLLECTOR_CLASS = "doc-yaml-inventory"

#: A container's prefix at the start of a line: list / admonition / tab
#: indentation and blockquote `>` markers, at any depth. What a rendered
#: block's lines and the file's lines are matched without (`locate_blocks`).
_CONTAINER_PREFIX_RE = re.compile(r"^[ \t>]*")

#: The fence characters at the start of a line, once the container prefix is
#: off. Only a boundary for `locate_blocks` (a candidate position sits between
#: two such lines) and for reading back the declaration's text: whether a
#: line opens a code block is the renderer's to say.
_FENCE_RUN_RE = re.compile(r"^(?:`{3,}|~{3,})")


@dataclass
class CodeBlock:
    """One fenced code block as the site's renderer reads it."""
    language: "str | None"
    classes: "list[str]"        # `{.class}` attributes the author wrote, in order
    attrs: "dict[str, str]"     # `key=value` and `.class=value` attributes
    id_value: str               # `{#id}`
    options: "dict[str, Any]"   # the highlighter's own options (`title`, `hl_lines`, `linenums`)
    source: str                 # the code as the renderer gives it (container prefixes removed)

    @property
    def is_yaml(self) -> bool:
        return is_yaml_language(self.language)

    @property
    def declared(self) -> bool:
        """Did the author write an attribute (the highlighter's options are not one)?"""
        return bool(self.classes or self.attrs or self.id_value)

    def same_fence(self, other: "CodeBlock") -> bool:
        return (self.language, self.classes, self.attrs, self.id_value, self.options) == (
            other.language, other.classes, other.attrs, other.id_value, other.options)


_YAML_LANGUAGE: "dict[str, bool]" = {}


def is_yaml_language(language: "str | None") -> bool:
    """Does the highlighter read *language* as YAML? The name resolves, as
    pymdownx.highlight resolves it (pygments' `get_lexer_by_name`, any case),
    to pygments' YAML lexer: ``yaml``, ``yml``, ``YAML``. Another lexer
    (``yaml+jinja``), an unknown name and no language are not YAML."""
    if not language:
        return False
    if language not in _YAML_LANGUAGE:
        try:
            from pygments.lexers import get_lexer_by_name
            from pygments.lexers.data import YamlLexer
            from pygments.util import ClassNotFound
        except ImportError as e:
            raise RendererError(_not_installed(e)) from e
        try:
            _YAML_LANGUAGE[language] = type(get_lexer_by_name(language)) is YamlLexer
        except ClassNotFound:
            _YAML_LANGUAGE[language] = False
    return _YAML_LANGUAGE[language]


class RendererError(Exception):
    """The site's renderer cannot be had (a package missing, a mkdocs.yml
    the inventory cannot read): nothing is inventoried then."""


def _not_installed(e: ImportError) -> str:
    return (f"the site's renderer is not installed ({e}): install markdown, "
            "pymdown-extensions, mkdocs and pygments with -c requirements/ci-constraints.txt")


class Renderer:
    """Python-Markdown with mkdocs.yml's `markdown_extensions` and their
    configs, read the way mkdocs reads them (`MarkdownExtensions`, the same
    built-ins), without the theme or the plugins; `UNEXPANDED_EXTENSIONS`
    left out. Every pymdownx.superfences fence — mkdocs.yml's custom fences
    and a catch-all `*` fence standing in for the default one, with the
    default one's validator — records the block it formats and formats it as
    before. One Markdown instance, reset between files."""

    def __init__(self, repo_root: "Path | str"):
        import copy

        try:
            import markdown
            from mkdocs.config.config_options import MarkdownExtensions
            from mkdocs.utils.yaml import yaml_load
            from pymdownx import superfences
            # The highlighter's lexers decide what is yaml (`is_yaml_language`):
            # asked here, so a missing pygments is this error before any file.
            import pygments.lexers  # noqa: F401
        except ImportError as e:
            raise RendererError(_not_installed(e)) from e

        config_path = Path(repo_root) / MKDOCS_YML
        if not config_path.is_file():
            config_path = Path(_HERE).parents[1] / MKDOCS_YML
        option = MarkdownExtensions(builtins=["toc", "tables", "fenced_code"],
                                    configkey="mdx_configs")
        option.key_name = "markdown_extensions"
        try:
            with open(config_path, encoding="utf-8") as fh:
                raw = yaml_load(fh) or {}
            validated = option.validate(raw.get("markdown_extensions") or [])
        except Exception as e:  # noqa: BLE001 — any failure: no renderer, no inventory
            raise RendererError(f"cannot read {config_path}'s markdown_extensions: {e}") from e
        extensions = [e for e in validated if e not in UNEXPANDED_EXTENSIONS]
        configs = {k: copy.deepcopy(v) for k, v in option.configdata.items()
                   if k not in UNEXPANDED_EXTENSIONS}
        if "pymdownx.superfences" not in extensions:
            raise RendererError(f"{config_path}: markdown_extensions has no pymdownx.superfences — "
                             "the inventory reads code blocks through its fences")
        self.blocks: "list[CodeBlock]" = []

        def recording(fmt):
            def format_(source, language, class_name, options, md, **kwargs):
                classes = list(kwargs.get("classes") or [])
                if classes and classes[0] == class_name:
                    classes = classes[1:]
                self.blocks.append(CodeBlock(
                    language=language or None, classes=classes,
                    attrs=dict(kwargs.get("attrs") or {}),
                    id_value=kwargs.get("id_value") or "",
                    options=dict(options or {}), source=source))
                return fmt(source, language, class_name, options, md, **kwargs)
            return format_

        fences = dict(configs.get("pymdownx.superfences") or {})
        fences["custom_fences"] = [
            {**f, "format": recording(f.get("format", superfences.fence_code_format))}
            for f in fences.get("custom_fences") or []] + [
            {"name": "*", "class": _COLLECTOR_CLASS,
             "format": recording(superfences.fence_code_format),
             "validator": superfences.highlight_validator}]
        configs["pymdownx.superfences"] = fences
        self.md = markdown.Markdown(extensions=extensions, extension_configs=configs)

    def code_blocks(self, content: str) -> "list[CodeBlock]":
        """Every fenced code block the site renders from *content*, any
        language, in the order the renderer formats them."""
        self.blocks = []
        self.md.reset()
        self.md.convert(content)
        return self.blocks


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
    info: str          # the opening line after its fence and language, stripped
    body: "list[str]"  # the lines inside the fence (module docstring: which text)
    block: CodeBlock   # the renderer's reading of it


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


def parse_declaration(block: CodeBlock, raw: str = "") -> "Declaration | None":
    """The fence's attributes as the renderer read them, or None for a fence
    that carries none (the highlighter's `title` / `hl_lines` / `linenums`
    are its own options, no declaration). *raw* is the attribute text as
    written, kept for messages and the JSON."""
    if not block.declared:
        return None
    decl = Declaration(raw=raw.strip() or "{}")
    # `.name` → (name, None); `.name=value` (the renderer keeps it as an
    # attribute named `.name`) → (name, value); `key=value` → key.
    tokens: "list[tuple[str, str | None, bool]]" = [(c, None, True) for c in block.classes]
    for k, v in block.attrs.items():
        tokens.append((k[1:], v, True) if k.startswith(".") else (k, v, False))
    reason_given = None
    consumers: "list[str]" = []
    if block.id_value:
        decl.errors.append(f"unknown attribute '#{block.id_value}' (an id names no consumer)")
    for name, value, is_class in tokens:
        if is_class:
            if name in CONSUMERS and value is None:
                consumers.append(name)
            elif name == ILLUSTRATIVE and value is None:
                decl.illustrative = decl.illustrative or ""
            elif name == EXPECT_REFUSED:
                if not value:
                    decl.errors.append(".expect-refused needs a kind: .expect-refused=<kind>")
                else:
                    decl.expect_refused = value
            elif name == EXPECT_INVALID and value is None:
                decl.expect_invalid = True
            else:
                shown = f".{name}" + (f"={value}" if value is not None else "")
                decl.errors.append(f"unknown attribute {shown!r} (known: "
                                   + ", ".join(f".{c}" for c in CONSUMERS)
                                   + f", .{ILLUSTRATIVE} reason=\"…\", .{EXPECT_REFUSED}=<kind>, "
                                   f".{EXPECT_INVALID})")
        elif name == "reason":
            reason_given = value
        else:
            decl.errors.append(f"unknown attribute {name}={value!r} (only .{ILLUSTRATIVE} "
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


def _bare(line: str, tab_length: int) -> str:
    """*line* as `locate_blocks` compares it: tabs expanded (as
    Python-Markdown expands them), a CRLF's CR and the container prefix off."""
    if line.endswith("\r"):
        line = line[:-1]
    return _CONTAINER_PREFIX_RE.sub("", line.expandtabs(tab_length), count=1)


def _info_text(opening: str) -> str:
    """The opening fence line's text after its fence and its language
    (```` ```yaml {.confd} ```` → ``{.confd}``; ```` ``` {.yaml .k8s} ```` →
    ``{.yaml .k8s}``), as written."""
    rest = _FENCE_RUN_RE.sub("", opening, count=1).strip(" \t")
    if rest and not rest.startswith("{"):
        word = re.match(r"[^\s{]*", rest).group(0)
        rest = rest[len(word):].strip(" \t")
    return rest


def locate_blocks(path: str, lines: "list[str]", blocks: "list[CodeBlock]", *,
                  tab_length: int = 4,
                  errors: "list[tuple[str, int, str]] | None" = None
                  ) -> "list[tuple[CodeBlock, int]]":
    """Where each YAML block of *blocks* (`Renderer.code_blocks` of the file
    whose lines are *lines*) sits: ``(block, i)``, *i* the 0-based index of
    its first line, in file order.

    A block is matched against the file by its lines, each without its
    container prefix (`_bare`): a candidate is a run of lines equal to the
    block's, between a line that starts with a fence and one that does. Blocks
    of identical text (any language) share their candidates as a multiset,
    in file order. Fail closed, as (path, line, message) on *errors*, never a
    guess: a YAML block with no candidate, more candidates than rendered
    blocks of that text (a copy the renderer does not render as code, so the
    inventory cannot tell which line is the rendered one), or blocks of that
    text with different fence info (which line holds which?). A block with
    no line of code (an empty fence) is no unit, as before.
    """
    errors = [] if errors is None else errors
    bare = [_bare(line, tab_length) for line in lines]
    starts: "dict[str, list[int]]" = {}
    for i, b in enumerate(bare):
        starts.setdefault(b, []).append(i)
    groups: "dict[tuple[str, ...], list[CodeBlock]]" = {}
    for block in blocks:
        if block.source:
            key = tuple(_bare(line, tab_length) for line in block.source.split("\n"))
            groups.setdefault(key, []).append(block)
    found: "list[tuple[CodeBlock, int]]" = []
    for key, group in groups.items():
        if not any(b.is_yaml for b in group):
            continue
        n = len(key)
        candidates = [i for i in starts.get(key[0], [])
                      if i >= 1 and _FENCE_RUN_RE.match(bare[i - 1])
                      and i + n < len(bare) and _FENCE_RUN_RE.match(bare[i + n])
                      and tuple(bare[i:i + n]) == key]
        first = key[0] if key[0] else (key[1] if n > 1 else "")
        if not candidates:
            errors.append((path, 1, (
                f"a yaml code block the site renders (its first line {first!r}) is found "
                "nowhere in the file's lines: the inventory cannot say where it is, so no "
                "gate would judge it. Report this (#3814): the inventory's reading of the "
                "file is wrong")))
            continue
        where = ", ".join(str(i + 1) for i in candidates)
        if len(candidates) != len(group):
            errors.append((path, candidates[0] + 1, (
                f"the text of a yaml code block is at {len(candidates)} places (lines "
                f"{where}) and the site renders {len(group)} code block(s) of it: the "
                "inventory cannot tell which line is the rendered one — make the copies "
                "differ, or drop the one that is not a code block")))
            continue
        if any(not b.same_fence(group[0]) for b in group[1:]):
            errors.append((path, candidates[0] + 1, (
                f"{len(group)} code blocks of the same text (lines {where}) carry different "
                "fence info: the inventory cannot tell which line holds which — make "
                "their text differ")))
            continue
        found.extend((b, i) for b, i in zip(group, candidates) if b.is_yaml)
    return sorted(found, key=lambda bi: bi[1])


def yaml_fences(path: str, content: str, renderer: Renderer,
                errors: "list[tuple[str, int, str]] | None" = None) -> "Iterator[Fence]":
    """Every YAML code block the site renders from a Markdown text, in file
    order: which blocks exist, their language and their attributes are the
    renderer's answer (`Renderer`), where they are is `locate_blocks`'.

    The body is the file's lines as written (a list item's indentation
    kept), or, for a block inside a blockquote, the renderer's text (no
    `>`). Problems locating a block go to *errors* as (path, line, message).
    """
    lines = content.split("\n")
    for block, i in locate_blocks(path, lines, renderer.code_blocks(content),
                                  tab_length=renderer.md.tab_length, errors=errors):
        n = block.source.count("\n") + 1
        opening = lines[i - 1].expandtabs(renderer.md.tab_length)
        prefix = _CONTAINER_PREFIX_RE.match(opening).group(0)
        body = block.source.split("\n") if ">" in prefix else lines[i:i + n]
        yield Fence(path, i, _info_text(opening[len(prefix):]), body, block)


def split_documented_files(block_lines: "list[str]") -> "list[tuple[int, str]]":
    """Cut a fence that illustrates SEVERAL files into one segment per file.

    Several docs teach inheritance by stacking `# L0 _defaults.yaml`,
    `# L1 finance/_defaults.yaml` and a tenant file into ONE fence. Read
    whole, such a block repeats its top-level keys, which says nothing about
    whether the documented config is right.

    Returns (line offset, text) pairs (offset 0 is the fence's first body
    line). Pinned by tests/shared/docs_yaml_split_matrix.json.
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
    """(repo path, text) of every docs/**/*.md, sorted. Read as bytes and
    decoded, never with `read_text`: its universal newlines would turn a lone
    CR into a line break, and the line numbers would no longer be the file's
    (lines are split on ``\\n`` only). Decoded as mkdocs decodes a page
    (`File.content_string`: ``utf-8-sig``), so a leading BOM is not text and
    a fence on line 1 is still a fence."""
    docs = repo_root / "docs"
    for md in sorted(docs.rglob("*.md")):
        rel = md.relative_to(repo_root).as_posix()
        yield rel, md.read_bytes().decode("utf-8-sig", errors="replace")


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
    # `.k8s` is a manifest, read as kubectl applies it (every document an
    # object, #3426); `.helm-values` is no manifest: the same conversion with
    # no object shape.
    for group, read_each in ((by.get("k8s", []), _lib_consumer_read.kubernetes_text_documents_each),
                             (by.get("helm-values", []),
                              _lib_consumer_read.any_yaml_text_documents_each)):
        if not group:
            continue
        readings = read_each([u.text for u in group], keep_refusals=True)
        for u, r in zip(group, readings):
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
    # `.k8s` says "Kubernetes objects": kubectl reads a repeated key as the
    # last one, so without this a block of anything (a conf.d example with a
    # repeated key) declared `.k8s` would pass every gate unjudged (#3416
    # review). Each document must be an object kubectl could apply.
    if u.consumer == "k8s" and u.read and not u.errors:
        bad = [i + 1 for i, d in enumerate(u.documents) if not _is_k8s_object(d)]
        if not u.documents:
            u.errors.append("declared .k8s, but kubectl's reading (da-crdecode) finds no "
                            "document in it")
        elif bad:
            u.errors.append(
                f"declared .k8s, but document(s) {bad} of kubectl's reading are not "
                "Kubernetes objects (a mapping with a string apiVersion and kind): "
                "declare the block's real consumer")


# ── the chart's conf.d (`{.helm-values}`) ───────────────────────────────────

#: helm/threshold-exporter/templates/configmap.yaml: the `thresholdConfig`
#: keys the chart writes as top-level keys of `_defaults.yaml`, each with when
#: it is written (`defaults` always; the three others when truthy;
#: `max_metrics_per_tenant` when not 0). `tenants.<t>` becomes `<t>.yaml`,
#: `tenants: {<t>: …}`; `existingConfigMap` set renders no ConfigMap at all.
#: tests/lint/test_doc_yaml_inventory.py reads the template and holds it to
#: this mapping.
HELM_DEFAULTS_FILE = "_defaults.yaml"
HELM_PLATFORM_KEYS = ("defaults", "optional_overrides", "dimensions", "state_filters",
                      "max_metrics_per_tenant")


def helm_values_confd(values: Any) -> "list[tuple[str, dict]]":
    """The conf.d files the threshold-exporter chart renders from Helm values
    *values* — (file name, content) — as far as *values* writes them: a key
    it leaves out is the chart's own `values.yaml`, not the documented
    example's. Empty when the chart renders no ConfigMap
    (`thresholdConfig.existingConfigMap`) or *values* has no `thresholdConfig`."""
    tc = values.get("thresholdConfig") if isinstance(values, dict) else None
    if not isinstance(tc, dict) or tc.get("existingConfigMap"):
        return []
    platform: "dict[str, Any]" = {}
    for key in HELM_PLATFORM_KEYS:
        value = tc.get(key)
        if key == "defaults" and key in tc:
            platform[key] = value
        elif key == "max_metrics_per_tenant" and value is not None and value != 0:
            platform[key] = value
        elif key not in ("defaults", "max_metrics_per_tenant") and value:
            platform[key] = value
    out = [(HELM_DEFAULTS_FILE, platform)] if platform else []
    tenants = tc.get("tenants")
    if isinstance(tenants, dict):
        out.extend((f"{t}.yaml", {"tenants": {t: body}}) for t, body in tenants.items())
    return out


def build(repo_root: "Path | str", *,
          consumers: "Iterable[str] | None" = None) -> "Inventory":
    """The inventory of *repo_root*/docs/**/*.md.

    *consumers* limits which non-conf.d consumers are run (default: all); a
    unit of one left out keeps no reading and no refusal. parse-tree always
    runs: it is the reader of every undeclared unit. Raises
    `_lib_parse_tree.ParseTreeError` / `_lib_consumer_read.ConsumerReadError`
    when a reader cannot answer (no binary, a crash), `RendererError` when the
    site's renderer cannot be had: no partial inventory.
    """
    root = Path(repo_root).resolve()
    wanted = frozenset(CONSUMERS if consumers is None else consumers)
    inv = Inventory()
    renderer = Renderer(root)
    for rel, content in iter_markdown(root):
        for i, line in enumerate(content.split("\n"), 1):
            if RETIRED_MARKER_RE.search(line):
                inv.file_errors.append((
                    rel, i, "the `md-yaml-drift:` comment marker is retired (#3416): declare "
                    "the fence instead — ```yaml {.illustrative reason=\"…\"}, "
                    "{.confd .expect-refused=<kind>} or {.confd .expect-invalid}"))
        for fence in yaml_fences(rel, content, renderer, inv.file_errors):
            decl = parse_declaration(fence.block, fence.info)
            for offset, text in split_documented_files(fence.body):
                # `fence.line` is the opening fence line itself; +1 lands on
                # the segment's first real line.
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

    def helm_confd(self) -> "list[tuple[Unit, str, dict]]":
        """(unit, file name, content) of every conf.d file the chart renders
        from a judged `{.helm-values}` unit (`helm_values_confd`)."""
        return [(u, fname, data) for u in self.judged("helm-values")
                for doc in u.documents for fname, data in helm_values_confd(doc)]

    def judged(self, consumer: "str | None" = ...) -> "list[Unit]":
        """Units with a reading and no error or exemption — what a gate
        judges; of *consumer* when it is given (None: no consumer)."""
        return [u for u in self.units
                if u.read and not u.errors and u.exemption is None
                and (consumer is ... or u.consumer == consumer)]


def print_exemptions(inv: "Inventory | list[tuple[str, str]]", file=None) -> None:
    """Name every exempted unit (or every (where, reason) pair given): a gate
    that drops inputs silently reads as 'covered everything' when it did not.
    Every gate that reads the inventory prints this."""
    ex = ([(u.where, u.exemption) for u in inv.exemptions()]
          if isinstance(inv, Inventory) else list(inv))
    if not ex:
        return
    print(f"Units exempted (.{ILLUSTRATIVE}): {len(ex)}", file=file)
    for where, reason in ex:
        print(f"    {where} — {reason}", file=file)


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
    except (_lib_parse_tree.ParseTreeError, _lib_consumer_read.ConsumerReadError,
            RendererError) as e:
        print(f"ERROR: a reader of the documented YAML examples did not answer: {e}",
              file=sys.stderr)
        return 2
    if args.json:
        # #3216: the document goes out through the one encoder.
        return _lib_exitcodes.Report(as_json(inv), 0)
    else:
        for path, line, err in inv.errors():
            print(f"{path}:{line}: {err}")
        print_exemptions(inv)
        print(f"{len(inv.units)} unit(s), {len(inv.errors())} error(s)")
    return 0


if __name__ == "__main__":
    _lib_exitcodes.run(main)
