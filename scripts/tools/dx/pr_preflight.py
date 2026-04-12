#!/usr/bin/env python3
"""PR Preflight Check — branch 收尾前的自動化檢查。

在 merge PR 前執行，確保 branch 處於可合併狀態。
純檢查 + 報告，不自動修改任何東西。

檢查項目：
  1. Branch 身份：是否在 feature branch（非 main/master）
  2. 同步狀態：behind main 幾個 commit（>0 = 可能有 conflict）
  3. Conflict 偵測：dry-run merge 看有無衝突
  4. Local hooks：pre-commit run --all-files（可選）
  5. CI 狀態：透過 gh pr checks 查詢（需 gh CLI）
  6. PR mergeable：透過 gh pr view 查詢

用法：
  python scripts/tools/dx/pr_preflight.py                    # 完整檢查
  python scripts/tools/dx/pr_preflight.py --skip-hooks       # 跳過 local hooks
  python scripts/tools/dx/pr_preflight.py --ci               # CI 模式（exit 1 on failure）
  python scripts/tools/dx/pr_preflight.py --pr 23            # 指定 PR 號碼

設計原則：
  - 純 diagnostic，不改檔案、不 merge、不 push
  - 每項檢查獨立：一項失敗不影響其他項執行
  - 結果用 ✅ / ⚠️ / ❌ 分類，最後給出 go/no-go 總結
"""

import argparse
import io
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

# Windows console 預設 cp950/cp936 無法印 emoji — 強制 UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("cp"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


class Status(Enum):
    PASS = "✅"
    WARN = "⚠️"
    FAIL = "❌"
    SKIP = "⏭️"


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    detail: str = ""


