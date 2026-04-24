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
    """Run a command with sensible defaults.

    Uses errors="replace" on decoding because tools like git may emit
    localized progress/stderr in Windows codepages (e.g. 0x93 smart-quote
    from cp1252), which would otherwise crash the whole preflight with a
    UnicodeDecodeError. We only consume stderr for display/grep, so
    replacement characters are fine.
    """
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
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


# ---------------------------------------------------------------------------
# Conventional-commits message / PR title validator (PR #44 C2)
# ---------------------------------------------------------------------------
# Mirrors the subset of commitlint rules encoded in .commitlintrc.yaml:
#   - type-enum (level 2, always)
#   - scope-enum (level 2, always)
#   - subject non-empty
#   - header max-length (default 100 — conventional-commits)
# Kept pure so tests can feed messages without touching git state.

CONVENTIONAL_HEADER_RE = re.compile(
    r"^(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[^)]+)\))?"
    r"(?P<bang>!)?"
    r":\s*(?P<subject>.+)$"
)


def _read_commitlint_enum(repo_root: Path, key: str) -> Optional[List[str]]:
    """Parse type-enum / scope-enum from .commitlintrc.yaml without PyYAML.

    The rule block looks like:
        type-enum:
          - 2
          - always
          - - feat
            - fix
            ...

    We just need the leaf list. Hand-rolled so we don't add a runtime dep.
    Returns None if the key isn't found (callers treat that as "no restriction").
    """
    config = repo_root / ".commitlintrc.yaml"
    if not config.exists():
        return None
    # Explicit utf-8 for parity with check_commit_msg_file (L270) — the file
    # is currently ASCII but defensive encoding avoids future cp950 surprises
    # on Windows.
    lines = config.read_text(encoding="utf-8", errors="replace").splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == f"{key}:":
            # Skip the level (2) and applicability (always) lines, then consume
            # the leaf list.
            j = i + 1
            enum: List[str] = []
            seen_inner = False
            while j < len(lines):
                line = lines[j]
                s = line.strip()
                if not s or s.startswith("#"):
                    j += 1
                    continue
                # Dedent-check: if we hit a line at column 0 or less indent
                # than the leaf list, we're done.
                indent = len(line) - len(line.lstrip())
                if indent <= 2 and seen_inner:
                    break
                # Leaf entries: "- foo" at deeper indent than the key
                if s.startswith("- "):
                    val = s[2:].strip()
                    # Skip the "2" and "always" meta entries
                    if val in ("2", "always", "never", "0", "1"):
                        j += 1
                        continue
                    # Nested list start ("- - foo") — treat same: strip extra "-"
                    if val.startswith("- "):
                        val = val[2:].strip()
                    # Strip quotes if present
                    val = val.strip("'\"")
                    # Strip inline comment
                    if "#" in val:
                        val = val.split("#", 1)[0].strip()
                    if val:
                        enum.append(val)
                        seen_inner = True
                j += 1
            return enum
        i += 1
    return None


def validate_conventional_header(
    header: str,
    type_enum: Optional[List[str]] = None,
    scope_enum: Optional[List[str]] = None,
    max_length: int = 100,
) -> List[str]:
    """Validate a single conventional-commits header line.

    Returns a list of error messages. Empty list == pass.
    """
    errors: List[str] = []
    header = header.rstrip("\n\r")

    if not header.strip():
        errors.append("header is empty")
        return errors

    if len(header) > max_length:
        errors.append(f"header too long: {len(header)} > {max_length}")

    m = CONVENTIONAL_HEADER_RE.match(header)
    if not m:
        errors.append(
            "header does not match conventional-commits format "
            "'type(scope): subject' or 'type: subject'"
        )
        return errors

    t = m.group("type")
    s = m.group("scope")
    subject = m.group("subject").strip()

    if type_enum is not None and t not in type_enum:
        errors.append(
            f"type '{t}' not in allowed enum: {', '.join(sorted(type_enum))}"
        )

    if s is not None and scope_enum is not None and s not in scope_enum:
        # Multi-scope like "dx+e2e" is a single scope string (already allowed
        # via explicit enum entries); nothing special needed here.
        errors.append(
            f"scope '{s}' not in allowed enum: {', '.join(sorted(scope_enum))}"
        )

    if not subject:
        errors.append("subject is empty")

    return errors


