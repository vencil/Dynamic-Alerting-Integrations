"""Unit tests for check_unpinned_deps.py (#1158).

Regression target: `pip install <pkg>` / `go install <mod>@latest` with no
version tracks *latest* on every CI run — the schemathesis `filter_too_much`
seed-flake was one of ~24 such sites. This gate blocks a NEW unpinned install
from silently reintroducing that drift class.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_unpinned_deps as gate  # noqa: E402


def _write(tmp_path: Path, name: str, content: str) -> Path:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    return f


class TestLiveRepoIsClean:
    def test_real_repo_has_no_unpinned_installs(self):
        """The shipped CI/build files must have no unpinned pip/go/npm install.

        Failure here means someone reintroduced the #1158 drift pattern (or a
        new one) without pinning it or baselining it — the regression target.
        """
        findings = gate.scan_repo(gate._repo_root())
        assert findings == [], "unpinned installs found:\n" + "\n".join(
            f"  {f.file}:{f.lineno} [{f.kind}] {f.detail}" for f in findings
        )


class TestPipDetection:
    def test_constraint_file_pins_everything(self):
        assert gate._pip_unpinned(" pyyaml -c requirements/ci-constraints.txt") == []

    def test_requirements_file_ok(self):
        assert gate._pip_unpinned(" -r tests/contract/requirements.txt") == []

    def test_double_equals_ok(self):
        assert gate._pip_unpinned(" --quiet 'bandit==1.9.4'") == []

    def test_upgrade_pip_self_ok(self):
        assert gate._pip_unpinned(" --upgrade pip") == []
        assert gate._pip_unpinned(" --quiet --upgrade pip") == []

    def test_bare_package_flagged(self):
        assert gate._pip_unpinned(" pyyaml") == ["pyyaml"]
        assert "requests" in gate._pip_unpinned(" requests flask")

    def test_stops_at_shell_operator(self):
        # only the pip command counts, not a following `&& something`
        assert gate._pip_unpinned(" --no-cache-dir --upgrade pip && echo done") == []

    def test_trailing_shell_comment_not_tokenized(self):
        # a pinned install with a trailing `# comment` must not tokenize the
        # comment words as unpinned packages (CodeRabbit #1166) …
        assert gate._pip_unpinned(" foo==1.0  # pin for reproducibility") == []
        # … while an unpinned package before a comment is still caught
        assert gate._pip_unpinned(" foo  # note") == ["foo"]


class TestGoDetection:
    def test_latest_flagged(self):
        assert gate._GO_RE.search("go install github.com/swaggo/swag/cmd/swag@latest")

    def test_pinned_semver_not_flagged(self):
        assert not gate._GO_RE.search("go install github.com/swaggo/swag/cmd/swag@v1.16.6")

    def test_pinned_pseudo_version_not_flagged(self):
        assert not gate._GO_RE.search(
            "go install golang.org/x/perf/cmd/benchstat@v0.0.0-20260709024250-82a0b07e230d"
        )


class TestNpmDetection:
    # _npm_unpinned receives the text AFTER the install/i/add keyword
    # (the `rest` group of _NPM_RE), matching the real call site.
    def test_unpinned_global_flagged(self):
        assert gate._npm_unpinned(" -g some-cli") == ["some-cli"]

    def test_unpinned_scoped_flagged(self):
        # a leading @scope is not a version
        assert gate._npm_unpinned(" --save-dev @commitlint/cli") == ["@commitlint/cli"]

    def test_versioned_ok(self):
        assert gate._npm_unpinned(" -g @mermaid-js/mermaid-cli@11.2.0") == []

    def test_bare_install_uses_lockfile(self):
        assert gate._npm_unpinned(" ") == []


class TestScanFileCatchesViolations:
    def test_workflow_with_unpinned_installs(self, tmp_path):
        f = _write(
            tmp_path,
            "w.yml",
            "jobs:\n  x:\n    steps:\n"
            "      - run: pip install requests\n"
            "      - run: go install example.com/x/tool@latest\n"
            "      - run: npm install -g some-cli\n",
        )
        kinds = {fi.kind for fi in gate.scan_file(f, tmp_path)}
        assert kinds == {"pip", "go", "npm"}


class TestChainedAndContinuation:
    def test_second_chained_install_not_hidden(self, tmp_path):
        # a pinned first install must not shield an unpinned second (idiomatic
        # in Dockerfiles: `RUN pip install A && pip install B`)
        f = _write(tmp_path, "Dockerfile",
                   "RUN pip install foo==1.0 && pip install bar\n")
        found = gate.scan_file(f, tmp_path)
        assert [x.detail for x in found] == ["unpinned package(s): bar"]

    def test_backslash_continuation_packages_caught(self, tmp_path):
        f = _write(tmp_path, "Dockerfile",
                   "RUN pip install \\\n    flask requests\n")
        bad = {p for x in gate.scan_file(f, tmp_path) for p in x.detail.split(": ")[1].split(", ")}
        assert {"flask", "requests"} <= bad

    def test_backslash_continuation_constraint_not_false_flagged(self, tmp_path):
        # `-c` on the continuation line must count — no false positive on a
        # correctly pinned multi-line install (would otherwise red a valid PR)
        f = _write(tmp_path, "w.yml",
                   "        run: pip install requests \\\n"
                   "          -c requirements/ci-constraints.txt\n")
        assert gate.scan_file(f, tmp_path) == []


class TestCommentsAndAllowlist:
    def test_whole_line_and_makefile_at_comments_skipped(self, tmp_path):
        f = _write(
            tmp_path,
            "Makefile",
            "\t@# 依賴：pip install mkdocs-material\n"
            "# go install example.com/tool@latest\n",
        )
        assert gate.scan_file(f, tmp_path) == []

    def test_baseline_allowlist_has_justified_reasons(self):
        # every baseline entry must carry a non-trivial reason (no silent skips)
        assert gate.BASELINE_ALLOWLIST
        for a in gate.BASELINE_ALLOWLIST:
            assert a.file and a.contains and len(a.reason) > 20
