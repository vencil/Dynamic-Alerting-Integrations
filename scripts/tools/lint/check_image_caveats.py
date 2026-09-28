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

Scanned: every `*.md` under `docs/` and `components/`, plus the root
`README*.md`. Skipped: `CHANGELOG*.md` (release notes name old images on
purpose, and are never edited to expire).

Exit codes (scripts/tools/_lib_exitcodes.py): 0 clean / 1 finding /
2 caller error (VERSION missing or unparseable, nothing to scan).
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
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


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
            for m in MARKER_RE.finditer(line):
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
                if CAVEAT_PHRASE_RE.search(lines[i]):
                    findings.append({"file": rel, "line": i + 1,
                                     "kind": "unmarked",
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

    findings = scan(REPO_ROOT, current)
    cur = "v{}.{}.{}".format(*current)
    if args.json_output:
        print(json.dumps({"tool": "check-image-caveats", "tools_version": cur,
                          "findings": findings, "count": len(findings)},
                         ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print(f"{f['file']}:{f['line']}: [{f['kind']}] {f['text']}",
                  file=sys.stderr)
    if findings:
        print(i18n_text(
            f"\n{len(findings)} 筆 image-caveat 問題（da-tools VERSION = {cur}）。\n"
            "  stale：那顆映像已不是最新，拿掉註記或改寫成歷史敘述，連同標記一起刪。\n"
            "  malformed：標記要寫成 <!-- image-caveat: vX.Y.Z -->，版號不得高於 VERSION。\n"
            "  unmarked：這段用了「vX.Y.Z 映像／你手上這顆映像」的句型，補上標記。",
            f"\n{len(findings)} image-caveat finding(s) (da-tools VERSION = {cur}).\n"
            "  stale: that image is no longer the newest; remove the caveat or rewrite\n"
            "         it as history, and delete the marker with it.\n"
            "  malformed: write the marker as <!-- image-caveat: vX.Y.Z -->, no newer than VERSION.\n"
            "  unmarked: this block uses the 'vX.Y.Z image / the image you have' phrasing; add a marker."),
            file=sys.stderr)
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
