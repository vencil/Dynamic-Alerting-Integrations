#!/usr/bin/env python3
"""Unified validation entry point for all documentation and config validation tools.

Usage:
  python3 scripts/tools/validate_all.py                # sequential (default)
  python3 scripts/tools/validate_all.py --parallel      # parallel execution
  python3 scripts/tools/validate_all.py --ci            # stop at first failure (exit 1); summary + later flags still run
  python3 scripts/tools/validate_all.py --skip links,mermaid
  python3 scripts/tools/validate_all.py --only versions,tool_map
  python3 scripts/tools/validate_all.py --list           # registered check names
  python3 scripts/tools/validate_all.py --json          # JSON summary output
  python3 scripts/tools/validate_all.py --json --baseline  # save JSON as baseline
  python3 scripts/tools/validate_all.py --json --compare   # compare against baseline
  python3 scripts/tools/validate_all.py --diff-report       # show what --fix would change (needs `git add -u` first)
  python3 scripts/tools/validate_all.py --fix              # auto-fix all drift
  python3 scripts/tools/validate_all.py --profile          # append timing to CSV
  python3 scripts/tools/validate_all.py --watch            # file-watch auto-rerun
  python3 scripts/tools/validate_all.py --smart            # git-diff based auto-skip
  python3 scripts/tools/validate_all.py --notify           # desktop notification on completion

Exit codes:
  0  every selected check passed
  1  a check failed
  2  the invocation could not be used: an unrecognised flag, or a --only /
     --skip value that no registered check answers to (--list prints them).
     In --json mode this path writes nothing to stdout; the reason goes to
     stderr, the same as argparse's own errors. (#1620)

--json output contract (#1772): stdout carries exactly one JSON document on
every path that gets past argument parsing, including a failing run combined
with --fix / --diff-report / --verbose / --profile / --notify. Everything
those flags say goes to stderr; the report body is not printed at all.
"""

import argparse
import json as json_mod
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
# EXIT_CALLER_ERROR (2) for an unusable invocation. ⚠️ dev-rules #13's literal
# scope is "da-tools 子命令" and this runner is NOT one — it is absent from
# components/da-tools/app/entrypoint.py's COMMAND_MAP. The gate that enforces
# dev-rules #13 (tests/shared/test_tool_exit_codes.py) used to walk ops/ dx/
# lint/ only, so this file sat outside it; since #1642 the top level of
# scripts/tools/ is enumerated too and this file IS under that contract
# (--help → 0, unknown flag → 2).
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
BASELINE_FILE = REPO_ROOT / ".validation-baseline.json"
PROFILE_CSV = REPO_ROOT / ".validation-profile.csv"
# 每個 check 的時間預算；check 的測試若要對真樹跑，拿這個當上限而不是另開一個數字。
CHECK_TIMEOUT_SECONDS = 120

# Mapping from check name → fix command (script + args).
# Only checks that have a regenerate/fix mode are listed here.
FIX_COMMANDS: Dict[str, List[str]] = {
    "tool_map": ["dx/generate_tool_map.py", "--generate", "--lang", "all"],
    "doc_map": ["dx/generate_doc_map.py", "--generate", "--include-adr", "--lang", "all"],
    "rule_pack_stats": ["dx/generate_rule_pack_stats.py", "--generate", "--lang", "all"],
    "byo_rulepack_table": ["dx/generate_byo_rulepack_table.py", "--generate", "--lang", "all"],
    "versions": ["lint/validate_docs_versions.py", "--fix"],
    "alerts": ["dx/generate_alert_reference.py"],
    "rule_packs": ["dx/generate_rule_pack_readme.py", "--update"],
    "includes": ["lint/check_includes_sync.py", "--fix"],
    "freshness": ["lint/check_doc_freshness.py", "--fix"],
    "platform_data": ["dx/generate_platform_data.py"],
    "repo_name": ["lint/check_repo_name.py", "--fix"],
    "frontmatter_versions": ["lint/check_frontmatter_versions.py", "--fix"],
}

# ⛔ A row registered without its script's gate flag CANNOT FAIL — the tool
# prints the violations and still returns 0, so the runner shows a tick
# (#1702). links and mermaid are armed below and pinned in
# tests/shared/test_validate_all.py::TestRearmedRows; translation and
# freshness are still un-armed (#1735): translation is red on today's
# content, and freshness's `--check` is rc 0 ONLY on a shallow clone (the
# graft commit's date hides every doc's real age) -- on full history docs
# older than 90 days exist, so arming it would make a bare run red for
# content reasons this runner does not own.
TOOLS = [
    ("links", "lint/check_doc_links.py", ["--ci"], "Link validation"),
    ("mermaid", "lint/validate_mermaid.py", ["docs/", "rule-packs/", "--ci"], "Mermaid diagram syntax"),
    ("translation", "lint/check_translation.py", [], "Bilingual structure consistency"),
    ("glossary", "dx/sync_glossary_abbr.py", ["--check"], "Glossary abbreviation sync"),
    ("schema", "dx/sync_schema.py", ["--check"], "Go→JSON Schema drift"),
    ("alerts", "dx/generate_alert_reference.py", ["--check"], "Alert reference drift"),
    ("rule_packs", "dx/generate_rule_pack_readme.py", ["--check"], "Rule Pack README drift"),
    ("freshness", "lint/check_doc_freshness.py", [], "Dead doc detection"),
    ("includes", "lint/check_includes_sync.py", ["--check"], "Include snippet zh/en sync"),
    ("changelog", "dx/generate_changelog.py", ["--check"], "Conventional commit format"),
    # ⛔ Separate entry on purpose: `--check` walks git log and never opens the
    # file. Until #1765 that was the ONLY thing a CHANGELOG.md edit triggered,
    # so a green run meant "commit messages are well formed", not "this file's
    # structure is intact" — and nothing was checking the latter.
    # ⛔ Paths are explicit. A bare `--lint` defaults to CHANGELOG.md, so this
    # row would have linted THAT file even when it was selected because
    # CHANGELOG-archive.md changed — the same "checks a file it was not
    # pointed at" shape #1765 is about, one layer up.
    # ⛔ These targets and the WATCH_TRIGGERS keys below must stay in step: a
    # file mapped to this check but absent from this list is claimed coverage
    # that does not exist. `CHANGELOG.en.md` is mapped and does NOT exist in
    # the tree, so it is deliberately not listed here — naming it would make
    # every run exit 2 ("not a readable file"). A test fires the moment that
    # file appears, because then it must be added here too.
    ("changelog_format", "dx/generate_changelog.py",
     ["--lint", "CHANGELOG.md", "CHANGELOG-archive.md"], "Changelog file structure"),
    ("versions", "lint/validate_docs_versions.py", ["--ci"], "Version/count consistency"),
    ("rule_pack_stats", "dx/generate_rule_pack_stats.py", ["--check", "--lang", "all"], "Rule Pack stats include drift"),
    ("byo_rulepack_table", "dx/generate_byo_rulepack_table.py", ["--check", "--lang", "all"], "BYO Prometheus rule-pack table drift"),
    ("tool_map", "dx/generate_tool_map.py", ["--check", "--lang", "all"], "Tool map coverage drift"),
    ("doc_map", "dx/generate_doc_map.py", ["--check", "--include-adr", "--lang", "all"], "Doc map coverage drift"),
    ("platform_data", "dx/generate_platform_data.py", ["--check"], "Platform data drift (JSON vs YAML)"),
    ("tool_consistency", "lint/lint_tool_consistency.py", [], "Tool registry ↔ Hub ↔ JSX ↔ MD links"),
    ("repo_name", "lint/check_repo_name.py", ["--ci"], "Repo name guard (no vibe-k8s-lab in URLs)"),
    ("structure", "lint/check_structure.py", ["--ci"], "Project structure enforcement"),
    ("jsx_babel", "lint/lint_jsx_babel.py", ["--ci"], "JSX Babel standalone parse validation"),
    ("html_doc_links", "lint/lint_html_doc_links.py", ["--ci"], "Raw HTML doc-link validation (MkDocs-aware)"),
    ("head_blob_hygiene", "lint/check_head_blob_hygiene.py", ["--ci"], "HEAD blob hygiene (NUL bytes / truncated EOF)"),
    ("cli_coverage", "lint/check_cli_coverage.py", ["--ci"], "CLI command coverage (entrypoint ↔ docs)"),
    ("cli_default_drift", "lint/check_cli_default_drift.py", ["--ci"], "cli-reference default column ↔ argparse defaults (#1556)"),
    ("cli_contract", "lint/check_cli_contract.py", ["--ci"], "documented da-tools invocations ↔ argparse contract + baseline ledger (#1379)"),
    ("bilingual_content", "lint/check_bilingual_content.py", ["--ci"], "Bilingual content CJK ratio check"),
    ("frontmatter_versions", "lint/check_frontmatter_versions.py", ["--ci"], "Frontmatter version global scan"),
    ("path_metadata", "lint/check_path_metadata_consistency.py", ["--ci"], "conf.d path vs _metadata consistency (warning-only)"),
    ("commit_scope", "lint/check_commit_scope_doc.py", ["--ci"], "Commit scope drift (commit-convention.md vs .commitlintrc.yaml)"),
    ("hardcode_tenant", "lint/check_hardcode_tenant.py", ["--ci"], "Hardcoded tenant literals in PromQL (Rule #2)"),
    ("changelog_no_tbd", "lint/check_changelog_no_tbd.py", ["--ci"], "TBD/TODO placeholders in CHANGELOG (Self-review Gap A.c)"),
    ("undefined_tokens", "lint/check_undefined_tokens.py", ["--ci"], "JSX/CSS/HTML references to undefined --da-* tokens (S#85 colour-only → S#86 all categories → S#88 +.html +--report-orphans)"),
    ("jsx_loader_compat", "lint/check_jsx_loader_compat.py", ["--ci"], "JSX-loader Babel-standalone compat (named exports / non-allowlist imports / require() — S#93 from PR #182 fix)"),
    ("playwright_rtl_drift", "lint/check_playwright_rtl_drift.py", ["--ci"], "Playwright spec uses React Testing Library API names (getByDisplayValue / getByLabelText / getByPlaceholderText — S#96 mechanical safety net for testing-playbook §LL §10 from PR #184 fix)"),
]


