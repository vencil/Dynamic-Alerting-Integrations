"""File I/O and YAML helpers for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

from _lib_confd import warn_nested
from _lib_constants import ONBOARD_HINTS_FILENAME


def load_yaml_file(path: Optional[str], default: Any = None) -> Any:
    """Load a YAML file with UTF-8 encoding and safe parsing.

    Args:
        path: Filesystem path.  Returns *default* if ``None``, empty,
              or non-existent.
        default: Fallback value when the file is missing or empty.

    Returns:
        Parsed YAML data, or *default*.
    """
    if not path or not Path(path).is_file():
        return default
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else default


def iter_yaml_files(
    config_dir: str,
    *,
    skip_reserved: bool = True,
) -> list[tuple[str, str]]:
    """List YAML files in *config_dir*, sorted deterministically.

    Args:
        config_dir: Path to the configuration directory.
        skip_reserved: If ``True`` (default), skip files whose names
                       start with ``_`` or ``.`` (reserved / dotfiles).

    Returns:
        List of ``(filename, full_path)`` tuples, sorted by filename.
    """
    if not config_dir:
        return []
    base = Path(config_dir)
    if not base.is_dir():
        return []
    # #1339: flat read — a hierarchical conf.d must not look empty.
    # tool= omitted on purpose: this helper is reached from several
    # entry points, so the message should name the command the
    # operator ran, not this function.
    warn_nested(base)
    result: list[tuple[str, str]] = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        fname = entry.name
        if not (fname.endswith(".yaml") or fname.endswith(".yml")):
            continue
        if skip_reserved and (fname.startswith("_") or fname.startswith(".")):
            continue
        if entry.is_file():
            result.append((fname, str(entry)))
    return result


def load_tenant_configs(config_dir: str) -> dict[str, dict[str, Any]]:
    """Load all tenant configurations from a config directory.

    Handles both the ``{tenants: {name: {...}}}`` wrapper format
    (used in ``conf.d/``) and the flat single-tenant format.
    Files starting with ``_`` or ``.`` are skipped.

    Args:
        config_dir: Path to the configuration directory.

    Returns:
        Dict mapping ``tenant_name`` → ``config_dict``.  Empty dict when
        *config_dir* is missing or holds no eligible files.

        ⚠️ A document that parses to a non-mapping is skipped, but an EMPTY
        file is not: ``load_yaml_file`` turns it into the ``{}`` default, so
        the file registers a tenant named after it with no thresholds. Same
        for a comments-only file. Measured, and load-bearing for every caller
        that counts tenants.

    Raises:
        Anything raised while listing the directory or reading a file
        propagates. ``yaml.YAMLError``, ``UnicodeDecodeError`` and ``OSError``
        are the common ones, but this is deliberately NOT a closed list — a
        deeply nested document raises ``RecursionError``, which is a sibling
        of none of them, and an unreadable *directory* raises from
        :func:`iter_yaml_files` rather than from :func:`load_yaml_file`.
        Callers that need a CLI exit code should catch broadly at their entry
        point rather than name types here.

    ⛔ This previously documented itself as returning an "empty dict on any
    error", which was never true — the exceptions above have always
    propagated. The wording mattered because it invites callers to treat a
    malformed config directory as an empty one, i.e. to fail OPEN on exactly
    the input that should be loudest. Callers that need a CLI exit code
    should map these to ``EXIT_CALLER_ERROR`` at their own entry point (see
    ``scripts/tools/ops/config_diff.py:main``); swallowing them *here* would
    silently turn "your config is broken" into "you have no tenants" for
    every one of this helper's callers at once.
    """
    configs: dict[str, dict[str, Any]] = {}
    for fname, fpath in iter_yaml_files(config_dir):
        raw = load_yaml_file(fpath, default={})
        if not isinstance(raw, dict):
            continue
        if "tenants" in raw and isinstance(raw.get("tenants"), dict):
            for t_name, t_data in raw["tenants"].items():
                if isinstance(t_data, dict):
                    configs[t_name] = t_data
        else:
            tenant = fname.rsplit(".", 1)[0]
            configs[tenant] = raw
    return configs


def write_text_secure(path: str, content: str) -> None:
    """Write text to *path* with UTF-8 encoding, LF endings, and ``0o600``.

    Centralises the SAST-mandated pattern::

        with open(path, "w", encoding="utf-8", newline="\\n") as f:
            f.write(content)
        Path(path).chmod(0o600)

    ⛔ ``newline="\\n"`` is load-bearing, not cosmetic. Without it Python's
    text layer translates every ``\\n`` to ``os.linesep``, so the SAME
    generator emits LF on Linux/CI and CRLF on a Windows host. Since reads go
    through universal-newline translation, the CRLF is invisible to the tool's
    own ``--check`` and ``.gitattributes`` (``* text=auto eol=lf``) normalises
    it away at commit — so the staged diff stays correct and nothing fails.
    What breaks is the *working copy*: it diverges byte-wise from every other
    file in the tree, which defeats byte-level comparisons (mutation harnesses
    asserting a file was restored byte-identical) and emits a confusing
    "CRLF will be replaced by LF" warning on every subsequent ``git diff``.

    Note this pins LF unconditionally, which is correct for every current
    caller. If a caller ever needs to emit a Windows-shell script
    (``.bat`` / ``.cmd`` / ``.ps1`` — the only paths ``.gitattributes`` marks
    ``eol=crlf``), it must NOT use this helper.

    Args:
        path: Filesystem path to write.
        content: Text content.
    """
    target = Path(path)
    target.write_text(content, encoding="utf-8", newline="\n")
    target.chmod(0o600)


def write_json_secure(
    path: str,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    """Write *data* as JSON to *path* with ``0o600`` permissions.

    Args:
        path: Filesystem path to write.
        data: JSON-serializable object.
        indent: JSON indentation (default 2).
        ensure_ascii: If ``False`` (default), allow non-ASCII characters.

    ``newline="\\n"`` is load-bearing for the same reason as
    :func:`write_text_secure` — ``json.dump`` emits ``\\n`` between lines and
    the text layer would translate every one of them to CRLF on a Windows
    host. ``.gitattributes`` pins ``*.json`` to ``eol=lf``.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=indent, ensure_ascii=ensure_ascii)
    Path(path).chmod(0o600)


