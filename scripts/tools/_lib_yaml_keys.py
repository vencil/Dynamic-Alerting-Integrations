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
to the tenant keys above. Likewise a scalar VALUE that names a key — a
tenant's ``_profile: 010`` naming profile ``010:`` — is opt-in per call
through *raw_text_scalars* (#2216; non-null scalars only).

A tool that WRITES the file back reads it through ``load_for_rewrite`` /
``RewriteLoader`` instead (#2220, end of this module): there plain scalar
values keep their source text too, so the rewrite does not retype them.

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
    "KeepPlainScalarText",
    "RawPlain",
    "RewriteDumper",
    "RewriteLoader",
    "dump_for_rewrite",
    "load_all_exporter_keys",
    "load_exporter_keys",
    "load_first_document_exporter_keys",
    "load_for_rewrite",
]

_NULL_TAG = "tag:yaml.org,2002:null"


class ExporterKeyLoader(yaml.SafeLoader):
    """``SafeLoader``, except a mapping key is the scalar's RAW TEXT.

    ``raw_text_sequences``: mapping keys whose SEQUENCE value is returned as
    its scalar items' source text (set per loader instance by the functions
    below; empty by default, so the class alone changes keys only).
    """

    raw_text_sequences: "frozenset[str]" = frozenset()
    #: mapping keys whose non-null SCALAR value is returned as its source
    #: text (#2216: a tenant's ``_profile: 010`` names profile ``"010"``, as
    #: the exporter's ``ScheduledValue`` keeps ``value.Value``). A null value
    #: stays ``None``: ``~`` is no profile name, and writing ``'~'`` back
    #: would turn it into one.
    raw_text_scalars: "frozenset[str]" = frozenset()

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
            if (key in self.raw_text_scalars
                    and isinstance(value_node, yaml.ScalarNode)
                    and value_node.tag != _NULL_TAG):
                mapping[key] = value_node.value
                continue
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _make_loader(stream: Any, raw_text_sequences: Iterable[str],
                 raw_text_scalars: Iterable[str] = ()) -> Any:
    loader = ExporterKeyLoader(stream)
    if raw_text_sequences:
        loader.raw_text_sequences = frozenset(raw_text_sequences)
    if raw_text_scalars:
        loader.raw_text_scalars = frozenset(raw_text_scalars)
    return loader


def load_exporter_keys(stream: Any, *,
                       raw_text_sequences: Iterable[str] = (),
                       raw_text_scalars: Iterable[str] = ()) -> Any:
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

    *raw_text_scalars*: see ``ExporterKeyLoader.raw_text_scalars``.
    """
    loader = _make_loader(stream, raw_text_sequences, raw_text_scalars)
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


def load_all_exporter_keys(stream: Any, *,
                           raw_text_sequences: Iterable[str] = (),
                           raw_text_scalars: Iterable[str] = ()) -> "list[Any]":
    """EVERY document of *stream*, keys as text: ``list(yaml.safe_load_all(
    stream))`` with the exporter's tenant ids (#2216).

    NOT strict (a repeated key keeps the last value), like the two entries
    above; the strict sibling is ``_lib_io.strict_load_all_exporter_keys``.
    Eager, so the loader is disposed before this returns; an error in ANY
    document raises, as ``list(yaml.safe_load_all(...))`` did. Longhand for
    the reason given in ``load_exporter_keys``. *raw_text_sequences* /
    *raw_text_scalars*: see ``ExporterKeyLoader``.
    """
    loader = _make_loader(stream, raw_text_sequences, raw_text_scalars)
    try:
        docs = []
        while loader.check_data():
            docs.append(loader.get_data())
        return docs
    finally:
        loader.dispose()


# ── #2220: reading a file that will be WRITTEN BACK ─────────────────────────
#
# A tool that loads a conf.d file, deletes or sets one key and dumps the rest
# (``deprecate_rule --execute``, ``patch_config``) re-serialises every OTHER
# value it did not mean to touch. Through ``safe_load`` + ``safe_dump`` that
# is a retype, and the exporter's value moves with it (measured with
# ``da-guard served-values``): ``010`` is written as ``8`` (10 → 8), ``12:30``
# as ``750`` (12 → 750), ``0x1F`` as ``31``, ``yes`` as ``true``, and
# ``expires: 2099-01-01T00:00:00Z`` as ``2099-01-01 00:00:00+00:00`` — which
# the exporter no longer reads as a time, so a maintenance window closes.
#
# The fix is (a'): an UNQUOTED (plain) scalar is loaded as :class:`RawPlain`,
# its source text, and :class:`RewriteDumper` writes it back plain with the
# tag that text resolves to — so the emitter prints the same characters. A
# quoted scalar was a ``str`` already and still is; ``null`` / ``~`` / empty
# stays ``None``.
#
# ⛔ NOT (a) "keep every scalar as a string": a string is written back QUOTED
# unless YAML would read it as a string anyway, and the exporter's typed
# fields refuse a quoted number — ``defaults`` is ``map[string]float64``
# (the whole ``_defaults.yaml`` is dropped), ``max_metrics_per_tenant`` is an
# ``int``, ``_custom_alerts[].min_events`` a ``strictInt`` (the alert is
# dropped). RawPlain keeps them plain, hence typed.
#
# ⚠️ Rewrite paths ONLY. Every other reader keeps PyYAML-typed values (a
# RawPlain ``"8"`` compares unequal to ``8``); that is why these are separate
# entry points and not a flag on the loaders above. Comments are still lost
# on rewrite — that is the existing behaviour, not addressed here.


class RawPlain(str):
    """A plain (unquoted) scalar's source text, as read for a rewrite.

    A ``str``, so every string operation and comparison a caller already
    does works; :class:`RewriteDumper` is what gives it back its style.
    """

    __slots__ = ()


def _implicit_tag(resolver: Any, text: str) -> str:
    """The tag YAML gives *text* written plain (what the composer stamps)."""
    return resolver.resolve(yaml.ScalarNode, text, (True, False))


#: The implicit tags a RawPlain may carry: the ones ``SafeConstructor``
#: builds a value for. ⛔ NOT every tag the resolver can give — ``=`` resolves
#: to ``!!value`` and ``<<`` (as a value) to ``!!merge``, which SafeConstructor
#: REFUSES ("could not determine a constructor"), and the non-rewrite read
#: (``deprecate_rule``'s scan, ``patch_config``'s pre-#2220 reader) refuses
#: the file with it. Wrapping them would let the rewrite read — and rewrite —
#: a file every other read of the same run calls unreadable (#2220 review).
_RAW_PLAIN_TAGS = frozenset("tag:yaml.org,2002:" + t for t in
                            ("str", "int", "float", "bool", "timestamp"))


def _is_plain_as_written(resolver: Any, node: Any) -> bool:
    """A plain scalar whose tag is the one its text implies — i.e. writing
    the text back plain reproduces the node — and a tag ``SafeConstructor``
    accepts (``_RAW_PLAIN_TAGS``; null stays None). ``!!str 5`` is not (its
    tag differs), so it keeps its constructed ``"5"``; ``=`` / ``<<`` go to
    ``SafeConstructor`` and fail there exactly as in the non-rewrite read."""
    return (isinstance(node, yaml.ScalarNode) and node.style is None
            and node.tag in _RAW_PLAIN_TAGS
            and node.tag == _implicit_tag(resolver, node.value))


class KeepPlainScalarText:
    """Loader mixin: plain scalars — values AND mapping keys — are RawPlain.

    Put it BEFORE the loader class in the bases, so ``construct_object``
    sees the node first. Keys are text already (``ExporterKeyLoader``); a
    plain key is re-wrapped so it is written back plain too (``010:`` stays
    ``010:``, not ``'010':``).
    """

    def construct_object(self, node, deep=False):  # noqa: D102 — see class
        if _is_plain_as_written(self, node):
            return RawPlain(node.value)
        return super().construct_object(node, deep=deep)  # type: ignore[misc]

    def construct_mapping(self, node, deep=False):  # noqa: D102 — see class
        mapping = super().construct_mapping(node, deep=deep)  # type: ignore[misc]
        # `node.value` is flattened by now (merge keys expanded). By text,
        # the LAST spelling wins — the same one whose value the dict kept.
        plain = {k.value: _is_plain_as_written(self, k)
                 for k, _v in node.value if isinstance(k, yaml.ScalarNode)}
        return {(RawPlain(k) if isinstance(k, str) and plain.get(k) else k): v
                for k, v in mapping.items()}


class RewriteLoader(KeepPlainScalarText, ExporterKeyLoader):
    """``ExporterKeyLoader`` for a file that will be dumped back (#2220)."""


class RewriteDumper(yaml.SafeDumper):
    """``SafeDumper`` that writes a :class:`RawPlain` back as it was read."""


def _represent_raw_plain(dumper: Any, data: RawPlain) -> Any:
    text = str(data)
    return yaml.ScalarNode(_implicit_tag(dumper, text), text, style=None)


RewriteDumper.add_representer(RawPlain, _represent_raw_plain)


def load_for_rewrite(stream: Any) -> Any:
    """:func:`load_exporter_keys` for a rewrite: plain scalars are RawPlain.

    Longhand for the reason given in ``load_exporter_keys``; NOT strict (a
    repeated key keeps the last value), like that function. The strict
    sibling is ``_lib_io.strict_load_for_rewrite``.
    """
    loader = RewriteLoader(stream)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def dump_for_rewrite(data: Any, **kwargs: Any) -> str:
    """``yaml.safe_dump(data, **kwargs)`` that writes RawPlain back plain."""
    return yaml.dump(data, Dumper=RewriteDumper, **kwargs)
