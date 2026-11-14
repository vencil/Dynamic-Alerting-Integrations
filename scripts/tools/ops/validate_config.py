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
  1b. Quoting      — A value in a field the JSON Schema types as a string,
                    that yaml.v3 (every reader of conf.d, #2906) reads as a
                    boolean / number / timestamp / null (`channel: 1e3` →
                    1000.0; `channel: yes` is the string "yes"). parse-tree's
                    tag and the schema's types decide; no word list (#2164)
  1c. JSON Schema  — conf.d against tenant-config / platform-defaults /
                    routing-profiles JSON Schema, by calling
                    `lint/check_confd_schema.py`'s verdict; a misread scalar
                    is left to 1b. WARN "not checked" without jsonschema (#2695)
  3. Routes        — `generate-routes --validate` on the tree (with --policy
                    and --strict as given): the row is that command's verdict,
                    the shared key verdict included (`_lib_key_verdict`,
                    ADR-038: keys no reader reads, #3873)
  5. Custom rules  — Deny-list linting on rule-packs/ (if --rule-packs provided),
                    by the lint policy --lint-policy names (#3797)
  6. Profiles      — Tenant _profile references validation
  7. Versions      — bump_docs.py --check version consistency (if --version-check)
  8. Policy-as-Code — Declarative DSL policy evaluation (if _policies in _defaults.yaml or --policy-dsl)
  9. Tenant uniqueness — one tenant id declared by two files makes the exporter
                    reject the ENTIRE config dir (`DuplicateTenantError`), so
                    this one FAIL is about every tenant in the tree (#1577)
  9b. Tenant file name — a tenant file's name (stem) is the one tenant id it
                    declares; tenant-api takes the id from the name, the
                    exporter from `tenants:`, so a mismatch (or a file that
                    declares several tenants) is a FAIL (#3157)
  10. Root defaults — `defaults:` in the root _defaults.yaml, as the
                    exporter decodes it: a mapping or a list as a value makes
                    the exporter drop every platform threshold, and an empty
                    value leaves the key undeclared (#1414, #2518); a scalar
                    it refuses is da-guard's error (row 13); and no `_routing*`
                    key there — the route generator reads routing only from
                    top-level `_routing_defaults` (#2291)
  13. Guard errors — every error finding of da-guard's own check
                    (`da-guard --format json`, `report.findings` with
                    `severity: error`), whatever its kind: da-guard is the one
                    source of the severity (#3387). Its exit 3 (files the load
                    drops) is a FAIL; its exit 4 (routing / domain policy,
                    which the `routes` row judges) is not
  15. Custom alert sources — the custom alerts the compiler builds a rule
                    for beside the ones the exporter serves a threshold for
                    (#3042): a rule that never fires, a served threshold no
                    rule reads. Report only; run only where the compiler is
                    installed (the repo — the da-tools image ships none)
  16. Unsupplied defaults — a threshold the registry ships armed
                    (`arming: default`), of a pack this tree deploys (the
                    root platform file carries one of its keys, or a tenant
                    declares it in `_metadata.db_type`), that the root
                    platform file neither sets under `defaults:` nor lists
                    under `optional_overrides:`. Advisory: WARN at most (#3830)

Hierarchical trees: a row may get its input from a reader that is FLAT — it
reads only the top level of --config-dir — while threshold-exporter reads the
whole tree (today policy_dsl's root-carrier lookup; the routes row reads
the whole tree since #2326, the routing plane's hierarchy).
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
  # Minimal:
  python3 scripts/tools/ops/validate_config.py \\
    --config-dir components/threshold-exporter/config/conf.d/

  # Full suite (CI):
  python3 scripts/tools/ops/validate_config.py \\
    --config-dir components/threshold-exporter/config/conf.d/ \\
    --policy .github/deployment-policy.yaml \\
    --rule-packs rule-packs/ \\
    --lint-policy .github/custom-rule-policy.yaml \\
    --version-check

  # JSON output for CI consumption:
  python3 scripts/tools/ops/validate_config.py \\
    --config-dir components/threshold-exporter/config/conf.d/ \\
    --json
"""

from __future__ import annotations

import argparse
import contextlib
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
from _lib_python import YamlFileError, detect_cli_lang  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_exitcodes import (  # noqa: E402
    CRASH_LINE_PREFIX, EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR, Report)
from _lib_confd import (  # noqa: E402
    WARN_LIMIT,
    FlatRead,
    config_listing,
    duplicate_declarations,
    tenant_declarations,
    iter_config_files,
    list_config_tree,
    observe_flat_reads,
    printable_name,
    resolve_defaults_file,
    unusable_entry,
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
    'lint_policy': {
        'zh': '自訂規則 lint 的 policy YAML（交給 lint 的 --policy；只與 --rule-packs 一起讀）',
        'en': "The custom-rule lint policy YAML (handed to lint's --policy; read only "
              "with --rule-packs)"
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
        'zh': 'routes 列改以 generate-routes --validate --strict 判定（domain policy（ADR-007）等違規由 WARN 升級為 FAIL）',
        'en': 'Judge the routes row as generate-routes --validate --strict (domain-policy (ADR-007) and the other --strict violations go from WARN to FAIL)'
    }
}

def _h(key):
    """Get help text in detected language."""
    return _HELP[key].get(_LANG, _HELP[key]['en'])
import subprocess
import sys

import yaml

from _lib_python import (  # noqa: E402
    load_yaml_file, DOCS_SITE_BASE,
)
# #1577 / #2114: tenant declarations are scanned by
# `_lib_confd.tenant_declarations` since #2315. `check_profiles` reads
# `_profiles.yaml` through the exporter-key loader (#2297: a profile name is
# its source text); the tenants' `_profile` refs come from da-guard (#2115).
# ADR-036 step 4 (#2906): every row's conf.d read is `da-guard parse-tree`'s,
# one document per run (`_lib_parse_cache.session` in `main`), as the route
# generator these rows ask reads it — never a PyYAML reading beside it.
from _lib_io import load_confd_file  # noqa: E402
import _lib_input  # noqa: E402  (#3755: --config-dir is a declared input)
import _lib_parse_cache  # noqa: E402
import _lib_parse_tree  # noqa: E402
from _lib_parse_tree import FileUnreadable, ParseTreeError  # noqa: E402
# #2115 0-B: the tenants as threshold-exporter reads them (`check_profiles`,
# and the error row `check_policy_dsl` shares with it).
from _lib_tenant_values import (  # noqa: E402
    DA_GUARD_PREFIX,
    GUARD_REPORT_SEVERITY_ERROR,
    DaGuardError,
    DaGuardNotFoundError,
    ParseFailedError,
    load_effective_tree,
    load_guard_report,
    load_served_schedules,
    load_served_tree,
    print_load_error,
)
from _lib_tenant_values import key_spellings as tv_key_spellings  # noqa: E402
# ADR-038: the shared verdict on keys no reader reads (`check_no_alert`).
import _lib_key_verdict as key_verdict  # noqa: E402
# #3142: the deployment --policy's remedy, shared with the tools that take it.
from _grar_validate import (  # noqa: E402
    POLICY_REFUSAL_REMEDY, POLICY_VERSION_REMEDY, POLICY_WARN_PREFIX,
    PolicyInputError, PolicyVersionError, is_field_type_error)

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
POLICY_ONLY_HINT = (
    "The findings above are ADR-007 domain-policy errors (a policy that "
    "cannot be used is refused in every mode) or --strict errors (a routing "
    "matcher value that is not a string, or a bad group_by entry): fix "
    "what each error names (fix the domain policy file, or move a misplaced "
    "domain_policies block into _domain_policy.yaml; quote the matcher "
    "value in YAML, e.g. team: \"yes\", or quote / remove the group_by "
    "entry), then re-run.")
# #2490: the non-strict twin — the same findings as WARNs (non-blocking).
POLICY_ONLY_WARN_HINT = (
    "The findings above are ADR-007 domain-policy warnings: change the value "
    "each warning names or amend the domain policy.")
# #3284: a row whose findings are all `ERROR (field type):` lines (#2906 —
# a group_by that is not a list, a quoted boolean, an unknown
# _severity_dedup word) is neither "a line ending in 'skipping'" nor a
# receiver problem; the generator refused the WHOLE tree in every mode.
FIELD_TYPE_HINT = (
    "Each 'ERROR (field type)' line above is a value of the wrong type or "
    "one the generator does not know, and the route generator refuses the "
    "whole tree in every mode until it is fixed. Fix the value as the ERROR "
    "line says (e.g. write group_by as a YAML list: group_by: [alertname]; "
    "a boolean as unquoted true / false), then re-run.")
# #2279: the generic routes advice ("check _routing … for invalid receiver
# types") points at a receiver that is fine; what is wrong is its NAME.
RECEIVER_COLLISION_ROUTES_HINT = (
    "Two sources above generate a receiver of the same name — usually a "
    "tenant whose id ends in -route-<n> or -override-<n> next to the tenant "
    "it extends. Rename one of the tenants (or remove one of the entries); "
    "the receivers themselves are not invalid.")
# The routes row is `generate-routes --validate`, Alertmanager's own reading
# included (#3689: its config loader, da-read). These replace the generic
# routes advice where it would send the operator to the wrong place.
AM_REJECTED_ROUTES_HINT = (
    "Alertmanager's config loader refused the "
    "config generated from this tree; its reason is not printed (it can "
    "quote config values) — run `generate-routes --validate "
    "--show-loader-reason` on this tree locally to see the line and field. "
    "Often it is a receiver value in _routing (e.g. a proxy URL, or a "
    "string where http_config wants a bool) that the generator accepts but "
    "Alertmanager cannot load. Fix that value, then re-run.")
AM_UNUSABLE_ROUTES_HINT = (
    "Alertmanager's reading could not be had (no da-read / da-crdecode, or "
    "one could not run) — that is this environment, not your config. Fix "
    "that, then re-run.")
ROUTES_INVARIANT_HINT = (
    "A platform invariant refused the generated config (see the lines "
    "above) — the same verdict `generate_alertmanager_routes.py --validate` "
    "gives. Fix the _routing / _severity_dedup input that produced it, then "
    "re-run.")
NOTHING_GENERATED_ROUTES_HINT = (
    "The tree has tenants, but they generate no route and no inhibit rule "
    "(for example no `_routing` anywhere and `_severity_dedup: disable`), and "
    "`generate-routes --validate` refuses a tree that renders nothing. Give "
    "a tenant a `_routing` receiver or leave severity dedup on, then re-run.")
# #3873: `--validate` reports the shared key verdict (ADR-038), so its
# `key verdict:` lines are this row's, and so is the advice on them
# (`_key_verdict_hint`): one of the hints below.
KEY_VERDICT_ROUTES_HINT = (
    "On each `key verdict:` line: fix a misspelt key, or remove a key nothing "
    "reads; a `disable` that switches nothing off usually sits on a misspelt "
    "key; a value_unread "
    "line is a key that is read with a value its reader does not read — "
    "fix the value, not the key; a dead_entry line is a schedule entry "
    "the windows before it (or, for the default, all of them) cover: "
    "remove it or reorder the windows; a schedule_unread line is a "
    "reserved key written with a window or expiry its readers ignore — "
    "only its default is read: remove the window or expiry; a line on a "
    "`version` label or a renamed key is the exporter's and says what to "
    "change. `da-tools explain-keys --config-dir <dir> --tenant <name>` shows, per "
    "key, who reads it and why not. An error, or a threshold key no root "
    "platform file lets the verdict judge, fails the run (#3016).")
KEY_VERDICT_INCOMPLETE_HINT = (
    "No key was judged: the verdict needs da-guard (built into the image; in "
    "the repo `make da-guard-build`, or set `DA_GUARD_BINARY`). Fix what the "
    "`key verdict incomplete` line names and run again: a tree no verdict was "
    "given for does not pass (#3016).")
KEY_VERDICT_UNDECIDABLE_HINT = (
    "Whether a threshold key is read is judged against the root platform "
    "files (`_defaults.yaml` at the root of --config-dir); none was read. "
    "Point --config-dir at the conf.d root (a sharded team's directory alone "
    "has none: validate the assembled tree), or add the root `_defaults.yaml`, "
    "and run again (#3016).")
NO_DECLARED_DEFAULTS_HINT = (
    "This config declares no platform defaults at all, so every tenant key "
    "is reported as unread — that is the platform's side missing, not a "
    "typo in the tenant files. \u26d4 Do NOT remove the keys named above: "
    "they are the tenants' settings, served once the platform declares "
    "them. Add a `defaults:` block — create "
    "_defaults.yaml if the tree has none, or restore the block that "
    "was emptied.")

# #1556: the three hints below are shown when --policy / --rule-packs /
# --policy-dsl was supplied but is unusable. Without them the row inherits
# the generic per-check advice, which points at tenant YAML — the wrong place
# for a mistake that lives entirely in the operator's argv. Measured for
# --rule-packs: the row read "Validate rule pack YAML syntax … run
# rule-pack-split --check", about a tree the tool had never opened.
_RULE_PACKS_INPUT_HINT = (
    "The custom rule lint refused its input (exit 2): the line above is its "
    "own, and names --rule-packs as lint's `--rule-pack-dir` (--lint-policy as "
    "`--policy`). Point --rule-packs at a rule-packs/ directory that holds at "
    "least one tenant rule pack. ⛔ Dropping "
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

# `--policy` is judged before any row runs (#3797) and only the routes row
# judges under it (#3303). Dropping the flag lets the run pass by switching
# the allowlist off — hence the ⛔.
_POLICY_ROUTES_ROW_HINT = (
    "Point --policy at the deployment policy YAML (an `allowed_domains:` list; "
    "the file generate-routes --policy takes; see docs/cli-reference.md § "
    "validate-config) — the custom-rule lint policy goes to --lint-policy. ⛔ "
    "Dropping the flag lets the run pass by switching the webhook domain "
    "allowlist off — that is the failure this check exists to stop.")

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

    ⛔ When nothing is declared, the shared key verdict reads every tenant
    threshold key as one no reader reads (``unread … (no_row)``), and the
    generic advice on the key verdict says to remove a key nothing
    reads — pointing at overrides that are legal and in force. Following it
    deletes a working config. (The ``routes`` row carries this hint since
    #3873, the ``unread_keys`` row before it, the ``schema`` row until
    ADR-038 §11.)

    Only the *advice* changes here. The findings are the shared verdict's;
    silencing them in this report would hide what the gates will refuse.

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
    when the check knows the generic advice would be wrong.
    """
    return {"check": name, "status": status, "details": details or [],
            "caller_error": caller_error,
            "unusable_files": unusable_files or [],
            "hint": hint}


# ============================================================
# Check 1: YAML Syntax
# ============================================================
def _unusable_consequence(bad: Path, listing) -> str:
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

    *listing* is the snapshot *bad* came from (#3206): what *bad* is (Go's
    `role` and class) and what lives under it are read off it.
    """
    # ⛔ A DIRECTORY SYMLINK FIRST (#1972). `iter_config_files(bad)` would
    # walk INTO the link — it is the root of that call, and `os.walk`
    # follows a symlinked root — and the branch below would then print
    # "the config file(s) INSIDE it ARE loaded", contradicting the reason
    # printed just before it. Neither this scan nor the exporter enters it.
    # The reason already says "NOT loaded", so the clause here is the remedy
    # only — repeating the loss read as a stutter (#1972 blind review N3).
    entry = unusable_entry(bad, listing)
    if entry is not None and entry.dir_link:
        return ("project those files as flat ConfigMap keys (no `/` in "
                "`items[].path`), or copy them into conf.d")
    # #3206: Go's structured verdict (role `unlistable_dir`, class
    # `directory`), never a prefix of the wording `unusable_reason` prints.
    # ⛔ #3755: a directory BELOW the root only. A root da-guard cannot list
    # never reaches a row: `--config-dir` is refused first (rc 2, naming the
    # flag, `declared_trees` in `_main`); reading it here was a finding, rc 1.
    if entry is not None and entry.unlistable_dir:
        return ("so THIS REPORT IS INCOMPLETE: fix the permissions and "
                "re-run before trusting it")
    inside = ([e for e in listing.config_files() if e.path.startswith(entry.path + "/")]
              if entry is not None and entry.cls == "directory" else [])
    if inside:
        return (f"but the {len(inside)} config file(s) INSIDE it ARE "
                f"loaded — by this scan and by threshold-exporter's "
                f"recursive scanner alike, so those declarations are live. "
                f"A `.yaml` name on a DIRECTORY is almost always an "
                f"interrupted `mkdir` or a bad merge: rename it")
    return "nothing in it is loaded, by this tool or by threshold-exporter"


def _has_config_file(config_dir: str) -> bool:
    """Whether the run's listing of *config_dir* holds any config file (Go's
    `role`). A listing that cannot be had is not "no config file": True, so
    the caller says "cannot be read" rather than "points at nothing"."""
    try:
        reader = _lib_parse_cache.reader_for(config_dir)
        return next(iter_config_files(config_dir, reader=reader), None) is not None
    except ParseTreeError:
        return True


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
    # ADR-036 step 4 (#2906): "loads" is `da-guard parse-tree`'s verdict —
    # the reading every routing row reads, so this row and theirs cannot
    # disagree about a file. A file it cannot read is named with its kind
    # (syntax_error, duplicate_key, invalid_utf8, top_level_not_mapping, …).
    # A file no reader reads (an unselected `_defaults` spelling, `not_read`)
    # has no content to judge; the routes row names it.
    reader = _lib_parse_cache.reader_for(config_dir)
    tree = reader.tree()
    root = Path(config_dir)
    # #3206: ONE listing for both halves and every reason below — the
    # entries of that same document (Go's `role`), not a second walk.
    listed = list_config_tree(config_dir, reader=reader)
    for fpath_p in listed.files:
        file_count += 1
        # Relative path, not bare filename: now that the scan is recursive,
        # two levels can hold the same `_defaults.yaml`.
        try:
            label = fpath_p.relative_to(root).as_posix()
        except ValueError:
            label = fpath_p.name
        if label in tree.unreadable:
            kind, message = tree.unreadable[label]
            errors.append(f"{label}: could not be read ({kind}): {' '.join(message.split())}"
                          + (" — every consumer reaches for keys on it and finds none, so "
                             "nothing in this file is loaded"
                             if kind == "top_level_not_mapping" else ""))
            unusable.append(label)
        elif label not in tree.selected and label not in tree.not_read:
            errors.append(f"{label}: da-guard parse-tree does not list this file — this "
                          f"tool's walk and the exporter's disagree about it")
            unusable.append(label)

    # #1469: entries NAMED like a config file that `iter_config_files`
    # correctly refuses to hand over — a directory called `beta.yaml`, a
    # broken symlink, a file with no read permission. This check is the one
    # place that enumerates the whole tree, so it is the only place that can
    # name them; before this they were invisible here while the routing
    # parser reported them, which is the two-readers-two-answers split #1469
    # is about. ⛔ The selection is NOT re-derived locally — that would make
    # a third implementation of "what is a config file".
    for bad in listed.unusable:
        try:
            label = bad.relative_to(Path(config_dir)).as_posix()
        except ValueError:
            label = bad.name
        if label in (".", ""):
            label = bad.name
        errors.append(f"{label}: {unusable_reason(bad, listed.listing)} — "
                      f"{_unusable_consequence(bad, listed.listing)}")
        unusable.append(label)

    if errors:
        return _make_result("yaml_syntax", FAIL, errors,
                            unusable_files=unusable)
    return _make_result("yaml_syntax", PASS,
                        [f"{file_count} files parsed successfully"])


# ============================================================
# Check 1b: Quoting of string fields (#2164)
# ============================================================
# The names are `_lib_io`'s: the one quoting selection
# (`confd_quoting_schemas`, #3647) looks the schemas up by them.
# #2245 / #2232 item 3: `_routing_profiles.yaml` — each profile `$ref`s the
# tenant `routing` definition, so the quoting check reaches profiles too.
from _lib_io import (  # noqa: E402
    CONFD_PLATFORM_SCHEMA as _PLATFORM_SCHEMA,
    CONFD_ROUTING_PROFILES_SCHEMA as _PROFILES_SCHEMA,
    CONFD_TENANT_SCHEMA as _TENANT_SCHEMA)


def _find_schema(name: str) -> Path | None:
    """Flat first (the da-tools image ships the schemas beside the tools —
    build.sh REPO_DATA_FILES), then `docs/schemas/` under THIS tree's root
    (`find_project_root`, #2845: never an enclosing project's)."""
    from _lib_compat import find_project_root
    here = Path(__file__).resolve().parent
    if (here / name).is_file():
        return here / name
    root = find_project_root(here)
    if root is None:
        return None
    candidate = root / "docs" / "schemas" / name
    return candidate if candidate.is_file() else None


def _quoting_scope(config_dir: str, schemas: "dict[str, object]"
                   ) -> list[tuple[Path, str, list[tuple[dict, str]]]]:
    """(file, label, [(schema, schema name), …]) for every file `yaml_quoting`
    reads, *schemas* being basename → parsed schema.

    #2695: `json_schema` leaves the misread scalars of exactly these files
    to `yaml_quoting`. Which schemas a file is walked with is
    `_lib_io.confd_quoting_schemas` — the function `check_confd_schema`
    calls too (#3647): were the two rows to pick differently, a value would
    be reported twice, or read as quoted by `json_schema` where
    `yaml_quoting` never walked, so nobody reports it. A file parse-tree
    lists as `not_read` is read by no reader and by no row here.
    """
    from _lib_io import confd_quoting_schemas
    reader = _lib_parse_cache.reader_for(config_dir)
    out: list[tuple[Path, str, list[tuple[dict, str]]]] = []
    for e in reader.listing().config_files():
        if e.not_read is not None:
            continue
        pairs = confd_quoting_schemas(e, schemas)
        if pairs:
            out.append((Path(config_dir) / e.path, e.path, pairs))
    return out


def check_yaml_quoting(config_dir: str) -> dict[str, object]:
    """A value in a string-typed field that yaml.v3 — every reader of conf.d
    (ADR-036 step 4, #2906) — reads as something else: `channel: 1e3` is the
    float 1000, `name: 2024-01-02` a timestamp (#2164). Tenant files are held
    to tenant-config.schema.json, `_defaults*` to platform-defaults.schema.json
    and `_routing_profiles.y(a)ml` to routing-profiles.schema.json (#2245;
    the same selection `check_confd_schema` makes); other `_*` files have
    no schema and are read only for a root one's `tenants:` block (below).
    Which values are not strings is yaml.v3's
    verdict (parse-tree's tag) and which fields are strings is the schema's —
    see `_lib_io.find_misread_scalars`; nothing is listed here. `channel: yes`
    is the string "yes" to yaml.v3 and is no finding.

    #3764: a number in a field that takes only numbers (`min_events`) must
    be written in plain decimal — `0x10`, `010`, `1e1` are rewritten on the
    way to the exporter's readers, which judge that field by its text
    (`_lib_io.find_nondecimal_numbers`). Same row, same FAIL.

    A file that cannot be read is skipped: `yaml_syntax` already names it.

    #2509: a YAML 1.1 boolean word (`yes` / `no` / `on` / `off`), quoted or
    not, in a field that takes a boolean is listed too, as WARN (the row is
    WARN when that is all it found): it is a string to yaml.v3, which the
    field refuses — `_lib_io.find_yaml11_bool_words`. An explicit
    `!!bool yes` makes the whole file unreadable to parse-tree
    (`decode_error`), which `yaml_syntax` reports. A root `_defaults*` file's
    `tenants:` block is held to the tenant schema too (#2509 review F3), a
    nested one is not (the exporter does not read it); so is that of every
    other root platform file the exporter reads (#2743), and each profile's
    `_custom_alerts` in such a file's `profiles:` block (#3647) — the one
    selection, `_quoting_scope` over `_lib_io.confd_quoting_schemas`.
    """
    from _lib_io import (find_misread_scalars, find_nondecimal_numbers,
                         find_yaml11_bool_words)
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
    reader = _lib_parse_cache.reader_for(config_dir)
    errors: list[str] = []
    warnings: list[str] = []
    checked = 0
    try:
        scope = _quoting_scope(config_dir, schemas)
    except KeyError:
        # #3647: a platform schema without `definitions.rootPlatformProfiles`
        # (one older than this tool) cannot judge a root `profiles:` block.
        return _make_result(
            "yaml_quoting", FAIL,
            [f"{_PLATFORM_SCHEMA} has no `definitions.rootPlatformProfiles` "
             f"— it is older than this tool; the quoting check cannot run"],
            caller_error=True)
    for fpath, label, pairs in scope:
        try:
            root = reader.node(fpath)
        except FileUnreadable:
            continue
        checked += 1
        for schema_, name_ in pairs:
            for hit in find_misread_scalars(root, schema_, schemas, name_):
                errors.append(f"{label}: {hit.message()}")
            for num in find_nondecimal_numbers(root, schema_, schemas, name_):
                errors.append(f"{label}: {num.message()}")
            for word in find_yaml11_bool_words(root, schema_, schemas, name_):
                warnings.append(f"WARN: {label}: {word.message()}")
    # #2509 blind review 5: each line carries its own remedy; the row's hint
    # only points back at them (a per-kind hint contradicted the lines).
    if errors:
        return _make_result("yaml_quoting", FAIL, errors + warnings)
    if warnings:
        return _make_result("yaml_quoting", WARN, warnings)
    return _make_result(
        "yaml_quoting", PASS,
        [f"{checked} files checked: no value in a string field "
         f"is read by yaml.v3 as a non-string, and every number in a "
         f"number field is written in plain decimal"])


# ============================================================
# Check 1c: JSON Schema (#2695)
# ============================================================
JSON_SCHEMA_NOT_CHECKED_HINT = (
    "Run validate-config where the Python package jsonschema is installed "
    "and check_confd_schema.py is present (the da-tools image, or a "
    "repository checkout with jsonschema installed) so this row can check "
    "conf.d against its JSON Schemas — without them, a value of the "
    "wrong shape (e.g. `group_by: alertname` where a list is required) is "
    "not detected here.")


def check_json_schema(config_dir: str) -> dict[str, object]:
    """conf.d against its JSON Schemas — the verdict of
    `scripts/tools/lint/check_confd_schema.py`, called, not re-implemented
    (#2695): before this the only row with "schema" in its name was the
    route generator's key check, and a tree that lint rejects read
    `[PASS] schema` here.

    A misread scalar (`mysql_connection_usage_percent: 70`) is `yaml_quoting`'s finding
    and is not reported again here: `validate_dir` is told which files
    that row reads (`_quoting_scope`) and validates them as if every such
    value had been quoted, as that row asks — so the value is no type
    error here and cannot mask another violation, while one that is wrong
    even quoted (`group_wait: 30` → `"30"`, no unit) is still reported.

    Without jsonschema or without the lint module beside this tool (a
    checkout whose Python lacks the package; the da-tools image ships both,
    #2744), the row is WARN and says it did not check — not a caller error:
    every other row still gave its verdict.
    """
    schemas: dict[str, dict] = {}
    for name in (_TENANT_SCHEMA, _PLATFORM_SCHEMA, _PROFILES_SCHEMA):
        path = _find_schema(name)
        if path is None:
            return _make_result(
                "json_schema", FAIL,
                [f"{name} not found beside this tool or under docs/schemas/ "
                 f"— the JSON Schema check cannot run"], caller_error=True)
        with open(path, encoding="utf-8") as fh:
            schemas[name] = json.load(fh)
    lint_dir = str(Path(__file__).resolve().parent.parent / "lint")
    if os.path.isdir(lint_dir) and lint_dir not in sys.path:
        sys.path.append(lint_dir)
    try:
        import jsonschema
        import check_confd_schema as ccs
    except ImportError as exc:
        missing = getattr(exc, "name", None) or str(exc)
        return _make_result(
            "json_schema", WARN,
            [f"Not checked against the JSON Schema: {missing} could not be "
             f"imported, so this row gave no verdict on the shape of any "
             f"value"],
            hint=JSON_SCHEMA_NOT_CHECKED_HINT)
    tenant = schemas[_TENANT_SCHEMA]
    platform = schemas[_PLATFORM_SCHEMA]
    profiles = schemas[_PROFILES_SCHEMA]
    try:
        ccs.checked_schema_registry(tenant, platform, profiles)
        ccs.require_root_profiles_definition(platform)
        not_read: list[str] = []
        checked, violations, skipped = ccs.validate_dir(
            config_dir, tenant, jsonschema, platform, profiles,
            quoting_reported_for=frozenset(
                label for _p, label, _s in _quoting_scope(config_dir, schemas)),
            not_read=not_read)
    except ccs.UnresolvableSchemaRef as exc:
        return _make_result("json_schema", FAIL, [str(exc)],
                            caller_error=True)
    except ccs._CallerError as exc:
        # A file that does not parse: `yaml_syntax` names it, and `main()`
        # turns this caller error into exit 1 because of that.
        return _make_result(
            "json_schema", FAIL,
            [f"could not read the config: {exc}",
             "No file was checked against the JSON Schema."],
            caller_error=True)
    errors = [v[len("ERROR: "):] if v.startswith("ERROR: ") else v
              for v in violations]
    if errors:
        return _make_result("json_schema", FAIL, errors)
    # #2743: a root one's `tenants:` block is checked; its own shape is not.
    note = (f"; {len(skipped)} other `_` file(s) have their own shape, "
            f"not checked here" if skipped else "")
    # #2906 review: a file no reader reads (parse-tree's `not_read`) is not
    # checked; said here, the `routes` row names why.
    note += (f"; not checked, no reader reads: {', '.join(not_read)}" if not_read else "")
    return _make_result(
        "json_schema", PASS,
        [f"{checked} document(s) valid against tenant-config / "
         f"platform-defaults / routing-profiles JSON Schema{note}"])


# ============================================================
# Check 3: Route validation
# ============================================================
def check_routes(config_dir: str, policy: object = None,
                 strict: bool = False, verdict_out: "list | None" = None) -> dict[str, object]:
    """`generate-routes --validate` on the tree, as a row.

    The generator's `validate_tree` is the whole judgement: its exit code is
    the row's status (0 → PASS, or WARN when it reported a warning; 1 → FAIL;
    2 → FAIL as a caller error) and what it printed is the row's details.
    This function decides only which advice line goes with a FAIL or a WARN.
    *policy*: the deployment policy ``main`` resolved (``resolve_policy``),
    None when ``--policy`` was not given.
    *verdict_out*, given, receives the shared key verdict the run gave, or the
    exception that kept it from being given (`ValidateOutcome.key_verdict`),
    so the `no_alert` row reads the same one instead of judging the tree again.
    """
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import generate_alertmanager_routes as gen

    printed = io.StringIO()
    with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
        outcome = gen.validate_tree(config_dir, strict=strict, policy=policy)
    details = [ln.rstrip() for ln in printed.getvalue().splitlines() if ln.strip()]
    if verdict_out is not None and outcome.key_verdict is not None:
        verdict_out.append(outcome.key_verdict)

    findings = [gen.as_finding(f) for f in outcome.findings]
    key_hint = _key_verdict_hint(gen, config_dir, findings)
    if outcome.exit_code == EXIT_OK:
        warned = [f for f in findings if f.severity != "note"]
        if not warned:
            return _make_result("routes", PASS, details)
        others = [f for f in warned if not f.kind.startswith("key_")]
        hint = ("" if not others else POLICY_ONLY_WARN_HINT
                if all(f.lstrip().startswith(POLICY_WARN_PREFIX) for f in others) else None)
        return _with_key_hint(_make_result("routes", WARN, details), hint, key_hint)
    hint = _routes_fail_hint(gen, outcome, findings, strict)
    row = _make_result("routes", FAIL, details,
                       caller_error=outcome.exit_code != EXIT_VIOLATION)
    if hint == POLICY_REFUSAL_REMEDY:
        row["hint_docs"] = "docs/cli-reference.md#validate-config"
    return _with_key_hint(row, hint, key_hint)


def _key_verdict_hint(gen, config_dir: str, findings: list) -> str | None:
    """The advice on the shared key verdict's lines in a routes row (#3873),
    by their `kind`; None when no such line is above a notice."""
    key_lines = [f for f in findings if f.kind.startswith("key_")]
    if all(f.severity == "note" for f in key_lines):
        return None
    kinds = {f.kind for f in key_lines}
    if "key_verdict_incomplete" in kinds:
        return KEY_VERDICT_INCOMPLETE_HINT
    found = kinds & set(gen._KEY_VERDICT_KINDS.values())
    if not found:
        # No key was found unread (an exporter key check is no finding):
        # the advice is about the root file when it was missing.
        return KEY_VERDICT_UNDECIDABLE_HINT if "key_undecidable" in kinds else KEY_VERDICT_ROUTES_HINT
    # ⛔ A platform declaring nothing: every key reads as unread, and
    # "remove a key nothing reads" would delete a working config.
    return _no_declared_defaults_hint(config_dir) or KEY_VERDICT_ROUTES_HINT


def _with_key_hint(row: dict[str, object], hint: str | None,
                   key_hint: str | None) -> dict[str, object]:
    """*row* with its advice: *hint* for the lines that are not the key
    verdict's ("" when none needs any, None for the generic routes line),
    then *key_hint* for the key verdict's."""
    if key_hint is None:
        row["hint"] = hint or None
    elif hint == "":
        row["hint"] = key_hint
        row["hint_docs"] = "docs/cli-reference.md#validate-config"
    else:
        row["hint"] = f"{hint or _CHECK_HINTS['routes'][0]} {key_hint}"
    return row


def _routes_fail_hint(gen, outcome, findings: list, strict: bool) -> str | None:
    """The advice for a routes FAIL where the generic line would point at the
    wrong thing, by what blocked the run: the findings in force (their
    `kind` / prefix, never the message text), else the verdict on the
    generated config. None: the generic routes advice; "": only the shared
    key verdict blocked (`_key_verdict_hint` advises on it)."""
    in_force = gen.in_force_blocks(validate=True, strict=strict)
    in_force_lines = [f for f in findings
                      if f.blocks in in_force and f.kind != "refusal_summary"]
    # The shared key verdict's lines have their own advice
    # (`_key_verdict_hint`): this is for the rest, when there is any.
    blocking = [f for f in in_force_lines if not f.kind.startswith("key_")]
    if in_force_lines and not blocking:
        return ""
    if blocking:
        unusable_policy = [f for f in blocking if f.kind == "domain_policy_unusable"]
        # #3284: the generator refused the whole tree on field-type lines.
        if all(is_field_type_error(f) for f in blocking) and len(
                unusable_policy) < len(blocking):
            return FIELD_TYPE_HINT
        if all(f.lstrip().startswith(gen.POLICY_ERROR_PREFIX) or f in unusable_policy
               for f in blocking):
            return POLICY_ONLY_HINT
        # #2279: every blocking line is a duplicate receiver name.
        if all(gen.is_receiver_name_collision(f) for f in blocking):
            return RECEIVER_COLLISION_ROUTES_HINT
        # #3303: every one is a `--policy` allowed_domains refusal.
        if all(gen.is_policy_refusal(f) for f in blocking):
            return POLICY_REFUSAL_REMEDY
        return None
    verdict = outcome.generated
    if verdict is None:
        return (NOTHING_GENERATED_ROUTES_HINT
                if outcome.exit_code == EXIT_VIOLATION else None)
    if verdict.assembly_error is not None:
        return ROUTES_INVARIANT_HINT
    am = verdict.alertmanager
    if am is not None and am.status == gen.AM_REJECTED:
        return AM_REJECTED_ROUTES_HINT
    if am is not None and am.status == gen.AM_UNUSABLE:
        return AM_UNUSABLE_ROUTES_HINT
    return None


# ============================================================
# Check 5: Custom rule linting
# ============================================================
def check_custom_rules(
    rule_packs_dir: str | None, lint_policy_file: str | None = None
) -> dict[str, object]:
    """Run lint_custom_rules.py on rule packs directory.

    ⛔ #1556, the shape the removed `policy` row had (#3303): ``--rule-packs``
    pointing at a path
    that is not a directory used to produce the same ``PASS … skipped`` row as
    omitting it, so a typo or a renamed directory silently removed the custom
    rule lint from the run.

    ⛔ #3755: this tool does not judge or list ``--rule-packs``: the lint
    subprocess is its one reader (``--rule-pack-dir``, declared there with
    ``_lib_input.add_input``). Judging it here as well was the double
    judgement #3721 forbids. Lint's exit 2 — the directory missing,
    unlistable or holding no tenant rule pack, an undeclared YAML beside the
    packs, an unusable ``--policy``, a missing reader — is a caller error,
    and this row says so (``caller_error``, the run's rc 2, the report kept),
    carrying lint's own line that names the directory. It was folded into a
    finding, rc 1.
    """
    # ⛔ `is None`, not `not rule_packs_dir`. An empty string is SUPPLIED — it is
    # what an unset shell variable expands to — and the falsy test routed it
    # into the skipped branch, removing the custom rule lint from the run at
    # exit 0. The flag's argparse default is None, so nothing else reaches this
    # branch. Same split as #1616's --base-config.
    if rule_packs_dir is None:
        return _make_result("custom_rules", PASS,
                            ["No rule-packs dir — skipped"])
    # #3426: the rule packs are chosen by the one declaration of what in a
    # rule-packs directory is a rule pack (`_lib_consumer_read.rule_pack_files`
    # behind `--rule-pack-dir`), never by a list of `--exclude` of our own.
    cmd = [sys.executable, str(_THIS_DIR / "lint_custom_rules.py"),
           "--rule-pack-dir", rule_packs_dir, "--ci"]
    # #1556: pass the value through even when it is not a file. Dropping it
    # here made `validate-config --policy <typo>` lint with the built-in
    # policy while reporting success; lint_custom_rules now exits 2 on it.
    # #3797: the lint policy is `--lint-policy`, lint's own input (lint
    # judges it); the deployment `--policy` is never handed to lint.
    if lint_policy_file is not None:   # ⛔ not truthiness: "" is supplied (#1616)
        cmd.extend(["--policy", lint_policy_file])

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

        if result.returncode == EXIT_CALLER_ERROR and any(
                l.startswith(CRASH_LINE_PREFIX) for l in lines):
            # #3755 review: a crash exits 2 as a refusal does; it is no
            # verdict on --rule-packs — never sent there to be fixed.
            row = _make_result(
                "custom_rules", FAIL,
                ["lint_custom_rules crashed (an internal error, not your input):",
                 *lines],
                caller_error=True,
                hint="This is a defect in lint_custom_rules: report it with the lines above.")
            row["reads_config_dir"] = False
            return row
        if result.returncode == EXIT_CALLER_ERROR:
            # #3755: lint could not lint — its refusal is the caller's
            # (argv or environment), never a finding about the rule packs or
            # the conf.d: `_argv_error_row` keeps the run at rc 2 even when
            # the conf.d has an unreadable file (`reads_config_dir`, as
            # `_argv_error_row` sets it).
            row = _make_result(
                "custom_rules", FAIL,
                ["lint_custom_rules refused its input (exit 2):",
                 *(lines or ["(no output)"]),
                 "  ⛔ Do not drop the flag to clear this — that removes the "
                 "custom rule lint from the run, which is what this error stops."],
                caller_error=True, hint=_RULE_PACKS_INPUT_HINT)
            row["reads_config_dir"] = False
            return row
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
    "Set $DA_GUARD_BINARY to a da-guard binary, or, when running this script "
    "directly, put da-guard on $PATH (the da-tools image ships it as "
    "/usr/local/bin/da-guard; in a checkout of this repo, `make da-guard-build` "
    "builds it to .build/da-guard). `make validate-config` never looks at "
    "$PATH: it uses $DA_GUARD_BINARY, or builds .build/da-guard when that is "
    "unset. Then re-run.")
_PROFILES_NO_DA_GUARD_HINT = _DA_GUARD_SOURCES + " No _profile reference was checked."
# ADR-036 step 4 (#2906): every row reads conf.d through `da-guard parse-tree`.
_PARSE_TREE_HINT = _DA_GUARD_SOURCES + " No row was run: every row reads the conf.d through it."
# Shared by every row that reads through da-guard: a da-guard named as older
# than this tool (its output lacks a field this tool reads, e.g. effective's
# `skipped`, or it does not accept the subcommand and flags — the released
# v2.9.x has no `effective` at all) is the binary's fault, not the tree's.
_DA_GUARD_TOO_OLD_HINT = (
    "This da-guard is older than this tool: point $DA_GUARD_BINARY at a current "
    "one, rebuild it (in a checkout of this repo, `make da-guard-build`) or "
    "upgrade the da-tools image, then re-run. The config tree was not checked; "
    "do not edit it for this.")
# The same for a da-guard that cannot be run or does not answer as da-guard
# does (not executable, an exit code it never uses, output that is not its
# JSON) (#2725): before, all of it was advice to repair the tree.
_DA_GUARD_BROKEN_HINT = (
    "The da-guard binary itself failed, as the lines above say: point "
    "$DA_GUARD_BINARY at a working da-guard (executable, built from this repo: "
    "`make da-guard-build`) or use the da-tools image, then re-run. The config "
    "tree was not checked; do not edit it for this.")


def _parse_tree_failure_row(check: str, exc: Exception,
                            no_da_guard_hint: str) -> dict[str, object]:
    """The FAIL row of a check whose conf.d read through `da-guard
    parse-tree` (ADR-036 step 4, #2906) failed: no da-guard (`BinaryMissing`),
    one older than this tool, or one that cannot be run or answers as no
    da-guard does. Always a caller error — the tree was not read."""
    from _lib_parse_tree import BinaryMissing
    text = " ".join(str(exc).split())
    if isinstance(exc, BinaryMissing):
        hint = no_da_guard_hint
    elif "older than this tool" in text:
        hint = _DA_GUARD_TOO_OLD_HINT
    else:
        hint = _DA_GUARD_BROKEN_HINT
    return _make_result(check, FAIL,
                        ["the conf.d cannot be read through `da-guard parse-tree` (ADR-036):",
                         text], caller_error=True, hint=hint)


def _tenant_load_failure_row(check: str, exc: Exception, config_dir: str,
                             hints: tuple[str, str, str]) -> dict[str, object]:
    """The FAIL row of a check that reads the tenants through da-guard
    (`_lib_tenant_values`) and could not (#2115 0-B): da-guard's own words
    below one line, and the hint for the cause — `hints` is (tree
    unreadable, no config file, no da-guard). A caller error: da-guard
    missing, failing itself (`binary_fault`: it cannot be run, does not
    answer as da-guard does, or is older than this tool, #2725), and a
    --config-dir with no config file at all (#2725); everything else is the
    tree's."""
    unreadable_hint, no_config_hint, no_da_guard_hint = hints
    buf = io.StringIO()
    print_load_error(exc, buf)
    caller_error = True
    if isinstance(exc, DaGuardNotFoundError):
        hint = no_da_guard_hint
    elif isinstance(exc, DaGuardError) and exc.stale:
        hint = _DA_GUARD_TOO_OLD_HINT
    elif isinstance(exc, DaGuardError) and exc.broken:
        hint = _DA_GUARD_BROKEN_HINT
    elif isinstance(exc, ParseFailedError):
        # da-guard named the paths it dropped or could not read (its
        # `parse_failed` / `unreadable`) — a dangling symlink included.
        hint, caller_error = unreadable_hint, False
    elif not _has_config_file(config_dir):
        hint = no_config_hint   # no path at all: the argv points at nothing
    else:
        hint, caller_error = unreadable_hint, False
    return _make_result(check, FAIL,
                        ["the tenants cannot be read as threshold-exporter reads them:",
                         # "\n" only: `splitlines` also cuts at U+2028 etc.,
                         # which can sit in a file name (_lib_tenant_values).
                         *(ln for ln in buf.getvalue().split("\n") if ln)],
                        caller_error=caller_error, hint=hint)


# #2740: one `da-guard effective` run per validate-config run, shared by the
# rows that read it (`yaml_quoting`, `profiles`). `main()`
# opens it for its rows and closes it after them; a check called on its own
# (None) runs da-guard itself, so a test that edits the tree between calls
# never reads a stale verdict. Key: the --config-dir string as passed.
_effective_runs: dict[str, tuple[object, BaseException | None]] | None = None


def _effective_tree(config_dir: str):
    """`load_effective_tree(config_dir)`, from the shared run when `main()`
    opened one — its result or its exception (`DaGuardNotFoundError`,
    `DaGuardError`, `ParseFailedError`), raised again for each row."""
    runs = _effective_runs
    if runs is None:
        return load_effective_tree(config_dir)
    if config_dir not in runs:
        try:
            runs[config_dir] = (load_effective_tree(config_dir), None)
        except (DaGuardNotFoundError, DaGuardError, ParseFailedError) as exc:
            runs[config_dir] = (None, exc)
    tree, exc = runs[config_dir]
    if exc is not None:
        raise exc
    return tree


# #2914: the profiles row's FAIL for a routing-layer key in a threshold profile.
_PROFILE_ROUTING_KEY_HINT = (
    "A threshold profile (`profiles:`) may not carry `_severity_dedup` or any "
    "`_routing*` key: the route generator never reads them there (each line "
    "above says where the key belongs). `da-tools generate-routes --config-dir "
    "<conf.d> --validate --strict` names each as routing_in_unread_location.")

# #2904: the route generator could not read the tree for the row's #2914 check.
_PROFILE_ROUTING_UNREAD_HINT = (
    "The route generator could not read this tree (the line above says why), "
    "so whether a threshold profile carries a routing key is NOT checked; run "
    "`da-tools generate-routes --validate --strict --config-dir <dir>` to see it.")


class ProfileRoutingUnjudged(RuntimeError):
    """The route generator could not read the tree for the #2914 check."""


def _profile_routing_findings(config_dir: str) -> list[tuple[str, str]]:
    """(field, message) of each `routing_in_unread_location` the route
    generator records inside a threshold profile (field
    `<file>:profiles.<profile>.<key>`) — `_grar_parse._unread_routing`, read
    through the generator's own `_parse_config_files`, the call this module
    already makes (`_declared_defaults_hint`). #2914 / #2904: the verdict is
    the generator's, not this row's and no longer da-guard's (it judges no
    routing since ADR-036 §9 step 2). Every root `_` file's `profiles:`, bound
    or not, in a tree with no tenant too. The field is compared whole, never
    split: a profile name and a key may both hold a `.`.

    ⛔ Fail-closed: a generator that cannot read the tree (it exits, or
    raises) is `ProfileRoutingUnjudged`, never "no finding"."""
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import generate_alertmanager_routes as gen
    try:
        parsed = gen._parse_config_files(config_dir)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — named in the row
        raise ProfileRoutingUnjudged(f"{type(exc).__name__}: {exc}") from exc
    return [(f"{fname}:{fld}", msg)
            for kind, fname, fld, msg in parsed.get("routing_tree_problems", [])
            if kind == "routing_in_unread_location" and fld.startswith("profiles.")]


def check_profiles(config_dir: str) -> dict[str, object]:
    """Validate tenant _profile references and profile structure.

    Checks:
      - Each tenant's `_profile` binds the profile it names (#2115 0-B: the
        exporter's answer, `da-guard effective`'s `profile`, not a Python
        reading of the files)
      - No threshold profile carries a routing-layer key (#2914): FAIL. The
        verdict is the route generator's own `routing_in_unread_location`
        inside a profile (#2904: da-guard judges no routing any more), so it
        covers every root `_` file's `profiles:`, not only `_profiles.yaml`,
        and profiles no tenant elects — in a tree with no tenant at all too.
        A generator that cannot read the tree fails the row (fail-closed).
      - No key-name judgment: whether a reader reads a `_` key in a profile
        is the `routes` row's (the shared verdict, from the registry's
        `profile` layer, bound or not; #3042)
      - Profile values are valid types (numeric, string, dict for scheduled)
      - Profiles have at least one metric key

    One da-guard run, `effective`: its `skipped` names the files with no
    tenant, so served-values (/metrics) is not needed — a tree whose /metrics
    da-guard refuses (e.g. `X_critical` written at the root and in a tenant,
    #2115 F2) is not this row's business. A file the load drops is named
    with the exporter's reason from effective's own stderr (#2115: it logs
    the load as served-values does), so served-values is not run for it.

    The tenants are the exporter's: the whole tree (a tenant in a
    sub-directory included — this row has read recursively since PR #1343),
    a file with no `tenants:` mapping is not one (it is named in the row's
    details, from effective's `skipped`, #2115 R3; the row's status does not
    change for it), and the ids are the exporter's text.
    A `_profile` is read as written plus inherited (#2115 (c)): one that
    binds no profile is reported — as an unknown profile when the tenant's
    own entry names it, and as binding nothing when it comes from a defaults
    file, where the exporter does not read it (the root's: a subtree
    `_defaults.yaml` does not carry `_profile`, so effective shows none from
    it and da-guard's `subtree_default_reserved_key` names it, #3341). A file the exporter's load
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
        tree = _effective_tree(config_dir)
    except (DaGuardNotFoundError, DaGuardError, ParseFailedError) as exc:
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
    profiles_raw = load_confd_file(config_dir, profiles_path, default={})
    profiles = profiles_raw.get("profiles", {}) if isinstance(profiles_raw, dict) else {}

    warnings = []
    profile_ref_count = 0

    # ── #2914: routing-layer keys in a threshold profile (FAIL) ──
    # The route generator's own `routing_in_unread_location` inside a profile
    # (#2904): every root platform file's `profiles:`, bound or not.
    try:
        in_profiles = _profile_routing_findings(config_dir)
    except ProfileRoutingUnjudged as exc:
        return _make_result("profiles", FAIL,
                            [f"the route generator cannot read this tree: {exc}"],
                            hint=_PROFILE_ROUTING_UNREAD_HINT)
    errors = [msg for _field, msg in in_profiles]

    # ── Profile structure validation ──
    for p_name, p_data in profiles.items():
        if not isinstance(p_data, dict):
            warnings.append(f"profile={p_name}: value is not a mapping (got {type(p_data).__name__})")
            continue

        if not p_data:
            warnings.append(f"profile={p_name}: empty profile (no metric keys)")
            continue
        # ⛔ No key-name judgment here (#3042, ADR-038 decision 3): whether a
        # `_` key in a profile is read is the shared key verdict (`routes` row),
        # from the registry's layers — `_custom_alerts`, for one, is read in
        # a profile (ADR-024 §3).

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

    if errors:
        return _make_result("profiles", FAIL, errors + warnings + skipped_lines,
                            hint=_PROFILE_ROUTING_KEY_HINT)
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
_POLICY_DSL_INVALID_RULE_HINT = (
    "Fix the rules named above so the engine reads them as written (a rule it "
    "cannot read is no longer skipped, #2961: a skipped rule passes). No policy "
    "was evaluated.")
_POLICY_DSL_NO_CONFIG_FILE_HINT = (
    "There is no config file under --config-dir at all: point it at your conf.d "
    "tree, then re-run. No policy was evaluated.")
_POLICY_DSL_NO_DA_GUARD_HINT = _DA_GUARD_SOURCES + " No policy was evaluated."
_POLICY_DSL_ROUTING_REFUSED_HINT = (
    "A rule reads `_routing`, and the route generator refuses this tree for the "
    "reasons above (`da-tools generate-routes --config-dir <conf.d> --validate` "
    "shows the same): fix "
    "them, then re-run. No policy was evaluated.")


def pe_policy_file_document() -> object:
    """``--policy-dsl``'s declaration: evaluate-policy's ``--policy`` one
    (``policy_engine.POLICY_FILE_DOCUMENT``), the same file read the same way."""
    import policy_engine as pe  # noqa: PLC0415
    return pe.POLICY_FILE_DOCUMENT


class _Unread(str):
    """A --policy-dsl a library caller named and this row could not read:
    the refusal, said where the CLI's declaration would have refused it."""


def check_policy_dsl(config_dir: str, policy_dsl_file: str | None = None,
                     policy_dsl_text: str | None = None) -> dict[str, object]:
    """Evaluate declarative policies from _defaults.yaml _policies or standalone file.

    Loads policy rules from _defaults.yaml ``_policies`` section and/or
    a standalone policy DSL file, then evaluates against all tenant configs.
    *policy_dsl_text* is that file's content as ``main`` read it once
    (``--policy-dsl``, a declared document, #3797); None (a library caller)
    reads the file here, once — a file that cannot be read is the row's
    argv error, as the CLI's declaration refuses it.

    The row carries ``_policy_scope`` (``root_carrier``: whether --config-dir
    has a root defaults carrier — True, False, or ``"unreadable"`` for a path
    that exists but is not a file it can open, e.g. a dangling symlink;
    ``policy_dsl``: whether --policy-dsl was loaded, not merely given;
    ``evaluated``: whether tenants were evaluated) for ``_flag_flat_reads`` to
    word its line by, and only for it: it pops the key, so it never reaches
    the report (#2115 0-B, #2725).
    """
    scope: dict[str, object] = {"root_carrier": None, "evaluated": False,
                                "policy_dsl": False}
    if policy_dsl_file is not None and policy_dsl_text is None:
        try:
            policy_dsl_text = Path(policy_dsl_file).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            policy_dsl_text = _Unread(f"--policy-dsl: cannot read {policy_dsl_file!r}: {exc}")
    row = _policy_dsl_row(config_dir, policy_dsl_file, policy_dsl_text, scope)
    row["_policy_scope"] = scope
    return row


def _policy_dsl_row(config_dir: str, policy_dsl_file: str | None, policy_dsl_text: str | None,
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
    # The carrier read is the one parse-tree selects (#2906 review), not
    # `resolve_defaults_file`'s own selection: that one names the file to
    # create when there is none, and prints the skipped-carriers note. Both
    # ask the run's reader (#3206: its listing, no second walk), so both are
    # in the da-guard failure arm.
    try:
        reader = _lib_parse_cache.reader_for(config_dir)
        defaults_path = str(resolve_defaults_file(
            Path(config_dir), skipped_note=pe.POLICIES_SKIPPED_NOTE, reader=reader))
        chosen = reader.carrier(config_dir)
    except ParseTreeError as exc:
        return _parse_tree_failure_row("policy_dsl", exc, _POLICY_DSL_NO_DA_GUARD_HINT)
    if chosen is not None:
        defaults_path = str(chosen)
    # #2725: a carrier that is there but cannot be opened (a dangling
    # symlink) is not "no carrier" — that line told the operator to create one.
    # #3206: both answers are the reader's listing — a carrier Go selects is a
    # config file (read, failing loudly), an entry at the carrier's path that
    # is none (a dangling link) is "unreadable"; no `isfile` / `lexists` here.
    if chosen is not None:
        scope["root_carrier"] = True
        # #2961: the customer's tree — a tree finding (FAIL, rc 1), as the
        # comment at the --policy-dsl read below says.
        try:
            rules.extend(pe.load_policies(defaults_path, config_dir))
        except ParseTreeError as exc:
            return _parse_tree_failure_row("policy_dsl", exc, _POLICY_DSL_NO_DA_GUARD_HINT)
        except pe.InvalidPolicyRules as exc:
            return _make_result("policy_dsl", FAIL,
                                [f"{os.path.basename(defaults_path)}: {p}" for p in exc.problems],
                                hint=_POLICY_DSL_INVALID_RULE_HINT)
    else:
        scope["root_carrier"] = "unreadable" if reader.entry(defaults_path) is not None else False

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
        # #3797: the path (and strict UTF-8) was judged, and the file read,
        # ONCE before any row ran (`_lib_input.document`, rc 2 naming the
        # flag); what follows reads *policy_dsl_text*, never the file again —
        # a second read (`load_policies` by path) read a file gone in between
        # as "no rules", rc 1, or crashed.
        if isinstance(policy_dsl_text, _Unread):
            return _argv_error_row("policy_dsl", str(policy_dsl_text), _POLICY_DSL_INPUT_HINT)
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
        # ADR-036 step 4 (#2906): yaml.v3, as every other read of this run —
        # `da-guard parse-tree`'s file mode (`pe.policy_text_data`).
        try:
            dsl_data = pe.policy_text_data(policy_dsl_text, str(policy_dsl_file))
        except (yaml.YAMLError, ParseTreeError) as exc:
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
        # ⚠️ An empty file, or an empty mapping (`{}`), still reaches "No
        # _policies defined — skipped" at exit 0; any other top-level key is
        # refused below (#2961). That is the CONTENT axis, which this PR does not close for
        # any of the three flags; see the NOT COVERED list in _grar_validate.
        # #2961: an unknown top-level key (`polices:`) read as "no rules" at
        # exit 0, and a rule the engine cannot read was skipped. Both are the
        # operator's argv, so both are argv errors (exit 2).
        unknown = (sorted(str(k) for k in dsl_data if k not in pe.POLICY_FILE_KEYS)
                   if isinstance(dsl_data, dict) else [])
        if isinstance(dsl_data, dict) and set(dsl_data) >= pe.POLICY_FILE_KEYS:
            return _argv_error_row(
                "policy_dsl",
                f"--policy-dsl: both `policies:` and `_policies:`; only one is read — keep one: "
                f"{policy_dsl_file!r}",
                _POLICY_DSL_INPUT_HINT)
        if unknown:
            return _argv_error_row(
                "policy_dsl",
                f"--policy-dsl: unknown top-level key(s) {unknown} (read: `policies:`): "
                f"{policy_dsl_file!r}",
                _POLICY_DSL_INPUT_HINT)
        try:
            rules.extend(pe.rules_from_policy_data(dsl_data, source=str(policy_dsl_file)))
        except pe.InvalidPolicyRules as exc:
            row = _argv_error_row(
                "policy_dsl",
                f"--policy-dsl: policy rules the engine cannot read as written in "
                f"{policy_dsl_file!r}:",
                _POLICY_DSL_INVALID_RULE_HINT)
            row["details"].extend(exc.problems)
            return row
        # Only now: a --policy-dsl that failed above supplied no rule (#2725).
        scope["policy_dsl"] = True

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

    try:
        # It asks da-guard for the #1231 spellings (#3556): the load's failure.
        result = pe.evaluate_policies(rules, tenant_configs)
    except (DaGuardNotFoundError, DaGuardError) as exc:
        return _tenant_load_failure_row(
            "policy_dsl", exc, config_dir,
            (_POLICY_DSL_TREE_UNREADABLE_HINT, _POLICY_DSL_NO_CONFIG_FILE_HINT,
             _POLICY_DSL_NO_DA_GUARD_HINT))
    scope["evaluated"] = True

    details = []
    for v in result.violations:
        icon = "ERROR" if v.severity == "error" else "WARN"
        details.append(f"[{icon}] {v.tenant}: {v.rule_name} — {v.message}")
    notes = _policy_dsl_threshold_notes(tenant_configs)

    if result.error_count > 0:
        details.append(f"{result.error_count} error(s), {result.warning_count} warning(s) "
                       f"across {result.tenants_evaluated} tenants")
        return _make_result("policy_dsl", FAIL, details + notes)
    if result.warning_count > 0:
        details.append(f"{result.warning_count} warning(s) "
                       f"across {result.tenants_evaluated} tenants")
        return _make_result("policy_dsl", WARN, details + notes)
    return _make_result("policy_dsl", PASS,
                        [f"{len(rules)} rules evaluated across "
                         f"{result.tenants_evaluated} tenants — all passed", *notes])


def _policy_dsl_threshold_notes(views: dict[str, dict[str, object]]) -> list[str]:
    """What the threshold rules had to compare (#2725), from the same
    `load_policy_inputs` views the rules read — da-guard's answer, no YAML
    read here. "all passed" alone read the same on a tree whose /metrics
    serves no threshold at all (no root `defaults:`), where no threshold
    rule had a number to compare. The verdict is unchanged: thresholds are
    what /metrics serves (#2115 (c))."""
    import policy_engine as pe
    served = sum(isinstance(v, pe.ServedThreshold) for view in views.values() for v in view.values())
    unserved = [f"{t}: {k}" for t, view in sorted(views.items())
                for k, v in sorted(view.items()) if isinstance(v, pe.Unserved)]
    notes = []
    if not served:
        notes.append("/metrics serves no threshold value for any tenant, so no rule "
                     "compared a threshold")
    if unserved:
        shown = ", ".join(printable_name(u) for u in unserved[:WARN_LIMIT])
        more = f" (+{len(unserved) - WARN_LIMIT} more)" if len(unserved) > WARN_LIMIT else ""
        notes.append(f"{len(unserved)} threshold value(s) are configured but not served by "
                     f"/metrics (da-guard served-values `unserved`), so only `required` "
                     f"rules see them: {shown}{more}")
    return notes


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
    both-spellings enumerator, and Go's class (``Entry.reserved``) is the
    ``_``-prefix gate the walker applies *before* it parses a ``tenants:``
    block at all.
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
# Check 9b: Tenant file name = declared tenant id (#3157)
# ============================================================
def check_tenant_file_name(config_dir: str) -> dict[str, object]:
    """A tenant file's name (stem) must be the one tenant id it declares.

    Two planes name a tenant from two different places: tenant-api takes the
    id from the file NAME (``confdname.TenantNamedBy``: `es.yaml` is tenant `es`),
    the exporter from the keys under ``tenants:``. When they differ the same
    tenant has one id on the API (RBAC grants, audit records, portal links)
    and another on ``/metrics``, and tenant-api treats the tenant as
    git-managed (``id_mismatch``), refusing every write to it. #3157 made
    "stem = declared id" a platform invariant: such a file is a FAIL here.

    A file that declares SEVERAL tenants breaks the invariant for all but at
    most one of them, and tenant-api git-manages every tenant in it
    (``tenant_count``, the same ``TenantFileManagement`` verdict), so it is a
    FAIL too, worded as "split it" rather than "rename it": no name fits.

    Selection is ``tenant_declarations``' — the scan ``tenant_uniqueness``
    runs, which mirrors the exporter's walker: recursive (a file in a
    sub-directory is checked against its own base name), ``_``-prefixed and
    ``.``-prefixed names skipped, both extension spellings in any case. A
    file that declares no tenant (no or an empty ``tenants:``) has nothing
    to compare and is not listed. The comparison is case-sensitive, as the
    stem is: ``Upper.YAML`` carries tenant ``Upper``, not ``upper``. The id a
    file name carries is Go's (the entry's ``tenant_named``, #3206), not a
    stem computed here.

    ⚠️ A declared id is the key's source text (``tenant_declarations``'
    notion), not the decoded value tenant-api compares; the two differ only
    for a tagged key such as ``? !!binary ...``, which tenant-api already
    git-manages as ``yaml_indirection``.
    """
    declared, unreadable = tenant_declarations(config_dir)
    listing = config_listing(config_dir)  # the same reader's one snapshot

    by_file: dict[str, list[str]] = {}
    for tenant_id, labels in declared.items():
        for label in labels:
            by_file.setdefault(label, []).append(str(tenant_id))

    # tenant-api enumerates the conf.d ROOT only (`ListTenantFiles`), so its
    # consequence is named for a root file; a file in a sub-directory is held
    # to the same invariant (the exporter reads it) with that said instead.
    nested_note = ("tenant-api reads only the conf.d root, but the invariant "
                   "holds at every level: the file keeps its tenant's id "
                   "wherever it is moved.")

    details: list[str] = []
    for label in sorted(by_file):
        ids = sorted(by_file[label])
        name = label.rsplit("/", 1)[-1]
        stem = listing.entry(label).tenant_named
        parent = label[: len(label) - len(name)]
        if len(ids) > 1:
            shown = ", ".join(f'"{printable_name(i)}"' for i in ids)
            files = ", ".join(
                f"{parent}{printable_name(i)}.yaml" for i in ids)
            why = (nested_note if parent else
                   "tenant-api treats every tenant of this file as "
                   "git-managed (tenant_count) and refuses writes to them.")
            details.append(
                f"{printable_name(label)} declares {len(ids)} tenants "
                f"({shown}): a tenant file declares exactly one tenant, the "
                f"one its file name names — split it into one file per "
                f"tenant ({files}). {why}")
        elif ids[0] != stem:
            why = (nested_note if parent else
                   f"tenant-api names this tenant \"{printable_name(stem)}\" "
                   f"(from the file name) and the exporter "
                   f"\"{printable_name(ids[0])}\" (from `tenants:`), so RBAC, "
                   f"audit records and portal links do not match its "
                   f"metrics, and tenant-api refuses writes to it "
                   f"(id_mismatch).")
            details.append(
                f"{printable_name(label)} declares tenant "
                f"\"{printable_name(ids[0])}\", but its file name says "
                f"\"{printable_name(stem)}\" — rename the file to "
                f"{parent}{printable_name(ids[0])}.yaml. {why}")

    if details:
        if unreadable:
            details.append(
                f"{len(unreadable)} file(s) could not be read and were not "
                f"examined, so there may be more: {', '.join(sorted(unreadable))}")
        return _make_result("tenant_file_name", FAIL, details)

    if unreadable:
        return _make_result("tenant_file_name", WARN, [
            f"every one of the {len(by_file)} tenant file(s) that could be "
            f"read is named after the tenant it declares, but "
            f"{len(unreadable)} file(s) could not be read and were not "
            f"examined: {', '.join(sorted(unreadable))} — this is a limit on "
            f"what was checked, not a clean result"],
            hint="Fix the unreadable file(s) named above (yaml_syntax says why) "
                 "and re-run.")

    return _make_result("tenant_file_name", PASS, [
        f"{len(by_file)} tenant file(s), each named after the one tenant it "
        f"declares"])


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
    (``_root_defaults_verdicts``) rejects it, None when it decodes.
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

    *key* and *raw* are one item of ``_root_defaults_verdicts`` (key
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
                f"drops ALL of it — the whole file, every platform threshold "
                f"included. Write the root "
                f"`defaults:` as `metric_name: <number>` lines.")
    shown = {"<mapping>": "a mapping", "<list>": "a list"}.get(
        raw, f"`{raw}`, which is not a number")
    return (f"{rel}: `defaults.{key}` is {shown} — the "
            f"root `defaults:` holds numbers only, so threshold-exporter "
            f"cannot decode this file and drops ALL of it: the whole file, "
            f"every platform threshold included, not just this key. Write a plain, "
            f"unquoted number.")


# ============================================================
# Check 13: da-guard's error findings (#3387)
# ============================================================
_GUARD_ERRORS_TREE_UNREADABLE_HINT = (
    "threshold-exporter cannot load this tree as the lines above say: repair "
    "or remove the file they name (or the shape da-guard refuses), then re-run. "
    "Any finding listed covers only the files the load kept.")
_GUARD_ERRORS_NO_CONFIG_FILE_HINT = (
    "There is no config file under --config-dir at all: point it at your conf.d "
    "tree, then re-run. da-guard judged nothing.")
_GUARD_ERRORS_NO_DA_GUARD_HINT = _DA_GUARD_SOURCES + " da-guard judged nothing."


def _guard_error_lines(findings) -> list[str]:
    """One line per da-guard error finding — its kind, field, tenant(s) and
    message as da-guard wrote them. Findings that differ only in the tenant
    (the same kind, field and message: one file's key that every tenant
    below it inherits) are one line listing the tenants, as the shared key
    verdict lists them."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for f in findings:
        if f.severity != GUARD_REPORT_SEVERITY_ERROR:
            continue
        tenants = groups.setdefault((f.kind, f.field, " ".join(f.message.split())), [])
        if f.tenant_id and f.tenant_id not in tenants:
            tenants.append(f.tenant_id)
    out = []
    for (kind, field, message), tenants in groups.items():
        where = f" `{field}`" if field else ""
        ts = sorted(tenants)
        who = (" — tenant(s) " + ", ".join(ts[:WARN_LIMIT])
               + (f" (+{len(ts) - WARN_LIMIT} more)" if len(ts) > WARN_LIMIT else "")
               if ts else "")
        out.append(f"error: {kind}{where}{who}: {message}")
    return out


def check_guard_errors(config_dir: str) -> dict[str, object]:
    """Every error finding of da-guard's own check (#3387): `da-guard
    --config-dir <dir> --format json`, its `report.findings` with
    `severity: error` — a FAIL whatever the kind, so the severity is
    da-guard's alone and an error kind added to it is one here too. Its
    warnings are not this row's.

    da-guard's exit code (`load_guard_report`): 0 clean; 1 error findings,
    a FAIL; 3 files the exporter's load drops or cannot read, a FAIL that
    names them, beside any error finding in the rest of the tree; 4 routing
    or domain policy in the tree, which da-guard does not judge — not a
    FAIL (the `routes` row judges them), its findings read as for 0. A
    da-guard that is missing, older than this tool or does not answer as
    da-guard does, and exit 2, are the load-failure row `profiles` uses
    (`_tenant_load_failure_row`).
    """
    try:
        rep = load_guard_report(config_dir)
    except (DaGuardNotFoundError, DaGuardError) as exc:
        return _tenant_load_failure_row(
            "guard_errors", exc, config_dir,
            (_GUARD_ERRORS_TREE_UNREADABLE_HINT, _GUARD_ERRORS_NO_CONFIG_FILE_HINT,
             _GUARD_ERRORS_NO_DA_GUARD_HINT))
    details = _guard_error_lines(rep.findings)
    if rep.parse_failed or rep.unreadable:
        dropped = [f"{p} (does not decode)" for p in rep.parse_failed] + [
            f"{u.file} ({u.reason})" for u in rep.unreadable]
        details.append(f"da-guard (exit {rep.returncode}): the exporter's load drops "
                       f"{len(dropped)} path(s): {', '.join(dropped)} — the findings cover "
                       "only the rest of the tree")
        # The exporter's reasons are in da-guard's stderr, as the load-failure
        # rows show them (`print_load_error`).
        details += [f"{DA_GUARD_PREFIX}{safe_label(ln)}" for ln in rep.stderr_lines]
        return _make_result("guard_errors", FAIL, details,
                            hint=_GUARD_ERRORS_TREE_UNREADABLE_HINT)
    if details:
        return _make_result("guard_errors", FAIL, details)
    warns = sum(f.severity != GUARD_REPORT_SEVERITY_ERROR for f in rep.findings)
    passed = ["no error finding from da-guard"
              + (f" ({warns} warning(s), not this row's)" if warns else "")]
    if rep.returncode == 4:
        passed.append(f"da-guard exit 4: {rep.undetermined} routing / domain-policy item(s) "
                      "da-guard does not judge — the `routes` row judges them")
    return _make_result("guard_errors", PASS, passed)


_NO_ALERT_HINT = (
    "Judged against the alert rules this platform ships "
    "(`metric_observed_map.yaml`, kept equal to the rule packs by "
    "`check_threshold_observed_map.py`): a key only your own Prometheus rules "
    "read is listed too, and can be left as it is. Otherwise the value "
    "reaches no alert: fix a misspelt key name, or remove the key. Never "
    "blocks (#3591).")


_NO_ALERT_NOT_CHECKED_HINT = (
    "Nothing was judged, so nothing is known to be unused: fix what the line "
    "above names (a root `_defaults.yaml` at the root of --config-dir, a "
    "working da-guard) and run again. Never blocks (#3591).")


def check_no_alert(config_dir: str, verdict: "key_verdict.TreeVerdict | None" = None,
                   judge: bool = True) -> dict[str, object]:
    """Threshold keys /metrics serves that no shipped alert compares (ADR-038
    decision 10, #3591): a typo the exporter still serves
    (`mysql_connectoins` in the root `defaults:`), a `<key>_critical` tier
    whose rule pack has no critical tier.

    The served keys are the shared verdict's (*verdict*: the `routes`
    row's, or judged here): every key the exporter reads, with the file its
    value comes from, that /metrics serves a row for in some part of the UTC
    day; the severities each is served at over the day are served-values'
    `--schedules-only` (#3883). Nothing here is read at one instant: the
    time of day the check runs at changes nothing — `expires:` alone is read
    at the run's clock, as the verdict reads it. A part of the day /metrics
    cannot be gathered in serves nothing, so no severity is read there.
    Whether an alert compares a key is the observed-map's
    answer (`_observed_map_lib.lookup`), asked for the key in the exporter's
    canonical spelling (asked of da-guard), the dimension part dropped — and
    for a key served at critical by its value (`"95:critical"`), for its
    `<key>_critical` tier, which is what a pack with a critical tier compares.
    No second key → alert table. A key whose base the map defers
    (KNOWN_DEFERRED) is not listed.

    Advisory only: WARN or PASS, never FAIL — a reader outside the shipped
    rule packs (a customer's own rules) is not seen. A tree the verdict could
    not judge (no root platform file), or a verdict, served reading or map
    that cannot be read, is a WARN saying the keys were not checked.

    *judge* False (`main`, after the `routes` row tried and gave no verdict):
    no verdict is judged here — the reason is that row's, already reported —
    and the row says it was not checked.
    """
    import _observed_map_lib  # ops/: the observed-map the recommender reads

    def not_checked(why: str) -> dict[str, object]:
        return _make_result("no_alert", WARN, [
            f"not checked: whether a shipped alert compares each served key was not judged ({why})"],
            hint=_NO_ALERT_NOT_CHECKED_HINT)
    if verdict is None and not judge:
        return not_checked("the routes row gave no key verdict; its line says why")
    try:
        if verdict is None:
            verdict = key_verdict.judge_tree(config_dir)
        if verdict.undecidable:
            return not_checked("no root platform file was read, so the threshold keys were not judged")
        observed = _observed_map_lib.load_observed_map()
        days = load_served_schedules(config_dir, partial=True).tenants
        # #3883: (tenant, key) -> every severity /metrics serves the key at,
        # in any part of the day (the key verdict carries no value read at
        # one instant since #3835).
        severities = {(t, k): {seg.severity for seg in sched.segments if seg.value is not None}
                      for t, keys in days.items() for k, sched in keys.items()}
        read = [v for vs in verdict.keys.values() for v in vs
                if v.verdict == key_verdict.READ and "exporter" in v.read_by and not v.key.startswith("_")]
        spell = tv_key_spellings(sorted({v.key.partition("{")[0] for v in read}))

        def served_as(v: "key_verdict.KeyVerdict") -> str:
            # The schedules name a key as served-values does: the base in the
            # exporter's canonical #1231 spelling (asked of da-guard, the
            # `spell` above), the dimension part as written; a verdict row
            # names it as the tenant wrote it (`mysql_cpu` vs
            # `mysql_threads_running`).
            base, sep, rest = v.key.partition("{")
            return spell.canonical(base) + sep + rest
        sevs = {(v.tenant, v.key): severities.get((v.tenant, served_as(v)), set()) for v in read}
        served = [v for v in read if sevs[(v.tenant, v.key)]]
    except (DaGuardError, DaGuardNotFoundError, _observed_map_lib.ObservedMapError, OSError,
            YamlFileError) as exc:
        return not_checked(f"{type(exc).__name__}: {' '.join(str(exc).split())}")
    # ONE LINE PER KEY (#3591): what is judged is the key — no shipped alert
    # compares it, whatever file writes it — so the line is the key's in the
    # exporter's canonical spelling, the dimension part dropped (the lookup's
    # own unit; label values are the tenants' and unbounded), with its
    # critical tier apart. The lines are bounded by the platform's declared
    # keys, twice. (canonical base, served at critical by its value) ->
    # (files, tenants)
    found: dict[tuple[str, bool], tuple[set[str], set[str]]] = {}
    for v in served:
        base = spell.canonical(v.key.partition("{")[0])
        if base in _observed_map_lib.KNOWN_DEFERRED:
            continue
        for sev in sorted(sevs[(v.tenant, v.key)], key=str):
            key = base
            if sev == "critical" and not base.endswith("_critical"):
                key = base + "_critical"
            if _observed_map_lib.lookup(observed, key)[0] is _observed_map_lib.LookupKind.NO_ALERT:
                files, tenants = found.setdefault((base, key != base), (set(), set()))
                files.add(v.file or "?")
                tenants.add(v.tenant)
    if not found:
        return _make_result("no_alert", PASS, [
            f"{len(served)} served key row(s): every one is compared by a shipped alert "
            "(or deferred by the observed-map)"])

    def capped(names: "set[str]") -> str:
        ordered = sorted(names)
        return ", ".join(ordered[:WARN_LIMIT]) + (
            f" (+{len(ordered) - WARN_LIMIT} more)" if len(ordered) > WARN_LIMIT else "")
    details = []
    for (key, promoted), (files, tenants) in sorted(found.items()):
        tier = " (served as critical)" if promoted else ""
        details.append(f"no_alert `{key}`{tier} — served, but no shipped alert compares it "
                       f"— file(s) {capped(files)} — tenant(s) {capped(tenants)}")
    return _make_result("no_alert", WARN, details)


_UNSUPPLIED_DEFAULTS_HINT = (
    "Each line is a threshold the platform ships switched on that the root "
    "platform file of this tree does not carry, so the alert comparing it "
    "stays silent for every tenant. Add the key under `defaults:` (the line "
    "gives the platform's value), or, to leave it off on purpose, list it "
    "under `optional_overrides:` (a tenant can still set it there). A pack "
    "counts as deployed when the root platform file carries any of its keys "
    "or a tenant declares it in `_metadata.db_type`. Judged against the "
    "registry this tool was built with: a rule pack older than the tool may "
    "not read the key yet. Never blocks (#3830).")


_UNSUPPLIED_DEFAULTS_NOT_CHECKED_HINT = (
    "Nothing was judged, so no default is known to be supplied: fix what the "
    "line above names (a working, current da-guard; a root platform file "
    "that reads) and run again. Never blocks (#3830).")


def check_unsupplied_defaults(config_dir: str) -> dict[str, object]:
    """Thresholds the platform ships armed that this tree does not supply
    (#3830): a key the registry marks `arming: default`, of a pack this tree
    deploys, that the root platform file neither sets under `defaults:` nor
    lists under `optional_overrides:`. `da-tools init` copies the platform
    defaults into the customer's tree, so a default added or replaced later
    (#3778's `mysql_connection_usage_percent`) never reaches it, and the
    alert comparing it is silent with no row saying so.

    Which keys ship armed is the registry's answer, as the generated module
    carries it (`_registry_lib.packaged_default_armed`): no list is kept
    here.

    A pack is deployed when either holds — no deployment inventory exists:

    * the root platform file carries one of its keys, under `defaults:` or
      `optional_overrides:`, in any #1231 spelling (the exporter's rule,
      asked of da-guard), or a retired key whose replacement is one of its
      keys (#3829; the old key shows the pack, it does not supply the new
      one). A tenant's or a profile's key adds nothing: one the root file
      does not declare is already `unread (no_row)`.
    * a tenant's `_metadata.db_type`, as /metrics serves it (served-values:
      a root platform file's `tenants:` entry counts), names it.

    The root platform file is the one parse-tree says the root's chain
    selects (`_defaults.yaml` / `_defaults.yml`): a key only a subtree
    carrier declares reaches no tenant (#1976). A tree with no root platform
    file supplies nothing: a pack a `db_type` shows deployed is named whole.

    Advisory only: WARN or PASS, never FAIL — the registry read is this
    tool's, and the rule packs a deployment runs are another release line.
    A root file that does not read, a served reading or spelling answer that
    cannot be had, and a file the exporter's load drops (a tenant file: its
    tenants' `db_type` is not served; the root file: nothing in it is
    supplied) are each a WARN saying nothing was checked.
    """
    import _registry_lib  # ops/: the registry as the generated module carries it

    def not_checked(why: str) -> dict[str, object]:
        return _make_result("unsupplied_defaults", WARN, [
            f"not checked: whether every default-armed threshold is supplied was not judged ({why})"],
            hint=_UNSUPPLIED_DEFAULTS_NOT_CHECKED_HINT)
    # This tool's own data: a failure here is a defect, not a "not checked".
    registry = _registry_lib.registry_keys(_registry_lib.packaged_registry_doc())
    armed = _registry_lib.packaged_default_armed()
    retired = _registry_lib.packaged_retired_key_packs()
    try:
        carrier = _lib_parse_cache.reader_for(config_dir).carrier(config_dir)
        raw = load_confd_file(config_dir, str(carrier), default={}) if carrier is not None else {}
        block = raw.get("defaults") if isinstance(raw, dict) else None
        listed = raw.get("optional_overrides") if isinstance(raw, dict) else None
        written = sorted(
            {str(k).partition("{")[0] for k in (block if isinstance(block, dict) else {})}
            | {k for k in (listed if isinstance(listed, list) else []) if isinstance(k, str)})
        spell = tv_key_spellings(written)
        served = load_served_tree(config_dir, partial=True)
    except (DaGuardError, DaGuardNotFoundError, OSError, YamlFileError, ParseTreeError,
            *_INPUT_ERRORS) as exc:
        return not_checked(f"{type(exc).__name__}: {' '.join(str(exc).split())}")
    dropped = [*served.parse_failed, *(u.file for u in served.unreadable)]
    if dropped:
        # A tenant of a dropped file is not served, so its db_type is not
        # read — a PASS would read as "every tenant checked".
        return not_checked(f"the exporter's load drops {', '.join(sorted(dropped))}; "
                           "their tenants are not served")
    where = carrier.name if carrier is not None else None
    supplied = {spell.canonical(k): k for k in written}

    def pack_of(canonical: str) -> "str | None":
        """The pack a root-file key shows deployed: its own, or — for a
        retired key (and its `_critical`) — the pack of its replacement."""
        if canonical in registry:
            return registry[canonical]["pack"]
        base = canonical[:-len("_critical")] if canonical.endswith("_critical") else canonical
        return retired.get(base)
    # pack -> what shows it is deployed: (root keys as written, tenants)
    seen: dict[str, tuple[set[str], set[str]]] = {}
    for canonical, as_written in supplied.items():
        pack = pack_of(canonical)
        if pack in armed:
            seen.setdefault(pack, (set(), set()))[0].add(as_written)
    for tenant, tv in served.tenants.items():
        meta = tv.values.get("_metadata")
        kinds = meta.get("db_type") if isinstance(meta, dict) else None
        for kind in ([kinds] if isinstance(kinds, str) else kinds if isinstance(kinds, list) else []):
            if isinstance(kind, str) and kind in armed:
                seen.setdefault(kind, (set(), set()))[1].add(tenant)
    judged = [(pack, key) for pack in sorted(seen) for key in armed[pack]]
    missing = [(pack, key) for pack, key in judged if key not in supplied]
    if not missing:
        if where is None:
            return _make_result("unsupplied_defaults", PASS, [
                "no root _defaults.yaml, and no tenant declares a db_type whose pack ships "
                "an armed threshold — nothing to check"])
        return _make_result("unsupplied_defaults", PASS, [
            f"{len(judged)} default-armed threshold(s) of {len(seen)} deployed pack(s): "
            f"each is set or listed in {where}"])

    def capped(names: "set[str]") -> str:
        ordered = sorted(names)
        return ", ".join(ordered[:WARN_LIMIT]) + (
            f" (+{len(ordered) - WARN_LIMIT} more)" if len(ordered) > WARN_LIMIT else "")
    details = []
    for pack, key in missing:
        keys, tenants = seen[pack]
        why = "; ".join(
            ([f"{where} carries {capped(keys)}"] if keys else [])
            + ([f"db_type of tenant(s) {capped(tenants)}"] if tenants else []))
        entry = registry[key]
        value = f"{entry['value']} {entry.get('unit', '')}".rstrip()
        lacks = (f"{where} neither sets it under `defaults:` nor lists it under "
                 "`optional_overrides:`" if where is not None
                 else "this tree has no root _defaults.yaml to set it in")
        details.append(f"unsupplied `{key}` — the {pack} pack ships it armed (platform value "
                       f"{value}), but {lacks} — {pack} is deployed here: {why}")
    return _make_result("unsupplied_defaults", WARN, details)


_TIER_INVERSION_HINT = (
    "Make each tenant's critical threshold stricter than its warning one "
    "(above it for an alert that fires on a high value, below it for one that "
    "fires on a low value), in whichever file the line names the value from: "
    "the tenant's file, a profile or a platform file. An inverted pair has the "
    "critical alert fire first or together, and severity dedup then mutes the "
    "warning for as long as both fire (#3691).")


_TIER_INVERSION_NOT_CHECKED_HINT = (
    "Nothing was judged, so no inversion is known to be absent: fix what the "
    "line above names (a working, current da-guard; a tree the exporter reads "
    "whole) and run again (#3691).")


def _hhmm(t: str) -> int:
    h, _, m = t.partition(":")
    return int(h) * 60 + int(m)


def check_tier_inversion(config_dir: str, at: str | None = None) -> dict[str, object]:
    """A tenant whose served critical threshold is not stricter than its
    warning one, across files (#3691): a profile's `<base>_critical` against a
    platform file's `<base>`, a tenant's value against a profile's, a window of
    one against the other's default.

    ⛔ Judged on what /metrics serves each tenant over the whole UTC day
    (da-guard served-values `--schedules-only`: every part of the day in which
    some schedule changes, and nothing read at the run's instant, so the time
    of day the check runs at cannot change its verdict, #3877), never on one file: check_threshold_unit_sanity's
    TIER-INVERSION judges each file alone, which is what let a profile's
    critical land under a platform default's warning unseen. Per tenant and
    `metric_key`, the largest warning value served is compared with the
    largest critical value served, minute range by minute range — what the
    shipped alert compares, its threshold recording rule being
    `max by(tenant)` over the tier's series, dimensional ones included.

    Which way is stricter is the shipped alerts' comparison (`direction` in
    `metric_observed_map.yaml`, generated from the rule packs): `>` needs
    critical above warning, `<` below. Judged only when a shipped alert
    compares BOTH tiers — the map holds `<base>` and `<base>_critical` as
    entries of their own, and a pack that normalises only the warning tier
    has no `_critical` entry: no critical alert fires there, so there is
    nothing to invert — and the two entries agree on the direction. A key
    the map defers (`_observed_map_lib.KNOWN_DEFERRED`: its alerts compare a
    version-aware recording rule) has no direction and is not judged.
    Not covered: a profile no tenant binds (no tenant is served from it).

    FAIL on an inverted pair. A tree served-values or the map cannot read,
    or one whose files the exporter's load drops (the dropped files' tenants
    are not served, so not judged), is a WARN saying it was not checked. A
    part of the day /metrics cannot be gathered in (nothing is served then)
    is named as not judged — wherever in the day the check runs.

    `at` (RFC3339; None = now) is the instant time-boxes (`expires:`) are
    judged at; its time of day changes nothing.
    """
    import _observed_map_lib  # ops/: the observed-map the recommender reads

    def not_checked(why: str) -> dict[str, object]:
        return _make_result("tier_inversion", WARN, [
            f"not checked: tier inversion across files was not judged ({why})"],
            hint=_TIER_INVERSION_NOT_CHECKED_HINT)
    try:
        observed = _observed_map_lib.load_observed_map()
        tree = load_served_schedules(config_dir, at=at, partial=True)
    except (DaGuardError, DaGuardNotFoundError, _observed_map_lib.ObservedMapError, OSError,
            YamlFileError) as exc:
        return not_checked(f"{type(exc).__name__}: {' '.join(str(exc).split())}")
    dropped = [*tree.parse_failed, *(u.file for u in tree.unreadable)]
    if dropped:
        # #3691: a tenant of a dropped file is not served, so not judged —
        # a PASS would read as "every tenant checked".
        return not_checked(f"the exporter's load drops {', '.join(sorted(dropped))}; "
                           "their tenants are not served")

    found: list[str] = []
    ungathered: list[str] = []
    pairs = 0
    for tenant, days in sorted(tree.tenants.items()):
        # metric_key -> severity -> [(start, end, value)]. Dimensions are
        # pooled: every shipped threshold recording rule is
        # `max by(tenant)(user_threshold{…, severity=…})` (the
        # `ha-max-threshold-aggregation` pre-commit gate holds them to `max`),
        # so the alert compares the tenant's largest warning against its
        # largest critical, whatever selectors they came from.
        rows: dict[str, dict[str, list[tuple[int, int, float]]]] = {}
        for sched in days.values():
            for seg in sched.segments:
                if seg.error is not None:
                    ungathered.append(f"tenant {tenant!r} {seg.start}-{seg.end} UTC")
                if seg.value is None or not seg.series:
                    continue
                for se in seg.series:
                    if se.legacy_twin:
                        continue
                    sev = se.labels.get("severity")
                    if sev not in ("warning", "critical"):
                        continue
                    rows.setdefault(se.metric_key, {}).setdefault(sev, []).append(
                        (_hhmm(seg.start), _hhmm(seg.end), seg.value))
        for metric_key, by_sev in sorted(rows.items()):
            warnings_, crits = by_sev.get("warning"), by_sev.get("critical")
            if not warnings_ or not crits:
                continue
            directions = set()
            for key in (metric_key, f"{metric_key}_critical"):
                kind, entry = _observed_map_lib.lookup(observed, key)
                directions.add((entry or {}).get("direction")
                               if kind is _observed_map_lib.LookupKind.COMPARED else None)
            if len(directions) != 1:
                continue  # one tier compared by no shipped alert, or the two disagree
            direction = directions.pop()
            if direction not in (">", "<"):
                continue
            cuts = sorted({t for s_, e_, _v in warnings_ + crits for t in (s_, e_)})
            run = None  # (start, end, warning, critical) of the inversion being extended
            spans = []
            for a, b in zip(cuts, cuts[1:]):
                w = [v for s_, e_, v in warnings_ if s_ <= a < e_]
                c = [v for s_, e_, v in crits if s_ <= a < e_]
                if not w or not c:
                    run = None
                    continue
                pairs += 1
                wv, cv = max(w), max(c)
                if (cv <= wv) if direction == ">" else (cv >= wv):
                    if run and run[1] == a and run[2:] == (wv, cv):
                        run = (run[0], b, wv, cv)
                        spans[-1] = run
                    else:
                        run = (a, b, wv, cv)
                        spans.append(run)
                else:
                    run = None
            for a, b, wv, cv in spans:
                found.append(
                    f"tier_inversion tenant {tenant!r} `{metric_key}` "
                    f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d} UTC: "
                    f"critical {cv:g} is not {'above' if direction == '>' else 'below'} "
                    f"warning {wv:g} (the shipped alert compares the tenant's largest of each "
                    f"and fires when the value is {direction} the threshold)")
    # A part of the day /metrics cannot be gathered in serves nothing, so
    # nothing is inverted there — but it was not judged either (#3691).
    note = ([f"not judged where /metrics cannot be gathered (nothing is served): "
             f"{', '.join(sorted(set(ungathered))[:WARN_LIMIT])}"
             + (f" (+{len(set(ungathered)) - WARN_LIMIT} more)" if len(set(ungathered)) > WARN_LIMIT else "")]
            if ungathered else [])
    if not found:
        if not pairs:
            # Nothing compared is not "compared and fine" (#3691).
            return _make_result("tier_inversion", PASS, [
                "no tenant is served a warning and a critical threshold of one key that "
                "shipped alerts compare at both tiers, so there was nothing to compare", *note])
        return _make_result("tier_inversion", PASS, [
            f"{pairs} served warning/critical pair-window(s): every critical is stricter than "
            "its warning (a profile no tenant binds is not served, so not judged)", *note])
    shown = found[:WARN_LIMIT]
    if len(found) > len(shown):
        shown.append(f"+{len(found) - len(shown)} more")
    return _make_result("tier_inversion", FAIL, shown + note, hint=_TIER_INVERSION_HINT)


def _root_defaults_verdicts(root: dict, dr) -> list[tuple[str | None, str, str]]:
    """What of the root carrier's `defaults:` makes the exporter drop the
    file or leave a key unwritten, read off the file's parse-tree nodes
    (aliases and `<<` merges expanded): `[(key, source text, kind), …]`
    with *dr*'s kinds (``deprecate_rule``), key None for the whole block.

    A null leaves the key unwritten (#2518, ``NULL_NOT_DECLARED``); a
    mapping or a list fails the decode and the exporter drops the whole
    file (``UNPARSEABLE``). A scalar is never judged here: since #3250 the
    exporter reads its text through parseThresholdNumber and refuses that key
    alone — da-guard's root_default_refused, the `guard_errors` row's. A
    file parse-tree cannot read (a repeated key, several documents …) never
    gets here: its read fails first. The truth
    table is ``tests/golden/fixtures/defaults-carrier-oracle.json``, which the
    Go test ``TestDefaultsCarrierOracle`` judges against the exporter."""
    block = root.get("defaults")
    if block is None or (_lib_parse_tree.is_scalar(block) and block["tag"] == "!!null"):
        return []                              # absent, or `defaults:` with no value
    if _lib_parse_tree.is_scalar(block) or not isinstance(block, dict):
        return [(None, "defaults is not a mapping", dr.UNPARSEABLE)]
    out: list[tuple[str | None, str, str]] = []
    for key, node in block.items():
        if not _lib_parse_tree.is_scalar(node):
            out.append((key, "<mapping>" if isinstance(node, dict) else "<list>",
                        dr.UNPARSEABLE))
        elif node["tag"] == "!!null":
            out.append((key, node["text"], dr.NULL_NOT_DECLARED))
    return out


def check_root_defaults(config_dir: str) -> dict[str, object]:
    """The ROOT `_defaults.yaml`'s `defaults:` as the exporter decodes it.

    Three rules, one row:

    * #1414 — a mapping or a list as a value fails the exporter's decode
      and it drops the whole root file — every platform threshold and
      every other setting in it, while the load is reported as successful; a null / empty
      value is reported as having no value (#2518; before it, a 0
      threshold) — what it changes depends on the rest of the tree and is
      not judged here. Both FAIL. The structure is ``_root_defaults_verdicts``
      over the file's ``da-guard parse-tree`` nodes (ADR-036 step 4, #2906),
      checked against the truth table
      ``tests/golden/fixtures/defaults-carrier-oracle.json`` that the Go test
      ``TestDefaultsCarrierOracle`` judges. Every kind in
      ``deprecate_rule.BLOCKING_KINDS`` is a FAIL here too.
    * #2291 — no ``_routing*`` key, whatever its value (below). A
      ``_routing*`` key gets that rule's line only, never a second one from
      the value rule.
    * #3045 — any other ``_`` key under ``defaults:`` whose value decodes
      is a WARN: no ``_`` key is a threshold (the exporter makes no row of
      one) and no other reader reads one there, so it is dead config. A
      value that does not decode keeps the value rule's FAIL line only.

    #3387: what da-guard reports as an error about this file is the
    ``guard_errors`` row's, not this one's — a scalar the exporter refuses
    (``abc``, ``0x10``, ``.nan``, ``disable``: root_default_refused), a
    registered ``_`` key that reaches no tenant (root_defaults_reserved_key),
    and a carrier with no ``defaults:`` mapping whose top level holds a key
    the merge takes (root_defaults_unwrapped).

    ⚠️ A file parse-tree cannot read at all fails through
    ``load_confd_file`` and ``_run_check``'s input-error row;
    a top level that is not a mapping stays ``yaml_syntax``'s finding. The
    value rule speaks about files that load.

    ``defaults:`` holds platform thresholds. Routing defaults live in the
    top-level ``_routing_defaults:`` block, and a ``_routing`` written under
    ``defaults:`` instead gets a different answer from every reader:

    * threshold-exporter decodes the root ``defaults:`` as
      ``map[string]float64``. A mapping there fails that decode and the whole
      file is dropped — every platform threshold, with the load still
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
    deliberately not about. The carrier is the one parse-tree says the root's
    chain selects (``ConfdReader.carrier``, its ``defaults_carriers``), the
    rule every plane shares (#1674) — not a Python selection, a
    second implementation of it that could hand the read a file parse-tree
    does not select (rc 2, #2906 review).
    """
    carrier = _lib_parse_cache.reader_for(config_dir).carrier(config_dir)
    if carrier is None:
        return _make_result("root_defaults", PASS,
                            ["no root _defaults.yaml — nothing to check"])
    rel = carrier.name
    raw = load_confd_file(config_dir, str(carrier), default={})
    if not isinstance(raw, dict):
        return _make_result("root_defaults", PASS,
                            [f"{rel}: no `defaults:` mapping — nothing to check"])
    # #1414 → ADR-036 step 4 (#2906): yaml.v3's own reading of the file
    # (parse-tree's tags), the same read as `raw` above — not a PyYAML
    # mirror of yaml.v3's decode. The verdict kinds stay deprecate_rule's.
    import deprecate_rule as dr
    verdicts = _root_defaults_verdicts(
        _lib_parse_cache.reader_for(config_dir).node(carrier), dr)
    dropped = {k: r for k, r, kind in verdicts
               if k is not None and kind in dr.BLOCKING_KINDS}
    details = [_root_defaults_value_detail(rel, k, r,
                                           kind == dr.NULL_NOT_DECLARED)
               for k, r, kind in verdicts
               if not (k is not None and k.startswith("_routing"))
               and (kind in dr.BLOCKING_KINDS or kind == dr.NULL_NOT_DECLARED)]
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
    # #3387: which scalar the exporter refuses (root_default_refused) and a
    # registered `_` key that reaches no tenant (root_defaults_reserved_key)
    # are da-guard's errors, the `guard_errors` row's — not judged here.
    dead = [f"{rel}: `defaults.{k}` is read by nothing — no `_` key is a threshold "
            "(threshold-exporter makes no row of one, #3045) and no other reader reads "
            "`_` keys under `defaults:`; delete it, or drop the `_` to make it a threshold"
            for k in block
            if isinstance(k, str) and k.startswith("_") and not k.startswith("_routing")
            and k not in dropped]
    if dead:
        return _make_result("root_defaults", WARN, dead)
    return _make_result("root_defaults", PASS, [
        f"{rel}: {len(block)} key(s) under `defaults:`, no mapping, list or null value "
        f"and no `_routing*` key (a value the exporter refuses: `guard_errors`)"])


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
        "Fix each value listed above as its own line says. A plain true / 123 / "
        "1e3 / 2024-01-02 in a field the schema types as a string is read as "
        "a boolean, number or timestamp by yaml.v3 — by every reader of "
        "conf.d — so quote it; yes / no / on / off are strings to yaml.v3 and "
        "a field that takes a boolean refuses them: write true / false. A "
        "field that takes only numbers (min_events) takes them in plain "
        "decimal: write the value the line names, unquoted (#3764).",
        "docs/cli-reference.md#validate-config",
    ),
    "json_schema": (
        "Fix each value listed above to the shape its JSON Schema requires "
        "(the message names the field path after `@`). This is the same "
        "verdict as scripts/tools/lint/check_confd_schema.py.",
        "docs/cli-reference.md#validate-config",
    ),
    "routes": (
        "Check _routing and _routing_defaults for invalid receiver types, "
        "group_by, or timing values. "
        "Run: da-tools explain-route --config-dir <dir> --tenant <id>",
        "docs/scenarios/alert-routing-split.md",
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
    # #3157: each row already names the file, the declared id and the new name.
    "tenant_file_name": (
        "Rename each listed file to the tenant id it declares (`<id>.yaml`, in "
        "the same directory, keeping its history), and split a file that "
        "declares several tenants into one file per tenant. A tenant file's "
        "name is its tenant id on tenant-api; the exporter takes the id from "
        "`tenants:`.",
        "docs/cli-reference.md#validate-config",
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
    # #3387: each line is one of da-guard's error findings, as it wrote it.
    "guard_errors": (
        "Fix each finding as its own message says (each line is da-guard's "
        "finding: its kind, the field, the tenants it affects). `da-tools guard "
        "defaults-impact --config-dir <dir>` prints the same report.",
        "docs/cli-reference.md#validate-config",
    ),
    # ADR-038: each line names the file, the key, the reason and the tenants.
    "no_alert": (_NO_ALERT_HINT, "docs/cli-reference.md#validate-config"),
    "unsupplied_defaults": (_UNSUPPLIED_DEFAULTS_HINT, "docs/cli-reference.md#validate-config"),
    "tier_inversion": (_TIER_INVERSION_HINT, "docs/cli-reference.md#validate-config"),
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
    on what remained — so a row can say PASS about a tenant that was never
    loaded. Before #1448 nobody saw
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


def json_document(results: list[dict[str, object]]) -> list[dict[str, object]]:
    """The --json document: the rows with their suggested actions (v2.5.0)
    — run() encodes it (#3216)."""
    unusable = _unusable_files(results)
    # v2.5.0: Inject hints into JSON output for programmatic consumers
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
    return enriched


def print_report(results: list[dict[str, object]]) -> None:
    """Print the validation report with suggested actions (v2.5.0)."""
    unusable = _unusable_files(results)
    caveat = ""
    if unusable:
        listed = ", ".join(unusable[:5]) + (" …" if len(unusable) > 5 else "")
        caveat = UNUSABLE_CAVEAT.format(n=len(unusable),
                                        listed=listed)

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
            # dict, because the --json document (`json_document`) is built from
            # the same rows and its bytes must not change (JSON escapes C0).
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
                # file. With a broken `_defaults.yaml`, a row can report
                # legitimate tenant keys as unread, and following its advice
                # deletes a working config. The finding is not suppressed (it
                # may be real once the read error is fixed); the order of
                # operations is.
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
    `policy_dsl`'s lookup is left): `routes` and `policy_dsl` got their
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
            if scope.get("root_carrier") in (False, "unreadable"):
                source = ("the root defaults carrier cannot be opened, so no `_policies` "
                          "were read from it" if scope["root_carrier"] == "unreadable"
                          else "there is no root defaults carrier")
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
        # config directory has gone, which a check that reads the tree can
        # reach. `main()` checks `isdir` first, so this
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
def main() -> "Report | None":
    """CLI entry point: One-stop configuration validation. Every row reads
    one `da-guard parse-tree` document of the tree (ADR-036 step 4). A
    judge (`judging`, #3206): a path Go names is its finding, exit 1.
    Under --json the rows are handed back for run() to encode (#3216)."""
    with _lib_parse_cache.session(), _lib_parse_cache.judging():
        return _main()


def _main() -> "Report | None":
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=_h('description'))
    # #3755: a declared conf.d (`_lib_input`, listed by da-guard), judged
    # once by `_lib_parse_cache.declared_trees` below, before any row runs.
    _lib_input.add_input(parser, "--config-dir", select=_lib_input.DA_GUARD_LISTS,
                         empty=_lib_input.EMPTY_REFUSED, required=True,
                         help=_h('config_dir'))
    # #3797: the deployment policy is the generator's declared document
    # (`add_policy_argument`), judged and read once below, before any row
    # runs; the lint policy is lint's own input (`--lint-policy`, handed to it
    # as `--policy`, never read here).
    import generate_alertmanager_routes as gen  # noqa: PLC0415
    gen.add_policy_argument(parser, lang=_LANG)
    parser.add_argument("--lint-policy", metavar="PATH", help=_h('lint_policy'))
    parser.add_argument("--rule-packs",
                        help=_h('rule_packs'))
    parser.add_argument("--version-check", action="store_true",
                        help=_h('version_check'))
    parser.add_argument("--json", action="store_true",
                        help=_h('json'))
    # #3797: a declared document, judged and read once below.
    _lib_input.add_input(parser, "--policy-dsl", decode=pe_policy_file_document(),
                         remedy=_POLICY_DSL_INPUT_HINT, help=_h('policy_dsl'))
    parser.add_argument("--strict", action="store_true",
                        help=_h('strict'))
    args = parser.parse_args()
    # #1650's class: a flag accepted and then never read is a caller error.
    if args.lint_policy is not None and args.rule_packs is None:
        parser.error("--lint-policy is read only with --rule-packs (the custom rule lint)")

    # #3755: --config-dir judged ONCE, before any row runs, by its
    # declaration (`_lib_parse_cache.declared_trees`, in this run's session,
    # so every row reads the reader it judged): not there, no directory, a
    # parent it cannot enter, a root da-guard cannot list, or no config file
    # under it — each rc 2, one line naming the flag, no document (#3617:
    # no row ran, so there is no answer). A root da-guard cannot list was a
    # yaml_syntax finding, rc 1; an empty tree a partial report at rc 2 that
    # never named the flag. ⛔ `judging()` does not change this: the root is
    # judged (`ConfdReader._judge_root`) before the reader decides whether
    # to refuse a tree Go names paths on. A CallerError is not caught here:
    # `_lib_exitcodes.run` prints its line and exits 2.
    # ADR-036 step 4 (#2906): no usable `da-guard parse-tree` — no row can
    # read the tree, so no row runs (a caller error, as for a missing dir).
    try:
        _lib_parse_cache.declared_trees(args, "config_dir", lang="en")
    except ParseTreeError as exc:
        row = _parse_tree_failure_row("config_dir", exc, _PARSE_TREE_HINT)
        print(f"ERROR: {safe_label(' '.join(row['details']))}", file=sys.stderr)
        # #3617: the advice the row carried (rebuild da-guard, do not edit the
        # tree, …) — no document carries it now.
        if row["hint"]:
            print(f"  hint: {safe_label(row['hint'])}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # Ensure tools dir is in sys.path for imports
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    # #3797: --policy judged and read ONCE, before any row runs, by the
    # generator's own resolver — a path that is no readable YAML mapping
    # (CallerError through `run()`), a mapping that is no usable policy or
    # asks for a newer generator: rc 2, no document (#3617: no row ran).
    # The routes row judges under the policy this run holds.
    # --policy-dsl too: the path and strict UTF-8 judged, the file read
    # once; its content is the policy_dsl row's to judge (an argv row).
    policy_dsl_text = _lib_input.document(args, "policy_dsl", lang=_LANG)
    try:
        policy = gen.resolve_policy(args, lang=_LANG)
    except PolicyInputError as exc:
        print(f"ERROR: {safe_label(str(exc))}", file=sys.stderr)
        hint = (POLICY_VERSION_REMEDY if isinstance(exc, PolicyVersionError)
                else _POLICY_ROUTES_ROW_HINT)
        print(f"  hint: {safe_label(hint)}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    results = []
    # #2740: the rows below share one `da-guard effective` run; closed after
    # them. `_run_check` catches everything a row raises, so nothing skips
    # the close.
    global _effective_runs
    _effective_runs = {}

    # 1. YAML syntax
    results.append(_run_check("yaml_syntax", check_yaml_syntax,
                              args.config_dir, _config_dir=args.config_dir))

    # 1b. Quoting of string-typed fields (#2164)
    results.append(_run_check("yaml_quoting", check_yaml_quoting,
                              args.config_dir, _config_dir=args.config_dir))

    # 1c. JSON Schema (#2695) — after 1b, whose findings it leaves out.
    results.append(_run_check("json_schema", check_json_schema,
                              args.config_dir, _config_dir=args.config_dir))

    # 3. `generate-routes --validate` (with --policy / --strict as given):
    # the one verdict on routing, the generator's tree checks, the domain
    # policy and the shared key verdict (#3873). There is no row beside it
    # that re-reads its findings or judges the keys again.
    verdicts: list = []
    results.append(_run_check("routes", check_routes, args.config_dir,
                              policy, strict=args.strict, verdict_out=verdicts,
                              _config_dir=args.config_dir))

    # 5. Custom rule lint (if rule-packs dir provided)
    # ⛔ THIS dispatcher IS the verdict site.
    # `check_custom_rules` has exactly one call site — this one — so there is no
    # unconditional second path to catch an unusable value. MEASURED: reverting
    # this line to truthiness takes `--rule-packs ""` from rc=2 to rc=0 with the
    # whole report reading PASS, verdict and row gone together.
    if args.rule_packs is not None:
        results.append(_run_check("custom_rules", check_custom_rules,
                                  args.rule_packs, args.lint_policy,
                                  _config_dir=args.config_dir))

    # 6. Profile references (v1.12.0)
    results.append(_run_check("profiles", check_profiles, args.config_dir, _config_dir=args.config_dir))

    # 7. Version consistency (if requested)
    if args.version_check:
        results.append(_run_check("versions", check_versions, _config_dir=args.config_dir))

    # 8. Policy-as-Code (DSL evaluation)
    results.append(_run_check("policy_dsl", check_policy_dsl,
                              args.config_dir, args.policy_dsl, policy_dsl_text,
                              _config_dir=args.config_dir))

    # 9. Tenant declaration uniqueness (#1577) — unconditional: the state it
    # finds makes the exporter refuse the whole tree, so there is no flag
    # under which a customer would want this one skipped.
    results.append(_run_check("tenant_uniqueness", check_tenant_uniqueness,
                              args.config_dir, _config_dir=args.config_dir))

    # 9b. Tenant file name = declared tenant id (#3157) — unconditional: a
    # platform invariant; tenant-api and the exporter name the tenant apart.
    results.append(_run_check("tenant_file_name", check_tenant_file_name,
                              args.config_dir, _config_dir=args.config_dir))

    # 10. Root `defaults:` block (#2291) — unconditional, same reason: a
    # `_routing` mapping there costs the exporter every platform threshold.
    results.append(_run_check("root_defaults", check_root_defaults,
                              args.config_dir, _config_dir=args.config_dir))

    # 13. Every error finding of da-guard's own check (#3387) — unconditional:
    # the severity is da-guard's alone (it replaced the rows that copied one
    # finding each: values_not_served, defaults_wrapper, schedule_null,
    # custom_alert_cap, and root_defaults' share).
    results.append(_run_check("guard_errors", check_guard_errors,
                              args.config_dir, _config_dir=args.config_dir))

    # 15. Served keys no shipped alert compares (ADR-038 decision 10, #3591)
    # — unconditional, advisory: WARN at most, never a FAIL. It reads the
    # key verdict the routes row gave; judges one only when that row ended
    # before judging keys (a refusal), and none when it tried and could not.
    given = verdicts[0] if verdicts else None
    results.append(_run_check("no_alert", check_no_alert, args.config_dir,
                              verdict=given if isinstance(given, key_verdict.TreeVerdict) else None,
                              judge=given is None, _config_dir=args.config_dir))

    # 16. Default-armed thresholds of a deployed pack that the root platform
    # file neither sets nor lists (#3830) — unconditional, advisory: WARN at
    # most, never a FAIL.
    results.append(_run_check("unsupplied_defaults", check_unsupplied_defaults,
                              args.config_dir, _config_dir=args.config_dir))

    # 17. A tenant's served critical not stricter than its warning, across
    # files (#3691) — unconditional; FAIL on an inverted pair.
    results.append(_run_check("tier_inversion", check_tier_inversion, args.config_dir,
                              _config_dir=args.config_dir))

    _effective_runs = None

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
    unanswered = [
        r["check"] for r in results
        if r["status"] == FAIL and r.get("caller_error")
        and not (input_unreadable and r.get("reads_config_dir", True))
    ]
    status = (EXIT_CALLER_ERROR if unanswered
              else EXIT_VIOLATION if has_fail else EXIT_OK)

    # Report. #3617: rc 2 here is a partial answer — every row, the rows the
    # caller's environment kept from answering named.
    if args.json:
        return Report(json_document(results), status,  # #3216: run() encodes it
                      unanswered=unanswered if status == EXIT_CALLER_ERROR else None,
                      of=len(results))
    print_report(results)
    sys.exit(status)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