def _unknown_check_names(only_set, skip_set):
    """Requested check names that no registered check answers to.

    The accepted set is re-derived from ``TOOLS`` here — the same list
    ``--list`` prints and the same list the runnable filter matches against —
    so registering a check makes it acceptable with no second place to update,
    and un-registering one makes it unacceptable the same way. There is
    deliberately no list of *rejected* spellings: a name is acceptable iff a
    check answers to it.

    Returns a list of ``(flag, sorted_unknown_names)`` pairs, empty when every
    requested name exists. The flag is carried so the message can say which of
    the two flags the operator has to look at.
    """
    known = {name for name, _, _, _ in TOOLS}
    problems = []
    for flag, names in (("--only", only_set), ("--skip", skip_set)):
        unknown = sorted(n for n in names if n not in known)
        if unknown:
            problems.append((flag, unknown))
    return problems


def _unknown_check_names_message(problems):
    """Operator-facing text for a non-empty _unknown_check_names() result.

    Three things this message has already been wrong about, each fixed here:

    1. ⛔ The two ways out are NOT equivalent, and the first draft offered them
       as peers ("fix the spelling, or drop the name"). Correcting the spelling
       runs the check; deleting the name ALSO reaches green. So: lead with the
       close match, and name the cost of the other route instead of offering
       it.
    2. ⛔ That cost is OPPOSITE for the two flags, and the second draft printed
       one sentence for both. Measured: `--skip glossary` runs the other
       checks; before #1620, `--skip glossary_TYPO` ran everything — the
       unknown name made MORE run, not fewer. Deleting a name from `--skip`
       makes that check start running; deleting one from `--only` makes it
       stop. Each flag gets its own consequence line.
    3. ⚠️ ASCII only. This goes to stderr, and `_lib_compat.try_utf8_stdout`
       deliberately patches stdout alone (see its own rationale), so on the
       cp950-class consoles this repo actually runs on, `⛔` degraded to
       `\\u26d4` and an em dash to mojibake — on the one line carrying the
       warning. Severity is carried by the word WARNING, not by a glyph.

    The registry is pointed at via ``--list`` rather than repeated, so the
    advice cannot go stale.
    """
    import difflib

    known = sorted(name for name, _, _, _ in TOOLS)
    # ⛔ Match case-insensitively. difflib is not, so `VERSIONS` scored
    # below the cutoff and got NO suggestion -- and this whole message is
    # built on "the safe remedy is also the cheapest, and it is on line 2".
    # `VERSIONS` is one of the probes the tests use, which is how the gap
    # stayed invisible: they only asserted it was rejected (#1620).
    _folded = {k.lower(): k for k in known}
    detail = "; ".join(f"{flag} {', '.join(names)}"
                       for flag, names in problems)
    hint = ""
    for flag, names in problems:
        sugg = []
        for n in names:
            for c in difflib.get_close_matches(n.lower(), list(_folded),
                                               n=3, cutoff=0.6):
                if _folded[c] not in sugg:
                    sugg.append(_folded[c])
        if sugg:
            hint += f"  Did you mean ({flag}): {', '.join(sugg)}\n"
    # ⛔ The WHOLE consequence is per-flag, not just the warning. An earlier
    # draft split the warning but left one shared body sentence, and that
    # sentence was `--only`'s failure shape printed under `--skip` too.
    consequence = {
    # ⛔ NOTHING here may predict what this run will do. Three rounds
    # of #1620 blind review died on that: the builder cannot see the
    # mode -- `--smart` fills only_set AFTER this guard, and `--watch`
    # never reads args.only at all -- so every mode-dependent clause was
    # false for some caller, and each fix invented a new one. What is
    # left is true whatever mode printed it: what the NAME did, and what
    # deleting it would and would not accomplish.
    # ⛔ NOTHING here may predict what this run will do. Three rounds
    # of #1620 blind review died on that: the builder cannot see the
    # mode -- `--smart` fills only_set AFTER this guard, and `--watch`
    # never reads args.only at all -- so every mode-dependent clause was
    # false for some caller, and each fix invented a new one. Two lines
    # per flag: what the NAME did, and what the cheap fix would cost.
    # The history of the bug belongs in the commit, not in an error.
        "--only": (
            "  --only: no registered check answers to this name, so it selects\n"
            "  nothing.\n"
            "  WARNING (--only): deleting the name also turns this green without\n"
            "  fixing anything -- only some checks are pinned as must-still-be-\n"
            "  selected (#1492).\n"),
        "--skip": (
            "  --skip: no registered check answers to this name, so it subtracts\n"
            "  nothing.\n"
            "  WARNING (--skip): deleting the name also turns this green, and\n"
            "  subtracts nothing either way.\n"),
    }
    tail = "".join(consequence[flag] for flag, _names in problems)
    return (
        f"error: no registered check is named: {detail}\n"
        + hint +
        "  `python3 scripts/tools/validate_all.py --list` prints every "
        "registered name.\n"
        + tail
    ).rstrip("\n")


# #1695 family 2, the `--watch` half: `_run_watch` is handed `args` and
# reads ONLY `args.skip` and `args.verbose`, then main() returns before the
# summary and before every flag handled after the run loop. So each flag
# below was accepted with `--watch` and did nothing. dest -> flag. This is
# a PIN, not a derivation: the test suite derives the same set from the
# parser's store_true flags minus the `args.<attr>` reads inside
# `_run_watch` (AST) and compares, so a flag added later that `_run_watch`
# does not read goes red there instead of being dropped quietly again.
_WATCH_NEVER_READS: Dict[str, str] = {
    "ci": "--ci",
    "parallel": "--parallel",
    "json": "--json",
    "baseline": "--baseline",
    "compare": "--compare",
    "fix": "--fix",
    "profile": "--profile",
    "smart": "--smart",
    "diff_report": "--diff-report",
    "notify": "--notify",
}


