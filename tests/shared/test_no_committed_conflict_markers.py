"""沒有檔案帶著 git 的 merge 衝突標記進 repo。

#3909 合入 main 時 `docs/cli-reference.en.md` 有一處衝突沒被解掉，`<<<<<<< HEAD` /
`>>>>>>> origin/main` 連同兩邊的內容一起 commit、合併進 main：文件照樣 build、沒有任何
閘門出聲，直到另一張 PR 改到同一段才順手修掉。這支測試掃每個追蹤中的文字檔。

只認開頭與結尾兩種標記（`<<<<<<< <ref>`、`>>>>>>> <ref>`）：中間的 `=======` 是合法的
Markdown／reStructuredText 標題底線，不能拿來判。
"""
from __future__ import annotations

import re
from pathlib import Path

from _tree import REPO_ROOT, repo_files

#: A conflict hunk's first or last line: seven markers, a space, a ref name.
_MARKER = re.compile(r"^(?:<{7}|>{7}) \S")


def conflict_marker_lines(text: str) -> list[int]:
    """1-based numbers of the lines of *text* that open or close a conflict hunk."""
    return [n for n, line in enumerate(text.splitlines(), 1) if _MARKER.match(line)]


def test_the_scan_recognises_a_conflict_hunk_and_nothing_else():
    hunk = "kept\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> origin/main\nkept\n"
    assert conflict_marker_lines(hunk) == [2, 6]
    # A heading underline, a quoted diff, shell redirection and a longer run are not markers.
    harmless = "Title\n=======\n> >>>>>>> quoted\ncat <<EOF\n<<<<<<<<< nine\n<<<<<<<\n"
    assert conflict_marker_lines(harmless) == []


def test_no_tracked_file_carries_a_conflict_marker():
    this_file = Path(__file__).resolve()
    scanned, found = 0, []
    for path in repo_files():
        if path.resolve() == this_file:
            continue  # its own examples
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary, or gone since the listing
        scanned += 1
        found += [f"{path.relative_to(REPO_ROOT).as_posix()}:{n}"
                  for n in conflict_marker_lines(text)]
    assert scanned > 1000, f"only {scanned} text files scanned — the listing looks broken"
    assert not found, (
        "merge conflict markers were committed — resolve the hunk (both sides are "
        "in the file) and remove the markers: " + ", ".join(found))
