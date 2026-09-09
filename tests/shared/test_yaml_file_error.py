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

* **The helper names the file.** ``load_yaml_file`` now reads bytes,
  decodes them as strict UTF-8, and wraps the ``UnicodeDecodeError`` or any
  ``YAMLError`` in :class:`YamlFileError` (itself a ``YAMLError``) whose
  ``str()`` starts with the path. ``UnicodeDecodeError`` carries a
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

Decoding stays STRICT UTF-8 (what text mode did): the helper must not
start serving UTF-16 while ``validate_config``'s own reads and the routes
generator still refuse it — that would be one file, two answers inside the
Python tool family (blind review). The exporter's parser (``yaml.v3``)
does read UTF-16-with-BOM; whether the family should follow is a separate
decision, pinned here as the rejected-and-named row.
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
        assert isinstance(exc.__cause__, UnicodeDecodeError), (
            "strict UTF-8 decode: the cause is the UnicodeDecodeError, now with a path")
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

    def test_utf16_with_bom_is_rejected_and_named(self, tmp_path):
        """Text mode raised a bare UnicodeDecodeError here; the helper still
        refuses the file (strict UTF-8, same accepted encodings as before)
        but now names it. Not served: `validate_config` and the routes
        generator read in text mode and would reject the same file.
        """
        p = tmp_path / "alpha.yaml"
        p.write_bytes(UTF16_DOC)
        with pytest.raises(lio.YamlFileError) as ei:
            lio.load_yaml_file(str(p))
        assert str(p) in str(ei.value)
        assert isinstance(ei.value.cause, UnicodeDecodeError)

    def test_broken_yaml_mark_names_the_real_file(self, tmp_path):
        """PyYAML's own mark says `in "<path>"`, not `"<unicode string>"`."""
        p = tmp_path / "alpha.yaml"
        p.write_text("a: [\n", encoding="utf-8")
        with pytest.raises(lio.YamlFileError) as ei:
            lio.load_yaml_file(str(p))
        assert f'in "{p}"' in str(ei.value.cause)
        assert "<unicode string>" not in str(ei.value)

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

    def test_path_is_escaped_as_an_untrusted_label(self, capsys):
        """#1538: the path in the message is a filename someone else chose.
        Found by tests/shared/test_output_label_escaping.py on the first
        cut of this wrapper — a ``\\x1b[31m`` in the name reached stderr raw."""
        @lio.exit_on_yaml_file_error
        def main():
            raise lio.YamlFileError("conf.d/\x1b[31mred\x1b[0m.yaml",
                                    yaml.YAMLError("bad"))
        with pytest.raises(SystemExit):
            main()
        err = capsys.readouterr().err
        assert "\x1b" not in err and "?[31mred?[0m.yaml" in err, err

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


# ---------------------------------------------------------------------------
# Tool level — real binaries, one \xff byte in the relevant input
# ---------------------------------------------------------------------------
OPS = TOOLS / "ops"
LINT = TOOLS / "lint"
_TIMEOUT = 120
_PROM = "http://127.0.0.1:9"   # nothing listens; every row here is Prometheus-free or skips
BETA_DOC = b"tenants:\n  beta:\n    cpu_usage: 85\n"
DEFAULTS_DOC = b"defaults:\n  cpu_usage: 90\n"
MAPPING_DOC = (b"instance_tenant_mapping:\n  db1:\n"
               b"    - tenant: alpha\n      filter: 'db=\"a\"'\n")
POLICY_DOC = (b"policies:\n  - name: routing-required\n    description: r\n"
              b"    target: _routing\n    operator: required\n    severity: error\n")
AM_DOC = (b"route:\n  receiver: default\n  routes:\n    - match:\n        tenant: alpha\n"
          b"      receiver: alpha-hook\nreceivers:\n  - name: default\n  - name: alpha-hook\n"
          b"    webhook_configs:\n      - url: https://hooks.example.com/alpha\n")


def _dirty(doc: bytes) -> bytes:
    return b"# legacy \xff marker\n" + doc


