#!/usr/bin/env python3
"""check_cross_ns_url_consistency.py — cross-namespace service-URL consistency lint (#1004)

Locks in the #1004 namespace ruling: every governed service has ONE canonical
namespace — `tenant-api` lives in its dedicated `tenant-api` namespace (raw
v2.4.0 intent + GHSA blast-radius isolation), `recipe-preview` lives in
`monitoring` — and every service URL in Helm values, raw k8s manifests, the
portal nginx proxy and try-local must agree with it.

WHY: the past drift this lint prevents happened twice, in two shapes:
  - Helm charts / docs hardcoded `tenant-api.monitoring.svc.cluster.local`
    while the raw manifests deployed tenant-api into the `tenant-api` ns —
    the FQDN resolved to NOTHING on a raw-manifest cluster.
  - recipe-preview called bare `http://tenant-api:8080` from the `monitoring`
    ns — a bare service name only resolves inside the CALLER's own namespace,
    so the cross-ns call was unresolvable.
All instances are fixed in the #1004 ns-hardening PR; this lint turns "fixed
once" into "CI won't let it drift back". kube-linter cannot express this
semantic check (verified: .kube-linter.yaml carries only built-in checks), so
per the hybrid lint policy this is a Vibe wrapper with NO open-source engine
— same class as check_helm_values_secrets.py (L3).

WHAT THIS CHECKS (two rules, applied to string scalars — PARSE, not grep, so
YAML comments never false-positive):
  R1 wrong-namespace FQDN: any scalar containing `<svc>.<ns>.svc[.cluster.
     local]` where svc is governed and ns != its canonical namespace.
     Context-free — a wrong-ns FQDN is wrong everywhere (incl. try-local).
  R2 bare-name cross-ns: a URL-ish bare host — `http(s)://<svc>[:port]` or
     `<svc>:<port>` — where svc is governed and the scan context's namespace
     is NOT the canonical one (or is unknown → conservative violation). A
     bare name in its canonical namespace is legitimate same-ns DNS.

Scan targets + deploy-contract context (CHART_CONTEXT_NS / per-doc
metadata.namespace):
  1. helm/*/values*.yaml       R1+R2, context = the chart's deploy contract
  2. k8s/**/*.yaml             R1+R2, context = each doc's metadata.namespace
  3. components/da-portal/nginx.conf  R1+R2, context = monitoring (da-portal)
  4. try-local/docker-compose.yaml    R1 ONLY — bare names are legitimate
     inside compose's single network; only FQDN-form drift matters there.

WHAT THIS DOES **NOT** CHECK (deliberately):
  - A scalar that is EXACTLY the bare service name with no scheme/port
    (`name: tenant-api`, `app: tenant-api` labels, the chart name itself,
    `tenant-api-netpol`) — those are object names / selectors, not hosts.
    Only URL-ish shapes (`://host`, `host:port`, dotted `.svc`) are flagged.
  - Host+port split across two YAML keys (`host: tenant-api` + `port: 8080`)
    — accepted residual; no such pattern exists in the tree today.
  - Helm templates / docs prose — values*.yaml are the URL SSOT in this repo
    (templates reference `.Values.*`); doc drift is cheap to fix and noisy to
    gate. An unmapped NEW chart gets context None → R2 fires conservatively,
    forcing a deliberate CHART_CONTEXT_NS entry (same governance as L2's
    EXEMPTIONS registry).

DATA vs POLICY (#1004, Option D — "separate policy from data"): the governed
services, chart deploy-contract namespaces and the exemption registry are DATA
and live in an external YAML file (cross_ns_url_lint.config.yaml, co-located
with this script). A non-Python contributor edits the YAML, never this code;
the LOGIC (regexes, scan, verdict) stays here. The config is loaded at import
and FAILS CLOSED — a missing / unparseable / schema-invalid config is a
caller-error (exit 2), never a silent pass (this is a security control). The
config file is co-located under scripts/tools/lint/ specifically so it is
OUTSIDE every scan glob (helm/*/values*.yaml, k8s/**/*.yaml) and both fixed
targets — the lint never scans its own data.

EXEMPTIONS: central registry in the config file (path + substring + rationale
+ exit condition; every entry MUST carry a non-empty exit_condition, enforced
at load). A matched hit is reported as INFO, not a violation — same reporting
style as L2/L4 baseline exemptions.

Exit codes:
    0  every service URL agrees with the canonical namespaces
    1  violations present (--ci)
    2  caller error (a fixed scan target is missing / YAML unparseable /
       the config file is missing, unparseable, or schema-invalid)

Usage:
    python3 scripts/tools/lint/check_cross_ns_url_consistency.py        # report
    python3 scripts/tools/lint/check_cross_ns_url_consistency.py --ci   # exit 1
    python3 scripts/tools/lint/check_cross_ns_url_consistency.py \
        --config path/to/alt.config.yaml   # override data file (testing)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import yaml

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

SKIP_DIR_PARTS = {".claude", ".git", "node_modules", ".venv", "venv"}

# --- Config DATA file (Option D: policy here, data external) ------------------
# Co-located with this script *on purpose*: scripts/tools/lint/ is OUTSIDE every
# scan glob (helm/*/values*.yaml, k8s/**/*.yaml) and both fixed targets, so the
# governed strings the config carries can never be flagged as the lint's own
# data (self-scan guard; proven by test).
DEFAULT_CONFIG_PATH = os.path.join(_THIS_DIR, "cross_ns_url_lint.config.yaml")

# Fixed (non-glob) scan targets — missing means the lint's subject moved
# (caller-error, like check_single_writer_invariant's _TARGETS contract).
_NGINX_CONF = "components/da-portal/nginx.conf"
_COMPOSE = "try-local/docker-compose.yaml"
_FIXED_TARGETS = [_NGINX_CONF, _COMPOSE]


class ConfigError(Exception):
    """Raised when the config DATA file is missing, unparseable, or fails
    schema validation. main() turns this into EXIT_CALLER_ERROR (2) — this is
    a security control, so a config problem FAILS CLOSED, never a silent 0."""


def load_config(path: str = DEFAULT_CONFIG_PATH):
    """Read + validate the external config YAML; return the three internal
    structures the rest of the module uses:

        (GOVERNED_SERVICES: dict[str, str],
         CHART_CONTEXT_NS:   dict[str, Optional[str]],
         EXEMPTIONS:         dict[tuple[str, str], tuple[str, str]])

    FAILS CLOSED: any missing file / parse error / schema violation raises
    ConfigError (never returns partial data), which main() reports as
    EXIT_CALLER_ERROR. Exposed as a function so tests can point it at a temp
    fixture file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"config data file not found: {path} — the lint FAILS CLOSED on a "
            f"missing config (it cannot know the canonical namespaces)."
        ) from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config data file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config data file {path} is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"config data file {path} must be a YAML mapping at top level, "
            f"got {type(data).__name__}."
        )

    for key in ("governed_services", "chart_context_ns", "exemptions"):
        if key not in data:
            raise ConfigError(
                f"config data file {path} is missing required top-level key "
                f"`{key}`."
            )

    # governed_services: non-empty dict[str, str]
    gov = data["governed_services"]
    if not isinstance(gov, dict) or not gov:
        raise ConfigError(
            f"config {path}: `governed_services` must be a non-empty mapping."
        )
    for svc, ns in gov.items():
        if not isinstance(svc, str) or not svc:
            raise ConfigError(
                f"config {path}: `governed_services` has a non-string / empty "
                f"service key ({svc!r})."
            )
        if not isinstance(ns, str) or not ns:
            raise ConfigError(
                f"config {path}: `governed_services[{svc}]` must be a non-empty "
                f"string namespace, got {ns!r}."
            )
    governed_services: dict[str, str] = dict(gov)

    # chart_context_ns: dict[str, str | None]
    charts = data["chart_context_ns"]
    if not isinstance(charts, dict):
        raise ConfigError(
            f"config {path}: `chart_context_ns` must be a mapping."
        )
    for chart, ns in charts.items():
        if not isinstance(chart, str) or not chart:
            raise ConfigError(
                f"config {path}: `chart_context_ns` has a non-string / empty "
                f"chart key ({chart!r})."
            )
        if ns is not None and (not isinstance(ns, str) or not ns):
            raise ConfigError(
                f"config {path}: `chart_context_ns[{chart}]` must be a "
                f"non-empty string or null (tenant-scoped), got {ns!r}."
            )
    chart_context_ns: dict[str, Optional[str]] = dict(charts)

    # exemptions: list of mappings, each with non-empty path / substring /
    # rationale / exit_condition. Rebuilt into the (path, substring) ->
    # (rationale, exit_condition) shape the rest of the code already expects.
    exemptions_raw = data["exemptions"]
    if not isinstance(exemptions_raw, list):
        raise ConfigError(
            f"config {path}: `exemptions` must be a list (may be empty)."
        )
    exemptions: dict[tuple[str, str], tuple[str, str]] = {}
    for i, entry in enumerate(exemptions_raw):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"config {path}: exemptions[{i}] must be a mapping."
            )
        for field in ("path", "substring", "rationale", "exit_condition"):
            val = entry.get(field)
            if not isinstance(val, str) or not val.strip():
                raise ConfigError(
                    f"config {path}: exemptions[{i}] is missing a non-empty "
                    f"`{field}` (every exemption MUST carry a non-empty "
                    f"exit_condition and identifying path/substring/rationale)."
                )
        exemptions[(entry["path"], entry["substring"])] = (
            entry["rationale"], entry["exit_condition"]
        )

    return governed_services, chart_context_ns, exemptions


