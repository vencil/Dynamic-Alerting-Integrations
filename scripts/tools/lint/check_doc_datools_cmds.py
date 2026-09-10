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
# ⛔ The tag part must accept ANY reference form, not just `:vN.N`. The
# previous `(?::v[0-9.]+)?` silently excluded `:latest` (36 occurrences in the
# scanned docs), `:2.9.0`, `:v2.9.0-rc1` and `@sha256:…` digest pins — and two
# of those `:latest` lines carried the stale `guard --conf-d` invocation, so
# the narrow tag pattern was a second, independent way to miss the same defect.
_DATOOLS_RE = re.compile(
    r"da-tools(?:[:@][^\s\\]+)?\s+(guard|parser|batch-pr)(?:\s+([^\s\\]+))?")

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
                # ⛔ Only `--help` / `-h` are valid WITHOUT a subcommand. The
                # previous predicate was `nxt.startswith("-")`, i.e. the code
                # was broader than the comment right above it claimed — every
                # dash-prefixed token was waved through. Measured on the tree
                # this narrowing landed in: it turned 12 shipped invocations of
                # the exact class this checker exists for
                # (`da-tools guard --conf-d conf.d/ --layer schema`, which the
                # real dispatcher answers with `Error: unknown guard subcommand
                # '--conf-d'`) from CLEAN into findings, with zero new false
                # positives across all of docs/.
                if nxt is None or nxt in ("--help", "-h"):
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

    The image is the pivot of the whole command: everything BEFORE it is a
    docker flag, everything after it is an argument handed to the container.
    That is why the index — not merely the presence — of `--user` matters.

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
            # ⛔ Drop placeholder specs INDIVIDUALLY, not the whole block. A
            # `<…>` spec cannot be judged, but its neighbours in the same
            # example can. Measured: `-v /srv/<tenant>/conf.d:/data/conf.d -v
            # $(pwd)/out:/data/output` with no `--user` reported 0 issues,
            # while the identical `$(pwd)/out` mount alone reported 1 — one
            # unjudgeable spec was silencing a judgeable sibling.
            # ⚠️ The reviewer's own example (`-v <your-repo>:/workspace`) does
            # NOT reach here: `_is_bind_mount` already rejects it, so it never
            # entered `specs`. The reachable shape is a placeholder embedded in
            # an otherwise concrete path, which does pass that filter.
            specs = [s for s in specs if not ("<" in s or ">" in s)]
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
            # ⚠️ `${{ … }}` is NOT excluded: it is a real CI mount with a
            # template in it, and skipping those hid a live defect.
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
                    f"Add `--user $(id -u):$(id -g)` (#1495)."))
                    # ⛔ This message deliberately offers exactly ONE remedy.
                    # It used to end "or mark the mount `:ro` if it is only
                    # read", and `:ro` is seven characters against a judgement
                    # call — so it is the cheaper fix for anyone who just wants
                    # green. Measured on a real example that writes via
                    # `--output /data/output/...`: deleting `--user` reports 1
                    # issue, appending `:ro` reports 0. The customer's failure
                    # merely changes from PermissionError to "Read-only file
                    # system", and the block leaves the rule's judged set for
                    # good (an empty `writable` also disables the
                    # user-flag-after-image check on it). `:ro` remains the
                    # correct annotation for a genuinely read-only mount — but
                    # that is an author's up-front declaration, not something
                    # to suggest to someone at the moment they are blocked.
    return issues


