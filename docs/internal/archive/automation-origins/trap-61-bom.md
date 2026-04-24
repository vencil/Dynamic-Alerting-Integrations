---
title: "Trap #61 原 RCA — PowerShell BOM 污染 commit message"
tags: [archive, automation-origins, windows-mcp, powershell, commitlint]
audience: [maintainers, ai-agent]
codified-as: PR #56
original-playbook: windows-mcp-playbook.md
codified-at-version: v2.8.0
status: archived
lang: zh
---

# Trap #61 — PowerShell `Out-File -Encoding utf8` 寫 commit message 加 U+FEFF BOM

> **⚠️ 本文件為 archive**：trap 已由 `scripts/tools/dx/pr_preflight.py::check_commit_msg_file` 先跑 `detect_commit_msg_bom()` codified（PR #56, v2.8.0）。實戰請看 [`windows-mcp-playbook.md`](../../windows-mcp-playbook.md) 已知陷阱清單第 61 列的精簡版。
> 本 archive 保留原 RCA 作為 debug 參考：若 automation 自身出 bug、若需追溯設計脈絡、或若規則精神需驗證時閱讀。

## 情境

Windows PowerShell 5.1 的 `Out-File -Encoding utf8` 與 `Set-Content -Encoding utf8` **強制**在檔案開頭寫 U+FEFF BOM（3 bytes：`EF BB BF`）。`git commit -F file.txt` 把 BOM 當成 subject 的第一個字元，commitlint 用 regex `^<type>(<scope>)?: <subject>` 解 header 時，`<type>` 吃不到字母（因為首字是 U+FEFF 不是 ASCII alnum），進入 header-trim / subject-empty / type-empty 三層失敗 cascade。

## 受影響流程

1. AI agent 在 PowerShell 寫 commit message：
   ```ps1
   $msg | Out-File -Encoding utf8 commit.txt
   git commit -F commit.txt
   ```
2. 本地 commit-msg hook 看到 U+FEFF 後 `validate_conventional_header` 回 `type '\ufefffeat' not in allowed enum`
3. 若 bypass 本地 hook（`--no-verify`）則 push 後 CI commitlint 以同類錯誤擋 PR

## 與 #32 的區別

- **Trap #32**：`Set-Content` 預設 BOM 汙染 JSON → `curl --data-binary @file` GitHub API parse fail
- **Trap #61**：同一類型 BOM 污染但目標是 git commit message body / subject → commitlint header regex 匹配失敗

## 修法（long-term, codified PR #56）

`scripts/tools/dx/pr_preflight.py`:
```python
_KNOWN_COMMIT_MSG_BOMS = {
    b"\xef\xbb\xbf": ("UTF-8 BOM", "U+FEFF"),
    b"\xff\xfe":     ("UTF-16 LE BOM", "PS default Out-File encoding"),
    b"\xfe\xff":     ("UTF-16 BE BOM", "less common but same failure mode"),
}

def detect_commit_msg_bom(path: Path) -> Optional[str]:
    head = path.read_bytes()[:3]
    for marker, (name, origin) in _KNOWN_COMMIT_MSG_BOMS.items():
        if head.startswith(marker):
            return f"{name} (bytes {hex_str}, typical origin: {origin})"
    return None
```

`check_commit_msg_file` 在讀檔解碼前先呼叫 `detect_commit_msg_bom()`；命中時 exit 1 + stderr 直接印對應修法：

```
❌ commit-msg encoding error:
   - file starts with UTF-8 BOM (bytes EF BB BF, typical origin: U+FEFF)
   - commitlint interprets the BOM as part of the subject → type-empty / subject-empty cascade.
     Fix (PowerShell):
       [IO.File]::WriteAllText($p, $msg, [Text.UTF8Encoding]::new($false))
     Fix (bash): printf '%s\n' "$msg" > commit.txt
     Recovery (already-pushed commits):
       git filter-branch --msg-filter "sed '1s/^\xEF\xBB\xBF//'" <range>
```

錯誤訊息本身即 playbook，AI agent 或人類看 stderr 即可修復，不需翻 playbook。

## 出事救援

| 情境 | 修法 |
|---|---|
| **寫檔前** | PowerShell: `[IO.File]::WriteAllText($p, $msg, [Text.UTF8Encoding]::new($false))`<br>bash: `printf '%s\n' "$msg" > file.txt` |
| **Commit 已建立但未 push** | `git commit --amend -F <new-no-bom-file>` |
| **Commit 已 push**（歷史多筆 BOM） | `git filter-branch --msg-filter "sed '1s/^\xEF\xBB\xBF//'" <range>` → `git push --force-with-lease` |

## 偵測優先序

`detect_commit_msg_bom()` 在 `check_commit_msg_file` 內**首先**執行，**先於** conventional-header validation 與 body/footer line-length check。理由：BOM 命中時，後續的 header 解析會回出無意義的 `type 'X' not in enum` 錯誤（X 是 BOM + 真 type 混字），對 user 毫無幫助。BOM-first 策略確保 user 直接看到「你的檔有 BOM，這樣修」，而不是「你的 type 叫 `\ufefffeat` 不在 enum」。

## 原 playbook 歸檔入口

v2.8.0-planning §12.4 #8（historical trap ledger，maintainer-local / gitignored）。

## 相關 trap 交叉引用

- **Trap #32** — `Set-Content` 預設 BOM 破 JSON parse（同根因不同受害對象）
- **Trap #45** — `.bat` 檔案 UTF-8 BOM 破 cmd batch parser（BOM 破壞的第三個場景）
- **PR #55** — commit-msg hook body/footer line-length enforcement（同 hook 內另一層 validation）