def _build_matchers(governed_services: dict[str, str]):
    """Compile the R1/R2 regexes from the governed-service alternation. Kept a
    function so the module-level constants AND any test using an alternate
    config are built the same way — all matchers derive from GOVERNED_SERVICES,
    so they MUST be built after the config loads."""
    svc_alt = "|".join(sorted(governed_services))
    # R1: `<svc>.<ns>.svc` (also matches the `.svc.cluster.local` long form).
    # Lookbehind rejects `x-tenant-api.…` / `foo.tenant-api.…` (different host);
    # lookahead rejects `….svcx`. `/` is allowed before (URL `://<svc>.…`).
    fqdn_re = re.compile(
        rf"(?<![\w.-])(?P<svc>{svc_alt})\."
        rf"(?P<ns>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.svc(?![\w-])"
    )
    # R2a: scheme + bare host. Lookahead rejects FQDNs (`http://tenant-api.…`)
    # and longer names (`http://tenant-api-canary`).
    bare_scheme_re = re.compile(rf"https?://(?P<svc>{svc_alt})(?![\w.-])")
    # R2b: `<svc>:<port>` host:port shape. Lookbehind additionally rejects `/`
    # so `http://tenant-api:8080` isn't double-counted (R2a owns it) and an
    # image ref `registry/tenant-api:1234` isn't a host. Lookahead keeps YAML
    # keys (`tenant-api:` + space/EOL) and label-ish values out: a port must
    # follow the colon immediately.
    bare_hostport_re = re.compile(rf"(?<![\w.\-/])(?P<svc>{svc_alt}):\d+(?!\w)")
    return svc_alt, fqdn_re, bare_scheme_re, bare_hostport_re


