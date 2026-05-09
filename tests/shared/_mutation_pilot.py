"""Mutation-test pilot runner for the audit's ④ "Better Methods" dimension.

Underscored prefix → pytest does NOT collect this module; it's a
re-runnable research artifact, not part of the test suite. Sits beside
test_property_tools.py for context.

Applies a hand-crafted set of mutations to 4 pure functions; for each
mutation, runs the relevant pytest scope and records whether the suite
caught the mutation (test failed → caught) or missed it (tests still
passed → SURVIVED, gap).

Why hand-crafted vs `mutmut`/`cosmic-ray`:
  - mutmut would be a new project dependency for a one-off audit pilot.
  - Hand-crafted mutations let us focus on MEANINGFUL ones (constants,
    operators, control flow) rather than exhaustive surface mutations
    that produce many equivalent-mutant noise.
  - Output of this script is the audit's reproducible evidence.

Usage:
  python tests/shared/_mutation_pilot.py [--target FUNC]

Latest run (v2.8.0 audit ④ pilot): 12/13 caught (~92%); the 1 survivor
is an equivalent mutation (redundant early-out check). See PR
description / commit message for findings.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Mutation:
    target_file: str        # source file relative to REPO_ROOT
    test_file: str          # pytest target relative to REPO_ROOT
    label: str              # short description
    old: str                # exact string to find
    new: str                # replacement
    fn_name: str            # which target function

    def apply(self) -> None:
        path = REPO_ROOT / self.target_file
        # Read in binary-preserving mode (newline=""), so we don't trash the
        # source file's LF line endings on Windows by accident.
        with open(path, encoding="utf-8", newline="") as f:
            src = f.read()
        if self.old not in src:
            raise ValueError(f"old_string not found in {self.target_file}: {self.label}")
        if src.count(self.old) > 1:
            raise ValueError(f"old_string ambiguous (>1 match) in {self.target_file}: {self.label}")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(src.replace(self.old, self.new))

    def revert(self, original: str) -> None:
        path = REPO_ROOT / self.target_file
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(original)


# ── Mutation catalog ──────────────────────────────────────────────────

MUTATIONS: list[Mutation] = [
    # ── _audience_str (generate_doc_map) ────────────────────────────
    Mutation(
        target_file="scripts/tools/dx/generate_doc_map.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_generate_doc_map.py",
        label="audience: empty-list returns 'None' instead of 'All'",
        fn_name="_audience_str",
        old='if not audience_list:\n        return "All"',
        new='if not audience_list:\n        return "None"',
    ),
    Mutation(
        target_file="scripts/tools/dx/generate_doc_map.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_generate_doc_map.py",
        label="audience: drop default arg in mapping.get",
        fn_name="_audience_str",
        old="parts.append(mapping.get(slug, slug))",
        new="parts.append(mapping.get(slug, ''))",
    ),
    Mutation(
        target_file="scripts/tools/dx/generate_doc_map.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_generate_doc_map.py",
        label="audience: separator ', ' → '/'",
        fn_name="_audience_str",
        old='return ", ".join(parts)',
        new='return "/".join(parts)',
    ),
    Mutation(
        target_file="scripts/tools/dx/generate_doc_map.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_generate_doc_map.py",
        label="audience: invert empty-check",
        fn_name="_audience_str",
        old="if not audience_list:",
        new="if audience_list:",
    ),
    # ── _parse_front_matter (generate_doc_map) ──────────────────────
    Mutation(
        target_file="scripts/tools/dx/generate_doc_map.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_generate_doc_map.py",
        label="frontmatter: skip prefix check (--- not required)",
        fn_name="_parse_front_matter",
        old='if not content.startswith("---"):\n        return {}',
        new='if False:\n        return {}',
    ),
    Mutation(
        target_file="scripts/tools/dx/generate_doc_map.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_generate_doc_map.py",
        label="frontmatter: skip ':' splitter check (allow malformed lines)",
        fn_name="_parse_front_matter",
        old='if ":" not in line:\n            continue',
        new='if False:\n            continue',
    ),
    Mutation(
        target_file="scripts/tools/dx/generate_doc_map.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_generate_doc_map.py",
        label="frontmatter: drop quote stripping",
        fn_name="_parse_front_matter",
        old='val = val.strip().strip(\'"\').strip("\'")',
        new='val = val.strip()',
    ),
    Mutation(
        target_file="scripts/tools/dx/generate_doc_map.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_generate_doc_map.py",
        label="frontmatter: list detection startswith only (no endswith)",
        fn_name="_parse_front_matter",
        old='if val.startswith("[") and val.endswith("]"):',
        new='if val.startswith("["):',
    ),
    # ── parse_commit (generate_changelog) ──────────────────────────
    Mutation(
        target_file="scripts/tools/dx/generate_changelog.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_generate_changelog_extra.py",
        label="commit: scope falls back to None (was '')",
        fn_name="parse_commit",
        old='"scope": m.group("scope") or "",',
        new='"scope": m.group("scope"),',
    ),
    Mutation(
        target_file="scripts/tools/dx/generate_changelog.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_generate_changelog_extra.py",
        label="commit: drop bool() wrapper on breaking",
        fn_name="parse_commit",
        old='"breaking": bool(m.group("breaking")),',
        new='"breaking": m.group("breaking"),',
    ),
    Mutation(
        target_file="scripts/tools/dx/generate_changelog.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_generate_changelog_extra.py",
        label="commit: invert m falsiness check (no-match returns dict)",
        fn_name="parse_commit",
        old="if not m:\n        return None",
        new="if m is None:\n        m = re.match(r'(?P<type>.*)', subject)",
    ),
    # ── extract_metrics_from_expr (generate_rule_pack_split) ──────
    Mutation(
        target_file="scripts/tools/ops/generate_rule_pack_split.py",
        test_file="tests/shared/test_property_tools.py tests/ops/test_generate_rule_pack_split.py",
        label="metrics: drop builtin-fn filter (rate/sum/avg counted as metrics)",
        fn_name="extract_metrics_from_expr",
        old="        if m not in builtin_funcs and not m[0].isupper():",
        new="        if not m[0].isupper():",
    ),
    Mutation(
        target_file="scripts/tools/ops/generate_rule_pack_split.py",
        test_file="tests/shared/test_property_tools.py tests/ops/test_generate_rule_pack_split.py",
        label="metrics: drop uppercase-token filter (labels counted as metrics)",
        fn_name="extract_metrics_from_expr",
        old="        if m not in builtin_funcs and not m[0].isupper():",
        new="        if m not in builtin_funcs:",
    ),
    # ── parse_duration_seconds (_lib_validation) ──────────────────
    Mutation(
        target_file="scripts/tools/_lib_validation.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="duration: drop float from numeric pass-through (only int)",
        fn_name="parse_duration_seconds",
        old="    if isinstance(value, (int, float)):\n        return int(value)",
        new="    if isinstance(value, int):\n        return int(value)",
    ),
    Mutation(
        target_file="scripts/tools/_lib_validation.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="duration: drop type-check (let m.match raise on non-string)",
        fn_name="parse_duration_seconds",
        old="    if not value or not isinstance(value, str):\n        return None",
        new="    if not value:\n        return None",
    ),
    # ── format_duration (_lib_validation) ─────────────────────────
    Mutation(
        target_file="scripts/tools/_lib_validation.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="format: drop modulo check (3600s+1 wrongly emits 'h' rounded)",
        fn_name="format_duration",
        old="    if seconds >= 3600 and seconds % 3600 == 0:",
        new="    if seconds >= 3600:",
    ),
    Mutation(
        target_file="scripts/tools/_lib_validation.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="format: drop minute-modulo check (61s wrongly emits 'm')",
        fn_name="format_duration",
        old="    if seconds >= 60 and seconds % 60 == 0:",
        new="    if seconds >= 60:",
    ),
    # ── is_disabled (_lib_validation) ─────────────────────────────
    Mutation(
        target_file="scripts/tools/_lib_validation.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="is_disabled: drop case-folding (.lower() removed)",
        fn_name="is_disabled",
        old="    return value.strip().lower() in _DISABLED_VALUES",
        new="    return value.strip() in _DISABLED_VALUES",
    ),
    Mutation(
        target_file="scripts/tools/_lib_validation.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="is_disabled: drop whitespace strip",
        fn_name="is_disabled",
        old="    return value.strip().lower() in _DISABLED_VALUES",
        new="    return value.lower() in _DISABLED_VALUES",
    ),
    # ── validate_and_clamp (_lib_validation) ──────────────────────
    Mutation(
        target_file="scripts/tools/_lib_validation.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="clamp: invert lower-bound check (< → <=)",
        fn_name="validate_and_clamp",
        old="    if seconds < min_sec:",
        new="    if seconds <= min_sec:",
    ),
    Mutation(
        target_file="scripts/tools/_lib_validation.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="clamp: invert upper-bound check (> → >=)",
        fn_name="validate_and_clamp",
        old="    if seconds > max_sec:",
        new="    if seconds >= max_sec:",
    ),
    # ── strip_frontmatter (axe_lite_static) ──────────────────────
    Mutation(
        target_file="scripts/tools/dx/axe_lite_static.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_axe_lite_static.py",
        label="frontmatter: search from index 0 (would match opening --- as separator)",
        fn_name="strip_frontmatter",
        old='        end = src.find("\\n---", 3)',
        new='        end = src.find("\\n---", 0)',
    ),
    Mutation(
        target_file="scripts/tools/dx/axe_lite_static.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_axe_lite_static.py",
        label="frontmatter: off-by-one slice end + 4 → end + 3 (loses byte)",
        fn_name="strip_frontmatter",
        old="            return src[end + 4 :]",
        new="            return src[end + 3 :]",
    ),
    # ── scan_unicode_status (axe_lite_static) ────────────────────
    Mutation(
        target_file="scripts/tools/dx/axe_lite_static.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_axe_lite_static.py",
        label="status: drop aria-hidden escape (everything flagged)",
        fn_name="scan_unicode_status",
        old='if "aria-hidden" in attrs or "aria-label" in attrs or "aria-labelledby" in attrs:',
        new='if "aria-label" in attrs or "aria-labelledby" in attrs:',
    ),
    # ── scan_buttons_without_name (axe_lite_static) ──────────────
    Mutation(
        target_file="scripts/tools/dx/axe_lite_static.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_axe_lite_static.py",
        label="buttons: drop title= as accessible name (title-only buttons flagged)",
        fn_name="scan_buttons_without_name",
        old='            "aria-label" in attrs\n            or "aria-labelledby" in attrs\n            or "title=" in attrs',
        new='            "aria-label" in attrs\n            or "aria-labelledby" in attrs',
    ),
    # ── scan_unlabeled_inputs (axe_lite_static) ──────────────────
    Mutation(
        target_file="scripts/tools/dx/axe_lite_static.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_axe_lite_static.py",
        label="inputs: drop placeholder as label hint",
        fn_name="scan_unlabeled_inputs",
        old='                    "placeholder",\n                    "title=",',
        new='                    "title=",',
    ),
    # ── scan_color_only_severity (axe_lite_static) ───────────────
    Mutation(
        target_file="scripts/tools/dx/axe_lite_static.py",
        test_file="tests/shared/test_property_tools.py tests/dx/test_axe_lite_static.py",
        label="color: drop font-bold from non-color signals",
        fn_name="scan_color_only_severity",
        old='                "font-bold",\n                "font-semibold",',
        new='                "font-semibold",',
    ),
    # ── load_yaml_file (_lib_io) ─────────────────────────────────
    Mutation(
        target_file="scripts/tools/_lib_io.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="load_yaml: drop isfile check (would attempt to open missing path)",
        fn_name="load_yaml_file",
        old="    if not path or not os.path.isfile(path):\n        return default",
        new="    if not path:\n        return default",
    ),
    Mutation(
        target_file="scripts/tools/_lib_io.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="load_yaml: drop None-coalesce (empty file returns None instead of default)",
        fn_name="load_yaml_file",
        old="    return data if data is not None else default",
        new="    return data",
    ),
    # ── iter_yaml_files (_lib_io) ────────────────────────────────
    Mutation(
        target_file="scripts/tools/_lib_io.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="iter_yaml: drop .yml from extension check",
        fn_name="iter_yaml_files",
        old='        if not (fname.endswith(".yaml") or fname.endswith(".yml")):',
        new='        if not fname.endswith(".yaml"):',
    ),
    Mutation(
        target_file="scripts/tools/_lib_io.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="iter_yaml: drop dotfile filter (.hidden.yaml leaks through)",
        fn_name="iter_yaml_files",
        old='        if skip_reserved and (fname.startswith("_") or fname.startswith(".")):',
        new='        if skip_reserved and fname.startswith("_"):',
    ),
    Mutation(
        target_file="scripts/tools/_lib_io.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="iter_yaml: drop isfile filter (directories ending in .yaml leak through)",
        fn_name="iter_yaml_files",
        old="        fpath = os.path.join(config_dir, fname)\n        if os.path.isfile(fpath):\n            result.append((fname, fpath))",
        new="        fpath = os.path.join(config_dir, fname)\n        result.append((fname, fpath))",
    ),
    # ── format_json_report (_lib_io) ─────────────────────────────
    Mutation(
        target_file="scripts/tools/_lib_io.py",
        test_file="tests/shared/test_property_tools.py",
        label="format_json: drop pretty-print default (indent=0 → no newlines)",
        fn_name="format_json_report",
        old='    kwargs.setdefault("indent", 2)',
        new='    kwargs.setdefault("indent", 0)',
    ),
    Mutation(
        target_file="scripts/tools/_lib_io.py",
        test_file="tests/shared/test_property_tools.py",
        label="format_json: drop ensure_ascii default (Unicode gets escaped)",
        fn_name="format_json_report",
        old='    kwargs.setdefault("ensure_ascii", False)',
        new='    kwargs.setdefault("ensure_ascii", True)',
    ),
]


def run_tests(test_target: str) -> tuple[int, str]:
    """Run pytest, return (returncode, output_tail)."""
    cmd = [sys.executable, "-m", "pytest"] + test_target.split() + [
        "--tb=line", "-q", "--no-header", "--maxfail=1",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(REPO_ROOT),
        timeout=120, encoding="utf-8", errors="replace",
    )
    tail = (proc.stdout or "").splitlines()[-3:]
    return proc.returncode, " | ".join(tail)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", help="Filter to mutations whose fn_name contains this")
    args = parser.parse_args()

    selected = [m for m in MUTATIONS if not args.target or args.target in m.fn_name]
    print(f"Running {len(selected)} mutations\n")

    results: list[tuple[Mutation, str]] = []
    for i, m in enumerate(selected, 1):
        path = REPO_ROOT / m.target_file
        with open(path, encoding="utf-8", newline="") as f:
            original = f.read()

        try:
            m.apply()
        except ValueError as e:
            results.append((m, f"SETUP-FAIL: {e}"))
            continue

        try:
            rc, tail = run_tests(m.test_file)
            verdict = "CAUGHT" if rc != 0 else "SURVIVED"
            results.append((m, f"{verdict} (rc={rc}) :: {tail[:160]}"))
        finally:
            m.revert(original)

        print(f"[{i:2d}/{len(selected)}] {m.fn_name}: {m.label[:60]}")
        print(f"      → {results[-1][1]}\n")

    # Summary
    caught = sum(1 for _, v in results if v.startswith("CAUGHT"))
    survived = sum(1 for _, v in results if v.startswith("SURVIVED"))
    setup_fail = sum(1 for _, v in results if v.startswith("SETUP-FAIL"))
    print(f"\n=== SUMMARY: {caught}/{len(results)} caught, {survived} survived, {setup_fail} setup-failures ===\n")

    if survived:
        print("SURVIVING MUTATIONS (test gaps):")
        for m, v in results:
            if v.startswith("SURVIVED"):
                print(f"  - {m.fn_name}: {m.label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
