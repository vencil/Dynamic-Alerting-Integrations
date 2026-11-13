"""Tests for check_iac_vibe_rules.py — Container SAST Layer 1 (#448 / TRK-311).

Pinned contracts (exercised without the real hadolint / docker; the engine's
own run is the CI lint job, which installs the pinned binary):

1. **Runtime base / distroless detection**: the LAST FROM is the runtime
   stage; distroless runtime => HEALTHCHECK auto-exempt.
2. **V1 HEALTHCHECK-or-rationale**: non-distroless image with neither a
   HEALTHCHECK instruction nor a `# rationale:` comment => violation.
3. **V2 over-broad COPY/ADD**: a source operand of bare `.` / `./` / `*`
   => flagged; specific sources and `--from=`/`--chown=` flags => not.
4. **V3 .dockerignore baseline** (pathspec gitwildmatch): equivalent glob
   spellings cover the baseline; comment lines don't false-cover.
5. **Level -> action mapping**: error=BLOCK, warning=WARN, info/style=INFO.
6. **A run that does not show it ran is no run** (#3439): stand-ins for
   hadolint / docker on PATH, the wrapper run as a process. A non-zero exit,
   an empty stdout, or stdout that is no JSON array of findings is exit 2 —
   `docker run` with no daemon (rc 1, nothing on stdout) printed OK, rc 0.
7. **A run at other levels than the registered ones is no run** (#3643):
   the stand-ins answer for the canary on stdin too, and one that reports it
   at another level, or not at all, is exit 2. The last section runs the
   real hadolint where it is installed (skipped elsewhere; a remote session
   must have it): HADOLINT_* variables, inline ignore pragmas and a
   `.hadolint.yaml` put back in the repo all leave the verdict unchanged.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
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
            iac._lib_engine.shutil, "which",
            lambda n: "/usr/bin/hadolint" if n == "hadolint" else None,
        )
        loc = iac.locate_engine()
        assert (loc.mode, loc.path) == ("binary", "/usr/bin/hadolint")

    def test_docker_fallback(self, monkeypatch):
        monkeypatch.setattr(
            iac._lib_engine.shutil, "which",
            lambda n: "/usr/bin/docker" if n == "docker" else None,
        )
        loc = iac.locate_engine()
        assert (loc.mode, loc.image) == ("docker", iac.HADOLINT_IMAGE)

    def test_none_available(self, monkeypatch):
        monkeypatch.setattr(iac._lib_engine.shutil, "which", lambda n: None)
        assert isinstance(iac.locate_engine(), iac._lib_engine.NoVerdict)


# ---------------------------------------------------------------------------
# collect_findings — aggregation + level->action, run_hadolint stubbed
# ---------------------------------------------------------------------------
def _ran(findings):
    return iac._lib_engine.Ran(findings, None)


_NO_RUN = iac._lib_engine.NoVerdict("hadolint lint did not run: stub")


class TestCollectFindings:
    def test_hadolint_levels_classified(self, monkeypatch):
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: _ran([
            {"file": "components/da-tools/app/Dockerfile", "line": 1,
             "code": "DL9999", "level": "error", "message": "boom"},
            {"file": "components/da-tools/app/Dockerfile", "line": 2,
             "code": "DL3018", "level": "warning", "message": "pin"},
            {"file": "components/da-tools/app/Dockerfile", "line": 3,
             "code": "DL3059", "level": "info", "message": "consolidate"},
        ]))
        f = iac.collect_findings(["components/da-tools/app/Dockerfile"])
        assert any("DL9999" in x for x in f["BLOCK"])
        assert any("DL3018" in x for x in f["WARN"])
        assert any("DL3059" in x for x in f["INFO"])

    def test_unregistered_dockerfile_blocks(self, monkeypatch):
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: _ran([]))
        f = iac.collect_findings(["some/random/Dockerfile"])
        assert any("unregistered" in x.lower() for x in f["BLOCK"])

    def test_engine_unavailable_sets_sentinel(self, monkeypatch):
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: _NO_RUN)
        f = iac.collect_findings(["components/da-tools/app/Dockerfile"])
        assert "__engine_error__" in f

    def test_v3_checks_the_per_dockerfile_ignore_file(self, monkeypatch, tmp_path):
        """#3425: BuildKit reads `<Dockerfile>.dockerignore` in place of the
        context root's file, so V3 must judge that one — a gap there blocks
        even though the root .dockerignore is complete."""
        df = "app/Dockerfile"
        (tmp_path / "app").mkdir()
        (tmp_path / df).write_text("FROM gcr.io/distroless/static\n", encoding="utf-8")
        (tmp_path / ".dockerignore").write_text(
            "/.git/\n/tests/\n/scripts/\n/docs/\n*.md\n/.github/\n*.log\n.env*\n",
            encoding="utf-8")
        own = tmp_path / f"{df}.dockerignore"
        own.write_text("!scripts/ops/go_build.sh\n", encoding="utf-8")
        monkeypatch.setattr(iac, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(iac, "DOCKERFILE_CONTEXTS", {df: "."})
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: _ran([]))
        blocks = iac.collect_findings([df])["BLOCK"]
        assert any("app/Dockerfile.dockerignore" in b and "V3" in b for b in blocks), blocks
        own.write_text("*\n!scripts/ops/go_build.sh\n", encoding="utf-8")
        assert not [b for b in iac.collect_findings([df])["BLOCK"] if "V3" in b]


def test_per_dockerfile_ignore_files_are_not_discovered_as_dockerfiles():
    found = iac.find_dockerfiles()
    assert "components/threshold-exporter/app/Dockerfile" in found
    assert not [d for d in found if d.endswith(".dockerignore")], found


# ---------------------------------------------------------------------------
# main() exit codes (run_hadolint stubbed; real repo files are clean)
# ---------------------------------------------------------------------------
class TestMainExitCodes:
    def test_list_returns_zero(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["prog", "--list"])
        assert iac.main() == 0

    def test_clean_returns_zero(self, monkeypatch):
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: _ran([]))
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert iac.main() == 0

    def test_block_returns_one_with_ci(self, monkeypatch):
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: _ran([
            {"file": "x", "line": 1, "code": "DL3007",
             "level": "error", "message": "no :latest"}]))
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        monkeypatch.delenv("PR_BODY", raising=False)
        assert iac.main() == 1

    def test_engine_unavailable_is_a_caller_error(self, monkeypatch):
        monkeypatch.setattr(iac, "run_hadolint", lambda dfs: _NO_RUN)
        monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
        assert iac.main() == 2


# ---------------------------------------------------------------------------
# #3439: the engine's run, through stand-ins on PATH (the wrapper as a process)
# ---------------------------------------------------------------------------
_SCRIPT = os.path.join(_TOOLS_DIR, "check_iac_vibe_rules.py")
#: A stand-in hadolint that keeps the real one's exit contract: one
#: error-level finding, exit 1 for it unless --no-fail (failure-threshold
#: error) — so the wrapper reads it as a finding only by passing --no-fail.
_FINDING = {"file": "x/Dockerfile", "line": 1, "code": "DL3007", "level": "error",
            "message": "latest"}


def _canary_findings(**override):
    """What hadolint reports for the canary on stdin (`-`): each registered
    rule once at its registered level. ``override`` maps a code to the levels
    it comes back with instead (``[]``: not at all)."""
    levels = {**iac.hadolint_canary_expectation(), **override}
    return [{"file": "-", "line": 1, "code": code, "level": level, "message": "canary"}
            for code, lvls in levels.items() for level in lvls]


def _report(*findings, **override):
    """A stand-in's stdout: *findings* on the repo's files plus the canary's."""
    return json.dumps([*findings, *_canary_findings(**override)])


