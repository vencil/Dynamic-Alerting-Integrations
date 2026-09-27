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

A key that is NOT a scalar (``? [a, b]``) is refused with a
``ConstructorError`` — Go refuses it (``cannot unmarshal !!seq into
string``) and so did ``safe_load`` (``found unhashable key``); so is a
``!!map`` tag on something that is not a mapping. Both are
``yaml.YAMLError``, so every reader's existing "unreadable file" handling
names the file (#2114 review: before, these leaked ``TypeError`` /
``ValueError`` and a policy-engine run became a traceback).

⚠️ Measured residuals, deliberately not closed here:

* Go **rejects** a mapping that repeats a key (``mapping key "123" already
  defined``, also for a repeated ``tenants:``), and keys are compared by
  raw text — so ``123:`` and ``"123":`` in ONE mapping are that duplicate.
  This loader ALONE, like PyYAML, keeps the last one. The strict readers do
  not use it alone: ``_lib_io.StrictExporterKeyLoader`` composes it with
  #2123's ``RejectDuplicateKeys`` (whose ``_key_identity`` is the same
  kind + raw text), and every reader that was strict on main reads through
  that — there the two spellings raise ``DuplicateKeyError``. The residual
  is the readers that were NOT strict on main and use this loader directly:
  ``_lib_confd.declared_tenant_ids``, ``diagnose``, and
  ``analyze_rule_pack_gaps --tenant-config`` fold the two spellings into
  one tenant (``safe_load`` made them two, 123 and "123"). Whether they
  become strict is #2123's decision per reader. Both halves are pinned in
  ``tests/shared/test_tenant_id_yaml_spelling_parity.py``.

⚠️ Only mapping KEYS change. Values stay PyYAML-typed (``_severity_dedup:
off`` is still ``False``); every consumer of values is unchanged. The one
place a VALUE is a tenant id — a list of them (``exclude_tenants:`` in a
policy, ``tenants:`` in a domain policy) — is opt-in per call through
*raw_text_sequences*: those sequences come back as the items' source text,
so ``exclude_tenants: [010, yes]`` is ``["010", "yes"]`` and compares equal
to the tenant keys above.

⛔ Leaf module: imports ``yaml`` and the standard library ONLY, never another
``_lib_*`` — ``_lib_io`` and ``_lib_confd`` import it.

Parser choice: the pure-Python ``yaml.SafeLoader``, i.e. exactly the parser
every caller used before (``yaml.safe_load`` / ``load_yaml_file``).
libyaml's ``CSafeLoader`` was considered (~10x faster, and closer to yaml.v3
on malformed input: it accepts a trailing tab and deep nesting the pure
parser refuses) and deliberately NOT used: the two parsers disagree exactly
on malformed files, and switching only the tenant-key readers made one run
give two answers — ``validate_config``'s ``yaml_syntax`` called a file
unreadable while ``tenant_uniqueness`` read it, and the routing reader
dropped a tenant file that ``declared_tenant_ids`` still counted, so a
platform block created the tenant silently. Which parser the tool family
uses on malformed files is decided in #2123, for all readers at once.
"""
from __future__ import annotations

from typing import Any, Iterable

import yaml

__all__ = [
    "ExporterKeyLoader",
    "load_exporter_keys",
    "load_first_document_exporter_keys",
]

_NULL_TAG = "tag:yaml.org,2002:null"


class ExporterKeyLoader(yaml.SafeLoader):
    """``SafeLoader``, except a mapping key is the scalar's RAW TEXT.

    ``raw_text_sequences``: mapping keys whose SEQUENCE value is returned as
    its scalar items' source text (set per loader instance by the functions
    below; empty by default, so the class alone changes keys only).
    """

    raw_text_sequences: "frozenset[str]" = frozenset()

    def construct_mapping(self, node, deep=False):  # noqa: D102 — see module
        # Same guard as SafeConstructor's: `!!map [a, b]` / `!!map abc` reach
        # here with a non-mapping node, and must be a YAMLError, not the
        # TypeError / ValueError the loop below would raise.
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None, None,
                "expected a mapping node, but found %s" % node.id,
                node.start_mark)
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                # Go: `cannot unmarshal !!seq into string`; safe_load:
                # `found unhashable key`. Never a tenant named "[]".
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "found unhashable key (a %s node is not a tenant id)"
                    % key_node.id, key_node.start_mark)
            if key_node.tag == _NULL_TAG:
                continue
            key = key_node.value
            if (key in self.raw_text_sequences
                    and isinstance(value_node, yaml.SequenceNode)):
                mapping[key] = [
                    item.value if isinstance(item, yaml.ScalarNode)
                    else self.construct_object(item, deep=True)
                    for item in value_node.value]
                continue
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _make_loader(stream: Any, raw_text_sequences: Iterable[str]) -> Any:
    loader = ExporterKeyLoader(stream)
    if raw_text_sequences:
        loader.raw_text_sequences = frozenset(raw_text_sequences)
    return loader


def load_exporter_keys(stream: Any, *,
                       raw_text_sequences: Iterable[str] = ()) -> Any:
    """``yaml.load(stream, Loader=ExporterKeyLoader)``, spelled the long way.

    Single document, like ``yaml.safe_load`` (a second document raises).

    ⛔ NOT a style choice, and NOT a way around the safety rule. The loader
    has to be a ``SafeLoader`` **subclass** (it changes how mapping KEYS are
    read), and no automated check in this repo can express that: dev-rules
    §5 item 4 is enforced by bandit B506, which flags
    ``Loader=<SafeLoader subclass>`` as a violation just as the AST guard
    removed in #1643 did. Spelling the call out longhand keeps it outside a
    predicate that would be wrong about it either way.

    ⚠️ Be exact about the cost. This body never calls ``yaml.load``, so it is
    not a *rejected* spelling — it is OUTSIDE both predicates. Someone
    copying this shape with ``yaml.UnsafeLoader`` gets no red from anything.

    The safety property is therefore pinned by BEHAVIOUR, not by spelling:
    ``tests/shared/test_tenant_id_yaml_spelling_parity.py::
    test_the_loader_cannot_construct_python_objects`` feeds both entry
    points an actual ``!!python/object/apply`` payload and requires it to be
    refused, with a must-still-work control beside it. ⛔ Deleting that test
    leaves this shape completely unguarded. ⚠️ NOT GUARDED: it pins THIS
    loader only — nothing pins a future copy.
    """
    loader = _make_loader(stream, raw_text_sequences)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def load_first_document_exporter_keys(
        stream: Any, *, raw_text_sequences: Iterable[str] = ()) -> Any:
    """The FIRST document of *stream* (None when there is none), keys as text.

    As the exporter's walker reads a config file: yaml.v3 ``Unmarshal``
    decodes one document. The loader is never advanced past the first
    document, so an error in a LATER document cannot drop this one (same
    rule as ``next(yaml.safe_load_all(fh), None)``, which this replaces).
    Longhand for the reason given in ``load_exporter_keys``.
    """
    loader = _make_loader(stream, raw_text_sequences)
    try:
        if loader.check_data():
            return loader.get_data()
        return None
    finally:
        loader.dispose()
