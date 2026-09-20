"""The C YAML parser is in use, and it agrees with the pure-Python one.

Two halves, each pinned because each can silently regress:

* **tests/conftest.py rebinds `yaml.SafeLoader` to `yaml.CSafeLoader`** and
  `_lib_io.SAFE_LOADER` does the same for the tools. Both are conditional on
  `yaml.__with_libyaml__`, so a PyYAML install without libyaml (a source
  build, a wheel for an exotic platform) falls back to the pure-Python parser
  and everything still passes — just slower. That is the right behaviour for
  a dev box and the WRONG behaviour for CI, where "the fast path quietly went
  away" is exactly the kind of green nobody looks at. Under `CI`, libyaml is
  required.

* **The two parsers must produce the same documents** for everything the
  repository ships. PyYAML documents them as equivalent; this measures it,
  over the tracked corpus, every run — so a libyaml upgrade that starts
  disagreeing on some edge (tabs, flow-style, anchors) is caught here rather
  than as a mysterious diff in a lint that reads rule packs.
"""
from __future__ import annotations

import os

import pytest
import yaml
from yaml import loader as _pure  # the pure-Python classes, whatever the module attr says

from _tree import repo_files

_LIBYAML = bool(getattr(yaml, "__with_libyaml__", False))


def test_libyaml_is_present_in_ci():
    """Fail closed on CI, skip with a reason elsewhere."""
    if not _LIBYAML:
        if os.environ.get("CI"):
            pytest.fail(
                "PyYAML was installed without libyaml on CI — every "
                "`yaml.safe_load` in the suite and in `_lib_io` just fell back "
                "to the pure-Python parser (11.7x slower on the corpus). Check "
                "the wheel `pip install pyyaml` resolved to.")
        pytest.skip("PyYAML built without libyaml on this machine; the C-parser "
                    "fast path is not exercised here")


@pytest.mark.skipif(not _LIBYAML, reason="no libyaml on this machine")
def test_the_test_process_and_the_tools_use_the_c_parser():
    """conftest's rebind took effect, and the tools' constant agrees."""
    assert yaml.SafeLoader is yaml.CSafeLoader, (
        "tests/conftest.py no longer rebinds yaml.SafeLoader — every "
        "safe_load in the suite is back on the pure-Python parser")
    import _lib_io
    assert _lib_io.SAFE_LOADER is yaml.CSafeLoader


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