def _run(script: Path, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    # DA_LANG pinned: policy_engine's handler is bilingual and prints
    # 無法讀取 under a zh locale, which the "cannot read" assertions below
    # would miss (blind review).
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PROMETHEUS_URL=_PROM, DA_LANG="en",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(
        [sys.executable, "-s", str(script), *argv],
        capture_output=True, timeout=_TIMEOUT, encoding="utf-8", errors="replace",
        cwd=str(cwd) if cwd else None, env=env)


@pytest.fixture(scope="module")
def fx(tmp_path_factory) -> dict[str, Path]:
    """One tree per shape; ``bad`` variants differ from ``ok`` by one byte."""
    root = tmp_path_factory.mktemp("axis6")

    def tree(name: str, files: dict[str, bytes]) -> Path:
        d = root / name
        d.mkdir()
        for fn, content in files.items():
            (d / fn).write_bytes(content)
        return d

    out = {
        "confd_ok": tree("confd_ok", {"alpha.yaml": XFF_DOC.replace(b"# legacy \xff marker\n", b""),
                                      "beta.yaml": BETA_DOC, "_defaults.yaml": DEFAULTS_DOC}),
        "confd_bad": tree("confd_bad", {"alpha.yaml": XFF_DOC, "beta.yaml": BETA_DOC,
                                        "_defaults.yaml": DEFAULTS_DOC}),
        "confd_bad_defaults": tree("confd_bad_defaults", {"alpha.yaml": CLEAN_DOC,
                                                          "_defaults.yaml": _dirty(DEFAULTS_DOC)}),
        "map_ok": tree("map_ok", {"alpha.yaml": CLEAN_DOC, "_instance_mapping.yaml": MAPPING_DOC}),
        "map_bad": tree("map_bad", {"alpha.yaml": CLEAN_DOC,
                                    "_instance_mapping.yaml": _dirty(MAPPING_DOC)}),
        "map_bad_tenant": tree("map_bad_tenant", {"alpha.yaml": XFF_DOC,
                                                  "_instance_mapping.yaml": MAPPING_DOC}),
    }
    files = root / "files"
    files.mkdir()
    for name, doc in (("policy", POLICY_DOC), ("alertmanager", AM_DOC),
                      ("metric-dictionary", b"cpu_usage:\n  maps_to: cpu_usage\n")):
        (files / f"{name}.yaml").write_bytes(doc)
        (files / f"{name}.bad.yaml").write_bytes(_dirty(doc))
    out["files"] = files

    # --git-diff needs a repo whose HEAD~1..HEAD touches conf.d/ (ticket recipe)
    def repo(name: str, doc: bytes) -> Path:
        r = root / name
        (r / "conf.d").mkdir(parents=True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

        def g(*a: str) -> None:
            subprocess.run(["git", "-C", str(r), *a], check=True,
                           capture_output=True, timeout=_TIMEOUT, env=env)
        g("init", "-q")
        (r / "conf.d" / "alpha.yaml").write_bytes(doc + b"    mem_usage: 70\n")
        g("add", "-A"); g("commit", "-qm", "before")
        (r / "conf.d" / "alpha.yaml").write_bytes(doc)
        g("add", "-A"); g("commit", "-qm", "after")
        return r
    out["repo_ok"] = repo("repo_ok", CLEAN_DOC)
    out["repo_bad"] = repo("repo_bad", XFF_DOC)
    return out


# (id, script, argv builder, cwd key or None, file the message must name, control argv builder)
# Every bad row: rc 2, stderr names the file, no traceback. Every control row:
# same invocation on the clean twin → the rc stated in the last column.
_ROWS = [
    ("check_routing_profiles", LINT / "check_routing_profiles.py",
     lambda f: ["--config-dir", str(f["confd_bad"])], None, "alpha.yaml",
     lambda f: ["--config-dir", str(f["confd_ok"])], EXIT_OK),
    ("analyze_gaps --tenant-config", OPS / "analyze_rule_pack_gaps.py",
     lambda f: ["--tenant-config", str(f["confd_bad"] / "alpha.yaml")], None, "alpha.yaml",
     lambda f: ["--tenant-config", str(f["confd_ok"] / "alpha.yaml")], EXIT_OK),
    ("analyze_gaps --metric-dictionary", OPS / "analyze_rule_pack_gaps.py",
     lambda f: ["--config-dir", str(f["confd_ok"]),
                "--metric-dictionary", str(f["files"] / "metric-dictionary.bad.yaml")],
     None, "metric-dictionary.bad.yaml",
     lambda f: ["--config-dir", str(f["confd_ok"]),
                "--metric-dictionary", str(f["files"] / "metric-dictionary.yaml")], EXIT_OK),
    ("gen_tenant_mapping mapping", OPS / "generate_tenant_mapping_rules.py",
     lambda f: ["--config-dir", str(f["map_bad"]), "--metrics", "cpu_usage", "--dry-run"],
     None, "_instance_mapping.yaml",
     lambda f: ["--config-dir", str(f["map_ok"]), "--metrics", "cpu_usage", "--dry-run"], EXIT_OK),
    ("gen_tenant_mapping tenant", OPS / "generate_tenant_mapping_rules.py",
     lambda f: ["--config-dir", str(f["map_bad_tenant"]), "--metrics", "cpu_usage", "--dry-run",
                "--validate"], None, "alpha.yaml",
     lambda f: ["--config-dir", str(f["map_ok"]), "--metrics", "cpu_usage", "--dry-run",
                "--validate"], EXIT_OK),
    ("onboard --alertmanager-config", OPS / "onboard_platform.py",
     lambda f: ["--alertmanager-config", str(f["files"] / "alertmanager.bad.yaml"), "--dry-run"],
     None, "alertmanager.bad.yaml",
     lambda f: ["--alertmanager-config", str(f["files"] / "alertmanager.yaml"), "--dry-run"], EXIT_OK),
    ("policy_opa_bridge --dry-run", OPS / "policy_opa_bridge.py",
     lambda f: ["--config-dir", str(f["confd_bad"]), "--dry-run"], None, "alpha.yaml",
     lambda f: ["--config-dir", str(f["confd_ok"]), "--dry-run"], EXIT_OK),
    ("policy_opa_bridge _defaults", OPS / "policy_opa_bridge.py",
     lambda f: ["--config-dir", str(f["confd_bad_defaults"]), "--dry-run"], None, "_defaults.yaml",
     lambda f: ["--config-dir", str(f["confd_ok"]), "--dry-run"], EXIT_OK),
    ("blind_spot_discovery", OPS / "blind_spot_discovery.py",
     lambda f: ["--config-dir", str(f["confd_bad"]), "--prometheus", _PROM], None, "alpha.yaml",
     lambda f: ["--config-dir", str(f["confd_ok"]), "--prometheus", _PROM], EXIT_OK),
    ("notification_tester --dry-run", OPS / "notification_tester.py",
     lambda f: ["--config-dir", str(f["confd_bad"]), "--dry-run"], None, "alpha.yaml",
     lambda f: ["--config-dir", str(f["confd_ok"]), "--dry-run"], EXIT_OK),
    ("maintenance_scheduler --dry-run", OPS / "maintenance_scheduler.py",
     lambda f: ["--config-dir", str(f["confd_bad"]), "--dry-run"], None, "alpha.yaml",
     lambda f: ["--config-dir", str(f["confd_ok"]), "--dry-run"], EXIT_OK),
    ("threshold_recommend", OPS / "threshold_recommend.py",
     lambda f: ["--config-dir", str(f["confd_bad"]), "--prometheus", _PROM], None, "alpha.yaml",
     lambda f: ["--config-dir", str(f["confd_ok"]), "--prometheus", _PROM], EXIT_OK),
    # envelope tools: their own load-site handlers, not the decorator
    ("backtest --git-diff", OPS / "backtest_threshold.py",
     lambda f: ["--git-diff", "--prometheus", _PROM, "--skip-if-unavailable"], "repo_bad",
     "conf.d/alpha.yaml",
     lambda f: ["--git-diff", "--prometheus", _PROM, "--skip-if-unavailable"], EXIT_OK),
    ("backtest --config-dir", OPS / "backtest_threshold.py",
     lambda f: ["--config-dir", str(f["confd_bad"]), "--baseline", str(f["confd_ok"]),
                "--prometheus", _PROM, "--skip-if-unavailable"], None, "alpha.yaml",
     lambda f: ["--config-dir", str(f["confd_ok"]), "--baseline", str(f["confd_ok"]),
                "--prometheus", _PROM, "--skip-if-unavailable"], EXIT_OK),
    ("policy_engine _defaults", OPS / "policy_engine.py",
     lambda f: ["--config-dir", str(f["confd_bad_defaults"])], None, "_defaults.yaml",
     lambda f: ["--config-dir", str(f["confd_ok"]), "--policy", str(f["files"] / "policy.yaml")],
     EXIT_OK),
    ("policy_engine tenant", OPS / "policy_engine.py",
     lambda f: ["--config-dir", str(f["confd_bad"]), "--policy", str(f["files"] / "policy.yaml")],
     None, "alpha.yaml",
     lambda f: ["--config-dir", str(f["confd_ok"]), "--policy", str(f["files"] / "policy.yaml")],
     EXIT_OK),
    # existing catch-all: rc was already 2, the file name is what is new
    ("config_diff", OPS / "config_diff.py",
     lambda f: ["--old-dir", str(f["confd_ok"]), "--new-dir", str(f["confd_bad"])], None,
     "alpha.yaml",
     lambda f: ["--old-dir", str(f["confd_ok"]), "--new-dir", str(f["confd_ok"])], EXIT_OK),
]
_ROW_IDS = [r[0] for r in _ROWS]

def _expected_bad_rc(script: Path) -> int:
    """Derived from the tool's CLASS, not a per-label table (re-review).

    A LINT (``scripts/tools/lint/``) isolates per file (#1008): one
    unreadable file is one ERROR finding — ``EXIT_VIOLATION`` — and the
    other files are still checked. A CLI tool that cannot read its input is
    a caller error — ``EXIT_CALLER_ERROR``. A new lint tool added to
    ``_ROWS`` gets the lint answer without anyone editing a dict.
    """
    return EXIT_VIOLATION if script.parent == LINT else EXIT_CALLER_ERROR


@pytest.mark.parametrize("label, script, bad, cwd, named, ctrl, ctrl_rc", _ROWS, ids=_ROW_IDS)
def test_tool_names_the_unreadable_file_with_its_class_rc(fx, label, script, bad, cwd, named, ctrl, ctrl_rc):
    p = _run(script, bad(fx), fx[cwd] if cwd else None)
    expected_rc = _expected_bad_rc(script)
    assert p.returncode == expected_rc, (
        f"{label}: rc {p.returncode}, expected {expected_rc} "
        f"({'lint finding' if expected_rc == EXIT_VIOLATION else 'caller error'}); an "
        f"uncaught traceback also exits 1, so the traceback check below is what tells "
        f"a lint finding from a crash.\nstderr={p.stderr[-500:]!r}")
    assert "Traceback" not in p.stderr, f"{label}: {p.stderr[-500:]!r}"
    assert named in p.stderr, f"{label}: stderr must name the file; got {p.stderr[-500:]!r}"
    assert "cannot read" in p.stderr or "cannot compare" in p.stderr, p.stderr[-500:]
    assert "UnicodeDecodeError" in p.stderr, "the cause class tells apart bad bytes from bad syntax"


@pytest.mark.parametrize("label, script, bad, cwd, named, ctrl, ctrl_rc", _ROWS, ids=_ROW_IDS)
def test_control_clean_bytes_keep_the_normal_rc(fx, label, script, bad, cwd, named, ctrl, ctrl_rc):
    """Same fixture minus the bad byte. Every row here measured 0 on the
    clean twin (Prometheus-dependent tools either skip or warn and still
    report). Without this row the test above cannot tell 'named the bad
    file' from 'every input is called unreadable'."""
    p = _run(script, ctrl(fx), fx[cwd.replace("bad", "ok")] if cwd else None)
    assert p.returncode == ctrl_rc, f"{label}: control rc {p.returncode}\nstderr={p.stderr[-500:]!r}"
    assert "cannot read" not in p.stderr and "Traceback" not in p.stderr, p.stderr[-500:]


@pytest.mark.parametrize("label, script, argv, cwd, reason", [
    ("backtest --git-diff", OPS / "backtest_threshold.py",
     lambda f: ["--git-diff", "--prometheus", _PROM, "--json", "--skip-if-unavailable"],
     "repo_bad", "conf_file_unreadable"),
    ("policy_engine tenant", OPS / "policy_engine.py",
     lambda f: ["--config-dir", str(f["confd_bad"]), "--policy",
                str(f["files"] / "policy.yaml"), "--json"], None, "yaml_file_unreadable"),
    ("policy_engine _defaults", OPS / "policy_engine.py",
     lambda f: ["--config-dir", str(f["confd_bad_defaults"]), "--json"], None,
     "yaml_file_unreadable"),
], ids=["backtest --git-diff", "policy_engine tenant", "policy_engine _defaults"])
def test_json_envelope_tools_still_emit_one_document(fx, label, script, argv, cwd, reason):
    """These two tools promise --json one document on EVERY terminal path
    (their existing ``caller_error`` envelope); measured before: 0 bytes."""
    import json
    p = _run(script, argv(fx), fx[cwd] if cwd else None)
    assert p.returncode == EXIT_CALLER_ERROR, p.stderr[-500:]
    doc = json.loads(p.stdout)
    assert doc["status"] == "caller_error"
    assert doc["reason"] == reason
    assert "Traceback" not in p.stderr


@pytest.mark.parametrize("label, script, argv", [
    ("analyze_gaps --json", OPS / "analyze_rule_pack_gaps.py",
     lambda f: ["--config-dir", str(f["confd_bad"]), "--json"]),
    ("onboard --json", OPS / "onboard_platform.py",
     lambda f: ["--alertmanager-config", str(f["files"] / "alertmanager.bad.yaml"),
                "--dry-run", "--json"]),
    ("policy_opa_bridge --json", OPS / "policy_opa_bridge.py",
     lambda f: ["--config-dir", str(f["confd_bad"]), "--json"]),
], ids=["analyze_gaps", "onboard", "policy_opa_bridge"])
def test_decorated_tools_leave_stdout_empty_under_json(fx, label, script, argv):
    """No envelope convention on their caller-error paths (their existing
    ``sys.exit(EXIT_CALLER_ERROR)`` sites are stderr-only), so the wrapper
    is stderr-only too — and stdout must not carry a half-written document."""
    p = _run(script, argv(fx))
    assert p.returncode == EXIT_CALLER_ERROR, p.stderr[-500:]
    assert p.stdout == "", p.stdout[:200]


def test_backtest_baseline_side_is_wrapped_too(fx, monkeypatch, capsys):
    """``--baseline`` is read only inside ``extract_changes_from_dirs``, which
    runs after the Prometheus gate — unreachable from a subprocess without a
    Prometheus. In-process, with the gate answered True, the same file is
    named and rc is 2 (the recipe scan reads ``--config-dir`` first, so the
    bad byte sits on the baseline side here on purpose)."""
    sys.path.insert(0, str(OPS))
    import backtest_threshold as bt
    monkeypatch.setattr(bt, "prometheus_available", lambda _url: True)
    monkeypatch.setattr(sys, "argv", [
        "backtest_threshold.py", "--config-dir", str(fx["confd_ok"]),
        "--baseline", str(fx["confd_bad"]), "--prometheus", _PROM])
    with pytest.raises(SystemExit) as ei:
        bt.main()
    assert ei.value.code == EXIT_CALLER_ERROR
    err = capsys.readouterr().err
    assert "cannot read" in err and "alpha.yaml" in err, err


def test_deprecate_rule_names_and_continues(fx):
    """Class (ii): the decode failure is a NAMED finding, not a traceback. The
    tool keeps going, but an unreadable tenant file withholds the completion
    claim (#1787): rc 1 (EXIT_VIOLATION), the file named in the 下架未完成
    summary."""
    p = _run(OPS / "deprecate_rule.py", ["cpu_usage", "--config-dir", str(fx["confd_bad"])])
    assert p.returncode == EXIT_VIOLATION, p.stderr[-500:]
    assert "Traceback" not in p.stderr
    assert "alpha.yaml" in p.stdout and "無法讀取" in p.stdout, p.stdout[-500:]
    assert "下架未完成" in p.stdout, p.stdout[-500:]


def test_validate_config_unchanged_rc_1_named(fx):
    """Reads the tree itself before any helper call and already named the
    file; the exit-code meaning (1 = finding about the customer's tree) is
    pinned by tests/ops/test_validate_config.py and must not move to 2."""
    p = _run(OPS / "validate_config.py", ["--config-dir", str(fx["confd_bad"])])
    assert p.returncode == EXIT_VIOLATION, p.stderr[-500:]
    assert "Traceback" not in p.stderr
    assert "alpha.yaml" in p.stdout + p.stderr


# ---------------------------------------------------------------------------
# Regression tripwire (derivation): every lib load site is guarded or wrapped
# ---------------------------------------------------------------------------
_LIB_ROOTS = {"load_yaml_file", "load_tenant_configs"}
_LIB_MODULES = {"_lib_io", "_lib_python", "scripts.tools._lib_io", "scripts.tools._lib_python"}
# Handler spellings that catch YamlFileError (a yaml.YAMLError). Matched on
# the LAST attribute segment, so `yaml.error.YAMLError`, `_lib_io.YamlFileError`
# and `_lib_python.YamlFileError` count too (re-review: they were judged
# unguarded); a bare `except:` and `BaseException` catch everything.
_GUARD_NAMES = {"YAMLError", "YamlFileError", "Exception", "BaseException", "_INPUT_ERRORS"}
_ENTRY = "exit_on_yaml_file_error"


def _is_guard(handler: str) -> bool:
    return handler == "<bare>" or handler.rsplit(".", 1)[-1] in _GUARD_NAMES

# ⛔ Exit-locked. Modules whose lib load sites are NOT lexically guarded and
# whose main is NOT wrapped, because they guard by hand somewhere up the
# call chain — each with the function and handler that does it. May only
# SHRINK (a module moving to the decorator deletes its row); adding a row
# is adding a hand-written exception to the shared rule, and the ticket's
# whole point was one answer, not twenty-one.
_HAND_GUARDED: dict[str, tuple[str, str]] = {
    "ops/backtest_threshold.py": ("main", "YamlFileError"),   # --json envelope
    "ops/policy_engine.py": ("main", "YamlFileError"),        # --json envelope
    "ops/config_diff.py": ("main", "Exception"),              # catch-all since #1448 era
    "ops/validate_config.py": ("_run_check", "_INPUT_ERRORS"),  # dispatch wrapper (#1448)
}


def _handler_names(h: ast.ExceptHandler) -> list[str]:
    if h.type is None:
        return ["<bare>"]
    if isinstance(h.type, ast.Tuple):
        return [ast.unparse(e) for e in h.type.elts]
    return [ast.unparse(h.type)]


def _lib_aliases(tree: ast.Module) -> dict[str, str]:
    """Local names bound to the lib's load_yaml_file / load_tenant_configs."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _LIB_MODULES:
            for a in node.names:
                if a.name in _LIB_ROOTS:
                    out[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            # `from policy_engine import load_yaml_file` — a tool module
            # re-exporting the lib helper (blind review: was invisible).
            for a in node.names:
                if a.name in _LIB_ROOTS and _module_reexports_lib_root(node.module, a.name):
                    out[a.asname or a.name] = a.name
    return out


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """``import policy_engine as pe`` → {"pe": "policy_engine"}."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name] = a.name
    return out


def _module_reexports_lib_root(modname: str, attr: str) -> bool:
    """Does ``<modname>.<attr>`` resolve to the lib helper (imported, not
    defined locally)? ``gen.load_tenant_configs`` is grar's own function;
    ``pe.load_yaml_file`` is the lib's, re-exported."""
    for d in (TOOLS, OPS, LINT):
        p = d / f"{modname}.py"
        if p.is_file():
            t = ast.parse(p.read_text(encoding="utf-8"))
            defines = any(isinstance(n, ast.FunctionDef) and n.name == attr for n in t.body)
            return (not defines) and attr in _lib_aliases(t).values()
    return False


class _Sites(ast.NodeVisitor):
    def __init__(self, tree: ast.Module) -> None:
        self.lib = _lib_aliases(tree)
        self.mods = _module_aliases(tree)
        self.func: list[str] = []
        self.handlers: list[list[str]] = []
        self.sites: list[tuple[int, str, bool]] = []   # (line, func, guarded)
        self.main_wrapped = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name == "main" and not self.func:
            self.main_wrapped = any(
                (isinstance(d, ast.Name) and d.id == _ENTRY)
                or (isinstance(d, ast.Attribute) and d.attr == _ENTRY)
                for d in node.decorator_list)
        self.func.append(node.name)
        self.generic_visit(node)
        self.func.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Try(self, node: ast.Try) -> None:
        self.handlers.append([n for h in node.handlers for n in _handler_names(h)])
        for n in node.body:
            self.visit(n)
        self.handlers.pop()
        for part in (node.handlers, node.orelse, node.finalbody):
            for n in part:
                self.visit(n)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        is_root = False
        if isinstance(f, ast.Name) and f.id in self.lib:
            is_root = True
        elif (isinstance(f, ast.Attribute) and f.attr in _LIB_ROOTS
              and isinstance(f.value, ast.Name) and f.value.id in self.mods):
            target = self.mods[f.value.id]
            # `import _lib_io as lio; lio.load_yaml_file(...)` IS the lib
            # (blind review: this shape was invisible); a tool-module alias
            # counts only when that module re-exports the lib helper.
            is_root = (target in _LIB_MODULES
                       or _module_reexports_lib_root(target, f.attr))
        if is_root:
            guarded = any(_is_guard(h) for hs in self.handlers for h in hs)
            self.sites.append((node.lineno, ".".join(self.func) or "<module>", guarded))
        self.generic_visit(node)


def _tool_modules() -> list[Path]:
    return sorted(p for d in (TOOLS, OPS, LINT, TOOLS / "dx") if d.is_dir()
                  for p in d.glob("*.py") if not p.name.startswith("_"))


def _scan(path: Path) -> _Sites:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    v = _Sites(tree)
    v.visit(tree)
    return v


def test_scan_sees_the_known_population():
    """Anti-vacuity: the scanner must find the sites the ticket counted, or a
    green run below proves nothing. 13 modules on main at the time of the fix;
    a new consumer is fine (it will be judged), a smaller count means the
    scanner went blind."""
    found = {p.name for p in _tool_modules() if _scan(p).sites}
    expected = {
        "check_routing_profiles.py", "analyze_rule_pack_gaps.py", "backtest_threshold.py",
        "config_diff.py", "deprecate_rule.py", "generate_tenant_mapping_rules.py",
        "migrate_to_operator.py", "onboard_platform.py", "operator_generate.py",
        "policy_engine.py", "policy_opa_bridge.py", "validate_config.py",
        "blind_spot_discovery.py", "notification_tester.py", "maintenance_scheduler.py",
        "threshold_recommend.py",
    }
    assert expected <= found, sorted(expected - found)


@pytest.mark.parametrize("path", _tool_modules(), ids=lambda p: p.name)
def test_every_lib_load_site_is_guarded_or_the_entry_is_wrapped(path):
    rel = path.relative_to(TOOLS).as_posix()
    v = _scan(path)
    unguarded = [(ln, fn) for ln, fn, g in v.sites if not g]
    if rel in _HAND_GUARDED:
        assert unguarded, (
            f"{rel}: every lib load site is now lexically guarded — delete its "
            "_HAND_GUARDED row so the list keeps shrinking")
        func, handler = _HAND_GUARDED[rel]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        fdefs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func]
        assert fdefs, f"{rel}: hand-guard function {func} is gone"
        handlers = [h for n in ast.walk(fdefs[0]) if isinstance(n, ast.Try)
                    for hh in n.handlers for h in _handler_names(hh)]
        assert handler in handlers, (
            f"{rel}: {func} no longer catches {handler} (has {handlers}); the sites "
            f"at {unguarded} would traceback again")
        if handler == "_INPUT_ERRORS":
            src = path.read_text(encoding="utf-8")
            assert "_INPUT_ERRORS = (yaml.YAMLError" in src, (
                f"{rel}: _INPUT_ERRORS no longer starts with yaml.YAMLError")
        return
    if unguarded:
        assert v.main_wrapped, (
            f"{rel}: lib load site(s) {unguarded} have no handler that catches "
            f"yaml.YAMLError / YamlFileError / Exception, and main() is not "
            f"decorated with @{_ENTRY} — a \\xff byte in that input is a traceback "
            "again (#1654). Wrap the entry, or add a handler at the site.")


def test_hand_guarded_list_only_shrinks():
    """The lock itself. Each row must still exist AND still need the row; the
    set may lose members, never gain them without editing this literal."""
    assert set(_HAND_GUARDED) == {
        "ops/backtest_threshold.py", "ops/policy_engine.py",
        "ops/config_diff.py", "ops/validate_config.py",
    }
    for rel in _HAND_GUARDED:
        assert (TOOLS / rel).is_file(), rel


# ── tripwire self-controls for the two alias shapes blind review found ─────

def _sites_of(src: str) -> list[tuple[int, str, bool]]:
    tree = ast.parse(src)
    v = _Sites(tree)
    v.visit(tree)
    return v.sites


def test_scanner_sees_module_alias_attribute_calls():
    """`import _lib_io as lio; lio.load_yaml_file(p)` is a lib site."""
    assert _sites_of("import _lib_io as lio\ndef f(p):\n    return lio.load_yaml_file(p)\n")
    assert _sites_of("import _lib_python\ndef f(p):\n    return _lib_python.load_yaml_file(p)\n")


@pytest.mark.parametrize("handler", [
    "except:", "except BaseException:", "except Exception:",
    "except yaml.error.YAMLError:", "except _lib_io.YamlFileError:",
    "except _lib_python.YamlFileError:", "except (OSError, yaml.YAMLError):",
])
def test_scanner_accepts_every_handler_spelling_that_catches_it(handler):
    """Each of these catches a YamlFileError at runtime; the first cut of the
    scanner matched whole strings and called the dotted ones unguarded.

    ⚠️ Latent alias gaps, recorded and NOT closed here (no code in the tree
    uses them; a wrong classifier reads as a rule): ``from ops import
    policy_engine as pe`` (package-qualified module import — `_module_aliases`
    only sees `import X as y`), ``from scripts.tools import _lib_io`` (the
    lib module bound by ImportFrom, not Import), and ``getattr(mod,
    "load_yaml_file")(p)`` (no static name at all). A site reached through
    one of those is invisible, i.e. silently passes — the scanner errs open
    on shapes it cannot see, which is the direction to remember."""
    src = ("import yaml\nimport _lib_io\nimport _lib_python\n"
           "from _lib_io import load_yaml_file\n"
           f"def f(p):\n    try:\n        return load_yaml_file(p)\n    {handler}\n        return None\n")
    sites = _sites_of(src)
    assert sites and all(g for _, _, g in sites), (handler, sites)


def test_scanner_still_flags_a_handler_that_does_not_catch_it():
    """Control for the test above: `except OSError` / `except KeyError` are
    not guards, and neither is a dotted name ending in something else."""
    for handler in ("except OSError:", "except (KeyError, ValueError):", "except yaml.reader.ReaderError:"):
        src = ("import yaml\nfrom _lib_io import load_yaml_file\n"
               f"def f(p):\n    try:\n        return load_yaml_file(p)\n    {handler}\n        return None\n")
        sites = _sites_of(src)
        assert sites and not any(g for _, _, g in sites), (handler, sites)


def test_scanner_sees_reexport_through_a_tool_module():
    """`from policy_engine import load_yaml_file` resolves to the lib helper
    (policy_engine imports it and does not define its own)."""
    assert _sites_of("from policy_engine import load_yaml_file\ndef f(p):\n    return load_yaml_file(p)\n")
    # control: a tool's OWN function of the same name is not the lib
    assert not _sites_of("from generate_alertmanager_routes import load_tenant_configs\ndef f(p):\n    return load_tenant_configs(p)\n")


# ── per-file isolation kept where the tool already had it (blind review) ──

def test_onboard_rule_file_loop_still_isolates_per_file(tmp_path):
    """`onboard_platform.analyze_rule_files` had an `errors.append(...);
    continue` branch for a file it could not load; the entry-level rc-2
    wrapper must not make that branch unreachable. One bad rule file is one
    error line and the good file is still analysed."""
    sys.path.insert(0, str(OPS))
    import onboard_platform as ob
    bad = tmp_path / "bad.yaml"
    bad.write_bytes(b"groups:\n  - name: \xff\n")
    good = tmp_path / "good.yaml"
    good.write_text("groups:\n  - name: g\n    rules:\n      - record: r\n        expr: up\n",
                    encoding="utf-8")
    _c, recording, summary = ob.analyze_rule_files([str(bad), str(good)])
    assert any(str(bad) in e for e in summary["errors"]), summary["errors"]
    assert len(recording) == 1, recording
    # Re-review: isolation names every bad file, but the RUN must not end
    # 0 — main turns a non-empty summary["errors"] into rc 2 after writing.
    assert "Failed to load" in summary["errors"][0]


_RULE_GOOD = b"groups:\n  - name: g\n    rules:\n      - alert: A\n        expr: up > 1\n"
_RULE_BAD = b"groups:\n  - name: \xff\n"


@pytest.mark.parametrize("shape, files, rc, plan_written", [
    ("all-bad", {"a.yml": _RULE_BAD, "b.yml": _RULE_BAD}, EXIT_CALLER_ERROR, True),
    ("bad+good", {"a.yml": _RULE_BAD, "b.yml": _RULE_GOOD}, EXIT_CALLER_ERROR, True),
    ("all-good", {"a.yml": _RULE_GOOD, "b.yml": _RULE_GOOD}, EXIT_OK, True),
], ids=["all-bad", "bad+good", "all-good"])
def test_onboard_rule_files_unreadable_ends_with_rc_2_after_writing(tmp_path, shape, files, rc, plan_written):
    """The three shapes of `--rule-files` with per-file isolation: every bad
    file named in one run, the plan still written from the readable ones,
    and the rc says whether every input was read. Measured before the
    re-review fix: all-bad → rc 0 with a header-only migration-plan.csv."""
    rules = tmp_path / "rules"
    rules.mkdir()
    for name, body in files.items():
        (rules / name).write_bytes(body)
    out = tmp_path / "out"
    p = _run(OPS / "onboard_platform.py",
             ["--rule-files", str(rules / "*.yml"), "-o", str(out)])
    assert p.returncode == rc, f"{shape}: rc {p.returncode}\nstderr={p.stderr[-600:]!r}"
    assert "Traceback" not in p.stderr
    assert any(out.rglob("migration-plan.csv")) == plan_written, \
        sorted(str(f) for f in out.rglob("*"))
    bad_names = [n for n, b in files.items() if b is _RULE_BAD]
    if bad_names:
        assert f"ERROR: {len(bad_names)} rule file(s) could not be read" in p.stderr, p.stderr[-600:]
        for n in bad_names:
            assert n in p.stderr
    else:
        assert "could not be read" not in p.stderr


def test_onboard_rule_files_json_keeps_envelope_with_errors(tmp_path):
    import json
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "a.yml").write_bytes(_RULE_BAD)
    (rules / "b.yml").write_bytes(_RULE_GOOD)
    p = _run(OPS / "onboard_platform.py",
             ["--rule-files", str(rules / "*.yml"), "-o", str(tmp_path / "out"), "--json"])
    assert p.returncode == EXIT_CALLER_ERROR, p.stderr[-600:]
    doc = json.loads(p.stdout)
    errors = doc["phases"]["phase2"]["summary"]["errors"]
    assert len(errors) == 1 and "a.yml" in errors[0], errors


def test_routing_lint_reports_the_bad_file_and_still_reads_the_rest(tmp_path):
    """A lint isolates per file (#1008): the unreadable file is one ERROR
    finding (rc 1) and the other control files are still collected."""
    sys.path.insert(0, str(LINT))
    import check_routing_profiles as crp
    confd = tmp_path / "conf.d"
    confd.mkdir()
    (confd / "_routing_profiles.yaml").write_text(
        "routing_profiles:\n  standard:\n    receiver: r\n", encoding="utf-8")
    (confd / "alpha.yaml").write_bytes(b"# \xff\ntenants:\n  alpha: {}\n")
    data = crp._collect_data(str(confd))
    assert "standard" in data["profiles"], data
    assert len(data["unreadable"]) == 1 and "alpha.yaml" in data["unreadable"][0], data["unreadable"]
