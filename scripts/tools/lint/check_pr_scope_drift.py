#!/usr/bin/env python3
"""PR scope drift 偵測（pr-preflight 級）。

設計動機——PR #39/#40 踩坑：merge 後才發現 CLAUDE.md 計數沒 bump、
有未 commit 的 playbook LL 更新散在工作目錄，被迫另開 mini-PR 處理
瑣碎 drift。單次 gh pr create + CI rerun + review round-trip 的成本
遠高於「把相關 drift 塞進同一 commit」的邊界風險。

本 hook 在 `make pr-preflight` 時強制執行，硬性 fail 以下任一：

1. **Tool map drift** — `generate_tool_map.py --check` 不通過。典型肇因：
   新增 / 移除 `scripts/tools/**/*.py` 但 `docs/internal/tool-map.md`
   未重新產生（並連帶 CLAUDE.md「N 個 Python 工具」計數漂移）。
2. **Working-tree dirty** — 準備 merge 的 branch 工作目錄含 unstaged
   或 uncommitted staged 修改。典型肇因：session 中邊做邊改 playbook /
   CLAUDE.md 但忘記 git add 進本次 commit。

Exit:
  0 = 通過
  1 = 偵測到 drift

說明：本 hook **刻意保守**，只報告高信號項，避免 false-positive 被繞過。
新增 drift 項目時先把訊號量測明確再加入。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def find_repo_root() -> Path:
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return Path(__file__).resolve().parent.parent.parent.parent


def run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run a subprocess, return (exit, stdout, stderr)."""
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout, proc.stderr


def check_tool_map(repo: Path) -> tuple[bool, str]:
    """Return (ok, message).

    generate_tool_map.py --check prints a clear "outdated" line and is tightly
    scoped to scripts/tools/**/*.py vs docs/internal/tool-map.md consistency.
    Caveat: exits 0 even on drift (known issue with that script). We parse
    stdout for the failure sentinel instead of relying on exit code.
    """
    rc, stdout, stderr = run(
        ["python3", "scripts/tools/dx/generate_tool_map.py", "--check"], repo
    )
    combined = (stdout + stderr).strip()
    if rc == 0 and "outdated" not in combined.lower():
        return True, "tool-map --check: PASS"
    last = combined.splitlines()[-1] if combined else "(no output)"
    return False, f"tool-map drift: {last}"


def check_working_tree_clean(repo: Path) -> tuple[bool, str]:
    """Both `git diff --quiet` (unstaged) and `git diff --cached --quiet` (staged)
    must return 0. Untracked files are NOT checked here — they're handled by the
    author's own discretion (adopt or delete).
    """
    rc_unstaged, _, _ = run(["git", "diff", "--quiet"], repo)
    rc_staged, _, _ = run(["git", "diff", "--cached", "--quiet"], repo)
    if rc_unstaged == 0 and rc_staged == 0:
        return True, "working-tree clean (no unstaged / uncommitted staged)"

    rc, stdout, _ = run(["git", "status", "--short"], repo)
    lines = [ln for ln in stdout.splitlines() if ln and not ln.startswith("??")]
    preview = "\n".join("    " + ln for ln in lines[:15])
    return False, (
        "working-tree has uncommitted changes (should be in this PR or reverted):\n"
        + preview
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail on PR-level drift (tool-map outdated or working-tree dirty)."
    )
    parser.parse_args()  # no flags — just enforce unknown-arg rejection
    repo = find_repo_root()
    print("[check_pr_scope_drift] scanning for PR-level drift signals...\n")

    checks = [
        ("tool-map", check_tool_map(repo)),
        ("working-tree", check_working_tree_clean(repo)),
    ]

    failed = [name for name, (ok, _) in checks if not ok]
    for name, (ok, msg) in checks:
        prefix = "  PASS" if ok else "  FAIL"
        print(f"{prefix}  [{name}] {msg}")

    print()
    if failed:
        print(
            "[check_pr_scope_drift] FAIL: "
            + ", ".join(failed)
            + "\n\n  Either fold the drift into this PR's commit, or revert it.\n"
              "  Do NOT open a follow-up mini-PR unless the drift is genuinely\n"
              "  out of scope (use your judgement, then justify in PR body).",
            file=sys.stderr,
        )
        return 1
    print("[check_pr_scope_drift] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
