"""#902 L3 — guard the Renovate config against silent no-op + incomplete coverage.

Renovate cannot run in this PR's own CI (it is owner-activated via
`.github/workflows/renovate.yaml` + a RENOVATE_TOKEN PAT). So this test is the
offline safety net: it parses `renovate.json`, applies each customManager's regex to
the ACTUAL repo files, and asserts that every one of the 15 #902 L2-pinned
third-party images is matched with a sane (depName, tag, digest) — and that the
scan-matrix refs and the deploy refs resolve to the SAME depName set (so a Renovate
bump updates both in one PR and the drift-guard stays green).

A custom-manager regex that silently matches NOTHING is the classic failure mode; it
would go unnoticed until an owner runs Renovate weeks later. Here it fails loud, in
the normal Python Tests lane.

#1354 added the built-in github-actions / dockerfile / devcontainer managers. The
tests at the bottom pin their scope: one group per manager, no digest pinning,
majors behind the Dependency Dashboard, and every pin whose other side is NOT a
Renovate dep (Go builder, runs-on, `with:` tool versions, devcontainer language
versions, the amtool COPY custom.regex already owns) disabled — a one-sided bump
would turn a parity guard red in Renovate's own PR.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RENOVATE_JSON = REPO / "renovate.json"

# Directories never worth walking when resolving managerFilePatterns.
_SKIP_DIRS = {".git", "node_modules", ".claude", "site", "__pycache__", ".mypy_cache", ".pytest_cache"}

# The 15 third-party images #902 L2 pinned. depName == the registry/repo path carried
# in the chart values, the k8s manifests, and the scan matrix. Pin a NEW third-party
# image -> add it here AND ensure a customManager matches it (this set is the SSOT the
# coverage test enforces against the config).
EXPECTED_DEPNAMES = {
    "envoyproxy/envoy",
    "quay.io/oauth2-proxy/oauth2-proxy",
    # docker.io, not quay: upstream never pushed v0.14.0 to quay (#1243).
    "prometheuscommunity/prom-label-proxy",
    "timberio/vector",
    "victoriametrics/victoria-logs",
    # chargeback-aggregator; was `python` (3.14-slim) until #1243 moved it off
    # pip-carrying images. The `python` depName rules still govern Dockerfile FROMs.
    "gcr.io/distroless/python3-debian13",
    "mariadb",
    "prom/mysqld-exporter",
    "grafana/grafana",
    "prom/prometheus",
    "prom/alertmanager",
    "registry.k8s.io/kube-state-metrics/kube-state-metrics",
    # replaced ghcr.io/jimmidyson/configmap-reload (release-dormant; #1243).
    "quay.io/prometheus-operator/prometheus-config-reloader",
    "alpine/git",
    # #1316 store-mount preflight init-container, in BOTH federation consumer
    # charts. Deployed from two values.yaml files with one digest, so Renovate
    # has to bump BOTH. ⛔ The shared-depname invariant below does NOT prove
    # that — it compares depName SETS, and `busybox` is in the set whether one
    # file matches or two, so a manager that saw only one would leave the other
    # pinned to a stale digest with every test here green.
    # test_busybox_is_matched_in_both_consumer_charts is the one that proves it.
    "busybox",
}

# #2442: pins Renovate owns that are NOT deployed images — test infrastructure
# only. Kept out of EXPECTED_DEPNAMES on purpose: that set is also the scan-matrix
# SSOT (test_scan_matrix_and_deploy_refs_share_depnames), and a forge-e2e image is
# neither deployed nor scanned. Each one gets its own customManager and group.
FORGE_E2E_DEPNAMES = {"gitlab/gitlab-ce"}

# #1337 ①: images ONLY the try-local demo stack pulls. They are in the scan
# matrix (so the matrix manager extracts them) but in no deploy file, so they
# sit outside the matrix == deploy depName invariant. Renovate bumps only their
# matrix row; the compose file stays tag-only with no updater, and
# test_every_trylocal_image_is_in_the_scan_matrix goes red on that Renovate PR
# until try-local is moved to the same tag — the accepted cost of A′.
TRY_LOCAL_ONLY_DEPNAMES = {"prom/pushgateway", "curlimages/curl"}
FORGE_E2E_SCRIPT = "scripts/ops/forge_e2e_run.sh"


# #1354 option B: the built-in managers the owner approved, on top of custom.regex.
BUILTIN_MANAGERS = ("github-actions", "dockerfile", "devcontainer")
ENABLED_MANAGERS = ("custom.regex", *BUILTIN_MANAGERS)


def _load_config() -> dict:
    return json.loads(RENOVATE_JSON.read_text(encoding="utf-8"))


def _py_regex(renovate_pattern: str) -> re.Pattern:
    # Renovate uses RE2 named groups `(?<name>...)`; Python's re wants `(?P<name>...)`.
    # The constructs used in this config (named groups, [\s\S], non-greedy, [ \t]) are
    # otherwise RE2/Python-compatible, so the translation is faithful for validation.
    #
    # ⚠️ RE2 (Renovate's engine) does NOT support lookaround — (?=...) (?!...) (?<=...)
    #    (?<!...) — but Python's `re` DOES. A matchString using them would PASS this
    #    suite yet CRASH Renovate at runtime (false confidence). test_no_re2_lookaround()
    #    forbids them so this offline guard can't lie about a config Renovate can't run.
    translated = re.sub(r"\(\?<([a-zA-Z][a-zA-Z0-9]*)>", r"(?P<\1>", renovate_pattern)
    return re.compile(translated)


def _manager_file_patterns(mgr: dict) -> list[str]:
    return mgr.get("managerFilePatterns", mgr.get("fileMatch", []))


def _files_for_manager(mgr: dict) -> list[Path]:
    """Resolve a manager's regex-form (`/.../`) file patterns to actual repo files."""
    regexes = []
    for p in _manager_file_patterns(mgr):
        assert p.startswith("/") and p.endswith("/"), f"expected regex-form pattern, got {p!r}"
        regexes.append(re.compile(p[1:-1]))
    out: list[Path] = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            rel = (Path(root) / fn).relative_to(REPO).as_posix()
            if any(rx.search(rel) for rx in regexes):
                out.append(Path(root) / fn)
    return out