# v2.8.0 Issue #53: commitlint body/footer line-length enforcement.
#
# Before this PR the local commit-msg hook only validated the header.
# commitlint in CI (.github/workflows/commitlint.yaml) additionally
# enforces footer-max-line-length=100 via @commitlint/config-conventional
# defaults, which bit PR #51 and PR #52 — long pytest command paths at
# the end of the body got classified as "footer" and rejected in CI.
#
# Commitlint uses conventional-commits-parser to split the message into
# header / body / footer. We don't re-implement the full parser (would
# require Node-style trailer detection); instead we apply the
# **conservative** rule: **any post-header line > 100 chars = ERROR**.
# This is strictly stricter than CI (which lets body lines up to 200 per
# our .commitlintrc.yaml override) — if a committer writes a legit long
# prose line in body, they'll need to wrap at 100 locally. Trade-off is
# acceptable: false-positive rate is low (commit messages should wrap at
# 72-100 anyway per git convention), false-negative rate would be high
# (letting PR #51-class errors through is what we want to avoid).
#
# The 100-char bound and the name `POST_HEADER_MAX_LINE_LENGTH` are
# intentional: commitlint default for footer-max-line-length is 100.
POST_HEADER_MAX_LINE_LENGTH = 100


def validate_commit_msg_body(lines: list[str], max_line_length: int = POST_HEADER_MAX_LINE_LENGTH) -> list[str]:
    """Check every post-header line for length + blank-line conventions.

    `lines` is the raw splitlines() of the commit-msg file. Leading
    comment/empty lines are discarded; the first non-comment non-empty
    line is treated as the header. Everything below goes through the
    body/footer checks.

    Returns list of error strings (empty = pass). Each error prefixed
    with an [E] (error) or [W] (warning) tag so the caller can route to
    stderr/stdout appropriately.

      [E] line N too long (L chars > max): <snippet>
      [W] line N should be preceded by blank line after header (body-leading-blank)
    """
    errors: list[str] = []

    # Skip leading comments + empty lines to find the header.
    header_idx = -1
    for i, line in enumerate(lines):
        if not line.strip() or line.startswith("#"):
            continue
        header_idx = i
        break

    if header_idx < 0:
        return errors  # empty commit message, caller handles

    # Every post-header non-comment line subject to line-length check.
    # (Git strips comment lines before passing to hooks, but we stay
    # safe and skip them here too.)
    any_post_header_content = False
    for i in range(header_idx + 1, len(lines)):
        line = lines[i]
        if line.startswith("#"):
            continue
        # First non-empty line after header: should have blank line between.
        stripped = line.strip()
        if stripped and not any_post_header_content:
            any_post_header_content = True
            # Check blank-line-after-header convention. lines[header_idx+1]
            # should be empty if there's any body at all.
            if header_idx + 1 < len(lines) and lines[header_idx + 1].strip():
                errors.append(
                    f"[W] line {header_idx + 2}: body should be preceded by blank "
                    f"line after header (body-leading-blank)"
                )

        if len(line) > max_line_length:
            snippet = line if len(line) <= 60 else line[:57] + "..."
            errors.append(
                f"[E] line {i + 1} too long ({len(line)} chars > {max_line_length}): "
                f"{snippet}"
            )

    return errors


# Byte-order-mark patterns commitlint chokes on. PS 5.1's
# `Out-File -Encoding utf8` / `Set-Content -Encoding utf8` both prepend U+FEFF
# (EF BB BF) to the file — commitlint then sees the BOM as the first char of
# the header, subject-empty / type-empty / header-trim all fail in a cascade.
# See windows-mcp-playbook.md Trap #61 + v2.8.0-planning §12.4 #8.
_KNOWN_COMMIT_MSG_BOMS = {
    b"\xef\xbb\xbf": ("UTF-8 BOM", "U+FEFF"),
    b"\xff\xfe": ("UTF-16 LE BOM", "PS default Out-File encoding"),
    b"\xfe\xff": ("UTF-16 BE BOM", "less common but same failure mode"),
}


def detect_commit_msg_bom(path: Path) -> Optional[str]:
    """Return a human-readable BOM description if `path` starts with a known BOM, else None.

    Only inspects the first 3 bytes — cheap and safe on empty files.
    """
    try:
        head = path.read_bytes()[:3]
    except OSError:
        return None
    # Longer prefixes first so UTF-8 BOM (3 bytes) doesn't get masked by the
    # shorter UTF-16 LE BOM (2 bytes) on the rare chance they overlap.
    for marker, (name, origin) in sorted(
        _KNOWN_COMMIT_MSG_BOMS.items(), key=lambda kv: -len(kv[0])
    ):
        if head.startswith(marker):
            hex_str = " ".join(f"{b:02X}" for b in marker)
            return f"{name} (bytes {hex_str}, typical origin: {origin})"
    return None


