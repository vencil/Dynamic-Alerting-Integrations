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

import re
from typing import Any

#: ADR-006: the external label that says which cluster a Prometheus is in.
CLUSTER_IDENTITY_LABEL = "da_cluster"

#: What a check says about a Prometheus whose `da_cluster` has no value — one
#: wording for every tool that points it out.
CLUSTER_IDENTITY_UNSET = (
    f"external_labels.{CLUSTER_IDENTITY_LABEL} is not set — add it under "
    "global.external_labels with this cluster's id (ADR-006)")

_EXTERNAL_LABELS = re.compile(r"(?m)^(?P<indent>[ ]*)external_labels:[ ]*$")
_LABEL_LINE = re.compile(r"^(?P<indent>[ ]+)(?P<key>[A-Za-z_][A-Za-z0-9_]*):[ ]*(?P<value>.*?)[ ]*$")


def external_labels(config_yaml: str) -> "dict[str, str] | None":
    """`global.external_labels` of a configuration as `/api/v1/status/config`
    prints it — the keys directly under that one mapping and nothing else in
    the file (a scrape job's `labels:` or `target_label:` is not it). None
    when the configuration has no such mapping.

    The text is Prometheus' own rendering (block style, one key a line), which
    is what makes reading it line by line safe; it is not a YAML reader for a
    hand-written file (that one is :func:`config_external_labels`, on the
    document Prometheus' loader parsed).
    """
    m = _EXTERNAL_LABELS.search(config_yaml)
    if m is None:
        return None
    base, out = len(m["indent"]), {}
    for line in config_yaml[m.end():].split("\n")[1:]:
        if not line.strip():
            continue
        k = _LABEL_LINE.match(line)
        if k is None or len(k["indent"]) <= base:
            break
        out[k["key"]] = k["value"].strip("\"'")
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
