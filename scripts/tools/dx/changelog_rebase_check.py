#!/usr/bin/env python3
"""changelog_rebase_check.py — did a rebase keep exactly the right CHANGELOG lines?

A rebase can drop or duplicate a CHANGELOG bullet without a single conflict
marker, and the bullet *count* often stays the same, so nothing looks wrong.
This tool compares line multisets against the oracle

    expected = (main − what I removed since base) + what I added since base

where ``base`` is the fork point before the rebase, ``mine`` is the branch tip
before the rebase, and ``main`` is the upstream the branch was rebased onto.
``mine ∪ main`` is the wrong oracle: it reports lines that upstream rewrote as
missing. A union-style conflict resolution fails the other way — it keeps the
old and the rewritten text of one bullet — so extras are reported too.

It is a line-level oracle. When both sides edited the same bullet, both
versions count as expected; read the diff for those by hand.

Usage:
  # right after `git rebase origin/main` (ORIG_HEAD = the pre-rebase tip)
  python3 scripts/tools/dx/changelog_rebase_check.py
  # other commands since the rebase overwrote ORIG_HEAD → name the old tip
  python3 scripts/tools/dx/changelog_rebase_check.py --mine 'my-branch@{1}'
  python3 scripts/tools/dx/changelog_rebase_check.py --after HEAD

Exit codes: 0 = matches the oracle, 1 = lines missing or extra,
2 = caller error (unknown ref, not a repository, file absent at a ref).
"""
import argparse
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

SHOW_LIMIT = 20


class CallerError(Exception):
    """A ref or path the caller named cannot be resolved."""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    if proc.returncode != 0:
        raise CallerError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def _lines(text: str) -> Counter:
    return Counter(ln for ln in text.splitlines() if ln.strip())


def _at_ref(repo: Path, ref: str, path: str) -> Counter:
    return _lines(_git(repo, "show", f"{ref}:{path}"))


def _sha(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def check(repo: Path, path: str, mine: str, main: str,
          base: str | None, after: str | None) -> dict:
    """Compare the post-rebase file against the oracle. Raises CallerError."""
    mine_sha = _sha(repo, mine)
    main_sha = _sha(repo, main)
    base_sha = _sha(repo, base) if base else _git(
        repo, "merge-base", mine_sha, main_sha).strip()

    base_c = _at_ref(repo, base_sha, path)
    mine_c = _at_ref(repo, mine_sha, path)
    main_c = _at_ref(repo, main_sha, path)
    if after is None:
        file_path = repo / path
        if not file_path.is_file():
            raise CallerError(f"{file_path} does not exist in the working tree")
        after_c = _lines(file_path.read_text(encoding="utf-8"))
        after_label = "working tree"
    else:
        after_sha = _sha(repo, after)
        after_c = _at_ref(repo, after_sha, path)
        after_label = after_sha[:12]

    added = mine_c - base_c
    removed = base_c - mine_c
    expected = (main_c - removed) + added
    missing = expected - after_c
    extra = after_c - expected
    return {
        "file": path,
        "refs": {"base": base_sha[:12], "mine": mine_sha[:12],
                 "main": main_sha[:12], "after": after_label},
        "mine_added": sum(added.values()),
        "mine_removed": sum(removed.values()),
        "main_added_since_base": sum((main_c - base_c).values()),
        "main_removed_since_base": sum((base_c - main_c).values()),
        "missing": sorted(missing.elements()),
        "extra": sorted(extra.elements()),
        "ok": not missing and not extra,
    }


def _render(r: dict) -> str:
    refs = r["refs"]
    out = [
        f"{r['file']}: base={refs['base']} mine={refs['mine']} "
        f"main={refs['main']} after={refs['after']}",
        f"mine since base: +{r['mine_added']} / -{r['mine_removed']} lines; "
        f"main since base: +{r['main_added_since_base']} / "
        f"-{r['main_removed_since_base']} lines",
    ]
    for key, sign, title in (("missing", "-", "MISSING (expected, absent)"),
                             ("extra", "+", "EXTRA (present, not expected)")):
        lines = r[key]
        out.append(f"{title}: {len(lines)}")
        out.extend(f"  {sign} {ln[:160]}" for ln in lines[:SHOW_LIMIT])
        if len(lines) > SHOW_LIMIT:
            out.append(f"  … {len(lines) - SHOW_LIMIT} more")
    out.append("OK — matches main − (base − mine) + (mine − base)" if r["ok"]
               else "MISMATCH — fix the file, then re-run")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=i18n_text(
            "rebase 後驗 CHANGELOG 的行多重集合是否恰為「main 扣掉我刪的、加上我新增的」："
            "抓出被靜默吃掉或重複的條目。",
            "After a rebase, check that CHANGELOG lines (as a multiset) equal "
            "main minus what I deleted plus what I added: catches bullets "
            "silently dropped or duplicated."),
    )
    p.add_argument("--repo", default=".", help=i18n_text(
        "repo 或 worktree 路徑（預設目前目錄）",
        "repository or worktree path (default: current directory)"))
    p.add_argument("--file", default="CHANGELOG.md", help=i18n_text(
        "要驗的檔，相對 repo 根目錄（預設 CHANGELOG.md）",
        "file to check, relative to the repo root (default: CHANGELOG.md)"))
    p.add_argument("--mine", default="ORIG_HEAD", help=i18n_text(
        "rebase 前的分支 tip（預設 ORIG_HEAD；之後又跑過 reset/merge 就改用 "
        "'<branch>@{1}'）",
        "branch tip before the rebase (default: ORIG_HEAD; after a later "
        "reset/merge use '<branch>@{1}')"))
    p.add_argument("--main", default="origin/main", help=i18n_text(
        "rebase 的目標（預設 origin/main）",
        "the upstream rebased onto (default: origin/main)"))
    p.add_argument("--base", default=None, help=i18n_text(
        "rebase 前的分岔點（預設 merge-base(mine, main)）",
        "fork point before the rebase (default: merge-base(mine, main))"))
    p.add_argument("--after", default=None, help=i18n_text(
        "要驗的結果 ref（預設讀 working tree，解衝突途中也能用）",
        "ref holding the result (default: the working tree, so it also "
        "works mid-conflict)"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        result = check(repo, args.file.replace("\\", "/"), args.mine,
                       args.main, args.base, args.after)
    except CallerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    except subprocess.TimeoutExpired as exc:
        print(f"error: git timed out: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    print(_render(result))
    return EXIT_OK if result["ok"] else EXIT_VIOLATION


if __name__ == "__main__":
    sys.exit(main())
