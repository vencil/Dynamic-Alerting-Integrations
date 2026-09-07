"""Tests for scripts/tools/lint/check_chart_ship_surface.py (#1755).

The gate asserts that every file at the root of a chart this repo `helm
package`s is declared — SHIP, or EXCLUDE plus a matching `.helmignore` line.
Each test pins one branch of that contract:

  - the LIVE repo is green, and green for the right reason: the three charts
    the release pipeline packages are exactly the ones under scrutiny, and the
    one EXCLUDE declaration is honoured by a real `.helmignore` line;
  - counterfactual — replaying #1597 (the lint variant present, the
    `.helmignore` line absent) turns the gate RED, so the gate is demonstrably
    doing the catching;
  - a new undeclared root file is RED: that is the next `values-*.yaml`;
  - a declaration whose file is gone is RED, so the table cannot rot;
  - a chart that starts shipping without a declaration block is RED — which is
    what makes "derive which charts ship" worth doing at all;
  - fail-closed — an unresolvable Makefile target, a templated workflow target
    and a missing chart directory are caller errors, never silent passes.

⚠️ Boundary, stated in the gate's own docstring: this asserts the chart SOURCE
tree, not the bytes of the `.tgz`. Reading the tarball needs `helm`, which is
unavailable in this environment; no test here claims otherwise.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_chart_ship_surface.py"

_spec = importlib.util.spec_from_file_location("check_chart_ship_surface", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
sys.modules["check_chart_ship_surface"] = gate
assert _spec.loader is not None
_spec.loader.exec_module(gate)


# ---------------------------------------------------------------------------
# fixture repo builder
# ---------------------------------------------------------------------------
def _make_repo(
    tmp_path: Path,
    *,
    chart: str = "helm/demo",
    root_files: dict[str, str] | None = None,
    helmignore: str | None = None,
    workflow: str | None = None,
    makefile: str | None = None,
) -> Path:
    repo = tmp_path / "repo"
    chart_dir = repo / chart
    (chart_dir / "templates").mkdir(parents=True)
    for name, body in (root_files or {"Chart.yaml": "name: demo\n"}).items():
        (chart_dir / name).write_text(body, encoding="utf-8")
    if helmignore is not None:
        (chart_dir / ".helmignore").write_text(helmignore, encoding="utf-8")

    wf_dir = repo / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "release.yaml").write_text(
        workflow if workflow is not None else f"          helm package {chart} -d .build/\n",
        encoding="utf-8",
    )
    if makefile is not None:
        (repo / "Makefile").write_text(makefile, encoding="utf-8")
    return repo


# ---------------------------------------------------------------------------
# the live repo
# ---------------------------------------------------------------------------
class TestLiveRepoIsClean:
    def test_no_violations(self) -> None:
        assert gate.check(REPO_ROOT) == []

    def test_discovery_finds_the_packaged_charts(self) -> None:
        """Green for the right reason: the charts under scrutiny are the ones
        the release pipeline actually packages, not a hand-kept list."""
        sites = gate.discover_shipping_charts(REPO_ROOT)
        assert set(sites) == {
            "helm/threshold-exporter",
            "helm/recipe-preview",
            "helm/tenant-api",
        }
        # tenant-api is discovered from the release workflow, not from DECLARED.
        assert any(
            "release.yaml" in site for site in sites["helm/tenant-api"]
        ), sites["helm/tenant-api"]
        # threshold-exporter is packaged by the Makefile too, through $(CHART_DIR).
        assert any(
            site.startswith("Makefile:") for site in sites["helm/threshold-exporter"]
        ), sites["helm/threshold-exporter"]

    def test_the_one_exclude_is_honoured_by_a_real_helmignore_line(self) -> None:
        patterns = gate.helmignore_patterns(REPO_ROOT / "helm" / "tenant-api")
        assert gate._is_ignored("values-scope-enforce.yaml", patterns)

    def test_every_live_ship_declaration_survives_helmignore(self) -> None:
        """The SHIP side, asserted on the live repo rather than a fixture.

        `helm/threshold-exporter/README.md` was declared SHIP with the reason
        "chart usage doc, written for whoever pulls it" while line 12 of that
        chart's `.helmignore` is `README.md` — so nobody pulling the chart had
        ever received it. A real `helm package` confirmed the `.tgz` root held
        only `.helmignore`, `Chart.yaml` and `values.yaml`.
        """
        offenders = []
        for chart, entries in gate.DECLARED.items():
            patterns = gate.helmignore_patterns(REPO_ROOT / chart)
            for name, (disposition, _reason) in entries.items():
                if disposition != gate.SHIP:
                    continue
                hit = gate.matching_pattern(name, patterns)
                if hit is not None:
                    offenders.append(f"{chart}/{name} killed by {hit!r}")
        # ⛔ Vacuity guard: with no SHIP entries the loop asserts nothing.
        ship_count = sum(
            1
            for entries in gate.DECLARED.values()
            for d, _ in entries.values()
            if d == gate.SHIP
        )
        assert ship_count >= 7, ship_count
        assert offenders == [], offenders


# ---------------------------------------------------------------------------
# counterfactuals
# ---------------------------------------------------------------------------
class TestViolations:
    def test_replay_of_1597_is_red(self, tmp_path: Path, monkeypatch) -> None:
        """The lint variant is present, the .helmignore line is not — exactly
        the state #1597 had to find by hand."""
        repo = _make_repo(
            tmp_path,
            root_files={
                "Chart.yaml": "name: demo\n",
                "values-scope-enforce.yaml": "rbac: enforce\n",
            },
            helmignore="# nothing excluded\n",
        )
        monkeypatch.setattr(
            gate,
            "DECLARED",
            {
                "helm/demo": {
                    "Chart.yaml": (gate.SHIP, "metadata"),
                    ".helmignore": (gate.SHIP, "packing control file"),
                    "values-scope-enforce.yaml": (gate.EXCLUDE, "lint variant"),
                }
            },
        )
        violations = gate.check(repo)
        assert len(violations) == 1
        assert "values-scope-enforce.yaml" in violations[0]
        assert "no .helmignore line matches it" in violations[0]

    def test_the_same_file_declared_and_ignored_is_green(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Control for the test above: with the line present it passes, so the
        RED comes from the missing exclusion and nothing else."""
        repo = _make_repo(
            tmp_path,
            root_files={
                "Chart.yaml": "name: demo\n",
                "values-scope-enforce.yaml": "rbac: enforce\n",
            },
            helmignore="values-scope-enforce.yaml\n",
        )
        monkeypatch.setattr(
            gate,
            "DECLARED",
            {
                "helm/demo": {
                    "Chart.yaml": (gate.SHIP, "metadata"),
                    ".helmignore": (gate.SHIP, "packing control file"),
                    "values-scope-enforce.yaml": (gate.EXCLUDE, "lint variant"),
                }
            },
        )
        assert gate.check(repo) == []

    def test_a_glob_helmignore_line_also_satisfies_exclude(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = _make_repo(
            tmp_path,
            root_files={
                "Chart.yaml": "name: demo\n",
                "values-scope-enforce.yaml": "rbac: enforce\n",
            },
            helmignore="values-*.yaml\n",
        )
        monkeypatch.setattr(
            gate,
            "DECLARED",
            {
                "helm/demo": {
                    "Chart.yaml": (gate.SHIP, "metadata"),
                    ".helmignore": (gate.SHIP, "packing control file"),
                    "values-scope-enforce.yaml": (gate.EXCLUDE, "lint variant"),
                }
            },
        )
        assert gate.check(repo) == []

    def test_undeclared_root_file_is_red(self, tmp_path: Path, monkeypatch) -> None:
        """The next values-*.yaml, whatever it turns out to be called."""
        repo = _make_repo(
            tmp_path,
            root_files={"Chart.yaml": "name: demo\n", "values-debug.yaml": "x: 1\n"},
        )
        monkeypatch.setattr(
            gate, "DECLARED", {"helm/demo": {"Chart.yaml": (gate.SHIP, "metadata")}}
        )
        violations = gate.check(repo)
        assert len(violations) == 1
        assert "values-debug.yaml" in violations[0]
        assert "undeclared" in violations[0]

    def test_ship_declaration_killed_by_helmignore_is_red(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The threshold-exporter/README.md shape, as a fixture.

        Before the SHIP side was asserted, this passed: the gate only ever
        checked EXCLUDE against `.helmignore`, so a table could promise a file
        reached customers while the packer dropped it.
        """
        repo = _make_repo(
            tmp_path,
            root_files={"Chart.yaml": "name: demo\n", "README.md": "docs\n"},
            helmignore="README.md\n",
        )
        monkeypatch.setattr(
            gate,
            "DECLARED",
            {
                "helm/demo": {
                    "Chart.yaml": (gate.SHIP, "metadata"),
                    ".helmignore": (gate.SHIP, "packing control file"),
                    "README.md": (gate.SHIP, "chart usage doc"),
                }
            },
        )
        violations = gate.check(repo)
        assert len(violations) == 1
        assert "README.md" in violations[0]
        assert "declared SHIP" in violations[0]
        assert "'README.md'" in violations[0]  # names the offending line

    def test_the_same_file_declared_exclude_is_green(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Control for the test above: only the disposition changes, so the RED
        comes from the SHIP/ignore contradiction and nothing else."""
        repo = _make_repo(
            tmp_path,
            root_files={"Chart.yaml": "name: demo\n", "README.md": "docs\n"},
            helmignore="README.md\n",
        )
        monkeypatch.setattr(
            gate,
            "DECLARED",
            {
                "helm/demo": {
                    "Chart.yaml": (gate.SHIP, "metadata"),
                    ".helmignore": (gate.SHIP, "packing control file"),
                    "README.md": (gate.EXCLUDE, "excluded on purpose"),
                }
            },
        )
        assert gate.check(repo) == []

    def test_a_ship_file_no_pattern_touches_is_green(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """⛔ Anti-vacuity: the SHIP assertion must not fire on everything."""
        repo = _make_repo(
            tmp_path,
            root_files={"Chart.yaml": "name: demo\n", "README.md": "docs\n"},
            helmignore="values-*.yaml\n",
        )
        monkeypatch.setattr(
            gate,
            "DECLARED",
            {
                "helm/demo": {
                    "Chart.yaml": (gate.SHIP, "metadata"),
                    ".helmignore": (gate.SHIP, "packing control file"),
                    "README.md": (gate.SHIP, "chart usage doc"),
                }
            },
        )
        assert gate.check(repo) == []

    def test_stale_declaration_is_red(self, tmp_path: Path, monkeypatch) -> None:
        repo = _make_repo(tmp_path, root_files={"Chart.yaml": "name: demo\n"})
        monkeypatch.setattr(
            gate,
            "DECLARED",
            {
                "helm/demo": {
                    "Chart.yaml": (gate.SHIP, "metadata"),
                    "gone.yaml": (gate.SHIP, "deleted two releases ago"),
                }
            },
        )
        violations = gate.check(repo)
        assert len(violations) == 1
        assert "gone.yaml" in violations[0] and "stale" in violations[0]

    def test_newly_shipping_chart_without_declarations_is_red(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The point of deriving the chart list: a fourth chart starts shipping
        and the gate demands its declarations instead of ignoring it."""
        repo = _make_repo(tmp_path, root_files={"Chart.yaml": "name: demo\n"})
        monkeypatch.setattr(gate, "DECLARED", {})
        violations = gate.check(repo)
        assert len(violations) == 1
        assert "no entry in DECLARED" in violations[0]


# ---------------------------------------------------------------------------
# fail-closed
# ---------------------------------------------------------------------------
class TestFailsClosed:
    def test_missing_chart_directory_is_a_caller_error(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        (repo / ".github" / "workflows" / "release.yaml").write_text(
            "          helm package helm/does-not-exist -d .build/\n", encoding="utf-8"
        )
        with pytest.raises(gate.CallerError, match="not a directory"):
            gate.check(repo)

    def test_templated_workflow_target_is_a_caller_error(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            workflow="          helm package helm/${{ matrix.chart }} -d .build/\n",
        )
        with pytest.raises(gate.CallerError, match="templated target"):
            gate.discover_shipping_charts(repo)

    def test_unresolvable_makefile_target_is_a_caller_error(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(
            tmp_path,
            workflow="# no helm package here\n",
            makefile="\t@helm package $(MYSTERY_DIR) -d .build/\n",
        )
        with pytest.raises(gate.CallerError, match="unresolvable target"):
            gate.discover_shipping_charts(repo)

    def test_makefile_variable_is_resolved(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            workflow="# packaged from the Makefile only\n",
            makefile="CHART_DIR  := helm/demo\n\t@helm package $(CHART_DIR) -d .build/\n",
        )
        sites = gate.discover_shipping_charts(repo)
        assert "helm/demo" in sites


# ---------------------------------------------------------------------------
# .helmignore semantics (read off helm/pkg/ignore/rules.go, not from memory)
# ---------------------------------------------------------------------------
class TestHelmignoreSemantics:
    """`_is_ignored` answers "would helm exclude this root file?".

    Getting it wrong in the permissive direction ships an undeclared file;
    getting it wrong in the strict direction fails a chart that is fine. Both
    spellings below are ones helm's own docs hand to chart authors.
    """

    def test_root_relative_pattern_matches_a_root_file(self) -> None:
        """`/values-*.yaml` — helm strips the leading slash and matches the
        path relative to the chart root, which for a root file is its name."""
        assert gate._is_ignored("values-scope-enforce.yaml", ["/values-*.yaml"])

    def test_root_relative_literal_matches(self) -> None:
        assert gate._is_ignored("values-scope-enforce.yaml",
                                ["/values-scope-enforce.yaml"])

    def test_bare_basename_still_matches(self) -> None:
        """Control: the spelling the live repo uses must keep working."""
        assert gate._is_ignored("values-scope-enforce.yaml",
                                ["values-scope-enforce.yaml"])

    def test_directory_only_rule_never_matches_a_file(self) -> None:
        """A trailing slash sets helm's `mustDir`; a root FILE is not a dir,
        so declaring it EXCLUDE against `values-scope-enforce.yaml/` must stay
        a violation rather than reading as protection."""
        assert not gate._is_ignored("values-scope-enforce.yaml",
                                    ["values-scope-enforce.yaml/"])

    def test_unrelated_pattern_does_not_match(self) -> None:
        assert not gate._is_ignored("values-scope-enforce.yaml", ["/README.md"])

    def test_matching_pattern_returns_the_line_verbatim(self) -> None:
        """The violation message quotes it, so it must be the raw line — the
        leading slash included — not the normalized form used for matching."""
        assert gate.matching_pattern("values-a.yaml", ["x", "/values-*.yaml"]) == (
            "/values-*.yaml"
        )
        assert gate.matching_pattern("values-a.yaml", ["/README.md"]) is None

    def test_root_relative_pattern_satisfies_an_exclude_end_to_end(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The same shape through `check()`: before this was modelled, the
        gate reported a violation for a file helm does exclude."""
        repo = _make_repo(
            tmp_path,
            root_files={
                "Chart.yaml": "name: demo\n",
                "values-scope-enforce.yaml": "rbac: enforce\n",
            },
            helmignore="/values-*.yaml\n",
        )
        monkeypatch.setattr(
            gate,
            "DECLARED",
            {
                "helm/demo": {
                    "Chart.yaml": (gate.SHIP, "metadata"),
                    ".helmignore": (gate.SHIP, "packing control file"),
                    "values-scope-enforce.yaml": (gate.EXCLUDE, "lint variant"),
                }
            },
        )
        assert gate.check(repo) == []


# ---------------------------------------------------------------------------
# unreadable inputs are caller errors, not tracebacks
# ---------------------------------------------------------------------------
class TestUnreadableInputsFailClosed:
    """The docstring promises exit 2 for "could not RUN". An OSError escaping
    as a traceback would break that contract — and pre-commit would surface it
    as a crash rather than as the documented, greppable caller error.

    uid-independent on purpose: `chmod 000` does not stop root, so these tests
    would pass in CI and quietly do nothing in a root dev container.
    """

    @staticmethod
    def _raise_for(monkeypatch, attr: str, predicate) -> None:
        real = getattr(Path, attr)

        def patched(self, *args, **kwargs):
            if predicate(self):
                raise PermissionError(13, "Permission denied")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, attr, patched)

    def test_unreadable_makefile(self, tmp_path: Path, monkeypatch) -> None:
        repo = _make_repo(tmp_path, makefile="CHART_DIR := helm/demo\n")
        self._raise_for(monkeypatch, "read_text", lambda p: p.name == "Makefile")
        with pytest.raises(gate.CallerError, match="cannot read"):
            gate.discover_shipping_charts(repo)

    def test_unlistable_workflows_dir(self, tmp_path: Path, monkeypatch) -> None:
        repo = _make_repo(tmp_path)
        self._raise_for(monkeypatch, "iterdir", lambda p: p.name == "workflows")
        with pytest.raises(gate.CallerError, match="cannot list"):
            gate.discover_shipping_charts(repo)

    def test_unreadable_workflow_file(self, tmp_path: Path, monkeypatch) -> None:
        repo = _make_repo(tmp_path)
        self._raise_for(monkeypatch, "read_text",
                        lambda p: p.name == "release.yaml")
        with pytest.raises(gate.CallerError, match="cannot read"):
            gate.discover_shipping_charts(repo)

    def test_non_yaml_file_in_workflows_dir_is_skipped(
        self, tmp_path: Path
    ) -> None:
        """Control for the two above: a stray file is skipped, not read — so
        the errors they raise come from the failure, not from the walk."""
        repo = _make_repo(tmp_path)
        (repo / ".github" / "workflows" / "README.txt").write_text(
            "helm package helm/not-a-chart\n", encoding="utf-8"
        )
        assert set(gate.discover_shipping_charts(repo)) == {"helm/demo"}


# ---------------------------------------------------------------------------
# the pre-commit hook must actually FIRE on the files it guards
# ---------------------------------------------------------------------------
class TestHookTriggers:
    """A gate that never runs is not a gate (dev-rules / rulebook D-04).

    `pass_filenames: false` means the hook checks the whole repo — but only
    when pre-commit decides a changed path matches `files:`. An extension
    allowlist there silently excluded `Chart.lock` / `LICENSE` / `values.json`,
    i.e. the very additions this gate exists to catch.
    """

    @staticmethod
    def _hook_files_regex() -> "re.Pattern[str]":
        cfg = yaml.safe_load(
            (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        )
        hooks = [h for repo in cfg["repos"] for h in repo.get("hooks", [])]
        hook = next(h for h in hooks if h.get("id") == "chart-ship-surface")
        return re.compile(hook["files"])

    def test_every_live_chart_root_file_triggers_the_hook(self) -> None:
        """Derived from the charts discovery finds, not a hand-kept list."""
        pattern = self._hook_files_regex()
        roots = [
            f"{chart}/{f.name}"
            for chart in gate.discover_shipping_charts(REPO_ROOT)
            for f in (REPO_ROOT / chart).iterdir()
            if f.is_file()
        ]
        # ⛔ Vacuity guard: an empty list would make the loop assert nothing.
        assert len(roots) >= 8, roots
        unmatched = [r for r in roots if not pattern.match(r)]
        assert unmatched == [], unmatched

    @pytest.mark.parametrize(
        "name", ["Chart.lock", "LICENSE", "values.json", "NOTES.txt", "app.tgz"]
    )
    def test_a_new_root_file_of_any_shape_triggers_the_hook(
        self, name: str
    ) -> None:
        """These do not exist yet — which is the point. The next one to appear
        must re-run the gate rather than slip in unchecked."""
        assert self._hook_files_regex().match(f"helm/tenant-api/{name}")

    def test_the_pattern_stays_scoped_to_the_chart_root(self) -> None:
        """Negative control: `templates/` is out of scope by design, so a
        match there would mean the pattern is not saying what it claims."""
        pattern = self._hook_files_regex()
        assert not pattern.match("helm/tenant-api/templates/deployment.yaml")
        assert not pattern.match("helm/README.md")


# ---------------------------------------------------------------------------
# the archive check must stay wired into every `helm package` call site
# ---------------------------------------------------------------------------
class TestPackageVerificationWiring:
    """#1755's second half lives in release workflows, where helm exists.

    A step that only lives in a workflow can be deleted, renamed or have its
    exit code discarded, and nothing notices until a release ships something it
    should not have. This assertion is the thing that notices — and it needs no
    helm, so it runs on every PR.
    """

    @staticmethod
    def _repo(tmp_path: Path, workflow: str, makefile: str | None = None) -> Path:
        repo = tmp_path / "repo"
        (repo / ".github" / "workflows").mkdir(parents=True)
        (repo / ".github" / "workflows" / "release.yaml").write_text(
            workflow, encoding="utf-8"
        )
        if makefile is not None:
            (repo / "Makefile").write_text(makefile, encoding="utf-8")
        return repo

    def test_live_repo_has_every_call_site_wired(self) -> None:
        """Green for the right reason: the four real call sites (#1755 lists
        them) are all covered, not zero call sites found."""
        assert gate.check_package_verification_wiring(REPO_ROOT) == []
        sites = [
            s
            for sites in gate.discover_shipping_charts(REPO_ROOT).values()
            for s in sites
        ]
        # ⛔ Vacuity guard: no call sites would make the assertion above empty.
        assert len(sites) >= 4, sites

    def test_package_without_verification_is_red(self, tmp_path: Path) -> None:
        repo = self._repo(
            tmp_path,
            "          helm package helm/demo -d .build/\n"
            "          helm push .build/demo-1.0.0.tgz oci://reg/charts\n",
        )
        v = gate.check_package_verification_wiring(repo)
        assert len(v) == 1
        assert "nothing invokes check_chart_package_contents.py" in v[0]
        assert "before the `helm push` on line 2" in v[0]

    def test_verification_after_the_push_is_red(self, tmp_path: Path) -> None:
        """Ordering is the point: checking after publication is a post-mortem."""
        repo = self._repo(
            tmp_path,
            "          helm package helm/demo -d .build/\n"
            "          helm push .build/demo-1.0.0.tgz oci://reg/charts\n"
            "          python3 scripts/tools/lint/check_chart_package_contents.py\n",
        )
        assert len(gate.check_package_verification_wiring(repo)) == 1

    def test_verification_before_the_push_is_green(self, tmp_path: Path) -> None:
        """Control for the two above — only the position changes."""
        repo = self._repo(
            tmp_path,
            "          helm package helm/demo -d .build/\n"
            "          python3 scripts/tools/lint/check_chart_package_contents.py\n"
            "          helm push .build/demo-1.0.0.tgz oci://reg/charts\n",
        )
        assert gate.check_package_verification_wiring(repo) == []

    # The verifier invocation and the push, as the real workflow spells them.
    _V = "          python3 x/check_chart_package_contents.py\n"
    _U = "          helm push .build/demo-1.0.0.tgz oci://reg/charts\n"

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("inline || true", "          python3 x/check_chart_package_contents.py || true\n" + _U),
            ("inline ||true", "          python3 x/check_chart_package_contents.py ||true\n" + _U),
            ("set +e before it", "          set +e\n" + _V + _U),
            (
                "continue-on-error in ITS step",
                "      - name: Verify\n        continue-on-error: true\n        run: |\n" + _V + _U,
            ),
        ],
    )
    def test_a_neutered_verification_is_red(
        self, tmp_path: Path, label: str, body: str
    ) -> None:
        """⭐ Present-but-defanged reads exactly like protection while providing
        none. Finding the invocation is not enough."""
        repo = self._repo(
            tmp_path, "          helm package helm/demo -d .build/\n" + body
        )
        v = gate.check_package_verification_wiring(repo)
        assert len(v) == 1, (label, v)
        assert "neutralises" in v[0]

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            # ⛔ The dangerous direction: a check that is commented out reads as
            # absent to a human and as PRESENT to a naive substring scan. This
            # gate exists to catch a disabled check; being fooled by `#` would
            # make it decorative. (CodeRabbit on #1780; verified, then fixed.)
            ("commented-out verifier", "          # python3 x/check_chart_package_contents.py\n" + _U),
        ],
    )
    def test_a_commented_out_verification_does_not_count(
        self, tmp_path: Path, label: str, body: str
    ) -> None:
        repo = self._repo(
            tmp_path, "          helm package helm/demo -d .build/\n" + body
        )
        v = gate.check_package_verification_wiring(repo)
        assert len(v) == 1, (label, v)
        assert "nothing invokes" in v[0]

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            # A guard that reports work nobody can do gets switched off, so each
            # of these false positives was worth removing.
            ("commented `helm push` before the real one",
             "          # helm push (old spelling)\n" + _V + _U),
            ("another command's `|| true`",
             "          rm -rf .build/tmp || true\n" + _V + _U),
            ("`set +e` AFTER the verifier has already run",
             _V + "          set +e\n" + _U),
            ("continue-on-error on a DIFFERENT step",
             "      - name: Other\n        continue-on-error: true\n"
             "        run: echo hi\n      - name: Verify\n        run: |\n" + _V + _U),
        ],
    )
    def test_wiring_that_is_actually_fine_is_green(
        self, tmp_path: Path, label: str, body: str
    ) -> None:
        repo = self._repo(
            tmp_path, "          helm package helm/demo -d .build/\n" + body
        )
        assert gate.check_package_verification_wiring(repo) == [], label

    def test_a_makefile_call_site_counts_too(self, tmp_path: Path) -> None:
        """`make chart-package` publishes through `chart-push`; the workflows are
        not the only road to the registry."""
        repo = self._repo(
            tmp_path,
            "# no helm package in this workflow\n",
            makefile="\t@helm package helm/demo -d .build/\n"
            "\t@helm push .build/demo-1.0.0.tgz oci://reg/charts\n",
        )
        v = gate.check_package_verification_wiring(repo)
        assert len(v) == 1
        assert v[0].startswith("Makefile:1")

    def test_unlistable_workflows_dir_is_a_caller_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """⛔ Same contract as the sibling assertions: could-not-run is exit 2,
        never a quiet "no violations". monkeypatch rather than chmod, because
        chmod 000 does not stop root and this repo's dev container runs as root
        (#1264) — that test would pass in CI and do nothing here."""
        repo = self._repo(tmp_path, "          helm package helm/demo\n")
        real = Path.iterdir

        def patched(self, *a, **k):
            if self.name == "workflows":
                raise PermissionError(13, "Permission denied")
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "iterdir", patched)
        with pytest.raises(gate.CallerError, match="cannot list"):
            gate.check_package_verification_wiring(repo)

    def test_unreadable_runner_file_is_a_caller_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = self._repo(tmp_path, "          helm package helm/demo\n")
        real = Path.read_text

        def patched(self, *a, **k):
            if self.name == "release.yaml":
                raise PermissionError(13, "Permission denied")
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", patched)
        with pytest.raises(gate.CallerError, match="cannot read"):
            gate.check_package_verification_wiring(repo)

    def test_no_call_sites_means_nothing_to_assert(self, tmp_path: Path) -> None:
        """⛔ Anti-vacuity in the other direction: the assertion must not invent
        violations where no chart is packaged at all."""
        repo = self._repo(tmp_path, "# nothing here\n")
        assert gate.check_package_verification_wiring(repo) == []

    def test_a_comment_mentioning_helm_package_is_not_a_call_site(
        self, tmp_path: Path
    ) -> None:
        """Found the hard way: a fixture comment reading "# no helm package in
        this workflow" was counted as a call site. A guard that matches prose
        reports work nobody can do."""
        repo = self._repo(
            tmp_path,
            "# no helm package in this workflow\n"
            "        # helm package helm/demo -d .build/  (disabled)\n",
            makefile="# helm package helm/demo -d .build/\n",
        )
        assert gate.check_package_verification_wiring(repo) == []
        assert gate.discover_shipping_charts(repo) == {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_on_the_live_repo_exits_zero() -> None:
    assert gate.main([]) == gate.EXIT_OK


def test_cli_returns_exit_violation_and_prints_each_one(monkeypatch, capsys):
    """The exit CODE is the whole contract with pre-commit: `check()` finding
    a violation is worthless if `main()` still returns 0."""
    monkeypatch.setattr(
        gate, "check", lambda: ["helm/demo/values-debug.yaml: undeclared file"]
    )
    assert gate.main([]) == gate.EXIT_VIOLATION
    assert "values-debug.yaml" in capsys.readouterr().err


def test_cli_returns_exit_caller_error(monkeypatch, capsys):
    def boom():
        raise gate.CallerError("packages an unresolvable target")

    monkeypatch.setattr(gate, "check", boom)
    assert gate.main([]) == gate.EXIT_CALLER_ERROR
    assert "unresolvable target" in capsys.readouterr().err
