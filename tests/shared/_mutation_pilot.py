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

Latest run (v2.8.0 audit ④ pilot): 67/70 caught (~96%) across 31
functions. The 3 survivors are all equivalent mutations:
  - parse_duration_seconds: drop type-check before m.match's str()
    coercion (str() catches the non-string case downstream).
  - strip_frontmatter: offset 3→0 in `find("\\n---", 3)` — opening
    `---` is always at index 0, so the alternate offset matches the
    same closing tag for any valid frontmatter.
  - _parse_front_matter: drop the explicit `startswith("---")` early
    return — the subsequent `re.match(r"^---\\n…", …)` already rejects
    non-frontmatter inputs, so the early return is redundant.

Two further obvious-looking mutations on `latest_version_from_changelog`
were found to be equivalent (anchored regex makes match≡search; CHANGELOG
regex's capture always satisfies parse_version's shape) and skipped — see
the inline note above that mutation entry.

The 4% survivor rate is the floor: real test gaps have been chased
down across batches; what remains is true code-level redundancy that
no behavioral test can pin without overspecifying the implementation.
See PR descriptions / commit messages for findings across batches.
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
    # ── _validate_url_scheme (_lib_prometheus) ───────────────────
    Mutation(
        target_file="scripts/tools/_lib_prometheus.py",
        test_file="tests/shared/test_property_tools.py",
        label="url_scheme: invert allowlist check (in → not in)",
        fn_name="_validate_url_scheme",
        old="    if scheme not in _ALLOWED_SCHEMES:",
        new="    if scheme in _ALLOWED_SCHEMES:",
    ),
    Mutation(
        target_file="scripts/tools/_lib_prometheus.py",
        test_file="tests/shared/test_property_tools.py",
        label="url_scheme: drop scheme parsing (always pass)",
        fn_name="_validate_url_scheme",
        old="    scheme = urllib.parse.urlparse(url).scheme\n    if scheme not in _ALLOWED_SCHEMES:\n        return f\"Unsupported URL scheme: {scheme}\"\n    return None",
        new="    return None",
    ),
    # ── detect_cli_lang (_lib_validation) ────────────────────────
    Mutation(
        target_file="scripts/tools/_lib_validation.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="detect_lang: swap precedence (DA_LANG/LC_ALL/LANG → LANG/LC_ALL/DA_LANG)",
        fn_name="detect_cli_lang",
        old='for var in ("DA_LANG", "LC_ALL", "LANG"):',
        new='for var in ("LANG", "LC_ALL", "DA_LANG"):',
    ),
    Mutation(
        target_file="scripts/tools/_lib_validation.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="detect_lang: default falls to 'zh' instead of 'en'",
        fn_name="detect_cli_lang",
        old='        if val.startswith("en"):\n            return "en"\n    return "en"',
        new='        if val.startswith("en"):\n            return "en"\n    return "zh"',
    ),
    # ── i18n_text (_lib_validation) ──────────────────────────────
    Mutation(
        target_file="scripts/tools/_lib_validation.py",
        test_file="tests/shared/test_property_tools.py tests/shared/test_lib_python.py",
        label="i18n: swap zh/en branches (always returns wrong language)",
        fn_name="i18n_text",
        old='    return zh if detect_cli_lang() == "zh" else en',
        new='    return en if detect_cli_lang() == "zh" else zh',
    ),
    # ── parse_version (check_flaky_registry) ─────────────────────
    # Note on the obvious-looking `re.match → re.search` mutation: the
    # _VERSION_RE pattern has explicit ^…$ anchors, so re.match and
    # re.search are functionally equivalent for this regex (verified
    # empirically). Skipped as a known-equivalent — would always survive
    # without representing a real defect.
    Mutation(
        target_file="scripts/tools/lint/check_flaky_registry.py",
        test_file="tests/shared/test_property_tools.py tests/lint/test_check_flaky_registry.py",
        label="version: drop strip() (whitespace breaks parse)",
        fn_name="parse_version",
        old="    m = _VERSION_RE.match(s.strip())",
        new="    m = _VERSION_RE.match(s)",
    ),
    Mutation(
        target_file="scripts/tools/lint/check_flaky_registry.py",
        test_file="tests/shared/test_property_tools.py tests/lint/test_check_flaky_registry.py",
        label="version: drop cross-line guard (different prefixes compare)",
        fn_name="__lt__",
        old='        if self.prefix != other.prefix:\n            raise ValueError(\n                f"cannot compare versions across release lines: "\n                f"{self.prefix or \'<root>\'!r} vs {other.prefix or \'<root>\'!r}"\n            )',
        new='        if False:\n            raise ValueError("never")',
    ),
    # ── _resolve_binary (_lib_godispatch) ────────────────────────
    Mutation(
        target_file="scripts/tools/_lib_godispatch.py",
        test_file="tests/shared/test_property_tools.py",
        label="resolve: drop isfile check on explicit (missing path returns string)",
        fn_name="_resolve_binary",
        old="        if explicit:\n            return (\n                explicit if os.path.isfile(explicit) else None\n            ), cleaned",
        new="        if explicit:\n            return explicit, cleaned",
    ),
    Mutation(
        target_file="scripts/tools/_lib_godispatch.py",
        test_file="tests/shared/test_property_tools.py",
        label="resolve: skip eq-form branch (--flag=value not stripped)",
        fn_name="_resolve_binary",
        old='            if a.startswith(eq_form):\n                explicit = a.split("=", 1)[1]\n                i += 1\n                continue',
        new='            if False:\n                pass',
    ),
    Mutation(
        target_file="scripts/tools/_lib_godispatch.py",
        test_file="tests/shared/test_property_tools.py",
        label="resolve: drop env-var fallback (only flag + PATH consulted)",
        fn_name="_resolve_binary",
        old='        env_override = os.environ.get(self.env_var, "").strip()\n        if env_override:\n            return (\n                env_override if os.path.isfile(env_override) else None\n            ), cleaned',
        new='        env_override = ""\n        if env_override:\n            return None, cleaned',
    ),
    # ── _substitute_tenant (_grar_merge) ─────────────────────────
    Mutation(
        target_file="scripts/tools/ops/_grar_merge.py",
        test_file="tests/shared/test_property_tools.py",
        label="substitute: drop dict recursion (nested {{tenant}} unchanged)",
        fn_name="_substitute_tenant",
        old="    if isinstance(obj, dict):\n        return {k: _substitute_tenant(v, tenant_name) for k, v in obj.items()}",
        new="    if isinstance(obj, dict):\n        return obj",
    ),
    Mutation(
        target_file="scripts/tools/ops/_grar_merge.py",
        test_file="tests/shared/test_property_tools.py",
        label="substitute: drop list recursion (nested {{tenant}} unchanged)",
        fn_name="_substitute_tenant",
        old="    if isinstance(obj, list):\n        return [_substitute_tenant(item, tenant_name) for item in obj]",
        new="    if isinstance(obj, list):\n        return obj",
    ),
    Mutation(
        target_file="scripts/tools/ops/_grar_merge.py",
        test_file="tests/shared/test_property_tools.py",
        label="substitute: empty replacement (placeholder removed but no name inserted)",
        fn_name="_substitute_tenant",
        old='        return obj.replace("{{tenant}}", tenant_name)',
        new='        return obj.replace("{{tenant}}", "")',
    ),
    # ── _contains_tenant_placeholder (_grar_merge) ───────────────
    Mutation(
        target_file="scripts/tools/ops/_grar_merge.py",
        test_file="tests/shared/test_property_tools.py",
        label="contains: skip dict recursion (nested marker missed)",
        fn_name="_contains_tenant_placeholder",
        old="    if isinstance(obj, dict):\n        return any(_contains_tenant_placeholder(v) for v in obj.values())",
        new="    if isinstance(obj, dict):\n        return False",
    ),
    Mutation(
        target_file="scripts/tools/ops/_grar_merge.py",
        test_file="tests/shared/test_property_tools.py",
        label="contains: any → all (one missing marker masks the others)",
        fn_name="_contains_tenant_placeholder",
        old="    if isinstance(obj, list):\n        return any(_contains_tenant_placeholder(item) for item in obj)",
        new="    if isinstance(obj, list):\n        return all(_contains_tenant_placeholder(item) for item in obj)",
    ),
    # ── merge_routing_with_defaults (_grar_merge) ────────────────
    Mutation(
        target_file="scripts/tools/ops/_grar_merge.py",
        test_file="tests/shared/test_property_tools.py",
        label="merge: defaults shadow tenant (precedence inverted)",
        fn_name="merge_routing_with_defaults",
        old="    merged = dict(defaults)\n    if isinstance(tenant_routing, dict):\n        for key, value in tenant_routing.items():\n            merged[key] = value",
        new="    merged = dict(tenant_routing) if isinstance(tenant_routing, dict) else {}\n    for key, value in defaults.items():\n        merged[key] = value",
    ),
    Mutation(
        target_file="scripts/tools/ops/_grar_merge.py",
        test_file="tests/shared/test_property_tools.py",
        label="merge: skip tenant substitution (markers leak through)",
        fn_name="merge_routing_with_defaults",
        old="    return _substitute_tenant(merged, tenant_name)",
        new="    return merged",
    ),
    Mutation(
        target_file="scripts/tools/ops/_grar_merge.py",
        test_file="tests/shared/test_property_tools.py",
        label="merge: in-place mutation (caller dict scrambled)",
        fn_name="merge_routing_with_defaults",
        old="    merged = dict(defaults)",
        new="    merged = defaults",
    ),
    # ── _extract_host (_grar_validate) ───────────────────────────
    Mutation(
        target_file="scripts/tools/ops/_grar_validate.py",
        test_file="tests/shared/test_property_tools.py",
        label="extract_host: drop type-check (raises on non-string input)",
        fn_name="_extract_host",
        old="    if not value or not isinstance(value, str):\n        return None",
        new="    if not value:\n        return None",
    ),
    Mutation(
        target_file="scripts/tools/ops/_grar_validate.py",
        test_file="tests/shared/test_property_tools.py",
        label="extract_host: drop lower() (uppercase host leaks past allowlist)",
        fn_name="_extract_host",
        old='        return value.split(":")[0].lower() or None',
        new='        return value.split(":")[0] or None',
    ),
    Mutation(
        target_file="scripts/tools/ops/_grar_validate.py",
        test_file="tests/shared/test_property_tools.py",
        label="extract_host: keep port (host:port returns whole string)",
        fn_name="_extract_host",
        old='        return value.split(":")[0].lower() or None',
        new='        return value.lower() or None',
    ),
    # ── parse_command_map (_lint_helpers) ────────────────────────
    Mutation(
        target_file="scripts/tools/lint/_lint_helpers.py",
        test_file="tests/shared/test_property_tools.py tests/lint/test_check_cli_coverage.py",
        label="parse_cmd_map: ignore closing brace (slurps text after }}",
        fn_name="parse_command_map",
        old='                if stripped == "}":\n                    break',
        new='                if False:\n                    break',
    ),
    Mutation(
        target_file="scripts/tools/lint/_lint_helpers.py",
        test_file="tests/shared/test_property_tools.py tests/lint/test_check_cli_coverage.py",
        label="parse_cmd_map: relax key regex (uppercase keys leak in)",
        fn_name="parse_command_map",
        old='                m = re.match(r\'"([a-z][a-z0-9-]+)":\\s*"([^"]+)"\', stripped)',
        new='                m = re.match(r\'"([a-zA-Z][a-zA-Z0-9-]+)":\\s*"([^"]+)"\', stripped)',
    ),
    # ── parse_build_sh_tools (_lint_helpers) ─────────────────────
    Mutation(
        target_file="scripts/tools/lint/_lint_helpers.py",
        test_file="tests/shared/test_property_tools.py",
        label="parse_build_sh: skip basename (full paths leak through)",
        fn_name="parse_build_sh_tools",
        old="                if name:\n                    tools.add(os.path.basename(name))",
        new="                if name:\n                    tools.add(name)",
    ),
    Mutation(
        target_file="scripts/tools/lint/_lint_helpers.py",
        test_file="tests/shared/test_property_tools.py",
        label="parse_build_sh: ignore closing paren (slurps next array)",
        fn_name="parse_build_sh_tools",
        old='                if stripped == ")":\n                    break',
        new='                if False:\n                    break',
    ),
    Mutation(
        target_file="scripts/tools/lint/_lint_helpers.py",
        test_file="tests/shared/test_property_tools.py",
        label="parse_build_sh: drop comment skip (# lines included as tools)",
        fn_name="parse_build_sh_tools",
        old="                if not stripped or stripped.startswith(\"#\"):\n                    continue",
        new="                if not stripped:\n                    continue",
    ),
    # ── latest_version_from_changelog (check_flaky_registry) ─────
    Mutation(
        target_file="scripts/tools/lint/check_flaky_registry.py",
        test_file="tests/shared/test_property_tools.py tests/lint/test_check_flaky_registry.py",
        label="latest_changelog: drop break (last match wins, not first)",
        fn_name="latest_version_from_changelog",
        old='        for line in f:\n            m = pattern.match(line)\n            if m:\n                try:\n                    return parse_version(m.group(1))\n                except ValueError:\n                    continue',
        new='        last = None\n        for line in f:\n            m = pattern.match(line)\n            if m:\n                try:\n                    last = parse_version(m.group(1))\n                except ValueError:\n                    continue\n        return last',
    ),
    # Note: two more obvious mutations are equivalent so skipped:
    #   - `re.match → re.search`: regex has `^` anchor + line-by-line scan;
    #     `^` anchors to position 0 of `line`, identical to match.
    #   - drop the `try: parse_version → except ValueError: continue`: the
    #     CHANGELOG regex captures `v\d+\.\d+\.\d+` which always satisfies
    #     parse_version's shape requirement, so the except is defensive
    #     dead code at runtime.
    Mutation(
        target_file="scripts/tools/lint/check_flaky_registry.py",
        test_file="tests/shared/test_property_tools.py tests/lint/test_check_flaky_registry.py",
        label="latest_changelog: drop is_file check (raises FileNotFoundError)",
        fn_name="latest_version_from_changelog",
        old="    if not path.is_file():\n        return None",
        new="    if False:\n        return None",
    ),
    # ── _apply_timing_params (_grar_merge) ───────────────────────
    Mutation(
        target_file="scripts/tools/ops/_grar_merge.py",
        test_file="tests/shared/test_property_tools.py",
        label="apply_timing: drop falsy guard (None/empty pass to clamp, broken)",
        fn_name="_apply_timing_params",
        old='        val = source_dict.get(param)\n        if val:',
        new='        val = source_dict.get(param)\n        if True:',
    ),
    Mutation(
        target_file="scripts/tools/ops/_grar_merge.py",
        test_file="tests/shared/test_property_tools.py",
        label="apply_timing: skip group_wait param (only 2 of 3 params handled)",
        fn_name="_apply_timing_params",
        old='    for param in ("group_wait", "group_interval", "repeat_interval"):',
        new='    for param in ("group_interval", "repeat_interval"):',
    ),
    # ── validate_receiver_domains (_grar_validate) ───────────────
    Mutation(
        target_file="scripts/tools/ops/_grar_validate.py",
        test_file="tests/shared/test_property_tools.py",
        label="domains: drop empty-allowlist guard (always check, breaks empty)",
        fn_name="validate_receiver_domains",
        old="    if not allowed_domains or not isinstance(receiver_obj, dict):\n        return warnings",
        new="    if not isinstance(receiver_obj, dict):\n        return warnings",
    ),
    Mutation(
        target_file="scripts/tools/ops/_grar_validate.py",
        test_file="tests/shared/test_property_tools.py",
        label="domains: any → all (tightens to require ALL patterns match)",
        fn_name="validate_receiver_domains",
        old="        if not any(fnmatch.fnmatch(host, pat) for pat in allowed_domains):",
        new="        if not all(fnmatch.fnmatch(host, pat) for pat in allowed_domains):",
    ),
    Mutation(
        target_file="scripts/tools/ops/_grar_validate.py",
        test_file="tests/shared/test_property_tools.py",
        label="domains: invert match check (allowlist becomes denylist)",
        fn_name="validate_receiver_domains",
        old="        if not any(fnmatch.fnmatch(host, pat) for pat in allowed_domains):",
        new="        if any(fnmatch.fnmatch(host, pat) for pat in allowed_domains):",
    ),
    # ── validate_tenant_keys (_grar_validate) ────────────────────
    Mutation(
        target_file="scripts/tools/ops/_grar_validate.py",
        test_file="tests/shared/test_property_tools.py",
        label="tenant_keys: drop reserved-keys allowlist (every reserved → warning)",
        fn_name="validate_tenant_keys",
        old="        if key in VALID_RESERVED_KEYS:\n            continue",
        new="        if False:\n            continue",
    ),
    Mutation(
        target_file="scripts/tools/ops/_grar_validate.py",
        test_file="tests/shared/test_property_tools.py",
        label="tenant_keys: drop _critical suffix resolution (suffix keys warn)",
        fn_name="validate_tenant_keys",
        old='        if key.endswith("_critical"):\n            base = key.removesuffix("_critical")\n            if base in defaults_keys:\n                continue',
        new='        if False:\n            base = ""\n            if base in defaults_keys:\n                continue',
    ),
    Mutation(
        target_file="scripts/tools/ops/_grar_validate.py",
        test_file="tests/shared/test_property_tools.py",
        label="tenant_keys: drop dimensional-key resolution ({labels} keys warn)",
        fn_name="validate_tenant_keys",
        old='        if "{" in key:\n            base = key.split("{")[0]\n            if base in defaults_keys:\n                continue',
        new='        if False:\n            base = ""\n            if base in defaults_keys:\n                continue',
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