@dataclass
class PreflightReport:
    results: List[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    @property
    def has_failure(self) -> bool:
        return any(r.status == Status.FAIL for r in self.results)

    @property
    def has_warning(self) -> bool:
        return any(r.status == Status.WARN for r in self.results)

    def print_summary(self) -> None:
        width = 60
        print()
        print("=" * width)
        print("  PR Preflight Report")
        print("=" * width)
        for r in self.results:
            line = f"  {r.status.value} {r.name}: {r.message}"
            print(line)
            if r.detail:
                for dl in r.detail.strip().split("\n"):
                    print(f"     {dl}")
        print("-" * width)
        if self.has_failure:
            print("  ❌ BLOCKED — 有必須修復的問題")
        elif self.has_warning:
            print("  ⚠️  CAUTION — 可合併但建議先處理警告")
        else:
            print("  ✅ READY — 所有檢查通過，可以 merge")
        print("=" * width)
        print()


def run(cmd: List[str], capture: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a command with sensible defaults."""
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        # Command not found — return a synthetic failure
        return subprocess.CompletedProcess(
            cmd, returncode=127, stdout="", stderr=f"command not found: {cmd[0]}"
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, returncode=124, stdout="", stderr=f"timeout after {timeout}s"
        )


def find_repo_root() -> Path:
    """從 cwd 向上找 .git 目錄。"""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    # fallback
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent.parent


# ─── Check Functions ─────────────────────────────────────


def check_branch_identity() -> CheckResult:
    """確認不在 main/master 上。"""
    r = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    branch = r.stdout.strip() if r.returncode == 0 else "unknown"
    if branch in ("main", "master"):
        return CheckResult(
            "Branch",
            Status.FAIL,
            f"目前在 {branch}（應在 feature branch）",
        )
    if branch == "HEAD":
        return CheckResult(
            "Branch",
            Status.WARN,
            "Detached HEAD — 無法判斷 branch 名稱",
        )
    return CheckResult("Branch", Status.PASS, branch)


def check_behind_main() -> CheckResult:
    """檢查 feature branch 落後 main 幾個 commit。"""
    # Fetch latest main (best-effort)
    run(["git", "fetch", "origin", "main"], timeout=30)

    r = run(["git", "rev-list", "--count", "HEAD..origin/main"])
    if r.returncode != 0:
        return CheckResult(
            "Behind main",
            Status.WARN,
            "無法計算（origin/main 不存在？）",
        )
    behind = int(r.stdout.strip())
    if behind == 0:
        return CheckResult("Behind main", Status.PASS, "已同步（0 commits behind）")
    if behind <= 5:
        return CheckResult(
            "Behind main",
            Status.WARN,
            f"落後 {behind} commits — 建議 merge main 再推",
        )
    return CheckResult(
        "Behind main",
        Status.WARN,
        f"落後 {behind} commits — 強烈建議先 merge main",
    )


def check_conflict() -> CheckResult:
    """Dry-run merge 偵測衝突（不改工作區）。

    策略優先級：
    1. behind == 0 → 已同步，不需要 merge
    2. git merge-tree --write-tree（git >= 2.38，不動工作區）
    3. git merge --no-commit fallback（會碰工作區，結束後 abort）
    4. FUSE lock 導致 dry-run 失敗 → 降級為 WARN
    """
    # Fast path: if behind == 0, no merge needed
    r = run(["git", "rev-list", "--count", "HEAD..origin/main"])
    if r.returncode == 0 and r.stdout.strip() == "0":
        return CheckResult("Conflict", Status.PASS, "已同步 origin/main — 無衝突風險")

    # Try merge-tree (git >= 2.38, doesn't touch working tree)
    r = run(["git", "merge-tree", "--write-tree", "HEAD", "origin/main"])
    if r.returncode == 0:
        return CheckResult("Conflict", Status.PASS, "無衝突（merge-tree 驗證）")

    # merge-tree might have reported conflicts (git >= 2.38)
    combined = (r.stdout or "") + (r.stderr or "")
    if "CONFLICT" in combined:
        conflicts = re.findall(r"CONFLICT.*?:\s*(.+)", combined)
        detail = "\n".join(f"· {c}" for c in conflicts[:10])
        return CheckResult(
            "Conflict",
            Status.FAIL,
            f"{len(conflicts)} 個檔案衝突 — 必須先 merge main 並解衝突",
            detail=detail,
        )

    # merge-tree not available (old git) — use merge --no-commit fallback
    # Check for FUSE lock issues first
    lock_path = Path(".git/ORIG_HEAD.lock")
    if lock_path.exists():
        return CheckResult(
            "Conflict",
            Status.WARN,
            "無法 dry-run merge（FUSE lock 殘留）— 請先 make git-preflight",
            detail="建議在 Windows 側執行 merge 驗證",
        )

    r2 = run(["git", "merge", "--no-commit", "--no-ff", "origin/main"])
    # Always abort regardless of result
    run(["git", "merge", "--abort"])

    if r2.returncode == 0:
        return CheckResult("Conflict", Status.PASS, "無衝突（merge dry-run 驗證）")

    combined2 = (r2.stdout or "") + (r2.stderr or "")
    conflict_files = re.findall(r"CONFLICT.*?:\s*Merge conflict in (.+)", combined2)
    if conflict_files:
        detail = "\n".join(f"· {f}" for f in conflict_files)
        return CheckResult(
            "Conflict",
            Status.FAIL,
            f"{len(conflict_files)} 個檔案衝突",
            detail=detail,
        )

    # Merge failed for non-conflict reasons (FUSE, permission, etc.)
    if "unable to unlink" in combined2 or "lock" in combined2.lower():
        return CheckResult(
            "Conflict",
            Status.WARN,
            "無法 dry-run merge（FUSE/lock 問題）— 建議在 Windows 側驗證",
            detail=combined2[:200],
        )
    return CheckResult(
        "Conflict",
        Status.WARN,
        "Merge dry-run 失敗但無法解析原因",
        detail=combined2[:200],
    )


def check_local_hooks() -> CheckResult:
    """跑 pre-commit run --all-files。"""
    r = run(["pre-commit", "run", "--all-files"], timeout=300)
    if r.returncode == 0:
        return CheckResult("Local hooks", Status.PASS, "pre-commit 全部通過")

    # Count failures
    failed = re.findall(r"^(.+?)\.+Failed$", r.stdout or "", re.MULTILINE)
    passed = re.findall(r"^(.+?)\.+Passed$", r.stdout or "", re.MULTILINE)
    if failed:
        detail = "\n".join(f"· {f.strip()}" for f in failed[:10])
        return CheckResult(
            "Local hooks",
            Status.FAIL,
            f"{len(failed)} hook(s) 失敗 / {len(passed)} 通過",
            detail=detail,
        )
    return CheckResult(
        "Local hooks",
        Status.FAIL,
        "pre-commit 執行失敗",
        detail=(r.stderr or r.stdout or "")[:300],
    )


def _classify_ci_failures(failed_checks: list) -> str:
    """A/B 分類：比對 main 最近一次 CI run，判斷失敗是 pre-existing 還是 this-PR 引入。

    類似 pre-push drift Layer 1 的 A/B 驗證邏輯，但套用在 CI checks 上。
    """
    import json as _json

    # 查 main 最近一次 workflow run 的結論
    r = run(
        ["gh", "run", "list", "--branch", "main", "--limit", "1",
         "--json", "conclusion,headBranch,databaseId"],
        timeout=15,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return ""  # gh 不可用，跳過分類

    try:
        runs = _json.loads(r.stdout)
    except _json.JSONDecodeError:
        return ""

    if not runs:
        return ""

    main_run = runs[0]
    main_conclusion = main_run.get("conclusion", "")
    run_id = main_run.get("databaseId", "")

    if main_conclusion == "success":
        return "→ main CI 目前是 ✅ — 這些失敗是本 PR 引入的，必須修"

    # main 也有失敗 — 查具體哪些 job 失敗
    if run_id:
        r2 = run(
            ["gh", "run", "view", str(run_id), "--json", "jobs"],
            timeout=15,
        )
        if r2.returncode == 0:
            try:
                data = _json.loads(r2.stdout)
                main_failed_jobs = {
                    j["name"] for j in data.get("jobs", [])
                    if j.get("conclusion") == "failure"
                }
                pr_failed_names = {c["name"] for c in failed_checks}
                only_pr = pr_failed_names - main_failed_jobs
                shared = pr_failed_names & main_failed_jobs

                parts = []
                if shared:
                    parts.append(f"pre-existing（main 也 fail）: {', '.join(sorted(shared))}")
                if only_pr:
                    parts.append(f"本 PR 引入: {', '.join(sorted(only_pr))}")
                if parts:
                    return "→ A/B 分類: " + " | ".join(parts)
            except (_json.JSONDecodeError, KeyError):
                pass

    return f"→ main CI 也是 {main_conclusion} — 部分失敗可能是 pre-existing"


def check_ci_status(pr_number: Optional[int] = None) -> CheckResult:
    """查詢 GitHub CI 狀態。"""
    # gh pr checks --json fields: name, state, bucket, description, event, link, startedAt, completedAt, workflow
    # Note: 'conclusion' is NOT a valid field (use 'bucket' for PASS/FAIL/PENDING)
    if pr_number:
        cmd = ["gh", "pr", "checks", str(pr_number), "--json", "name,state,bucket"]
    else:
        cmd = ["gh", "pr", "checks", "--json", "name,state,bucket"]

    r = run(cmd, timeout=30)
    if r.returncode != 0:
        # gh not available or no PR
        err = (r.stderr or "").strip()
        if "no pull requests" in err.lower() or "no open pull request" in err.lower():
            return CheckResult(
                "CI status",
                Status.SKIP,
                "尚未建立 PR — 無法查詢 CI",
            )
        return CheckResult(
            "CI status",
            Status.WARN,
            "無法查詢（gh CLI 不可用或網路問題）",
            detail=err[:200],
        )

    import json

    try:
        checks = json.loads(r.stdout)
    except json.JSONDecodeError:
        return CheckResult("CI status", Status.WARN, "無法解析 gh 輸出")

    if not checks:
        return CheckResult("CI status", Status.WARN, "PR 無 CI checks（workflow 未觸發？）")

    # bucket: "pass" | "fail" | "pending" | "skipping"
    failed = [c for c in checks if c.get("bucket") == "fail"]
    pending = [c for c in checks if c.get("bucket") == "pending"]
    passed = [c for c in checks if c.get("bucket") == "pass"]

    if failed:
        detail = "\n".join(f"· {c['name']}" for c in failed)
        # A/B 分類：比對 main 的 CI 狀態，區分 pre-existing vs this-PR failure
        ab_note = _classify_ci_failures(failed)
        if ab_note:
            detail += f"\n{ab_note}"
        return CheckResult(
            "CI status",
            Status.FAIL,
            f"{len(failed)} failed / {len(passed)} passed / {len(pending)} pending",
            detail=detail,
        )
    if pending:
        names = ", ".join(c["name"] for c in pending[:3])
        return CheckResult(
            "CI status",
            Status.WARN,
            f"{len(pending)} 個 check 還在跑: {names}",
        )
    return CheckResult("CI status", Status.PASS, f"全部 {len(passed)} 個 checks 通過")


def check_pr_mergeable(pr_number: Optional[int] = None) -> CheckResult:
    """查詢 PR mergeable 狀態。"""
    if pr_number:
        cmd = ["gh", "pr", "view", str(pr_number), "--json", "mergeable,mergeStateStatus,reviewDecision"]
    else:
        cmd = ["gh", "pr", "view", "--json", "mergeable,mergeStateStatus,reviewDecision"]

    r = run(cmd, timeout=30)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "no pull requests" in err.lower() or "no open pull request" in err.lower():
            return CheckResult(
                "PR mergeable",
                Status.SKIP,
                "尚未建立 PR",
            )
        return CheckResult("PR mergeable", Status.WARN, "無法查詢", detail=err[:200])

    import json

    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return CheckResult("PR mergeable", Status.WARN, "無法解析 gh 輸出")

    mergeable = data.get("mergeable", "UNKNOWN")
    state = data.get("mergeStateStatus", "UNKNOWN")
    review = data.get("reviewDecision", "")

    if mergeable == "CONFLICTING":
        return CheckResult(
            "PR mergeable",
            Status.FAIL,
            f"GitHub 偵測到衝突（state={state}）",
        )
    if state == "BLOCKED":
        reason = "需要 review approval" if review != "APPROVED" else "其他 branch protection rule"
        return CheckResult(
            "PR mergeable",
            Status.WARN,
            f"BLOCKED — {reason}",
        )
    if mergeable == "MERGEABLE" and state == "CLEAN":
        return CheckResult("PR mergeable", Status.PASS, "可直接 merge")
    return CheckResult(
        "PR mergeable",
        Status.WARN,
        f"mergeable={mergeable}, state={state}",
    )


# ─── Main ────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PR Preflight Check — branch 收尾前的自動化檢查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例：
  %(prog)s                    # 完整檢查（含 local hooks）
  %(prog)s --skip-hooks       # 跳過 pre-commit（快速檢查）
  %(prog)s --ci               # CI 模式（有 FAIL 則 exit 1）
  %(prog)s --pr 23            # 指定 PR 號碼
""",
    )
    parser.add_argument("--skip-hooks", action="store_true", help="跳過 local pre-commit hooks（快速模式）")
    parser.add_argument("--ci", action="store_true", help="CI 模式：有 FAIL 時 exit 1")
    parser.add_argument("--pr", type=int, default=None, help="指定 PR 號碼（不指定則自動偵測）")
    args = parser.parse_args()

    # cd to repo root
    repo_root = find_repo_root()
    os.chdir(repo_root)

    report = PreflightReport()

    # 1. Branch identity
    report.add(check_branch_identity())

    # 2. Behind main
    report.add(check_behind_main())

    # 3. Conflict detection
    report.add(check_conflict())

    # 4. Local hooks (optional)
    if args.skip_hooks:
        report.add(CheckResult("Local hooks", Status.SKIP, "已跳過（--skip-hooks）"))
    else:
        report.add(check_local_hooks())

    # 5. CI status
    report.add(check_ci_status(args.pr))

    # 6. PR mergeable
    report.add(check_pr_mergeable(args.pr))

    report.print_summary()

    if args.ci and report.has_failure:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
