"""Tests for check_iac_vibe_rules.py — Container SAST Layer 1 (#448 / TRK-311).

Pinned contracts (exercised without invoking hadolint / docker — the engine
integration is covered when CI runs the iac-sast-check hook end-to-end):

1. **Runtime base / distroless detection**: the LAST FROM is the runtime
   stage; distroless runtime => HEALTHCHECK auto-exempt.
2. **V1 HEALTHCHECK-or-rationale**: non-distroless image with neither a
   HEALTHCHECK instruction nor a `# rationale:` comment => violation.
3. **V2 over-broad COPY/ADD**: a source operand of bare `.` / `./` / `*`
   => flagged; specific sources and `--from=`/`--chown=` flags => not.
4. **V3 .dockerignore baseline** (pathspec gitwildmatch): equivalent glob
   spellings cover the baseline; comment lines don't false-cover.
5. **Level -> action mapping**: error=BLOCK, warning=WARN, info/style=INFO.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

# pathspec is a hard dependency of the V3 .dockerignore check. It's installed
# in every workflow that runs this suite (ci.yml / validate.yaml /
# nightly-mutation-pilot.yaml). The skipif below is a safety net: if some
# future runner forgets the dep, the V3 tests SKIP instead of hard-failing —
# a hard fail on the first CI run wedges the pre-push preflight-marker gate
# (the bootstrap deadlock), and that escape is human-only.
_HAS_PATHSPEC = importlib.util.find_spec("pathspec") is not None

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_iac_vibe_rules as iac  # noqa: E402


# ---------------------------------------------------------------------------
# Runtime base / distroless
# ---------------------------------------------------------------------------
class TestRuntimeBase:
    def test_single_stage(self):
        assert iac.runtime_base_image("FROM alpine:3.22\nUSER x") == "alpine:3.22"

    def test_multistage_uses_last_from(self):
        df = (
            "FROM golang:1.26 AS builder\nRUN go build\n"
            "FROM gcr.io/distroless/static-debian12:nonroot\nCOPY --from=builder /a /a"
        )
        assert "distroless" in iac.runtime_base_image(df)
        assert iac.is_distroless(df) is True

    def test_alpine_not_distroless(self):
        assert iac.is_distroless("FROM alpine:3.22") is False


# ---------------------------------------------------------------------------
# V1 HEALTHCHECK-or-rationale
# ---------------------------------------------------------------------------
class TestHealthcheckRule:
    def test_distroless_auto_exempt(self):
        df = "FROM gcr.io/distroless/static-debian12:nonroot\nENTRYPOINT [\"/x\"]"
        assert iac.healthcheck_violation(df) is False

    def test_healthcheck_present_passes(self):
        df = "FROM nginx:1.28-alpine\nHEALTHCHECK CMD wget -qO- localhost || exit 1"
        assert iac.has_healthcheck(df) is True
        assert iac.healthcheck_violation(df) is False

    def test_rationale_comment_passes(self):
        df = "FROM alpine:3.22\n# rationale: CLI tool, no long-running service\nUSER x"
        assert iac.has_rationale(df) is True
        assert iac.healthcheck_violation(df) is False

    def test_missing_both_violates(self):
        df = "FROM alpine:3.22\nRUN apk add --no-cache git\nUSER nonroot"
        assert iac.healthcheck_violation(df) is True

    def test_rationale_requires_content(self):
        # bare `# rationale:` with no reason should NOT satisfy
        df = "FROM alpine:3.22\n# rationale:\nUSER x"
        assert iac.has_rationale(df) is False
        assert iac.healthcheck_violation(df) is True


# ---------------------------------------------------------------------------
# V2 over-broad COPY/ADD
# ---------------------------------------------------------------------------
class TestBroadCopy:
    @pytest.mark.parametrize("line", [
        "COPY . /app",
        "COPY ./ /app",
        "COPY * /app/",
        "ADD . .",
        "COPY --chown=nginx:nginx . /usr/share/nginx/html",
    ])
    def test_broad_sources_flagged(self, line):
        assert iac.over_broad_copy_lines(line) != []

    @pytest.mark.parametrize("line", [
        "COPY entrypoint.py .",
        "COPY tools/ ./",
        "COPY --from=builder /tenant-api /usr/local/bin/tenant-api",
        "COPY go.mod go.sum ./",
        "RUN echo .",
    ])
    def test_specific_sources_ok(self, line):
        assert iac.over_broad_copy_lines(line) == []

    def test_line_number_reported(self):
        df = "FROM alpine\nWORKDIR /app\nCOPY . /app"
        hits = iac.over_broad_copy_lines(df)
        assert hits and hits[0][0] == 3


# ---------------------------------------------------------------------------
# V3 .dockerignore baseline (pathspec)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _HAS_PATHSPEC, reason="pathspec not installed — V3 baseline check unavailable")
class TestDockerignoreBaseline:
    def test_complete_baseline_no_gaps(self):
        text = "/.git/\n/.github/\n/scripts/\n/tests/\n/docs/\n*.md\n*.log\n.env*\n"
        assert iac.dockerignore_baseline_gaps(text) == []

    def test_unanchored_equivalent_also_covers(self):
        text = ".git\n.github\nscripts\ntests\ndocs\n*.md\n*.log\n.env*\n"
        assert iac.dockerignore_baseline_gaps(text) == []

    def test_missing_entries_reported(self):
        text = "/.git/\n*.log\n"
        gaps = iac.dockerignore_baseline_gaps(text)
        assert ".env*" in gaps
        assert "*.md" in gaps
        assert "tests/" in gaps

    def test_comment_lines_do_not_false_cover(self):
        # A comment mentioning .git must not satisfy the .git baseline.
        text = "# remember to exclude .git and tests\n*.log\n"
        gaps = iac.dockerignore_baseline_gaps(text)
        assert ".git/" in gaps
        assert "tests/" in gaps


# ---------------------------------------------------------------------------
# Level -> action
# ---------------------------------------------------------------------------
class TestClassify:
    @pytest.mark.parametrize("level,action", [
        ("error", "BLOCK"),
        ("warning", "WARN"),
        ("info", "INFO"),
        ("style", "INFO"),
        ("unknown", "INFO"),
    ])
    def test_mapping(self, level, action):
        assert iac.classify_level(level) == action


# ---------------------------------------------------------------------------
# Engine location (monkeypatched — no real hadolint/docker invoked)
# ---------------------------------------------------------------------------
class TestEngineLocate:
    def test_prefers_binary(self, monkeypatch):
        monkeypatch.setattr(
            iac.shutil, "which",
            lambda n: "/usr/bin/hadolint" if n == "hadolint" else None,
        )
        assert iac.locate_engine() == ("binary", "/usr/bin/hadolint")

    def test_docker_fallback(self, monkeypatch):
        monkeypatch.setattr(
            iac.shutil, "which",
            lambda n: "/usr/bin/docker" if n == "docker" else None,
        )
        assert iac.locate_engine() == ("docker", None)

    def test_none_available(self, monkeypatch):
        monkeypatch.setattr(iac.shutil, "which", lambda n: None)
        assert iac.locate_engine() == (None, None)


# ---------------------------------------------------------------------------
# collect_findings — aggregation + level->action, run_hadolint stubbed
# ---------------------------------------------------------------------------
class TestCollectFindings:
    def test_hadolint_levels_classified(self, monkeypatch):
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: [
            {"file": "components/da-tools/app/Dockerfile", "line": 1,
             "code": "DL9999", "level": "error", "message": "boom"},
            {"file": "components/da-tools/app/Dockerfile", "line": 2,
             "code": "DL3018", "level": "warning", "message": "pin"},
            {"file": "components/da-tools/app/Dockerfile", "line": 3,
             "code": "DL3059", "level": "info", "message": "consolidate"},
        ])
        f = iac.collect_findings(["components/da-tools/app/Dockerfile"])
        assert any("DL9999" in x for x in f["BLOCK"])
        assert any("DL3018" in x for x in f["WARN"])
        assert any("DL3059" in x for x in f["INFO"])

    def test_unregistered_dockerfile_blocks(self, monkeypatch):
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: [])
        f = iac.collect_findings(["some/random/Dockerfile"])
        assert any("unregistered" in x.lower() for x in f["BLOCK"])

    def test_engine_unavailable_sets_sentinel(self, monkeypatch):
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: None)
        f = iac.collect_findings(["components/da-tools/app/Dockerfile"])
        assert "__engine_error__" in f


# ---------------------------------------------------------------------------
# main() exit codes (run_hadolint stubbed; real repo files are clean)
# ---------------------------------------------------------------------------
class TestMainExitCodes:
    def test_list_returns_zero(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["prog", "--list"])
        assert iac.main() == 0

    def test_clean_returns_zero(self, monkeypatch):
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: [])
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert iac.main() == 0

    def test_block_returns_one_with_ci(self, monkeypatch):
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: [
            {"file": "x", "line": 1, "code": "DL3007",
             "level": "error", "message": "no :latest"}])
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        monkeypatch.delenv("PR_BODY", raising=False)
        assert iac.main() == 1

    def test_engine_unavailable_returns_three(self, monkeypatch):
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: None)
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert iac.main() == 3


# ---------------------------------------------------------------------------
# Registry integrity — every Dockerfile in the tree must be registered
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_all_discovered_dockerfiles_registered(self):
        for df in iac.find_dockerfiles():
            assert df in iac.DOCKERFILE_CONTEXTS, (
                f"{df} not registered in DOCKERFILE_CONTEXTS — declare its "
                f"build-context root (see check_iac_vibe_rules.py)"
            )
