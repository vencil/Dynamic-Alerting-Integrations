#!/usr/bin/env python3
"""check_doc_datools_cmds.py — documented `da-tools` binary-wrapper subcommands
must be valid.

Static guard for the #141 Track A / F3 class: the try-local README showed
``da-tools ... guard /conf.d``, but the shipped CLI takes ``guard
defaults-impact --config-dir ...`` — a stale subcommand that only surfaced
when a human ran it. No check covered command validity, so it shipped.

**Scope decision.** A broader check (validate every ``da-tools <command>``
against the full CLI command tree) was prototyped and rejected: scenario docs
use illustrative / aspirational pseudo-commands even inside code blocks
(``da-tools describe-tenant``, ``list-tenants``, ``upgrade-check`` per issue
#405), giving ~88 false positives — the same noise that sank a broad
path-existence lint. So this is scoped to the three **binary-wrapper**
commands (``guard`` / ``parser`` / ``batch-pr``), whose subcommand sets are a
small, stable, real contract — and which is exactly where F3 lived.

Only fenced code blocks are scanned (prose mentions and inline-code
suggestions are not runnable). Lines with a ``<placeholder>`` or an inline
``datools-cmd-ignore`` are skipped; ``guard --help`` / ``-h`` is allowed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Set

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
OPS_DIR = REPO_ROOT / "scripts" / "tools" / "ops"
DOCS_DIR = REPO_ROOT / "docs"

# Binary-wrapper command -> valid subcommands. Source of truth: the
# `Subcommands:` block of each dispatcher in scripts/tools/ops/*_dispatch.py
# (mirrors the Go binary). Kept as a literal for robustness; the self-test
# `test_subcommand_map_matches_dispatchers` greps the dispatchers so this drifts
# loudly if a subcommand is added/removed.
WRAPPER_SUBCOMMANDS: Dict[str, Set[str]] = {
    "guard": {"defaults-impact"},
    "parser": {"import", "allowlist"},
    "batch-pr": {"apply", "refresh", "refresh-source"},
}

# `da-tools` (binary) or `da-tools:vX.Y.Z` (image), then the wrapper command +
# whatever token follows (the candidate subcommand).
_DATOOLS_RE = re.compile(
    r"da-tools(?::v[0-9.]+)?\s+(guard|parser|batch-pr)(?:\s+([^\s\\]+))?")

_PLACEHOLDER_CHARS = "<>${}"
INLINE_IGNORE = "datools-cmd-ignore"


class Issue(NamedTuple):
    check: str
    file: str
    line: int
    message: str

    def to_dict(self) -> dict:
        return self._asdict()


def _doc_files(docs_dir: Path) -> List[Path]:
    return [f for f in sorted(docs_dir.rglob("*.md"))
            if "/internal/archive/" not in f.as_posix()]


def check_datools_subcommands(doc_files: List[Path],
                              sub_map: Dict[str, Set[str]],
                              repo_root: Path) -> List[Issue]:
    issues: List[Issue] = []
    for f in doc_files:
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        rel = str(f.relative_to(repo_root)).replace("\\", "/")
        in_code = False
        for i, raw in enumerate(lines, 1):
            # ⛔ Same fence handling as the mount rule below. Fixing blockquote
            # fences only there left this check blind to the very file the fix
            # was about — the shared include is one document, and "the class"
            # is both rules, not the one that was pointed at.
            line = _unquote_md(raw)
            if _is_fence(line):
                in_code = not in_code
                continue
            # Only fenced code blocks hold real invocations; prose / inline-code
            # mentions are illustrative and must not be flagged.
            if not in_code:
                continue
            if INLINE_IGNORE in line or any(c in line for c in _PLACEHOLDER_CHARS):
                continue
            for m in _DATOOLS_RE.finditer(line):
                wrapper, nxt = m.group(1), m.group(2)
                valid = sub_map.get(wrapper, set())
                # A bare `--flag` (e.g. --help) is a valid invocation.
                if nxt is None or nxt.startswith("-"):
                    continue
                if nxt not in valid:
                    issues.append(Issue(
                        "datools-bad-subcommand", rel, i,
                        f"da-tools {wrapper} '{nxt}' is not a subcommand "
                        f"(valid: {', '.join(sorted(valid))})"))
    return issues


# Customer-facing markdown that lives OUTSIDE docs/. Enumerated by FILE, not
# by spelling: these are the entry points a customer actually lands on, and
# each already carries `docker run` examples. A new one has to be added here —
# accepted, because the alternative (scan every .md in the repo) pulls in
# internal notes and archived reports whose examples are deliberately stale.
_EXTRA_DOC_FILES = (
    "components/da-tools/README.md",
    "components/da-tools/app/QUICKSTART.md",
    "try-local/README.md",
)

_DOCKER_RUN_RE = re.compile(r"\bdocker\s+run\b")
_DATOOLS_IMAGE_RE = re.compile(r"da[-_]tools", re.IGNORECASE)
# The image token itself — everything before it is a docker flag, everything
# after it is an argument handed to the container.
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")

# docker flags that consume the NEXT token as their value. Needed to tell the
# image apart from a flag argument that merely happens to mention da-tools.
_VALUE_FLAGS = frozenset({
    "-v", "--volume", "-e", "--env", "-w", "--workdir", "-u", "--user",
    "--name", "--network", "--entrypoint", "--mount", "--label", "-l",
    "--env-file", "--add-host", "-p", "--publish",
})


def _image_index(toks: List[str]) -> "int | None":
    """Index of the IMAGE token — the first bare operand after `docker run`.

    ⛔ Not "the first token containing da-tools". A mount path may contain it
    (`-v $(pwd)/da-tools-out:/data/output`), and taking that as the image made a
    correctly-placed `--user` look like it came after the image — a誤紅 whose
    message says "move it ahead of the image" when it already is, leaving no
    legal way to go green.
    """
    i = 0
    while i < len(toks) and toks[i] != "run":
        i += 1
    i += 1
    operands = []
    while i < len(toks):
        t = toks[i]
        if t in _VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        operands.append(i)
        i += 1
    # ⛔ TIER 1 — the operand must actually LOOK like the image. `_VALUE_FLAGS`
    # is an enumeration, and an enumeration of docker's flags is never complete:
    # any value-taking flag missing from it (`--platform linux/amd64`,
    # `--group-add 999`, `--userns host`, `--security-opt label=disable`, …)
    # donates its VALUE as the "image", so a `--user` that is correctly ahead of
    # the real image gets reported as coming after it — with a message
    # prescribing a move that is already done, i.e. no legal way to go green.
    # Measured: four such flags each produced that誤紅.
    #
    # This predicate is derivable rather than enumerated, and the two are
    # complementary: the flag list still has to skip `-v
    # $(pwd)/da-tools-out:/data/output`, whose VALUE does contain da-tools.
    for k in operands:
        if _DATOOLS_IMAGE_RE.search(toks[k]):
            return k
    # ⛔ TIER 2 — no da-tools-shaped operand at all. Returning None here is
    # fail-OPEN: the caller's position check is `img_i is not None and ...`, so
    # a genuinely misplaced `--user` in an example that pulls the tool under a
    # different image name (a private mirror, a renamed build) passes silently.
    # Measured: `-v $(pwd)/da-tools-out:/data/output ghcr.io/acme/platform-cli:v1
    # --user 1000 init` was reported CLEAN once tier 1 was the only tier, while
    # the same shape with a da-tools image was correctly flagged.
    #
    # Falling back to the FIRST bare operand restores that. It cannot reopen the
    # tier-1誤紅 class: those all carry a real da-tools image, so tier 1 wins
    # before this line is ever reached.
    return operands[0] if operands else None


def _unquote_md(line: str) -> str:
    """Drop one level of markdown blockquote prefix.

    ⛔ The shared `docs/includes/docker-usage-pattern{,.en}.md` — the pattern
    every other page copies — puts its fence inside a blockquote (``> ```bash``).
    Without this, `startswith("```")` is False, the code block never opens, and
    that file is invisible: reverting the fix it carries left the guard GREEN.
    """
    s = line.lstrip()
    return s[2:] if s.startswith("> ") else (s[1:] if s.startswith(">") else line)


def _is_fence(line: str) -> bool:
    # `~~~` is a valid mkdocs fence too; treating only ``` as one leaves a
    # whole fence style unscanned.
    s = line.lstrip()
    return s.startswith("```") or s.startswith("~~~")


# `${{ github.workspace }}` — a CI template whose INTERNAL spaces would split
# a mount spec into fragments. Collapsed to a space-free token so the spec
# stays parseable. ⛔ It is deliberately NOT skipped: an earlier version
# excluded any spec containing `{{`, and that exclusion hid a real
# customer-facing defect (a GitHub Actions example whose writable `/output`
# mount had no `--user`, on a command that writes unconditionally). Removing a
# 誤紅 by widening an exclusion is how a guard acquires a false GREEN.
# ⛔ `.*?\}\}`, not `[^}]*`. A CI expression may contain braces of its own —
# `${{ format('{0}/out', github.workspace) }}` — and a class that stops at the
# first `}` fails to match it, so the mount is split on its spaces, becomes the
# fragment `${{`, is neither a bind mount nor a placeholder, and vanishes
# SILENTLY. That is the exact failure this round was about; leaving a second
# spelling of it in place would repeat it.
_CI_TEMPLATE_RE = re.compile(r"\$\{\{.*?\}\}")


def _normalise(flat: str) -> str:
    return _CI_TEMPLATE_RE.sub(lambda m: "".join(m.group(0).split()), flat)


def _mounts(flat: str) -> List[str]:
    """Every `-v` / `--volume` spec in *flat*, quoting-normalised.

    ⛔ Strip quotes and trailing punctuation BEFORE reading the option field.
    A naive regex reads `"$(pwd)/conf.d:/conf.d:ro"` as writable because the
    closing quote lands in the option group — i.e. it over-reports, which for
    a guard means誤紅 on examples that are already correct.

    ⚠️ `--mount type=bind,...` is NOT recognised. Disclosed rather than
    modelled: it is a different syntax with its own option grammar, and this
    rule would need a second parser to judge it. Consequence: rewriting `-v`
    as `--mount` leaves the example unchecked.
    """
    # ⛔ Drop continuation backslashes HERE too. This function does its own
    # split, so filtering them in the caller's token list left this path
    # unchanged: a `-v` at the end of a wrapped line took `\` as its value,
    # which `rstrip` reduced to the empty string — no mount, silently clean.
    toks = [t for t in _normalise(flat).split() if t != "\\"]
    out: List[str] = []
    for i, t in enumerate(toks):
        if t in ("-v", "--volume") and i + 1 < len(toks):
            out.append(toks[i + 1].strip("\"'").rstrip("\\,;)]\"'"))
        elif t.startswith(("--volume=", "-v=")):
            # `--volume=host:ctr` is the same mount in the form docker also
            # accepts; not recognising it was a second silent pass.
            out.append(t.split("=", 1)[1].strip("\"'").rstrip("\\,;)]\"'"))
    return out


def _is_bind_mount(spec: str) -> bool:
    """Is *spec* a HOST-PATH mount (as opposed to a named/anonymous volume)?

    ⛔ Only bind mounts are in this rule's domain. A named volume
    (`da-tools-cache:/cache`) has no host directory and no host uid: docker
    seeds it from the image, ownership included, so the container's own user
    can write there and `--user $(id -u)` would BREAK it — measured both ways.
    An anonymous volume (`-v /cache`, one segment) is the same story. Flagging
    those would be a誤紅 whose prescribed remedy is actively harmful.
    """
    # ⛔ Windows drive letters must be recognised BEFORE splitting on ":".
    # An earlier version tested `head[1] == ":"` on `spec.split(":")[0]`, where
    # a colon can never appear — dead code that nonetheless carried a comment
    # claiming the case was handled, so `C:\Users\me:/data/output` was silently
    # treated as a named volume and skipped.
    if _WINDOWS_PATH_RE.match(spec):
        # …but a drive letter alone is not a mount: `-v C:\cache` has no
        # container path, so it is still an anonymous volume. The colon that
        # `":" not in spec` was meant to catch IS the drive's colon, which is
        # why that guard cannot be the one asking.
        return ":" in spec[2:]
    if spec.startswith("\\\\"):
        # UNC (`\\server\share:/data`) is a host path, so it IS a bind mount;
        # judging it as a named volume was fail-open.
        return ":" in spec[2:]
    if ":" not in spec:
        return False                      # anonymous volume
    head = spec.split(":")[0]
    return head.startswith(("/", ".", "~", "$"))


def _is_writable(spec: str) -> bool:
    parts = spec.split(":")
    # A Windows host path (`C:\x:/y`) shifts the field count; only treat the
    # last field as options when it looks like one.
    opts = parts[-1].split(",") if len(parts) >= 3 else []
    return not any(o in ("ro", "readonly") for o in opts)


def _has_user_flag(toks: List[str]) -> bool:
    """`--user`, `--user=…` or the official short form `-u`.

    ⛔ Not `"--user" in flat`. That substring test both over- and
    under-matches: it misses `-u $(id -u)` (a correct command, reported as
    missing the flag, with a message telling the author to add what is already
    there) and it accepts `--userns=keep-id`, which sets no uid at all.
    """
    return any(t == "--user" or t == "-u" or t.startswith("--user=")
               for t in toks)


def check_writable_mount_has_user(doc_files: List[Path],
                                  repo_root: Path) -> List[Issue]:
    """A da-tools `docker run` with a WRITABLE mount must pass `--user`.

    The shipped image ends ``USER nonroot:nonroot`` (uid 10001,
    ``components/da-tools/app/Dockerfile``) while the directory a customer
    mounts is their own checkout (typically uid 1000). Any subcommand that
    writes then dies on a bare ``PermissionError`` traceback with zero files
    produced — measured inside a single Linux container: uid 10001 fails,
    uid 1000 succeeds (#1495).

    ⛔ The predicate is the MOUNT, not the subcommand. "Which subcommands
    write" is a list that has to be maintained against every tool's argparse
    (and the same tool writes or not depending on its flags — ``generate-routes
    --validate`` exits before it touches ``-o``). A writable mount is the
    example's own declaration of intent, so the invariant reads: if you
    declared you may write there, pass the uid that can. An example that never
    writes should say so with ``:ro`` and is then out of scope by construction.

    Mount specs that are still placeholders (``<host>:/path``) are skipped —
    there is nothing to judge yet. ⚠️ Note this is NOT the subcommand check's
    ``_PLACEHOLDER_CHARS`` set: that one contains ``$``, which appears in
    essentially every real example, and reusing it here skipped 97 of 122
    blocks including every site this rule was written to hold.

    ⚠️ **Known NOT covered — disclosed rather than left to be discovered:**

    1. ``--mount type=bind,...`` is a different syntax and is not parsed, so
       rewriting ``-v`` as ``--mount`` leaves an example unchecked.
    2. **Writes to a RELATIVE default output path** are a different failure with
       the same symptom, and ``--user`` does not fix them: e.g. ``validate``
       defaults ``--output-dir`` to ``validation_output``, which resolves under
       the image's ``WORKDIR /opt/da-tools`` — root-owned, so uid 10001 AND the
       customer's uid both fail. The remedy there is ``-o`` into a mounted path
       or ``-w /workspace``, and this rule cannot see it because such examples
       legitimately mount nothing writable.
    3. **tenant-api's docs** have the same root cause (that image is also
       non-root and its gitops writer creates temp files inside the mounted
       ``conf.d``) but are outside this scan set.
    4. **A template ``_normalise`` cannot join back together is skipped in
       silence.** Measured, three forms behave differently and only the third
       is blind: ``${{ github.workspace }}/out:/output`` is joined and judged
       (skipping it once hid a live defect), and so is the nested-brace ``${{
       format('{0}/out', github.workspace) }}:/output`` — the joined spec still
       starts with ``$``, which is what ``_is_bind_mount`` keys on. What falls
       out unchecked is a template that is not GitHub's at all
       (``{{ mkdocs_var }}/out:/output`` — the joined head is ``}}``) or an
       unterminated one (``${{ github.workspace /out:/output`` — the ``-v``
       value becomes the ``${{`` fragment, so the real spec is never a mount
       operand). Both are CLEAN today.
       A revision that reported these ("cannot be judged") was removed: judging
       by BRACE PRESENCE is an appearance test standing in for the consequence
       "could this spec be parsed", and it fired on the supported ``${{ x }}``
       form, on plain ``${PWD}``/``${HOME}`` shell expansion, and on ``:ro``
       specs out of this rule's domain — while DOWNGRADING the one real defect
       it existed to protect from a named finding with a correct fix to
       "unjudgeable". Two brace-shaped predicates were wrong in this spot; the
       fix, if taken, is a consequence test (does the spec parse?), not a third
       appearance test.

    (2), (3) and (4) are tracked separately; they are not "rare edge cases" but
    classes this invariant genuinely does not express.
    """
    issues: List[Issue] = []
    for f in doc_files:
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        rel = str(f.relative_to(repo_root)).replace("\\", "/")
        in_code = False
        i = 0
        while i < len(lines):
            line = _unquote_md(lines[i])
            if _is_fence(line):
                in_code = not in_code
                i += 1
                continue
            if not in_code or not _DOCKER_RUN_RE.search(line):
                i += 1
                continue
            start = i
            buf = [line]
            while (buf[-1].rstrip().endswith("\\")
                   and i + 1 < len(lines)
                   and not _is_fence(_unquote_md(lines[i + 1]))):
                i += 1
                buf.append(_unquote_md(lines[i]))
            blk = "\n".join(buf)
            i += 1
            flat = " ".join(blk.split())
            if not _DATOOLS_IMAGE_RE.search(flat):
                continue
            # ⛔ NOT `_PLACEHOLDER_CHARS`. That set is `<>${}`, and `$` appears
            # in essentially every real example (`-v $(pwd)/conf.d:...`), so
            # reusing it here skipped 97 of 122 blocks — including all fifteen
            # this rule was written to hold. Measured before shipping, which is
            # the only reason it was noticed: the check was GREEN either way.
            #
            # The two checks need different skips because they judge different
            # things. A `<command>` placeholder makes the SUBCOMMAND
            # unjudgeable; it says nothing about the MOUNT, and `$(pwd)` is a
            # real shell expression rather than a placeholder at all. So skip
            # only when the mount spec itself is a placeholder.
            if INLINE_IGNORE in blk:
                continue
            norm = _normalise(flat)
            # ⛔ Drop the line-continuation backslashes. Flattening a multi-line
            # command leaves each `\` as its own token, and a bare `\` is not a
            # flag — so the FIRST one was picked as the image and every
            # correctly-ordered `--user` in a multi-line example was reported as
            # coming after it. The single-line probe I first checked this with
            # had no backslashes, which is precisely why it looked fine.
            toks = [t for t in norm.split() if t != "\\"]
            specs = [s for s in _mounts(flat) if _is_bind_mount(s)]
            writable = [m for m in specs if _is_writable(m)]
            img_i = _image_index(toks)
            if _has_user_flag(toks):
                # ⛔ Position matters as much as presence. Docker applies only
                # the flags BEFORE the image; a `--user` after it is handed to
                # the container as an argument — the uid is unchanged AND the
                # argv is polluted. Measured on an image ending `USER 10001`:
                # flag-after-image gave the same euid as no flag at all.
                # ⛔ Compare TOKEN indices. `norm.index(tok)` returns the first
                # SUBSTRING occurrence, which lands anywhere the same text
                # appears earlier — `-v $(pwd)/build-utils:...` contains `-u`,
                # so a genuinely misplaced short flag reported as fine.
                user_i = next(k for k, t in enumerate(toks)
                              if t in ("--user", "-u") or t.startswith("--user="))
                if writable and img_i is not None and user_i > img_i:
                    issues.append(Issue(
                        "datools-user-flag-after-image", rel, start + 1,
                        "`--user` appears AFTER the image reference, so docker "
                        "passes it to the container instead of applying it — "
                        "the uid is unchanged and the tool sees two junk "
                        "arguments. Move it ahead of the image (#1495)."))
                continue
            # `<host>:/path` is still a placeholder — nothing to judge yet.
            # ⚠️ `${{ … }}` is NOT excluded any more: it is a real CI mount
            # with a template in it, and skipping those hid a live defect.
            if any(("<" in s or ">" in s) for s in specs):
                continue
            # ⛔ BLIND SPOT, deliberately left open — see the module docstring.
            # A previous revision reported every spec that still carried a brace
            # after normalisation ("this example cannot be judged"). Measured, it
            # cost more than it bought: it fired on `${{ github.workspace }}`
            # (the supported form, which `_normalise` DOES join — the message
            # then prescribed the spelling already in use, leaving no legal way
            # to go green), on plain `${PWD}`/`${HOME}` shell expansion, and on
            # `:ro` specs this rule does not judge at all. Worse, it DOWNGRADED
            # the one real defect it was supposed to protect: a writable
            # `${{ … }}` mount stopped being named as
            # `datools-writable-mount-without-user` (with the correct fix) and
            # became "unjudgeable". Braces are an APPEARANCE test standing in
            # for a CONSEQUENCE ("could this spec be parsed"); two successive
            # brace-shaped predicates got it wrong in this same spot, so the
            # third one is not attempted here.
            if writable:
                issues.append(Issue(
                    "datools-writable-mount-without-user", rel, start + 1,
                    f"writable mount(s) {writable} but no --user; the image "
                    f"runs as uid 10001 and a customer's directory does not, "
                    f"so anything this writes fails with PermissionError. "
                    f"Add `--user $(id -u):$(id -g)`, or mark the mount `:ro` "
                    f"if it is only read (#1495)."))
    return issues


def run(repo_root: Path = REPO_ROOT) -> List[Issue]:
    docs = _doc_files(repo_root / "docs")
    extra = [repo_root / rel for rel in _EXTRA_DOC_FILES]
    return (check_datools_subcommands(docs, WRAPPER_SUBCOMMANDS, repo_root)
            + check_writable_mount_has_user(docs + [f for f in extra
                                                    if f.is_file()], repo_root))


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ci", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    issues = run()
    if args.json:
        print(json.dumps({"issues": [i.to_dict() for i in issues],
                          "count": len(issues)}, ensure_ascii=False, indent=2))
    elif issues:
        for it in issues:
            print(f"  ❌ [{it.check}] {it.file}:{it.line} — {it.message}",
                  file=sys.stderr)
        print(f"\n❌ {len(issues)} da-tools subcommand issue(s)", file=sys.stderr)
    else:
        print("✅ documented da-tools wrapper subcommands are valid")
    return EXIT_VIOLATION if (issues and args.ci) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