def _conflicting_flags(args, only_set):
    """Flag pairs where one flag would silently lose to the other (#1695,
    family 1, plus the ``--watch`` half of family 2).

    Each entry is ``(kept, ignored, what_the_ignored_flag_would_have_done)``.
    Measured on the parent of this change, every pair here was accepted at
    rc 0 with output indistinguishable from the invocation WITHOUT the
    losing flag:

      --smart with --only     `if args.smart and not only_set` never
                              computes the git-diff selection; ran exactly
                              what `--only` alone ran.
      --parallel with --ci    `if args.parallel and not args.ci` takes the
                              sequential branch; the report header, the
                              JSON `mode` and the profile CSV all still
                              said `parallel` (#1705's neighbour).
      --baseline with --compare
                              `if args.baseline: … elif args.compare:` —
                              the comparison is never printed.
      --watch with --only     `_run_watch` reads `args.skip` and never
                              `only_set`; watched everything. Note the
                              asymmetry this closes: `--watch --only
                              <typo>` was already rc 2 (#1620), so the
                              WRONG name got more information than the
                              right one.
      --watch with any flag in _WATCH_NEVER_READS
                              `_run_watch` returns before the summary; the
                              flag was parsed and never consulted (family
                              2's watch half). `--baseline` / `--compare`
                              also set `args.json`, so those name two
                              flags each.

    ``--only`` is judged by ``only_set`` (the parsed names), not by the raw
    string, so `--only ""` — which #1620 pins as "no restriction" — is not a
    conflict with anything.

    Callers were measured before any pair was rejected: the two automatic
    invocations (Makefile `lint-docs`, docs-ci.yaml `drift-checks`) pass
    `--only` with `--ci` or with `$(ARGS)`, and no file in the repo passes
    any pair listed here outside this tool's own tests.
    """
    pairs = []
    if args.smart and only_set:
        pairs.append(("--only", "--smart",
                      "the git-diff selection is never computed; --only "
                      "alone decides what runs"))
    if args.parallel and args.ci:
        pairs.append(("--ci", "--parallel",
                      "--ci runs sequentially so it can stop at the first "
                      "failure; nothing runs in parallel"))
    if args.baseline and args.compare:
        pairs.append(("--baseline", "--compare",
                      "the baseline is overwritten and no comparison is "
                      "printed"))
    if args.watch and only_set:
        pairs.append(("--watch", "--only",
                      "watch mode re-runs whatever the changed files map "
                      "to; --only never narrows it"))
    if args.watch:
        for dest, flag in _WATCH_NEVER_READS.items():
            if getattr(args, dest):
                pairs.append(("--watch", flag,
                              "watch mode reads only --skip and --verbose "
                              "and returns before the summary, so "
                              f"{flag} is never consulted"))
    return pairs


def _conflicting_flags_message(pairs):
    """Operator-facing text for a non-empty _conflicting_flags() result.

    Names BOTH flags and which one would have been ignored — the whole
    point is that the operator believed the ignored flag was in effect.
    ASCII only, for the same cp950-console reason as
    _unknown_check_names_message.
    """
    lines = []
    for kept, ignored, effect in pairs:
        lines.append(
            f"error: {kept} and {ignored} cannot be combined: {ignored} "
            f"would be ignored ({effect}).")
    lines.append("  Drop one flag of each pair. Before #1695 this ran at "
                 "exit 0 with the ignored flag silently dropped.")
    return "\n".join(lines)


def _run_one(
    short_name: str,
    script_path: str,
    tool_args: List[str],
    cwd: str,
) -> Tuple[str, str, float, str, str]:
    """Run a single validation tool (picklable for ProcessPoolExecutor).

    Returns:
        Tuple of (short_name, status, elapsed, detail, full_output)
    """
    start = time.time()
    try:
        result = subprocess.run(
            [sys.executable, script_path] + tool_args,
            capture_output=True,
            text=True,
            # PRE-EXISTING BUG, fixed in passing (#1267). `text=True` without an
            # explicit encoding decodes with locale.getpreferredencoding(). On a
            # Windows cp950/cp936 host that is NOT utf-8, so the first "✅" a child
            # prints (0xe2 …) raises UnicodeDecodeError inside subprocess's reader
            # thread; `result.stdout` then comes back as None and _extract_detail()
            # dies with AttributeError — taking the WHOLE validate_all run down, not
            # just the offending check. Reproduced on unmodified main with
            # generate_tool_map.py, i.e. `make lint-docs` was broken for every tool
            # on such a host. The children already emit utf-8 (try_utf8_stdout /
            # _force_utf8_streams); only the parent's decode side was wrong.
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT_SECONDS,
            cwd=cwd,
        )
        elapsed = time.time() - start

        if result.returncode == 0:
            detail = _extract_detail(result.stdout)
            return short_name, "pass", elapsed, detail, result.stdout
        else:
            # #1697: the SAME predicate as the pass branch. A failure used to
            # quote the FIRST stdout line, so a tool that prints progress
            # first and the reason last — this repo's norm — produced
            #     ✗ tool_map ... (✅ Tool map (zh) is up to date.)
            # the symbol saying fail and the parenthesis saying pass. The
            # predicate itself (and why it is not "prefer an error marker")
            # is justified on _extract_detail. The `Exit code` fallback
            # stays for a tool that wrote nothing usable to stdout — and
            # that is common: 9 of the 15 induced failures measured for
            # #1697 put their reason on stderr only (glossary, alerts,
            # rule_packs, changelog, rule_pack_stats, byo_rulepack_table,
            # repo_name, hardcode_tenant, changelog_no_tbd). Reading stderr
            # here is a separate change and is deliberately not made.
            detail = (_extract_detail(result.stdout)
                      or f"Exit code: {result.returncode}")
            return short_name, "fail", elapsed, detail, result.stdout

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return short_name, "error", elapsed, "Timeout after 120s", ""
    except (OSError, subprocess.SubprocessError) as e:
        elapsed = time.time() - start
        return short_name, "error", elapsed, str(e)[:80], ""


# A line made only of rule / border characters (`===`, `---`, `───`,
# `+---+`, `...`). Such a line says nothing about the run, so it is never
# the detail. This is in ADDITION to the older `===`-prefix rule, which
# also catches banners with words in them (`=== END ===`).
_DECORATIVE_LINE = re.compile(r"^[\s=\-─_*#.~|+]*$")


def _extract_detail(output: str) -> str:
    """Last meaningful line of a tool's stdout, cut to 80 characters.

    Since #1697 this is the detail for BOTH verdicts; a failure used to quote
    the opening line instead (see _run_one). The predicate was chosen after
    measuring, as the issue asked, not before: every TOOLS row was run with
    its registered args, and 15 failures were induced in a scratch copy of
    the repo. In all 15 the last meaningful stdout line WAS the reason
    (`❌ docs/internal/tool-map.en.md is outdated …`, `❌ 34 error(s), 0
    warning(s)`). A marker-preferring variant — walk from the end, prefer
    the last line containing ❌ / ✗ / ERROR / error: / FAIL / Traceback —
    picked the identical line in 15/15, so it buys nothing measurable, and
    it has a measured false-positive source: jsx_babel prints `✗ 361
    browser-incompatible pattern(s) found:` on a GREEN run, so a marker in
    the middle of an output proves nothing about the verdict. The simpler
    predicate is the one kept. Tracebacks are on stderr and never reach
    this function either way.

    Truncation is by CHARACTER: this is str slicing, so a CJK line is never
    cut inside a character and the result always re-encodes as valid UTF-8
    (#1697's "half a character" worry describes BYTE slicing, which this
    never did; pinned by TestExtractDetail). What a character cut can still
    do is cosmetic: separate a combining mark or VS16 — the second code
    point of ⚠️ — from its base when it sits exactly at the boundary.
    """
    lines = output.strip().split("\n")
    for line in reversed(lines):
        line = line.strip()
        if (line and not line.startswith("===")
                and not _DECORATIVE_LINE.match(line)):
            return line[:80]
    return ""


