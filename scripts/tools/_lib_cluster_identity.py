"""Which cluster a Prometheus says it is in — `global.external_labels.da_cluster`.

ADR-006 contract 1 and 7 (#4097): every Prometheus sets it, a single cluster
too. The one reading of that fact for every tool that points out a Prometheus
without it — `federation-check edge` and `byo-check prometheus` (the running
config, as `/api/v1/status/config` prints it), `onboard` (a config file,
already parsed by Prometheus' own loader) and the repo's scrape-config lint.

Also the one split of a query answer by that label (`rows_by_cluster`): the
rule packs record a tenant's series once per cluster, so a tool that replays
or measures one must do it cluster by cluster (ADR-006, #1200).

Standard library only, and no I/O: a tool that reads a file imports this
without becoming a tool that can use HTTP.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

#: ADR-006: the external label that says which cluster a Prometheus is in.
CLUSTER_IDENTITY_LABEL = "da_cluster"

#: What a check says about a Prometheus whose `da_cluster` has no value — one
#: wording for every tool that points it out.
CLUSTER_IDENTITY_UNSET = (
    f"external_labels.{CLUSTER_IDENTITY_LABEL} is not set — add it under "
    "global.external_labels with this cluster's id (ADR-006)")

class NotAConfigRendering(ValueError):
    """The text is not Prometheus' rendering of a configuration — nothing can
    be said about its external labels. "Could not read", never "not set"."""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _key_and_rest(line: str) -> "tuple[str, str]":
    """``(key, text after the colon)`` of the mapping line *line* as yaml
    prints one: a plain key runs to the first ``: `` (or the colon that ends
    the line), a quoted key (``'note: x': v``, ``"a b": v``) to its closing
    quote. Label names are UTF-8 in Prometheus 3 (``cluster.name``)."""
    s = line.strip()
    if s[:1] in ("'", '"'):
        quote, i, key = s[0], 1, []
        while i < len(s):
            c = s[i]
            if quote == "'" and s[i:i + 2] == "''":
                key.append("'")
                i += 2
            elif quote == '"' and c == "\\" and i + 1 < len(s):
                key.append(s[i + 1])
                i += 2
            elif c == quote:
                break
            else:
                key.append(c)
                i += 1
        after = s[i + 1:]
        if i < len(s) and (after == ":" or after.startswith(": ")):
            return "".join(key), after[1:].strip()
    else:
        at = s.find(": ")
        if at > 0:
            return s[:at], s[at + 2:].strip()
        if len(s) > 1 and s.endswith(":"):
            return s[:-1], ""
    raise NotAConfigRendering(f"not a `key: value` line: {s[:40]!r}")


def _scalar(text: str) -> str:
    """The value yaml printed on a key's own line, without its quotes."""
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1]
    return text


def external_labels(config_yaml: str) -> "dict[str, str] | None":
    """`global.external_labels` of a configuration as `/api/v1/status/config`
    prints it — the keys directly under that one mapping and nothing else in
    the file (a scrape job's `labels:` or `target_label:` is not it). None
    when the configuration has no such mapping.

    Raises :class:`NotAConfigRendering` when the text is not that rendering:
    Prometheus always prints a top-level ``global:`` section, so a text
    without one (an empty one among them) was not read, and is not a
    configuration without external labels.

    The text is Prometheus' own rendering (block style, one key a line), which
    is what makes reading it line by line safe; it is not a YAML reader for a
    hand-written file (that one is :func:`config_external_labels`, on the
    document Prometheus' loader parsed). Read as that rendering is laid out:

    - the mapping is the `external_labels:` key of the top-level `global:`
      section — the same words anywhere else (a label value among them) are
      not it;
    - its keys are exactly the lines at the indent of its first key. A line
      indented deeper continues the value above it (a multi-line value is
      printed as a block scalar) and is never read as a key, whatever it
      looks like;
    - line endings are normalised first.
    """
    lines = config_yaml.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "global:"), None)
    if start is None:
        raise NotAConfigRendering("no top-level `global:` section")
    section = []
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith(" "):
            break
        section.append(line)
    first = next((line for line in section if line.strip()), None)
    if first is None:
        return None
    key_indent = _indent(first)
    at = rest = None
    for i, line in enumerate(section):
        if line.strip() and _indent(line) == key_indent:
            key, text = _key_and_rest(line)
            if key == "external_labels":
                at, rest = i, text
                break
    if at is None:
        return None
    if rest:
        if rest == "{}":
            return {}
        raise NotAConfigRendering(f"external_labels is not a block mapping: {rest[:40]!r}")
    body = []
    for line in section[at + 1:]:
        if line.strip() and _indent(line) <= key_indent:
            break
        body.append(line)
    first = next((line for line in body if line.strip()), None)
    if first is None:
        return {}
    label_indent, out, block = _indent(first), {}, None
    for line in body:
        if not line.strip():
            continue
        if _indent(line) > label_indent:
            # a continuation line of the value above: never a key
            if block is not None:
                out[block] += ("\n" if out[block] else "") + line.strip()
            continue
        key, text = _key_and_rest(line)
        if text[:1] in ("|", ">"):
            out[key], block = "", key
        else:
            out[key], block = _scalar(text), None
    return out


