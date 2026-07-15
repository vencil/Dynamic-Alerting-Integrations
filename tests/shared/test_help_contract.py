"""CLI help structural contract tests.

Replaces the former full-text ``--help`` snapshot tests
(``test_snapshot.py`` + ``tests/snapshots/*_help_py310.snap``), which
broke on any wording/whitespace/ordering tweak and — being
Python-version-suffixed (py310 only) — auto-created-then-skipped on
CI's Python 3.13, providing no regression value there.

What IS asserted (the stable CLI interface surface):
  - ``--help`` exits 0
  - every documented flag still exists (structural presence)
  - required flags stay required (bare, un-bracketed in argparse usage)
  - enum choices stay intact (argparse ``{a,b,c}`` token)

What is deliberately NOT asserted:
  - description/help wording, line wrapping, whitespace, option ordering

When adding a flag to one of these CLIs, add it to HELP_CONTRACTS below
(dev-rules #4 Doc-as-Code applies to the CLI change itself).
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).parent.parent.parent / "scripts" / "tools"

# Interface contract per CLI: flags = must exist; required = must appear
# un-bracketed in the usage block; choices = exact argparse choice tokens.
HELP_CONTRACTS = {
    "scaffold_tenant": {
        "script": TOOLS_DIR / "ops" / "scaffold_tenant.py",
        "flags": [
            "--tenant", "--db", "-o", "--output-dir", "--catalog",
            "--non-interactive", "--namespaces", "--routing-profile",
            "--topology", "--mapping-instance", "--mapping-filter",
            "--silent-mode", "--severity-dedup", "--routing-receiver",
            "--routing-receiver-type", "--routing-smarthost",
            "--routing-group-by", "--routing-group-wait",
            "--routing-group-interval", "--routing-repeat-interval",
            "--profile", "--from-onboard", "--generate-profile", "--tier",
        ],
        "required": [],
        "choices": {
            "--topology": "{1:1,N:1,1:N}",
            "--silent-mode": "{warning,critical,all,disable}",
            "--severity-dedup": "{enable,disable}",
            "--routing-receiver-type":
                "{webhook,email,slack,teams,rocketchat,pagerduty}",
            "--tier": "{prod,staging}",
        },
    },
    "validate_config": {
        "script": TOOLS_DIR / "ops" / "validate_config.py",
        "flags": [
            "--config-dir", "--policy", "--rule-packs", "--version-check",
            "--json", "--policy-dsl",
        ],
        "required": ["--config-dir"],
        "choices": {},
    },
    "operator_generate": {
        "script": TOOLS_DIR / "ops" / "operator_generate.py",
        "flags": [
            "--rule-packs-dir", "--config-dir", "--output-dir",
            "--namespace", "--api-version", "--gitops", "--dry-run",
            "--json", "--components", "--receiver-template",
            "--secret-name", "--secret-key", "--kustomize",
        ],
        "required": [],
        "choices": {
            "--api-version": "{v1alpha1,v1beta1}",
            "--components": "{all,rules,alertmanager,servicemonitor}",
            "--receiver-template":
                "{slack,pagerduty,email,teams,opsgenie,webhook}",
        },
    },
    "config_diff": {
        "script": TOOLS_DIR / "ops" / "config_diff.py",
        "flags": ["--old-dir", "--new-dir", "--json-output", "--format"],
        "required": ["--old-dir", "--new-dir"],
        "choices": {"--format": "{markdown,json}"},
    },
    "alert_quality": {
        "script": TOOLS_DIR / "ops" / "alert_quality.py",
        "flags": [
            "--prometheus", "--alertmanager", "--period", "--tenant",
            "--json", "--markdown", "--ci", "--min-score",
        ],
        "required": ["--prometheus"],
        "choices": {},
    },
}


def _declared_flags(help_text):
    """Extract option strings DECLARED in the argparse options section.

    Full-text search is not enough: epilog examples and help
    descriptions mention flags verbatim (e.g. ``requires --tenant``),
    which would keep a presence assertion green after a rename.
    Option entries are indented exactly 2 spaces; help-text
    continuation lines are indented to help_position (deeper), so
    ``^  -`` uniquely identifies entry lines across py3.10–3.14
    (including the py3.13 ``-o, --output-dir METAVAR`` reformat).
    """
    flags = set()
    in_options = False
    for line in help_text.splitlines():
        if re.match(r"^(options|optional arguments):", line):
            in_options = True
            continue
        if in_options:
            if line and not line[0].isspace():
                in_options = False  # left-margin line: next section/epilog
                continue
            m = re.match(r"^  (-\S.*)$", line)
            if not m:
                continue
            invocation = re.split(r"\s{2,}", m.group(1))[0]
            for token in invocation.split(","):
                token = token.strip().split()[0] if token.strip() else ""
                if token.startswith("-"):
                    flags.add(token)
    return flags


def _usage_block(help_text):
    """Return the argparse usage block joined to one line.

    argparse wraps usage across indented continuation lines and ends the
    block with a blank line; joining makes bracket checks wrap-proof.
    """
    lines = []
    started = False
    for line in help_text.splitlines():
        if line.startswith("usage:"):
            started = True
        if started:
            if not line.strip():
                break
            lines.append(line.strip())
    assert lines, f"no usage block found in help output:\n{help_text[:500]}"
    return " ".join(lines)


@pytest.mark.parametrize("tool", sorted(HELP_CONTRACTS))
def test_help_interface_contract(tool):
    """<tool> --help must expose all contracted flags/choices."""
    contract = HELP_CONTRACTS[tool]
    result = subprocess.run(
        [sys.executable, str(contract["script"]), "--help"],
        capture_output=True, timeout=15, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"{tool} --help failed (rc={result.returncode}): "
        f"{result.stderr[:200]}"
    )
    help_text = result.stdout

    declared = _declared_flags(help_text)
    missing = [flag for flag in contract["flags"] if flag not in declared]
    assert not missing, (
        f"{tool} --help lost flags: {missing} (declared: {sorted(declared)})"
    )

    usage = _usage_block(help_text)
    for flag in contract["required"]:
        # A required option appears bare in usage; optional ones are
        # wrapped as [--flag ...]. Losing required-ness silently changes
        # the CLI contract (argparse exits 2 on missing required args).
        assert re.search(rf"(?<!\[){re.escape(flag)}(?![\w-])", usage), (
            f"{tool}: {flag} is no longer a required argument "
            f"(usage: {usage})"
        )

    for flag, choice_token in contract["choices"].items():
        # Scoped to the usage block (not full help text) so epilog /
        # description mentions can never satisfy the assertion.
        assert f"{flag} {choice_token}" in usage, (
            f"{tool}: choices for {flag} changed — expected "
            f"'{flag} {choice_token}' in usage: {usage}"
        )