# --- Import-time load from the default config --------------------------------
# The module-level constants + regexes below are DERIVED from the config, so
# they are built AFTER load_config() succeeds — every downstream function and
# every existing test references these names, so they must exist at import.
# The load is GUARDED: a config problem is stashed as _CONFIG_LOAD_ERROR (a
# ConfigError) rather than crashing import with a raw traceback, and main()
# reports it as EXIT_CALLER_ERROR. GOVERNED_SERVICES etc. fall back to empty so
# the names exist; the guard in main() ensures we never run a scan on empty
# data and silently pass.
GOVERNED_SERVICES: dict[str, str] = {}
CHART_CONTEXT_NS: dict[str, Optional[str]] = {}
EXEMPTIONS: dict[tuple[str, str], tuple[str, str]] = {}
_CONFIG_LOAD_ERROR: Optional[ConfigError] = None
try:
    GOVERNED_SERVICES, CHART_CONTEXT_NS, EXEMPTIONS = load_config()
except ConfigError as _exc:  # pragma: no cover - exercised via reload in tests
    _CONFIG_LOAD_ERROR = _exc

_SVC_ALT, _FQDN_RE, _BARE_SCHEME_RE, _BARE_HOSTPORT_RE = _build_matchers(
    GOVERNED_SERVICES
)