def config_external_labels(config: "dict[str, Any]") -> "dict[str, Any] | None":
    """`global.external_labels` of a Prometheus configuration already parsed
    (``da-read prometheus``: only the keys the text writes). ``{}`` when the
    document has a `global` section that sets no external label; None when it
    has no `global` section at all — a `scrape_configs` fragment says nothing
    about the external labels of the Prometheus that loads it."""
    if "global" not in config:
        return None
    return (config.get("global") or {}).get("external_labels") or {}


def cluster_identity(labels: "dict[str, Any] | None") -> "str | None":
    """The cluster id among the external labels *labels* (either reader's
    answer), or None when `da_cluster` has no value — absent, empty or
    blank."""
    value = (labels or {}).get(CLUSTER_IDENTITY_LABEL)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def rows_by_cluster(rows: "list[dict[str, Any]]") -> "dict[str, list[dict[str, Any]]]":
    """The rows of a Prometheus query answer (`data.result`), grouped by
    their `da_cluster` label: ``{cluster: [row, ...]}``, the clusters in
    sorted order. A row whose `metric` has no `da_cluster` — or an empty one,
    which is the same thing to Prometheus — is in the group ``""``, a group
    of its own beside the named clusters.

    ADR-006 (#1200): a `tenant:*` recording rule has one series per (tenant,
    cluster) and its alert fires per cluster, so the samples of two groups
    are never one series. Nothing is merged here: what a group holding more
    than one row means is `repeated_rows`' to say (two series at once, or one
    series in succession) — never to add up."""
    groups: "dict[str, list[dict[str, Any]]]" = {}
    for row in rows:
        metric = row.get("metric") if isinstance(row, dict) else None
        value = metric.get(CLUSTER_IDENTITY_LABEL) if isinstance(metric, dict) else None
        groups.setdefault(value if isinstance(value, str) else "", []).append(row)
    return {cluster: groups[cluster] for cluster in sorted(groups)}


def in_time_order(rows: "list[Any]", span: Any) -> "list[Any]":
    """*rows* by the time of their first sample (`span(row)` is a row's
    ``(first, last)`` sample timestamps, None for a row with no sample,
    which sorts first)."""
    return sorted(rows, key=lambda row: (span(row) is not None, span(row) or (0, 0)))


def repeated_rows(groups: "dict[str, list[dict[str, Any]]]", span: Any) -> "dict[str, str]":
    """``{cluster: why}`` for every group of *groups* (`rows_by_cluster`)
    that cannot be read as one series: it holds rows whose samples OVERLAP
    in time (`span(row)`: a row's ``(first, last)`` sample timestamps, or
    None) — two series at once that the `da_cluster` label does not tell
    apart. Typically an HA pair read without deduplication (the rows differ
    in `replica`), not several clusters. *why* names the labels the rows
    differ in and what to do; it is never a reason to add the rows up or to
    pick one.

    Rows of one cluster whose time ranges do not overlap are not in the
    answer: they are ONE series in succession — a label that changed once
    inside the window (the pod that evaluates the rule was replaced) starts
    a new row where the old one ends — and the caller joins them in time
    order (`in_time_order`)."""
    out: "dict[str, str]" = {}
    for cluster, rows in groups.items():
        spans = sorted(s for s in (span(row) for row in rows) if s is not None)
        if (len(rows) < 2 or len(spans) == len(rows) and all(
                later[0] > earlier[1] for earlier, later in zip(spans, spans[1:]))):
            continue
        metrics = [row.get("metric") if isinstance(row.get("metric"), dict) else {}
                   for row in rows]
        names = sorted({name for metric in metrics for name in metric})
        differ = [name for name in names
                  if len({repr(metric.get(name)) for metric in metrics}) > 1]
        how = ("that differ only in " + ", ".join(differ) if differ
               else "with identical labels")
        out[cluster] = (f"{len(rows)} series at the same time {how} — not one series, and "
                        "never added up; query a deduplicated endpoint (one series per cluster)")
    return out


#: The characters `markdown_text` escapes: the ones that start markup inside
#: a line of GitHub-flavoured Markdown (a table cell among them).
_MARKDOWN_ACTIVE = "\\`*_[]<>|~&#@"


# What GitHub links with no markup: `www.` at a word start, and `scheme://`.
_WWW = re.compile(r"(?i)(?<![0-9A-Za-z])(www)\.")
_SCHEME = re.compile(r":(?=//)")


def markdown_text(text: str) -> str:
    """*text* as literal text inside one line of Markdown — the one escaping
    for anything that carries a cluster name (a label value, set outside
    this repo) into a Markdown report: a control character, a line break
    among them, becomes ``?``, and every character that starts markup (a
    table's ``|``, a code span's backtick, an HTML tag's ``<``, emphasis, a
    link, an entity) is backslash-escaped, so it renders as itself. GitHub
    also links bare addresses (`www.…`, `https://…`, `a@b.c`) with no markup
    at all: the `@`, the `:` of `://` and the dot after a leading `www` are
    escaped for that."""
    out = []
    for ch in text:
        if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            out.append("?")
        elif ch in _MARKDOWN_ACTIVE:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return _SCHEME.sub(r"\\:", _WWW.sub(r"\1\\.", "".join(out)))


def cluster_text(cluster: str) -> str:
    """A `rows_by_cluster` group as a report names it."""
    return (f'{CLUSTER_IDENTITY_LABEL}="{cluster}"' if cluster
            else f"no {CLUSTER_IDENTITY_LABEL}")