def _effective_mode(args) -> str:
    """``"parallel"`` or ``"sequential"`` — the branch main() actually takes.

    #1705: the report header, the JSON ``mode`` field and the profile CSV
    ``mode`` column each read ``args.parallel`` on their own, while the
    dispatch read ``args.parallel and not args.ci``. So ``--ci --parallel``
    ran sequentially and wrote ``parallel`` into all three. One derivation,
    used by the dispatch AND by every label, is what keeps them from
    disagreeing again. (The pair itself is now rejected at exit 2 by
    _conflicting_flags, so the two conditions coincide today; the labels
    still read this function rather than the flag so that stays true if
    the guard is ever relaxed.)
    """
    return "parallel" if (args.parallel and not args.ci) else "sequential"


def _status_symbol(status: str) -> str:
    """Return symbol for status."""
    return {"pass": "✓", "fail": "✗"}.get(status, "⊘")


def _format_time(elapsed: float) -> str:
    """Format elapsed time."""
    return f"{elapsed:.1f}s"


# ---------------------------------------------------------------------------
# --watch: file-change → affected-check mapping
# ---------------------------------------------------------------------------
# Maps file path patterns to the check names they affect.
# Patterns are prefix-matched against relative paths.
WATCH_TRIGGERS: Dict[str, List[str]] = {
    "docs/": ["links", "translation", "freshness", "includes", "versions",
              "doc_map", "tool_consistency", "bilingual_content",
              "frontmatter_versions", "byo_rulepack_table", "cli_default_drift",
              "cli_contract"],
    "docs/assets/": ["platform_data", "tool_consistency"],
    # ⚠️ byo_rulepack_table 的主要來源其實是 k8s/03-monitoring/deployment-prometheus.yaml，
    # 而本 dict 沒有 k8s/ 這個 key（rule_pack_stats 也有同樣的既有缺口）。watch 模式因此
    # 不會因為改 projected volume 而重跑；pre-commit 的 files: 與 CI lint job 兩者都涵蓋，
    # 故強制力未流失，僅 watch 便利性打折。
    "rule-packs/": ["alerts", "rule_packs", "rule_pack_stats", "byo_rulepack_table", "versions",
                    "platform_data"],
    # ⛔ cli_default_drift 兩側都要掛：它比對「文件的預設值欄」與「argparse 的
    # 真實 default」，任一側改動都會造成漂移。只掛 docs/ 會讓「改了 argparse
    # 預設值、沒動文件」這個方向在 watch/smart 模式下完全沒有偵測。
    # cli_contract likewise compares docs against argparse and COMMAND_MAP
    # (components/), so it hangs on every side.
    "scripts/tools/": ["tool_map", "cli_coverage", "cli_default_drift", "cli_contract"],
    "CLAUDE.md": ["versions", "doc_map"],
    "CHANGELOG.md": ["changelog", "changelog_format"],
    "CHANGELOG.en.md": ["changelog", "changelog_format"],
    "CHANGELOG-archive.md": ["changelog_format"],
    # An EMPTY list means "changes here affect no check", and since #1704
    # that is honoured: a diff touching only this file runs nothing, it no
    # longer falls through to "run everything" (see _selection_outcome).
    ".pre-commit-config.yaml": [],
    "components/": ["versions", "cli_coverage", "cli_contract"],
    "mkdocs.yml": ["versions"],
}


def _send_notification(title: str, message: str, bell_to=None) -> None:
    """Send an OS-native desktop notification (best-effort, cross-platform).

    Supported backends (tried in order):
    - Linux: notify-send (libnotify)
    - macOS: osascript (AppleScript)
    - Windows: PowerShell toast notification via BurntToast or fallback
    - Fallback: terminal bell (\\a), written to `bell_to` (default: the
      current sys.stdout). main() passes stderr under --json so the bell
      cannot land after the JSON document (#1772).
    """
    import platform
    system = platform.system()

    try:
        if system == "Linux":
            subprocess.run(
                ["notify-send", "--app-name=validate_all", title, message],
                timeout=5,
                capture_output=True,
            )
            return
        if system == "Darwin":
            script = (
                f'display notification "{message}" '
                f'with title "{title}"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                timeout=5,
                capture_output=True,
            )
            return
        if system == "Windows":
            ps_cmd = (
                f'[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null; '
                f'$n = New-Object System.Windows.Forms.NotifyIcon; '
                f'$n.Icon = [System.Drawing.SystemIcons]::Information; '
                f'$n.Visible = $true; '
                f'$n.ShowBalloonTip(5000, "{title}", "{message}", '
                f'[System.Windows.Forms.ToolTipIcon]::Info)'
            )
            subprocess.run(
                ["powershell", "-Command", ps_cmd],
                timeout=10,
                capture_output=True,
            )
            return
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Fallback: terminal bell. sys.stdout is resolved at CALL time, not at
    # definition time -- pytest's capsys swaps it per test.
    print("\a", end="", flush=True,
          file=bell_to if bell_to is not None else sys.stdout)


def _snapshot_mtimes(repo_root: Path) -> Dict[str, float]:
    """Build a dict of relative_path → mtime for watched files.

    Path keys always use forward slashes (POSIX-style) so the snapshot is
    cross-platform identical: snapshots taken on Windows/macOS/Linux can
    be diffed without separator translation. `Path.as_posix()` does this
    portably without any string mangling.
    """
    snap: Dict[str, float] = {}
    watch_dirs = ["docs", "rule-packs", "scripts/tools", "components"]
    # ⛔ A file that is MAPPED to a check but not watched can never trigger it
    # in `--watch`: the mapping reads as coverage that does not exist.
    watch_files = ["CLAUDE.md", "CHANGELOG.md", "CHANGELOG.en.md",
                   "CHANGELOG-archive.md",
                   "mkdocs.yml", ".pre-commit-config.yaml"]

    for wf in watch_files:
        p = repo_root / wf
        if p.exists():
            snap[wf] = p.stat().st_mtime

    for wd in watch_dirs:
        base = repo_root / wd
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix not in (".md", ".py", ".yaml", ".yml", ".json",
                                ".jsx"):
                continue
            rel = f.relative_to(repo_root).as_posix()
            snap[rel] = f.stat().st_mtime

    return snap


class SmartSelection(NamedTuple):
    """What a set of changed files says about which checks to run (#1704).

    ``checks``     sorted union of the WATCH_TRIGGERS lists of every file
                   that matched a prefix
    ``unmatched``  changed files matching NO prefix -- their impact is
                   unknown, which is not the same thing as "affects nothing"
    ``changed``    every changed file, sorted, so a message can name them

    Deliberately no "run everything" fallback in here: that fallback,
    applied inside the detector, is what turned an explicit empty set into
    "run all 33" before #1704. The caller classifies (_selection_outcome)
    and says why.
    """
    checks: List[str]
    unmatched: List[str]
    changed: List[str]


def _select_checks(changed) -> SmartSelection:
    """Map changed paths to checks through WATCH_TRIGGERS.

    The ONE derivation behind both ``--smart`` and ``--watch``. A file that
    matches a prefix whose list is empty (``.pre-commit-config.yaml``) is
    MATCHED and contributes nothing -- it lands in ``checks`` as nothing, not
    in ``unmatched``.
    """
    changed = sorted(set(changed))
    affected: set = set()
    unmatched: List[str] = []
    for cf in changed:
        matched = False
        for prefix, checks in WATCH_TRIGGERS.items():
            if cf.startswith(prefix) or cf == prefix.rstrip("/"):
                affected.update(checks)
                matched = True
        if not matched:
            unmatched.append(cf)
    return SmartSelection(sorted(affected), unmatched, changed)


OUTCOME_UNKNOWN = "unknown"
OUTCOME_EMPTY = "empty"
OUTCOME_SELECTED = "selected"