def write_onboard_hints(output_dir: str, hints: dict[str, Any]) -> str:
    """Write onboard hints JSON for scaffold consumption.

    Args:
        output_dir: Directory to write ``onboard-hints.json`` into.
        hints: Data dict (tenants, db_types, routing_hints, …).

    Returns:
        Absolute path to the written file.
    """
    path = str(Path(output_dir) / ONBOARD_HINTS_FILENAME)
    write_json_secure(path, hints)
    return path


def read_onboard_hints(path: Optional[str]) -> Optional[dict[str, Any]]:
    """Read onboard hints JSON.

    Returns:
        Parsed dict, or ``None`` if file is missing / unreadable.
    """
    if not path or not Path(path).is_file():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe_label(value: Any) -> str:
    """Neutralise control characters in an untrusted value before it is PRINTED.

    #1538. A tenant config *filename* is untrusted input — ``tenants/onboarding``
    and the tenant self-service flow both create files whose names the platform
    never chose. Every plain-text report in ``scripts/tools/`` interpolates those
    names (and the exception text derived from them) straight into ``print()``.
    Two things ride in on that:

    * **a newline** ends the current report line and starts a new one, so a file
      named ``evil\\n[PASS] all good\\nx.yaml`` prints ``[PASS] all good`` at
      column 0 — indistinguishable, to a human or to a ``grep '^\\[PASS\\]'``, from
      a verdict the tool actually emitted;
    * **an ESC (``\\x1b``)** starts an ANSI sequence, so the same channel can
      recolour the terminal, move the cursor, or clear the screen.

    Replacing each control char with ``?`` defuses both while keeping the payload
    visible (``\\x1b[2J`` prints as ``?[2J``) — the operator still sees that
    something odd is in the name, which deleting the characters would hide.

    ⛔ **Scope, stated so nobody over-reads it.** This covers C0
    ``\x00-\x1f``, DEL ``\x7f``, and the C1 range ``\x80-\x9f``.

    C1 was added in the #1538 review round, and not as widening for its own
    sake: ``U+0085`` (NEL) is *the same attack as the newline* — terminals that
    decode C1 treat it as a line break, so an unescaped NEL forges a report line
    exactly as ``\n`` does, which is the one thing this function exists to stop.
    An adversarial review measured it passing through ten already-fixed tools.

    ⚠️ Honest boundary on that evidence: what was measured is **byte
    passthrough**, not rendering. Nobody drove a real terminal to confirm NEL
    breaks the line there. C1 is covered because it belongs to the same attack
    class, not because the terminal behaviour was demonstrated.

    ⛔ This also means the class is NO LONGER byte-identical to the one
    ``compile_custom_alerts._safe_log`` carried from #1008 until this change: a
    C1 character that used to survive that tool's quarantine line now renders as
    ``?``. A deliberate behaviour change, not a refactoring accident.

    Still NOT covered, deliberately:

    * bidi overrides ``U+202A-U+202E`` / ``U+2066-U+2069``. They reorder a line
      **visually** without breaking it — a different attack class from forging a
      line, and folding it in here would blur what this function promises.
    * homoglyph or zero-width confusables.

    Those remain open. Do not read "went through ``safe_label``" as "is safe to
    render in an arbitrary terminal."

    This is an OUTPUT-layer helper, not a validator: it must not be used to
    sanitise a value on its way *into* a config, a filename, or a subprocess
    argument. It lives beside :func:`format_json_report` because they are the two
    halves of the same decision — ``--json`` carries its own escaping and this is
    the plain-text branch's equivalent. ⛔ Never apply it to data destined for the
    ``--json`` branch: that would corrupt machine-readable output.

    ⚠️ **``--json`` is NOT unconditionally safe, and an earlier wording here said
    it was.** ``json.dumps`` is only required to escape ``"``, ``\\`` and
    ``U+0000``–``U+001F``; under ``ensure_ascii=False`` (this repo's default, see
    :func:`format_json_report`) the **C1** range passes through verbatim —
    measured, ``json.dumps("a\\x85b", ensure_ascii=False)`` keeps the raw byte
    while ``"a\\x0ab"`` becomes ``\\n``. So a ``--json`` payload piped straight to
    a terminal that interprets C1 is still forgeable. That is EXISTING behaviour,
    deliberately not changed here: the byte-for-byte stability of ``--json`` is
    the mechanism guarantee this escaping rests on, and rewriting the serialized
    output would trade a measured guarantee for an unmeasured one. Tracked
    separately; do not read this paragraph as "handled".

    ⛔ Apply it to the FIELD, never to a whole rendered multi-line report — the
    report's own ``\\n`` separators are control characters too and would become
    ``?``, collapsing the layout.

    Args:
        value: Any value; coerced with ``str()``.

    Returns:
        *value* as text with every C0, DEL **and C1** character replaced by
        ``?`` — the class is ``[\\x00-\\x1f\\x7f-\\x9f]``. C1 is in because
        ``\\x85`` (NEL) forges a line exactly like ``\\n`` does.
    """
    return _CONTROL_CHARS_RE.sub("?", str(value))