_FAITHFUL = f"""#!/bin/sh
echo '{_report(_FINDING)}'
for a in "$@"; do [ "$a" = "--no-fail" ] && exit 0; done
exit 1
"""


def _stand_in(tmp_path, name, body):
    """A PATH holding only *name* (a shell script); returns that PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / name
    exe.write_text(body, encoding="utf-8")
    exe.chmod(0o755)
    return str(bin_dir)


def _wrapper(path):
    env = {k: v for k, v in os.environ.items() if k != "PR_BODY"}
    env["PATH"] = path
    return subprocess.run([sys.executable, _SCRIPT, "--ci"], capture_output=True, text=True,
                          encoding="utf-8", timeout=120, env=env, stdin=subprocess.DEVNULL)


@pytest.mark.parametrize("ci", [True, False], ids=["ci", "dev-box"])
def test_no_engine_at_all_is_rc_2_with_or_without_ci(tmp_path, ci):
    """#3611 Q1 (owner 2026-11-06): no hadolint and no docker is no verdict,
    rc 2, on a dev box too — it used to be a WARN and rc 0 there. Skipping
    the hook is pre-commit's own SKIP=iac-sast-check."""
    empty = tmp_path / "empty"
    empty.mkdir()
    proc = _wrapper(str(empty)) if ci else _wrapper_bare(str(empty))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "hadolint is not available" in proc.stderr, proc.stderr
    assert "OK Container SAST" not in proc.stdout, proc.stdout