def _selection_outcome(sel: Optional[SmartSelection]) -> Tuple[str, List[str]]:
    """``(outcome, checks)`` -- the three outcomes #1704 asked to keep apart.

    ``unknown``   git could not answer (``sel is None``), or at least one
                  changed file matches no trigger: the impact is unknown, so
                  fail safe -- every registered check. The caller has to say
                  WHY and name the files.
    ``empty``     nothing changed, or every changed file matched a trigger
                  and the union of their lists is empty: an explicit empty
                  set, so run nothing.
    ``selected``  a non-empty selection -- exactly those checks.

    Before #1704 the second and first outcomes collapsed into each other at
    three sites (``sorted(affected) if affected else <all>`` in both
    detectors, and ``only_set`` empty meaning "no restriction" in main()),
    so a clean tree and an unknown file both ran everything and only one of
    them had a reason to.
    """
    if sel is None or sel.unmatched:
        return OUTCOME_UNKNOWN, [n for n, _, _, _ in TOOLS]
    if not sel.checks:
        return OUTCOME_EMPTY, []
    return OUTCOME_SELECTED, list(sel.checks)


def _name_files(paths: List[str], cap: int = 10) -> str:
    """``a, b, c`` or ``a, ..., j, ... and N more`` -- for messages."""
    if len(paths) <= cap:
        return ", ".join(paths)
    return ", ".join(paths[:cap]) + f", ... and {len(paths) - cap} more"


def _unmatched_clause(sel: Optional[SmartSelection]) -> str:
    """The WHY for OUTCOME_UNKNOWN, naming the files. ASCII only: under
    ``--json`` this goes to stderr, which is not utf-8-patched."""
    if sel is None:
        return "git could not be read"
    return (f"{len(sel.unmatched)} changed file(s) match no known trigger "
            f"({_name_files(sel.unmatched)})")


def _detect_changed_checks(old_snap: Dict[str, float],
                           new_snap: Dict[str, float]) -> SmartSelection:
    """Compare two snapshots and map the changed files to checks.

    Returns a SmartSelection; ``changed`` empty means nothing changed. No
    "run all" fallback here any more (#1704) -- the caller classifies via
    _selection_outcome, exactly as ``--smart`` does.
    """
    changed_files = set()
    for rel, mtime in new_snap.items():
        if rel not in old_snap or old_snap[rel] != mtime:
            changed_files.add(rel)
    # Also detect deletions
    for rel in old_snap:
        if rel not in new_snap:
            changed_files.add(rel)
    return _select_checks(changed_files)


def _run_watch(args, tools_dir: Path, project_root: Path) -> None:
    """Watch mode: poll for changes and re-run affected checks.

    Reads ``args.skip`` and ``args.verbose`` and nothing else from ``args``
    -- every other flag is rejected with ``--watch`` by _conflicting_flags
    (#1695 family 2), and the test suite derives that set from the
    ``args.<attr>`` reads in THIS function, so adding a read here is what
    makes a flag legal alongside ``--watch``.
    """
    poll_interval = 2  # seconds

    print("=" * 60)
    print("  Watch mode — polling for file changes (Ctrl+C to stop)")
    print("=" * 60)
    print()

    snap = _snapshot_mtimes(project_root)
    print(f"  Watching {len(snap)} files...", flush=True)
    print(flush=True)

    skip_set = set(
        s.strip() for s in args.skip.split(",") if s.strip())

    try:
        while True:
            time.sleep(poll_interval)
            new_snap = _snapshot_mtimes(project_root)
            sel = _detect_changed_checks(snap, new_snap)

            if not sel.changed:
                continue
            snap = new_snap

            # The same three outcomes as --smart (#1704). Before, an
            # explicit empty set (only .pre-commit-config.yaml touched)
            # re-ran every check with no reason given.
            outcome, chosen = _selection_outcome(sel)
            print(f"\n{'─'*60}")
            if outcome == OUTCOME_EMPTY:
                print(f"  {len(sel.changed)} file(s) changed "
                      f"({_name_files(sel.changed)}) → nothing to re-run: "
                      f"they affect no check")
                print(f"{'─'*60}\n")
                print("  Watching... (Ctrl+C to stop)")
                continue

            runnable = [(n, s, a, d)
                        for n, s, a, d in TOOLS
                        if n in chosen and n not in skip_set]
            if outcome == OUTCOME_UNKNOWN:
                print(f"  {_unmatched_clause(sel)} → impact unknown, "
                      f"re-running all {len(runnable)} check(s)")
            else:
                print(f"  {len(sel.changed)} file(s) changed → running: "
                      f"{', '.join(n for n, _, _, _ in runnable)}")
            print(f"{'─'*60}\n")

            for short_name, script_name, tool_args, _ in runnable:
                script_path = str(tools_dir / script_name)
                _, status, elapsed, detail, full_out = _run_one(
                    short_name, script_path, tool_args,
                    str(project_root))
                if args.verbose and full_out:
                    print(f"\n--- {short_name.upper()} ---")
                    print(full_out)
                sym = _status_symbol(status)
                detail_str = f" ({detail})" if detail else ""
                print(f"  {sym} {short_name:20} ... "
                      f"{_format_time(elapsed)}{detail_str}")

            print("\n  Watching... (Ctrl+C to stop)")

    except KeyboardInterrupt:
        print("\n\n  Watch mode stopped.")