# --- pinned invocations, for dx/bump_docs.py (#1534) ------------------------
#
# `bump_docs --tools X.Y.Z` mechanically repoints every documented
# `ghcr.io/vencil/da-tools:vX.Y.Z` pin. Only the `k8s/03-monitoring/cronjob-*`
# targets land inside the scan surface of check_image_pin_capability.py
# (`k8s/**` + `helm/*`); the rest rewrite pins in prose that NOTHING checks for
# capability — so a doc could keep teaching `docker run ...:vNEW <subcommand>`
# for a subcommand the new image does not dispatch, and every gate stays green.
# These helpers give bump_docs the missing oracle. List the split with:
#
#   python3 -c "import importlib.util,pathlib; s=importlib.util.spec_from_file_location('b',pathlib.Path('scripts/tools/dx/bump_docs.py')); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); \
#     print([r.get('glob_dir', r['file']) for r in m._build_tools_rules() if 'ghcr\\\\.io/vencil/da-tools:v?' in r.get('pattern','')])"
#
# ⛔ This is deliberately NOT a new standalone gate over every documented
# subcommand. That shape was prototyped and rejected as too FP-heavy (#405) —
# see the Scope decision in this module's docstring. What keeps the precision
# here is the narrowing, not the capability lookup: only fenced blocks, only
# `docker run`, and only the first bare operand AFTER the image as located by
# `_image_index`.

# The image reference must carry a real `:vX.Y.Z`. `:latest` and untagged
# mentions are OUT OF SCOPE by declaration, not by oversight: they name no tag,
# so there is no capability set to check them against (#1534 records this as a
# separate problem).
# ⛔ `v?`, matching the bump rules' own `da-tools:v?<SEMVER>` pattern. Requiring
# the `v` left `…/da-tools:2.9.0` rewritten by every release but invisible to
# this check — the exact "bumped but never verified" gap this exists to close.
_PINNED_TAG_RE = re.compile(
    r"da-tools:(v?[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9._-]+)?)")

# Shell punctuation that can abut the subcommand token. No da-tools subcommand
# contains any of these, so cutting at the first one is lossless.
_SHELL_META_RE = re.compile(r"[;|&()<>]")


class PinnedInvocation(NamedTuple):
    file: str
    line: int
    tag: str
    subcommand: str


def iter_pinned_invocations(doc_files: List[Path],
                            repo_root: Path = REPO_ROOT
                            ) -> List[PinnedInvocation]:
    """Every `docker run <da-tools:vX.Y.Z> <subcommand>` in a fenced block."""
    found: List[PinnedInvocation] = []
    for f in doc_files:
        # ⛔ NOT `except OSError: continue`. `check_datools_subcommands` above
        # can afford that (it is one of several rules over the same corpus, and
        # a skipped file merely under-reports one advisory check). Here the
        # caller is a RELEASE gate whose whole claim is "every documented
        # invocation was checked" — a file that silently drops out turns that
        # claim false while the release reports success. Fail closed instead.
        try:
            lines = f.read_text(encoding="utf-8",
                                errors="ignore").splitlines()
        except OSError as exc:
            raise RuntimeError(
                f"{f} could not be read, so the invocations it documents "
                f"cannot be checked against the tag being released. Refusing "
                f"to report a clean result over a corpus that lost a file."
            ) from exc
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
            if INLINE_IGNORE in blk:
                continue
            flat = " ".join(blk.split())
            if not _DATOOLS_IMAGE_RE.search(flat):
                continue
            toks = [t for t in _normalise(flat).split() if t != "\\"]
            k = _image_index(toks)
            if k is None:
                continue
            # ⛔ `--entrypoint` replaces the program, so what follows the image
            # is that program's argv, not a da-tools subcommand. Grading it
            # reports a "subcommand" the CLI was never asked to run — a red on
            # a correct example, which for a release gate is the costly
            # direction. Measured: `--entrypoint /bin/sh … -c 'ls'` reported
            # `runs 'ls'`.
            #
            # ⛔ Two things the first version of this got wrong, both measured:
            #   * `in toks` missed `--entrypoint=/bin/sh`. Docker accepts the
            #     `=` form, and the un-skipped block then graded the shell's
            #     argv — the same 誤紅 this skip exists to prevent, via a
            #     spelling it did not cover.
            #   * Scanning the WHOLE token list also skipped blocks where
            #     `--entrypoint` appears AFTER the image, i.e. as an argument
            #     handed to da-tools rather than a docker flag. Docker only
            #     applies flags before the image, so such a block has a normal
            #     entrypoint and its subcommand is judgeable; skipping it was
            #     fail-OPEN (`… :v2.9.0 frobnicate --entrypoint x` reported 0).
            # Hence: only the tokens BEFORE the image, and both spellings.
            if any(t == "--entrypoint" or t.startswith("--entrypoint=")
                   for t in toks[:k]):
                continue
            tag_m = _PINNED_TAG_RE.search(toks[k])
            if tag_m is None:
                continue
            sub = next((t for t in toks[k + 1:] if not t.startswith("-")), None)
            # ⛔ Cut at the first shell metacharacter, then unquote — the same
            # over-reporting concern `_mounts` documents: a guard that
            # over-reports goes red on examples that are already correct. A
            # subcommand is routinely followed by `;`, `| jq`, or the closing
            # `)` of `$(…)`, and may be quoted. ⚠️ rstrip alone is not enough:
            # `validate|jq` ends in `q`, so the pipe has to be SPLIT on, not
            # stripped. All four shapes reported a valid `validate` as unknown
            # before this; no real subcommand contains any of these characters.
            if sub is not None:
                sub = _SHELL_META_RE.split(sub, 1)[0].strip("\"'").rstrip("\\,")
            # No operand at all is `--help` or a bare image — nothing claimed,
            # nothing to check. A placeholder (`<command>`) is a deliberate
            # "fill this in", not an assertion that the command exists.
            if not sub or any(c in sub for c in _PLACEHOLDER_CHARS):
                continue
            found.append(PinnedInvocation(rel, start + 1, tag_m.group(1), sub))
    return found


