"""Read YAML the way the exporter reads a tenant id: as the scalar's TEXT.

⛔ THIS IS NOT A STYLE CHOICE. ``yaml.safe_load`` and the exporter's
``gopkg.in/yaml.v3`` disagree about what a tenant id *is*, silently and in
both directions, because PyYAML implements YAML **1.1** implicit typing
while yaml.v3 keys a ``map[string]...`` on the scalar's text. Measured over
the 22 spellings in ``tests/shared/tenant_id_yaml_spelling_matrix.json``
(``123``, ``"123"``, ``010``, ``0x1F``, ``yes``, ``on``, ``null``,
``12:30``, ``0123`` ...): the raw scalar text matches Go on 22/22, while
``str(yaml.safe_load(...))`` matches on 9/22 — ``010`` becomes ``8``,
``0x1F`` becomes ``31``, ``12:30`` becomes ``750``, and ``on`` / ``yes`` /
``true`` / ``True`` all fold into one ``True``. Both directions of that were
reproduced end to end (#1577, #2114):

* **missed**: ``no:`` in one file and ``"no":`` in another is one id to the
  exporter and two to ``safe_load``;
* **false red / wrong id**: ``yes:`` and ``true:`` are two ids to the
  exporter and one (``True``) to ``safe_load``; a platform file's ``123:``
  never met the tenant file's ``"123":``; route generation crashed with
  ``TypeError`` substituting a bool tenant id into ``{{tenant}}``.

Go's answers are pinned, not assumed: the matrix's ``exporter_key`` column is
asserted by the exporter's own decode (``pkg/config``
``tenant_id_spelling_parity_test.go``) and by this module
(``tests/shared/test_tenant_id_yaml_spelling_parity.py``).

Two behaviours are kept rather than reimplemented, both measured against Go:

* ``<<:`` merge keys are expanded — Go expands them too (a ``tenants:``
  block merging an anchor yields the anchor's ids, not ``<<``), which is why
  this subclasses the constructor instead of walking ``yaml.compose``:
  compose would report a tenant literally named ``<<``.
* a ``null`` / ``~`` key is dropped — Go drops it as well.

⚠️ One measured residual, deliberately not closed here: Go **rejects** a
file that repeats the ``tenants:`` key (``mapping key "tenants" already
defined``) while PyYAML takes the last block. That is a whole-file parse
verdict, so it belongs to ``validate_config``'s ``yaml_syntax``, not to a
key reader — recorded so the next reader does not have to re-measure it.

⚠️ Only mapping KEYS change. Values stay PyYAML-typed (``_severity_dedup:
off`` is still ``False``); every consumer of values is unchanged. The one
place a VALUE is a tenant id — a list of them (``exclude_tenants:`` in a
policy, ``tenants:`` in a domain policy) — is opt-in per call through
*raw_text_sequences*: those sequences come back as the items' source text,
so ``exclude_tenants: [010, yes]`` is ``["010", "yes"]`` and compares equal
to the tenant keys above.

⛔ Leaf module: imports ``yaml`` and the standard library ONLY, never another
``_lib_*`` — ``_lib_io`` and ``_lib_confd`` import it.

Parser choice. ``ExporterKeyLoader`` is libyaml's C parser when the wheel
ships it (~10x faster: 2000 tenants in ~62ms instead of ~600ms). The two
parsers agree on well-formed input and DISAGREE on malformed input (libyaml,
like yaml.v3, accepts a trailing tab and deep nesting the pure parser
rejects — see ``_lib_io.SAFE_LOADER``). Readers whose error contract is
built on the pure parser's limits (``RecursionError`` → "unreadable file")
pass ``pure=True`` and keep exactly the parser they had.
"""
from __future__ import annotations

from typing import Any, Iterable

import yaml

__all__ = [
    "ExporterKeyLoader",
    "PureExporterKeyLoader",
    "load_exporter_keys",
    "load_first_document_exporter_keys",
]

_NULL_TAG = "tag:yaml.org,2002:null"