def _smart_detect(project_root: Path) -> Optional[SmartSelection]:
    """Map git's view of the working tree (HEAD diff, index, untracked) to
    checks.

    Returns a SmartSelection, or None when git could not answer -- a
    command that could not run OR one that ran and failed (not a repo,
    unborn HEAD). Both are "no information", and since #1704 an empty
    selection means "run nothing", so a failed probe must not read as a
    clean tree.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            cwd=str(project_root),
        )
        staged = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            cwd=str(project_root),
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            cwd=str(project_root),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    # A probe that ran and failed used to be skipped silently, which left
    # `changed` empty -- harmless while empty meant "run all", wrong now
    # that it means "run nothing".
    if any(r.returncode != 0 for r in (result, staged, untracked)):
        return None

    changed = set()
    for r in (result, staged, untracked):
        changed.update(
            ln.strip() for ln in r.stdout.splitlines() if ln.strip())
    return _select_checks(changed)


def _compare_baseline(current: dict) -> None:
    """Compare current results against saved baseline. Prints to stderr."""
    if not BASELINE_FILE.exists():
        print("\n⚠️  No baseline file found. Run with --baseline first.",
              file=sys.stderr)
        return

    baseline = json_mod.loads(
        BASELINE_FILE.read_text(encoding="utf-8"))

    print("\n" + "=" * 60, file=sys.stderr)
    print("Baseline Comparison", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    # Status changes (regression / improvement / vanished / new)
    b_results = baseline.get("results", {})
    c_results = current.get("results", {})
    regressions = []
    improvements = []
    new_checks = []

    for name in sorted(set(b_results) | set(c_results)):
        b_status = b_results.get(name, {}).get("status", "N/A")
        c_status = c_results.get(name, {}).get("status", "N/A")
        if b_status == "pass" and c_status in ("fail", "error"):
            regressions.append(f"  ✗ {name}: {b_status} → {c_status}")
        elif b_status != "N/A" and c_status == "N/A":
            # #1703. Only the two branches above existed, so a check that
            # was in the baseline and is NOT in this run fell through both,
            # `regressions` stayed empty and the verdict line below said
            # "No regressions detected" — directly under a "3 pass → 2
            # pass" it did not explain. That is the reported shape of this
            # whole family ("required check green, but one check never
            # ran"), and this was its only detector. It is counted as a
            # regression so the verdict cannot say green, and the message
            # says VANISHED rather than failed: someone running `--only` on
            # purpose can read that and move on, someone in the incident
            # needs the name. A baseline fail/error that vanishes is
            # listed too — removing a red check from the set is the same
            # incident with a stronger motive.
            regressions.append(
                f"  ✗ {name}: {b_status} in the baseline, absent from this "
                f"run — it vanished from the run set (not selected), it did "
                f"not fail")
        elif b_status in ("fail", "error") and c_status == "pass":
            improvements.append(f"  ✓ {name}: {b_status} → {c_status}")
        elif b_status == "N/A" and c_status != "N/A":
            # The reverse direction, reported under its own neutral heading
            # (#1703): a check the baseline never saw is neither a
            # regression nor an improvement, but staying silent about it
            # is how the two sides of a comparison drift apart unnoticed.
            new_checks.append(
                f"  + {name}: not in the baseline, {c_status} in this run")

    if regressions:
        print("\n🔴 Regressions:", file=sys.stderr)
        for r in regressions:
            print(r, file=sys.stderr)
    if improvements:
        print("\n🟢 Improvements:", file=sys.stderr)
        for r in improvements:
            print(r, file=sys.stderr)
    if new_checks:
        print("\n🆕 New checks (not in the baseline):", file=sys.stderr)
        for r in new_checks:
            print(r, file=sys.stderr)

    # Timing comparison (>20% slower = warning)
    timing_warnings = []
    for name in sorted(set(b_results) & set(c_results)):
        b_time = b_results[name].get("elapsed", 0)
        c_time = c_results[name].get("elapsed", 0)
        if b_time > 0.5 and c_time > b_time * 1.2:
            pct = ((c_time - b_time) / b_time) * 100
            timing_warnings.append(
                f"  ⏱  {name}: {b_time:.1f}s → {c_time:.1f}s "
                f"(+{pct:.0f}%)")

    if timing_warnings:
        print("\n⏱  Timing warnings (>20% slower):", file=sys.stderr)
        for w in timing_warnings:
            print(w, file=sys.stderr)

    # Summary
    b_pass = baseline.get("passed", 0)
    c_pass = current.get("passed", 0)
    b_fail = baseline.get("failed", 0)
    c_fail = current.get("failed", 0)
    print(f"\n  Baseline: {b_pass} pass / {b_fail} fail", file=sys.stderr)
    print(f"  Current:  {c_pass} pass / {c_fail} fail", file=sys.stderr)

    if not regressions and not timing_warnings:
        print("\n✅ No regressions detected.", file=sys.stderr)
    print("=" * 60, file=sys.stderr)


def _unstaged_tracked_files(project_root: Path):
    """Tracked files whose working-tree copy differs from the index.

    This approximates what the ``git checkout .`` below overwrites: that
    command restores from the index, so staged content survives and untracked
    files are left alone. Deriving the precondition from what the destructive
    command actually destroys, rather than from a general "is the tree clean",
    is what keeps it from refusing on the untracked scratch files every
    worktree has. ``-z`` because git C-quotes non-ASCII paths otherwise, and
    the names this prints are the whole point of the refusal.

    ⛔ THIS IS NOT PROTECTION — it narrows an unconditional loss to a race.
    Two gaps still lose data, and both are open in #1706: the probe samples
    one instant while each fix command below runs before the restore (TOCTOU),
    and ``--assume-unchanged`` files are absent here while ``git checkout .``
    still overwrites them. Do not cite this function as proof of safety.

    Returns ``None`` when git cannot answer. A probe that did not run is not
    evidence of a clean tree, and the caller treats it the same as dirty.
    """
    try:
        probe = subprocess.run(
            ["git", "diff", "--name-only", "-z"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, cwd=str(project_root),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    return [p for p in probe.stdout.split("\0") if p]


def _generate_diff_report(failed_checks: dict, tools_dir: Path,
                          project_root: Path) -> str:
    """Generate unified diff for failed checks that have fix commands.

    For each failed check with a fix command:
    1. Capture current state of potentially affected files
    2. Run the fix command
    3. Capture git diff
    4. Restore original files

    Step 4 is ``git checkout .`` at the repo root and step 3's diff is
    whole-tree, so this mode refuses to start when any tracked file has
    unstaged changes: it would both overwrite them and report them as the
    fix's own (#1706).

    Returns formatted diff report string.
    """
    lines = []
    lines.append("")
    lines.append("=" * 60)
    lines.append("Diff Report (what --fix would change)")
    lines.append("=" * 60)

    fixable = {n for n in failed_checks if n in FIX_COMMANDS}
    if not fixable:
        lines.append("  No auto-fixable checks failed.")
        return "\n".join(lines)

    # Checked after the no-op case above: with nothing to run there is nothing
    # to restore, so there is nothing to refuse.
    dirty = _unstaged_tracked_files(project_root)
    if dirty is None or dirty:
        lines.append("")
        lines.append("  Refusing to run. This mode runs each fix command and then")
        lines.append("  restores with `git checkout .`, which overwrites EVERY")
        lines.append("  unstaged change to a tracked file, not only the ones the fix")
        lines.append("  touched. The report would be wrong too: the `git diff` it")
        lines.append("  prints would carry your edits mixed in with the fix's.")
        if dirty is None:
            lines.append("")
            lines.append("  `git diff --name-only` could not be read here, so an")
            lines.append("  unstaged change cannot be ruled out. Get that command")
            lines.append("  working in this directory first — committing or stashing")
            lines.append("  is not the problem here. (#1706)")
        else:
            lines.append("")
            lines.append(f"  {len(dirty)} tracked file(s) with unstaged changes:")
            for path in dirty[:20]:
                lines.append(f"    {path}")
            if len(dirty) > 20:
                lines.append(f"    ... and {len(dirty) - 20} more")
            lines.append("")
            lines.append("  Run `git add -u` and re-run. Staging is the lossless")
            lines.append("  route: `git checkout .` restores FROM the index, so your")
            lines.append("  edits survive it, and the diff you get back is then the")
            lines.append("  fix's alone. Untracked files are unaffected either way.")
            lines.append("  ⛔ `git stash` is NOT equivalent: if your edit is what")
            lines.append("  made a check fail, stashing it makes the failure go away")
            lines.append("  and this report is never produced. (#1706)")
        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)

    for name in sorted(fixable):
        cmd = FIX_COMMANDS[name]
        script_path = str(tools_dir / cmd[0])
        fix_args = cmd[1:]

        lines.append(f"\n--- {name} ---")

        try:
            # Run fix command
            subprocess.run(
                [sys.executable, script_path] + fix_args,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                cwd=str(project_root),
            )

            # Capture diff
            diff_result = subprocess.run(
                ["git", "diff", "--no-color"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                cwd=str(project_root),
            )

            if diff_result.stdout.strip():
                lines.append(diff_result.stdout.rstrip())
            else:
                lines.append("  (no diff produced — fix may need manual review)")

            # Restore changed files
            subprocess.run(
                ["git", "checkout", "."],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                cwd=str(project_root),
            )
        except subprocess.TimeoutExpired:
            lines.append("  (timeout running fix command)")
            # Attempt restore anyway
            subprocess.run(
                ["git", "checkout", "."],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                cwd=str(project_root),
            )
        except (OSError, subprocess.SubprocessError) as e:
            lines.append(f"  (error: {e})")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    """CLI entry point: Unified validation entry point for all documentation and config validation tools."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Unified validation for documentation and configuration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="Stop at the first failure (exit 1); the summary, --json, "
             "--fix, --profile, --notify and --diff-report still run, and "
             "the checks not reached are listed. Always sequential, so "
             "combining it with --parallel is a caller error (exit 2)",
    )
    parser.add_argument(
        "--parallel", action="store_true",
        help="Run all checks in parallel (faster, ~40-60%% speedup)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show each tool's full output",
    )
    parser.add_argument(
        "--skip", type=str, default="",
        help="Comma-separated list of tools to skip (e.g. links,mermaid). "
             "A name no check answers to is a caller error (exit 2); "
             "--list prints the registered names",
    )
    parser.add_argument(
        "--only", type=str, default="",
        help="Comma-separated list of tools to run (e.g. versions,tool_map). "
             "A name no check answers to is a caller error (exit 2); "
             "--skip still subtracts from this list",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List available check names and exit",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--baseline", action="store_true",
        help="Save --json output as .validation-baseline.json for regression detection",
    )
    parser.add_argument(
        "--compare", action="store_true",
        help="Compare --json output against saved baseline (shows regressions/improvements)",
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="Auto-fix all drift by running --generate/--fix for failed checks",
    )
    parser.add_argument(
        "--profile", action="store_true",
        help="Append per-check timing to .validation-profile.csv for trend tracking",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Watch mode: poll for file changes and re-run the affected "
             "checks. Reads only --skip and --verbose; any other flag would "
             "be ignored, so combining one with --watch is a caller error "
             "(exit 2)",
    )
    parser.add_argument(
        "--smart", action="store_true",
        help="Select checks from git diff HEAD (staged, unstaged, untracked). "
             "Three outcomes: a changed file no trigger knows -> impact "
             "unknown, every check runs and the file is named; a clean tree "
             "or files that affect no check -> nothing runs (exit 0); "
             "otherwise only the affected checks",
    )
    parser.add_argument(
        "--diff-report", action="store_true",
        help="Show unified diff of what --fix would change for failed checks. "
             "Refuses to run while any tracked file has unstaged changes: it "
             "restores with `git checkout .` afterwards (#1706)",
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="Send desktop notification when validation completes "
             "(useful with --watch or long-running parallel runs)",
    )

    args = parser.parse_args()

    # --baseline and --compare imply --json
    if args.baseline or args.compare:
        args.json = True

    # --list: show available checks
    if args.list:
        for short_name, script_name, _, desc in TOOLS:
            print(f"  {short_name:20} {desc} ({script_name})")
        return

    skip_set = set(s.strip() for s in args.skip.split(",") if s.strip())
    only_set = set(s.strip() for s in args.only.split(",") if s.strip())

    # #1620: reject names nothing answers to, BEFORE every mode dispatch
    # EXCEPT `--list` -- that one returns above this point on purpose, so
    # the remedy this message points at still works while the invocation
    # is broken (pinned by test_list_still_works_alongside_a_bad_only, and
    # `--list`/`--watch` are the named exclusions in the test file's
    # _parser_boolean_flags).
    # Parsed here rather than after the --watch branch so that
    # `--watch --skip <typo>` is rejected too — watch mode reads args.skip
    # itself and would otherwise drop the name in its own filter.
    problems = _unknown_check_names(only_set, skip_set)
    if problems:
        print(_unknown_check_names_message(problems), file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # #1695 family 1: a flag pair where one flag silently loses. Sits AFTER
    # the name guard on purpose — a bad name is the more basic error, and
    # its message is pinned byte-for-byte across modes (including
    # `--watch --only <name>`), so it has to be the first thing said.
    conflicts = _conflicting_flags(args, only_set)
    if conflicts:
        print(_conflicting_flags_message(conflicts), file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # #1772: under --json, stdout is ONE JSON document and nothing else.
    # Two kinds of human text exist once the parser has accepted the flags:
    #   - the report body (banner, per-check lines, summary): the JSON
    #     document IS that information in another shape, so it is
    #     suppressed (`if not args.json:`), not duplicated;
    #   - what the tail flags say (--verbose full output, the --diff-report
    #     text, the --fix log, the --profile / --baseline file notices, the
    #     --notify bell): none of it has a JSON representation, so it is
    #     REDIRECTED to stderr rather than lost.
    # `say` is the only stream those tail prints may use -- the same rule the
    # --smart notice and the #1620 / #1695 error paths already follow. stderr
    # is not utf-8-patched (see _unknown_check_names_message), so on a cp950
    # console the emoji in these lines degrade to `\uXXXX` under --json; that
    # is cosmetic, the trade try_utf8_stdout documents.
    say = sys.stderr if args.json else sys.stdout

    tools_dir = Path(__file__).parent
    project_root = tools_dir.parent.parent
    os.chdir(project_root)

    # --watch: enter watch mode
    if args.watch:
        _run_watch(args, tools_dir, project_root)
        return

    # --smart: derive the selection from git diff (#1704). `--smart --only
    # <names>` is rejected above, so only_set is empty here. The selection
    # is NOT poured into only_set any more: an empty only_set means "no
    # restriction" below, which is how an explicit empty selection used to
    # become "run all 33" (#1620's stopgap only corrected the printed
    # number). `smart_restrict is None` = no restriction; a set, possibly
    # EMPTY, = run exactly these.
    smart_restrict: Optional[set] = None
    if args.smart:
        sel = _smart_detect(project_root)
        outcome, chosen = _selection_outcome(sel)
        # Printed through the shared `say` (stderr under --json); ASCII
        # because stderr is not utf-8-patched (see
        # _unknown_check_names_message).
        if outcome == OUTCOME_UNKNOWN:
            # ⛔ Announce from the SAME filter the run uses, `--skip`
            # included -- `--smart --skip a,b,c` once said 33 and ran 30.
            will_run = [n for n, _s, _a, _d in TOOLS if n not in skip_set]
            print(f"Smart mode: {_unmatched_clause(sel)} -- impact "
                  f"unknown, running all {len(will_run)} check(s)"
                  f"{' not skipped' if skip_set else ''}\n", file=say)
        elif outcome == OUTCOME_EMPTY:
            smart_restrict = set()
            if sel.changed:
                reason = (f"the {len(sel.changed)} changed file(s) "
                          f"({_name_files(sel.changed)}) affect no check")
            else:
                reason = "the working tree has no changes against HEAD"
            print(f"Smart mode: nothing to run -- {reason}. Use --only to "
                  f"pick checks or drop --smart to run everything.\n",
                  file=say)
        else:
            smart_restrict = set(chosen)
            will_run = [n for n, _s, _a, _d in TOOLS
                        if n in smart_restrict and n not in skip_set]
            print(f"Smart mode: running {len(will_run)} check(s) based on "
                  f"git diff: {', '.join(will_run)}\n", file=say)

    # Filter to runnable tools. `--skip` subtracts in EVERY branch (#1620):
    # it used to be ignored whenever `--only` was present, while the
    # skipped-items loop below printed `... skipped` for it regardless, so one
    # invocation reported both outcomes for the same check —
    #   `--only versions --skip versions`
    #     ⊘ versions  ... skipped
    #     ✓ versions  ... 2.7s
    # An empty result is still exit 0: every name existed, and asking for an
    # empty intersection -- or a `--smart` diff that affects no check -- is a
    # request to run nothing, not a bad invocation.
    if smart_restrict is not None:
        runnable = [(n, s, a, d) for n, s, a, d in TOOLS
                    if n in smart_restrict and n not in skip_set]
    elif only_set:
        runnable = [(n, s, a, d) for n, s, a, d in TOOLS
                    if n in only_set and n not in skip_set]
    else:
        runnable = [(n, s, a, d) for n, s, a, d in TOOLS if n not in skip_set]
    skipped = len(TOOLS) - len(runnable)

    effective_mode = _effective_mode(args)
    if not args.json:
        print("=" * 60)
        print(f"Documentation & Config Validation Report "
              f"({effective_mode.upper()})")
        print("=" * 60)
        print()

    # Print skipped items
    if not args.json:
        for n, _, _, _ in TOOLS:
            if n in skip_set:
                print(f"{_status_symbol('skip')} {n:20} ... skipped")

    results: Dict[str, Tuple[str, float, str]] = {}
    # #1695 family 2: set when --ci stopped the sequential loop. Both are
    # reported by the tail (text line, JSON keys) and neither goes into
    # `results` -- a not-run check is not a result. `_compare_baseline`
    # therefore lists it as vanished when the baseline had it (#1703), and
    # that is the intended reading: after a --ci stop the rest of the run
    # set genuinely did not run, which a comparison must not paper over.
    ci_stopped_after: Optional[str] = None
    not_run: List[str] = []

    if effective_mode == "parallel":
        # ------------------------------------------------------------------
        # Parallel execution
        # ------------------------------------------------------------------
        wall_start = time.time()
        futures = {}
        with ProcessPoolExecutor() as pool:
            for short_name, script_name, tool_args, _desc in runnable:
                script_path = str(tools_dir / script_name)
                fut = pool.submit(
                    _run_one, short_name, script_path, tool_args,
                    str(project_root),
                )
                futures[fut] = short_name

            for fut in as_completed(futures):
                short_name, status, elapsed, detail, full_out = fut.result()
                results[short_name] = (status, elapsed, detail)
                if args.verbose and full_out:
                    print(f"\n--- {short_name.upper()} ---", file=say)
                    print(full_out, file=say)

        wall_elapsed = time.time() - wall_start

        # Print results in original TOOLS order for consistent output
        if not args.json:
            for short_name, _, _, _ in runnable:
                if short_name not in results:
                    continue
                status, elapsed, detail = results[short_name]
                sym = _status_symbol(status)
                detail_str = f" ({detail})" if detail else ""
                print(f"{sym} {short_name:20} ... {_format_time(elapsed)}"
                      f"{detail_str}")
    else:
        # ------------------------------------------------------------------
        # Sequential execution (default, or --ci which stops at the first
        # failure and then falls through to the same tail as every mode)
        # ------------------------------------------------------------------
        wall_start = time.time()
        for idx, (short_name, script_name, tool_args, _desc) in enumerate(
                runnable):
            script_path = str(tools_dir / script_name)
            _, status, elapsed, detail, full_out = _run_one(
                short_name, script_path, tool_args, str(project_root),
            )
            results[short_name] = (status, elapsed, detail)

            if args.verbose and full_out:
                print(f"\n--- {short_name.upper()} ---", file=say)
                print(full_out, file=say)

            if not args.json:
                sym = _status_symbol(status)
                detail_str = f" ({detail})" if detail else ""
                print(f"{sym} {short_name:20} ... {_format_time(elapsed)}"
                      f"{detail_str}")

            if args.ci and status in ("fail", "error"):
                # #1695 family 2: `break`, not `sys.exit(1)`. The exit here
                # skipped the summary and every flag handled after the
                # loop: `--json` wrote no document exactly when the run
                # failed, and `--fix` / `--profile` / `--notify` /
                # `--diff-report` never ran. The exit code is unchanged --
                # `failed` is >= 1 on this path, so the tail exits 1.
                ci_stopped_after = short_name
                not_run = [n for n, _, _, _ in runnable[idx + 1:]]
                if not args.json:
                    print()
                    print("=" * 60)
                    print(f"CI mode: Stopping after failure ({short_name})")
                    print("=" * 60)
                break

        wall_elapsed = time.time() - wall_start

    # Summary
    passed = sum(1 for s, _, _ in results.values() if s == "pass")
    failed = sum(1 for s, _, _ in results.values() if s in ("fail", "error"))
    total = len(runnable)
    sum_elapsed = sum(e for _, e, _ in results.values())

    if args.json:
        out = {
            "mode": effective_mode,
            "wall_time": round(wall_elapsed, 2),
            "sum_time": round(sum_elapsed, 2),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "total": total,
            # Always present (null / []), so a consumer can read the shape
            # without probing for the key. total == len(results) +
            # len(not_run) whenever ci_stopped_after is set.
            "ci_stopped_after": ci_stopped_after,
            "not_run": not_run,
            "results": {
                n: {"status": s, "elapsed": round(e, 2), "detail": d}
                for n, (s, e, d) in results.items()
            },
        }

        if args.baseline:
            import stat
            BASELINE_FILE.write_text(
                json_mod.dumps(out, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.chmod(BASELINE_FILE,
                     stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                     | stat.S_IROTH)
            print(json_mod.dumps(out, indent=2, ensure_ascii=False))
            print(f"\n📊 Baseline saved to {BASELINE_FILE.name}",
                  file=sys.stderr)
        elif args.compare:
            print(json_mod.dumps(out, indent=2, ensure_ascii=False))
            _compare_baseline(out)
        else:
            print(json_mod.dumps(out, indent=2, ensure_ascii=False))
    else:
        print()
        print("=" * 60)
        if total == 0:
            print("Result: All tools skipped")
        else:
            time_info = ""
            if effective_mode == "parallel":
                time_info = (f"  (wall: {wall_elapsed:.1f}s, "
                             f"sum: {sum_elapsed:.1f}s)")
            else:
                time_info = f"  (total: {wall_elapsed:.1f}s)"
            print(f"Result: {passed}/{total} passed, {failed} failed, "
                  f"{skipped} skipped{time_info}")
        if ci_stopped_after is not None:
            names = f": {', '.join(not_run)}" if not_run else ""
            print(f"CI mode: stopped after {ci_stopped_after}; "
                  f"{len(not_run)} check(s) not run{names}")
        print("=" * 60)

    # --profile: append timing data to CSV
    if args.profile and results:
        import stat
        from datetime import datetime, timezone
        # #1705: ONE column list for the header and for every row. The
        # header used to be written once, on file creation, while each row
        # expanded the CURRENT `TOOLS` — so registering a check made every
        # later row two columns wider than the header, silently, and the
        # same column index then meant different checks in early and late
        # rows. Now the file's header is compared to the derived one and a
        # mismatch ROTATES the old file aside rather than appending to it.
        all_names = [n for n, _, _, _ in TOOLS]
        header = ("timestamp,mode,wall_time," +
                  ",".join(f"{n}_time,{n}_status" for n in all_names))
        if PROFILE_CSV.exists():
            with open(PROFILE_CSV, encoding="utf-8") as csvf:
                existing_header = csvf.readline().rstrip("\r\n")
            if existing_header != header:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                rotated = PROFILE_CSV.with_name(
                    f"{PROFILE_CSV.name}.bak-{stamp}")
                os.replace(PROFILE_CSV, rotated)
                # stderr in every mode: --profile combines with --json,
                # whose stdout has to stay a single JSON document.
                print(f"\n⚠️  {PROFILE_CSV.name}: its header no longer "
                      f"matches the registered checks (a check was added, "
                      f"removed or renamed). Appending would misalign the "
                      f"columns, so the old file was moved to "
                      f"{rotated.name} and a new one starts here (#1705).",
                      file=sys.stderr)
        write_header = not PROFILE_CSV.exists()
        # newline="\n" (not the csv module's newline=""): these rows are
        # written as plain strings with an explicit "\n", so there is no
        # csv.writer adding its own \r\n terminator to preserve.
        with open(PROFILE_CSV, "a", encoding="utf-8", newline="\n") as csvf:
            if write_header:
                csvf.write(header + "\n")
            row_parts = [
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                effective_mode,
                f"{wall_elapsed:.2f}",
            ]
            for n in all_names:
                if n in results:
                    s, e, _ = results[n]
                    row_parts.extend([f"{e:.2f}", s])
                else:
                    row_parts.extend(["", "skip"])
            csvf.write(",".join(row_parts) + "\n")
        os.chmod(PROFILE_CSV,
                 stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                 | stat.S_IROTH)
        print(f"\n📊 Timing appended to {PROFILE_CSV.name}", file=say)

    # --diff-report: show what --fix would change
    if args.diff_report and failed > 0:
        failed_checks = {n: s for n, (s, _, _) in results.items()
                         if s in ("fail", "error")}
        print(_generate_diff_report(
            failed_checks, tools_dir, project_root), file=say)

    # --fix: auto-fix failed checks that have fix commands
    if args.fix and failed > 0:
        print(file=say)
        print("=" * 60, file=say)
        print("Auto-fixing drift...", file=say)
        print("=" * 60, file=say)
        fix_count = 0
        for name, (status, _, _) in results.items():
            if status not in ("fail", "error"):
                continue
            if name not in FIX_COMMANDS:
                print(f"  ⊘ {name:20} ... no auto-fix available", file=say)
                continue
            cmd = FIX_COMMANDS[name]
            script_path = str(tools_dir / cmd[0])
            fix_args = cmd[1:]
            try:
                result = subprocess.run(
                    [sys.executable, script_path] + fix_args,
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                    cwd=str(project_root),
                )
                if result.returncode == 0:
                    detail = _extract_detail(result.stdout)
                    print(f"  🔧 {name:20} ... fixed ({detail})", file=say)
                    fix_count += 1
                else:
                    print(f"  ✗ {name:20} ... fix failed "
                          f"(exit {result.returncode})", file=say)
            except (OSError, subprocess.SubprocessError) as e:
                print(f"  ✗ {name:20} ... fix error: {e}", file=say)

        if fix_count > 0:
            print(f"\n🔧 Fixed {fix_count} check(s). Re-run to verify.",
                  file=say)

    # --notify: send desktop notification
    if args.notify:
        if failed == 0:
            _send_notification(
                "Validation Passed",
                f"All {passed}/{total} checks passed ({wall_elapsed:.1f}s)",
                bell_to=say,
            )
        else:
            _send_notification(
                "Validation Failed",
                f"{failed}/{total} checks failed ({wall_elapsed:.1f}s)",
                bell_to=say,
            )

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
