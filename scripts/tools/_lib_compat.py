"""Cross-platform compatibility helpers for Dynamic Alerting CLI tools.

Stdlib-only by design: this module is imported by tools that otherwise have
zero third-party dependencies (e.g. state_reconcile, rule_pack_diff), so we
must NOT pull in yaml / requests / etc. transitively. If a helper would
need a third-party dep, it belongs in _lib_io / _lib_validation / etc.,
not here.

Module history:
  - Introduced 2026-05-12 alongside #432 to consolidate the
    `_try_utf8_stdout()` helper that had been duplicated across four tools
    (state_reconcile, rule_pack_diff, silencer_drift_check,
    analyze_bench_history) after each was found to crash on legacy Windows
    console codecs (cp950 / cp936 / cp1252) when rendering Unicode
    characters (✓ ⚠️ ➕ ≤ → etc.) to stdout. PR comment trail on #422
    (round-2 self-review) → #424 (round-2) → #431 (round-2) → #67 hands-on
    discovered the bug class one-at-a-time; once #67 surfaced the 4th
    instance the duplication-cost crossed into refactor territory.
"""
from __future__ import annotations

import sys


def try_utf8_stdout() -> None:
    """Best-effort: reconfigure stdout to UTF-8 with replacement errors.

    Why this exists:
        CLI tools in this repo emit Unicode characters (emoji ✓ ⚠️ ➕,
        math symbols ≤, arrows →) in their stdout. Modern terminals
        (UTF-8 Linux, macOS, Windows Terminal, Docker Alpine bundle)
        handle these natively; legacy Windows consoles default to
        cp950 (zh-TW) / cp936 (zh-CN) / cp1252 (Western) which CAN'T
        encode most emoji — Python raises UnicodeEncodeError mid-print,
        killing the tool.

    What this does:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        - encoding="utf-8": stdout bytes are now valid UTF-8 regardless
          of terminal locale
        - errors="replace": any character the codec still can't represent
          gets substituted with "?" rather than crashing
        Legacy terminal then interprets UTF-8 bytes as its local codec,
        producing garbled-but-non-fatal output. Modern terminal renders
        emoji correctly. Output piped to a file is clean UTF-8 either way.

    Why best-effort (the try/except):
        - Python <3.7 lacks sys.stdout.reconfigure (AttributeError)
        - pytest's capsys / custom stdout wrappers don't support reconfigure
          (AttributeError or OSError depending on wrapper)
        - In all those cases, fall through silently — output may degrade
          but the tool keeps running. The original encoding crash is the
          worst possible UX; anything else is an improvement.

    Why stdout only (not stderr):
        Python's default stderr already uses errors="backslashreplace" so
        stderr writes never crash — they just show "\\u2713" instead of "✓".
        That's cosmetic, not fatal, so we don't reconfigure it.

    Call site:
        Add `try_utf8_stdout()` as the first line of `main()`. Idempotent —
        safe to call multiple times.

    Scope note (post-PR #432 audit):
        Currently called from four tools (state_reconcile, rule_pack_diff,
        silencer_drift_check, analyze_bench_history) that ship in v2.8.0.
        A grep for `print.*[✓⚠➕→]` reveals 20+ additional ops/ and dx/
        tools that ALSO emit emoji to stdout and would crash on the same
        legacy Windows codecs. They were NOT migrated in PR #432 because
        the existing user base hasn't filed bug reports — those tools are
        most often invoked inside the Docker image (Alpine UTF-8) or
        modern Windows Terminal (UTF-8), where the crash doesn't trigger.
        Apply this helper proactively when next touching one of those
        tools; don't sweep all 20+ in a single PR (high diff cost,
        low immediate user impact).

    sys.path side-effect note:
        Tools importing this helper use the standard sys.path insert
        pattern (see lint_custom_rules.py et al.). This pollutes the
        importer's sys.path globally and is technically a side-effect
        at module import time. Verified via the 137-test suite (state-
        reconcile + rule-pack-diff + silencer-drift-check) that this
        doesn't break pytest, since conftest.py manages test sys.path
        independently. Live with it; cleaner alternatives (e.g.
        package-relative imports) would require restructuring scripts/
        tools/ into proper Python packages — out of scope.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        # Older Python, non-stream stdout, or pytest capture wrapper.
        # Output may degrade but won't crash. Defensive fallthrough.
        pass