def _extract(mgr: dict) -> list[dict]:
    """Apply a customManager's matchStrings to its files; return captured deps."""
    regexes = [_py_regex(s) for s in mgr["matchStrings"]]
    found: list[dict] = []
    for f in _files_for_manager(mgr):
        text = f.read_text(encoding="utf-8")
        for rx in regexes:
            for m in rx.finditer(text):
                found.append({"file": f.relative_to(REPO).as_posix(), **m.groupdict()})
    return found


def _matrix_manager(cfg: dict) -> dict:
    return next(m for m in cfg["customManagers"]
                if "nightly-image-scan" in "".join(_manager_file_patterns(m)))


def _forge_manager(cfg: dict) -> dict:
    return next(m for m in cfg["customManagers"]
                if "forge_e2e_run" in "".join(_manager_file_patterns(m)))


def _deploy_managers(cfg: dict) -> list[dict]:
    """The customManagers that own DEPLOYED refs (helm + k8s + da-tools COPY)."""
    skip = (_matrix_manager(cfg), _forge_manager(cfg))
    return [m for m in cfg["customManagers"] if not any(m is x for x in skip)]


def test_renovate_json_is_strict_json_and_well_formed():
    cfg = _load_config()
    # #1354: exactly these managers. docker-compose stays OFF (the tests/** compose
    # images are held by bindings, not Renovate — see
    # test_federation_e2e_mirrors_deploy_sot.py); gomod stays OFF (go.mod is the Go
    # SSOT and lang-dep bumps are #902 L3 Category C, deliberately out).
    assert sorted(cfg["enabledManagers"]) == sorted(ENABLED_MANAGERS)
    assert cfg.get("pinDigests") is True
    assert len(cfg["customManagers"]) == 4
    assert cfg.get("packageRules"), "expected grouping + major-approval rules"


