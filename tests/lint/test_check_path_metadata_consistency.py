#!/usr/bin/env python3
"""test_check_path_metadata_consistency — A-9 柔性一致性警告測試。

覆蓋：
  1. 路徑推斷正確（domain = 第一層；environment = allowlist 命中）
  2. _metadata 與 path 不符時發出警告（exit 0）
  3. _metadata 缺欄位時不警告
  4. `_*.yaml` 檔案被略過
  5. 扁平配置（無階層路徑）不警告
  6. 大小寫比對（prod == PROD）
  7. CI 模式輸出格式
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import check_path_metadata_consistency as cpmc  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _tenant_yaml(
    tenant_id: str,
    *,
    domain: str | None = None,
    region: str | None = None,
    environment: str | None = None,
) -> str:
    meta_lines: list[str] = []
    if domain is not None:
        meta_lines.append(f"      domain: {domain}")
    if region is not None:
        meta_lines.append(f"      region: {region}")
    if environment is not None:
        meta_lines.append(f"      environment: {environment}")
    meta_block = ""
    if meta_lines:
        meta_block = "    _metadata:\n" + "\n".join(meta_lines) + "\n"
    return (
        "tenants:\n"
        f"  {tenant_id}:\n"
        f"{meta_block}"
        "    threshold:\n"
        "      cpu: 80\n"
    )


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    repo_root: Path,
    *args: str,
) -> tuple[int, str, str]:
    """Invoke main() in-process so coverage.py captures execution."""
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(
        cpmc.sys, "argv",
        ["check_path_metadata_consistency.py", *args],
    )
    try:
        exit_code = cpmc.main()
    except SystemExit as exc:
        exit_code = int(exc.code) if exc.code is not None else 0
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


# ── TestPathInferences ─────────────────────────────────────────────────


class TestPathInferences:
    def test_first_segment_is_domain(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        inferred = cpmc._path_inferences(f, config_dir)
        assert inferred["domain"] == "db"

    def test_environment_from_allowlisted_segment(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        inferred = cpmc._path_inferences(f, config_dir)
        assert inferred["environment"] == "prod"

    def test_no_environment_inference_for_non_allowlist(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "t.yaml"
        inferred = cpmc._path_inferences(f, config_dir)
        assert "environment" not in inferred

    def test_flat_file_has_no_inferences(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        config_dir.mkdir(parents=True)
        f = config_dir / "flat.yaml"
        inferred = cpmc._path_inferences(f, config_dir)
        assert inferred == {}


# ── TestScanFile ───────────────────────────────────────────────────────


class TestScanFile:
    def test_warns_on_environment_mismatch(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        _write(f, _tenant_yaml("t-prod", environment="staging"))
        mismatches = cpmc.scan_file(f, config_dir)
        assert len(mismatches) == 1
        m = mismatches[0]
        assert m.field == "environment"
        assert m.path_value == "prod"
        assert m.metadata_value == "staging"

    def test_warns_on_domain_mismatch(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        _write(f, _tenant_yaml("t", domain="web", environment="prod"))
        mismatches = cpmc.scan_file(f, config_dir)
        # environment matches (prod == prod) → only domain mismatch.
        assert len(mismatches) == 1
        assert mismatches[0].field == "domain"

    def test_no_warning_when_aligned(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        _write(f, _tenant_yaml("t", domain="db", environment="prod"))
        assert cpmc.scan_file(f, config_dir) == []

    def test_no_warning_when_metadata_missing(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        _write(f, _tenant_yaml("t"))  # no _metadata at all
        assert cpmc.scan_file(f, config_dir) == []

    def test_case_insensitive_match(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        _write(f, _tenant_yaml("t", domain="DB", environment="PROD"))
        assert cpmc.scan_file(f, config_dir) == []

    def test_malformed_yaml_is_silent(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "prod" / "bad.yaml"
        _write(f, "tenants:\n  - not-a-map\n    x: [unclosed")
        assert cpmc.scan_file(f, config_dir) == []

    def test_region_field_never_warns_without_path_inference(
        self, tmp_path
    ):
        """`region` is not in the path-inference heuristic (only domain
        and environment are). A tenant declaring _metadata.region must
        not produce a warning just because the path doesn't mention it.
        """
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        _write(
            f,
            _tenant_yaml(
                "t", domain="db", region="us-east-1", environment="prod"
            ),
        )
        assert cpmc.scan_file(f, config_dir) == []


# ── TestScan (full-directory) ──────────────────────────────────────────


class TestScan:
    def test_ignores_underscore_files(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        _write(
            config_dir / "db" / "prod" / "_defaults.yaml",
            "defaults:\n  _metadata:\n    environment: staging\n",
        )
        # _defaults.yaml would mismatch (path=prod, meta=staging), but
        # underscore-prefixed files must be skipped.
        assert cpmc.scan(config_dir) == []

    def test_multiple_files_aggregate(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        _write(
            config_dir / "db" / "prod" / "a.yaml",
            _tenant_yaml("a", environment="staging"),
        )
        _write(
            config_dir / "db" / "staging" / "b.yaml",
            _tenant_yaml("b", environment="prod"),
        )
        # Third file: correctly aligned.
        _write(
            config_dir / "db" / "prod" / "c.yaml",
            _tenant_yaml("c", environment="prod"),
        )
        mismatches = cpmc.scan(config_dir)
        assert len(mismatches) == 2
        tenants = {m.tenant for m in mismatches}
        assert tenants == {"a", "b"}


# ── TestCLI ────────────────────────────────────────────────────────────


class TestCLI:
    def test_clean_dir_exit_zero(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        config_dir = tmp_path / "conf.d"
        _write(
            config_dir / "db" / "prod" / "t.yaml",
            _tenant_yaml("t", domain="db", environment="prod"),
        )
        exit_code, stdout, _ = _run_cli(
            monkeypatch, capsys, tmp_path,
            "--config-dir", str(config_dir),
        )
        assert exit_code == 0
        assert "0 mismatch(es)" in stdout

    def test_warning_still_exits_zero(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        config_dir = tmp_path / "conf.d"
        _write(
            config_dir / "db" / "prod" / "t.yaml",
            _tenant_yaml("t", environment="staging"),
        )
        exit_code, stdout, stderr = _run_cli(
            monkeypatch, capsys, tmp_path,
            "--config-dir", str(config_dir),
        )
        assert exit_code == 0
        assert "WARN path/metadata mismatch" in stdout
        assert "1 mismatch(es)" in stderr

    def test_ci_mode_single_line_format(
        self, tmp_path, monkeypatch, capsys
    ):
        (tmp_path / ".git").mkdir()
        config_dir = tmp_path / "conf.d"
        _write(
            config_dir / "db" / "prod" / "t.yaml",
            _tenant_yaml("t", environment="staging"),
        )
        exit_code, stdout, _ = _run_cli(
            monkeypatch, capsys, tmp_path,
            "--config-dir", str(config_dir), "--ci",
        )
        assert exit_code == 0
        assert ":0: warning: path/metadata mismatch" in stdout
        assert "tenant=t" in stdout
        assert "field=environment" in stdout

    def test_missing_config_dir_is_soft(
        self, tmp_path, monkeypatch, capsys
    ):
        (tmp_path / ".git").mkdir()
        exit_code, _, stderr = _run_cli(
            monkeypatch, capsys, tmp_path,
            "--config-dir", str(tmp_path / "does-not-exist"),
        )
        assert exit_code == 0
        assert "config dir not found" in stderr

    def test_default_config_dir_when_unset(
        self, tmp_path, monkeypatch, capsys
    ):
        """Without --config-dir, the tool resolves to the repo-default
        conf.d path (which may or may not exist in the test tmp_path).
        Either way, exit code must be 0 (soft fail on missing dir)."""
        (tmp_path / ".git").mkdir()
        exit_code, _, _ = _run_cli(monkeypatch, capsys, tmp_path)
        assert exit_code == 0


# ── TestExtractTenantMetadata ─────────────────────────────────────────


class TestExtractTenantMetadata:
    """Cover the defensive shape-guard branches."""

    def test_returns_empty_for_non_dict_root(self):
        assert cpmc._extract_tenant_metadata(["not", "a", "dict"]) == {}

    def test_returns_empty_when_tenants_missing(self):
        assert cpmc._extract_tenant_metadata({"defaults": {}}) == {}

    def test_returns_empty_when_tenants_is_list(self):
        assert cpmc._extract_tenant_metadata({"tenants": []}) == {}

    def test_skips_non_dict_tenant_block(self):
        data = {"tenants": {"t": "not-a-dict"}}
        assert cpmc._extract_tenant_metadata(data) == {}

    def test_skips_non_dict_metadata_block(self):
        data = {"tenants": {"t": {"_metadata": "scalar"}}}
        assert cpmc._extract_tenant_metadata(data) == {}

    def test_skips_non_string_field_values(self):
        data = {"tenants": {"t": {"_metadata": {"domain": 123}}}}
        assert cpmc._extract_tenant_metadata(data) == {}

    def test_skips_empty_string_field_values(self):
        data = {"tenants": {"t": {"_metadata": {"domain": ""}}}}
        assert cpmc._extract_tenant_metadata(data) == {}


# ── TestFindRepoRoot ─────────────────────────────────────────────────


class TestFindRepoRoot:
    def test_walks_up_to_git_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert cpmc.find_repo_root() == tmp_path

    def test_fallback_when_no_git_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Must not raise; returns some directory (script-location
        # heuristic 3 levels up from the module).
        result = cpmc.find_repo_root()
        assert result.is_dir()


# ── TestScanFileOSError ──────────────────────────────────────────────


class TestScanFileOSError:
    def test_unreadable_file_returns_empty(self, tmp_path, monkeypatch):
        """OSError on read_text should be swallowed (return [])."""
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "prod" / "missing.yaml"
        # File does not exist on disk.
        assert cpmc.scan_file(f, config_dir) == []

    def test_outside_config_dir_returns_empty(self, tmp_path):
        """A file outside config_dir (ValueError on relative_to) must
        be silently skipped."""
        config_dir = tmp_path / "conf.d"
        config_dir.mkdir()
        outside = tmp_path / "outside.yaml"
        outside.write_text(_tenant_yaml("t", environment="staging"))
        assert cpmc.scan_file(outside, config_dir) == []


# ── The extension-SPELLING axis (#1603) ──────────────────────────────
#
# `iter_tenant_files` and the "not checked" report in `main` both used to
# pass `suffixes=(".yaml",)` while the exporter's scanner
# (`scanDirHierarchical`, `config_hierarchy.go`) lowercases the entry name and accepts BOTH
# `.yaml` and `.yml`. Measured on a tree whose path says `staging/` while
# `_metadata` says `prod`, contents byte-identical, extension the only
# difference:
#
#     db-a.yaml -> 2 mismatches, and it says so on stderr   <- control
#     db-a.yml  -> `0 mismatch(es) across 0 tenant file(s)`, stderr empty
#
# i.e. a lint calling a tree clean because it never opened it, at exit 0.
#
# ⚠️ SCOPE. These pin the extension-SPELLING axis only, and neither of the
# other two divergences has a ticket of its own behind it:
#   * Hidden names: CLOSED by #2081 and pinned by `TestHiddenAxis` below.
#     Both walks (`iter_tenant_files` and `main`) used `rglob("*")`, so
#     `.old.yaml`, `.snap/…` and a ConfigMap mount's `..<ts>/` payload were
#     scanned although the exporter skips them — the last one reading one
#     tenant file twice. Both now take `_lib_confd.list_config_tree`.
#   * Entries `is_file()` drops are named by `main` — that half of #1607 is
#     wired up in this module.


class TestExtensionSpellingAxis:
    """`.yml` carriers must be scanned exactly as `.yaml` ones — no more."""

    @staticmethod
    def _seed(config_dir: Path, ext: str) -> Path:
        """A conf.d whose path says `staging` and whose `_metadata` says
        `prod`, so there is a real disagreement for the lint to find."""
        _write(config_dir / f"_defaults{ext}", "defaults:\n  cpu: 100\n")
        _write(config_dir / "payments" / "staging" / f"db-a{ext}",
               _tenant_yaml("db-a", domain="search", environment="prod"))
        return config_dir

    def test_scan_agrees_across_extension_spellings(self, tmp_path):
        """FLOOR. Two trees, same bytes, different extension, same findings.

        Asserted as an EQUALITY between the two runs rather than as
        "`.yml` is accepted": the latter is satisfied by a reader that takes
        `.yml` and drops `.yaml`. The `file` field legitimately carries the
        extension, so only that is folded away.
        """
        a = cpmc.scan(self._seed(tmp_path / "yaml" / "conf.d", ".yaml"))
        b = cpmc.scan(self._seed(tmp_path / "yml" / "conf.d", ".yml"))

        def comparable(ms):
            return sorted((m.tenant, m.field, m.path_value, m.metadata_value)
                          for m in ms)

        # Vacuity guard FIRST, and on BOTH sides: two trees the lint cannot
        # read at all also compare equal. Pinned as a concrete count so that
        # gutting `comparable` cannot silently disarm the equality — that
        # erosion path was measured on the sibling fix and left it green.
        for label, ms in (("`.yaml`", a), ("`.yml`", b)):
            assert len(ms) == 2, (
                f"the {label} tree produced {len(ms)} mismatch(es), not 2, "
                f"so the equality below would prove nothing. Got {ms!r}"
            )
        assert comparable(a) == comparable(b), (
            f"the lint reports different findings for a conf.d whose only "
            f"difference is `.yaml` vs `.yml`, both of which the exporter "
            f"serves:\n  .yaml: {comparable(a)}\n  .yml : {comparable(b)}"
        )

    def test_main_names_an_unreadable_yml_carrier(
            self, tmp_path, monkeypatch, capsys):
        """FLOOR, second site: the "not checked" report must see `.yml` too.

        The equality above only exercises `iter_tenant_files`. The
        `unusable_config_entries` call in `main` is a separate site, and
        reverting it alone leaves the equality green — the same gap that
        survived the first round of the sibling fix (#1663).

        The unreadable carrier is a DIRECTORY named like a config file, not
        a broken symlink: symlink creation needs administrator rights on
        Windows, so a symlink fixture would be skipped on the host most of
        this repo's maintainers use.
        """
        for ext in (".yaml", ".yml"):
            root = tmp_path / ext.lstrip(".")
            (root / ".git").mkdir(parents=True)
            config_dir = self._seed(root / "conf.d", ext)
            (config_dir / ("db-broken" + ext)).mkdir()

            code, _out, err = _run_cli(
                monkeypatch, capsys, root,
                "--config-dir", str(config_dir), "--ci",
            )

            assert code == 0, "this lint is warning-only and must stay exit 0"
            # Name the REASON, not just the filename. The sibling tool has a
            # SECOND stderr station that prints the same basename (its
            # parse-failure handler), and blind review showed a
            # filename-only assertion there can be satisfied by the wrong
            # station. This module has no such twin today; pinning the
            # reason is what keeps this test honest if one is ever added.
            # ⚠️ Only the reason fragment — the surrounding wording is
            # deliberately unpinned everywhere else in this file.
            named = [ln for ln in err.splitlines()
                     if ("db-broken" + ext) in ln
                     and "is a directory, not a config file" in ln]
            assert len(named) == 1, (
                f"the `not checked` report must name `db-broken{ext}` "
                f"exactly once, with the reason; stderr was {err!r}"
            )

    def test_a_json_carrier_is_not_scanned(self, tmp_path):
        """CEILING, by counterexample, which is all a ceiling can be.

        You cannot enumerate the complement of an accept-set, so this pins
        counterexamples and the name says which ones. Over-widening a call
        site to `(".yaml", ".yml", ".json")` is a single token, and
        `has_yaml_extension`'s own docstring says this argument gets
        touched. Measured on this change's base (`eed193a7`): with that
        widening applied and this class absent, the CI-exact suite is
        15909 passed / 185 skipped / rc=0.

        It does NOT cover dot-prefixed names, which this reader scans and
        the exporter skips — see the SCOPE block above. The fixture
        deliberately contains no dot-prefixed name rather than pinning
        today's answer for it.
        """
        config_dir = self._seed(tmp_path / "conf.d", ".yaml")
        # JSON parses fine as YAML, so nothing but the extension rule keeps
        # it out. It declares a tenant no other file in the tree declares.
        _write(config_dir / "payments" / "staging" / "db-json.json",
               '{"tenants": {"db-json": {"_metadata": '
               '{"environment": "prod"}, "threshold": {"cpu": 80}}}}')
        # `_`-prefixed control file, never a tenant carrier.
        _write(config_dir / "payments" / "staging" / "_profiles.yaml",
               _tenant_yaml("db-reserved", environment="prod"))

        tenants = {m.tenant for m in cpmc.scan(config_dir)}
        assert tenants == {"db-a"}, (
            f"a carrier the exporter does not serve was scanned: {tenants}"
        )

    def test_every_spelling_the_shared_set_names_is_scanned(self, tmp_path):
        """FLOOR, derived: one carrier per member of `CONFIG_SUFFIXES`.

        The tests above hard-code `.yaml` and `.yml`, so they stop covering
        the floor the day the exporter grows a third spelling and
        `_lib_confd.CONFIG_SUFFIXES` follows it. This reads the set instead
        of restating it.

        It does NOT re-implement `has_yaml_extension`; it uses the shared
        CONSTANT, whose agreement with the exporter is pinned by
        `tests/shared/confd_name_classification_matrix.json` (asserted from
        the Go side by `confd_name_classification_parity_test.go` and from
        the Python side by
        `tests/shared/test_confd_name_classification_parity.py`).

        Floor only: a too-WIDE reader satisfies it, which is what the
        counterexample test above is for.
        """
        from _lib_confd import CONFIG_SUFFIXES  # noqa: PLC0415

        # Anti-vacuity: an empty set makes the assertion `set() == set()`.
        # Guarding the set is not this test's job, so name who does.
        assert len(CONFIG_SUFFIXES) >= 2, (
            f"CONFIG_SUFFIXES collapsed to {CONFIG_SUFFIXES!r}; the shared "
            f"classification matrix should have gone red first"
        )

        config_dir = tmp_path / "conf.d"
        _write(config_dir / "_defaults.yaml", "defaults:\n  cpu: 100\n")
        for i, suffix in enumerate(CONFIG_SUFFIXES):
            _write(config_dir / "payments" / "staging" / f"t{i}{suffix}",
                   _tenant_yaml(f"t{i}", environment="prod"))

        tenants = {m.tenant for m in cpmc.scan(config_dir)}
        expected = {f"t{i}" for i in range(len(CONFIG_SUFFIXES))}
        assert tenants == expected, (
            f"one carrier was written per member of CONFIG_SUFFIXES "
            f"({CONFIG_SUFFIXES!r}) and the lint scanned {tenants}, "
            f"expected {expected}"
        )


# ── The HIDDEN-name axis (#2081) ─────────────────────────────────────


def _symlink_or_skip(link: Path, target: str) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover
        pytest.skip(f"cannot create symlink here: {exc}")


class TestHiddenAxis:
    """Only what the exporter's walker reads is scanned — no hidden paths.

    `pkg/config.ScanDirTree` prunes `.`-prefixed directories, drops
    `.`-prefixed files and never descends a directory symlink. Every case
    here carries its 必響對照: the SAME bytes under a non-hidden name must
    still be reported, so a lint that stopped reading anything at all
    cannot pass these.
    """

    OK = _tenant_yaml("acme", environment="staging")
    BAD = _tenant_yaml("acme", environment="prod")

    def _seed(self, config_dir: Path, shape: str, *, hidden: bool) -> Path:
        dot = "." if hidden else ""
        _write(config_dir / "staging" / "acme.yaml", self.OK)
        if shape == "file":
            _write(config_dir / "staging" / f"{dot}old.yaml", self.BAD)
        else:
            _write(config_dir / "staging" / f"{dot}snap" / "acme.yaml",
                   self.BAD)
        return config_dir

    @pytest.mark.parametrize("shape", ["file", "dir"])
    def test_hidden_carrier_is_not_scanned(
        self, shape, tmp_path, monkeypatch, capsys,
    ):
        (tmp_path / ".git").mkdir()
        vis = self._seed(tmp_path / "vis" / "conf.d", shape, hidden=False)
        hid = self._seed(tmp_path / "hid" / "conf.d", shape, hidden=True)

        # 必響對照 FIRST: same bytes, visible name -> reported, both entry
        # points. Without it the assertions below prove nothing.
        assert len(cpmc.scan(vis)) == 1
        _, out, err = _run_cli(monkeypatch, capsys, tmp_path,
                               "--config-dir", str(vis), "--ci")
        assert "1 mismatch(es) across 2 tenant file(s)" in err, (out, err)

        # `scan` goes through `iter_tenant_files`' own walk ...
        assert cpmc.scan(hid) == []
        assert [p.name for p in cpmc.iter_tenant_files(hid)] == ["acme.yaml"]
        # ... and `main` through its single walk. Both must agree.
        _, out, err = _run_cli(monkeypatch, capsys, tmp_path,
                               "--config-dir", str(hid), "--ci")
        assert "0 mismatch(es) across 1 tenant file(s)" in out, (out, err)
        assert "warning:" not in out + err

    def test_configmap_mount_payload_is_read_once(
        self, tmp_path, monkeypatch, capsys,
    ):
        """kubelet's ConfigMap layout: `..<ts>/` payload, `..data` -> it,
        `acme.yaml` -> `..data/acme.yaml`. The exporter reads the one
        tenant file via the root link only; `rglob` read it twice."""
        (tmp_path / ".git").mkdir()

        def mount(config_dir: Path, payload: str, data: str) -> Path:
            _write(config_dir / "staging" / payload / "acme.yaml", self.BAD)
            _symlink_or_skip(config_dir / "staging" / data, payload)
            _symlink_or_skip(config_dir / "staging" / "acme.yaml",
                             f"{data}/acme.yaml")
            return config_dir

        cm = mount(tmp_path / "cm" / "conf.d", "..2026_09_25", "..data")
        # 必響對照: payload directory NOT hidden -> it is a real directory
        # the exporter does read, so both copies are reported.
        vis = mount(tmp_path / "vis" / "conf.d", "2026_09_25", "data")

        assert len(cpmc.scan(vis)) == 2
        _, out, err = _run_cli(monkeypatch, capsys, tmp_path,
                               "--config-dir", str(vis), "--ci")
        assert "2 mismatch(es) across 2 tenant file(s)" in err, (out, err)

        ms = cpmc.scan(cm)
        assert [Path(m.file).relative_to(cm).as_posix() for m in ms] == [
            "staging/acme.yaml"], ms
        _, out, err = _run_cli(monkeypatch, capsys, tmp_path,
                               "--config-dir", str(cm), "--ci")
        assert "1 mismatch(es) across 1 tenant file(s)" in err, (out, err)
        # ⚠️ Not asserted: the DISPLAYED path. `main` shows
        # `Path(m.file).resolve()` relative to the repo root, which follows
        # the link into `..2026_09_25/` — a display choice that predates
        # #2081 and is not what this class pins (which files are READ).

    def test_hidden_ancestor_of_conf_d_does_not_hide_the_tree(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Only names BELOW conf.d count. A checkout under `.claude/worktrees/`
        or a cache dir puts a `.`-segment in every ABSOLUTE path, so a filter
        that tests all of `p.parts` would call the tree `0 across 0`."""
        (tmp_path / ".git").mkdir()
        conf = self._seed(tmp_path / ".cache" / "conf.d", "file", hidden=False)

        assert len(cpmc.scan(conf)) == 1
        _, out, err = _run_cli(monkeypatch, capsys, tmp_path,
                               "--config-dir", str(conf), "--ci")
        assert "1 mismatch(es) across 2 tenant file(s)" in err, (out, err)

    def test_not_checked_report_skips_hidden_entries_only(
        self, tmp_path, monkeypatch, capsys,
    ):
        """The "not checked" report walks what the selection walks: a broken
        link under a visible name is named, the same link under `.snap/`
        is not (the exporter never looks there)."""
        (tmp_path / ".git").mkdir()
        conf = self._seed(tmp_path / "conf.d", "file", hidden=False)
        (conf / "staging" / ".snap").mkdir()
        _symlink_or_skip(conf / "staging" / "brk.yaml", "missing.yaml")
        _symlink_or_skip(conf / "staging" / ".snap" / "brk.yaml",
                         "missing.yaml")

        _, out, err = _run_cli(monkeypatch, capsys, tmp_path,
                               "--config-dir", str(conf), "--ci")
        warned = [ln for ln in err.splitlines() if "not checked" in ln]
        # 必響對照: the visible broken link IS named.
        assert any(ln.startswith(f"{conf / 'staging' / 'brk.yaml'}:0:")
                   for ln in warned), err
        assert not any(".snap" in ln for ln in warned), err


def _run_as_unprivileged(argv: list[str], cwd: str):
    """Run `argv` where a chmod-000 directory really cannot be read.

    Under uid 0 the mode bits do not stop a read, so drop to `nobody` via
    `setpriv`; as any other uid (CI runners) run it as is.
    """
    if os.name == "nt":
        pytest.skip("chmod 000 does not make a directory unreadable on Windows")
    if os.geteuid() == 0:
        setpriv = shutil.which("setpriv")
        if setpriv is None:
            pytest.skip("running as root and `setpriv` is not installed, so "
                        "no chmod-000 directory can be made unreadable")
        argv = [setpriv, "--reuid=65534", "--regid=65534", "--clear-groups",
                *argv]
        # A root checkout can sit where `nobody` cannot traverse (`/root` is
        # 0700), and then the child fails before it scans anything. Probe the
        # interpreter + script + imports as the same uid; skip, not fail.
        probe = subprocess.run([*argv[:6], "--help"], cwd=cwd,
                               capture_output=True, text=True,
                               encoding="utf-8", timeout=60)
        if probe.returncode != 0:
            pytest.skip("uid 65534 cannot run the script from this checkout: "
                        + (probe.stderr.strip().splitlines() or ["?"])[-1])
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", timeout=60)


def test_unreadable_directories_are_named_even_when_underscored():
    """A directory the walk cannot enumerate hides every tenant under it, so
    it is named — `_locked/` exactly like `locked/`: the `_` filter is for
    ENTRIES (the exporter descends `_`-prefixed directories)."""
    # Not `tmp_path`: under root it sits in a 0700 directory `nobody` cannot
    # traverse, which would make conf.d itself the unreadable thing.
    base = Path(tempfile.mkdtemp(prefix="cpmc-2081-"))
    locked = [base / "conf.d" / "_locked", base / "conf.d" / "locked"]
    try:
        base.chmod(0o755)
        conf = base / "conf.d"
        _write(conf / "staging" / "acme.yaml",
               _tenant_yaml("acme", environment="prod"))
        for d in locked:
            _write(d / "t.yaml", _tenant_yaml("t", environment="prod"))
        for d in (conf, conf / "staging"):
            d.chmod(0o755)
        (conf / "staging" / "acme.yaml").chmod(0o644)
        for d in locked:
            d.chmod(0)
        script = Path(cpmc.__file__).resolve()
        r = _run_as_unprivileged(
            [sys.executable, str(script), "--config-dir", str(conf), "--ci"],
            cwd=str(base),
        )
        # 必響對照: the readable part of the tree WAS scanned.
        assert "1 mismatch(es) across 1 tenant file(s)" in r.stderr, r
        for d in locked:
            assert f"{d}:0: warning: not checked" in r.stderr, (d, r.stderr)
    finally:
        for d in locked:
            if d.exists():
                d.chmod(0o755)
        shutil.rmtree(base, ignore_errors=True)


# ── the hook's trigger must not be narrower than the script's selection ──

import re  # noqa: E402
import yaml  # noqa: E402


def _repo_root() -> Path:
    """Repo root derived from the module under test, not from `cwd`.

    `cpmc.find_repo_root()` walks up from `Path.cwd()`, and several tests in
    this file `monkeypatch.chdir` into a tmp tree — so calling it here would
    answer a different question depending on test order.
    """
    return Path(cpmc.__file__).resolve().parents[3]


class TestHookSelectsEverythingTheScriptScans:
    """The pre-commit `files:` regex is a SECOND copy of "which files matter".

    `test_check_admin_config_schema.py::TestGateIntegrity` pins one direction
    of this — regex ⊆ script, i.e. nothing the hook selects may be silently
    skipped. This is the mirror: script ⊆ regex, i.e. nothing the script
    would scan may fail to trigger the hook.

    It exists because #1603 widened `iter_tenant_files` to both YAML
    spellings while the hook's `files:` still ended in `\\.yaml$`. Nothing
    went red: the hook is `stages: [manual]` with `pass_filenames: false`,
    so the documented `--all-files` invocation still fired on some other
    `.yaml`. The hole only opens for a changed-files invocation of the
    manual stage on a PR that touches `.yml` carriers alone — which is
    exactly the PR this check exists for.
    """

    @staticmethod
    def _hook_files_regex() -> "re.Pattern[str]":
        cfg = yaml.safe_load(
            (_repo_root() / ".pre-commit-config.yaml").read_text(
                encoding="utf-8"))
        hooks = [h for repo in cfg["repos"] for h in repo.get("hooks", [])]
        hook = next(h for h in hooks
                    if h.get("id") == "path-metadata-consistency-check")
        return re.compile(hook["files"])

    def test_every_spelling_the_script_scans_also_triggers_the_hook(
            self, tmp_path):
        """Derived on both sides, so neither can drift alone.

        The candidate names come from `_lib_confd.CONFIG_SUFFIXES`, and
        "would the script scan it" is answered by running `iter_tenant_files`
        on a real tree rather than by re-implementing the predicate.
        """
        from _lib_confd import CONFIG_SUFFIXES  # noqa: PLC0415

        prefix = "components/threshold-exporter/config/conf.d/"
        pattern = self._hook_files_regex()

        config_dir = tmp_path / "conf.d"
        for i, suffix in enumerate(CONFIG_SUFFIXES):
            _write(config_dir / f"t{i}{suffix}", _tenant_yaml(f"t{i}"))
        scanned = {p.name for p in cpmc.iter_tenant_files(config_dir)}

        # ⛔ Vacuity guard: if the script scanned nothing, the loop below is
        # empty and the test passes without asserting anything.
        assert len(scanned) == len(CONFIG_SUFFIXES), (
            f"the script scanned {sorted(scanned)} out of "
            f"{len(CONFIG_SUFFIXES)} carriers written from CONFIG_SUFFIXES "
            f"({CONFIG_SUFFIXES!r}) — fix that before trusting this test"
        )

        for name in sorted(scanned):
            assert pattern.search(prefix + name), (
                f"`{name}` is scanned by the script but the hook's `files:` "
                f"regex ({pattern.pattern!r}) does not select it, so a change "
                f"touching only files like it would not run this check"
            )

    def test_the_regex_still_excludes_things_the_script_ignores(self):
        """The other side of the same coin — a trigger that fires on
        everything is not a fix, it is the check running on unrelated edits.

        ⛔ Positive control first: without it, a regex that matched nothing
        would satisfy every assertion below.
        """
        prefix = "components/threshold-exporter/config/conf.d/"
        pattern = self._hook_files_regex()

        assert pattern.search(prefix + "db-a.yaml"), (
            "the regex no longer selects an ordinary carrier — every "
            "exclusion asserted below would be vacuous"
        )
        for rel in ("notes.txt", "db-a.json", "README.md", "db-a.yaml.bak"):
            assert not pattern.search(prefix + rel), (
                f"the hook's `files:` regex selects `{rel}`, which this "
                f"script never scans"
            )
        for outside in ("rule-packs/rule-pack-mariadb.yaml",
                        "try-local/seed/conf.d/db-a.yaml"):
            assert not pattern.search(outside), (
                f"the hook's `files:` regex reaches outside the conf.d tree "
                f"it is scoped to: {outside}"
            )