def test_a_broken_engine_is_rc_2_on_a_dev_box_too(tmp_path):
    """An engine that is there and does not show it ran is no verdict, with or
    without --ci (as check_iac_helm's render / kube-linter failures)."""
    proc = _wrapper_bare(_stand_in(tmp_path, "hadolint", _replies(1, "")))
    assert proc.returncode == 2, proc.stdout + proc.stderr


def _wrapper_bare(path):
    env = {k: v for k, v in os.environ.items() if k != "PR_BODY"}
    env["PATH"] = path
    return subprocess.run([sys.executable, _SCRIPT], capture_output=True, text=True,
                          encoding="utf-8", timeout=120, env=env, stdin=subprocess.DEVNULL)


def _replies(rc, stdout):
    return f"#!/bin/sh\nprintf '%s' '{stdout}'\necho 'engine said why' >&2\nexit {rc}\n"


def test_docker_with_no_daemon_is_no_run(tmp_path):
    """The ticket's case: hadolint not on PATH, docker is, its daemon is not."""
    proc = _wrapper(_stand_in(tmp_path, "docker", (
        "#!/bin/sh\necho 'failed to connect to the docker API at "
        "unix:///var/run/docker.sock' >&2\nexit 1\n")))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "failed to connect to the docker API" in proc.stderr, proc.stderr
    assert "OK Container SAST" not in proc.stdout, proc.stdout


@pytest.mark.parametrize("rc, stdout", [
    (0, ""),          # ran? nothing says so: a clean run prints []
    (1, "[]"),        # under --no-fail a non-zero exit is no run, whatever stdout says
    (2, "[]"),
    (125, ""),        # docker's own failure
    (0, "{}"),        # JSON, not an array of findings
    (0, "null"),
    (0, "[1]"),
    (0, "not json"),
    (0, '[{"message":"parse failed"}]'),   # no level: it would pass as INFO
    # #3643 — a well-formed array at exit 0, as hadolint prints when it ran at
    # other levels than the registered ones:
    (0, "[]"),                                     # the canary never came back
    (0, _report(DL3007=["warning"])),              # default level: flags / config not applied
    (0, _report(DL3007=[])),                       # ignored (HADOLINT_IGNORE, a pragma)
    (0, _report(DL3026=[])),                       # no trusted registry applied
    (0, _report(DL3026=["error", "error"])),       # a trusted registry was dropped
], ids=["rc0-empty", "rc1-array", "rc2-array", "rc125-empty", "object", "null",
        "array-of-number", "not-json", "finding-without-level", "no-canary",
        "canary-at-default-level", "canary-rule-ignored", "canary-dl3026-silent",
        "canary-dl3026-on-a-trusted-registry"])
def test_an_engine_run_that_does_not_show_it_ran_is_a_caller_error(tmp_path, rc, stdout):
    proc = _wrapper(_stand_in(tmp_path, "hadolint", _replies(rc, stdout)))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "did not run" in proc.stderr and "engine said why" in proc.stderr, proc.stderr
    assert "OK Container SAST" not in proc.stdout, proc.stdout


def test_a_clean_engine_run_passes(tmp_path):
    """The control: the stand-in route itself reaches OK (rc 0) when hadolint
    exits 0 with nothing but the canary's findings: the rows above are not
    red for some other reason. The canary's findings are not the repo's."""
    proc = _wrapper(_stand_in(tmp_path, "hadolint", _replies(0, _report())))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK Container SAST Layer 1 — 0 BLOCK / 0 WARN / 0 INFO" in proc.stdout, proc.stdout