def test_every_custom_manager_matches_something():
    """The classic custom-manager failure is a regex that matches NOTHING. Assert each
    manager extracts >=1 dep, each with a non-empty tag and a well-formed sha256."""
    cfg = _load_config()
    for mgr in cfg["customManagers"]:
        deps = _extract(mgr)
        assert deps, f"customManager matched nothing: {mgr.get('description', '')[:70]}"
        for d in deps:
            assert d.get("depName"), f"empty depName in {d['file']}"
            assert d.get("currentValue"), f"empty currentValue ({d.get('depName')} in {d['file']})"
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", d.get("currentDigest", "")), \
                f"bad digest for {d.get('depName')} in {d['file']}: {d.get('currentDigest')!r}"


def test_coverage_is_complete_and_exact():
    """Every #902-pinned third-party image is covered — and nothing extra (a stray
    match would mean Renovate touches an unintended ref)."""
    cfg = _load_config()
    seen = {d["depName"] for mgr in cfg["customManagers"] for d in _extract(mgr)}
    expected = EXPECTED_DEPNAMES | FORGE_E2E_DEPNAMES | TRY_LOCAL_ONLY_DEPNAMES
    assert seen == expected, (
        f"\n  missing (pinned but Renovate won't bump): {sorted(expected - seen)}"
        f"\n  unexpected (Renovate would touch):       {sorted(seen - expected)}"
    )


def test_scan_matrix_and_deploy_refs_share_depnames():
    """Grouping invariant: the scan-matrix refs and the deploy refs (helm + k8s) must
    resolve to the SAME depName set, so a Renovate bump updates both in one PR and the
    drift-guard (matrix == deploy refs) stays green."""
    cfg = _load_config()
    matrix_mgr = _matrix_manager(cfg)
    matrix_names = {d["depName"] for d in _extract(matrix_mgr)}
    deploy_names = {d["depName"] for m in _deploy_managers(cfg) for d in _extract(m)}
    assert matrix_names == EXPECTED_DEPNAMES | TRY_LOCAL_ONLY_DEPNAMES, (
        f"matrix missing: {sorted((EXPECTED_DEPNAMES | TRY_LOCAL_ONLY_DEPNAMES) - matrix_names)}; "
        f"unexpected: {sorted(matrix_names - EXPECTED_DEPNAMES - TRY_LOCAL_ONLY_DEPNAMES)}")
    assert deploy_names == EXPECTED_DEPNAMES, f"deploy missing: {sorted(EXPECTED_DEPNAMES - deploy_names)}"


def test_busybox_is_matched_in_both_consumer_charts():
    """#1316 pins ONE image in TWO charts, and a digest refresh has to reach both.

    ⛔ Asserted per FILE, because the set-based invariants cannot see this: they
    compare depName sets, and `busybox` is present whether Renovate matches one
    values.yaml or both. A manager whose file pattern reached only the gateway
    chart would leave the reconciler pinned to a stale digest — with the scan
    matrix, the depname coverage test and the shared-depname test all green.
    """
    cfg = _load_config()
    matrix_mgr = _matrix_manager(cfg)
    files = {d["file"] for m in cfg["customManagers"] if m is not matrix_mgr
             for d in _extract(m) if d.get("depName") == "busybox"}
    assert files == {
        "helm/federation-gateway/values.yaml",
        "helm/federation-reconciler/values.yaml",
    }, f"busybox is not matched in both consumer charts, only: {sorted(files)}"