def check_commit_msg_file(path: Path, repo_root: Path) -> int:
    """Validate a commit-msg file (first non-comment line is the header).

    Returns 0 on pass, 1 on fail. Prints errors to stderr.

    v2.8.0 Issue #53: also validates post-header line-length against
    POST_HEADER_MAX_LINE_LENGTH (conservative match of commitlint's
    footer-max-line-length=100 default). Warnings (prefixed [W]) do
    not fail the validation.

    v2.8.0 Trap #61: detects UTF-8 / UTF-16 BOM at file start (PowerShell
    `Out-File -Encoding utf8` default) and fails fast with a BOM-stripping
    hint — commitlint would otherwise emit a confusing cascade of
    header-trim / subject-empty / type-empty errors.
    """
    if not path.exists():
        print(f"error: commit-msg file not found: {path}", file=sys.stderr)
        return 1

    # BOM detection runs BEFORE text-decoding: a BOM slipping through as U+FEFF
    # at the top of the header is exactly what makes commitlint's error
    # messages cryptic. Fail with a specific, actionable error instead.
    bom_description = detect_commit_msg_bom(path)
    if bom_description is not None:
        print("❌ commit-msg encoding error:", file=sys.stderr)
        print(f"   - file starts with {bom_description}", file=sys.stderr)
        print(
            "   - commitlint interprets the BOM as part of the subject → "
            "type-empty / subject-empty cascade.\n"
            "     Fix (PowerShell):"
            "\n       [IO.File]::WriteAllText($p, $msg, "
            "[Text.UTF8Encoding]::new($false))\n"
            "     Fix (bash): printf '%s\\n' \"$msg\" > commit.txt\n"
            "     Recovery (already-pushed commits):\n"
            "       git filter-branch --msg-filter "
            "\"sed '1s/^\\xEF\\xBB\\xBF//'\" <range>",
            file=sys.stderr,
        )
        return 1

    # First non-comment non-empty line is the header (standard git convention).
    # Explicit utf-8: commit messages can contain CJK / em-dash; Windows
    # default cp950 would raise UnicodeDecodeError (PR #52 hit this when
    # committing with --check-commit-msg as a commit-msg hook).
    all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    header: Optional[str] = None
    for line in all_lines:
        if not line.strip() or line.startswith("#"):
            continue
        header = line
        break

    if header is None:
        # Empty commit messages are allowed by git with --allow-empty-message;
        # we don't enforce beyond that.
        return 0

    type_enum = _read_commitlint_enum(repo_root, "type-enum")
    scope_enum = _read_commitlint_enum(repo_root, "scope-enum")

    header_errors = validate_conventional_header(header, type_enum, scope_enum)
    body_findings = validate_commit_msg_body(all_lines)

    # Split body findings into errors ([E]) vs warnings ([W]).
    body_errors = [e for e in body_findings if e.startswith("[E]")]
    body_warnings = [e for e in body_findings if e.startswith("[W]")]

    if not header_errors and not body_errors and not body_warnings:
        return 0

    # Print warnings first, then errors. Both go to stderr so
    # git's commit-msg hook pipeline surfaces them.
    if body_warnings:
        print("⚠  commit-msg warnings (not blocking):", file=sys.stderr)
        for w in body_warnings:
            print(f"   {w}", file=sys.stderr)

    if header_errors or body_errors:
        print("❌ commit-msg validation failed:", file=sys.stderr)
        for e in header_errors:
            print(f"   - {e}", file=sys.stderr)
        for e in body_errors:
            # Strip the [E] tag for display consistency with header_errors.
            print(f"   - {e[4:] if e.startswith('[E] ') else e}", file=sys.stderr)
        print(f"\nHeader was:\n   {header}", file=sys.stderr)
        return 1
    return 0


def check_pr_title(title: str, repo_root: Path, max_length: int = 70) -> int:
    """Validate a PR title.

    Project convention (CLAUDE.md PR creation): title < 70 chars.
    Also enforces conventional-commits type/scope enum.

    Returns 0 on pass, 1 on fail.
    """
    type_enum = _read_commitlint_enum(repo_root, "type-enum")
    scope_enum = _read_commitlint_enum(repo_root, "scope-enum")

    errors = validate_conventional_header(
        title, type_enum, scope_enum, max_length=max_length
    )
    if errors:
        print("❌ PR title validation failed:", file=sys.stderr)
        for e in errors:
            print(f"   - {e}", file=sys.stderr)
        print(f"\nTitle was:\n   {title}", file=sys.stderr)
        return 1
    return 0


def find_repo_root() -> Path:
    """從 cwd 向上找 .git 目錄。"""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    # fallback
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent.parent