def test_a_finding_is_a_finding_not_an_engine_failure(tmp_path):
    """--no-fail is what lets an error-level finding (hadolint exit 1 without
    it) reach the wrapper as a BLOCK, rc 1 — not exit 2."""
    proc = _wrapper(_stand_in(tmp_path, "hadolint", _FAITHFUL))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "[BLOCK] x/Dockerfile:1 [DL3007 error]" in proc.stdout, proc.stdout


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


# ---------------------------------------------------------------------------
# #3643: the real hadolint — nothing but the registry decides the levels
# ---------------------------------------------------------------------------
# The stand-ins above pin what the wrapper does with hadolint's output; these
# pin what the pinned hadolint does with the wrapper's command line. Each
# case below changed the verdict silently (rc 0, other levels) before #3643.
_HADOLINT = shutil.which("hadolint")
_needs_hadolint = pytest.mark.skipif(
    _HADOLINT is None, reason="hadolint not on PATH — its behaviour is not measured")

#: One Dockerfile tripping every registered rule, plus DL3019 (unregistered,
#: hadolint's default `info`); ghcr.io is trusted, so its FROM is quiet.
_DOCKERFILE = ("FROM alpine:latest\nRUN apk add curl\n"
               "FROM untrusted.invalid/x:1.0\nFROM ghcr.io/a/b:1.0\n")
_VERDICT = [("DL3007", "error"), ("DL3018", "warning"), ("DL3019", "info"),
            ("DL3026", "error")]


def test_hadolint_present_when_required():
    """A remote session installs the pinned hadolint (session-start.sh,
    #3627); there the skips below must not pass for green."""
    if os.environ.get("CLAUDE_CODE_REMOTE") == "true":
        assert _HADOLINT, ("CLAUDE_CODE_REMOTE=true but `hadolint` is not on PATH: check "
                           "/tmp/vibe-session-start-hook.ran and rerun the hook as CLAUDE.md says")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A repo root holding one Dockerfile; hadolint runs with it as cwd."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "Dockerfile").write_text(_DOCKERFILE, encoding="utf-8")
    monkeypatch.setattr(iac, "REPO_ROOT", tmp_path)
    return tmp_path


def _verdict():
    ran = iac.run_hadolint(["app/Dockerfile"])
    assert isinstance(ran, iac._lib_engine.Ran), ran
    assert {f["file"] for f in ran.data} == {"app/Dockerfile"}, ran.data   # no canary finding
    return sorted((f["code"], f["level"]) for f in ran.data)


@_needs_hadolint
def test_the_registry_s_levels_are_the_verdict(tree):
    """⛔ Behavioural pin of the registry: change a row's level, or drop a
    trusted registry, and this is red."""
    assert _verdict() == _VERDICT


@_needs_hadolint
@pytest.mark.parametrize("name, value", [
    ("HADOLINT_IGNORE", "DL3007,DL3019"),
    ("HADOLINT_OVERRIDE_INFO", "DL3007"),
    ("HADOLINT_OVERRIDE_ERROR", "DL3018"),
    ("HADOLINT_TRUSTED_REGISTRIES", "untrusted.invalid"),
    ("HADOLINT_DISABLE_IGNORE_PRAGMA", "false"),
    ("HADOLINT_FORMAT", "tty"),
    ("HADOLINT_NOFAIL", "false"),
    ("HADOLINT_FAILURE_THRESHOLD", "style"),
])
def test_a_hadolint_variable_in_the_environment_changes_nothing(tree, monkeypatch, name, value):
    """Measured on v2.12.0: the first four each beat the matching flag."""
    monkeypatch.setenv(name, value)
    assert _verdict() == _VERDICT


@_needs_hadolint
def test_an_inline_ignore_pragma_changes_nothing(tree):
    (tree / "app" / "Dockerfile").write_text(
        "# hadolint global ignore=DL3018\n# hadolint ignore=DL3007\nFROM alpine:latest\n"
        "RUN apk add curl\n# hadolint ignore=DL3026\nFROM untrusted.invalid/x:1.0\n",
        encoding="utf-8")
    assert _verdict() == _VERDICT