def test_alertmanager_is_matched_in_manifest_and_da_tools_dockerfile():
    """#2294: the da-tools image bundles amtool via `COPY --from=<the deployed
    Alertmanager ref>`, and test_da_tools_amtool_pin_parity.py requires the two
    refs to be identical. Renovate has to bump BOTH in one PR or every
    Alertmanager bump turns that guard red. Asserted per FILE for the same
    reason as busybox: `prom/alertmanager` is in the depName set either way."""
    cfg = _load_config()
    matrix_mgr = _matrix_manager(cfg)
    hits = [d for m in cfg["customManagers"] if m is not matrix_mgr
            for d in _extract(m) if d.get("depName") == "prom/alertmanager"]
    assert {d["file"] for d in hits} == {
        "k8s/03-monitoring/deployment-alertmanager.yaml",
        "components/da-tools/app/Dockerfile",
    }, f"prom/alertmanager is not matched in both files, only: {sorted(d['file'] for d in hits)}"
    assert len({(d["currentValue"], d["currentDigest"]) for d in hits}) == 1, hits


def test_forge_e2e_gitlab_pin_is_owned_by_its_own_manager():
    """#2442: the forge-e2e GitLab CE pin (a shell default, which no built-in
    manager parses) is matched exactly once, in the script CI actually runs, and
    by no other manager. Asserted per FILE for the same reason as busybox."""
    cfg = _load_config()
    forge = _forge_manager(cfg)
    hits = _extract(forge)
    assert [(d["file"], d["depName"]) for d in hits] == [(FORGE_E2E_SCRIPT, "gitlab/gitlab-ce")], hits
    others = [d for m in cfg["customManagers"] if m is not forge for d in _extract(m)
              if d["depName"] in FORGE_E2E_DEPNAMES]
    assert not others, f"another manager also owns the forge-e2e pin: {others}"


def test_forge_e2e_gitlab_versioning_parses_the_real_tag():
    """GitLab tags are `X.Y.Z-ce.N`. Under `docker` versioning the `-ce.N` part is
    a compatibility suffix, so a regex versioning is used instead — and a regex
    that does not match the CURRENT tag makes Renovate skip the dep silently.
    Parse the tag the script really carries, and reject a floating one."""
    cfg = _load_config()
    forge = _forge_manager(cfg)
    template = forge.get("versioningTemplate", "")
    assert template.startswith("regex:"), template
    rx = _py_regex(template[len("regex:"):])
    (dep,) = _extract(forge)
    m = rx.match(dep["currentValue"])
    assert m and {"major", "minor", "patch"} <= set(k for k, v in m.groupdict().items() if v), (
        f"versioning regex does not parse the pinned tag {dep['currentValue']!r}: {template}")
    assert not rx.match("latest") and not rx.match("nightly"), "versioning regex accepts a floating tag"


def test_forge_e2e_gitlab_has_its_own_group_and_gated_majors():
    """Test infrastructure must not ride the third-party DEPLOY group PR, and a
    GitLab major (config/upgrade-path changes) waits for the Dashboard."""
    cfg = _load_config()
    image_group = _resolve(cfg, manager="custom.regex", dep="grafana/grafana",
                           datasource="docker", update="minor").get("groupName")
    for update in ("patch", "minor", "digest"):
        got = _resolve(cfg, manager="custom.regex", dep="gitlab/gitlab-ce",
                       datasource="docker", update=update)
        assert got["enabled"] is True, got
        assert got.get("groupName") and got["groupName"] != image_group, (update, got)
        assert not got.get("dependencyDashboardApproval"), (update, got)
    major = _resolve(cfg, manager="custom.regex", dep="gitlab/gitlab-ce",
                     datasource="docker", update="major")
    assert major.get("dependencyDashboardApproval") is True, major


