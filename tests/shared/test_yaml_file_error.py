"""A conf.d file whose CONTENT is not UTF-8 must name itself, not traceback (#1654).

The content axis of the #1339 family. #1634 fixed the file-NAME axis; this
is the independent one: a pure-ASCII ``alpha.yaml`` with one ``\\xff`` byte
in a comment. ``_lib_io.load_yaml_file`` opened the file in text mode, so
that byte raised ``UnicodeDecodeError`` — a ``ValueError``, not a
``yaml.YAMLError`` — out of the helper, past every ``except yaml.YAMLError``
its callers had written, and the tool died with a traceback and rc 1
(``EXIT_VIOLATION``'s number, for an IO failure the SSOT files under 2).

Measured on ``origin/main`` before the fix (real binaries, one ``\\xff``
byte in the relevant input, ``rc / traceback / file named``):

    deprecate_rule                    1  yes  no
    backtest_threshold --git-diff     1  yes  no   (0 bytes stdout under --json)
    config_diff                       2  no   no   (its own catch-all; UnicodeDecodeError carries no path)
    policy_engine  (_defaults / tenant) 1 yes  no
    generate_tenant_mapping_rules     1  yes  no
    analyze_rule_pack_gaps            1  yes  no
    onboard_platform                  1  yes  (path only because the phase banner prints argv)
    check_routing_profiles            1  yes  no
    policy_opa_bridge / blind_spot_discovery / notification_tester /
    maintenance_scheduler / threshold_recommend   1  yes  no
    validate_config                   1  no   yes  (reads the tree itself first; unchanged)

Two decisions, both pinned here:

* **The helper names the file.** ``load_yaml_file`` now reads bytes and
  lets PyYAML decode them (``yaml.reader.ReaderError`` on bad UTF-8 — a
  ``YAMLError``), then wraps ANY ``YAMLError`` in :class:`YamlFileError`
  whose ``str()`` starts with the path. ``UnicodeDecodeError`` carries a
  codec and an offset and no path; the helper is the only layer that has
  the path AND sees the failure, so it is the one that speaks. Because
  ``YamlFileError`` is itself a ``yaml.YAMLError``, every existing
  ``except yaml.YAMLError`` / ``except Exception`` site now catches the
  decode failure through its existing message path — that is the mechanism,
  not a side effect.
* **Not a silent skip.** The helper raises; nothing here returns ``default``
  for a file that exists but cannot be read. Callers with no handler get
  the shared ``exit_on_yaml_file_error`` entry wrapper: ``ERROR: cannot
  read <path>: <reason>`` on stderr, rc 2.

Bytes-mode also moves the Python side ONTO the exporter's answer: measured
with ``gopkg.in/yaml.v3`` (the exporter's parser), ``\\xff`` is rejected,
UTF-8 BOM loads, UTF-16-with-BOM loads. PyYAML on bytes gives the same three
answers; the old text-mode read crashed on UTF-16 while the exporter served
it — one file, two answers, which is the family this ticket belongs to.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "scripts" / "tools"
sys.path.insert(0, str(TOOLS))

import _lib_io as lio  # noqa: E402
import _lib_python as lp  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402

# Pure-ASCII carrier name on purpose: the NAME axis is #1634's; only the
# content is bad here.
XFF_DOC = b"# legacy \xff marker\ntenants:\n  alpha:\n    cpu_usage: 80\n"
CP950_DOC = "# 註解\ntenants:\n  alpha:\n    cpu_usage: 80\n".encode("cp950")
CLEAN_DOC = "# 註解\ntenants:\n  alpha:\n    cpu_usage: 80\n".encode("utf-8")
BOM_DOC = "﻿" + "tenants:\n  alpha:\n    cpu_usage: 80\n"
UTF16_DOC = "tenants:\n  alpha:\n    cpu_usage: 80\n".encode("utf-16")
BROKEN_DOC = b"a: [\n"
PARSED = {"tenants": {"alpha": {"cpu_usage": 80}}}


# ---------------------------------------------------------------------------
# Helper level
# ---------------------------------------------------------------------------
class TestYamlFileErrorClass:
    def test_is_a_yaml_error(self):
        """The whole mechanism: existing ``except yaml.YAMLError`` sites see it."""
        assert issubclass(lio.YamlFileError, yaml.YAMLError)

    def test_str_names_path_and_cause_class(self):
        cause = yaml.reader.ReaderError("<byte string>", 9, 0xFF,
                                        "utf8", "invalid start byte")
        exc = lio.YamlFileError("conf.d/alpha.yaml", cause)
        text = str(exc)
        assert text.startswith("conf.d/alpha.yaml: "), text
        assert "ReaderError" in text
        assert "\n" not in text, "one line — callers interpolate it into a report line"
        assert exc.path == "conf.d/alpha.yaml"
        assert exc.cause is cause

    def test_facade_re_exports_it(self):
        assert lp.YamlFileError is lio.YamlFileError
        assert lp.exit_on_yaml_file_error is lio.exit_on_yaml_file_error


class TestLoadYamlFileContentEncoding:
    @pytest.mark.parametrize("label, raw", [("xff", XFF_DOC), ("cp950", CP950_DOC)])
    def test_invalid_utf8_raises_yaml_file_error_naming_the_path(self, tmp_path, label, raw):
        p = tmp_path / "alpha.yaml"
        p.write_bytes(raw)
        with pytest.raises(lio.YamlFileError) as ei:
            lio.load_yaml_file(str(p))
        exc = ei.value
        assert isinstance(exc, yaml.YAMLError)
        assert str(p) in str(exc)
        assert isinstance(exc.__cause__, yaml.reader.ReaderError), (
            "PyYAML decodes the bytes itself; the decode failure is a ReaderError")
        assert exc.cause is exc.__cause__

    def test_invalid_utf8_is_not_swallowed_into_default(self, tmp_path):
        """Constraint (1) of #1654: the crash must not become a silent skip."""
        p = tmp_path / "alpha.yaml"
        p.write_bytes(XFF_DOC)
        with pytest.raises(lio.YamlFileError):
            lio.load_yaml_file(str(p), default={})

    def test_valid_utf8_unchanged(self, tmp_path):
        p = tmp_path / "alpha.yaml"
        p.write_bytes(CLEAN_DOC)
        assert lio.load_yaml_file(str(p)) == PARSED

    def test_utf8_bom_loads(self, tmp_path):
        """Observed: PyYAML skips U+FEFF; text mode did too. No behaviour change."""
        p = tmp_path / "alpha.yaml"
        p.write_text(BOM_DOC, encoding="utf-8")
        assert lio.load_yaml_file(str(p)) == PARSED

    def test_utf16_with_bom_loads(self, tmp_path):
        """Observed, and a deliberate delta: text mode raised UnicodeDecodeError
        here; bytes mode lets PyYAML honour the BOM. The exporter's yaml.v3
        reads this file too (measured), so this is the parity direction.
        """
        p = tmp_path / "alpha.yaml"
        p.write_bytes(UTF16_DOC)
        assert lio.load_yaml_file(str(p)) == PARSED

    def test_broken_yaml_now_names_the_path(self, tmp_path):
        """Also improves every existing YAMLError site: the path rides along."""
        p = tmp_path / "alpha.yaml"
        p.write_bytes(BROKEN_DOC)
        with pytest.raises(lio.YamlFileError) as ei:
            lio.load_yaml_file(str(p))
        assert str(p) in str(ei.value)
        assert "ParserError" in str(ei.value)
        assert isinstance(ei.value.__cause__, yaml.YAMLError)

    def test_missing_empty_none_keep_default_semantics(self, tmp_path):
        """Pinned by test_lib_python / test_property_tools too; repeated here so
        this file is self-contained about what did NOT change."""
        assert lio.load_yaml_file(str(tmp_path / "nope.yaml")) is None
        assert lio.load_yaml_file(str(tmp_path / "nope.yaml"), default={}) == {}
        empty = tmp_path / "empty.yaml"
        empty.write_bytes(b"")
        assert lio.load_yaml_file(str(empty)) is None
        assert lio.load_yaml_file(str(empty), default=[]) == []
        assert lio.load_yaml_file(None) is None
        assert lio.load_yaml_file("") is None

    def test_oserror_stays_oserror(self, tmp_path):
        """IO failure is not a YAML failure; the wrapper must not eat it."""
        if os.name != "posix" or os.geteuid() == 0:
            pytest.skip("needs a non-root POSIX user for a chmod-000 read failure")
        p = tmp_path / "alpha.yaml"
        p.write_bytes(CLEAN_DOC)
        p.chmod(0)
        try:
            with pytest.raises(OSError) as ei:
                lio.load_yaml_file(str(p))
            assert not isinstance(ei.value, yaml.YAMLError)
        finally:
            p.chmod(0o600)

    def test_load_tenant_configs_propagates_the_named_error(self, tmp_path):
        """The hub six tools read through: it must not fail OPEN either."""
        (tmp_path / "alpha.yaml").write_bytes(XFF_DOC)
        (tmp_path / "beta.yaml").write_bytes(b"tenants:\n  beta:\n    cpu_usage: 85\n")
        with pytest.raises(lio.YamlFileError) as ei:
            lio.load_tenant_configs(str(tmp_path))
        assert "alpha.yaml" in str(ei.value)


