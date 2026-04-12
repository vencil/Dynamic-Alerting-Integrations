#!/usr/bin/env python3
"""reword_chain.py — 批次改寫 commit chain 的 subject line（preserve tree + author/committer date）

適用場景：commitlint / scope-enum / 樣式檢查事後才發現 N 個 commit 的 message
不合規，需要在 force-push 前批次改寫。典型觸發點是打完 PR 後 CI 才跑
commitlint，而本地 commit-msg hook 沒檔到。

相比 `git rebase -i`：
- 不需要 interactive editor（適合 agent/CI 環境）
- 不會觸發任何 pre-commit / pre-push / post-rewrite hook（純 plumbing）
- 不會跑 diff / merge（tree SHA 維持一模一樣）
- 保留 author/committer 的 name / email / date（rebase 預設會把 committer 改成當前身分與時間）
- 失敗時透過 backup tag 一鍵復原

與 `git filter-repo --message-callback`：
- 不需要額外安裝（純 built-in git plumbing）
- 只針對一段 chain，不會改寫整個 repo 歷史

設計原則：
- 純讀寫 refs，不碰 working tree（呼叫前請自行 stash 未追蹤變更）
- Fail-fast：任何一步出錯都 abort，backup tag 立即可用
- Dry-run 模式輸出完整計畫與 before/after subject 對照

用法：
    # 基本用法：從 mapping file 批次改寫當前 branch
    python3 scripts/tools/dx/reword_chain.py mapping.tsv

    # Dry-run 預覽，不寫入
    python3 scripts/tools/dx/reword_chain.py mapping.tsv --dry-run

    # 指定 base commit（改寫鏈的共同祖先，預設 = 第一個 entry 的 parent）
    python3 scripts/tools/dx/reword_chain.py mapping.tsv --base 80babb1

    # 自訂 backup tag 名稱（預設 = reword-backup-YYYYMMDD-HHMMSS）
    python3 scripts/tools/dx/reword_chain.py mapping.tsv --backup-tag my-backup

    # 復原：
    git reset --hard <backup-tag>

Mapping file 格式（TSV，每行一個 entry，# 開頭為註解）：

    # <old_sha>\\t<new_subject_or_dash>
    45ec99a\\trefactor(tools): rename scripts/ops/ to scripts/session-guards/ (P2a)
    cd357a3\\t-
    caf2049\\trefactor: group shell scenarios under tests/scenarios + fixtures (P2c)
    8a68762\\t-
    783fd7f\\tdocs: complete mod-repair-C truncated sentence and add route D

- 第一欄：原 commit 的 short/full SHA（短 SHA 必須唯一，會用 `git rev-parse` 解析）
- 第二欄：新 subject line。如果是 `-` 代表 subject 不變，但因 parent 改變仍會產生新 SHA
- 順序：從最舊到最新（list[0] 最接近 base，list[-1] 是新 HEAD）
- Body 會從原 commit 保留（`git log -1 --format='%b'`）
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Git helper
# ---------------------------------------------------------------------------


class GitError(RuntimeError):
    """Raised when a git subprocess exits non-zero."""


def _run(
    cmd: list[str],
    *,
    capture: bool = True,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> str:
    """Run a git command; return stripped stdout or raise GitError.

    Using bytes input avoids shell quoting issues with multi-line messages.
    """
    completed = subprocess.run(
        cmd,
        input=input_bytes,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
        env={**os.environ, **(env or {})},
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"`{' '.join(cmd)}` exited {completed.returncode}: {stderr}")
    if capture:
        return completed.stdout.decode("utf-8", errors="replace").rstrip("\n")
    return ""


def _git(*args: str) -> str:
    return _run(["git", *args])


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class CommitMeta:
    """Snapshot of a single commit's metadata, captured before rewrite."""

    old_sha: str
    tree: str
    old_subject: str
    body: str  # everything after the subject line + blank separator
    author_name: str
    author_email: str
    author_date: str
    committer_name: str
    committer_email: str
    committer_date: str

    def env(self) -> dict[str, str]:
        """Environment variables that make `git commit-tree` preserve identity."""
        return {
            "GIT_AUTHOR_NAME": self.author_name,
            "GIT_AUTHOR_EMAIL": self.author_email,
            "GIT_AUTHOR_DATE": self.author_date,
            "GIT_COMMITTER_NAME": self.committer_name,
            "GIT_COMMITTER_EMAIL": self.committer_email,
            "GIT_COMMITTER_DATE": self.committer_date,
        }


