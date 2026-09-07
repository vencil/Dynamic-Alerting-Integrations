"""File I/O and YAML helpers for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import argparse
import functools
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

import yaml

from _lib_confd import warn_nested
from _lib_constants import ONBOARD_HINTS_FILENAME
# No import cycle: _lib_exitcodes imports only sys + _lib_compat (#1641).
from _lib_exitcodes import EXIT_CALLER_ERROR

_F = TypeVar("_F", bound=Callable[..., Any])


class YamlFileError(yaml.YAMLError):
    """A YAML file that exists but cannot be read — and WHICH file (#1654).

    ``yaml.YAMLError`` subclass on purpose: every ``except yaml.YAMLError``
    (and ``except Exception``) a caller already has catches this, so a
    decode failure now takes the same message path as a syntax error
    instead of escaping as a traceback. Attributes:

    * ``path``  — the file as the caller named it.
    * ``cause`` — the original error (also ``__cause__``): the
      ``UnicodeDecodeError`` for content that is not valid UTF-8, the
      PyYAML error for bad syntax.

    ``str()`` is ONE line, ``<path>: <original message> (<CauseClass>)``,
    with the original's line breaks collapsed so the line/column PyYAML
    reports survive but a caller can interpolate it into a report line.
    """

    def __init__(self, path: str, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        detail = " ".join(str(cause).split()) or cause.__class__.__name__
        super().__init__(f"{path}: {detail} ({cause.__class__.__name__})")


def load_yaml_file(path: Optional[str], default: Any = None) -> Any:
    """Load a YAML file with UTF-8 encoding and safe parsing.

    Args:
        path: Filesystem path.  Returns *default* if ``None``, empty,
              or non-existent.
        default: Fallback value when the file is missing or empty.

    Returns:
        Parsed YAML data, or *default*.

    Raises:
        YamlFileError: the file exists but cannot be read — content that is
            not valid UTF-8 (``cause`` is the ``UnicodeDecodeError``) OR bad
            YAML syntax (``cause`` is the PyYAML error). Before #1654 the
            first escaped as a bare ``UnicodeDecodeError`` — a ``ValueError``
            no ``except yaml.YAMLError`` in this repo saw, and one that never
            carried the path. ⚠️ Never swallowed into *default*: a file that
            is present but unreadable is the loudest input, not an empty one.
        OSError: as before; not wrapped.

    Decoding is STRICT UTF-8, exactly what the text-mode ``open`` did, so
    the accepted encodings are unchanged: this helper must not start
    serving (say) UTF-16 while ``validate_config``'s own reads and the
    routes generator still refuse it — that would be #1339's "one input,
    two answers" inside the Python tool family (blind review). Whether the
    family should follow the exporter's parser on other encodings is a
    separate decision. The decoded text is parsed from a named stream so
    PyYAML's own marks (``in "<path>", line N, column M``) keep naming the
    real file instead of ``"<unicode string>"``.
    """
    if not path or not Path(path).is_file():
        return default
    raw = Path(path).read_bytes()
    try:
        stream = io.StringIO(raw.decode("utf-8"))
        stream.name = str(path)
        data = yaml.safe_load(stream)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise YamlFileError(str(path), exc) from exc
    return data if data is not None else default


def exit_on_yaml_file_error(fn: _F) -> _F:
    """Decorate a CLI ``main`` so an unreadable YAML input exits 2, named.

    The shared answer for every tool whose ``load_yaml_file`` call sites
    have no handler of their own (#1654): instead of 21 hand-written
    ``try/except`` blocks, the entry point is wrapped once, and
    :class:`YamlFileError` becomes ``ERROR: cannot read <path>: <reason>``
    on stderr plus ``sys.exit(EXIT_CALLER_ERROR)`` — an IO/decode failure
    is a caller error in ``_lib_exitcodes``, not the rc 1 an uncaught
    traceback produced.

    ⚠️ Only :class:`YamlFileError`. A bare ``yaml.YAMLError`` has no path to
    name and did not come from the helper; it keeps propagating so the
    layer that raised it stays visible. Tools that owe stdout a ``--json``
    envelope on this path (``backtest_threshold``, ``policy_engine``)
    catch the error themselves at the load site instead of using this.

    Lives here rather than in ``_lib_exitcodes`` because that module is
    kept stdlib-only for the tools that import nothing else; this one
    already owns ``yaml`` and the error class.
    """
    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except YamlFileError as exc:
            # ``safe_label``: the path is an untrusted filename (#1538) —
            # measured, a ``\x1b[31m`` in the name reached the terminal raw.
            print(f"ERROR: cannot read {safe_label(exc)}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
    return _wrapped  # type: ignore[return-value]


def iter_yaml_files(
    config_dir: str,
    *,
    skip_reserved: bool = True,
) -> list[tuple[str, str]]:
    """List YAML files in *config_dir*, sorted deterministically.

    The extension test is CASE-INSENSITIVE (#1537): the exporter lowercases
    the entry name before testing ``.yaml`` / ``.yml``, so it reads
    ``upper.YAML`` and merges it into the config it serves. An exact-suffix
    test here made that file invisible — not reported, simply absent — which
    is #1339's divergence with the extension as the axis rather than
    directory depth. Nothing here greps the Go source (#1448 measured that a
    Python guard asserting things about Go source text goes red on
    legitimate Go refactors while stating the opposite of the truth); the
    shared conf.d name classification matrix under ``tests/shared/`` is what
    keeps this side and the exporter's side pinned to one rule.

    Args:
        config_dir: Path to the configuration directory.
        skip_reserved: If ``True`` (default), skip files whose names
                       start with ``_`` or ``.`` (reserved / dotfiles).
                       ⚠️ It gates BOTH prefixes, so ``skip_reserved=False``
                       also stops hiding dotfiles — measured, ``.hidden.yaml``
                       comes back. That differs from ``_lib_confd``, whose
                       hidden filter is unconditional; a caller that wants
                       "reserved control files too, but still no dotfiles"
                       has to filter for itself.

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
        lower = fname.lower()
        if not (lower.endswith(".yaml") or lower.endswith(".yml")):
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
        propagates. :class:`YamlFileError` (bad syntax OR non-UTF-8 content,
        naming the file — #1654; it is a ``yaml.YAMLError``) and ``OSError``
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


class OutputWriteError(OSError):
    """A secure writer could not write (or create the directory for) *path*.

    #1641. Raised by :func:`write_text_secure` / :func:`write_json_secure` /
    :func:`ensure_dir` in place of the bare ``OSError`` family, so that the
    ONE class of failure — "the output path is unusable" — has one name and
    one message shape across every tool, instead of a traceback at rc=1
    (which in this repo reads as EXIT_VIOLATION, "your config has a
    violation", for what is a mistyped ``-o``).

    It subclasses :class:`OSError` on purpose: every call site that already
    guarded with ``except OSError`` keeps working unchanged, and ``errno`` /
    ``strerror`` / ``filename`` are populated from the original exception.

    Attributes:
        path:   The path that could not be written.
        flag:   The CLI flag the path came from (``"-o/--output"``), or
                ``None`` when the path is derived internally.
        cause:  The original :class:`OSError`.
        action: ``"write"`` (default) or ``"create directory"``.

    ``str(exc)`` is the operator-facing line::

        cannot write <path>: <strerror> (errno <n>) — check the value given to <flag>
        cannot write <path>: <strerror> (errno <n>) — internal output path, this is a bug or an unwritable workspace
    """

    def __init__(
        self,
        path: Any,
        cause: OSError,
        *,
        flag: Optional[str] = None,
        action: str = "write",
    ) -> None:
        self.path = str(path)
        self.flag = flag
        self.cause = cause
        self.action = action
        strerror = getattr(cause, "strerror", None) or str(cause) or type(cause).__name__
        super().__init__(getattr(cause, "errno", None), strerror, self.path)

    def __str__(self) -> str:
        detail = self.strerror or str(self.cause)
        if self.errno is not None:
            detail = f"{detail} (errno {self.errno})"
        if self.flag:
            hint = f"check the value given to {self.flag}"
        else:
            hint = "internal output path, this is a bug or an unwritable workspace"
        return f"cannot {self.action} {self.path}: {detail} — {hint}"


def _die_on_write_error(exc: OutputWriteError, exit_code: int) -> None:
    """Print the one-line message to stderr and exit — no traceback."""
    # #1538: the path is operator argv — still escape control characters
    # before it reaches a terminal (blind review).
    print(f"ERROR: {safe_label(str(exc))}", file=sys.stderr)
    sys.exit(exit_code)


def write_text_secure(path: str, content: str, *, flag: Optional[str] = None) -> None:
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
        flag: The CLI flag *path* came from (e.g. ``"-o/--output"``), named
              in the error message; ``None`` for an internally derived path.

    Raises:
        OutputWriteError: on any ``OSError`` from the write or the chmod
            (#1641). It IS an ``OSError``, so an existing ``except OSError``
            still catches it. Tool code on a CLI path should prefer
            :func:`write_text_or_die`, which turns it into rc=2 + one line.
    """
    target = Path(path)
    try:
        target.write_text(content, encoding="utf-8", newline="\n")
        target.chmod(0o600)
    except OSError as exc:
        raise OutputWriteError(path, exc, flag=flag) from exc


def write_json_secure(
    path: str,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    flag: Optional[str] = None,
) -> None:
    """Write *data* as JSON to *path* with ``0o600`` permissions.

    Args:
        path: Filesystem path to write.
        data: JSON-serializable object.
        indent: JSON indentation (default 2).
        ensure_ascii: If ``False`` (default), allow non-ASCII characters.
        flag: The CLI flag *path* came from; see :func:`write_text_secure`.

    ``newline="\\n"`` is load-bearing for the same reason as
    :func:`write_text_secure` — ``json.dump`` emits ``\\n`` between lines and
    the text layer would translate every one of them to CRLF on a Windows
    host. ``.gitattributes`` pins ``*.json`` to ``eol=lf``.

    Raises:
        OutputWriteError: on any ``OSError`` from the open / write / chmod
            (#1641), same contract as :func:`write_text_secure`. A
            non-serialisable *data* still raises ``TypeError`` — that is a
            programming error, not an output-path problem.
    """
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=indent, ensure_ascii=ensure_ascii)
        Path(path).chmod(0o600)
    except OSError as exc:
        raise OutputWriteError(path, exc, flag=flag) from exc


def write_text_or_die(
    path: str,
    content: str,
    *,
    flag: Optional[str] = None,
    exit_code: int = EXIT_CALLER_ERROR,
) -> None:
    """:func:`write_text_secure`, but an unusable path ends the process.

    #1641. On :class:`OutputWriteError` prints ``ERROR: <message>`` to stderr
    and exits with *exit_code* (default ``EXIT_CALLER_ERROR`` = 2, the
    exit-code SSOT's "IO failure" cell) — no traceback, and NOT rc=1, which
    would read as "your config has a violation".

    Args:
        path: Filesystem path to write.
        content: Text content.
        flag: The CLI flag *path* came from (e.g. ``"-o/--output"``). Pass it
              whenever the path is operator-supplied so the message can say
              which flag to check; leave ``None`` for internal paths.
        exit_code: Process exit code on failure.
    """
    try:
        write_text_secure(path, content, flag=flag)
    except OutputWriteError as exc:
        _die_on_write_error(exc, exit_code)


def write_json_or_die(
    path: str,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    flag: Optional[str] = None,
    exit_code: int = EXIT_CALLER_ERROR,
) -> None:
    """:func:`write_json_secure`, but an unusable path ends the process.

    Same contract as :func:`write_text_or_die` (#1641).
    """
    try:
        write_json_secure(path, data, indent=indent, ensure_ascii=ensure_ascii, flag=flag)
    except OutputWriteError as exc:
        _die_on_write_error(exc, exit_code)


def ensure_dir(path: Any, *, flag: Optional[str] = None) -> None:
    """``mkdir -p`` *path*, raising :class:`OutputWriteError` on failure.

    #1641. Output-directory tools create the directory themselves before the
    first secure write, and ``os.makedirs(..., exist_ok=True)`` raises the
    same ``OSError`` family (``NotADirectoryError`` when a path component is
    a file, ``PermissionError``, …) OUTSIDE the writer. Routing the mkdir
    through here keeps the failure in the one class, with the same message
    shape (``cannot create directory <path>: …``).
    """
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputWriteError(path, exc, flag=flag, action="create directory") from exc


def ensure_dir_or_die(
    path: Any,
    *,
    flag: Optional[str] = None,
    exit_code: int = EXIT_CALLER_ERROR,
) -> None:
    """:func:`ensure_dir`, but an unusable path ends the process (rc=2)."""
    try:
        ensure_dir(path, flag=flag)
    except OutputWriteError as exc:
        _die_on_write_error(exc, exit_code)


def write_onboard_hints(
    output_dir: str,
    hints: dict[str, Any],
    *,
    flag: Optional[str] = None,
) -> str:
    """Write onboard hints JSON for scaffold consumption.

    Args:
        output_dir: Directory to write ``onboard-hints.json`` into.
        hints: Data dict (tenants, db_types, routing_hints, …).
        flag: The CLI flag *output_dir* came from; see :func:`write_text_secure`.

    Returns:
        Absolute path to the written file.

    Raises:
        OutputWriteError: see :func:`write_json_secure` (#1641).
    """
    path = str(Path(output_dir) / ONBOARD_HINTS_FILENAME)
    write_json_secure(path, hints, flag=flag)
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