def format_json_report(data: Any, **kwargs: Any) -> str:
    """Serialize data as pretty-printed JSON (ensure_ascii=False).

    Thin wrapper to eliminate ``json.dumps(data, indent=2, ensure_ascii=False)``
    duplication across 20+ tools.  Extra kwargs are forwarded to ``json.dumps``.
    """
    kwargs.setdefault("indent", 2)
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(data, **kwargs)


# ── Common argparse helpers ─────────────────────────────────────────
# Extracted in v2.4.0 Phase B to eliminate argparse boilerplate across 20+ tools.


def add_config_dir_arg(
    parser: argparse.ArgumentParser,
    *,
    required: bool = True,
    default: str | None = None,
    help_text: str = "Path to tenant config directory (conf.d/)",
) -> None:
    """Add ``--config-dir`` argument with standard defaults."""
    parser.add_argument(
        "--config-dir",
        required=required and default is None,
        default=default,
        help=help_text,
    )


def add_json_arg(
    parser: argparse.ArgumentParser,
    *,
    help_text: str = "Output as JSON (for CI integration)",
) -> None:
    """Add ``--json`` boolean flag for machine-readable output."""
    parser.add_argument("--json", action="store_true", dest="json_output", help=help_text)


def add_ci_arg(
    parser: argparse.ArgumentParser,
    *,
    help_text: str = "CI mode: exit 1 on any issue",
) -> None:
    """Add ``--ci`` boolean flag for CI exit-code behaviour."""
    parser.add_argument("--ci", action="store_true", help=help_text)


def add_prometheus_arg(
    parser: argparse.ArgumentParser,
    *,
    default: str | None = None,
    help_text: str = "Prometheus URL (default: $PROMETHEUS_URL or http://localhost:9090)",
) -> None:
    """Add ``--prometheus`` argument with env-var fallback."""
    parser.add_argument(
        "--prometheus",
        # `... or "http://localhost:9090"` (not the get() default) so an
        # empty $PROMETHEUS_URL falls back to localhost too. This aligns the
        # empty-string semantics with entrypoint.py's inject_prometheus_env
        # (`if prom_url:`), which also treats "" as unset — otherwise a
        # deployment that sets PROMETHEUS_URL="" (e.g. a ConfigMap key that
        # resolves empty) would get an empty URL here but localhost via the
        # dispatcher, an inconsistency between the two fallback mechanisms.
        default=default or os.environ.get("PROMETHEUS_URL") or "http://localhost:9090",
        help=help_text,
    )
