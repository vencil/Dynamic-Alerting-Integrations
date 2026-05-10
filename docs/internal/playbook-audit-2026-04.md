---
title: "Playbook Audit (2026-04 / 05) — 標準偏離、harness 強化、逃生門收編"
tags: [internal, audit, governance]
audience: [maintainer]
version: v2.7.0
verified-at-version: v2.7.0
lang: zh
status: in-progress
---
# Playbook Audit (2026-04 / 05) — 標準偏離、harness 強化、逃生門收編

> Maintainer-driven audit。本文件是這次 audit 的**實作計畫 + 執行紀錄 + 自我 review**。
> 完成後抄入 dev-rules / windows-mcp-playbook，本檔轉 archive。

## 目的（user 起始 prompt 的 7 條精簡）

1. **走標準路線檢查** — Playbook 內容是否走偏？外面有更好的做法嗎？是否需要流程再造？
2. **避免「為寫而寫」** — 經驗累積要 ROI 正，不要徒增搜尋困難。
3. **AI 導覽品質** — 決策樹簡化判斷 vs 留合理發想空間，兩者要兼顧。
4. **Dev container 主路徑、Windows 逃生門** — 這個思路在專案中要保持，不能讓 session 因 FUSE 整個卡死。
5. **逃生門收編** — 已有 session 自己想出 Windows 逃生空間（`scripts/ops/win_*`），但 user 觀察到 `C:\Users\vencs` 有「超多檔案」。要驗收：(a) 是不是真的在重新造輪子；(b) 沒重新造輪子時，把該收的收進內部標準工具；(c) 確保 secret 不外洩；(d) 還使用者乾淨的 home 目錄。
6. **#11 檔案衛生** — `sed -i` 在掛載路徑仍常被踩。比起靠文字提醒，能不能 harness-level 攔截？
7. **先計畫再執行再 review** — Harness 級別比一段話可靠；避免「AI 看過但忽略」。

## §1 Audit findings

### 1.1 Playbook 落在標準偏離光譜的哪裡？