@_needs_hadolint
@pytest.mark.parametrize("name", [".hadolint.yaml", ".hadolint.yml"])
@pytest.mark.parametrize("config", [
    "ignored:\n  - DL3007\n  - DL3019\n",
    "override:\n  info:\n    - DL3007\n  error:\n    - DL3018\n",
    "trustedRegistries:\n  - untrusted.invalid\n",
    "override: [\n",        # the ticket's case: does not parse
    "",
], ids=["ignored", "override", "trusted-registries", "unparseable", "empty"])
def test_a_config_file_put_back_in_the_repo_is_not_read(tree, name, config):
    """hadolint discovers a config in its cwd — the repo root — unless it is
    given one. A well-formed one and a broken one both leave the verdict."""
    (tree / name).write_text(config, encoding="utf-8")
    assert _verdict() == _VERDICT


@_needs_hadolint
def test_a_changed_registry_row_reaches_the_engine(tree, monkeypatch):
    """The flags are generated from the registry: a row's new level is the
    level hadolint reports, on the canary and on the repo's files alike."""
    row = iac.HADOLINT_RULES["DL3007"]
    monkeypatch.setattr(iac, "HADOLINT_RULES", {
        **iac.HADOLINT_RULES, "DL3007": iac.HadolintRule("style", row.default, row.canary)})
    assert ("DL3007", "style") in _verdict()


@_needs_hadolint
@pytest.mark.parametrize("drop", [["--error", "DL3007"], ["--trusted-registry", "ghcr.io"],
                                  ["--trusted-registry", "docker.io"]],
                         ids=["level-flag", "registry-flag", "registry-of-a-canary-rule"])
def test_a_registry_row_that_did_not_reach_the_engine_is_no_verdict(tree, monkeypatch, drop):
    """What the canary is for: the registry says one thing, hadolint ran with
    another. DL3007 is the row off hadolint's default, so it is the one whose
    canary notices a level flag that was not applied."""
    flags = iac.hadolint_flags()
    at = next(i for i in range(0, len(flags), 2) if flags[i:i + 2] == drop)
    monkeypatch.setattr(iac, "hadolint_flags", lambda: flags[:at] + flags[at + 2:])
    result = iac.run_hadolint(["app/Dockerfile"])
    assert isinstance(result, iac._lib_engine.NoVerdict), result
    assert "canary" in result.reason, result.reason


@_needs_hadolint
def test_a_canary_that_does_not_trip_its_rule_is_no_verdict(tree, monkeypatch):
    row = iac.HADOLINT_RULES["DL3018"]
    monkeypatch.setattr(iac, "HADOLINT_RULES", {
        **iac.HADOLINT_RULES,
        "DL3018": iac.HadolintRule(row.level, row.default, "RUN apk add --no-cache curl=8.0")})
    result = iac.run_hadolint(["app/Dockerfile"])
    assert isinstance(result, iac._lib_engine.NoVerdict), result
    assert "DL3018" in result.reason, result.reason


@_needs_hadolint
def test_each_row_s_declared_default_is_hadolint_s(tree, monkeypatch):
    """`default` decides which canary counts as evidence that the level flags
    applied, so it is measured: with the trusted registries but no level
    flag, the canary comes back at exactly the declared defaults — and that
    run is no verdict against the registered levels."""
    monkeypatch.setattr(iac, "hadolint_flags", lambda: [
        a for reg in iac.HADOLINT_TRUSTED_REGISTRIES for a in ("--trusted-registry", reg)])
    assert isinstance(iac.run_hadolint(["app/Dockerfile"]), iac._lib_engine.NoVerdict)
    monkeypatch.setattr(iac, "hadolint_canary_expectation", lambda: {
        code: [rule.default] for code, rule in iac.HADOLINT_RULES.items()})
    assert isinstance(iac.run_hadolint(["app/Dockerfile"]), iac._lib_engine.Ran)


@_needs_hadolint
def test_the_wrapper_s_output_on_this_repo_ignores_the_environment():
    """The whole wrapper as a process over this repo's Dockerfiles: the same
    lines and the same exit status with HADOLINT_* variables set."""
    def run(**extra):
        env = {k: v for k, v in os.environ.items()
               if k != "PR_BODY" and not k.startswith("HADOLINT_")}
        proc = subprocess.run([sys.executable, _SCRIPT, "--ci"], capture_output=True,
                              text=True, encoding="utf-8", timeout=180,
                              env={**env, **extra}, stdin=subprocess.DEVNULL)
        return proc.returncode, proc.stdout

    plain = run()
    assert plain[0] in (0, 1) and "Container SAST Layer 1" in plain[1], plain
    assert run(HADOLINT_IGNORE="DL3018,DL3007", HADOLINT_OVERRIDE_ERROR="DL3059") == plain