# ─── Preflight Marker (consumed by pre-push gate) ────────────
#
# `.git/.preflight-ok.<HEAD-sha>` is a zero-byte marker file written when a
# preflight run completes without FAIL. The pre-push hook
# (scripts/ops/require_preflight_pass.sh) refuses to push unless the marker
# for the exact HEAD sha exists. This prevents pushing pre-preflight commits
# that CI will likely reject.

MARKER_PREFIX = ".preflight-ok"


def _git_dir(repo_root: Path) -> Path:
    """Resolve .git dir even for worktrees (git rev-parse --git-dir)."""
    r = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    if r.returncode == 0 and r.stdout.strip():
        p = Path(r.stdout.strip())
        return p if p.is_absolute() else (repo_root / p).resolve()
    return repo_root / ".git"


def _head_sha(repo_root: Path) -> Optional[str]:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def marker_path(repo_root: Path, head_sha: str) -> Path:
    return _git_dir(repo_root) / f"{MARKER_PREFIX}.{head_sha}"


def write_marker(repo_root: Path) -> Optional[Path]:
    """Touch `.git/.preflight-ok.<HEAD>`. Returns the path on success, else None."""
    sha = _head_sha(repo_root)
    if not sha:
        return None
    p = marker_path(repo_root, sha)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(exist_ok=True)
        return p
    except OSError:
        return None


def clear_markers(repo_root: Path) -> int:
    """Remove all `.preflight-ok.*` markers. Returns count removed."""
    git_dir = _git_dir(repo_root)
    if not git_dir.exists():
        return 0
    count = 0
    for f in git_dir.glob(f"{MARKER_PREFIX}.*"):
        try:
            f.unlink()
            count += 1
        except OSError:
            pass
    return count


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


def check_scope_drift() -> CheckResult:
    """跑 check_pr_scope_drift.py — tool-map 與 working-tree 是否乾淨。

    這是 code-driven §P2 rule 的執行點，偵測 PR 準備 merge 時仍有散落
    在工作目錄、未納入此 PR commit 的 drift（典型 PR #40 肇因）。
    """
    r = run(
        ["python3", "-X", "utf8", "scripts/tools/lint/check_pr_scope_drift.py"],
        timeout=60,
    )
    if r.returncode == 0:
        return CheckResult("Scope drift", Status.PASS, "無 drift 訊號")

    # Surface the FAIL summary line (first line of stderr from the hook)
    tail = (r.stdout + r.stderr).strip().splitlines()
    headline = next(
        (ln for ln in tail if "FAIL:" in ln),
        tail[0] if tail else "(no output)",
    )
    detail = "\n".join(
        ln.strip() for ln in tail if ln.strip().startswith(("FAIL", "PASS"))
    )[:400]
    return CheckResult("Scope drift", Status.FAIL, headline, detail=detail)


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
    parser.add_argument(
        "--check-commit-msg",
        metavar="FILE",
        help="只驗 commit-msg 檔案：type/scope enum + 長度；exit 1 失敗（git commit-msg hook 用）",
    )
    parser.add_argument(
        "--check-pr-title",
        metavar="TITLE",
        help="只驗 PR title 字串：type/scope enum + 長度（預設 70）；exit 1 失敗",
    )
    parser.add_argument(
        "--pr-title-max-length",
        type=int,
        default=70,
        help="PR title 長度上限（預設 70；CLAUDE.md PR creation convention）",
    )
    args = parser.parse_args()

    # cd to repo root
    repo_root = find_repo_root()
    os.chdir(repo_root)

    # Exit-early modes: just-validate-one-thing
    if args.check_commit_msg:
        return check_commit_msg_file(Path(args.check_commit_msg), repo_root)
    if args.check_pr_title:
        return check_pr_title(
            args.check_pr_title, repo_root, max_length=args.pr_title_max_length
        )

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

    # 5. Scope drift (code-driven §P2 rule)
    report.add(check_scope_drift())

    # 6. CI status
    report.add(check_ci_status(args.pr))

    # 7. PR mergeable
    report.add(check_pr_mergeable(args.pr))

    report.print_summary()

    # --- Preflight marker (consumed by pre-push gate) --------------------
    # On PASS (with or without WARN): write `.git/.preflight-ok.<HEAD>` so
    # require_preflight_pass.sh lets the subsequent `git push` through.
    # On FAIL: clear any stale markers so the user can't push a broken SHA
    # that happened to have an older successful marker.
    if report.has_failure:
        cleared = clear_markers(repo_root)
        if cleared:
            print(f"   ↳ cleared {cleared} stale preflight marker(s)")
    else:
        marker = write_marker(repo_root)
        if marker:
            print(f"   ↳ wrote preflight marker: {marker.name}")

    if args.ci and report.has_failure:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
