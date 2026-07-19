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


def harden_stdout_errors() -> None:
    """Best-effort: make stdout degrade (not crash) on unencodable chars.

    Why this exists (Wave 7, da-tools ROI round 4):
        `try_utf8_stdout()` above is called as the first line of `main()`,
        but argparse's `--help` prints (and exits) inside
        `parser.parse_args()` — i.e. BEFORE any main()-body call runs.
        On a zh-TW Windows console (cp950) a help text containing e.g.
        '≥' (U+2265) therefore crashed with UnicodeEncodeError and a
        non-zero exit, breaking the "--help always exits 0" contract on
        the exact consoles our customer base uses. The only spot that
        is guaranteed to run before parse_args() is module import — so
        this helper is invoked at import time of the shared root libs
        (_lib_compat itself, plus _lib_exitcodes / _lib_python /
        _lib_godispatch chain-import it). Every ops tool imports at
        least one of those four at module level (gate:
        tests/shared/test_console_encoding_resilience.py).

    What this does (and deliberately does NOT do):
        sys.stdout.reconfigure(errors="backslashreplace")
        - errors only, encoding UNTOUCHED: the console's native codec
          (cp950 / cp936 / cp1252 / utf-8) keeps rendering everything it
          CAN encode — Traditional Chinese help stays readable on cp950
          — and only the rare unencodable char degrades to '\\uXXXX'.
          (Contrast try_utf8_stdout(), which forces UTF-8 bytes: right
          for tools that opted in, but as an import-time default it
          would mojibake ALL Chinese text on legacy consoles.)
        - "backslashreplace" over "replace": info-preserving ('\\u2265'
          instead of '?') and matches Python's own stderr default.
          JSON-safety note: for BMP chars U+0100–U+FFFF the artifact is
          '\\uXXXX', which is itself a valid JSON escape — so the common
          case (this repo's status symbols ✓ ⚠ ➕ → ≥ ≤ are all BMP)
          keeps non-ensure_ascii --json output parseable on a legacy
          console. Latin-1 (U+0080–U+00FF → '\\xXX') and astral
          (→ '\\UXXXXXXXX') artifacts are NOT valid JSON; those --json
          outputs would need `| jq` to fail rather than parse a garbled
          char — still a strict improvement over the previous hard crash,
          just not silently-correct. (This window is invisible to the
          #1112 --json gate, which runs under utf-8 where this hook is a
          no-op.)
        - On UTF-8 stdout this is a byte-for-byte no-op (UTF-8 encodes
          everything), so CI / modern terminals / piped-to-file output
          are unchanged.

    Interaction with try_utf8_stdout():
        Independent and compatible. Tools that call try_utf8_stdout()
        in main() still get their historical UTF-8-forced stdout; this
        hook only covers the import→parse_args window and tools that
        never adopted the main() call.

    Why best-effort (the try/except):
        Guards genuinely non-reconfigurable stdout: Python <3.7 (no
        .reconfigure), plain StringIO stand-ins, or detached streams.
        NOTE — modern pytest capture (CaptureIO / fd-capture TextIOWrapper)
        DOES support .reconfigure(); under pytest this call succeeds and
        flips the capture stream's error mode. Verified harmless: capsys is
        function-scoped (no cross-test bleed) and encoding-sensitive tests
        use their own monkeypatched fake streams — the full suite passes
        with this side effect live. Idempotent: _lib_compat's module body
        runs once per process (sys.modules cache) however many carriers
        chain-import it, and reconfigure() is itself idempotent.
    """
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, OSError):
        # Non-reconfigurable stdout (pytest capture, StringIO, old
        # Python). Defensive fallthrough — worst case is the historical
        # behavior, never worse.
        pass


# Import-time side effect (intentional; see harden_stdout_errors docstring):
# hardening must land before argparse.parse_args() can print --help, and
# module import of a shared root lib is the only guaranteed pre-parse hook.
harden_stdout_errors()