class TestExitOnYamlFileErrorWrapper:
    def test_maps_to_caller_error_naming_the_path(self, capsys):
        @lio.exit_on_yaml_file_error
        def main():
            raise lio.YamlFileError("conf.d/alpha.yaml",
                                    yaml.reader.ReaderError("<byte string>", 9, 0xFF,
                                                            "utf8", "invalid start byte"))
        with pytest.raises(SystemExit) as ei:
            main()
        assert ei.value.code == EXIT_CALLER_ERROR
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "ERROR: cannot read conf.d/alpha.yaml: " in captured.err
        assert "ReaderError" in captured.err
        assert "Traceback" not in captured.err

    def test_passes_return_value_and_other_exceptions_through(self):
        @lio.exit_on_yaml_file_error
        def ok():
            return 7

        @lio.exit_on_yaml_file_error
        def boom():
            raise KeyError("x")

        assert ok() == 7
        assert ok.__name__ == "ok"
        with pytest.raises(KeyError):
            boom()

    def test_plain_yaml_error_is_not_its_business(self):
        """Only the file-attached class: a bare YAMLError has no path to name,
        and hiding it behind 'cannot read' would be a lie about which layer
        failed."""
        @lio.exit_on_yaml_file_error
        def boom():
            raise yaml.YAMLError("bare")
        with pytest.raises(yaml.YAMLError):
            boom()