@dataclass
class Entry:
    """One line from the mapping file."""

    old_ref: str  # as written by user (short SHA or full)
    new_subject: str | None  # None → keep original subject


# ---------------------------------------------------------------------------
# Mapping file parser
# ---------------------------------------------------------------------------


def parse_mapping(path: Path) -> list[Entry]:
    """Parse TSV mapping; validate non-empty."""
    if not path.is_file():
        raise SystemExit(f"mapping file not found: {path}")

    entries: list[Entry] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Support both TAB and >=2 spaces as delimiter for user convenience
        if "\t" in line:
            old_ref, _, rest = line.partition("\t")
        else:
            parts = line.split(None, 1)
            if len(parts) != 2:
                raise SystemExit(f"{path}:{lineno}: cannot split into <sha> <subject>")
            old_ref, rest = parts
        rest = rest.strip()
        new_subject: str | None
        if rest == "-":
            new_subject = None
        else:
            new_subject = rest
        entries.append(Entry(old_ref=old_ref.strip(), new_subject=new_subject))

    if not entries:
        raise SystemExit(f"mapping file {path} is empty")
    return entries


# ---------------------------------------------------------------------------
# Commit metadata capture
# ---------------------------------------------------------------------------


_FORMAT_SEP = "\x1f"  # ASCII unit separator — unlikely to appear in commit fields
_FORMAT = _FORMAT_SEP.join(
    [
        "%H",  # full SHA
        "%T",  # tree
        "%s",  # subject
        "%an",
        "%ae",
        "%aI",  # author date ISO 8601 strict
        "%cn",
        "%ce",
        "%cI",
        "%b",  # body (may contain newlines; MUST be last field)
    ]
)


def capture_meta(old_ref: str) -> CommitMeta:
    """Resolve old_ref → CommitMeta. Raises GitError if not found."""
    raw = _git("log", "-1", f"--format={_FORMAT}", old_ref)
    parts = raw.split(_FORMAT_SEP)
    if len(parts) != 10:
        raise GitError(
            f"unexpected git log output for {old_ref}: got {len(parts)} fields, expected 10"
        )
    (
        sha,
        tree,
        subject,
        a_name,
        a_email,
        a_date,
        c_name,
        c_email,
        c_date,
        body,
    ) = parts
    return CommitMeta(
        old_sha=sha,
        tree=tree,
        old_subject=subject,
        body=body,
        author_name=a_name,
        author_email=a_email,
        author_date=a_date,
        committer_name=c_name,
        committer_email=c_email,
        committer_date=c_date,
    )


# ---------------------------------------------------------------------------
# Chain validation
# ---------------------------------------------------------------------------


def validate_chain(metas: list[CommitMeta], base_sha: str) -> None:
    """Ensure metas[0] parent == base_sha, and metas[i+1] parent == metas[i] sha.

    Detects out-of-order entries or missing commits in the chain.
    """
    for i, meta in enumerate(metas):
        expected_parent = base_sha if i == 0 else metas[i - 1].old_sha
        actual = _git("rev-parse", f"{meta.old_sha}^")
        if actual != expected_parent:
            raise SystemExit(
                f"chain broken at entry {i} ({meta.old_sha[:7]}):\n"
                f"  expected parent: {expected_parent[:7]}\n"
                f"  actual parent:   {actual[:7]}\n"
                f"check mapping file order — entries must go oldest → newest, "
                f"linear --first-parent chain from base"
            )


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------


def compose_message(meta: CommitMeta, new_subject: str | None) -> bytes:
    subject = new_subject if new_subject is not None else meta.old_subject
    body = meta.body
    if body:
        msg = f"{subject}\n\n{body}"
    else:
        msg = f"{subject}\n"
    # Always end with LF
    if not msg.endswith("\n"):
        msg += "\n"
    return msg.encode("utf-8")


def commit_tree(meta: CommitMeta, parent_sha: str, new_subject: str | None) -> str:
    """Create a new commit object with preserved tree + identity; return new SHA."""
    msg_bytes = compose_message(meta, new_subject)
    new_sha = _run(
        ["git", "commit-tree", meta.tree, "-p", parent_sha, "-F", "-"],
        input_bytes=msg_bytes,
        env=meta.env(),
    )
    return new_sha.strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def working_tree_is_dirty() -> bool:
    status = _git("status", "--porcelain")
    # Ignore untracked-only (?? prefix); flag modifications and staged changes
    for line in status.splitlines():
        if line and not line.startswith("??"):
            return True
    return False


def default_backup_tag() -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"reword-backup-{stamp}"


