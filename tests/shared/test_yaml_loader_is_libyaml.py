"""The shared PyYAML reader stays on the pure-Python parser, never libyaml's.

`_lib_io.load_yaml_file()` reads with the pure parser. The two parsers agree
on well-formed input and DISAGREE on malformed input: libyaml accepts a
trailing tab the pure parser rejects, and parses nesting the pure parser
dies on. Tools reading operator files have made those limits part of their
contract (`validate_config`'s "unreadable file → exit 1"), so the shared
reader must not change parser underneath them. The test process does NOT
rebind `yaml.SafeLoader` either, for the same reason: in-process tool tests
would then run on a parser the tool never uses in production.

(The libyaml opt-in, `_lib_io.SAFE_LOADER` / `_lib_io.safe_load()` and
`strict_safe_load(fast=True)`, lost its last caller when the readers of
files with a Go consumer moved to that consumer's reading, ADR-036 §12; it
was deleted in #3206 together with the tests that pinned it. Nothing in
`scripts/` uses libyaml any more, so "CI has libyaml" — what
`test_libyaml_is_present_in_ci` guarded — is no longer a requirement, and
that test went with it. What stays needs no libyaml: the pure parser's
refusal is measured directly, and libyaml only adds a control when present,
so the test never skips.)
"""
from __future__ import annotations

import pytest
import yaml
from yaml import loader as _pure  # the pure-Python classes by their own name

_LIBYAML = bool(getattr(yaml, "__with_libyaml__", False))

# Accepted by libyaml (and by the exporter's yaml.v3, which is its port),
# rejected by the pure-Python scanner. The one input the two parsers are
# KNOWN to disagree on.
_TRAILING_TAB = "defaults:\n  cpu_usage: 80\t\n"


def test_the_shared_reader_is_the_pure_parser(tmp_path):
    """On the one input the parsers disagree about: the pure parser refuses
    it, and so does the shared reader. When libyaml is installed it reads the
    same input (the control: the input does separate the two parsers)."""
    import _lib_io
    with pytest.raises(yaml.scanner.ScannerError):
        yaml.load(_TRAILING_TAB, Loader=_pure.SafeLoader)
    if _LIBYAML:
        assert yaml.load(_TRAILING_TAB, Loader=yaml.CSafeLoader) == {"defaults": {"cpu_usage": 80}}

    assert yaml.SafeLoader is _pure.SafeLoader, (
        "something rebound yaml.SafeLoader in the test process — in-process "
        "tool tests would now run on a parser the tools never use in prod")
    path = tmp_path / "t.yaml"
    path.write_text(_TRAILING_TAB, encoding="utf-8")
    with pytest.raises(_lib_io.YamlFileError):
        _lib_io.load_yaml_file(str(path))