# ---------------------------------------------------------------------------
# V4 (#3643): lint directives hadolint has no flag to turn off
# ---------------------------------------------------------------------------
#: Comment lines that might steer the linter. The first group is what V4
#: flags; the second it leaves alone. Which of them hadolint really acts on
#: is asked of the engine below, not asserted here.
_FLAGGED = [
    "# hadolint shell=pwsh", "#hadolint shell=pwsh", "#    hadolint    shell=pwsh",
    "#\thadolint\tshell=pwsh", "# hadolint shell = pwsh", "# hadolint shell =pwsh",
    "# hadolint shell= pwsh", "   # hadolint shell=pwsh", "# hadolint shell=pwsh because",
    "# hadolint shell=pwsh # x", '# hadolint shell="pwsh"', "# hadolint shell=cmd",
    "# hadolint shell=powershell", "# hadolint shell=bash", "# hadolint shell=/bin/zsh",
    "# hadolint shell=nonsense", "# HADOLINT shell=pwsh", "# hadolint SHELL=pwsh",
    "# Hadolint Shell=pwsh", "## hadolint shell=pwsh",
    "# shellcheck disable=SC2164", "#shellcheck disable=SC2164,SC1072",
    "#   shellcheck   disable=SC2164", "   # shellcheck disable=SC2164",
    "# shellcheck disable=all", "# SHELLCHECK disable=SC2164", "# shellcheck shell=pwsh",
    "# shellcheck source=/dev/null disable=SC2164", "# shellcheck enable=all",
    "# shellcheck", "#shellcheck", "# shellcheck is what hadolint runs on a RUN",
    "true # shellcheck disable=SC2164", "# shellchecked: yes",
]
_NOT_FLAGGED = [
    "# hadolint ignore=SC1072,SC2164,DL3003", "# hadolint global ignore=SC1072,SC2164,DL3003",
    "# hadolint global shell=pwsh", "# x hadolint shell=pwsh", "# hadolint-shell=pwsh",
    "# hadolint shell:pwsh", "# hadolint shell pwsh", "# shell=pwsh", "# disable=SC2164", "# shell check disable=SC2164",
    "# x shellcheck disable=SC2164", "# hadolint shellcheck disable=SC2164",
    "# syntax=docker/dockerfile:1", "# check=skip=all", "# an ordinary comment",
    "# rationale: no shell in this image",
]
#: Where the line goes ({d}): the top of the file, between instructions, and
#: inside a heredoc RUN. Each body has an SC finding to lose.
_PLACES = {
    "top": "{d}FROM alpine:3.19\nRUN if true; then echo hi\n",
    "before-run": "FROM alpine:3.19\n{d}RUN if true; then echo hi\n",
    "heredoc": "FROM alpine:3.19\nRUN <<EOF\n{d}cd /x\nEOF\n",
}


@pytest.mark.parametrize("line", _FLAGGED)
def test_v4_flags_a_lint_directive(line):
    hits = iac.lint_directive_lines(f"FROM a:1\n{line}\nRUN true\n")
    assert [ln for ln, _, _ in hits] == [2], hits


@pytest.mark.parametrize("line", [
    *_NOT_FLAGGED, 'SHELL ["pwsh", "-c"]', "#!/usr/bin/env pwsh", "RUN echo hadolint shell=x",
    "ENV SHELLCHECK_OPTS=-e"])
def test_v4_leaves_other_lines_alone(line):
    assert iac.lint_directive_lines(f"FROM a:1\n{line}\nRUN true\n") == []


