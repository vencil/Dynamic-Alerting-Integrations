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
  1b. Quoting      — An unquoted value in a field the JSON Schema types as a
                    string, that PyYAML reads as a boolean / number / null
                    (`channel: yes` → True, while the Go readers and
                    Alertmanager read "yes"). The resolver's verdict and the
                    schema's types decide; no word list (#2164)
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
  10. Root defaults — `defaults:` in the root _defaults.yaml, as the
                    exporter decodes it: a value that is not a number
                    (`"70"`, `disable`, a mapping) makes the exporter drop
                    every platform threshold, and an empty value becomes a 0
                    threshold for every tenant (#1414); and no `_routing*`
                    key there — the route generator reads routing only from
                    top-level `_routing_defaults` (#2291)

Hierarchical trees: a row may get its input from a reader that is FLAT — it
reads only the top level of --config-dir — while threshold-exporter reads the
whole tree (today policy_dsl's root-carrier lookup; schema, routes and policy
read the whole tree since #2326, the routing plane's hierarchy).
When such a row runs on a tree that has config files in subdirectories, it
does not report PASS: a PASS becomes WARN, and every status gains a detail
line naming how many files that reader skipped and which (the first few;
the full list is the row's `skipped_nested_files` in --json). Which rows
this applies to is observed at run time, not listed here — a row is flagged
because it called a flat reader, so a check that stops (or starts) doing so
changes sides by itself. A row that never consulted a reader (e.g. `policy`
without an allowlist to enforce) keeps its PASS. ⚠️ Observed means "went
through `warn_nested` or `resolve_defaults_file`"; a root file opened by name
(`profiles` reads only the root `_profiles.yaml`) is not seen — see
`_lib_confd.observe_flat_reads` for the full list of what is not.

⚠️ The exit code does NOT carry this signal: WARN is exit 0 like every other
WARN, because a hierarchical conf.d/ is a supported layout (this repo's own
has an `examples/` subdirectory) and a FAIL would turn every such tree's CI
red over a limitation of this tool. Read `Result:` or --json, not the exit
code, to learn whether every row covered every file.

Usage:
  # Minimal (YAML + schema + routes):
  python3 scripts/tools/ops/validate_config.py \\
    --config-dir components/threshold-exporter/config/conf.d/

  # Full suite (CI):
  python3 scripts/tools/ops/validate_config.py \\
    --config-dir components/threshold-exporter/config/conf.d/ \\
    --policy .github/custom-rule-policy.yaml \\
    --rule-packs rule-packs/ \\
    --version-check

  # JSON output for CI consumption:
  python3 scripts/tools/ops/validate_config.py \\
    --config-dir components/threshold-exporter/config/conf.d/ \\
    --json
"""

from __future__ import annotations

import argparse
import io
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
    WARN_LIMIT,
    FlatRead,
    duplicate_declarations,
    tenant_declarations,
    is_dir_symlink,
    is_reserved_name,
    is_defaults_name,
    iter_config_files,
    observe_flat_reads,
    printable_name,
    readable_carriers,
    resolve_defaults_file,
    select_defaults_carrier,
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
# #2123: conf.d files are read strictly — a key written twice in one mapping
# is a YAML error (the exporter's yaml.v3 rejects the file), not last-wins.
from _lib_io import (  # noqa: E402
    load_yaml_file_strict, strict_safe_load,
)
# #1577 / #2114: tenant declarations are scanned by
# `_lib_confd.tenant_declarations` since #2315. `check_profiles` reads
# `_profiles.yaml` through the exporter-key loader (#2297: a profile name is
# its source text); the tenants' `_profile` refs come from da-guard (#2115).
from _lib_io import YamlFileError, load_yaml_file_strict_exporter_keys  # noqa: E402  (#2297)
# #2115 0-B: the tenants as threshold-exporter reads them (`check_profiles`,
# and the error row `check_policy_dsl` shares with it).
from _lib_tenant_values import (  # noqa: E402
    DaGuardError,
    DaGuardNotFoundError,
    ParseFailedError,
    load_effective_tree,
    load_served_tree,
    print_load_error,
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
# #2431: under --strict the same ERROR prefix also carries a routing matcher
# value that is not a string, and (#2503) a bad group_by entry, so the
# advice names every kind.
POLICY_ONLY_SCHEMA_HINT = (
    "The findings above are --strict errors (ADR-007 domain-policy errors, "
    "a routing matcher value that is not a string, or a bad group_by "
    "entry), not tenant key problems: no unknown key was reported in this "
    "run. \u26d4 Ignore the generic advice about removing keys: fix what "
    "each error names (the domain policy file, quote the matcher value in "
    "YAML, e.g. team: \"yes\", or quote / remove the group_by entry), then "
    "re-run.")
# #2490: the non-strict twin — the same findings as WARNs (non-blocking).
POLICY_ONLY_SCHEMA_WARN_HINT = (
    "The findings above are ADR-007 domain-policy warnings, not tenant key "
    "problems: no unknown key was reported in this run. \u26d4 Ignore the "
    "generic advice about removing keys: change the value each warning "
    "names or amend the domain policy.")
SKIPPED_ENTRY_SCHEMA_HINT = (
    "A line above ends in 'skipping': that setting was dropped as unusable, "
    "not merely flagged. Fix the value it names — e.g. "
    "`_routing_enforced.enabled` must be the YAML boolean true or false — "
    "then re-run.")
# #2279: the generic routes advice ("check _routing … for invalid receiver
# types") points at a receiver that is fine; what is wrong is its NAME.
RECEIVER_COLLISION_ROUTES_HINT = (
    "Two sources above generate a receiver of the same name — usually a "
    "tenant whose id ends in -route-<n> or -override-<n> next to the tenant "
    "it extends. Rename one of the tenants (or remove one of the entries); "
    "the receivers themselves are not invalid.")
# #2311: the routes row now runs every `--validate` step, including
# Alertmanager's own parser. These replace the generic routes advice where it
# would send the operator to the wrong place.
AMTOOL_REJECTED_ROUTES_HINT = (
    "Alertmanager's own parser (amtool check-config) refused the config "
    "generated from this tree; its message above names the field — often a "
    "receiver value in _routing (e.g. a webhook URL) that the generator "
    "accepts but Alertmanager cannot load. Fix that value, then re-run.")
AMTOOL_UNUSABLE_ROUTES_HINT = (
    "The amtool on PATH could not give a verdict (it could not run, timed "
    "out or crashed) — that is this environment, not your config. Fix or "
    "replace that amtool (or take it off PATH), then re-run.")
AMTOOL_NOT_FOUND_ROUTES_HINT = (
    "Put Alertmanager's amtool on PATH so this row can check the generated "
    "config with Alertmanager's own parser — without it, a value Alertmanager "
    "refuses to load is not detected here.")
ROUTES_INVARIANT_HINT = (
    "A platform invariant refused the generated config (see the lines "
    "above) — the same verdict `generate_alertmanager_routes.py --validate` "
    "gives. Fix the _routing / _severity_dedup input that produced it, then "
    "re-run.")
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
    # ⛔ A DIRECTORY SYMLINK FIRST (#1972). `iter_config_files(bad)` would
    # walk INTO the link — it is the root of that call, and `os.walk`
    # follows a symlinked root — and the branch below would then print
    # "the config file(s) INSIDE it ARE loaded", contradicting the reason
    # printed just before it. Neither this scan nor the exporter enters it.
    # The reason already says "NOT loaded", so the clause here is the remedy
    # only — repeating the loss read as a stutter (#1972 blind review N3).
    if is_dir_symlink(bad):
        return ("project those files as flat ConfigMap keys (no `/` in "
                "`items[].path`), or copy them into conf.d")
    if bad.is_symlink():
        return "nothing in it is loaded, by this tool or by threshold-exporter"
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

    Recursive since PR #1343 (conf.d family #1911): ADR-016 allows a hierarchical conf.d/ and the
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
                # Strict (#2123): a duplicate key raises a YAMLError naming
                # the line, and takes the syntax-error branch below.
                loaded = strict_safe_load(f)
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
# Check 1b: Quoting of string fields (#2164)
# ============================================================
_TENANT_SCHEMA = "tenant-config.schema.json"
_PLATFORM_SCHEMA = "platform-defaults.schema.json"
# #2245 / #2232 item 3: `_routing_profiles.yaml` — each profile `$ref`s the
# tenant `routing` definition, so the quoting check reaches profiles too.
_PROFILES_SCHEMA = "routing-profiles.schema.json"
_PROFILES_NAMES = ("_routing_profiles.yaml", "_routing_profiles.yml")


def _find_schema(name: str) -> Path | None:
    """Flat first (the da-tools image ships the schemas beside the tools —
    build.sh REPO_DATA_FILES), then `docs/schemas/` under the project root.
    Same lookup shape as `_grar_validate._find_platform_rules_configmap`."""
    from _lib_compat import PROJECT_ROOT_MARKERS
    here = Path(__file__).resolve().parent
    if (here / name).is_file():
        return here / name
    for base in (here, *here.parents):
        if any((base / m).exists() for m in PROJECT_ROOT_MARKERS):
            candidate = base / "docs" / "schemas" / name
            return candidate if candidate.is_file() else None
    return None


def check_yaml_quoting(config_dir: str) -> dict[str, object]:
    """An UNQUOTED scalar in a string-typed field that PyYAML reads as
    something else — `channel: yes` is True here, "yes" to the Go readers
    and to Alertmanager (#2164). Tenant files are held to
    tenant-config.schema.json, `_defaults*` to platform-defaults.schema.json
    and `_routing_profiles.y(a)ml` to routing-profiles.schema.json (#2245;
    the same selection `check_confd_schema` makes); other `_*` files have
    no schema and are not read. Which words are ambiguous is PyYAML's own
    resolver's verdict and which fields are strings is the schema's — see
    `_lib_io.find_misread_scalars`; nothing is listed here.

    A file that cannot be read is skipped: `yaml_syntax` already names it.
    """
    from _lib_io import compose_all_nodes, find_misread_scalars
    from _lib_confd import is_defaults_document_name
    schemas: dict[str, object] = {}
    for name in (_TENANT_SCHEMA, _PLATFORM_SCHEMA, _PROFILES_SCHEMA):
        path = _find_schema(name)
        if path is None:
            return _make_result(
                "yaml_quoting", FAIL,
                [f"{name} not found beside this tool or under docs/schemas/ "
                 f"— the quoting check cannot run"], caller_error=True)
        with open(path, encoding="utf-8") as fh:
            schemas[name] = json.load(fh)
    errors: list[str] = []
    checked = 0
    for fpath in iter_config_files(config_dir):
        name = fpath.name
        if is_defaults_document_name(name):
            schema_name = _PLATFORM_SCHEMA
        elif name in _PROFILES_NAMES:
            schema_name = _PROFILES_SCHEMA
        elif is_reserved_name(name):
            continue
        else:
            schema_name = _TENANT_SCHEMA
        try:
            label = fpath.relative_to(Path(config_dir)).as_posix()
        except ValueError:
            label = name
        try:
            text = fpath.read_text(encoding="utf-8")
            roots = list(compose_all_nodes(io.StringIO(text)))
        except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError):
            continue
        checked += 1
        for root in roots:
            for hit in find_misread_scalars(root, schemas[schema_name],
                                            schemas, schema_name):
                errors.append(f"{label}:{hit.line}: {hit.message()}")
    if errors:
        return _make_result("yaml_quoting", FAIL, errors)
    return _make_result(
        "yaml_quoting", PASS,
        [f"{checked} files checked: no unquoted value in a string field "
         f"is read as a non-string"])


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
    # #2164: the `--validate` predicate for "an entry was dropped as
    # unusable" (`generate_alertmanager_routes._validate_mode`). Today only
    # a non-boolean `_routing_enforced.enabled` puts one in schema_warnings;
    # without this it was a WARN row at exit 0 while `--validate` failed.
    # #2279: called, not re-spelled — the same function `_validate_mode` uses.
    skipped = gen.blocking_generation_errors(schema_warnings)
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
    if skipped:
        return _make_result("schema", FAIL, schema_warnings,
                            hint=hint or SKIPPED_ENTRY_SCHEMA_HINT)
    # #2490: the same set difference as the FAIL branch above — a row whose
    # only findings are (lenient) domain-policy WARNs reports no key, so
    # "Remove unknown keys" would be the destructive advice again.
    from _grar_validate import POLICY_WARN_PREFIX
    policy_warnings = [w for w in schema_warnings
                       if w.lstrip().startswith(POLICY_WARN_PREFIX)]
    if policy_warnings and not [w for w in schema_warnings
                                if w not in policy_warnings]:
        hint = POLICY_ONLY_SCHEMA_WARN_HINT
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
    if policy_file is not None:   # ⛔ not truthiness: "" is supplied (#1616)
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
            enforced_routing=enforced_routing, tenants=dedup)
        inhibit_rules, dedup_warnings = gen.generate_inhibit_rules(dedup)
    finally:
        sys.stderr = old_stderr

    captured_output = captured.getvalue().strip()
    all_issues = list(route_warnings) + list(dedup_warnings)
    if captured_output:
        all_issues.extend(captured_output.split("\n"))

    summary = (f"{len(routes)} routes, {len(receivers)} receivers, "
               f"{len(inhibit_rules)} inhibit_rules")
    if not routes and not inhibit_rules:
        # Nothing was generated, so there is nothing to assemble or hand to
        # amtool. ⚠️ This is where the row and `--validate` DISAGREE, and did
        # before #2311 too: on a tree whose tenants generate nothing (e.g.
        # only `_severity_dedup: disable`) `--validate` exits 1 with "No valid
        # routes or inhibit rules generated.", while this row is WARN/PASS at
        # exit 0. (A tree with no tenants at all is rc 0 on both sides.) Kept
        # as is: turning it into FAIL would change an existing exit code.
        errors = gen.blocking_generation_errors(all_issues)
        if errors:
            return _make_result("routes", FAIL, all_issues,
                                hint=_collision_hint(gen, errors))
        if all_issues:
            return _make_result("routes", WARN, all_issues)
        return _make_result("routes", PASS, [summary])

    # #2311: --validate semantics, by calling the SAME function
    # `_validate_mode` calls — skipped entries, duplicate receiver names
    # (#2164 / #2279), the ADR-025 inhibit tripwires, the invariants
    # `assemble_configmap` enforces, and Alertmanager's own parser. Called
    # OUTSIDE the stderr capture above: the function returns every line it has
    # instead of printing it, so nothing it says (amtool's "accepted" line, its
    # NOTICE) can be swept into this row as an issue.
    verdict = gen.evaluate_generated_config(routes, receivers, inhibit_rules,
                                            all_issues)
    details = list(all_issues)
    details.extend(e for e in verdict.errors if e not in all_issues)
    details.extend(verdict.warnings)
    if verdict.assembly_error is not None:
        details.append("the assembled Alertmanager config was refused: "
                       + verdict.assembly_error)
    amtool = verdict.amtool
    if amtool is not None and amtool.status != gen.AMTOOL_NOT_FOUND:
        details.extend(ln for ln in amtool.message.splitlines() if ln.strip())

    if verdict.errors:
        return _make_result("routes", FAIL, details,
                            hint=_collision_hint(gen, verdict.errors))
    if verdict.assembly_error is not None:
        return _make_result("routes", FAIL, details, hint=ROUTES_INVARIANT_HINT)
    if amtool.status == gen.AMTOOL_REJECTED:
        return _make_result("routes", FAIL, details,
                            hint=AMTOOL_REJECTED_ROUTES_HINT)
    if amtool.status == gen.AMTOOL_UNUSABLE:
        # No verdict on the config: an environment failure, exit 2 — the
        # same rc `--validate` gives for it.
        return _make_result("routes", FAIL, details, caller_error=True,
                            hint=AMTOOL_UNUSABLE_ROUTES_HINT)
    # Same shape as before #2311: a clean row leads with the summary, a row
    # with findings lists the findings.
    has_findings = bool(all_issues or verdict.warnings)
    if not has_findings:
        details.insert(0, summary)
    if amtool.status == gen.AMTOOL_NOT_FOUND:
        # ⛔ WARN, not a fourth status (the --json enum is pass/warn/fail,
        # #2301): the row ran every Python check and passed them, but the
        # verdict that catches what only Alertmanager refuses was not given.
        details.append(
            "Not validated by Alertmanager: amtool not found on PATH, so the "
            "generated config (assembled on the built-in default base) was "
            "not checked with Alertmanager's parser")
        return _make_result("routes", WARN, details,
                            hint=None if has_findings
                            else AMTOOL_NOT_FOUND_ROUTES_HINT)
    return _make_result("routes", WARN if has_findings else PASS, details)


def _collision_hint(gen, errors: list[str]) -> str | None:
    """#2279: the collision-specific hint when every blocking line is a
    duplicate receiver name; otherwise the generic routes advice."""
    if all(gen.is_receiver_name_collision(e) for e in errors):
        return RECEIVER_COLLISION_ROUTES_HINT
    return None


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
    if policy_file is None:   # ⛔ not truthiness: "" is supplied (#1616)
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

    routing, dedup, _sw, enforced_routing, _mc = gen.load_tenant_configs(config_dir)

    import io
    old_stderr = sys.stderr
    sys.stderr = captured = io.StringIO()

    try:
        _r, _recv, warnings = gen.generate_routes(
            routing, allowed_domains=allowed_domains,
            enforced_routing=enforced_routing, tenants=dedup)
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
    # ⛔ `is None`, not `not rule_packs_dir`. An empty string is SUPPLIED — it is
    # what an unset shell variable expands to — and the falsy test routed it
    # into the skipped branch, removing the custom rule lint from the run at
    # exit 0. The flag's argparse default is None, so nothing else reaches this
    # branch. Same split as #1616's --base-config.
    if rule_packs_dir is None:
        return _make_result("custom_rules", PASS,
                            ["No rule-packs dir — skipped"])
    if not os.path.isdir(rule_packs_dir):
        result = _make_result(
            "custom_rules", FAIL,
            [f"--rule-packs: not a directory: {rule_packs_dir!r}",
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
    if policy_file is not None:   # ⛔ not truthiness: "" is supplied (#1616)
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


# #2115 0-B: when a row cannot read the tenants as threshold-exporter reads
# them, the generic advice points at the wrong thing. One hint per cause; the
# row names what it did not do.
_PROFILES_TREE_UNREADABLE_HINT = (
    "threshold-exporter cannot load this tree as the lines above say: repair "
    "or remove the file they name (or the shape da-guard refuses), then re-run. "
    "No _profile reference was checked.")
_PROFILES_NO_CONFIG_FILE_HINT = (
    "There is no config file under --config-dir at all: point it at your conf.d "
    "tree, then re-run. No _profile reference was checked.")
# Shared by both rows' no-da-guard hints: where a da-guard comes from.
_DA_GUARD_SOURCES = (
    "Put da-guard on $PATH or set $DA_GUARD_BINARY to it (the da-tools image "
    "ships it as /usr/local/bin/da-guard; in a checkout of this repo, `make "
    "da-guard-build` builds it to .build/da-guard, and `make validate-config` "
    "builds and uses it by itself), then re-run.")
_PROFILES_NO_DA_GUARD_HINT = _DA_GUARD_SOURCES + " No _profile reference was checked."


def _tenant_load_failure_row(check: str, exc: Exception, config_dir: str,
                             hints: tuple[str, str, str]) -> dict[str, object]:
    """The FAIL row of a check that reads the tenants through da-guard
    (`_lib_tenant_values`) and could not (#2115 0-B): da-guard's own words
    below one line, and the hint for the cause — `hints` is (tree
    unreadable, no config file, no da-guard). da-guard missing is a caller
    error; everything else is the tree's."""
    unreadable_hint, no_config_hint, no_da_guard_hint = hints
    buf = io.StringIO()
    print_load_error(exc, buf)
    if isinstance(exc, DaGuardNotFoundError):
        hint = no_da_guard_hint
    elif isinstance(exc, ParseFailedError):
        # da-guard named the paths it dropped or could not read (its
        # `parse_failed` / `unreadable`) — a dangling symlink included.
        hint = unreadable_hint
    elif next(iter_config_files(config_dir), None) is None:
        hint = no_config_hint   # no path at all; only picks the advice
    else:
        hint = unreadable_hint
    return _make_result(check, FAIL,
                        ["the tenants cannot be read as threshold-exporter reads them:",
                         # "\n" only: `splitlines` also cuts at U+2028 etc.,
                         # which can sit in a file name (_lib_tenant_values).
                         *(ln for ln in buf.getvalue().split("\n") if ln)],
                        caller_error=isinstance(exc, DaGuardNotFoundError),
                        hint=hint)


def _with_exporter_reasons(exc: ParseFailedError, config_dir: str) -> ParseFailedError:
    """`da-guard effective` names the files the exporter's load drops but not
    WHY (its walk logs nothing); served-values runs the exporter's own load,
    whose log says it (e.g. "cannot unmarshal !!int ..."). On this failure
    path only, ask served-values for the same verdict and keep its error when
    it names the same first file; otherwise — it fails another way (e.g. it
    refuses the tree's /metrics, #2115 F2), or names another file — keep
    effective's. The row's verdict never depends on served-values."""
    try:
        load_served_tree(config_dir)
    except ParseFailedError as served:
        if served.path == exc.path:
            return served
    except (DaGuardNotFoundError, DaGuardError):
        pass
    return exc


def check_profiles(config_dir: str) -> dict[str, object]:
    """Validate tenant _profile references and profile structure.

    Checks:
      - Each tenant's `_profile` binds the profile it names (#2115 0-B: the
        exporter's answer, `da-guard effective`'s `profile`, not a Python
        reading of the files)
      - Profile keys in `_profiles.yaml` don't use reserved prefixes (_, _routing, _state_)
      - Profile values are valid types (numeric, string, dict for scheduled)
      - Profiles have at least one metric key

    One da-guard run, `effective`: its `skipped` names the files with no
    tenant, so served-values (/metrics) is not needed — a tree whose /metrics
    da-guard refuses (e.g. `X_critical` written at the root and in a tenant,
    #2115 F2) is not this row's business. served-values runs only after
    effective has already failed the row on a dropped file, for the
    exporter's reason (`_with_exporter_reasons`).

    The tenants are the exporter's: the whole tree (a tenant in a
    sub-directory included — this row has read recursively since PR #1343),
    a file with no `tenants:` mapping is not one (it is named in the row's
    details, from effective's `skipped`, #2115 R3; the row's status does not
    change for it), and the ids are the exporter's text.
    A `_profile` is read as written plus inherited (#2115 (c)): one that
    binds no profile is reported — as an unknown profile when the tenant's
    own entry names it, and as binding nothing when it comes from a defaults
    file, where the exporter does not read it. A file the exporter's load
    drops or cannot read is a FAIL naming it (#2115 R5: fail-closed), as is
    a missing da-guard (a caller error) and a --config-dir with no config
    file at all (the exporter refuses to start on it; it is not "0 tenants").

    ⚠️ The profile STRUCTURE checks still read `_profiles.yaml` only, and
    "N profiles defined" counts only that file. The exporter reads
    `profiles:` from every root platform file; a profile defined in another
    one is bound (and its references pass) but its body is not linted or
    counted here.

    ⚠️ Profile keys are NOT cross-checked against `defaults:` in
    `_defaults.yaml`. This function's Python does not read that file; da-guard
    effective does, as the exporter's load reads it (a `_defaults.yaml` that
    load drops fails the row, above), and nothing more. It
    used to read it into a `known_defaults` set that nothing consumed —
    no verdict depended on it — and that dead read went through the flat
    root-carrier lookup, so on an ADR-017 tree with a nested
    `_defaults.yaml` it turned this row PASS -> WARN for a file it never
    used (#1652). If the cross-check is ever added back, bring #1448's
    trap with it: `defaults:` written as a bare key parses to None, so
    read it as `raw.get("defaults") or {}`, never `.get("defaults", {})`.
    """
    try:
        tree = load_effective_tree(config_dir)
    except (DaGuardNotFoundError, DaGuardError, ParseFailedError) as exc:
        if isinstance(exc, ParseFailedError):
            exc = _with_exporter_reasons(exc, config_dir)
        return _tenant_load_failure_row(
            "profiles", exc, config_dir,
            (_PROFILES_TREE_UNREADABLE_HINT, _PROFILES_NO_CONFIG_FILE_HINT,
             _PROFILES_NO_DA_GUARD_HINT))
    effective = tree.tenants
    # In the row, not on stderr (#2115 F4): `--json` carries them, and with
    # `--policy-dsl` the policy row's own load already prints them once.
    skipped_lines = [f"not a tenant: {s.file}: {s.reason}" for s in tree.skipped]

    cfg = Path(config_dir)
    profiles_path = str(cfg / "_profiles.yaml")
    # #2297: profile names are the keys' source text, as the exporter keys
    # them — `010:` is "010", the name a tenant's `_profile: 010` reads as.
    profiles_raw = load_yaml_file_strict_exporter_keys(profiles_path, default={})
    profiles = profiles_raw.get("profiles", {}) if isinstance(profiles_raw, dict) else {}

    warnings = []
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

    # ── Tenant _profile reference validation (#2115 0-B) ──
    # `effective_config["_profile"]` is the name as written (source text) plus
    # inherited; `profile` is the one the exporter binds, None for none.
    for t_name, te in sorted(effective.items()):
        written = te.effective_config.get("_profile")
        if not isinstance(written, str) or not written.strip():
            continue
        profile_ref_count += 1
        if te.profile is not None:
            continue
        name = written.strip()
        source = te.key_sources.get("_profile")
        if source is not None and source.layer == "defaults":
            warnings.append(
                f"tenant={t_name}: _profile \"{name}\" comes from {source.file}, "
                f"and the exporter binds no profile from a defaults file (only from "
                f"the tenant's own entry or a root platform file's `tenants:` entry)")
        else:
            warnings.append(
                f"tenant={t_name}: _profile references unknown profile "
                f"\"{name}\"")

    if warnings:
        return _make_result("profiles", WARN, warnings + skipped_lines)
    details = [f"{len(effective)} tenants scanned, {profile_ref_count} profile refs, "
               f"{len(profiles)} profiles defined in _profiles.yaml"]
    return _make_result("profiles", PASS, details + skipped_lines)

# ============================================================
# Check 8: Policy-as-Code (DSL evaluation)
# ============================================================
# #2115 0-B: when policy_dsl cannot read the tenants, the generic "check the
# DSL syntax" advice points at the wrong thing — the policy file is fine.
_POLICY_DSL_TREE_UNREADABLE_HINT = (
    "threshold-exporter cannot load this tree as the lines above say: repair "
    "or remove the file they name (or the shape da-guard refuses), then re-run. "
    "No policy was evaluated.")
_POLICY_DSL_NO_CONFIG_FILE_HINT = (
    "There is no config file under --config-dir at all: point it at your conf.d "
    "tree, then re-run. No policy was evaluated.")
_POLICY_DSL_NO_DA_GUARD_HINT = _DA_GUARD_SOURCES + " No policy was evaluated."
_POLICY_DSL_ROUTING_REFUSED_HINT = (
    "A rule reads `_routing`, and the route generator refuses this tree for the "
    "reasons above (`da-tools generate-routes --validate` shows the same): fix "
    "them, then re-run. No policy was evaluated.")


def check_policy_dsl(config_dir: str, policy_dsl_file: str | None = None) -> dict[str, object]:
    """Evaluate declarative policies from _defaults.yaml _policies or standalone file.

    Loads policy rules from _defaults.yaml ``_policies`` section and/or
    a standalone policy DSL file, then evaluates against all tenant configs.

    The row carries ``_policy_scope`` (``root_carrier``: whether --config-dir
    has a root defaults carrier; ``policy_dsl``: whether --policy-dsl was
    given; ``evaluated``: whether tenants were evaluated) for ``_flag_flat_reads`` to word its line by, and only for it:
    it pops the key, so it never reaches the report (#2115 0-B).
    """
    scope: dict[str, object] = {"root_carrier": None, "evaluated": False,
                                "policy_dsl": policy_dsl_file is not None}
    row = _policy_dsl_row(config_dir, policy_dsl_file, scope)
    row["_policy_scope"] = scope
    return row


def _policy_dsl_row(config_dir: str, policy_dsl_file: str | None,
                    scope: dict[str, object]) -> dict[str, object]:
    """`check_policy_dsl`'s body; records into `scope` what it did."""
    try:
        import policy_engine as pe
    except ImportError:
        return _make_result("policy_dsl", PASS,
                            ["policy_engine.py not available — skipped"])

    rules = []

    # From _defaults.yaml
    # #2115 0-B: the tenants are read from the whole tree (below), so only
    # the nested carriers whose `_policies` this lookup skips are named.
    defaults_path = str(resolve_defaults_file(
        Path(config_dir), skipped_note=pe.POLICIES_SKIPPED_NOTE))
    scope["root_carrier"] = os.path.isfile(defaults_path)
    if os.path.isfile(defaults_path):
        rules.extend(pe.load_policies(defaults_path))

    # From standalone policy DSL file.
    # ⛔ The same collapse as --policy, written in the mirror form
    # (`x and is_file(x)` rather than `not x or not is_file(x)`), which is why
    # the structural scan did not see it. Measured before this split:
    # `--policy-dsl <missing>` produced output BYTE-IDENTICAL to passing no
    # flag at all, at exit 0 — the operator asked for a policy file and was
    # told "No _policies defined — skipped".
    #
    # ⛔ `is not None`, not truthiness. An empty string is SUPPLIED — it is what
    # an unset shell variable expands to — and the truthy test routed it back
    # into the same skipped row this split exists to abolish. The flag's
    # argparse default is None, so nothing else reaches the else side. Same
    # split as #1616's --base-config.
    if policy_dsl_file is not None:
        if not os.path.isfile(policy_dsl_file):
            return _argv_error_row(
                "policy_dsl",
                f"--policy-dsl: not a file: {policy_dsl_file!r}",
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
            dsl_data = pe.load_yaml_file_strict(policy_dsl_file)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            return _argv_error_row(
                "policy_dsl",
                f"--policy-dsl: unusable file: {policy_dsl_file!r}: {exc}",
                _POLICY_DSL_INPUT_HINT)
        if dsl_data is not None and not isinstance(dsl_data, dict):
            return _argv_error_row(
                "policy_dsl",
                f"--policy-dsl: top level is {type(dsl_data).__name__}, "
                f"expected a mapping with a `policies:` key: {policy_dsl_file!r}",
                _POLICY_DSL_INPUT_HINT)
        # ⚠️ An empty file, or a mapping with neither `policies:` nor
        # `_policies:`, still reaches "No _policies defined — skipped" at
        # exit 0. That is the CONTENT axis, which this PR does not close for
        # any of the three flags; see the NOT COVERED list in _grar_validate.
        rules.extend(pe.load_policies(policy_dsl_file))

    if not rules:
        return _make_result("policy_dsl", PASS,
                            ["No _policies defined — skipped"])

    # #2115 0-B: the tenants as the exporter reads them (thresholds as
    # /metrics serves them, every part of the day; reserved keys as written
    # plus inherited; `_routing` as the route generator resolves it) — the
    # same reader as `policy-engine --config-dir`. A tree it cannot read that
    # way is a FAIL naming the file / da-guard's words, never a pass over the
    # tenants that happened to be readable; da-guard missing is a caller error.
    try:
        inputs = pe.load_policy_inputs(config_dir, routing=pe.rules_read_routing(rules))
    except (DaGuardNotFoundError, DaGuardError, ParseFailedError) as exc:
        return _tenant_load_failure_row(
            "policy_dsl", exc, config_dir,
            (_POLICY_DSL_TREE_UNREADABLE_HINT, _POLICY_DSL_NO_CONFIG_FILE_HINT,
             _POLICY_DSL_NO_DA_GUARD_HINT))
    except pe.RoutingTreeRefused as exc:
        return _make_result("policy_dsl", FAIL,
                            ["the route generator refuses this tree, so `_routing` cannot "
                             "be resolved:", *exc.lines],
                            hint=_POLICY_DSL_ROUTING_REFUSED_HINT)
    tenant_configs = inputs.views
    if not tenant_configs:
        return _make_result("policy_dsl", PASS,
                            ["No tenant configs found — skipped"])

    result = pe.evaluate_policies(rules, tenant_configs, inputs.aliases)
    scope["evaluated"] = True

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
    # #2315: the scan itself is `_lib_confd.tenant_declarations`, shared with
    # the routing generator's duplicate refusal — one definition of "declares".
    declared, unreadable = tenant_declarations(config_dir)

    # #2049: the predicate is shared with describe_tenant — one answer to
    # "which tenants does more than one carrier declare".
    duplicates = duplicate_declarations(declared)

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
        # ⛔ Its own hint, not the check-level one. `_CHECK_HINTS` is keyed by
        # check NAME, so the FAIL advice ("decide which single file owns each
        # tenant listed above") also printed under this row — where nothing is
        # listed above, because this row just said there is no duplicate.
        # Blind review read it as an instruction to act on an empty list.
        return _make_result("tenant_uniqueness", WARN, [
            f"no duplicate declaration among the {len(declared)} tenant(s) in "
            f"the files that could be read, but {len(unreadable)} file(s) "
            f"could not be read and were not examined: "
            f"{', '.join(sorted(unreadable))} — this is a limit on what was "
            f"checked, not a clean result"],
            hint="Fix the unreadable file(s) named above (yaml_syntax says why) "
                 "and re-run: a second declaration of an existing tenant can be "
                 "sitting in a file this check could not open.")

    return _make_result("tenant_uniqueness", PASS, [
        f"{len(declared)} tenant(s), each declared in exactly one file"])


# ============================================================
# Check 10: Root `defaults:` block (#2291, #1414)
# ============================================================
#: Top-level keys of a `_defaults.yaml` that the routing generator reads. A
#: `_routing*` key written one level too deep, under `defaults:`, is reached by
#: none of them.
_TOP_LEVEL_ROUTING_KEYS = frozenset({"_routing_defaults", "_routing_enforced"})


def _root_defaults_routing_detail(rel: str, key: str, value: object,
                                  dropped_raw: str | None) -> str:
    """One FAIL line for a `_routing*` key under the root `defaults:`.

    *dropped_raw* is the value's source text when the exporter's decoder
    (``deprecate_rule.exporter_verdicts``) rejects it, None when it decodes.
    """
    if key == "_routing":
        move = ("move its contents to a top-level `_routing_defaults:` block "
                "in the same file — that is where platform routing defaults "
                "are read from")
    elif key in _TOP_LEVEL_ROUTING_KEYS:
        move = (f"`{key}` is a top-level key: move it out of `defaults:` to "
                f"the top level of the file")
    else:
        move = ("remove it; platform routing defaults go in a top-level "
                "`_routing_defaults:` block")
    # ⛔ The consequence depends on the value, and the two are not the same
    # severity. The root `defaults:` is `map[string]float64` in the exporter
    # (pkg/config/types.go ThresholdConfig), so a mapping, list, string or
    # boolean there fails the decode and the exporter drops the root file's
    # WHOLE `defaults:` block — every platform threshold, while the load
    # itself still succeeds (measured, #2291: `Defaults=map[]`, 0 resolved
    # rows, one `ERROR: skip unparseable defaults/profiles file` log line).
    # A number or null decodes and is skipped as a threshold; it is still a
    # routing setting nothing applies.
    # #1414: which of the two is yaml.v3's verdict (the same one the check's
    # value rule uses), not PyYAML's type — they disagree on plain scalars
    # (`1e3` is a string to PyYAML and a float to yaml.v3; `1:30` the other
    # way round), and a sentence chosen by PyYAML could contradict the verdict.
    if dropped_raw is None:
        effect = ("threshold-exporter skips it as a threshold and the route "
                  "generator never reads `defaults:`, so this routing setting "
                  "is applied by nothing")
    else:
        kind = {dict: "a mapping", list: "a list"}.get(
            type(value), f"`{dropped_raw}`, which is not a number")
        effect = (f"its value is {kind}, and the root "
                  f"`defaults:` holds numbers only: threshold-exporter cannot "
                  f"decode this file's `defaults:` block and drops ALL of it — "
                  f"every platform threshold, not just this key. The route "
                  f"generator never reads `defaults:` either")
    return f"{rel}: `defaults.{key}` — {effect}; {move}."


def _root_defaults_value_detail(rel: str, key: str | None, raw: str,
                                null: bool) -> str:
    """One FAIL line for a value the exporter drops or treats as unwritten
    (#1414, #2518).

    *key* and *raw* are one item of ``deprecate_rule.exporter_verdicts`` (key
    None = the whole document); *null* is whether its kind is
    ``NULL_NOT_DECLARED`` rather than a blocking one.
    """
    if null:
        # #2518: the fact and nothing else. Every added clause — the
        # line's effect, the key's declaration, a removal procedure, which
        # finding names whom, "numbers only", "compare served-values" — was
        # measured false for some shape (`<<:` merges, the other spelling,
        # `optional_overrides:`, `_critical` rows where served-values exits
        # 2). What a null changes is the exporter's to say, not this mirror's.
        return f"{rel}: `defaults.{key}` has no value (null)."
    if key is None:
        # ⚠️ *raw* (the mirror's reason) is not printed: `deprecate_rule`
        # words it in Chinese, this tool's operator strings are English, and
        # every structural reason that reaches here (a `defaults:` that is a
        # scalar or list, a bad `<<` merge, a non-scalar key) has one remedy.
        return (f"{rel}: threshold-exporter cannot decode this file's "
                f"`defaults:` as a mapping of metric names to numbers and "
                f"drops ALL of it — every platform threshold. Write the root "
                f"`defaults:` as `metric_name: <number>` lines.")
    shown = {"<mapping>": "a mapping", "<list>": "a list"}.get(
        raw, f"`{raw}`, which is not a number")
    return (f"{rel}: `defaults.{key}` is {shown} — the "
            f"root `defaults:` holds numbers only, so threshold-exporter "
            f"cannot decode this file's `defaults:` block and drops ALL of "
            f"it: every platform threshold, not just this key. Write a plain, "
            f"unquoted number.")


#: `_`-prefixed keys another reader takes from the TOP level of a defaults
#: file (#2386); the list lives in _lib_constants (shared with the policy
#: readers since #2115 0-B).
from _lib_constants import TOP_LEVEL_READ_ELSEWHERE  # noqa: E402

#: Keys the defaults-chain merge drops at every level, so they act in no
#: shape (#2386): Go's ``config.MergeDroppedKeys``, pinned by
#: tests/shared/defaults_wrapper_matrix.json.
MERGE_DROPPED_KEYS = frozenset({"_metadata"})


def _root_decoded_keys() -> tuple[set[str], bool]:
    """(the top-level fields the exporter's root decode reads, schema found).

    They are the non-``_`` ``properties`` of platform-defaults.schema.json;
    the Go test ``TestRootDecodedKeysMatchSchema`` pins them to the
    exporter's ``ThresholdConfig`` fields, which da-guard reads for the same
    judgements (#2386). Without the schema the set is empty and the callers
    fail closed, saying so.
    """
    schema_path = _find_schema(_PLATFORM_SCHEMA)
    if schema_path is None:
        return set(), False
    with open(schema_path, encoding="utf-8") as fh:
        props = json.load(fh).get("properties", {})
    return {k for k in props if not k.startswith("_")}, True


def _schema_missing_note(found: bool) -> str:
    return ("" if found else
            f" ({_PLATFORM_SCHEMA} was not found, so fields the exporter "
            f"does read may be listed)")


def _keys_acting_when_merged(raw: dict) -> tuple[list[str], bool]:
    """(sorted top-level keys the defaults merge takes into a tenant's config
    when it reads the whole document, schema found) — da-guard's
    ``actsWhenMerged``, pinned to it by tests/shared/defaults_wrapper_matrix.json.

    A threshold (no ``_`` prefix) or a reserved tenant key
    (``VALID_RESERVED_KEYS`` / ``VALID_RESERVED_PREFIXES``, pinned to Go by
    tests/shared/test_reserved_key_py_go_parity.py). Excluded: ``defaults``;
    a root-decode field (``_root_decoded_keys``); ``TOP_LEVEL_READ_ELSEWHERE``;
    ``MERGE_DROPPED_KEYS``, which act in no shape; ``_routing*`` keys, which
    the route generator does not read from a defaults file in any shape; and
    any other ``_`` key (``_x: &x``, a key that only carries a YAML anchor).
    """
    known, found = _root_decoded_keys()
    out = []
    for key in raw:
        k = str(key)
        if (k == "defaults" or k in known or k in TOP_LEVEL_READ_ELSEWHERE
                or k in MERGE_DROPPED_KEYS):
            continue
        if not k.startswith("_") or (
                (k in VALID_RESERVED_KEYS
                 or k.startswith(VALID_RESERVED_PREFIXES))
                and not k.startswith("_routing")):
            out.append(k)
    return sorted(out), found


_ROOT_NUMBERS_ONLY = (
    "A threshold among them goes under `defaults:`; the root `defaults:` "
    "holds numbers only (a non-numeric value there drops the whole block), "
    "so another key has no place in this file.")


def _root_defaults_unwrapped_detail(rel: str, raw: dict) -> list[str]:
    """The FAIL line for a root carrier with no `defaults:` mapping (#2386).

    Called when ``defaults`` is absent or has no value: the defaults-chain
    merge behind /effective then takes the whole document as the block and
    shows its top-level keys, while threshold-exporter's root decode reads
    platform thresholds only from a ``defaults:`` mapping and has no field
    for the others. Measured: a top-level threshold was absent from /metrics,
    and a top-level ``_severity_dedup: disable`` showed ``disable`` on
    /effective while /metrics served ``enable``. Reported keys:
    ``_keys_acting_when_merged``.
    """
    keys, found = _keys_acting_when_merged(raw)
    if not keys:
        return []
    shown = ", ".join(f"`{k}`" for k in keys)
    return [f"{rel}: no `defaults:` mapping, so /effective (the defaults-chain "
            f"merge, which then reads the whole document) shows the top-level "
            f"key(s) {shown}, but threshold-exporter's root decode does not "
            f"read them: /metrics does not carry them from this file"
            f"{_schema_missing_note(found)}. {_ROOT_NUMBERS_ONLY}"]


def _defaults_toplevel_ignored_detail(rel: str, raw: dict,
                                      root: bool) -> list[str]:
    """The FAIL line for a carrier whose `defaults:` is a mapping while its
    top level holds keys the merge would otherwise take (#2386).

    With ``defaults:`` a mapping (``{}`` included), the defaults-chain merge
    reads only that mapping; a ``defaults:`` with no value, or none at all,
    makes it read the whole document (Go ``ExtractDefaultsBlock``). Reported
    keys: ``_keys_acting_when_merged``. Measured on a subtree: wrapped this
    way, a top-level threshold and ``_severity_dedup`` were no longer served
    on /metrics; ``_silent_mode`` and ``_state_*`` changed /effective only.

    ⛔ Below the root, a RESERVED key (``_is_reserved_key``) is not told to
    move under ``defaults:`` (#2388): subtree defaults do not support reserved
    keys, and moving one there — or leaving ``defaults:`` with no value, which
    merges the whole document — only trades this line for da-guard's
    ``subtree_default_reserved_key``. The per-key fix lives in da-guard; this
    line names it rather than re-deriving it.
    """
    keys, found = _keys_acting_when_merged(raw)
    if not keys:
        return []
    shown = ", ".join(f"`{k}`" for k in keys)
    if root:
        fix = _ROOT_NUMBERS_ONLY
    else:
        fix = _subtree_toplevel_fix(keys)
    return [f"{rel}: `defaults:` is a mapping, so the defaults merge reads "
            f"only the keys under it; the top-level key(s) {shown} are left "
            f"out of every tenant's merged config (/effective), and a "
            f"threshold among them is not served on /metrics"
            f"{_schema_missing_note(found)}. {fix}"]


def _subtree_toplevel_fix(keys: list[str]) -> str:
    """The fix of a subtree carrier's `defaults_wrapper` line (#2388): the
    generic "move them under `defaults:`" for the other keys — without "or
    leave `defaults:` with no value" when a reserved key is present, since that
    merges the reserved key too — and, for reserved keys, "delete or set it in
    the tenant's own entry"."""
    reserved = [k for k in keys if _is_reserved_key(k)]
    other = [k for k in keys if k not in reserved]
    parts = []
    if other:
        shown = ", ".join(f"`{k}`" for k in other)
        if reserved:
            parts.append(f"Move {shown} under `defaults:`.")
        else:
            parts.append(f"Move {shown} under `defaults:`, or leave "
                         f"`defaults:` with no value so the whole document "
                         f"is merged.")
    if reserved:
        shown = ", ".join(f"`{k}`" for k in reserved)
        # ⛔ Not "or set them in each tenant's own entry" (#2388 r6): whether
        # that is the fix depends on the key (an undeclared `_state_<f>` is
        # read nowhere), which needs the root's `state_filters:` — da-guard's
        # verdict, not re-derived here.
        parts.append(
            f"{shown}: reserved key(s), which subtree defaults do not support "
            f"— do not move them under `defaults:`; delete them from this "
            f"file (see da-guard's `subtree_default_reserved_key` for whether "
            f"to set them in each tenant's own entry instead, #2388).")
    return " ".join(parts)


def _is_yaml_mapping(value: object) -> bool:
    """Whether a `defaults:` value is a YAML mapping as yaml.v3 decodes it.

    PyYAML turns ``!!set {a}`` — a mapping in YAML — into a Python ``set``;
    yaml.v3 decodes it as a mapping (nil values), and the merge then reads
    only it. ``!!omap`` / ``!!pairs`` are sequences on both sides. Pinned by
    tests/shared/defaults_wrapper_matrix.json.
    """
    return isinstance(value, (dict, set, frozenset))


#: `defaults_wrapper`'s hint when a SUBTREE carrier's listed keys include a
#: reserved key (#2388 r6): the generic one's "or give `defaults:` no value"
#: would merge that key into the subtree's defaults.
_DEFAULTS_WRAPPER_RESERVED_HINT = (
    "A `defaults:` mapping makes the defaults merge read only that mapping: "
    "move the listed thresholds under `defaults:`; below the root, reserved "
    "keys: see the detail.")


def check_defaults_wrapper(config_dir: str) -> dict[str, object]:
    """Every level's `_defaults.yaml`: top-level keys a `defaults:` mapping
    leaves out of the defaults merge (#2386).

    One carrier per directory, selected as every plane selects it
    (``select_defaults_carrier`` over the readable carriers); a file that
    does not load as a mapping is left to ``yaml_syntax`` /
    ``root_defaults``. Measured on a subtree: top-level
    ``_severity_dedup: disable`` was served as ``disable`` with no wrapper
    or with ``defaults:`` and no value, and as ``enable`` once
    ``defaults: {}`` was added.
    """
    root = Path(config_dir)
    by_dir: dict[Path, list[Path]] = {}
    for p in iter_config_files(root):
        if is_defaults_name(p.name):
            by_dir.setdefault(p.parent, []).append(p)
    details: list[str] = []
    checked = 0
    subtree_reserved = False
    for d in sorted(by_dir):
        readable, _unreadable = readable_carriers(by_dir[d])
        carrier = select_defaults_carrier(readable)
        if carrier is None:
            continue
        try:
            raw = load_yaml_file_strict(str(carrier), default={})
        except YamlFileError:
            continue  # yaml_syntax names a file that does not load
        if not isinstance(raw, dict):
            continue
        checked += 1
        if _is_yaml_mapping(raw.get("defaults")):
            rel = carrier.relative_to(root).as_posix()
            details += _defaults_toplevel_ignored_detail(rel, raw, d == root)
            if d != root and any(_is_reserved_key(k)
                                 for k in _keys_acting_when_merged(raw)[0]):
                subtree_reserved = True
    if details:
        # ⛔ The generic hint says "or give `defaults:` no value": below the
        # root that merges a reserved key into the subtree's defaults, which
        # da-guard reports as subtree_default_reserved_key (#2388 r6).
        return _make_result("defaults_wrapper", FAIL, details,
                            hint=_DEFAULTS_WRAPPER_RESERVED_HINT
                            if subtree_reserved else None)
    return _make_result("defaults_wrapper", PASS, [
        f"{checked} defaults file(s): no top-level key left out by a "
        f"`defaults:` mapping"])


def _schedule_null_lines(rel: str, where: str, body: object) -> list[str]:
    """One FAIL line per threshold key of *body* (a mapping) written as a
    schedule with override windows and a null in it (#2708). Reserved
    (``_``-prefixed) keys are not thresholds and are not judged."""
    if not isinstance(body, dict):
        return []
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from _grar_validate import schedule_null_problems
    out = []
    for key in sorted(k for k in body if isinstance(k, str)):
        if key.startswith("_"):
            continue
        problems = schedule_null_problems(body[key])
        if problems:
            out.append(f"{rel}: `{where}{key}`: {'; '.join(problems)}")
    return out


def check_schedule_null(config_dir: str) -> dict[str, object]:
    """A null inside a threshold schedule that has override windows (#2708).

    A null window entry, a window's ``window: null`` / ``value: null``, or a
    null ``default:`` beside windows has no defined meaning and is refused
    (``_grar_validate.schedule_null_problems``, Go ``scheduleNullProblems``).
    A schedule with no window and a null ``default:`` is plain null (no
    write) and is not this row's (a null in a tenant file is
    ``yaml_quoting``'s).

    Files read: every tenant file's
    ``tenants:`` entries; each directory's selected ``_defaults.yaml``
    carrier's defaults block (its ``defaults:`` mapping, else the whole
    document, as the chain merge reads it); and every ``_`` file at the root
    other than an unselected carrier — its ``tenants:`` entries and
    ``profiles:``. A ``_`` file below the root that is not the selected
    carrier is read by no plane. A file that does not load is left to
    ``yaml_syntax``.
    """
    root = Path(config_dir)
    by_dir: dict[Path, list[Path]] = {}
    files = list(iter_config_files(root))
    for p in files:
        if is_defaults_name(p.name):
            by_dir.setdefault(p.parent, []).append(p)
    selected = set()
    for d, carriers in by_dir.items():
        readable, _unreadable = readable_carriers(carriers)
        chosen = select_defaults_carrier(readable)
        if chosen is not None:
            selected.add(chosen)
    details: list[str] = []
    checked = 0
    for p in sorted(files):
        reserved = is_reserved_name(p.name)
        at_root = p.parent == root
        carrier = p in selected
        if reserved and is_defaults_name(p.name) and not carrier:
            continue  # an unselected carrier: no plane reads it (#1674)
        if reserved and not at_root and not carrier:
            continue  # a nested `_` file: read by no plane
        try:
            raw = load_yaml_file_strict_exporter_keys(str(p), default={})
        except YamlFileError:
            continue
        if not isinstance(raw, dict):
            continue
        checked += 1
        rel = p.relative_to(root).as_posix()
        if carrier:
            block = raw.get("defaults")
            details += _schedule_null_lines(
                rel, "defaults.", block if isinstance(block, dict) else raw)
        if not reserved or at_root:
            tenants = raw.get("tenants")
            if isinstance(tenants, dict):
                for tid, body in tenants.items():
                    details += _schedule_null_lines(rel, f"tenants.{tid}.", body)
        if reserved and at_root:
            profiles = raw.get("profiles")
            if isinstance(profiles, dict):
                for name, body in profiles.items():
                    details += _schedule_null_lines(rel, f"profiles.{name}.", body)
    if details:
        return _make_result("schedule_null", FAIL, details)
    return _make_result("schedule_null", PASS, [
        f"{checked} file(s): no null inside a threshold schedule with "
        f"override windows"])


def check_root_defaults(config_dir: str) -> dict[str, object]:
    """The ROOT `_defaults.yaml`'s `defaults:` as the exporter decodes it.

    Three rules, one row:

    * #1414 — every value must decode as a number. The exporter decodes the
      root ``defaults:`` as ``map[string]float64`` with yaml.v3: a value it
      cannot decode (``"70"``, ``disable``, a mapping …) fails the decode and
      the exporter drops the root file's whole ``defaults:`` — every platform
      threshold, while the load is reported as successful; a null / empty
      value is reported as having no value (#2518; before it, a 0
      threshold) — what it changes depends on the rest of the tree and is
      not judged here. Both FAIL. The verdict is
      ``deprecate_rule.exporter_verdicts`` — the yaml.v3 mirror whose truth
      table ``tests/golden/fixtures/defaults-carrier-oracle.json`` is judged
      by the Go test ``TestDefaultsCarrierOracle`` — called, not re-spelled.
      Every kind in its ``BLOCKING_KINDS`` is a FAIL here too.
    * #2291 — no ``_routing*`` key, whatever its value (below). A
      ``_routing*`` key gets that rule's line only, never a second one from
      the value rule.
    * #2386 — a root carrier with no ``defaults:`` mapping (the key absent,
      or present with no value) whose top level holds a key the merge takes
      (``_keys_acting_when_merged``: a threshold, or a reserved key such as
      ``_severity_dedup``) FAILs: /effective shows it, the exporter's root
      decode does not read it (``_root_defaults_unwrapped_detail``). With no
      such key it still PASSes as "nothing to check".
      A ``defaults:`` mapping beside ignored top-level keys is the
      ``defaults_wrapper`` row's finding, at every level.

    ⚠️ A file PyYAML cannot read at all still fails through
    ``load_yaml_file_strict`` and ``_run_check``'s input-error row, as before;
    a top level that is not a mapping stays ``yaml_syntax``'s finding. The
    value rule speaks about files that load.

    ``defaults:`` holds platform thresholds. Routing defaults live in the
    top-level ``_routing_defaults:`` block, and a ``_routing`` written under
    ``defaults:`` instead gets a different answer from every reader:

    * threshold-exporter decodes the root ``defaults:`` as
      ``map[string]float64``. A mapping there fails that decode and the whole
      block is dropped — every platform threshold, with the load still
      reported as successful (see ``_root_defaults_routing_detail``).
    * the route generator reads platform routing only from top-level
      ``_routing_defaults`` and never looks inside ``defaults:``.
    * the defaults-chain merge (``/effective``, da-guard) carries it into the
      tenant's effective ``_routing`` — a view of routing that never ships.

    The owner's ruling on #2291 is to refuse the shape rather than teach any
    reader to accept it, so every ``_routing``-prefixed key FAILs whatever its
    value, and the detail line says how bad the value makes it.

    ⛔ ROOT ONLY, and the file is the one the exporter selects. A subtree
    ``_defaults.yaml`` is merged by the hierarchical plane, which tolerates
    ``_routing`` keys (``subtree_defaults.go`` ``reservedShapeWins``); that
    shape is tracked with the hierarchical route walker (#1568), not here.
    ``platform-defaults.schema.json`` is applied to every ``_defaults*`` file
    at any depth and cannot tell the root from a subtree, so the rule lives in
    this check and the schema is unchanged.

    ⚠️ The root carrier is found without ``resolve_defaults_file``: that
    helper records nested ``_defaults.yaml`` files as skipped by a flat read,
    which would turn this row WARN on every ADR-017 tree for files it is
    deliberately not about. Selection is ``select_defaults_carrier`` over the
    readable carriers, the rule every plane shares (#1674). The candidates
    come from the shared recursive enumerator filtered to the root level, not
    from a second, flat listing of the directory.
    """
    root = Path(config_dir)
    entries = [p for p in iter_config_files(root)
               if len(p.relative_to(root).parts) == 1
               and is_defaults_name(p.name)]
    readable, _unreadable = readable_carriers(entries)
    carrier = select_defaults_carrier(readable)
    if carrier is None:
        return _make_result("root_defaults", PASS,
                            ["no root _defaults.yaml — nothing to check"])
    rel = carrier.name
    raw = load_yaml_file_strict(str(carrier), default={})
    if not isinstance(raw, dict):
        return _make_result("root_defaults", PASS,
                            [f"{rel}: no `defaults:` mapping — nothing to check"])
    # #1414: the one yaml.v3 mirror, imported in-process like
    # `generate_alertmanager_routes` above — a sibling tool shipped beside
    # this one in both layouts, not a second copy of the decode rules.
    import deprecate_rule as dr
    verdicts = dr.exporter_verdicts(carrier.read_bytes())
    dropped = {k: r for k, r, kind in verdicts
               if k is not None and kind in dr.BLOCKING_KINDS}
    details = [_root_defaults_value_detail(rel, k, r,
                                           kind == dr.NULL_NOT_DECLARED)
               for k, r, kind in verdicts
               if not (k is not None and k.startswith("_routing"))
               and (kind in dr.BLOCKING_KINDS or kind == dr.NULL_NOT_DECLARED)]
    if raw.get("defaults") is None:
        details += _root_defaults_unwrapped_detail(rel, raw)
    block = raw.get("defaults")
    if not isinstance(block, dict):
        if details:
            return _make_result("root_defaults", FAIL, details)
        return _make_result("root_defaults", PASS,
                            [f"{rel}: no `defaults:` mapping — nothing to check"])
    details += [_root_defaults_routing_detail(rel, str(k), v,
                                              dropped.get(str(k)))
                for k, v in block.items()
                if isinstance(k, str) and k.startswith("_routing")]
    if details:
        return _make_result("root_defaults", FAIL, details)
    return _make_result("root_defaults", PASS, [
        f"{rel}: {len(block)} key(s) under `defaults:`, every value a number "
        f"threshold-exporter decodes, no `_routing*` key"])


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
    "yaml_quoting": (
        "Quote each value listed above as it says. A plain yes / no / on / "
        "off / true / 123 in a field the schema types as a string is read "
        "as a boolean or number by PyYAML but as text by the exporter and "
        "Alertmanager, so the tools disagree about your config.",
        "docs/cli-reference.md#validate-config",
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
        "Write each `_profile` in the tenant's own entry (its tenant file, or a "
        "root `_` file's `tenants:`), naming a profile defined under `profiles:` "
        "in a `_` file at the conf.d ROOT (e.g. `_profiles.yaml`): the exporter "
        "ignores `profiles:` in a sub-directory file and binds no `_profile` "
        "inherited from a `_defaults.yaml`.",
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
    # #2291 / #1414: `defaults:` is for numeric thresholds; routing defaults
    # are a sibling block. Each row above already says which key and what to
    # do with it.
    "root_defaults": (
        "`defaults:` in the root _defaults.yaml holds plain numbers only: "
        "write each threshold as an unquoted number; declare a key without a "
        "platform value under `optional_overrides:` rather than leaving it "
        "empty; and move routing settings to a top-level "
        "`_routing_defaults:` block (the only place platform routing "
        "defaults are read from).",
        "docs/cli-reference.md#validate-config",
    ),
    # #2708: the row names the file, the key and where the null is.
    "schedule_null": (
        "Give each listed schedule's default and every override window a "
        "value. To leave that layer without a value for the key, remove the "
        "key or write it as plain `null`.",
        "docs/cli-reference.md#validate-config",
    ),
    # #2386: the row names the file and keys; where they go depends on level.
    "defaults_wrapper": (
        "A `defaults:` mapping makes the defaults merge read only that "
        "mapping: move the listed top-level keys under `defaults:` (in the "
        "root file, thresholds only), or give `defaults:` no value.",
        "docs/cli-reference.md#validate-config",
    ),
    # #1653: not a check — the one row `main()` emits under --json when
    # --config-dir is not a directory and no check could run at all.
    "config_dir": (
        "Point --config-dir at an existing directory (the conf.d/ tree that "
        "holds your tenant YAML files). No check ran, so this is the only "
        "row in the report.",
        "docs/cli-reference.md#validate-config",
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
            entry.pop("hint_docs", None)
            if not entry.get("unusable_files"):
                entry.pop("unusable_files", None)
            if unusable and _caveat_applies(r):
                entry["skipped_unusable_files"] = unusable
            if r["status"] != PASS:
                hint, docs = _CHECK_HINTS.get(r["check"], ("", ""))
                hint = r.get("hint") or hint
                docs = r.get("hint_docs") or docs
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
            docs = r.get("hint_docs") or docs
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


FLAT_READER_HINT = (
    "This row read only the top level of --config-dir, so it did not check "
    "the files named above, which threshold-exporter does load. To have "
    "this row check them, run validate-config once more with --config-dir "
    "pointed at each subdirectory listed, or flatten the tree. Neither "
    "reproduces the exporter's per-level _defaults.yaml inheritance.")

#: policy_dsl's version of `FLAT_READER_HINT` (#2115 0-B): its tenants come
#: from the whole tree, so the only thing not read is `_policies` in nested
#: carriers — and flattening or a sub-`--config-dir` would drop the root
#: defaults the tenants inherit.
POLICY_DSL_NESTED_HINT = (
    "Policy-as-Code takes `_policies` only from the root defaults carrier; the "
    "`_defaults.yaml` files named above are not read for `_policies`. If any of "
    "them holds `_policies`, move those rules to the root carrier or pass them "
    "with --policy-dsl. Do not flatten the tree or point --config-dir at a "
    "subdirectory: that drops the root defaults.")
#: The same, for a --config-dir with no root defaults carrier (#2115 0-B):
#: there is no root default to drop, and the carrier has to be created.
POLICY_DSL_NESTED_NO_ROOT_HINT = (
    "Policy-as-Code takes `_policies` only from the root defaults carrier, and "
    "--config-dir has none; the `_defaults.yaml` files named above are not read "
    "for `_policies`. If any of them holds `_policies`, create a `_defaults.yaml` "
    "at the root of --config-dir holding those rules, or pass them with "
    "--policy-dsl.")

#: Where `FLAT_READER_HINT` points. A row carrying that hint gets THIS link
#: instead of its check's `_CHECK_HINTS` page: the advice and the page it
#: sends the reader to have to be about the same thing.
FLAT_READER_DOCS = "docs/cli-reference.md#hierarchical-confd"


def _flag_flat_reads(row: dict[str, object], flat_reads: list[FlatRead],
                     config_dir: str | None) -> dict[str, object]:
    """Stop a row whose verdict came from a FLAT reader passing as complete.

    #1652 / #1911 (until #2326 made the routing rows read the tree, only
    `policy_dsl`'s lookup is left): `schema`, `routes`, `policy` and `policy_dsl` got their
    tenants from readers that read only the top level of the tree, while the
    exporter reads it recursively. On a tree whose tenants live in `prod/`
    those rows reported ``[PASS] routes  0 routes, 0 receivers`` and
    ``Result: PASS`` — the same answer as a clean flat tree — with the only
    trace of the difference a stderr WARN that reaches neither this report
    nor --json.

    ⛔ WHICH rows is observed, not declared: `flat_reads` is what
    `_lib_confd.observe_flat_reads` recorded while the row's check ran —
    each `warn_nested` call (every nested config file) and each
    `resolve_defaults_file` call (only nested `_defaults.yaml` carriers,
    because that is all a root-carrier lookup skips). A check that returns
    before reaching either — `policy` with no allowlist — keeps its PASS
    without being listed as an exception. ⚠️ `policy_dsl` is NOT such a
    check: it locates the root carrier before any early return, so "no
    policies" keeps its PASS only on a tree whose nested files are all
    tenants. On an ADR-017 tree (a `prod/_defaults.yaml` beside the root
    one) it is WARN naming that file, even when neither holds `_policies` —
    correctly, since the answer "no policies" was reached without opening
    it. ⚠️ What it cannot see is spelled out on
    `observe_flat_reads`: a flat read that calls neither (the enumeration
    contract matches guard NAMES, and two of its guard names record
    nothing — pinned by `tests/shared/test_flat_read_observation_pin.py`),
    and a root file opened by name, e.g. `check_profiles`' `_profiles.yaml`.

    ⛔ PASS -> WARN, never -> FAIL: see the module docstring. A WARN/FAIL
    keeps its status and its own hint (and that hint's docs link); it gains
    the detail line, because "3 findings" on a tree where the reader
    skipped part of the files is not the whole count either.
    """
    scope = row.pop("_policy_scope", None) or {}
    by_dir: dict[str, tuple[str, dict[str, None]]] = {}
    for rec in flat_reads:
        d_abs = os.path.abspath(rec.directory)
        _spelling, rels = by_dir.setdefault(d_abs, (rec.directory, {}))
        for p in rec.skipped:
            rels.setdefault(p.relative_to(Path(rec.directory)).as_posix(), None)
    root_abs = os.path.abspath(config_dir) if config_dir is not None else None
    skipped: list[str] = []
    for d_abs, (d, rel_set) in by_dir.items():
        rels = sorted(rel_set)
        if not rels:
            continue
        shown = [printable_name(r) for r in rels[:WARN_LIMIT]]
        more = (f" (+{len(rels) - WARN_LIMIT} more)"
                if len(rels) > WARN_LIMIT else "")
        where = "--config-dir" if d_abs == root_abs else printable_name(d)
        if row.get("check") == "policy_dsl":
            # #2115 0-B: policy_dsl reads its tenants from the whole tree; the
            # one flat read left is the root-carrier lookup of `_policies`.
            # Only a row that evaluated tenants says so (#2115 0-B).
            if scope.get("root_carrier") is False:
                source = "there is no root defaults carrier"
                if scope.get("policy_dsl"):
                    source += "; rules come from --policy-dsl"
            else:
                source = "`_policies` come from the root defaults carrier only"
                if scope.get("policy_dsl"):
                    source += "; rules also come from --policy-dsl"
            if scope.get("evaluated"):
                source += "; tenants are evaluated from the whole tree"
            line = (f"{len(rels)} defaults file(s) in subdirectories of {where} "
                    f"are not read for `_policies` ({source}): {', '.join(shown)}{more}")
        else:
            line = (f"{len(rels)} config file(s) in subdirectories of {where} were "
                    f"SKIPPED by this check (its reader is flat — top level only): "
                    f"{', '.join(shown)}{more}")
        row["details"] = list(row.get("details") or []) + [line]
        skipped.extend(rels if d_abs == root_abs
                       else [Path(d, r).as_posix() for r in rels])
    if skipped:
        row["skipped_nested_files"] = skipped
        if row["status"] == PASS:
            row["status"] = WARN
            if not row.get("hint"):
                if row.get("check") != "policy_dsl":
                    row["hint"] = FLAT_READER_HINT
                elif scope.get("root_carrier") is False:
                    row["hint"] = POLICY_DSL_NESTED_NO_ROOT_HINT
                else:
                    row["hint"] = POLICY_DSL_NESTED_HINT
                row["hint_docs"] = FLAT_READER_DOCS
    return row


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
    # #1652: every exit below goes through `_flag_flat_reads`, including
    # the crash rows — a check that died after reading flat still read flat.
    flat_reads: list[FlatRead] = []
    try:
        with observe_flat_reads(flat_reads):
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
        return _flag_flat_reads(row, flat_reads, _config_dir)
    except _INPUT_ERRORS as exc:
        detail = " ".join(str(exc).split())
        row = _make_result(
            name, FAIL,
            [f"could not read the config: {detail}",
             # ⛔ Not "the file reported by yaml_syntax above": an
             # unreadable `--policy` or `--rule-packs` file lives outside
             # `--config-dir`, so yaml_syntax passes and points at nothing.
             # ⛔ Does not promise a filename. Files read through the
             # shared `load_yaml_file` now always name themselves (#1654:
             # `YamlFileError` carries the path for both decode and syntax
             # failures); this tool's OWN text-mode reads (yaml_syntax,
             # --policy) still raise a bare UnicodeDecodeError that carries
             # a codec and an offset and nothing else — and an unreadable
             # --policy / --rule-packs file lives outside --config-dir, so
             # yaml_syntax cannot name it either. Measured before #1654: a
             # non-UTF-8 --policy file produced this row with no filename
             # anywhere in the report.
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
        return _flag_flat_reads(row, flat_reads, _config_dir)
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
        return _flag_flat_reads(row, flat_reads, _config_dir)
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
        return _flag_flat_reads(row, flat_reads, _config_dir)


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
        # #1653: under --json this path used to leave stdout EMPTY, so a
        # `validate-config --json | jq` consumer got a parse error instead of
        # a report. It now emits the same document shape as every other run —
        # a list of rows — holding one caller-error row, and still exits 2.
        # ⚠️ Routed through print_report, not hand-built, so the row carries
        # exactly the entry keys the documented contract allows
        # (docs/cli-reference.md, `#### validate-config`).
        if args.json:
            print_report([_make_result(
                "config_dir", FAIL,
                [f"config-dir not found: {args.config_dir}"],
                caller_error=True)], as_json=True)
        sys.exit(EXIT_CALLER_ERROR)

    # Ensure tools dir is in sys.path for imports
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    results = []

    # 1. YAML syntax
    results.append(_run_check("yaml_syntax", check_yaml_syntax,
                              args.config_dir, _config_dir=args.config_dir))

    # 1b. Quoting of string-typed fields (#2164)
    results.append(_run_check("yaml_quoting", check_yaml_quoting,
                              args.config_dir, _config_dir=args.config_dir))

    # 2. Schema validation (--strict: domain-policy violations → FAIL)
    results.append(_run_check("schema", check_schema, args.config_dir,
                              strict=args.strict, _config_dir=args.config_dir))

    # 3. Route validation
    results.append(_run_check("routes", check_routes, args.config_dir,
                              args.policy, _config_dir=args.config_dir))

    # 4. Policy check (if policy provided)
    # ⛔ `is not None`, not truthiness. The flags' argparse defaults are None,
    # so only an explicitly supplied empty string reaches the falsy branch.
    #
    # ⚠️ What THIS line (`--policy`) controls is the REPORT, not the verdict:
    # `check_routes` above takes `args.policy` unconditionally, so `--policy ""`
    # exits 2 from that row even with this line reverted to truthiness — what
    # gets lost is the `[FAIL] policy` row itself. MEASURED both ways.
    # ⛔ Do NOT generalise that to the `--rule-packs` dispatcher below: see the
    # comment there. Two earlier revisions of this comment were wrong in
    # opposite directions, and the second one was wrong because it stated a
    # result measured on `--policy` as if it covered both flags.
    if args.policy is not None:
        results.append(_run_check("policy", check_policy, args.config_dir,
                                  args.policy, _config_dir=args.config_dir))

    # 5. Custom rule lint (if rule-packs dir provided)
    # ⛔ Unlike the `--policy` line above, THIS dispatcher IS the verdict site.
    # `check_custom_rules` has exactly one call site — this one — so there is no
    # unconditional second path to catch an unusable value. MEASURED: reverting
    # this line to truthiness takes `--rule-packs ""` from rc=2 to rc=0 with the
    # whole report reading PASS, verdict and row gone together.
    if args.rule_packs is not None:
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

    # 10. Root `defaults:` block (#2291) — unconditional, same reason: a
    # `_routing` mapping there costs the exporter every platform threshold.
    results.append(_run_check("root_defaults", check_root_defaults,
                              args.config_dir, _config_dir=args.config_dir))

    # 11. `defaults:` mapping beside ignored top-level keys (#2386), every
    # level — unconditional: it changes what is served.
    results.append(_run_check("defaults_wrapper", check_defaults_wrapper,
                              args.config_dir, _config_dir=args.config_dir))

    # 12. A null inside a threshold schedule with override windows (#2708),
    # every layer — unconditional: da-guard refuses the same set.
    results.append(_run_check("schedule_null", check_schedule_null,
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
