#!/usr/bin/env python3
"""validate_config.py — One-stop configuration validation.

Runs all validation checks in sequence and produces a unified report.
Designed for CI pipelines: exit 0 = all pass, exit 1 = a finding about the
config (including a file in config-dir this tool could not read), exit 2 =
this run could not be completed for a reason that is not the config's fault
(a prerequisite tool missing or timed out).

Checks:
  1. YAML syntax  — Every file in config-dir decodes as UTF-8, parses, and
                    has a mapping at its top level (#1448: the other two are
                    as fatal to every consumer as a syntax error, and this is
                    the only check that opens each file by name, so it is the
                    only one that can name one. ⚠️ It enumerates via
                    `iter_config_files`, which yields FILES — a *directory*
                    named `x.yaml` is invisible to it while the routing
                    reader does see it, so that one shape is a stderr WARN
                    and a PASS row here. Tracked separately.)
  2. Schema        — Tenant keys validated against known defaults + reserved keys
  3. Routes        — Alertmanager route generation with --validate semantics
  4. Policy        — Webhook domain allowlist (if --policy provided)
  5. Custom rules  — Deny-list linting on rule-packs/ (if --rule-packs provided)
  6. Profiles      — Tenant _profile references validation
  7. Versions      — bump_docs.py --check version consistency (if --version-check)
  8. Policy-as-Code — Declarative DSL policy evaluation (if _policies in _defaults.yaml or --policy-dsl)
  9. Tenant uniqueness — one tenant id declared by two files makes the exporter
                    reject the ENTIRE config dir (`DuplicateTenantError`), so
                    this one FAIL is about every tenant in the tree (#1577)

Usage:
  # Minimal (YAML + schema + routes):
  python3 scripts/tools/validate_config.py \\
    --config-dir components/threshold-exporter/config/conf.d/

  # Full suite (CI):
  python3 scripts/tools/validate_config.py \\
    --config-dir components/threshold-exporter/config/conf.d/ \\
    --policy .github/custom-rule-policy.yaml \\
    --rule-packs rule-packs/ \\
    --version-check

  # JSON output for CI consumption:
  python3 scripts/tools/validate_config.py \\
    --config-dir components/threshold-exporter/config/conf.d/ \\
    --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

# Add script dir to path for lib imports
_THIS_DIR = Path(__file__).resolve().parent

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
sys.path.insert(0, str(_THIS_DIR))  # Docker flat layout
sys.path.insert(0, str(_THIS_DIR.parent))  # Repo subdir layout
from _lib_python import detect_cli_lang  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_confd import (  # noqa: E402
    is_reserved_name,
    iter_config_files,
    resolve_defaults_file,
    unusable_config_paths,
    unusable_reason,
)

# Language detection for bilingual help
_LANG = detect_cli_lang()

# Bilingual help strings
_HELP = {
    'description': {
        'zh': '一站式配置驗證',
        'en': 'One-stop configuration validation'
    },
    'config_dir': {
        'zh': '租戶配置目錄路徑 (conf.d/)',
        'en': 'Path to tenant config directory (conf.d/)'
    },
    'policy': {
        'zh': '策略 YAML 路徑 (allowed_domains 等)',
        'en': 'Path to policy YAML (allowed_domains etc.)'
    },
    'rule_packs': {
        'zh': '用於自訂規則 lint 的 rule-packs/ 目錄路徑',
        'en': 'Path to rule-packs/ directory for custom rule lint'
    },
    'version_check': {
        'zh': '同時執行版本一致性檢查',
        'en': 'Also run version consistency check'
    },
    'json': {
        'zh': '將結果輸出為 JSON (供 CI 使用)',
        'en': 'Output results as JSON (for CI consumption)'
    },
    'policy_dsl': {
        'zh': '獨立 Policy-as-Code DSL 檔案路徑（頂層 policies: key）',
        'en': 'Path to standalone Policy-as-Code DSL file (top-level policies: key)'
    },
    'strict': {
        'zh': 'domain policy（ADR-007）違規由 WARN 升級為 FAIL（與 CI 的 generate-routes --strict 對齊）',
        'en': 'Escalate domain-policy (ADR-007) violations from WARN to FAIL (matches CI generate-routes --strict)'
    }
}

def _h(key):
    """Get help text in detected language."""
    return _HELP[key].get(_LANG, _HELP[key]['en'])
import subprocess
import sys

import yaml

from _lib_python import (  # noqa: E402
    load_yaml_file, VALID_RESERVED_KEYS, VALID_RESERVED_PREFIXES,
    DOCS_SITE_BASE,
)

# ============================================================
# Check results
# ============================================================
PASS = "pass"
WARN = "warn"
FAIL = "fail"


# ============================================================
# Customer-facing strings that tests assert on
# ============================================================
# ⛔ Named, not inline. Blind review measured what inline costs: rewording
# the caveat sentence turned 5 tests red, and the two NEGATIVE controls
# ("this row must NOT carry the caveat") stayed green while asserting
# nothing at all, because `"<old wording>" not in output` is satisfied by
# any reword. A reword is now one edit here, and the negative controls keep
# their teeth because they reference the same constant.
UNUSABLE_CAVEAT = ("\u26a0\ufe0f {n} file(s) could not be read and were skipped "
                   "({listed}); nothing below covers them.")
READ_ERRORS_FIRST = ("-> Fix the read errors above FIRST — this check's "
                     "findings may be artefacts of the files it could not "
                     "read.")
POLICY_ONLY_SCHEMA_HINT = (
    "The findings above are ADR-007 domain-policy errors, not tenant key "
    "problems — no unknown key was reported in this run. \u26d4 Ignore the "
    "generic advice about removing keys: fix the domain policy file the "
    "error names, then re-run.")
NO_DECLARED_DEFAULTS_HINT = (
    "This config declares no platform defaults at all, so every tenant key "
    "is reported as unknown — that is the platform's side missing, not a "
    "typo in the tenant files. \u26d4 Do NOT remove the keys named above: "
    "they are in force. Add a `defaults:` block — create "
    "_defaults.yaml if the tree has none, or restore the block that "
    "was emptied.")

# #1556: the three hints below are shown when --policy / --rule-packs /
# --policy-dsl was supplied but is unusable. Without them the row inherits
# the generic per-check advice, which points at tenant YAML — the wrong place
# for a mistake that lives entirely in the operator's argv. Measured for
# --rule-packs: the row read "Validate rule pack YAML syntax … run
# rule-pack-split --check", about a tree the tool had never opened.
_RULE_PACKS_INPUT_HINT = (
    "Point --rule-packs at an existing rule-packs/ directory. ⛔ Dropping "
    "the flag does not make this row pass — the row DISAPPEARS (it is only "
    "appended when --rule-packs is given) and the exit code goes back to 0 "
    "with the custom rule lint no longer part of the run.")

# ⛔ The third sibling. Two rounds running, this PR added a hint for one row
# and left the identically-shaped one next to it on the generic advice; this
# constant exists so the third does not repeat it.
_POLICY_DSL_INPUT_HINT = (
    "Point --policy-dsl at a policy DSL file (top-level `policies:` key). "
    "⛔ Dropping the flag does not restore anything — it means the run "
    "evaluates only the `_policies` block in _defaults.yaml, which is what "
    "you already had before you passed the flag.")

# ⛔ TWO constants, because `--policy` fails two rows whose "what happens if I
# just drop the flag" answers are OPPOSITE, and one shared sentence was wrong
# on one of them. Measured: `validate-config --config-dir <ok>` reports five
# rows (yaml_syntax / schema / routes / profiles / policy_dsl); adding a valid
# `--policy` makes it six. So `routes` survives the flag being dropped and does
# go back to PASS, while `policy` DISAPPEARS — the sibling `--rule-packs` hint
# had already been corrected to say exactly that, three lines up, and this one
# was left behind saying "makes this row PASS again" for both.
_POLICY_ROUTES_ROW_HINT = (
    "Point --policy at a policy YAML holding an `allowed_domains:` list "
    "(see docs/cli-reference.md § validate-config). ⛔ Dropping the "
    "flag makes this row PASS again by switching the webhook domain "
    "allowlist off — that is the failure this check exists to stop.")

_POLICY_ROW_HINT = (
    "Point --policy at a policy YAML holding an `allowed_domains:` list "
    "(see docs/cli-reference.md § validate-config). ⛔ Dropping the flag "
    "does not make this row pass — the row DISAPPEARS (it is only appended "
    "when --policy is given) and the webhook domain allowlist is not checked "
    "at all, which is the failure this check exists to stop.")


def _argv_error_row(name: str, detail: str, hint: str) -> dict[str, object]:
    """A FAIL caused by the operator's argv, not by anything under --config-dir.

    ⛔ `reads_config_dir=False` is the load-bearing part. The exit-code rule
    downgrades a caller error to exit 1 when the config tree had an unreadable
    file AND the failing check reads that tree — which is right for a check
    that could not do its job because of the tree, and wrong for one that never
    got that far because a path on the command line was unusable. Measured
    before this existed: an unreadable tenant file silently turned
    `--policy <bad>` from exit 2 into exit 1.
    """
    row = _make_result(name, FAIL, [detail], caller_error=True, hint=hint)
    row["reads_config_dir"] = False
    return row


def _no_declared_defaults_hint(config_dir: str) -> str | None:
    """Replacement hint for when the platform declares no defaults at all.

    ⛔ ``check_schema`` reports every tenant key as ``unknown key … not in
    defaults`` when nothing is declared, and the generic hint for that row
    says **"Remove unknown keys"** — pointing at overrides that are legal
    and in force. Following it deletes a working config, and the run exits
    0 while saying so. Measured on ``origin/main`` with a ``_defaults.yaml``
    that has no ``defaults:`` key, and again with ``defaults: {}``.

    ⚠️ NOT with ``defaults:`` written as a bare key (null). That spelling
    reached ``check_profiles`` first and died there with an
    ``AttributeError`` — the crash this branch fixes elsewhere — so on
    ``origin/main`` it produced no report at all rather than bad advice. The
    three spellings are one class *here* and three different outcomes
    *there*; an earlier version of this sentence collapsed them.

    Only the *advice* changes here. The warnings themselves come from
    ``validate_tenant_keys``, whose verdicts are pinned key-for-key against
    the Go twin (``tests/shared/optional_overrides_membership_matrix.json``);
    silencing them in this report would put the two out of step.

    Returns ``None`` when defaults *are* declared, so the generic hint —
    which is right for an actual typo — is used unchanged.
    """
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import generate_alertmanager_routes as gen

    # ⛔ Fail soft. This reads a private helper through a re-export and two
    # dict keys that belong to another module; renaming any of them is an
    # ordinary refactor over there, and an exception raised here would be
    # caught by `_run_check`'s catch-all and turn a WARN row into "FAIL,
    # exit 2, please report this with the traceback" — for a config that is
    # fine. Losing the replacement hint degrades to today's generic advice,
    # which is the outcome this whole helper is an improvement on; losing
    # the run is not.
    try:
        parsed = gen._parse_config_files(config_dir)
        declared = (parsed.get("defaults_keys", set())
                    | parsed.get("optional_override_keys", set()))
    except (Exception, SystemExit):  # noqa: BLE001 — see above
        # ⛔ `SystemExit` is NOT an `Exception`. `_parse_config_files` calls
        # `sys.exit(EXIT_CALLER_ERROR)` when the directory is gone, which a
        # bare `except Exception` lets straight through — and `_run_check`
        # cannot catch it either, so the run dies with no report at all:
        # #1448 exactly, reintroduced by the guard written to prevent it.
        # Reachable by TOCTOU — a GitOps ConfigMap projected volume swaps
        # `..data` atomically between `main()`'s isdir() check and this call.
        return None
    if declared:
        return None
    return NO_DECLARED_DEFAULTS_HINT


def _make_result(
    name: str, status: str, details: list[str] | None = None,
    caller_error: bool = False, unusable_files: list[str] | None = None,
    hint: str | None = None,
) -> dict[str, object]:
    """Create a check result dict.

    #452/#737: ``caller_error`` marks a FAIL that stems from a caller-error
    class condition (a prerequisite tool missing/timed out, IO failure) rather
    than a genuine validation finding, so the CLI can exit 2 instead of 1.

    #1448: ``unusable_files`` carries the files ``yaml_syntax`` could not
    read, as *data*. The report needs that list to stop asserting health for
    files nobody could open, and the first draft recovered it by splitting
    the report lines it had just printed back apart on ``:`` — so the moment
    a detail line was prose rather than ``<file>: <error>`` the report
    invented filenames out of its own sentences. Structure computed upstream
    does not get re-guessed from a string downstream.

    #1448: ``hint`` replaces the generic ``_CHECK_HINTS`` line for this run
    when the check knows the generic advice would be wrong — see
    ``_no_declared_defaults_hint``.
    """
    return {"check": name, "status": status, "details": details or [],
            "caller_error": caller_error,
            "unusable_files": unusable_files or [],
            "hint": hint}


# ============================================================
# Check 1: YAML Syntax
# ============================================================
def _unusable_consequence(bad: Path) -> str:
    """What the operator actually loses because of `bad` — one clause.

    ⛔ This used to be the single fixed sentence "nothing in it is loaded,
    by this tool or by threshold-exporter", and for one shape that sentence
    was measurably FALSE. A DIRECTORY named `beta.yaml` that CONTAINS
    `.yaml` files does not stop anything from loading them: this very scan
    reads `beta.yaml/inner.yaml` through `iter_config_files`, and the Go
    side descends too — `hierarchy.go`'s `WalkDir` only `SkipDir`s names
    beginning with `.`. So the run printed a blocking `FAIL` whose stated
    reason had just been contradicted by the same run's own file list.

    Saying a true thing matters more here than usual: this check is a
    required gate, and the sentence is what a customer reads when it turns
    their build red.
    """
    reason = unusable_reason(bad)
    if reason.startswith("is a directory that could not be read"):
        return ("so THIS REPORT IS INCOMPLETE: fix the permissions and "
                "re-run before trusting it")
    inside = list(iter_config_files(bad)) if bad.is_dir() else []
    if inside:
        return (f"but the {len(inside)} config file(s) INSIDE it ARE "
                f"loaded — by this scan and by threshold-exporter's "
                f"recursive scanner alike, so those declarations are live. "
                f"A `.yaml` name on a DIRECTORY is almost always an "
                f"interrupted `mkdir` or a bad merge: rename it")
    return "nothing in it is loaded, by this tool or by threshold-exporter"


def check_yaml_syntax(config_dir: str) -> dict[str, object]:
    """Validate that every YAML file in config_dir loads into a usable shape.

    Recursive since #1339: ADR-016 allows a hierarchical conf.d/ and the
    exporter walks it, so a flat scan here reported `PASS` while never
    reading the tenants it was asked about.

    #1448 widened "loads" twice, because the check that reads every customer
    file first is the one place that can name the file:

    * **Not UTF-8.** ``open(..., encoding="utf-8")`` raises
      ``UnicodeDecodeError``, which is not a ``YAMLError``, so it escaped
      this loop entirely. An editor that saved one tenant file in the local
      code page therefore killed the whole run, and the exception text does
      **not** contain the path — the customer got a traceback naming a codec
      and no filename.
    * **Parses, but is not a mapping.** A bare YAML list or a lone scalar is
      dropped by every consumer (they all reach for ``data.get(...)``). That
      used to surface as a crash, which was at least loud; teaching the
      readers to skip it instead would have traded the crash for an all-green
      report on a conf.d whose tenants never loaded, so it is reported here,
      in the check whose question is "can this file be used at all".
    """
    errors = []
    unusable = []
    file_count = 0
    for fpath_p in iter_config_files(config_dir):
        fpath = str(fpath_p)
        file_count += 1
        # Relative path, not bare filename: now that the scan is recursive,
        # two levels can hold the same `_defaults.yaml`.
        try:
            label = fpath_p.relative_to(Path(config_dir)).as_posix()
        except ValueError:
            label = fpath_p.name
        try:
            with open(fpath, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
        except yaml.YAMLError as e:
            errors.append(f"{label}: {e}")
            unusable.append(label)
            continue
        except UnicodeDecodeError as e:
            errors.append(
                f"{label}: not valid UTF-8 ({e.reason} at byte {e.start}) — "
                f"re-save this file as UTF-8")
            unusable.append(label)
            continue
        except Exception as e:  # noqa: BLE001 — see below
            # ⛔ Deliberately open-ended, and this loop is the ONLY place in
            # the tool where that is the right shape: it is reading one
            # named file, so whatever comes back it can say *which* file.
            # Listing the exception types instead is a bet that keeps
            # losing — `yaml.safe_load` raises `RecursionError` (not a
            # YAMLError) on deeply nested input — measured at roughly half the recursion
            # limit — 495 flow-style, 493 block-style on this host —
            # and it tracks `sys.getrecursionlimit()` rather than being
            # a constant —
            # and before this it escaped to the catch-all in `_run_check`,
            # which exits 2 and prints "please report this" for a file the
            # customer wrote and this message could not name.
            errors.append(f"{label}: could not be read — "
                          f"{e.__class__.__name__}: {' '.join(str(e).split())}")
            unusable.append(label)
            continue
        if loaded is not None and not isinstance(loaded, dict):
            errors.append(
                # Not "so its tenants never load": this same message is
                # emitted for `_domain_policy.yaml` and
                # `_routing_profiles.yaml`, which hold no tenants at all.
                f"{label}: top level must be a mapping, got "
                f"{type(loaded).__name__} — every consumer reaches for keys "
                f"on it and finds none, so nothing in this file is loaded")
            unusable.append(label)

    # #1469: entries NAMED like a config file that `iter_config_files`
    # correctly refuses to hand over — a directory called `beta.yaml`, a
    # broken symlink, a file with no read permission. This check is the one
    # place that enumerates the whole tree, so it is the only place that can
    # name them; before this they were invisible here while the routing
    # parser reported them, which is the two-readers-two-answers split #1469
    # is about. ⛔ The selection is NOT re-derived locally — that would make
    # a third implementation of "what is a config file".
    for bad in unusable_config_paths(config_dir):
        try:
            label = bad.relative_to(Path(config_dir)).as_posix()
        except ValueError:
            label = bad.name
        if label in (".", ""):
            label = bad.name
        errors.append(f"{label}: {unusable_reason(bad)} — "
                      f"{_unusable_consequence(bad)}")
        unusable.append(label)

    if errors:
        return _make_result("yaml_syntax", FAIL, errors,
                            unusable_files=unusable)
    return _make_result("yaml_syntax", PASS,
                        [f"{file_count} files parsed successfully"])


# ============================================================
# Check 2: Schema validation
# ============================================================
def check_schema(config_dir: str, strict: bool = False) -> dict[str, object]:
    """Validate tenant config keys against known defaults and reserved keys.

    With strict=True, domain-policy (ADR-007) violations are escalated to
    blocking ERROR lines by load_tenant_configs and reported as FAIL here —
    matching `generate_alertmanager_routes.py --validate --strict` (CI).
    """
    # Import generate_alertmanager_routes in-process
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import generate_alertmanager_routes as gen

    _routing, _dedup, schema_warnings, _er, _mc = gen.load_tenant_configs(
        config_dir, strict_policies=strict)

    policy_errors = [w for w in schema_warnings
                     if w.lstrip().startswith(gen.POLICY_ERROR_PREFIX)]
    if not schema_warnings:
        return _make_result("schema", PASS, ["No schema warnings"])
    # ⛔ Computed once, for BOTH exits. The first version wired it only to
    # the WARN branch, so `--strict` plus an emptied `defaults:` plus any
    # domain-policy violation took the FAIL branch and printed "Remove
    # unknown keys" at overrides that are in force — the exact destructive
    # advice this hint exists to replace, on the path where the customer is
    # most likely to be in a hurry.
    hint = _no_declared_defaults_hint(config_dir)
    if policy_errors:
        # ⛔ This row is a multiplexer: its details carry unknown-key WARNs
        # AND ADR-007 policy ERRORs, while the advice line is keyed on the
        # check NAME. So a run whose only finding is a broken policy file
        # was still told "Remove unknown keys or add them to the schema" —
        # the same destructive advice this branch exists to stop, on a path
        # that reports zero keys. Derived by set difference on the list the
        # function already split, not by matching the message text.
        if not [w for w in schema_warnings if w not in policy_errors]:
            hint = POLICY_ONLY_SCHEMA_HINT
        return _make_result("schema", FAIL, schema_warnings, hint=hint)
    return _make_result("schema", WARN, schema_warnings, hint=hint)


# ============================================================
# Check 3: Route validation
# ============================================================
def check_routes(
    config_dir: str, policy_file: str | None = None
) -> dict[str, object]:
    """Validate Alertmanager route generation (--validate semantics)."""
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import generate_alertmanager_routes as gen

    routing, dedup, _sw, enforced_routing, _mc = gen.load_tenant_configs(config_dir)

    # Load allowed_domains from policy (#1556: unusable value is a caller
    # error, not "no policy" — see check_policy)
    allowed_domains = None
    if policy_file:
        try:
            allowed_domains = gen.load_policy(policy_file)
        except gen.PolicyInputError as exc:
            # ⛔ hint= is not decoration here. Without it this row inherits the
            # generic routes advice ("check _routing for invalid receiver
            # types…"), which sends the operator to look at tenant YAML for a
            # problem that is entirely in their own argv.
            return _argv_error_row("routes", str(exc),
                                   _POLICY_ROUTES_ROW_HINT)

    # Capture stderr for warnings
    import io
    old_stderr = sys.stderr
    sys.stderr = captured = io.StringIO()

    try:
        routes, receivers, route_warnings = gen.generate_routes(
            routing, allowed_domains=allowed_domains,
            enforced_routing=enforced_routing)
        inhibit_rules, dedup_warnings = gen.generate_inhibit_rules(dedup)
    finally:
        sys.stderr = old_stderr

    captured_output = captured.getvalue().strip()
    all_issues = list(route_warnings) + list(dedup_warnings)
    if captured_output:
        all_issues.extend(captured_output.split("\n"))

    # Match --validate semantics: errors are WARNs with "skipping"
    errors = [w for w in all_issues if "WARN" in w and "skipping" in w]

    if errors:
        return _make_result("routes", FAIL, all_issues)
    if all_issues:
        return _make_result("routes", WARN, all_issues)
    return _make_result("routes", PASS,
                        [f"{len(routes)} routes, {len(receivers)} receivers, "
                         f"{len(inhibit_rules)} inhibit_rules"])


# ============================================================
# Check 4: Policy (webhook domain allowlist)
# ============================================================
def check_policy(config_dir: str, policy_file: str | None) -> dict[str, object]:
    """Check webhook URLs against domain allowlist.

    ⛔ #1556: the previous single condition collapsed "operator did not ask
    for a policy" and "operator asked but the value is unusable" into one
    ``PASS … skipped`` row at exit 0. The documented example passes a
    comma-separated domain list, which is not a file, so every customer who
    copied it read ``[PASS] policy`` while the allowlist was off. Only the
    first of those two is a legitimate skip.
    """
    if not policy_file:
        return _make_result("policy", PASS, ["No policy file — skipped"])

    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import generate_alertmanager_routes as gen

    try:
        allowed_domains = gen.load_policy(policy_file)
    except gen.PolicyInputError as exc:
        return _argv_error_row("policy", str(exc), _POLICY_ROW_HINT)
    if not allowed_domains:
        return _make_result("policy", PASS,
                            ["No allowed_domains in policy — no restrictions"])

    routing, _dedup, _sw, enforced_routing, _mc = gen.load_tenant_configs(config_dir)

    import io
    old_stderr = sys.stderr
    sys.stderr = captured = io.StringIO()

    try:
        _r, _recv, warnings = gen.generate_routes(
            routing, allowed_domains=allowed_domains,
            enforced_routing=enforced_routing)
    finally:
        sys.stderr = old_stderr

    domain_issues = [w for w in warnings if "domain" in w.lower() or
                     "allowlist" in w.lower() or "blocked" in w.lower() or
                     "not in allowed_domains" in w]

    if domain_issues:
        return _make_result("policy", FAIL, domain_issues)
    return _make_result("policy", PASS,
                        [f"All webhook URLs comply with "
                         f"{len(allowed_domains)} allowed domain(s)"])


# ============================================================
# Check 5: Custom rule linting
# ============================================================
def check_custom_rules(
    rule_packs_dir: str | None, policy_file: str | None = None
) -> dict[str, object]:
    """Run lint_custom_rules.py on rule packs directory.

    ⛔ #1556, same shape as check_policy: ``--rule-packs`` pointing at a path
    that is not a directory used to produce the same ``PASS … skipped`` row as
    omitting it, so a typo or a renamed directory silently removed the custom
    rule lint from the run.
    """
    if not rule_packs_dir:
        return _make_result("custom_rules", PASS,
                            ["No rule-packs dir — skipped"])
    if not os.path.isdir(rule_packs_dir):
        result = _make_result(
            "custom_rules", FAIL,
            [f"--rule-packs: not a directory: {rule_packs_dir}",
             "  ⛔ Do not drop the flag to clear this — that removes the "
             "custom rule lint from the run, which is what this error stops."],
            caller_error=True, hint=_RULE_PACKS_INPUT_HINT)
        result["reads_config_dir"] = False
        return result

    cmd = [sys.executable, str(_THIS_DIR / "lint_custom_rules.py"),
           rule_packs_dir, "--ci"]
    # #1556: pass the value through even when it is not a file. Dropping it
    # here made `validate-config --policy <typo>` lint with the built-in
    # policy while reporting success; lint_custom_rules now exits 2 on it.
    if policy_file:
        cmd.extend(["--policy", policy_file])

    try:
        # ⛔ `encoding=`, not the locale default. The tool on the other end
        # forces UTF-8 on its own stdout (`_lib_compat.try_utf8_stdout`) and
        # prints `✅`/`—`, so on any non-UTF-8 locale `text=True` alone
        # decodes with e.g. cp950, raises inside subprocess's reader thread,
        # and hands back `stdout is None` — which the very next line turns
        # into `TypeError: NoneType + str`, uncaught, from a checker whose
        # job is to report FAIL rather than crash.
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=30)
        output = (result.stdout + result.stderr).strip()
        lines = [l for l in output.split("\n") if l.strip()] if output else []

        if result.returncode != 0:
            return _make_result("custom_rules", FAIL, lines)
        if any("WARN" in l for l in lines):
            return _make_result("custom_rules", WARN, lines)
        return _make_result("custom_rules", PASS,
                            lines or ["No violations found"])
    except subprocess.TimeoutExpired:
        return _make_result("custom_rules", FAIL, ["Lint timed out (30s)"],
                            caller_error=True)
    except FileNotFoundError:
        return _make_result("custom_rules", FAIL,
                            ["lint_custom_rules.py not found"],
                            caller_error=True)


# ============================================================
# Check 6: Profile references (v1.12.0)
# ============================================================
def _is_reserved_key(key: str) -> bool:
    """Check if a key is a reserved tenant config key (starts with _)."""
    if key in VALID_RESERVED_KEYS:
        return True
    for prefix in VALID_RESERVED_PREFIXES:
        if key.startswith(prefix):
            return True
    return False


def check_profiles(config_dir: str) -> dict[str, object]:
    """Validate tenant _profile references and profile structure.

    Checks:
      - Tenant _profile references point to defined profiles
      - Profile keys don't use reserved prefixes (_, _routing, _state_)
      - Profile values are valid types (numeric, string, dict for scheduled)
      - Profiles have at least one metric key
    """
    cfg = Path(config_dir)
    profiles_path = str(cfg / "_profiles.yaml")
    profiles_raw = load_yaml_file(profiles_path, default={})
    profiles = profiles_raw.get("profiles", {}) if isinstance(profiles_raw, dict) else {}

    # Load defaults for cross-referencing
    defaults_path = str(resolve_defaults_file(cfg))
    defaults_raw = load_yaml_file(defaults_path, default={})
    known_defaults = set()
    if isinstance(defaults_raw, dict):
        # `or {}`, not just the get() default: a `defaults:` key that exists
        # with nothing under it (every entry commented out — an ordinary
        # file) parses to None, and the get() default only fires when the
        # key is *absent*. #1448: this raised AttributeError and took the
        # whole report with it, in the very function the ticket named.
        declared = defaults_raw.get("defaults") or {}
        if isinstance(declared, dict):
            known_defaults = set(declared.keys())

    warnings = []
    tenant_count = 0
    profile_ref_count = 0

    # ── Profile structure validation ──
    for p_name, p_data in profiles.items():
        if not isinstance(p_data, dict):
            warnings.append(f"profile={p_name}: value is not a mapping (got {type(p_data).__name__})")
            continue

        if not p_data:
            warnings.append(f"profile={p_name}: empty profile (no metric keys)")
            continue

        for key in p_data:
            # Reserved keys should not be in profiles (they belong in tenant config)
            if key.startswith("_"):
                if _is_reserved_key(key):
                    warnings.append(
                        f"profile={p_name}: contains reserved key \"{key}\" "
                        f"(reserved keys belong in tenant config, not profiles)")
                else:
                    warnings.append(
                        f"profile={p_name}: contains unknown reserved key \"{key}\"")

    # ── Tenant _profile reference validation ──
    # Recursive since #1339 (see check_yaml_syntax). `_`/`.`-prefixed files
    # are level defaults / meta, not tenant files — skipped at every depth.
    for fpath_p in iter_config_files(config_dir):
        fname = fpath_p.name
        if fname.startswith("_") or fname.startswith("."):
            continue
        fpath = str(fpath_p)
        raw = load_yaml_file(fpath, default={})
        if not isinstance(raw, dict):
            continue

        tenants = {}
        if "tenants" in raw and isinstance(raw.get("tenants"), dict):
            tenants = raw["tenants"]
        else:
            tenant = fname.rsplit(".", 1)[0]
            tenants = {tenant: raw}

        for t_name, t_data in tenants.items():
            if not isinstance(t_data, dict):
                continue
            tenant_count += 1
            profile = t_data.get("_profile")
            if not profile or not isinstance(profile, str):
                continue
            profile_ref_count += 1
            profile = profile.strip()
            if profile and profile not in profiles:
                warnings.append(
                    f"tenant={t_name}: _profile references unknown profile "
                    f"\"{profile}\"")

    if warnings:
        return _make_result("profiles", WARN, warnings)
    details = [f"{tenant_count} tenants scanned, {profile_ref_count} profile refs, "
               f"{len(profiles)} profiles defined"]
    return _make_result("profiles", PASS, details)


# ============================================================
# Check 8: Policy-as-Code (DSL evaluation)
# ============================================================
def check_policy_dsl(config_dir: str, policy_dsl_file: str | None = None) -> dict[str, object]:
    """Evaluate declarative policies from _defaults.yaml _policies or standalone file.

    Loads policy rules from _defaults.yaml ``_policies`` section and/or
    a standalone policy DSL file, then evaluates against all tenant configs.
    """
    try:
        import policy_engine as pe
    except ImportError:
        return _make_result("policy_dsl", PASS,
                            ["policy_engine.py not available — skipped"])

    rules = []

    # From _defaults.yaml
    defaults_path = str(resolve_defaults_file(Path(config_dir)))
    if os.path.isfile(defaults_path):
        rules.extend(pe.load_policies(defaults_path))

    # From standalone policy DSL file.
    # ⛔ The same collapse as --policy, written in the mirror form
    # (`x and is_file(x)` rather than `not x or not is_file(x)`), which is why
    # the structural scan did not see it. Measured before this split:
    # `--policy-dsl <missing>` produced output BYTE-IDENTICAL to passing no
    # flag at all, at exit 0 — the operator asked for a policy file and was
    # told "No _policies defined — skipped".
    if policy_dsl_file:
        if not os.path.isfile(policy_dsl_file):
            return _argv_error_row(
                "policy_dsl",
                f"--policy-dsl: not a file: {policy_dsl_file}",
                _POLICY_DSL_INPUT_HINT)
        # ⛔ "not a file" is ONE of the five shapes of "supplied but unusable",
        # and the first cut of this split closed only that one — the same 1-of-5
        # that external review had already caught on `--policy` one round
        # earlier, rebuilt here by hand. Blind review measured the other four
        # falling through to `_run_check`, which attributes them to the config
        # tree: `--policy-dsl <non-UTF-8>` exited 1 saying "could not read the
        # config … If no file is named above, the fault is in one of the paths
        # you passed" — while naming the file, three lines up.
        #
        # The read below is the operator's argv. The `_defaults.yaml` read
        # above is the customer's tree and stays a tree finding on purpose.
        try:
            dsl_data = pe.load_yaml_file(policy_dsl_file)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            return _argv_error_row(
                "policy_dsl",
                f"--policy-dsl: unusable file: {policy_dsl_file}: {exc}",
                _POLICY_DSL_INPUT_HINT)
        if dsl_data is not None and not isinstance(dsl_data, dict):
            return _argv_error_row(
                "policy_dsl",
                f"--policy-dsl: top level is {type(dsl_data).__name__}, "
                f"expected a mapping with a `policies:` key: {policy_dsl_file}",
                _POLICY_DSL_INPUT_HINT)
        # ⚠️ An empty file, or a mapping with neither `policies:` nor
        # `_policies:`, still reaches "No _policies defined — skipped" at
        # exit 0. That is the CONTENT axis, which this PR does not close for
        # any of the three flags; see the NOT COVERED list in _grar_validate.
        rules.extend(pe.load_policies(policy_dsl_file))

    if not rules:
        return _make_result("policy_dsl", PASS,
                            ["No _policies defined — skipped"])

    tenant_configs = pe.load_tenant_configs(config_dir)
    if not tenant_configs:
        return _make_result("policy_dsl", PASS,
                            ["No tenant configs found — skipped"])

    result = pe.evaluate_policies(rules, tenant_configs)

    details = []
    for v in result.violations:
        icon = "ERROR" if v.severity == "error" else "WARN"
        details.append(f"[{icon}] {v.tenant}: {v.rule_name} — {v.message}")

    if result.error_count > 0:
        details.append(f"{result.error_count} error(s), {result.warning_count} warning(s) "
                       f"across {result.tenants_evaluated} tenants")
        return _make_result("policy_dsl", FAIL, details)
    if result.warning_count > 0:
        details.append(f"{result.warning_count} warning(s) "
                       f"across {result.tenants_evaluated} tenants")
        return _make_result("policy_dsl", WARN, details)
    return _make_result("policy_dsl", PASS,
                        [f"{len(rules)} rules evaluated across "
                         f"{result.tenants_evaluated} tenants — all passed"])


# ============================================================
# Check 7: Version consistency
# ============================================================
def check_versions() -> dict[str, object]:
    """Run bump_docs.py --check for version consistency."""
    # ⛔ `dx/`, not `_THIS_DIR`. bump_docs.py has never lived beside this
    # file, so this check has never once run: `make validate-config` was
    # red for every developer with "can't open file .../ops/bump_docs.py".
    # ⛔ Resolve BEFORE shelling out, and say so plainly when it is absent.
    # `subprocess.run([sys.executable, "<missing>.py"])` does NOT raise
    # FileNotFoundError — the INTERPRETER exists, so it starts, prints
    # "can't open file ..." and exits 2. That is why the `except
    # FileNotFoundError` branch below was unreachable, and why this check
    # spent years reporting a bare interpreter error as `[FAIL] versions`
    # (#1461).
    # ⚠️ In the published da-tools image this file lives at
    # `/opt/da-tools/validate_config.py` and `build.sh` FLATTENS the tool set,
    # so neither `<parent>/dx/bump_docs.py` nor a sibling copy exists —
    # bump_docs.py is not in the shipped tool list at all. Version/count
    # consistency is a repo-maintainer gate (`make version-check`,
    # `make pre-tag`), not something a customer runs from the image. So the
    # honest answer there is "not applicable here", not a red check.
    _bump_docs = _THIS_DIR.parent / "dx" / "bump_docs.py"
    if not _bump_docs.is_file():
        return _make_result(
            "versions", WARN,
            ["Skipped: bump_docs.py is not present in this installation "
             f"(looked for {_bump_docs}).",
             "This check is a repository-maintainer gate; the published "
             "da-tools image ships a flattened tool set that excludes it.",
             "In a repo checkout run: python3 scripts/tools/dx/bump_docs.py "
             "--check && python3 scripts/tools/dx/bump_docs.py "
             "--sync-counts --check"])

    cmd = [sys.executable, str(_bump_docs), "--check"]

    try:
        # ⛔ Same reason as `check_custom_rules`: bump_docs.py forces UTF-8 on
        # its own stdout and prints `✅`, so decoding it with the locale codec
        # crashes this checker outright on a non-UTF-8 developer machine.
        # Before the `dx/` path fix this never surfaced — the wrong path made
        # the child print pure-ASCII "can't open file", which any codec
        # decodes. Fixing the path is what made the latent half reachable.
        # ⛔ Not 15s. `bump_docs --check` now reads ~400 tracked files (#1407
        # repointed the JSX front-matter glob at `tools/portal/**`), and this
        # host measures min 3.9 / median 11.2 / max 21.8 seconds across six
        # runs — i.e. the old bound sat INSIDE the normal distribution and
        # turned an ordinary slow run into `[FAIL] versions: Version check
        # timed out`, which the caller cannot distinguish from real drift. A
        # timeout is a hang detector, not a performance budget; it belongs far
        # above the working range. Re-measure if the tracked-file set grows
        # again.
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=120)
        output = (result.stdout + result.stderr).strip()
        lines = [l for l in output.split("\n") if l.strip()] if output else []

        if result.returncode != 0:
            return _make_result("versions", FAIL, lines)
        return _make_result("versions", PASS,
                            lines or ["Version numbers consistent"])
    except subprocess.TimeoutExpired:
        return _make_result("versions", FAIL, ["Version check timed out"],
                            caller_error=True)
    except FileNotFoundError:
        return _make_result("versions", FAIL, ["bump_docs.py not found"],
                            caller_error=True)


# ============================================================
# Check 9: Tenant declaration uniqueness (#1577)
# ============================================================
def check_tenant_uniqueness(config_dir: str) -> dict[str, object]:
    """One tenant declared by two files makes the exporter refuse the WHOLE tree.

    The oracle is the exporter's own hierarchy walker: a tenant id found in
    two different files raises a typed ``*DuplicateTenantError``
    (``config_hierarchy.go``), which ``Load`` / ``fullDirLoad`` turn into
    ``config rejected (mixed-mode duplicate tenant)``. That rejects the
    **entire** configuration, not the one tenant — so the blast radius is
    every tenant in the tree, and it lands at deploy or restart time, after
    CI has already passed.

    Until this check existed nothing on the CI side could see the state.
    Measured on a tree holding ``same.yaml`` and ``same.yml`` for one
    tenant: this command printed ``Result: PASS``, exited 0, ran 5/5 checks
    and named neither file.

    ⛔ THE PREDICATE IS "ONE ID, TWO FILES", NOT "TWO SPELLINGS OF ONE STEM".
    Two spellings is only the cheapest accident — an editor leaving a
    ``.yml`` beside the ``.yaml``. ``db-a.yaml`` and ``archive/db-a.yaml``
    reach the identical rejection with no extension involved, and keying on
    the extension would pass a tree the exporter refuses while looking
    thorough. What the exporter rejects is the duplicate declaration; this
    check has to ask that same question.

    Selection mirrors the walker instead of restating it.
    ``iter_config_files`` is the shared recursive, case-insensitive,
    both-spellings enumerator, and ``is_reserved_name`` is the ``_``-prefix
    gate the walker applies *before* it parses a ``tenants:`` block at all.
    The second one matters in both directions: a ``tenants:`` block inside
    ``_defaults.yaml`` is not a declaration for this purpose, so pairing one
    with a real tenant file has to stay green.

    ⚠️ Files that will not parse are reported as a limit on this check's
    coverage, not folded into a PASS. ``yaml_syntax`` already FAILs and names
    them; the statement added here is the different one — *the uniqueness
    answer is incomplete* — because an unparseable file is exactly where the
    second declaration can hide.

    #1577 records the sibling asymmetry inside the exporter (its incremental
    path accepts this state silently while a full load rejects it). This
    check does not change that and cannot: it runs before the tree is
    deployed, and its job is to stop the state being committed at all.
    """
    root = Path(config_dir)
    declared: dict[str, set[str]] = {}
    unreadable: list[str] = []

    for path in iter_config_files(root):
        if is_reserved_name(path.name):
            continue
        try:
            label = path.relative_to(root).as_posix()
        except ValueError:
            label = path.name
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except Exception:  # noqa: BLE001 — `yaml_syntax` owns naming the reason
            unreadable.append(label)
            continue
        if not isinstance(data, dict):
            continue
        tenants = data.get("tenants")
        if not isinstance(tenants, dict):
            continue
        for tenant_id in tenants:
            declared.setdefault(str(tenant_id), set()).add(label)

    duplicates = {tid: sorted(paths)
                  for tid, paths in declared.items() if len(paths) > 1}

    if duplicates:
        details = []
        for tenant_id, paths in sorted(duplicates.items()):
            details.append(
                f"tenant \"{tenant_id}\" is declared in {len(paths)} files: "
                f"{', '.join(paths)} — the exporter rejects the ENTIRE config "
                f"dir in this state, so every tenant here loses alerting, not "
                f"just this one. Which file owns this tenant is a decision "
                f"this tool cannot make for you: removing the wrong one drops "
                f"that file's overrides silently.")
        if unreadable:
            details.append(
                f"{len(unreadable)} file(s) could not be read and were not "
                f"examined, so there may be more: {', '.join(sorted(unreadable))}")
        return _make_result("tenant_uniqueness", FAIL, details)

    if unreadable:
        return _make_result("tenant_uniqueness", WARN, [
            f"no duplicate declaration among the {len(declared)} tenant(s) in "
            f"the files that could be read, but {len(unreadable)} file(s) "
            f"could not be read and were not examined: "
            f"{', '.join(sorted(unreadable))} — this is a limit on what was "
            f"checked, not a clean result"])

    return _make_result("tenant_uniqueness", PASS, [
        f"{len(declared)} tenant(s), each declared in exactly one file"])


# ============================================================
# Report
# ============================================================
def _docs_url(rel_path: str) -> str:
    """Repo-relative ``docs/**.md`` path → the URL a customer can open.

    #1447: these hints used to print the repo-relative path verbatim
    (``-> See: docs/scenarios/alert-routing-split.md``). The audience for
    this report is a customer whose repository holds ``conf.d/`` and the
    files ``da-tools init`` wrote — there is no ``docs/`` tree there, so the
    pointer named a file they cannot open. Deriving the URL keeps
    ``_CHECK_HINTS`` greppable as repo paths while what gets *printed* is
    reachable from outside this repository.

    ``mkdocs.yml`` builds every page under ``docs/`` (only ``exclude_docs``
    is held back) and leaves ``use_directory_urls`` at its default, so
    ``docs/a/b.md`` is served at ``<base>a/b/``.

    ⛔ Two shapes are passed through untouched rather than mangled: an
    absolute URL (already openable) and anything that is not a
    ``docs/**.md`` path. A section anchor is kept — ``docs/a/b.md#c``
    becomes ``<base>a/b/#c`` — because dropping it would silently coarsen a
    pointer that someone deliberately made precise.
    """
    if rel_path.startswith(("http://", "https://")):
        return rel_path
    path, sep, anchor = rel_path.partition("#")
    if not path.startswith("docs/") or not path.endswith(".md"):
        return rel_path
    stem = path[len("docs/"):-len(".md")]
    # mkdocs maps `x/index.md` and `x/README.md` onto the directory itself
    # (and a top-level one onto the site root), so keeping the filename in
    # the URL produces a 404 that the file-exists check cannot see.
    head, _, last = stem.rpartition("/")
    if last in ("index", "README"):
        stem = head
    base = f"{DOCS_SITE_BASE}{stem}/" if stem else DOCS_SITE_BASE
    return f"{base}{sep}{anchor}" if sep else base


# v2.5.0 Phase C: Suggested actions for each check type.
# Maps check name → (hint_message, docs_link).
# ⛔ The link is stored as a repo-relative path and rendered through
# `_docs_url()`; `tests/ops/test_validate_config.py` asserts every target
# resolves to something a reader outside this repository can open, so a hint
# pointing at an unreachable page fails there rather than in their terminal.
_CHECK_HINTS: dict[str, tuple[str, str]] = {
    # #1448 widened this check beyond syntax (encoding, top-level shape), so
    # the one-line remedy had to widen with it: a file rejected for its
    # encoding sends the reader hunting for indentation that is not wrong.
    # Each listed file states its own remedy on its own line above this.
    "yaml_syntax": (
        "Fix each file listed above as it says — YAML syntax (indentation, "
        "quoting, colons), file encoding, or top-level shape.",
        "docs/getting-started/for-platform-engineers.md",
    ),
    "schema": (
        "Remove unknown keys or add them to the schema. "
        "Run: da-tools explain-route --tenant <id> to inspect resolved config.",
        "docs/scenarios/hands-on-lab.md",
    ),
    "routes": (
        "Check _routing and _routing_defaults for invalid receiver types, "
        "group_by, or timing values. "
        "Run: da-tools explain-route --config-dir <dir> --tenant <id>",
        "docs/scenarios/alert-routing-split.md",
    ),
    "policy": (
        "Review _domain_policy.yaml constraints (allowed/forbidden receiver types, "
        "timing guardrails). Contact a domain admin to update if needed.",
        "docs/internal/test-coverage-matrix.md",
    ),
    "custom_rules": (
        "Validate rule pack YAML syntax and ensure referenced profiles exist. "
        "Run: da-tools rule-pack-split --check to verify.",
        "docs/internal/test-coverage-matrix.md",
    ),
    "profiles": (
        "Ensure all _profile references in tenant configs match a defined profile "
        "in _defaults.yaml or the profiles section.",
        "docs/architecture-and-design.md",
    ),
    "versions": (
        "Run: make version-check && make bump-docs to synchronize version numbers.",
        "docs/internal/github-release-playbook.md",
    ),
    "policy_dsl": (
        "Check Policy-as-Code DSL syntax. "
        "Run: da-tools opa-evaluate --policy <file> --config-dir <dir>",
        "docs/scenarios/gitops-ci-integration.md",
    ),
    # ⛔ This hint deliberately does NOT say "delete the duplicate file". Each
    # row above already names every file holding the id, and which of them is
    # the survivor is a question about the customer's intent — the cheapest
    # way to a green run here is to delete one at random, and that discards
    # whatever only that file declared, silently.
    "tenant_uniqueness": (
        "Decide which single file owns each tenant listed above and move the "
        "other file's settings into it before removing anything — the "
        "exporter refuses the whole config dir while a tenant is declared "
        "twice.",
        "docs/scenarios/multi-domain-conf-layout.md",
    ),
}


def _unusable_files(results: list[dict[str, object]]) -> list[str]:
    """Files `yaml_syntax` could not read, taken from its own bookkeeping.

    #1448: every other check *skips* a file it cannot read and then reports
    on what remained — so `schema` says "No schema warnings" and `routes`
    says PASS about a tenant that was never loaded. Before #1448 nobody saw
    those rows (the process died first); making the report appear made a
    report that asserts health for files nobody could open. The rows are
    left as they are — this only lets the reader see what they exclude.

    ⛔ The list is read from the ``unusable_files`` key ``check_yaml_syntax``
    filled in, never re-derived from the printed detail lines. The first
    draft did the latter (``detail.split(":", 1)[0]``) and a run that
    crashed before the check finished therefore produced three *sentences*
    where the filenames go — "⚠️ 3 file(s) could not be parsed … (this check
    could not complete on your input, The report below is incomplete …)" —
    and put the same sentences into the ``skipped_unusable_files`` JSON
    field consumed programmatically.
    """
    for r in results:
        if r["check"] == "yaml_syntax":
            return [str(f) for f in r.get("unusable_files", [])]
    return []


def _caveat_applies(row: dict[str, object]) -> bool:
    """Does the "files were skipped" caveat belong on this row?

    Derived from what the row is, not from its name: it belongs on a check
    that reads the config tree (so the unreadable files really were part of
    its input) and that is not itself the row reporting them.

    ⛔ The first draft asked ``r["check"] != "yaml_syntax"``, which told
    ``versions`` (a ``bump_docs.py`` subprocess) and ``custom_rules`` (which
    reads ``--rule-packs``) that a broken file in ``conf.d/`` was why they
    failed — a false attribution printed straight at the customer.

    Rows built by hand rather than through ``_run_check`` keep the caveat,
    which is the older behaviour and the safer default: a missing caveat
    silently over-claims coverage, an extra one only over-warns.
    """
    return (bool(row.get("reads_config_dir", True))
            and not row.get("unusable_files"))


def print_report(results: list[dict[str, object]], as_json: bool = False) -> None:
    """Print the validation report with suggested actions (v2.5.0)."""
    unusable = _unusable_files(results)
    caveat = ""
    if unusable:
        listed = ", ".join(unusable[:5]) + (" …" if len(unusable) > 5 else "")
        caveat = UNUSABLE_CAVEAT.format(n=len(unusable),
                                        listed=listed)

    # v2.5.0: Inject hints into JSON output for programmatic consumers
    if as_json:
        enriched = []
        for r in results:
            entry = dict(r)
            # ⛔ Internal bookkeeping does not go in the customer's JSON.
            # These exist so the report can decide where the skipped-file
            # caveat belongs and which advice line to print; publishing them
            # turns an implementation detail into something a consumer can
            # depend on. An earlier version popped only `reads_config_dir`
            # while its own comment said "it appears on every row of a
            # perfectly healthy run" — which was equally true of the other
            # two: measured on a clean `try-local/seed/conf.d`, every one of
            # the five PASS rows carried `"unusable_files": []` and
            # `"hint": null`, growing the document 750B -> 970B for nothing.
            # `hint` is in any case a duplicate of `suggested_action`, which
            # this same function already emits.
            # ⚠️ `caller_error` matches that description too and is NOT
            # popped: it has been in this document since v2.5.0, so removing
            # it is a contract change rather than a contract not yet made.
            # The rule is "do not ADD internal fields", not "no internal
            # field may remain".
            entry.pop("reads_config_dir", None)
            entry.pop("hint", None)
            if not entry.get("unusable_files"):
                entry.pop("unusable_files", None)
            if unusable and _caveat_applies(r):
                entry["skipped_unusable_files"] = unusable
            if r["status"] != PASS:
                hint, docs = _CHECK_HINTS.get(r["check"], ("", ""))
                hint = r.get("hint") or hint
                if hint:
                    entry["suggested_action"] = hint
                    entry["docs_link"] = _docs_url(docs)
            enriched.append(entry)
        print(json.dumps(enriched, indent=2))
        return

    print("=" * 60)
    print("  validate-config — Unified Validation Report")
    print("=" * 60)

    status_icon = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}

    for r in results:
        icon = status_icon[r["status"]]
        print(f"\n[{icon}] {r['check']}")
        for detail in r.get("details", []):
            # #1538: `details` carries tenant-controlled filenames and the
            # exception text derived from them. Escaped HERE, not in the result
            # dict, because the --json branch above returns before this loop and
            # its bytes must not change (json.dumps already escapes).
            print(f"       {safe_label(detail)}")
        if caveat and _caveat_applies(r):
            print(f"       {safe_label(caveat)}")

        # v2.5.0 Phase C: Show suggested action for non-passing checks
        if r["status"] != PASS:
            hint, docs = _CHECK_HINTS.get(r["check"], ("", ""))
            hint = r.get("hint") or hint
            if hint:
                # ⛔ The hint is written for a report that could read every
                # file. With a broken `_defaults.yaml`, `schema` reports each
                # legitimate tenant key as "unknown" and this line says
                # "Remove unknown keys" — following it deletes a working
                # config. The finding is not suppressed (it may be real once
                # the read error is fixed); the order of operations is.
                if caveat and _caveat_applies(r):
                    print(f"       {READ_ERRORS_FIRST}")
                print(f"       -> Suggested action: {hint}")
                print(f"       -> See: {_docs_url(docs)}")

    # Summary
    total = len(results)
    passed = sum(1 for r in results if r["status"] == PASS)
    warned = sum(1 for r in results if r["status"] == WARN)
    failed = sum(1 for r in results if r["status"] == FAIL)

    print("\n" + "-" * 60)
    print(f"  Total: {total} checks | "
          f"{passed} pass | {warned} warn | {failed} fail")
    print("-" * 60)

    if failed > 0:
        print("  Result: FAIL")
    elif warned > 0:
        print("  Result: WARN (pass with warnings)")
    else:
        print("  Result: PASS")
    print()


# ============================================================
# Check dispatch
# ============================================================
# Exceptions a check raises when the customer's config could not be read,
# as opposed to "this tool broke".
#
# ⛔ This pair is NOT closed, and an earlier draft of this comment claimed it
# was ("the exception contract of the reader"). It is not: `yaml.safe_load`
# also raises `RecursionError` on deeply nested input (roughly half
# `sys.getrecursionlimit()`: 495 flow-style / 493 block-style here, and
# it moves with the limit, so it is not a constant), and `open()` raises `OSError` on a path it cannot read. The
# enumeration is only a shortcut for the two overwhelmingly common cases —
# the guarantee that a customer file is *named* rather than reported as our
# bug comes from `check_yaml_syntax`, which reads every file under
# `--config-dir` first, one at a time, and catches everything (it can, since
# it knows the filename). Anything reaching the classifier below has already
# been named there if it belongs to a file in that tree.
_INPUT_ERRORS = (yaml.YAMLError, UnicodeDecodeError)


def _reads_config_dir(config_dir, args, kwargs) -> bool:
    """Was this check actually handed the tree ``--config-dir`` points at?

    #1448: the skipped-file caveat used to be attached to every row except
    ``yaml_syntax``, so ``versions`` (which shells out to ``bump_docs.py``)
    and ``custom_rules`` (which reads ``--rule-packs``) were told that an
    unreadable file in ``conf.d/`` was why they failed.

    ⛔ The answer comes from the **value that was passed**, not from a
    parameter name. An earlier version asked
    ``"config_dir" in inspect.signature(fn).parameters``, which made a
    customer-facing guarantee depend on an identifier that carries no
    contract: renaming ``check_routes(config_dir, …)`` to ``conf_root``
    silently dropped the caveat and the "fix the read errors first" line
    from that row — measured — and the cheapest way back to green was to
    rename it back. A decorator without ``functools.wraps`` collapses the
    signature the same way. Comparing the argument survives both.

    ``config_dir`` of ``None`` means the caller did not say; the row keeps
    the caveat, because a missing caveat over-claims coverage while an
    extra one only over-warns.
    """
    if config_dir is None:
        return True
    return any(a == config_dir for a in args) or any(
        v == config_dir for v in kwargs.values())


def _run_check(name: str, fn, *args, _config_dir: str | None = None,
               **kwargs) -> dict[str, object]:
    """Run one check and turn an *input*-caused crash into a report row.

    #1448: a YAML syntax error anywhere in the customer's ``conf.d/`` escaped
    ``check_profiles`` as an uncaught ``yaml.ScannerError``. The process died
    before ``print_report`` ran, so the command printed a Python traceback on
    stderr and **zero bytes on stdout** — while §7 of the GitOps guide tells
    customers to run exactly this command and read the report. The exit code
    was 1 either way, so nothing downstream noticed the report had vanished.

    Wrapping the *dispatch* rather than the three ``load_yaml_file`` calls
    that happened to be named in the ticket means a check added later is
    covered without anyone remembering this;
    ``TestNoCheckCanKillTheReport`` in ``tests/ops/test_validate_config.py``
    is what keeps that true — it forces each check in turn to fail the way a
    bad customer file makes it fail, and requires the report to survive.
    (Earlier revisions of this branch cited an AST guard here; it was
    removed for false-redding five ordinary dispatch refactors, and the
    citation outlived it by a round.)

    ⛔ Every exception is caught, and the first draft of this got that wrong.
    It converted only ``yaml.YAMLError``/``OSError`` on the theory that
    anything else "is a defect in this tool" and should keep propagating.
    Measured against six syntactically **valid** customer configs, three
    still died with an empty stdout — one of them inside ``check_profiles``,
    the function #1448 named. The axis was wrong: "is this our bug or theirs"
    is a different question from "does the customer get a report", and the
    ticket is about the second.

    ⛔ The *classification*, however, is on the input axis and has to stay
    there. An editor that saved one tenant file in the local code page
    raises ``UnicodeDecodeError``; the first draft let that fall into the
    catch-all, which exits **2** and prints "Please report this along with
    the file it names" — telling the customer their own encoding problem is
    our bug, in a message that names no file because ``UnicodeDecodeError``
    carries no path. It is a finding about their config: exit 1, same as a
    syntax error.

    Nothing is swallowed: for anything that is genuinely not an input error
    the traceback still reaches stderr and the run exits 2, so a run that
    could not complete stays distinguishable from one that found violations.
    """
    try:
        row = fn(*args, **kwargs)
        # ⛔ setdefault, not assignment — and THIS is the one site where the
        # distinction is load-bearing, because `row` came from the check
        # itself: a check that already knows its failure is an argv error
        # rather than a config-tree finding says so itself (see
        # _argv_error_row). Overwriting it re-attributed
        # `--policy`/`--rule-packs` mistakes to the customer's conf.d, and the
        # exit-code downgrade below then turned a caller error into exit 1
        # whenever any tenant file happened to be unreadable — measured.
        row.setdefault("reads_config_dir",
                       _reads_config_dir(_config_dir, args, kwargs))
        return row
    except _INPUT_ERRORS as exc:
        detail = " ".join(str(exc).split())
        row = _make_result(
            name, FAIL,
            [f"could not read the config: {detail}",
             # ⛔ Not "the file reported by yaml_syntax above": an
             # unreadable `--policy` or `--rule-packs` file lives outside
             # `--config-dir`, so yaml_syntax passes and points at nothing.
             # ⛔ Does not promise a filename. A YAMLError carries the
             # path; a UnicodeDecodeError carries a codec and an offset and
             # nothing else — and an unreadable --policy / --rule-packs file
             # lives outside --config-dir, so yaml_syntax cannot name it
             # either. Measured: a non-UTF-8 --policy file produced this row
             # with no filename anywhere in the report, under a sentence
             # telling the reader to go read the name.
             "This check never reached its own logic. If no file is named "
             "above, the fault is in one of the paths you passed on the "
             "command line — check each one's encoding and syntax."])
        # ⚠️ setdefault here only for uniformity with the try branch above,
        # where it IS load-bearing. In this branch it is equivalent to plain
        # assignment: `row` was built three lines up by `_make_result`, which
        # never sets `reads_config_dir` (measured — its keys are caller_error /
        # check / details / hint / status / unusable_files). An earlier
        # revision copied the try branch's causal sentence into all three
        # sites; in this one it described a re-attribution that cannot happen,
        # because there is nothing here to overwrite.
        row.setdefault("reads_config_dir",
                       _reads_config_dir(_config_dir, args, kwargs))
        return row
    except SystemExit as exc:
        # ⛔ `SystemExit` is not an `Exception`, so the clause below does not
        # see it — and `_parse_config_files` calls `sys.exit()` when the
        # config directory has gone, which `check_schema` / `check_routes` /
        # `check_policy` all reach. `main()` checks `isdir` first, so this
        # needs a TOCTOU to fire (a GitOps ConfigMap projected volume swaps
        # `..data` atomically), and then the process dies before
        # `print_report`: #1448's own symptom, through the wrapper written
        # to prevent it. The sibling fix in `_no_declared_defaults_hint`
        # landed a round before this one and its comment said this clause
        # was already covered; it was not.
        detail = f"a check exited the process (code {exc.code})"
        row = _make_result(
            name, FAIL,
            [f"this check could not complete on your input: {detail}",
             "The report below is incomplete — this check reached none of "
             "its own conclusions.",
             "If every file listed above is readable, this is a defect in "
             "this tool — please report it."],
            caller_error=True)
        row["reads_config_dir"] = _reads_config_dir(_config_dir, args, kwargs)
        return row
    except Exception as exc:  # noqa: BLE001 — see the contract above
        traceback.print_exc()
        detail = " ".join(str(exc).split()) or exc.__class__.__name__
        row = _make_result(
            name, FAIL,
            [f"this check could not complete on your input: "
             f"{exc.__class__.__name__}: {detail}",
             "The report below is incomplete — this check reached none of "
             "its own conclusions. A traceback was written to stderr.",
             # ⛔ Conditional on purpose. When yaml_syntax has already named
             # a file it could not read, every later failure is downstream
             # of that, and telling the customer to file a bug against us
             # sends them to the wrong place — measured as the actual
             # outcome for a tenant file saved in the local code page.
             "If every file listed above is readable, this is a defect in "
             "this tool — please report it with the traceback."],
            caller_error=True)
        # ⚠️ setdefault here only for uniformity with the try branch above,
        # where it IS load-bearing. In this branch it is equivalent to plain
        # assignment: `row` was built three lines up by `_make_result`, which
        # never sets `reads_config_dir` (measured — its keys are caller_error /
        # check / details / hint / status / unusable_files). An earlier
        # revision copied the try branch's causal sentence into all three
        # sites; in this one it described a re-attribution that cannot happen,
        # because there is nothing here to overwrite.
        row.setdefault("reads_config_dir",
                       _reads_config_dir(_config_dir, args, kwargs))
        return row


# ============================================================
# Main
# ============================================================
def main() -> None:
    """CLI entry point: One-stop configuration validation."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=_h('description'))
    parser.add_argument("--config-dir", required=True,
                        help=_h('config_dir'))
    parser.add_argument("--policy",
                        help=_h('policy'))
    parser.add_argument("--rule-packs",
                        help=_h('rule_packs'))
    parser.add_argument("--version-check", action="store_true",
                        help=_h('version_check'))
    parser.add_argument("--json", action="store_true",
                        help=_h('json'))
    parser.add_argument("--policy-dsl",
                        help=_h('policy_dsl'))
    parser.add_argument("--strict", action="store_true",
                        help=_h('strict'))
    args = parser.parse_args()

    if not os.path.isdir(args.config_dir):
        print(f"ERROR: config-dir not found: {args.config_dir}",
              file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # Ensure tools dir is in sys.path for imports
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    results = []

    # 1. YAML syntax
    results.append(_run_check("yaml_syntax", check_yaml_syntax,
                              args.config_dir, _config_dir=args.config_dir))

    # 2. Schema validation (--strict: domain-policy violations → FAIL)
    results.append(_run_check("schema", check_schema, args.config_dir,
                              strict=args.strict, _config_dir=args.config_dir))

    # 3. Route validation
    results.append(_run_check("routes", check_routes, args.config_dir,
                              args.policy, _config_dir=args.config_dir))

    # 4. Policy check (if policy provided)
    if args.policy:
        results.append(_run_check("policy", check_policy, args.config_dir,
                                  args.policy, _config_dir=args.config_dir))

    # 5. Custom rule lint (if rule-packs dir provided)
    if args.rule_packs:
        results.append(_run_check("custom_rules", check_custom_rules,
                                  args.rule_packs, args.policy, _config_dir=args.config_dir))

    # 6. Profile references (v1.12.0)
    results.append(_run_check("profiles", check_profiles, args.config_dir, _config_dir=args.config_dir))

    # 7. Version consistency (if requested)
    if args.version_check:
        results.append(_run_check("versions", check_versions, _config_dir=args.config_dir))

    # 8. Policy-as-Code (DSL evaluation)
    results.append(_run_check("policy_dsl", check_policy_dsl,
                              args.config_dir,
                              getattr(args, 'policy_dsl', None), _config_dir=args.config_dir))

    # 9. Tenant declaration uniqueness (#1577) — unconditional: the state it
    # finds makes the exporter refuse the whole tree, so there is no flag
    # under which a customer would want this one skipped.
    results.append(_run_check("tenant_uniqueness", check_tenant_uniqueness,
                              args.config_dir, _config_dir=args.config_dir))

    # Report
    print_report(results, as_json=args.json)

    # Exit code
    has_fail = any(r["status"] == FAIL for r in results)
    # #452/#737: a FAIL caused by a missing/timed-out prerequisite tool
    # (lint_custom_rules.py / bump_docs.py) is a caller-error (exit 2), not a
    # config validation finding (exit 1).
    # #1448: …but only for a check that could actually read the customer's
    # config. If `yaml_syntax` named a file it could not read, a later
    # failure *in a check that reads that tree* is downstream of it, and
    # exit 2 ("this tool broke, report it") sends the customer to the wrong
    # place for a problem in their own tree.
    #
    # ⛔ Per row, not globally. The first version of this asked only "was
    # anything unreadable?" and downgraded EVERY caller-error — so a
    # genuine `bump_docs.py not found` from `versions`, which never opens
    # anything under `--config-dir`, silently became exit 1 because an
    # unrelated tenant file was saved in the wrong code page. That is the
    # same false attribution `_caveat_applies` exists to prevent, made in
    # the exit code instead of the text.
    # Measured, six cells — the third axis is the point of the rule and
    # an earlier version of this table left it out, which made the row that
    # matters most read backwards:
    #   unreadable=F caller=F                        → 1
    #   unreadable=F caller=T  reads_config_dir=F    → 2
    #   unreadable=F caller=T  reads_config_dir=T    → 2
    #   unreadable=T caller=F                        → 1
    #   unreadable=T caller=T  reads_config_dir=F    → 2  ← was 1
    #   unreadable=T caller=T  reads_config_dir=T    → 1  ← downgraded, and
    #     this is the case the downgrade exists for
    input_unreadable = bool(_unusable_files(results))
    caller_error = any(
        r["status"] == FAIL and r.get("caller_error")
        and not (input_unreadable and r.get("reads_config_dir", True))
        for r in results
    )
    if caller_error:
        sys.exit(EXIT_CALLER_ERROR)
    sys.exit(EXIT_VIOLATION if has_fail else EXIT_OK)


if __name__ == "__main__":
    main()