# A finding: (rel_path, line_or_None, keypath_or_None, matched_fragment,
#             containing_scalar, message)
Finding = Tuple[str, Optional[int], Optional[str], str, str, str]


def _expected(svc: str) -> str:
    return f"{svc}.{GOVERNED_SERVICES[svc]}.svc.cluster.local"


def scan_scalar(value: str, context_ns: Optional[str],
                apply_bare: bool = True) -> List[Tuple[str, str]]:
    """Pure core: (matched_fragment, message) per R1/R2 hit in one string.

    `context_ns` is the namespace the surrounding manifest deploys into
    (None = unknown → R2 fires conservatively). `apply_bare=False` runs R1
    only (the docker-compose mode — bare names are fine on one network)."""
    out: List[Tuple[str, str]] = []
    for m in _FQDN_RE.finditer(value):
        svc, ns = m.group("svc"), m.group("ns")
        if ns != GOVERNED_SERVICES[svc]:
            out.append((m.group(0),
                        f"FQDN pins `{svc}` to WRONG namespace `{ns}` "
                        f"(expected {_expected(svc)})"))
    if not apply_bare:
        return out
    for rx in (_BARE_SCHEME_RE, _BARE_HOSTPORT_RE):
        for m in rx.finditer(value):
            svc = m.group("svc")
            if context_ns == GOVERNED_SERVICES[svc]:
                continue  # same-ns bare DNS is legitimate
            ctx = context_ns if context_ns is not None else "unknown"
            out.append((m.group(0),
                        f"bare host `{svc}` outside its canonical namespace "
                        f"(context ns={ctx}; a bare service name only "
                        f"resolves same-ns) (expected {_expected(svc)})"))
    return out


def iter_string_scalars(node, keypath: str = "") -> Iterator[Tuple[str, str]]:
    """Yield (keypath, value) for every string scalar in a parsed YAML doc.

    Walks VALUES only — mapping keys (`tenant-api:` as a YAML key) are names,
    not hosts, and must not be scanned (word-boundary care, module docstring)."""
    if isinstance(node, dict):
        for k, v in node.items():
            kp = f"{keypath}.{k}" if keypath else str(k)
            yield from iter_string_scalars(v, kp)
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from iter_string_scalars(v, f"{keypath}[{i}]")
    elif isinstance(node, str):
        yield (keypath, node)


def _find_line(text: str, fragment: str) -> Optional[int]:
    """Best-effort 1-based line of a matched fragment in the raw file text."""
    for i, line in enumerate(text.splitlines(), 1):
        if fragment in line:
            return i
    return None