def test_v4_is_a_block_finding_and_rc_1(monkeypatch, tmp_path):
    """A repo-content problem (rc 1), waived like any other BLOCK by the PR
    body's bypass tag — not a no-verdict."""
    df = "app/Dockerfile"
    (tmp_path / "app").mkdir()
    (tmp_path / df).write_text(
        "# hadolint shell=pwsh\nFROM gcr.io/distroless/static\n", encoding="utf-8")
    (tmp_path / ".dockerignore").write_text(
        "/.git/\n/tests/\n/scripts/\n/docs/\n*.md\n/.github/\n*.log\n.env*\n", encoding="utf-8")
    monkeypatch.setattr(iac, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(iac, "DOCKERFILE_CONTEXTS", {df: "."})
    monkeypatch.setattr(iac, "find_dockerfiles", lambda: [df])
    monkeypatch.setattr(iac, "run_hadolint", lambda dfs: _ran([]))
    blocks = iac.collect_findings([df])["BLOCK"]
    assert len(blocks) == 1 and "app/Dockerfile:1 [V4 lint-directive]" in blocks[0], blocks
    monkeypatch.setattr(sys, "argv", ["prog", "--ci"])
    monkeypatch.delenv("PR_BODY", raising=False)
    assert iac.main() == 1
    monkeypatch.setenv("PR_BODY", "bypass-lint: iac-vibe-rules\nreason: " + "word " * 31)
    assert iac.main() == 0


def _engine_verdict(tree, text):
    (tree / "app" / "Dockerfile").write_text(text, encoding="utf-8")
    ran = iac.run_hadolint(["app/Dockerfile"])
    assert isinstance(ran, iac._lib_engine.Ran), ran
    return sorted((f["code"], f["level"]) for f in ran.data)


@_needs_hadolint
@pytest.mark.parametrize("place", sorted(_PLACES))
def test_every_comment_the_engine_acts_on_is_flagged(tree, place):
    """⛔ The property V4 exists for, asked of the pinned hadolint: under the
    wrapper's own command line, a comment line that changes what hadolint
    reports is one V4 flags. `--disable-ignore-pragma` covers `ignore=`
    only (measured, v2.12.0)."""
    body = _PLACES[place]
    plain = _engine_verdict(tree, body.format(d=""))
    assert any(code.startswith("SC") for code, _ in plain), plain   # something to lose
    acted_on = []
    for line in (*_FLAGGED, *_NOT_FLAGGED):
        text = body.format(d=line + "\n")
        if _engine_verdict(tree, text) != plain:
            acted_on.append(line)
            assert iac.lint_directive_lines(text), (
                f"hadolint acts on {line!r} ({place}) and V4 does not flag it")
    # Not vacuous: the engine does act on the canonical spelling of each kind.
    canonical = "# shellcheck disable=SC2164" if place == "heredoc" else "# hadolint shell=pwsh"
    assert canonical in acted_on, acted_on


@_needs_hadolint
def test_a_real_change_of_shell_is_reported_by_the_engine_and_not_flagged(tree):
    """The other side of V4's line: SHELL and a heredoc's `#!` also change
    what hadolint reports, because they change what the image runs."""
    for place, line in (("before-run", 'SHELL ["pwsh", "-c"]'), ("heredoc", "#!/usr/bin/env pwsh")):
        body = _PLACES[place]
        assert _engine_verdict(tree, body.format(d=line + "\n")) != \
            _engine_verdict(tree, body.format(d=""))
        assert iac.lint_directive_lines(body.format(d=line + "\n")) == []


# ---------------------------------------------------------------------------
# The hook fires on the files the wrapper reads
# ---------------------------------------------------------------------------
def test_the_hook_s_files_pattern_covers_what_the_wrapper_reads():
    """`find_dockerfiles` takes `Dockerfile` and `Dockerfile.<suffix>`; V3
    reads `<Dockerfile>.dockerignore` or the context root's `.dockerignore`."""
    import re

    import yaml
    config = yaml.safe_load((iac.REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [h for repo in config["repos"] for h in repo["hooks"] if h["id"] == "iac-sast-check"]
    assert len(hooks) == 1
    fires = re.compile(hooks[0]["files"]).search
    read = set(iac.find_dockerfiles())
    for df in sorted(read):
        own = f"{df}.dockerignore"
        ctx = iac.DOCKERFILE_CONTEXTS.get(df, ".")
        read.add(own if (iac.REPO_ROOT / own).is_file()
                 else ".dockerignore" if ctx == "." else f"{ctx}/.dockerignore")
    read |= {"x/Dockerfile.dev", "Dockerfile.dev", "x/Dockerfile.dev.dockerignore",
             "x/Dockerfile.dockerignore", "scripts/tools/lint/check_iac_vibe_rules.py",
             "scripts/tools/_lib_engine.py"}
    assert not sorted(p for p in read if not fires(p))
    assert not [p for p in ("x/Dockerfiles/a.txt", "x/notDockerfile", "docs/Dockerfile.md/x",
                            ".hadolint.yaml") if fires(p)]