def check_pinned_subcommands_against(command_map: Dict[str, str],
                                     tool_files: Set[str],
                                     doc_files: List[Path],
                                     repo_root: Path = REPO_ROOT
                                     ) -> List[Issue]:
    """Documented pinned invocations must be RUNNABLE by the image, not merely
    dispatched by it.

    Two questions, the same pair `check_image_pin_capability.evaluate` asks of a
    workload, because "can this image run this" has the same answer here:

      1. is the subcommand in COMMAND_MAP?  (else `Unknown command`)
      2. is the script it maps to in build.sh TOOL_FILES?  (else the command
         dispatches and the container dies on a missing file)

    ⛔ Asking only (1) is the #1044 shape — registered but never copied into the
    image. It reads as covered while the customer's run fails at a different
    layer. The two are separate findings because the fixes differ: (1) is the
    doc naming the wrong command, (2) is build.sh not shipping a real one.

    ⛔ An empty COMMAND_MAP or TOOL_FILES is refused rather than reported clean
    — an oracle that knows nothing marks every invocation bad or (if inverted)
    every invocation fine, and both are indistinguishable from "no findings" at
    the call site. `capabilities_for_tag` refuses the same way, for the reason.
    """
    if not command_map or not tool_files:
        raise ValueError(
            "refusing to check documented invocations against an empty "
            "COMMAND_MAP and/or TOOL_FILES — the capability oracle is the "
            "thing being trusted here, and an empty one silently grades "
            "everything."
        )
    issues: List[Issue] = []
    for inv in iter_pinned_invocations(doc_files, repo_root):
        script = command_map.get(inv.subcommand)
        if script is None:
            issues.append(Issue(
                "datools-pin-capability", inv.file, inv.line,
                f"documented `da-tools:{inv.tag}` runs '{inv.subcommand}', "
                f"which the image does not dispatch (it exits with "
                f"`Unknown command`). Either the doc names a command that was "
                f"renamed/removed, or the command is new and this release does "
                f"not ship it yet."))
        elif script not in tool_files:
            issues.append(Issue(
                "datools-pin-not-shipped", inv.file, inv.line,
                f"documented `da-tools:{inv.tag}` runs '{inv.subcommand}', "
                f"which COMMAND_MAP maps to {script} — but that file is NOT in "
                f"build.sh TOOL_FILES, so it is registered and never copied "
                f"into the image. The command dispatches and then fails on a "
                f"missing file. Add {script} to TOOL_FILES, or stop "
                f"documenting the command."))
    return issues