def _scan_yaml_docs(path: Path, rel: str, context_for_doc,
                    apply_bare: bool) -> List[Finding]:
    """Scan every YAML doc's string scalars. `context_for_doc(doc)` supplies
    the R2 namespace context per document (fixed for values files, per-doc
    metadata.namespace for raw k8s manifests)."""
    text = path.read_text(encoding="utf-8")
    findings: List[Finding] = []
    for doc in yaml.safe_load_all(text):
        if doc is None:
            continue
        ctx = context_for_doc(doc)
        for keypath, scalar in iter_string_scalars(doc):
            for frag, msg in scan_scalar(scalar, ctx, apply_bare):
                findings.append(
                    (rel, _find_line(text, frag), keypath, frag, scalar, msg))
    return findings


def scan_helm_values(path: Path, rel: str, chart: str) -> List[Finding]:
    ctx = CHART_CONTEXT_NS.get(chart)  # unmapped chart -> None (conservative)
    return _scan_yaml_docs(path, rel, lambda _doc: ctx, apply_bare=True)


def scan_k8s_manifest(path: Path, rel: str) -> List[Finding]:
    def ctx(doc):
        meta = doc.get("metadata") if isinstance(doc, dict) else None
        ns = meta.get("namespace") if isinstance(meta, dict) else None
        return ns if isinstance(ns, str) else None
    return _scan_yaml_docs(path, rel, ctx, apply_bare=True)


def scan_compose(path: Path, rel: str) -> List[Finding]:
    # R1 only: inside compose's single network every bare service name
    # resolves, so R2 has nothing to say there (module docstring, target 4).
    return _scan_yaml_docs(path, rel, lambda _doc: None, apply_bare=False)


def scan_nginx_conf(path: Path, rel: str,
                    context_ns: str = "monitoring") -> List[Finding]:
    """Plain-text line scan (nginx.conf is not YAML); comment lines skipped
    so prose like `# … to tenant-api …` can't false-positive."""
    findings: List[Finding] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for frag, msg in scan_scalar(line, context_ns, apply_bare=True):
            findings.append((rel, i, None, frag, line, msg))
    return findings


def match_exemption(rel: str, scalar: str) -> Optional[Tuple[str, str, str]]:
    """(substring, rationale, exit-condition) if the hit is registered."""
    for (path, sub), (rationale, exit_cond) in EXEMPTIONS.items():
        if rel == path and sub in scalar:
            return (sub, rationale, exit_cond)
    return None


def check_repo(repo: Path) -> Tuple[List[str], List[str], int]:
    """Run all scans. Returns (violation_lines, exempt_info_lines, n_files).
    Raises yaml.YAMLError on an unparseable target (caller-error in main)."""
    findings: List[Finding] = []
    n_files = 0

    # 1. helm/*/values*.yaml (+ .yml for hook-scope parity, cf. L3)
    for pattern in ("helm/*/values*.yaml", "helm/*/values*.yml"):
        for p in sorted(repo.glob(pattern)):
            rel = p.relative_to(repo).as_posix()
            if any(part in SKIP_DIR_PARTS for part in Path(rel).parts):
                continue
            n_files += 1
            findings.extend(scan_helm_values(p, rel, chart=p.parent.name))

    # 2. k8s/**/*.yaml — raw manifests, per-doc metadata.namespace context
    for pattern in ("k8s/**/*.yaml", "k8s/**/*.yml"):
        for p in sorted(repo.glob(pattern)):
            if not p.is_file():
                continue
            rel = p.relative_to(repo).as_posix()
            if any(part in SKIP_DIR_PARTS for part in Path(rel).parts):
                continue
            n_files += 1
            findings.extend(scan_k8s_manifest(p, rel))

    # 3 + 4. fixed targets (existence pre-checked in main)
    nginx = repo / _NGINX_CONF
    if nginx.exists():
        n_files += 1
        findings.extend(scan_nginx_conf(nginx, _NGINX_CONF))
    compose = repo / _COMPOSE
    if compose.exists():
        n_files += 1
        findings.extend(scan_compose(compose, _COMPOSE))

    violations: List[str] = []
    infos: List[str] = []
    for rel, line, keypath, frag, scalar, msg in findings:
        loc = f"{rel}:{line}" if line is not None else f"{rel} ({keypath})"
        what = f"{keypath}: {msg}" if keypath else msg
        ex = match_exemption(rel, scalar)
        if ex:
            sub, rationale, exit_cond = ex
            infos.append(f"{loc} — exempt `{frag}` — {rationale} "
                         f"(exit condition: {exit_cond})")
        else:
            violations.append(f"{loc} — {what}")
    return violations, infos, n_files


