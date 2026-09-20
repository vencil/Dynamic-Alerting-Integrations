"""The C YAML parser is available, opted into where it is safe, and agrees
with the pure-Python one on everything the repository ships.

Three things, each pinned because each can silently regress:

* **`_lib_io.SAFE_LOADER` / `_lib_io.safe_load()` use libyaml** when the
  wheel ships it. Tools that read repository-tracked YAML opt in
  (`generate_rule_pack_stats` halved its wall time). The fallback to the
  pure-Python parser is the right behaviour for a dev box with a source-built
  PyYAML and the WRONG behaviour for CI, where "the fast path quietly went
  away" is exactly the kind of green nobody looks at. Under `CI`, libyaml is
  required.

* **`_lib_io.load_yaml_file()` stays on the pure parser.** The two parsers
  agree on well-formed input and DISAGREE on malformed input: libyaml accepts
  a trailing tab the pure parser rejects, and parses nesting the pure parser
  dies on. Tools reading operator files have made those limits part of their
  contract (`deprecate_rule`'s "本工具讀不了（pure parser 限制）",
  `validate_config`'s "unreadable file → exit 1"), so the shared reader must
  not change parser underneath them. The test process does NOT rebind
  `yaml.SafeLoader` either, for the same reason: in-process tool tests would
  then run on a parser the tool never uses in production. Measured before
  deciding: 62% of the pure-parser time inside the test process is tools
  called in-process, and the whole of it is ~1–2% of worker time.

* **The two parsers must produce the same documents** for everything the
  repository tracks. PyYAML documents them as equivalent; this measures it,
  over the tracked corpus, every run — so a libyaml upgrade that starts
  disagreeing on some edge (tabs, flow-style, anchors) is caught here rather
  than as a mysterious diff in a lint that reads rule packs.
"""
from __future__ import annotations

import os

import pytest
import yaml
from yaml import loader as _pure  # the pure-Python classes by their own name

from _tree import repo_files

_LIBYAML = bool(getattr(yaml, "__with_libyaml__", False))

# Accepted by libyaml (and by the exporter's yaml.v3, which is its port),
# rejected by the pure-Python scanner. The one input the two parsers are
# KNOWN to disagree on; `deprecate_rule` builds a verdict on exactly this.
_TRAILING_TAB = "defaults:\n  cpu_usage: 80\t\n"


def test_libyaml_is_present_in_ci():
    """Fail closed on CI, skip with a reason elsewhere."""
    if not _LIBYAML:
        if os.environ.get("CI"):
            pytest.fail(
                "PyYAML was installed without libyaml on CI — `_lib_io.safe_load` "
                "and every tool that opted into it just fell back to the "
                "pure-Python parser (11.7x slower on the corpus). Check the "
                "wheel `pip install pyyaml` resolved to.")
        pytest.skip("PyYAML built without libyaml on this machine; the C-parser "
                    "fast path is not exercised here")


@pytest.mark.skipif(not _LIBYAML, reason="no libyaml on this machine")
def test_the_opt_in_loader_is_the_c_parser_and_the_shared_reader_is_not(tmp_path):
    """Both halves of the boundary, on the one input they disagree about."""
    import _lib_io
    assert _lib_io.SAFE_LOADER is yaml.CSafeLoader
    assert _lib_io.safe_load(_TRAILING_TAB) == {"defaults": {"cpu_usage": 80}}

    assert yaml.SafeLoader is _pure.SafeLoader, (
        "something rebound yaml.SafeLoader in the test process — in-process "
        "tool tests would now run on a parser the tools never use in prod")
    path = tmp_path / "t.yaml"
    path.write_text(_TRAILING_TAB, encoding="utf-8")
    with pytest.raises(_lib_io.YamlFileError):
        _lib_io.load_yaml_file(str(path))


@pytest.mark.skipif(not _LIBYAML, reason="no libyaml on this machine")
def test_both_parsers_agree_on_every_tracked_yaml_file():
    """Every `.yaml` / `.yml` the repo tracks: same documents, same failures.

    `load_all` rather than `load`, so multi-document files (k8s manifests)
    compare document-by-document instead of both raising "expected a single
    document" and comparing an exception to an exception.
    """
    files = repo_files(".yaml", ".yml")
    assert len(files) > 100, f"only {len(files)} YAML files found — scan broken?"

    def parse(loader, text):
        try:
            return ("ok", list(yaml.load_all(text, Loader=loader)))
        except yaml.YAMLError as exc:
            return ("error", type(exc).__name__)

    disagreements = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        pure, fast = parse(_pure.SafeLoader, text), parse(yaml.CSafeLoader, text)
        if pure != fast:
            disagreements.append((path.as_posix(), pure[0], fast[0]))
    assert not disagreements, (
        "libyaml and the pure-Python parser disagree on these tracked files "
        "(path, pure outcome, C outcome):\n  "
        + "\n  ".join(map(str, disagreements[:20])))