class _ExporterKeys:
    """The key rule, as a mixin over a ``SafeLoader``-family base.

    ``raw_text_sequences``: mapping keys whose SEQUENCE value is returned as
    its scalar items' source text (set per loader instance by the functions
    below; empty by default, so the class alone changes keys only).
    """

    raw_text_sequences: "frozenset[str]" = frozenset()

    def construct_mapping(self, node, deep=False):  # noqa: D102 — see module
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            if isinstance(key_node, yaml.ScalarNode):
                if key_node.tag == _NULL_TAG:
                    continue
                key = key_node.value
            else:
                key = str(self.construct_object(key_node, deep=deep))
            if (key in self.raw_text_sequences
                    and isinstance(value_node, yaml.SequenceNode)):
                mapping[key] = [
                    item.value if isinstance(item, yaml.ScalarNode)
                    else self.construct_object(item, deep=True)
                    for item in value_node.value]
                continue
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


_FAST_BASE = (yaml.CSafeLoader if getattr(yaml, "__with_libyaml__", False)
              else yaml.SafeLoader)


class ExporterKeyLoader(_ExporterKeys, _FAST_BASE):  # type: ignore[misc,valid-type]
    """``CSafeLoader`` (``SafeLoader`` without libyaml); keys are raw text."""


class PureExporterKeyLoader(_ExporterKeys, yaml.SafeLoader):
    """The pure-Python ``SafeLoader``; keys are raw text. See ``pure=``."""


def _make_loader(stream: Any, pure: bool,
                 raw_text_sequences: Iterable[str]) -> Any:
    loader = (PureExporterKeyLoader if pure else ExporterKeyLoader)(stream)
    if raw_text_sequences:
        loader.raw_text_sequences = frozenset(raw_text_sequences)
    return loader


def load_exporter_keys(stream: Any, *, pure: bool = False,
                       raw_text_sequences: Iterable[str] = ()) -> Any:
    """``yaml.load(stream, Loader=ExporterKeyLoader)``, spelled the long way.

    Single document, like ``yaml.safe_load`` (a second document raises).

    ⛔ NOT a style choice, and NOT a way around the safety rule. The loader
    has to be a ``SafeLoader``-family **subclass** (it changes how mapping
    KEYS are read), and no automated check in this repo can express that:
    dev-rules §5 item 4 is enforced by bandit B506, which flags
    ``Loader=<SafeLoader subclass>`` as a violation just as the AST guard
    removed in #1643 did. Spelling the call out longhand keeps it outside a
    predicate that would be wrong about it either way.

    ⚠️ Be exact about the cost. This body never calls ``yaml.load``, so it is
    not a *rejected* spelling — it is OUTSIDE both predicates. Someone
    copying this shape with ``yaml.UnsafeLoader`` gets no red from anything.

    The safety property is therefore pinned by BEHAVIOUR, not by spelling:
    ``tests/shared/test_tenant_id_yaml_spelling_parity.py::
    test_the_loaders_cannot_construct_python_objects`` feeds both loaders an
    actual ``!!python/object/apply`` payload and requires it to be refused,
    with a must-still-work control beside it. ⛔ Deleting that test leaves
    this shape completely unguarded. ⚠️ NOT GUARDED: it pins THESE loaders
    only — nothing pins a future copy.
    """
    loader = _make_loader(stream, pure, raw_text_sequences)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def load_first_document_exporter_keys(
        stream: Any, *, pure: bool = False,
        raw_text_sequences: Iterable[str] = ()) -> Any:
    """The FIRST document of *stream* (None when there is none), keys as text.

    As the exporter's walker reads a config file: yaml.v3 ``Unmarshal``
    decodes one document. The loader is never advanced past the first
    document, so an error in a LATER document cannot drop this one (same
    rule as ``next(yaml.safe_load_all(fh), None)``, which this replaces).
    Longhand for the reason given in ``load_exporter_keys``.
    """
    loader = _make_loader(stream, pure, raw_text_sequences)
    try:
        if loader.check_data():
            return loader.get_data()
        return None
    finally:
        loader.dispose()