def _apply_config(path: str) -> None:
    """Load *path* and rebind the module-level constants + regexes so the pure
    functions (scan_scalar / _expected / match_exemption) pick up this data.
    Raises ConfigError (fail-closed) on any problem."""
    global GOVERNED_SERVICES, CHART_CONTEXT_NS, EXEMPTIONS
    global _SVC_ALT, _FQDN_RE, _BARE_SCHEME_RE, _BARE_HOSTPORT_RE
    GOVERNED_SERVICES, CHART_CONTEXT_NS, EXEMPTIONS = load_config(path)
    _SVC_ALT, _FQDN_RE, _BARE_SCHEME_RE, _BARE_HOSTPORT_RE = _build_matchers(
        GOVERNED_SERVICES
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ci", action="store_true",
                        help="exit 1 on violation")
    parser.add_argument(
        "--config", default=None,
        help="path to the config DATA file (default: co-located "
             "cross_ns_url_lint.config.yaml); handy for testing fail-closed "
             "behaviour")
    args = parser.parse_args()

    # Fail closed on config: a --config override reloads deliberately; with no
    # override, surface any import-time load error (missing/malformed default
    # config) as caller-error rather than scanning on empty data → silent pass.
    try:
        if args.config is not None:
            _apply_config(args.config)
        elif _CONFIG_LOAD_ERROR is not None:
            raise _CONFIG_LOAD_ERROR
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    repo = Path(_THIS_DIR).resolve()
    for parent in [repo, *repo.parents]:
        if (parent / ".git").exists():
            repo = parent
            break

    missing = [t for t in _FIXED_TARGETS if not (repo / t).exists()]
    if missing:
        print(f"ERROR: fixed scan target(s) not found: {missing} "
              f"(the lint's subject moved — update _FIXED_TARGETS)",
              file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        violations, infos, n_files = check_repo(repo)
    except yaml.YAMLError as exc:
        print(f"ERROR: YAML parse failure: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    for line in violations:
        print(f"  [VIOLATION] {line}")
    for line in infos:
        print(f"  [INFO] {line}")

    if not violations:
        print(
            f"OK cross-namespace service-URL consistency (#1004) — "
            f"0 violations / {len(infos)} exempt-INFO across {n_files} "
            f"file(s).\n"
            f"   服務 URL namespace 一致：tenant-api → 專屬 `tenant-api` ns、"
            f"recipe-preview → `monitoring` ns；FQDN 與 bare-name 皆符合 "
            f"canonical（豁免登記於 EXEMPTIONS，含退場條件）。"
        )
        return EXIT_OK

    print(
        f"\nFAIL cross-namespace service-URL consistency (#1004) — "
        f"{len(violations)} violation(s) / {len(infos)} exempt-INFO.\n"
        f"   修法：tenant-api 一律 `tenant-api.tenant-api.svc.cluster.local`、"
        f"recipe-preview 一律 `recipe-preview.monitoring.svc.cluster.local`；\n"
        f"   跨 namespace 呼叫禁用 bare service name"
        f"（`http://tenant-api:8080` 只在 canonical ns 內可解析）。\n"
        f"   合法例外請登記 check_cross_ns_url_consistency.py 的 EXEMPTIONS"
        f"（path + substring + rationale + 退場條件），"
        f"並同步 docs/internal/iac-lint-baseline.md。"
    )
    return EXIT_VIOLATION if args.ci else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