def format_sha_short(sha: str) -> str:
    return sha[:7]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Batch-reword commit subject lines via git commit-tree "
        "(preserves tree + author/committer identity).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "mapping",
        type=Path,
        help="TSV mapping file: '<old_sha>\\t<new_subject_or_->' per line",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="base commit SHA (unchanged parent before first entry). "
        "Defaults to the parent of the first entry.",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="branch to update after rewrite (default: current branch). "
        "Pass empty string to skip ref update (only print new HEAD).",
    )
    parser.add_argument(
        "--backup-tag",
        default=None,
        help=f"backup tag name (default: reword-backup-YYYYMMDD-HHMMSS)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print plan without writing refs",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="proceed even if working tree has modifications (untracked files always ignored)",
    )
    args = parser.parse_args(argv)

    # 1. Parse mapping
    entries = parse_mapping(args.mapping)

    # 2. Capture metadata for each entry (fails early on bad SHA)
    metas: list[CommitMeta] = []
    for entry in entries:
        try:
            meta = capture_meta(entry.old_ref)
        except GitError as exc:
            raise SystemExit(f"cannot resolve {entry.old_ref}: {exc}")
        metas.append(meta)

    # 3. Resolve base
    if args.base:
        base_sha = _git("rev-parse", args.base)
    else:
        base_sha = _git("rev-parse", f"{metas[0].old_sha}^")

    # 4. Validate chain linearity
    validate_chain(metas, base_sha)

    # 5. Determine target branch
    if args.branch is None:
        current = _git("rev-parse", "--abbrev-ref", "HEAD")
        if current == "HEAD":
            raise SystemExit(
                "detached HEAD: pass --branch <name> or --branch '' to skip ref update"
            )
        target_branch = current
    else:
        target_branch = args.branch  # may be "" to skip

    # 6. Dirty check
    if not args.allow_dirty and working_tree_is_dirty():
        raise SystemExit(
            "working tree has modifications. Stash them first or pass --allow-dirty.\n"
            "(untracked files are always ignored)"
        )

    # 7. Dry-run print
    new_subjects = [
        (entry.new_subject if entry.new_subject is not None else metas[i].old_subject)
        for i, entry in enumerate(entries)
    ]

    print(f"=== reword_chain plan ===")
    print(f"  base      : {format_sha_short(base_sha)}")
    print(f"  branch    : {target_branch or '(skip ref update)'}")
    print(f"  entries   : {len(entries)}")
    print()
    for i, meta in enumerate(metas):
        marker = "  " if entries[i].new_subject is None else "→ "
        print(f"  [{i}] {format_sha_short(meta.old_sha)} {marker}{new_subjects[i]}")
        if entries[i].new_subject is not None:
            print(f"        was: {meta.old_subject}")
    print()

    if args.dry_run:
        print("(dry-run: no refs written)")
        return 0

    # 8. Backup tag
    backup_tag = args.backup_tag or default_backup_tag()
    head_sha = _git("rev-parse", "HEAD")
    _git("tag", "-f", backup_tag, head_sha)
    print(f"backup tag: {backup_tag} -> {format_sha_short(head_sha)}")

    # 9. Write commit objects
    parent = base_sha
    new_shas: list[str] = []
    for i, meta in enumerate(metas):
        try:
            new_sha = commit_tree(meta, parent, entries[i].new_subject)
        except GitError as exc:
            print(
                f"\nFAILED at entry {i} ({format_sha_short(meta.old_sha)}): {exc}\n"
                f"backup tag {backup_tag} still points at original HEAD; "
                f"run `git reset --hard {backup_tag}` to recover.",
                file=sys.stderr,
            )
            return 1
        new_shas.append(new_sha)
        print(
            f"  [{i}] {format_sha_short(meta.old_sha)} -> "
            f"{format_sha_short(new_sha)}  {new_subjects[i]}"
        )
        parent = new_sha

    final_sha = new_shas[-1]

    # 10. Update branch ref
    if target_branch:
        _git("update-ref", f"refs/heads/{target_branch}", final_sha)
        # Only reset if the target branch is currently checked out
        current_branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if current_branch == target_branch:
            _git("reset", "--hard", final_sha)
            print(f"\nbranch '{target_branch}' reset to {format_sha_short(final_sha)}")
        else:
            print(
                f"\nbranch '{target_branch}' ref updated to {format_sha_short(final_sha)} "
                f"(not checked out; no reset needed)"
            )
    else:
        print(f"\nnew chain HEAD: {final_sha}")
        print("(--branch '' passed: ref not updated)")

    print(f"\nrecover with: git reset --hard {backup_tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