| 維度 | 業界標準 | 本 repo 做法 | 評估 |
|---|---|---|---|
| Pre-commit hooks (file-hygiene / lint) | `pre-commit` framework | 同款 + 39 auto + 14 manual + 3 pre-push | ✅ 標準 |
| Conventional Commits + commitlint | commitlint + husky | 自寫 `scripts/hooks/commit-msg` 解 `.commitlintrc.yaml` (避開 npm 依賴 in MCP shell) | ⚠️ 偏離但合理：Windows 側無 husky 環境 |
| Branch protection / PR gate | GitHub Branch Protection rules | `protect_main_push.sh` + `require_preflight_pass.sh` (local pre-push) | ⚠️ 偏離但合理：個人帳號 repo + 想在 push 前就攔 |
| Dev Container 統一環境 | VS Code Dev Container | `vibe-dev-container` + `make dc-*` wrapper | ✅ 標準 |
| FUSE phantom lock 處理 | **無業界標準** — 這是 Cowork VM + Windows + Docker bind mount 的特殊組合 | 自寫 `fuse_plumbing_commit.py` + Windows 逃生門 + `make fuse-*` | 🆕 必要創新：沒有 off-the-shelf 解 |
| Ad-hoc script 防擴張 | 無一致業界做法 | `check_ad_hoc_git_scripts.py` whitelist (S#46 / Trap #54 因應) | ✅ 創新但合理 |
| `sed -i` 防護 | 通常靠 reviewer | 三層：`vibe-sed-guard.sh` shell wrapper + `detect_sed_damage.py` pre-commit + `fix_file_hygiene.py` repair | ⚠️ **Harness 層缺一塊** — Claude Code PreToolUse 沒擋 |

**結論**：playbook 沒有走偏標準到「需要流程再造」。FUSE / MCP 的部分本來就沒有業界 baseline。**真正可以強化的點是 #11 預防層**（見 §1.4）。

### 1.2 「為寫而寫」風險檢查

掃描 `docs/internal/`（24 份 .md，12468 行）找冗贅或可歸檔項目：

| 文件 | 行數 | 狀態 | 建議 |
|---|---|---|---|
| `windows-mcp-playbook.md` | 1031 | **§修復層 D 重複出現兩次**（lines 950-987 + 996-1035），near-identical 內容 | 🟡 Dedupe |
| `ssot-language-evaluation.md` | 787 | dev-rules §9b 寫 `status: superseded by S#101`（policy locked，不執行 ZH→EN 遷移） | 🟡 Move to `archive/` |
| `ssot-migration-pilot-report.md` | 196 | 同上，`execution phase cancelled` | 🟡 Move to `archive/` |
| `windows-mcp-playbook §已知陷阱速查` 1-62 | ~250 行 | 35 條已標 ✅ Codified / 🟢 Resolved；多條互相重複（Trap #44 phantom 薛丁格態 ↔ Trap #27 lock 殘留 ↔ Trap #43 stash 死鎖）；多條已收編成 hook + tool 的不再需要逐字看 | 🟡 split 為「**Active traps**（仍需人工注意）」+「**Codified history**（可摺疊／搬 archive）」兩層 |
| `dev-rules.md` | 499 | 線上 cap 500 已就位（`check_devrules_size.py`） | ✅ 良好控制 |
| `testing-playbook.md` | 1405 | 多個版本 LL 並列、無歸檔策略 | ⚪ 暫不動（出 scope；單獨可開 audit） |

### 1.3 AI 導覽品質：決策樹 vs 發想空間

| 區域 | 現況 | 評估 |
|---|---|---|
| `windows-mcp-playbook §Git 操作決策樹` | 完整 4 層 if-else（preflight → fuse-reset → win_git_escape → 回報使用者） | ✅ 範本 |
| `windows-mcp-playbook §修復層 C` 工具選擇表 | 表格化「操作類型 → 走哪邊 → 原因」 | ✅ 範本 |
| `windows-mcp-playbook §修復層 D` 三層改進 | 三層 + Layer 1 one-liner | ✅ 範本 |
| **dev-rules §11 (sed -i)** | **僅文字描述「禁用」，無決策樹、無「我該用什麼替代」的快查** | 🟡 改善點 |
| `testing-playbook` LL 區塊 | 純敘事，無「這條 LL 在什麼情境會 trigger」的 routing | ⚪ 暫不動 |

**設計原則複述**：決策樹適用於「動作有限 + 路徑可枚舉」的情境（git 操作、commit 路徑），不適用於「需要創造性 debug」的情境（像 race flake 排查）。後者要留給 AI 自行發想，playbook 只給「**這類問題怎麼開機（起手式）**」。

### 1.4 Dev container 主路徑 / Windows 逃生門 — 思路保持狀況

✅ **已落地的設計信號**（不需重做，但要 surface 得更顯眼）：

- `windows-mcp-playbook §三層環境職責矩陣` 已寫「**Dev Container 是主路徑。FUSE 卡死時 Windows 是逃生門**」
- `CLAUDE.md` 第 5-8 行已用 `> 主路徑 / 逃生門` blockquote 標出
- `make win-commit` 設計「Sandbox hook-gate → Windows stage/commit/push」三段式 — gate 沒繞過、Windows 端 `--no-verify` 是內部實作細節

⚠️ **discoverability gap**：
- 新 session 看到問題時不會立刻看到這 13 個 escape-hatch 工具的清單。要 grep 到 windows-mcp-playbook §修復層 C 的表格才看得到。
- `make help` 未把 escape-hatch 工具獨立分群。

### 1.5 是不是 session 重新造輪子？

驗收結果：**沒有**。`scripts/tools/lint/check_ad_hoc_git_scripts.py` whitelist 已物理性 block 掉 `_*.bat` / `_*.ps1` / `_*.cmd` 落在 `scripts/ops/` / `scripts/tools/` / `tools/` 之外。已有的 13 個 escape-hatch 工具 codify 完整（涵蓋 git / gh / async / fresh-read / sandbox-hooks / plumbing / index-recover / lock-detect）。

**但 user 觀察到 `C:\Users\vencs` 有「超多檔案」是事實** — 這些不是「重新造輪子的 escape-hatch 程式」，是 **runtime scratch artifacts**（漏掉 cleanup）：

| 路徑 | 檔案 | 性質 | 處置 |
|---|---|---|---|
| `C:\Users\vencs\AppData\Local\Temp\vibe-bat-*.txt` / `vibe-git-*.txt` | 4 個 | `win_git_escape.bat` 寫到 `%TEMP%\vibe-git-*.txt` 的 stdout/stderr 重定向（設計如此）— 但歷次 run 的 latest 一直留著 | 設計合理（最新一次的 log 確實要 read），但**沒人清舊** |
| `…\Temp\pr*-msg.txt` (8 個) | 8 個 | 某次 PR sweep 的 commit-message 草稿，sweep 完未刪 | 🔴 漏掉 cleanup |
| `…\Temp\commit-out*.txt` / `_jsx_out*.txt` / `_out.txt` / `pre-commit-final.yaml` | 6 個 | 同上 | 🔴 漏掉 cleanup |
| `C:\tmp\audit_open*.py` / `bulk_annotate_tests.py` / `fix_encoding.py` / `probe.go` / `_backup.css` / `_backup.jsx` / `_msg.txt` / `test_violations.txt` | ~10 個 | 過往 session 的 ad-hoc 分析腳本 / 備份 | 🔴 漏掉 cleanup |
| `C:\tmp\vibe-session-init.*` | 8 個 | session-init hook 的 marker（design intent — 跨 session 跑同一個 session 用同 marker） | ✅ 設計如此（跨 day 累積為 staleness signal — 最舊的 5/2，可清） |

**Secret 掃描**：以 `ghp_/github_pat_/password/token=/secret=/AKIA` regex 掃過全部 stray 檔案，**0 命中**。

**根因**：`make session-cleanup` 在 session 結束時跑，但**只清 repo 內**的東西，沒擴及 `%TEMP%` 和 `/c/tmp/`。`win_git_escape.bat` 自己沒做 log rotation。

### 1.6 #11 sed -i 預防層 gap

目前三層防護：

| 層 | 工具 | 觸發時機 | 缺口 |
|---|---|---|---|
| 1. Repair | `fix_file_hygiene.py` (pre-commit) | commit 時自動補 EOF newline + 移 NUL | 損壞已發生才補 |
| 2. Detect | `detect_sed_damage.py` (pre-commit) | commit 時 abort 損壞檔案 | 同上，token 已花掉 |
| 3. Prevent (shell) | `vibe-sed-guard.sh` (bash function override) | 在 shell session 內 source 過才生效 | **Claude Code Bash tool 不 source `.bashrc`** → 從未觸發 |

**Harness 層空缺**：Claude Code 的 `PreToolUse` hook 機制可以攔截 Bash tool 的命令字串、回傳 exit 2 + stderr message → Claude 看到後會自我修正。**這是 user 想要的「比文字更好的管制做法」**。

## §2 實作計畫（10 工項，按依賴序）

> 命名：`A=Audit/cleanup`, `H=Harness`, `D=Doc`, `T=Tooling`. 編號標 priority；P0 must-do。

### A1. (P0) 清理 user-home stray scratch（不刪 active session marker）

**動作**：
1. `C:\Users\vencs\AppData\Local\Temp\` 刪：`pr*-msg.txt` × 8 / `commit-out*.txt` × 3 / `_jsx_out*.txt` × 2 / `_out.txt` / `pre-commit-final.yaml`
2. `C:\Users\vencs\AppData\Local\Temp\vibe-{bat,git}-*.txt` × 4 — 保留（runtime artifact，下次 run 會 overwrite，刪掉沒事）→ 一併刪
3. `C:\tmp\` 刪：`audit_open*.py` / `bulk_annotate_tests.py` / `fix_encoding.py` / `probe.go` / `_backup.{css,jsx}` / `_msg.txt` / `test_violations.txt`
4. `C:\tmp\vibe-session-init.*` — 保留**最近 24 hr 內**的（current session 可能還在用），刪舊的（>1 day = stale）
5. **不動** `C:\Users\vencs` 根目錄（NTUSER.DAT 系列、`.gitconfig`、`.claude.json` 都是個人 / 系統檔案）
6. **不動** `Downloads\Fix-ClaudeDesktop.{bat,ps1}`（第三方 Italian script，user 個人 utility，無關 vibe）

**驗收**：`ls C:/Users/vencs/AppData/Local/Temp | grep -cE "pr[0-9]|commit-out|_jsx_out|vibe-bat|vibe-git"` ⇒ 0；`ls C:/tmp` 只剩當前 session 的 marker。

### H1. (P0) Claude Code PreToolUse hook：攔 `sed -i` 在掛載路徑

**動作**：
1. 寫 `scripts/session-guards/preflight_bash.py`（PreToolUse hook for `Bash` tool）：
   - 讀 stdin JSON（Claude Code 傳的 tool input：`{"tool_name":"Bash","tool_input":{"command":"...","description":"..."}}`）
   - 解析 `command`，偵測 `\bsed\s+(-[^-\s]*i|--in-place)` 樣式
   - 若 detected + 路徑符合 mount path pattern（`/sessions/*/mnt/`、`/workspaces/`、`C:[\\/].*[\\/]vibe-k8s-lab[\\/]`）→ exit 2 with stderr 給 Claude 看到的 remediation message
   - 其他 case → exit 0 (allow)
   - 失敗策略：parse error / unhandled exception → exit 0 + stderr warning（**永不 block 正常 tool call**）
2. 註冊到 `.claude/settings.json` PreToolUse 陣列
3. 寫 `tests/session-guards/test_preflight_bash.py` cover 6 種模式（`sed -i`、`sed -i.bak`、`sed -i'' '...'`、`sed -i...` 黏寫、`sed -i` 但路徑不在 mount、命令含 `&&` 串接時兩個都要看）

**為什麼比文字描述好**：
- 不依賴 Claude 「看到」規則 → 強制 enforcement
- exit 2 + stderr 是 Claude Code spec 的「block + 把錯誤訊息回傳給 model」機制，model 會自我修正
- `vibe-sed-guard.sh`（shell wrapper）保留作為「人類 dev / docker exec」的第二道防線

**驗收**：手動跑 `python scripts/session-guards/preflight_bash.py < echo '{"tool_name":"Bash","tool_input":{"command":"sed -i s/foo/bar/ /workspaces/vibe-k8s-lab/x.md"}}'` ⇒ exit 2 + 訊息對。

### H2. (P1) PreToolUse hook：擴展檢查 → 偵測 ad-hoc `_*.bat` write 嘗試

**動作**：在 H1 同支 hook 加「Write tool 檢查」分支 — 當 `tool_name=Write` 且 `tool_input.file_path` 命中 `^.*[\\/]_[^\\/]*\.(bat|ps1|cmd)$` 而**不在** `scripts/ops/` 下，攔截並指向 `windows-mcp-playbook §修復層 C`「擴充現有 wrapper，不要寫 sibling script」。

**為什麼**：現在 `check_ad_hoc_git_scripts.py` pre-commit hook 在 commit 時才擋；session 還是會花 token 把檔案寫好才被擋。Pre-write 攔截直接省 token + 引導 AI 走標準路徑。

**驗收**：手動測 `Write file_path=/workspaces/vibe-k8s-lab/_my_commit.bat` ⇒ exit 2，訊息提到 `win_git_escape.bat raw <args>`。

### D1. (P0) Dedupe `windows-mcp-playbook.md §修復層 D`

**動作**：line 952-987 是初版 §D，line 996+ 是「三層改進完整版」。保留後者（更完整），把前者當作開頭簡介，去掉 Layer 1 的重複範例。

**驗收**：`grep -c "^### Layer 1 — A/B 驗證 one-liner" docs/internal/windows-mcp-playbook.md` ⇒ 1。

### D2. (P0) `dev-rules §11` 加決策樹

**動作**：在 §11 規則描述後面加：

```
我要改檔案內容
├─ 單檔小改 → Read + Edit（首選）
├─ 整檔重寫 → Read + Write
├─ 批次同樣 pattern → Python script + atomic write
│                     範例：scripts/tools/dx/_atomic_write.py
└─ pipe 改完寫回 → git show HEAD:file | sed '...' | tr -d '\0' > file
                  （非 in-place，從 HEAD 讀避開 FUSE stale）
```

加在 §11 規則本文 + 範例之後。**Why：**user 明確問「除了文字還能怎樣」。決策樹給 AI 一個 4-way fallback 路徑，比 freeform 思考省 token。

**驗收**：`grep -A 8 "我要改檔案內容" docs/internal/dev-rules.md` 應顯示完整決策樹。

### D3. (P1) 歸檔 SSOT obsolete 評估文件

**動作**：
- `git mv docs/internal/ssot-language-evaluation.md docs/internal/archive/ssot-language-evaluation.md`
- `git mv docs/internal/ssot-migration-pilot-report.md docs/internal/archive/ssot-migration-pilot-report.md`
- 在 `dev-rules §9b`、`CLAUDE.md §語言策略` 更新引用路徑

**Why**：這兩份 dev-rules §9b 已宣告 `status: superseded by S#101`，留在 active 目錄會誤導未來 session。歸檔不刪除（保留 audit trail）。

**驗收**：`find docs/internal -name "ssot-*.md" -not -path "*/archive/*"` 為空。

### D4. (P1) `windows-mcp-playbook §已知陷阱速查` 拆 Active / Codified 兩層

**動作**：把 62 條陷阱拆成兩個 table：
- **Active traps** — 仍需人工警覺（沒 ✅ Codified / 🟢 Resolved 標籤的）：保留主表
- **Codified history** — 已被 hook / tool / CI gate 攔住的（35 條）：摺疊成 `<details>` / 移到子節 §H 「Codified Trap Archive」

**Why**：1031 行對新 session 是 cognitive load，35 條已 codified 的不需逐字看。但完全刪除會丟掉「為什麼 hook 長這樣」的 context — 摺疊保留比刪除好。

**驗收**：主表行數 < 30；摺疊區內容完整保留。

### T1. (P1) `make help-escape` — Escape-hatch 工具速查

**動作**：在 `Makefile` 加 `help-escape` target，列出 13 個 escape-hatch 工具的單行用法（從 windows-mcp-playbook §修復層 C 表格抄）。grep 友善 + 一頁顯示。

**Why**：discoverability。session 卡住時 `make help-escape` 比翻 1031 行 playbook 快。

**驗收**：`make help-escape | wc -l` ≤ 30；每行格式 `name — purpose`。

### T2. (P2) Session-cleanup 擴展到 user-home scratch

**動作**：`make session-cleanup` 加掃 `%TEMP%` / `~/.cache/` / `/c/tmp/` 內 vibe scratch artifacts（age > 1 hr，prefix 命中 `vibe-bat-`、`vibe-git-`、`pr*-msg`、`commit-out*`、`_jsx_out*`、`_out.txt`、`audit_*.py`、`bulk_*.py`、`probe.*`、`_backup.*`）。需要 `--dry-run` flag 預設只列、`--apply` 才刪。

**Why**：A1 的 cleanup 是一次性。沒這個 hook 下次 stray 又會堆積。

**驗收**：`make session-cleanup ARGS=--scratch --dry-run` 列出候選但不刪；`--apply` 才動。

### T3. (P2) `win_git_escape.bat` 自動 log rotation

**動作**：在 `:done` 路徑加「保留 5 份歷史」邏輯（`vibe-git-out.txt.{1..5}` rolling），或乾脆每次 run 用 timestamp suffix + 3 day 留存。

**Why**：A1 觀察 `vibe-bat-out.txt` 自 5/5 起一直存到 5/10，5 days 過了不再有意義。

**驗收**：跑 `win_git_escape.bat status` 5 次後，`%TEMP%\vibe-git-out.*` 不會無限堆積。

## §3 Out-of-scope（這次 audit 不動）

- `testing-playbook.md` 的 1405 行 LL 歸檔策略 — 應另開 audit
- `windows-mcp-playbook §FUSE Phantom Lock 防治` 整個架構 — 已是現行最佳設計
- `dev-rules §1-10, §12` 的 12 條規則本身 — user 只問 §11 改善
- `commit-convention.md` / `release-signing-runbook.md` / `security-audit-runbook.md` — 不在 scope
- 更動 pre-commit hook 數量 / 順序 — 風險過大，今次只加 PreToolUse 不動 commit
- 改 Dev Container 鏡像或 mount 設定 — 影響面太大

## §4 自我 review checklist（執行完跑一遍）

完成 §2 後逐條檢核：

1. ☐ A1 — 跑完 `ls` 確認 stray 已清，secret scan 0 hit
2. ☐ H1 — `python preflight_bash.py` 6 個 unit test 全綠
3. ☐ H1 — `.claude/settings.json` 註冊 hook，**不要** override 既有 session-init hook（要 append 在同 matcher 陣列）
4. ☐ H2 — `_*.bat` write attempt 在 `scripts/ops/` 內**不**被擋（whitelist semantics 對）
5. ☐ D1 — `grep -c "^#### Layer 1"` 為 1（不是 2）
6. ☐ D2 — 決策樹放在 §11 範例之後而非規則描述前
7. ☐ D3 — `git log` 顯示 `git mv`（不是 `delete + add`）
8. ☐ D4 — Active trap 表 + Codified archive 兩個 anchor 都存在；舊內錨連結（如 `#已知陷阱速查`）仍可解析（用 `python scripts/tools/lint/check_doc_links.py` 驗）
9. ☐ T1 — `make help-escape` 列出 13 個工具，**一個都沒少**（cross-check `windows-mcp-playbook §修復層 C` 表格）
10. ☐ T2 — `make session-cleanup ARGS=--scratch` 預設 dry-run（不誤刪）
11. ☐ pre-commit `pre-commit run --all-files` 全綠
12. ☐ 對照 user 7 條問題逐一答覆「這次有 / 沒有處理 + 如何處理」

## §5 風險與決策依據

- **H1 hook 失敗時的策略**：永遠 exit 0 + stderr warning，不 block normal tool call。理由：harness 不能因 false positive 把 session 卡死，這違反「主路徑優先」原則。
- **H2 攔 Write 的成本**：每個 Write 都要過一次 hook（regex 比對毫秒級）。可接受。
- **D3 歸檔不刪除**：保留 audit trail；未來若 trigger conditions 觸發（dev-rules §9b 列的 3 條），可再從 archive 翻出。
- **T2 session-cleanup 預設 dry-run**：避免「session 結束跑 cleanup → 不小心清掉下個 session 還要用的東西」。明確 `--apply` 才刪。
- **不引入新框架**（Bandit / Semgrep / Pre-commit-hooks new repo）：本次 audit 結論是現有框架夠用，缺的只是 harness 層。

## §6 執行紀錄（append-only，按完成順序）

- 2026-05-10 — **A1 完成**：清掉 `%TEMP%` 22 個 stray scratch（`pr*-msg`/`pr*-body`/`commit-out*`/`vibe-bat-*`/`vibe-git-*`/`_jsx_out*`/`_out.txt`/`pre-commit-final.yaml`）+ `/c/tmp` 9 個（`audit_*`/`bulk_annotate_tests`/`fix_encoding`/`probe.go`/`_backup.*`/`_msg.txt`/`test_violations`）+ 6 個 stale session-init markers (>24h)。0 secret hit。**未動 `Downloads/Fix-ClaudeDesktop.{bat,ps1}`**（user 個人第三方 utility，非 vibe）。
- 2026-05-10 — **H1 完成**：`scripts/session-guards/preflight_bash.py` PreToolUse hook 上線，攔 `sed -i` + 掛載路徑。註冊在 `.claude/settings.json` 的第二個 PreToolUse 陣列項目（不 override session-init hook）。17 個 test case + module load smoke = 18 tests 全綠。**End-to-end 驗證**：`echo '{...sed -i...}' | preflight_bash.py` exit 2 + clear remediation msg。
- 2026-05-10 — **H2 完成**：同支 hook 加 Write tool 分支，攔 `_*.bat`/`_*.ps1`/`_*.cmd` 寫到 `scripts/ops/`/`scripts/tools/`/`tools/` 之外。11 個 test case 涵蓋 whitelist + non-script / non-bat 例外。
- 2026-05-10 — **D1 完成**：`windows-mcp-playbook §修復層 D` 兩份 truncated 重複版本（lines 950-994 + 996-1031，自 commit 11206b9e 後就壞掉）合併為單一 slim 版（45 lines），含 Layer 1 A/B verifier、Layer 2 if-else 救火、Layer 3 長期解。
- 2026-05-10 — **D2 完成**：`dev-rules §11` 加改檔決策樹 + 四層防護總表（`preflight_bash.py` 列為 Prevent (harness) 第 1 層）。dev-rules.md 維持 499 lines，未撞 500 行 cap。
- 2026-05-10 — **D3 完成**：`git mv ssot-language-evaluation.md` + `ssot-migration-pilot-report.md` → `archive/`。同步更新 `CLAUDE.md` / `dev-rules.md §9b` / `dx-tooling-backlog.md` / `migrate_ssot_language.py` / `testing-playbook §LL §12a` 5 處引用路徑（CHANGELOG 歷史敘述保持原狀）。修補 archive 內檔案的相對路徑（`../internal/X` → `../X`，`../assets/Y` → `../../assets/Y`）。`doc-links-check` PASS。
- 2026-05-10 — **D4 完成**：`windows-mcp-playbook §已知陷阱速查` 加 reading guide callout（5 行）— 列出 codified 集合（#2-3, #14-15, #18, #27, #41, #43-46, #54-58, #60-62）告訴 AI 「掃 Active 集合就好」。**Anchor 完整保留**（外部 references 用 `#已知陷阱速查` / `#陷阱-NN` 都還能解析）。
- 2026-05-10 — **T1 完成**：`make help-escape` target 加在 `Makefile` 末尾，列出 5 個 Make 入口 + 4 個 Windows wrapper + 3 個 sandbox helper + 決策樹指引。輸出簡潔，搭配 `help` target 互補。
- 2026-05-10 — **T2 完成**：`scripts/session-guards/cleanup_scratch.py` 寫好。`make session-cleanup SCRATCH=1` 觸發；`DRY=1` 預覽。10 個 test case 全綠，含「<60min 不動」、「>24h session-marker stale」、「dry-run 不改檔」、「whitelist 不誤刪」。
- 2026-05-10 — **T3 取消（review premise wrong）**：`win_git_escape.bat` 寫到固定路徑 `%TEMP%\vibe-git-out.txt` 等 4 個檔，**每次 run 都 overwrite**（不累積）。原 audit 觀察的「stale 5 days」其實是「最後一次 run 的 snapshot」，不是歷史堆積。`cleanup_scratch.py` (T2) 的 sweep 已涵蓋此清理需求。

## §7 自我 review 與 follow-on

### 7.1 對照 §4 checklist

1. ✅ A1 — `find C:/Users/vencs/AppData/Local/Temp -name "pr*-msg.txt" -o -name "vibe-bat-*"` 0 hit；secret scan 0 hit
2. ✅ H1 — 18 test case (含 module smoke) 全綠；end-to-end harness fire 確認 exit 2 + clear msg
3. ✅ H1 — `.claude/settings.json` PreToolUse 陣列**新增 entry**（不 override session-init）
4. ✅ H2 — `scripts/ops/_local_helper.bat` 不被擋（whitelist 對）；`tools/portal/_internal.cmd` 不被擋；無 `_` 前綴 / 非 bat 例外通過
5. ✅ D1 — `grep -c "^### 修復層 D" docs/internal/windows-mcp-playbook.md` = 1
6. ✅ D2 — 決策樹放在規則描述後（§11 第 2 段），符合 audit plan 設計
7. ✅ D3 — `git mv` 而非 delete+add（status 顯示 `R  ssot-language-evaluation.md -> archive/...`）
8. ✅ D4 — `已知陷阱速查` anchor 完整保留；新增 reading guide 不影響 anchor
9. ✅ T1 — `make help-escape` 列出 13 個工具：win-commit / fuse-commit / fuse-locks / fuse-reset / recover-index / session-cleanup / win_git_escape.bat / win_gh.bat / win_async_exec.ps1 / win_read_fresh.ps1 / run_hooks_sandbox.sh / fuse_plumbing_commit.py / recover_index.sh
10. ✅ T2 — `python3 cleanup_scratch.py` (no flag) = dry-run，不誤刪
11. ✅ pre-commit `--files <changed>` 全綠；`--all-files` 兩個 pre-existing failure（playwright-lint 缺 eslint env / session-init.py --help 撞 cp950）與本次無關
12. ✅ user 7 條問題逐一答覆（見 §7.4）

### 7.2 二輪深 review — 上述計畫漏掉哪些

走完一輪後浮現的盲點：

1. **CLAUDE.md 沒寫 PreToolUse hook 的存在** — session 開始時 CLAUDE.md 是 tier 1 載入，新 session 不知道有 H1/H2 攔截，會困惑「我命令明明對為什麼被擋」。**Action**：CLAUDE.md §Agent 起手式段加一行「PreToolUse hooks: session-init + preflight (#11 + ad-hoc bat guard)」。
2. **harness hook timing 未測 multi-hook 互斥** — H1/H2 hook 與 session-init hook 都註冊在 `Bash|Write` 上，但 session-init 跑完寫 marker 才回，H1/H2 接著跑。預期是 sequential，但 Claude Code spec 沒明文，需要 dogfood 一次驗證。**Action**：FYI 即可，現實 case 已 dogfooded（本 session）。
3. **preflight_bash.py 對 MultiEdit 沒處理** — MultiEdit 可以一次改多個檔案，但若其中含 mount-path `_*.bat` 寫，現在不擋（只擋 Write）。風險：罕見場景，因 `_*.bat` 多半新增、走 Write。**Action**：暫不擴；列入 follow-on，需要時再加。
4. **`dev/null/` 目錄 untracked** — 當前 git status 顯示 `?? dev/`，內含 git-lfs 鉤子（`pre-push`/`post-checkout`/`post-commit`/`post-merge`），看起來是某 session 把 `2>/dev/null` 寫到 Windows path 結果建出 `dev\null\` 目錄被誤填。**Action**：surfacing 給 user，不主動清（pre-existing，不是本 session 寫的）。
5. **`win_git_escape.bat` 內 `pre-commit` 路徑寫死 `%LOCALAPPDATA%\Programs\Python` 系列** — 若 user Python 裝在不同位置會撞 trap #36。**Action**：已存在 trap #36 + #45；非本次 audit 範圍。
6. **`make help-escape` 沒被 Makefile drift hook 強制驗 13 個工具完整** — 未來新增 escape-hatch 工具忘記同步 help-escape 不會被擋。**Action**：可加 `check_help_escape_coverage.py` lint，但 ROI 邊際；列入 follow-on backlog。

### 7.3 額外發想（plan 沒寫但浮現的）

- **「session 結束自動跑 cleanup_scratch」**：能否註冊 SessionEnd hook 自動跑 `cleanup_scratch.py --apply --max-age-hours 1`？這比 user 手動 `make session-cleanup SCRATCH=1` 更省事。風險：current session 的 active scratch 被誤刪。緩衝：`_MIN_AGE_SECONDS = 60min` 的 floor 已經保護。**Decision**：列 follow-on，不在本 PR；要驗 SessionEnd hook 的 timing 是否真的在 session 終止才跑（vs ToolUse 之間）。
- **`vibe-sed-guard.sh` 與 `preflight_bash.py` 之間是否有重複？** — vibe-sed-guard 對人類 dev / docker exec 仍有效（shell function override），preflight_bash 對 Claude Code Bash tool 才生效。**互補不重疊**，OK。dev-rules §11 已說明分工。
- **「preflight_bash 失敗時靜默 allow」風險**：parse error → exit 0。若 hook 自身 broken，整個 #11 防護消失。緩衝：(a) 18 個 test case；(b) hook 程式 < 200 行非常單純；(c) 失敗策略文件化在 docstring。**OK**。

### 7.4 對照 user 起始 prompt 7 條 — 逐條回覆

1. **走標準路線檢查** — 結論寫在 §1.1：FUSE / MCP 部分**沒有業界 baseline**，本 repo 的做法（plumbing-commit / Windows escape hatch / ad-hoc whitelist hook）是必要創新；其他層面（pre-commit framework / 雙語 lint / dev container）都走標準。**沒有需要流程再造的地方**。
2. **避免為寫而寫** — D3 歸檔了 983 lines（兩份 SSOT 文件已 superseded）；D4 對 1031 行 trap table 加 reading guide 不刪除（保留 anchor）。dev-rules 線上 500 行 cap 已就位。
3. **AI 導覽品質** — D2 在 §11 加決策樹（4 路 fallback，比 freeform 思考省 token）；D1 dedupe 後 §修復層 D 有完整 Layer 1/2/3 + Q1 if-else；windows-mcp-playbook 既有的 Git 操作決策樹保留為範本。
4. **Dev container 主路徑、Windows 逃生門** — 思路保持完整；`make help-escape` (T1) 把 13 個逃生門工具集中顯示，session 卡住時更易發現。
5. **逃生門收編** — 驗收結論：**沒重新造輪子**（whitelist 已物理性 block）；user 觀察的「超多檔案」其實是 runtime scratch artifacts（已 A1 清完）。所有 escape-hatch 都在 `scripts/ops/` 下；無 secret 外洩；T2 的 `cleanup_scratch.py` 確保未來不再堆積。
6. **#11 比文字更好的管制做法** — H1 PreToolUse hook 是答案。harness 層 enforcement，不依賴 AI 「看到」規則；exit 2 + stderr 是 Claude Code spec 的「block + 教 model」機制；18 test 護身。
7. **先計畫再執行再 review** — 本檔即為計畫；§6 是執行紀錄；§7.1-7.3 是 review 與發想。

## §8 完成後的歸宿

- 抄 §7.4 結論進 `dev-rules` (§11 已含)；windows-mcp 段已含。
- §7.2.1（CLAUDE.md mention hooks）建議**在本 PR 內就做**，不拖。
- 本檔執行完轉 `docs/internal/archive/playbook-audit-2026-04.md` + frontmatter `status: archived`。但 v2.8.0 release 之前留在 active 位置，方便 maintainer 對照。
