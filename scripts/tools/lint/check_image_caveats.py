#!/usr/bin/env python3
"""Image-caveat gate — docs that say "the published image is still older" must expire.

WHY: docs are published when a change merges; the da-tools image only when a
`tools/v*` tag is cut. In between, a doc that describes new behaviour has to
warn that the image a reader can pull still behaves the old way ("the v2.9.0
image still ..."). Those warnings are true until the next image ships and
wrong the moment it does — and nothing tied them to a version, so every
release had to find them by grep and memory. A fixed-phrase grep found 43
such lines for v2.9.0 and missed at least 14 more worded differently.

HOW: every such caveat carries a marker naming the image version it is about:

    <!-- image-caveat: v2.9.0 -->

(optionally followed by a short note before the `-->`). The marker is an
HTML comment, so neither GitHub nor the docs site renders it. This gate
compares each marker with the da-tools version in
`components/da-tools/app/VERSION` — the version the NEXT `tools/v*` tag
publishes, and the one `bump_docs.py --tools` moves at release:

  * marker version < VERSION  -> STALE: that image is no longer the newest;
    remove the caveat or rewrite it as history.
  * marker version > VERSION, or not a version at all -> MALFORMED.
  * a block that uses the phrasing these caveats are written in
    (`v<X.Y.Z> 映像` / `v<X.Y.Z> image`, `你手上這顆映像`, `the image you have`)
    with no marker in it -> UNMARKED: a new caveat that would otherwise be
    invisible to the first check. A block is a paragraph (a run of non-blank
    lines), except that each table row is its own block and a fenced code
    block also counts the non-blank lines directly above and below the fence
    (a marker cannot go inside a fence without being rendered).

⚠️ Scope boundary, stated so nobody reads more into a green run: the third
check knows those phrasings only. A caveat worded some other way and left
unmarked is not seen — that is why the marker, not the phrasing, is the
contract. Pinned image references (`ghcr.io/vencil/da-tools:vX.Y.Z`) are NOT
this gate's job: `bump_docs.py --tools` rewrites them at release and
`bump_docs.py --check` (first step of `make pre-tag`) fails on any it missed.

SINCE MARKERS (the same gate, the other direction): a doc that says a
behaviour ships in a release ("v3.0.0 起…", "from v3.0.0", "the v3.0.0
contract") is true only if that release is cut under that number — docs once
said v2.10.0 and the release became v3.0.0. Such a statement carries

    <!-- since: v3.0.0 -->

and is checked against the RELEASED set: the `## [vX.Y.Z]` headings of the
root CHANGELOG.md (the wrap-up adds the new heading before `make pre-tag`):

  * marker version <= the newest heading but not a heading itself
    -> SINCE-NEVER-RELEASED: that number was skipped; rewrite the statement.
  * marker version newer than every heading -> fine while developing;
    with `--pre-tag` (what `make pre-tag` passes) SINCE-UNRELEASED: the
    release being tagged is not the one the docs promise.
  * not a version -> SINCE-MALFORMED.
  * a block that says "since / from / before / changed in / retired in
    vX.Y.Z", "vX.Y.Z 起／之前／前／的契約", "the vX.Y.Z contract" or
    "vX.Y.Z+" about a version newer than every heading, with no since marker
    in it -> SINCE-UNMARKED. Versions already released are history and need
    no marker. Same scope boundary as above: other wordings are not seen.

⚠️ The released set is main's CHANGELOG. A hotfix tagged off main (v2.9.1 was
a backport and has no heading here) is not in it, so a since marker naming it
reads as never released — write such statements against a main release.

Scanned: every `*.md` under `docs/` and `components/`, plus the root
`README*.md`. Skipped: `CHANGELOG*.md` (release notes name old images on
purpose, and are never edited to expire).

Exit codes (scripts/tools/_lib_exitcodes.py): 0 clean / 1 finding /
2 caller error (VERSION missing or unparseable, CHANGELOG.md missing or
without a single `## [vX.Y.Z]` heading, nothing to scan).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
VERSION_FILE = Path("components/da-tools/app/VERSION")
CHANGELOG_FILE = Path("CHANGELOG.md")
SCAN_DIRS = ("docs", "components")

_SEMVER = r"(\d+)\.(\d+)\.(\d+)"
# The marker as a whole; the version inside is validated separately so a
# typo is reported as MALFORMED rather than silently not being a marker.
MARKER_RE = re.compile(r"<!--\s*image-caveat:\s*(?P<body>.*?)-->")
_MARKER_VERSION_RE = re.compile(r"^v?" + _SEMVER + r"(?:\s|$)")
# The phrasings the existing caveats are written in. Deliberately narrow:
# measured on the tree this gate was introduced with, every line these match
# was a genuine caveat (51 of 51), so the check starts with no exceptions.
CAVEAT_PHRASE_RE = re.compile(
    r"v\d+\.\d+\.\d+ ?(?:映像|image\b)|你手上(?:這顆|的)映像|the image you have"
)
SINCE_MARKER_RE = re.compile(r"<!--\s*since:\s*(?P<body>.*?)-->")
_RELEASED_HEADING_RE = re.compile(r"(?m)^## \[v" + _SEMVER + r"\]")
# A version that is not the prefix of a longer dotted number (v2.10.0.1).
_VER = r"v(\d+)\.(\d+)\.(\d+)(?![\d.]*\d)"
# The phrasings "this ships in vX.Y.Z" statements are written in. Measured on
# the tree this was introduced with: every match about an unreleased version
# was such a statement (21 of 21). Bold markers may sit between the words.
SINCE_PHRASE_RE = re.compile(
    r"(?:[Ff]rom|[Ss]ince|[Bb]efore|[Aa]s of|(?:changed|retired|removed|introduced) in|自|從)"
    r"\s*\**\s*" + _VER
    + r"|" + _VER + r"\s*\**\s*(?:起|之前|前|以前|的契約|契約|contract|\+)"
)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# Text inside backticks is quoted, not said: a marker there is SHOWN to the
# reader (it is how the convention is documented), and a phrase there names
# the phrasing rather than making a claim about an image. Removed before
# either check matches.
_CODE_SPAN_RE = re.compile(r"(`+)(?:(?!\1).)+?\1")


def read_tools_version(root: Path) -> tuple[int, int, int] | None:
    """The da-tools VERSION file as a tuple, or None when unusable.

    Read strictly here rather than through `_lib_versions.read_da_tools_version`,
    whose fallback on a missing file is a hard-coded old version: for this
    gate that would turn "cannot tell" into "every marker is from the future".
    """
    path = root / VERSION_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    m = re.fullmatch(_SEMVER, text)
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def read_released_versions(root: Path) -> set[tuple[int, int, int]] | None:
    """The `## [vX.Y.Z]` headings of CHANGELOG.md, or None when there are none.

    None rather than an empty set: with nothing released every since marker
    would read as unreleased, and "cannot tell" must not look like a finding
    list (nor, under a bug that inverted the check, like a clean run).
    """
    try:
        text = (root / CHANGELOG_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    found = {tuple(int(g) for g in m.groups())
             for m in _RELEASED_HEADING_RE.finditer(text)}
    return found or None  # type: ignore[return-value]


def iter_markdown(root: Path) -> list[Path]:
    files: list[Path] = []
    for top in SCAN_DIRS:
        base = root / top
        if base.is_dir():
            files.extend(
                p for p in base.rglob("*.md")
                if not any(part.startswith(".") for part in p.relative_to(root).parts)
            )
    files.extend(p for p in root.glob("README*.md") if p.is_file())
    return sorted(
        p for p in files if not p.name.upper().startswith("CHANGELOG")
    )


def _blocks(lines: list[str]) -> list[list[int]]:
    """Group 0-based line indexes into the blocks the UNMARKED check reads."""
    blocks: list[list[int]] = []
    current: list[int] = []
    i = 0
    n = len(lines)

    def flush() -> None:
        nonlocal current
        if current:
            blocks.append(current)
            current = []

    while i < n:
        line = lines[i]
        if _FENCE_RE.match(line):
            # The fence plus the non-blank run directly above it (already in
            # `current`) and directly below its closing line.
            fence = _FENCE_RE.match(line).group(1)
            block = list(current)
            current = []
            block.append(i)
            i += 1
            while i < n:
                block.append(i)
                if lines[i].lstrip().startswith(fence):
                    i += 1
                    break
                i += 1
            while i < n and lines[i].strip():
                if _FENCE_RE.match(lines[i]):
                    break
                block.append(i)
                i += 1
            blocks.append(block)
            continue
        if not line.strip():
            flush()
        elif line.lstrip().startswith("|"):
            flush()
            blocks.append([i])
        else:
            current.append(i)
        i += 1
    flush()
    return blocks


def scan(root: Path, current: tuple[int, int, int]) -> list[dict]:
    findings: list[dict] = []
    for path in iter_markdown(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            findings.append({"file": path.relative_to(root).as_posix(),
                             "line": 0, "kind": "unreadable",
                             "text": str(exc)})
            continue
        rel = path.relative_to(root).as_posix()
        lines = text.splitlines()
        marked_lines: set[int] = set()
        for idx, line in enumerate(lines):
            for m in MARKER_RE.finditer(_CODE_SPAN_RE.sub("", line)):
                marked_lines.add(idx)
                body = m.group("body").strip()
                vm = _MARKER_VERSION_RE.match(body)
                if not vm:
                    findings.append({"file": rel, "line": idx + 1,
                                     "kind": "malformed", "text": m.group(0)})
                    continue
                ver = tuple(int(g) for g in vm.groups())
                if ver < current:
                    findings.append({"file": rel, "line": idx + 1,
                                     "kind": "stale", "text": line.strip()[:200]})
                elif ver > current:
                    findings.append({"file": rel, "line": idx + 1,
                                     "kind": "malformed", "text": m.group(0)})
        for block in _blocks(lines):
            if any(i in marked_lines for i in block):
                continue
            for i in block:
                if CAVEAT_PHRASE_RE.search(_CODE_SPAN_RE.sub("", lines[i])):
                    findings.append({"file": rel, "line": i + 1,
                                     "kind": "unmarked",
                                     "text": lines[i].strip()[:200]})
    return findings


def _phrase_version(m: re.Match) -> tuple[int, int, int]:
    g = m.groups()
    return tuple(int(x) for x in (g[:3] if g[0] is not None else g[3:]))  # type: ignore[return-value]


def scan_since(root: Path, released: set[tuple[int, int, int]],
               pre_tag: bool = False) -> list[dict]:
    """The since-marker checks (see the module docstring)."""
    newest = max(released)
    findings: list[dict] = []
    for path in iter_markdown(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # reported once, by scan()
        rel = path.relative_to(root).as_posix()
        lines = text.splitlines()
        marked_lines: set[int] = set()
        for idx, line in enumerate(lines):
            for m in SINCE_MARKER_RE.finditer(_CODE_SPAN_RE.sub("", line)):
                marked_lines.add(idx)
                vm = _MARKER_VERSION_RE.match(m.group("body").strip())
                if not vm:
                    findings.append({"file": rel, "line": idx + 1,
                                     "kind": "since-malformed", "text": m.group(0)})
                    continue
                ver = tuple(int(g) for g in vm.groups())
                if ver in released:
                    continue
                if ver < newest:
                    findings.append({"file": rel, "line": idx + 1,
                                     "kind": "since-never-released",
                                     "text": line.strip()[:200]})
                elif pre_tag:
                    findings.append({"file": rel, "line": idx + 1,
                                     "kind": "since-unreleased",
                                     "text": line.strip()[:200]})
        for block in _blocks(lines):
            if any(i in marked_lines for i in block):
                continue
            for i in block:
                stripped = _CODE_SPAN_RE.sub("", lines[i])
                if any(_phrase_version(m) > newest
                       for m in SINCE_PHRASE_RE.finditer(stripped)):
                    findings.append({"file": rel, "line": i + 1,
                                     "kind": "since-unmarked",
                                     "text": lines[i].strip()[:200]})
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "檢查「已發布的映像還是舊行為」這類註記：標記版號舊於 da-tools VERSION、"
            "標記格式錯誤、或用了這類句型卻沒有標記時失敗",
            "Check 'the published image still behaves the old way' caveats: fail "
            "on a marker older than the da-tools VERSION, a malformed marker, or "
            "that phrasing without a marker"))
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help=i18n_text(
                            "stdout 輸出機器可讀 JSON（人類訊息一律走 stderr）",
                            "emit machine-readable JSON on stdout (human messages go to stderr)"))
    parser.add_argument("--pre-tag", action="store_true", dest="pre_tag",
                        help=i18n_text(
                            "發版閘門模式（make pre-tag 會帶）：since 標記的版號不在 "
                            "CHANGELOG.md 的 ## [vX.Y.Z] 標題裡就失敗",
                            "release-gate mode (make pre-tag passes it): fail on a since "
                            "marker whose version is not a ## [vX.Y.Z] CHANGELOG.md heading"))
    args = parser.parse_args()

    current = read_tools_version(REPO_ROOT)
    if current is None:
        print(f"ERROR: cannot read a X.Y.Z version from {VERSION_FILE}",
              file=sys.stderr)
        return EXIT_CALLER_ERROR
    if not iter_markdown(REPO_ROOT):
        print(f"ERROR: no markdown found under {', '.join(SCAN_DIRS)} — "
              "nothing was measured", file=sys.stderr)
        return EXIT_CALLER_ERROR

    released = read_released_versions(REPO_ROOT)
    if released is None:
        print(f"ERROR: no ## [vX.Y.Z] heading readable in {CHANGELOG_FILE} — "
              "cannot tell which versions are released", file=sys.stderr)
        return EXIT_CALLER_ERROR

    findings = scan(REPO_ROOT, current) + scan_since(
        REPO_ROOT, released, pre_tag=args.pre_tag)
    cur = "v{}.{}.{}".format(*current)
    newest = "v{}.{}.{}".format(*max(released))
    if args.json_output:
        print(json.dumps({"tool": "check-image-caveats", "tools_version": cur,
                          "newest_released": newest, "pre_tag": args.pre_tag,
                          "findings": findings, "count": len(findings)},
                         ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print(f"{f['file']}:{f['line']}: [{f['kind']}] {f['text']}",
                  file=sys.stderr)
    if findings:
        print(i18n_text(
            f"\n{len(findings)} 筆 image-caveat／since 問題（da-tools VERSION = {cur}，"
            f"CHANGELOG 最新已發布 = {newest}）。\n"
            "  stale：那顆映像已不是最新，拿掉註記或改寫成歷史敘述，連同標記一起刪。\n"
            "  malformed：標記要寫成 <!-- image-caveat: vX.Y.Z -->，版號不得高於 VERSION。\n"
            "  unmarked：這段用了「vX.Y.Z 映像／你手上這顆映像」的句型，補上標記。\n"
            "  since-never-released：這個版號沒有發過（被跳過），把敘述改成實際發版的版號。\n"
            "  since-unreleased：要打的 tag 不是文件承諾的版號；先在 CHANGELOG 加這一版的標題，\n"
            "                    或把敘述改成實際版號。\n"
            "  since-malformed：標記要寫成 <!-- since: vX.Y.Z -->。\n"
            "  since-unmarked：這段寫了「vX.Y.Z 起／之前／的契約」卻沒有 since 標記，補上。",
            f"\n{len(findings)} image-caveat / since finding(s) (da-tools VERSION = {cur}, "
            f"newest CHANGELOG release = {newest}).\n"
            "  stale: that image is no longer the newest; remove the caveat or rewrite\n"
            "         it as history, and delete the marker with it.\n"
            "  malformed: write the marker as <!-- image-caveat: vX.Y.Z -->, no newer than VERSION.\n"
            "  unmarked: this block uses the 'vX.Y.Z image / the image you have' phrasing; add a marker.\n"
            "  since-never-released: that version was never released (skipped); name the real one.\n"
            "  since-unreleased: the release being tagged is not the one the docs promise; add its\n"
            "                    CHANGELOG heading first, or name the version actually cut.\n"
            "  since-malformed: write the marker as <!-- since: vX.Y.Z -->.\n"
            "  since-unmarked: this block says 'since / from / before vX.Y.Z' about an unreleased\n"
            "                  version with no since marker; add one."),
            file=sys.stderr)
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
