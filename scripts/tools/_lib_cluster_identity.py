"""Which cluster a Prometheus says it is in — `global.external_labels.da_cluster`.

ADR-006 contract 1 and 7 (#4097): every Prometheus sets it, a single cluster
too. The one reading of that fact for every tool that points out a Prometheus
without it — `federation-check edge` and `byo-check prometheus` (the running
config, as `/api/v1/status/config` prints it), `onboard` (a config file,
already parsed by Prometheus' own loader) and the repo's scrape-config lint.

Standard library only, and no I/O: a tool that reads a file imports this
without becoming a tool that can use HTTP.
"""
from __future__ import annotations

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