def test_renovate_config_validator_if_available():
    """If the official validator is installed, the config must pass its schema check.
    Skipped where renovate isn't available (e.g. the Python Tests CI lane has no node)."""
    validator = shutil.which("renovate-config-validator")
    if not validator:
        pytest.skip("renovate-config-validator not installed (node/renovate absent)")
    res = subprocess.run([validator, str(RENOVATE_JSON)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    assert res.returncode == 0, f"renovate-config-validator failed:\n{res.stdout}\n{res.stderr}"


def test_no_re2_incompatible_lookarounds():
    """Renovate's regex engine is RE2 (linear-time guarantee) — it does NOT support
    lookahead/lookbehind. Python's `re` DOES, so a matchString using them would PASS
    the rest of this suite yet CRASH Renovate at runtime (false confidence). Forbid
    them in the config so this offline guard can never green a config Renovate can't
    parse. (Named groups `(?<name>...)` are fine — only `(?<=`/`(?<!`/`(?=`/`(?!` are
    lookaround.)"""
    cfg = _load_config()
    banned = ["(?=", "(?!", "(?<=", "(?<!"]
    for mgr in cfg["customManagers"]:
        for s in mgr["matchStrings"]:
            for tok in banned:
                assert tok not in s, (
                    f"RE2 (Renovate) does not support lookaround {tok!r}; it would crash "
                    f"at runtime though Python's re accepts it. Offending matchString: {s[:90]}"
                )


def test_python_minor_bump_requires_dashboard_approval():
    """`python:3.x-slim` numbers its tag by Python FEATURE release, so a 3.13→3.14
    bump is semver-MINOR to Docker but a real language-version jump. Assert a
    packageRule gates python's MINOR updates behind dependencyDashboardApproval (so
    it can't silently auto-flow in the grouped PR), while NOT gating its digest-only
    refresh — the security re-pin must stay automatic."""
    cfg = _load_config()
    gated = [
        r for r in cfg.get("packageRules", [])
        if "python" in r.get("matchDepNames", [])
        and "minor" in r.get("matchUpdateTypes", [])
        and r.get("dependencyDashboardApproval") is True
    ]
    assert gated, "expected a packageRule gating python MINOR bumps behind the Dependency Dashboard"
    for r in gated:
        assert "digest" not in r.get("matchUpdateTypes", []), (
            "python digest-only refresh must stay automatic — only the version (minor) "
            "bump should wait for a Dependency Dashboard tick"
        )


# ── #1354: built-in managers ────────────────────────────────────────────────
#
# The assertions below evaluate the packageRules the way Renovate does (every
# `match*` of a rule must hold; later rules override earlier ones) for concrete
# (manager, dep, updateType) cases, instead of grepping for the rule text: a rule
# that is present but shadowed, mis-scoped or mis-spelled reads as "there" to a
# grep and as "not applied" here. The emulator only models exact-string matchers;
# a rule using anything else (glob, /regex/, `!` negation, another match* key)
# FAILS the suite rather than being mis-evaluated. Cross-checked against
# Renovate 44's own applyPackageRules when this was written (#1354).

_EMULATED_MATCHERS = {
    "matchManagers", "matchDatasources", "matchDepTypes",
    "matchDepNames", "matchPackageNames", "matchUpdateTypes",
}


def _resolve(cfg: dict, *, manager: str, dep: str, datasource: str,
             update: str, dep_type: str | None = None) -> dict:
    facts = {
        "matchManagers": manager, "matchDatasources": datasource,
        "matchDepTypes": dep_type, "matchDepNames": dep,
        "matchPackageNames": dep, "matchUpdateTypes": update,
    }
    out = {"enabled": True, "pinDigests": cfg.get("pinDigests", False)}
    for rule in cfg.get("packageRules", []):
        keys = {k for k in rule if k.startswith(("match", "exclude"))}
        assert keys <= _EMULATED_MATCHERS, (
            f"packageRule uses {sorted(keys - _EMULATED_MATCHERS)}, which this "
            f"emulator does not model — extend _resolve() before relying on it: "
            f"{rule.get('description', '')[:80]}")
        for k in keys:
            for v in rule[k]:
                assert not (v.startswith(("/", "!")) or "*" in v), (
                    f"{k} value {v!r} is a pattern; the emulator matches exact strings only")
        if all(facts[k] in rule[k] for k in keys):
            out.update({k: v for k, v in rule.items() if k not in keys and k != "description"})
    return out


# (manager, dep, datasource, depType, update) that must NEVER get a Renovate PR,
# each because its counterpart is not a Renovate dep (a one-sided bump is red).
_MUST_BE_DISABLED = [
    # go.mod is the Go SSOT; test_go_toolchain_parity.py binds every builder to it.
    ("dockerfile", "golang", "docker", "stage", "patch"),
    ("dockerfile", "golang", "docker", "stage", "minor"),
    # runs-on == dev container base image (test_toolchain_pin_parity.py, runner).
    ("github-actions", "ubuntu", "github-runners", "github-runner", "major"),
    ("devcontainer", "mcr.microsoft.com/devcontainers/base", "docker", "image", "major"),
    ("devcontainer", "mcr.microsoft.com/devcontainers/base", "docker", "image", "minor"),
    # `with:` tool versions, each bound to a devcontainer / Makefile / wrapper pin.
    ("github-actions", "node", "github-releases", "uses-with", "major"),
    ("github-actions", "python", "github-releases", "uses-with", "minor"),
    ("github-actions", "helm", "github-releases", "uses-with", "minor"),
    ("github-actions", "aquasecurity/trivy", "github-releases", "uses-with", "minor"),
    ("github-actions", "sigstore/cosign", "github-releases", "uses-with", "patch"),
    # language versions inside the dev container features, bound to CI / go.mod.
    ("devcontainer", "node", "node-version", None, "major"),
    ("devcontainer", "python", "python-version", None, "minor"),
    ("devcontainer", "go", "golang-version", None, "patch"),
]


@pytest.mark.parametrize("manager,dep,datasource,dep_type,update", _MUST_BE_DISABLED)
def test_ci_bound_pins_are_not_renovates(manager, dep, datasource, dep_type, update):
    got = _resolve(_load_config(), manager=manager, dep=dep, datasource=datasource,
                   dep_type=dep_type, update=update)
    assert got["enabled"] is False, (
        f"{manager} would open a PR for {dep} ({dep_type}, {update}); its other side "
        f"is not a Renovate dep, so the bump is one-sided and a parity guard goes red.")


@pytest.mark.parametrize("manager,dep,datasource,dep_type", [
    ("github-actions", "aquasecurity/trivy-action", "github-tags", "action"),
    ("dockerfile", "alpine", "docker", "final"),
    ("devcontainer", "ghcr.io/devcontainers/features/go", "docker", "feature"),
])
def test_each_builtin_manager_is_one_group_without_digest_pinning(manager, dep, datasource, dep_type):
    """One PR per manager (so every occurrence of a dep moves together — the
    single-version-per-action guard), and no pin-to-digest/SHA side effect."""
    cfg = _load_config()
    got = _resolve(cfg, manager=manager, dep=dep, datasource=datasource,
                   dep_type=dep_type, update="minor")
    assert got["enabled"] is True, f"{manager}/{dep} is disabled"
    assert got.get("groupName"), f"{manager}/{dep} has no groupName — one PR per dep"
    assert got["pinDigests"] is False, (
        f"{manager}/{dep} inherits the global pinDigests=true: Renovate would open a "
        f"pin PR rewriting every ref to a digest/SHA (deliberately out of #1354).")
    assert not got.get("dependencyDashboardApproval"), f"{manager}/{dep} minor is gated"


def test_builtin_manager_groups_are_distinct_and_not_the_image_group():
    cfg = _load_config()
    samples = {
        "github-actions": ("aquasecurity/trivy-action", "github-tags", "action"),
        "dockerfile": ("alpine", "docker", "final"),
        "devcontainer": ("ghcr.io/devcontainers/features/go", "docker", "feature"),
    }
    groups = {m: _resolve(cfg, manager=m, dep=d, datasource=ds, dep_type=t, update="patch").get("groupName")
              for m, (d, ds, t) in samples.items()}
    image_group = _resolve(cfg, manager="custom.regex", dep="grafana/grafana",
                           datasource="docker", update="minor").get("groupName")
    assert image_group, "the #902 image group vanished"
    assert len(set(groups.values())) == len(groups) and image_group not in groups.values(), (
        f"built-in managers must each have their own group, separate from the #902 "
        f"image group {image_group!r}: {groups}")


@pytest.mark.parametrize("manager,dep,datasource,dep_type", [
    ("github-actions", "actions/checkout", "github-tags", "action"),
    ("dockerfile", "alpine", "docker", "final"),
    ("devcontainer", "ghcr.io/devcontainers/features/node", "docker", "feature"),
])
def test_builtin_manager_majors_wait_for_the_dashboard(manager, dep, datasource, dep_type):
    got = _resolve(_load_config(), manager=manager, dep=dep, datasource=datasource,
                   dep_type=dep_type, update="major")
    assert got["enabled"] is True and got.get("dependencyDashboardApproval") is True, got


@pytest.mark.parametrize("dep", ["sigstore/cosign-installer", "anchore/sbom-action"])
def test_signing_chain_actions_wait_for_the_dashboard(dep):
    """release.yaml: 'bump deliberately' — never ride the grouped Actions PR."""
    got = _resolve(_load_config(), manager="github-actions", dep=dep,
                   datasource="github-tags", dep_type="action", update="patch")
    assert got.get("dependencyDashboardApproval") is True, got
    # Approval alone is not enough: without a group of its own the dep inherits
    # the general `GitHub Actions` groupName and, once approved, lands in the same
    # PR as every unrelated action bump (CodeRabbit on #2383).
    assert got.get("groupName") == "release signing chain", got
    assert got.get("groupName") != _resolve(
        _load_config(), manager="github-actions", dep="actions/checkout",
        datasource="github-tags", dep_type="action", update="patch").get("groupName"), got


def test_dockerfile_manager_never_co_owns_a_custom_regex_ref():
    """The dockerfile manager also extracts `COPY --from=<image>` refs. Any Dockerfile
    ref custom.regex already owns (today: the da-tools amtool COPY, #2294) must be
    disabled for the dockerfile manager, or the same line has two owners in two
    groups and the pair custom.regex keeps in ONE PR splits. Derived from what
    custom.regex actually extracts, not from a list written here."""
    cfg = _load_config()
    owned = {d["depName"] for m in cfg["customManagers"] for d in _extract(m)
             if Path(d["file"]).name.startswith("Dockerfile")}
    assert "prom/alertmanager" in owned, (
        f"expected custom.regex to own the da-tools amtool COPY; owned={sorted(owned)}")
    co_owned = [dep for dep in sorted(owned)
                if _resolve(cfg, manager="dockerfile", dep=dep, datasource="docker",
                            dep_type="final", update="patch")["enabled"]]
    assert not co_owned, f"dockerfile manager would also bump custom.regex refs: {co_owned}"


def test_tests_tree_is_out_of_renovates_reach():
    """tests/** images (the federation-e2e compose mirrors, the bench Dockerfiles)
    are held by bindings (test_federation_e2e_mirrors_deploy_sot.py, _SCAN_EXEMPT),
    not by Renovate. ignorePaths is spelled out here rather than inherited from
    config:recommended so a preset change cannot silently widen the reach."""
    cfg = _load_config()
    assert "docker-compose" not in cfg["enabledManagers"]
    ignored = set(cfg.get("ignorePaths", []))
    assert {"**/tests/**", "**/test/**"} <= ignored, sorted(ignored)
    anchors = [p for p in (REPO / "tests").rglob("*")
               if p.name == "Dockerfile" or p.name.startswith("docker-compose")]
    assert anchors, "no Dockerfile / compose under tests/ — re-derive what this guards"