def pin_capability_doc_files(repo_root: Path = REPO_ROOT) -> List[Path]:
    """The corpus bump_docs checks: docs/ plus the customer-facing landing pages.

    Deliberately the SAME corpus `run()` uses, so widening one widens the other
    and the two cannot drift into disagreeing about what "the docs" means.

    ⛔ Missing inputs RAISE; they are never quietly dropped. An earlier version
    filtered the extras with `if …is_file()` — the exact fail-open shape the ⛔
    note in `run()` calls "the same silent-gap shape this whole checker exists
    to close". Renaming a landing page would have shrunk the release check's
    corpus with no signal, and neither `components/da-tools/app/QUICKSTART.md`
    nor `try-local/README.md` is a bump-rule target, so nothing else would have
    noticed. The empty-`docs/` floor is the #1790 lesson applied here: a corpus
    that collapsed to nothing must not read as "checked, all clean".
    """
    docs = _doc_files(repo_root / "docs")
    if not docs:
        raise RuntimeError(
            f"{repo_root / 'docs'} yielded no markdown — refusing to report "
            f"documented invocations as clean over an empty corpus."
        )
    missing = [rel for rel in _EXTRA_DOC_FILES
               if not (repo_root / rel).is_file()]
    if missing:
        raise RuntimeError(
            "listed in _EXTRA_DOC_FILES but not found: "
            + ", ".join(missing)
            + ". Point the tuple at each file's current path (if it moved, "
              "this is a rename — follow it). Dropping the entry instead stops "
              "that page being checked at all (#1495)."
        )
    return docs + [repo_root / rel for rel in _EXTRA_DOC_FILES]


def run(repo_root: Path = REPO_ROOT) -> List[Issue]:
    docs = _doc_files(repo_root / "docs")
    # ⛔ A missing entry is reported, never silently skipped. The previous
    # `[f for f in extra if f.is_file()]` meant that renaming or moving one of
    # these landing pages shrank the scan surface with no signal at all — the
    # scan would keep passing while the file it was widened to cover had
    # stopped being read. That is the same silent-gap shape this whole checker
    # exists to close, so it must not be how the checker handles its own list.
    present, missing = [], []
    for rel in _EXTRA_DOC_FILES:
        (present if (repo_root / rel).is_file() else missing).append(rel)
    # ⛔ The message names exactly one remedy, and it is the one that keeps the
    # page covered. An earlier wording offered "either restore the path or drop
    # it from the tuple"; dropping is by far the cheapest and it re-opens the
    # very hole this Issue exists to close. Measured: rename a landing page ->
    # 1 `datools-doc-file-missing`; drop it from the tuple -> 0 issues, and the
    # writable-mount defect on that same page then ships unreported. Renaming
    # is also the likeliest trigger, and "update the tuple to the new path" was
    # the one option the old message did not list.
    issues = [Issue("datools-doc-file-missing", rel, 1,
                    "listed in _EXTRA_DOC_FILES but not found. Point the tuple "
                    "at the file's current path (if it moved, this is a rename "
                    "— follow it). Deleting the entry instead stops this page "
                    "being scanned at all, which is how a guard silently stops "
                    "guarding; only do that once the page itself is gone "
                    "(#1495).")
              for rel in missing]
    return (issues
            + check_datools_subcommands(docs, WRAPPER_SUBCOMMANDS, repo_root)
            + check_writable_mount_has_user(
                docs + [repo_root / rel for rel in present], repo_root))


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
