---
title: "Changelog"
tags: [changelog, releases]
audience: [all]
version: v2.7.0
lang: zh
---
# Changelog

All notable changes to the **Dynamic Alerting Integrations** project will be documented in this file.

## [Unreleased]

<!-- Editorial guideline（v2.8.0, 建立於 2026-04-23, PR #50）：

本節為 v2.8.0 in-progress 工作暫存區；**entries 目標長度：每筆 3-6 行面向
使用者的重點 + 一行 `詳見 planning §N` / `commit <sha>` 指回內部 artifacts**。
不要在此處記錄 session 過程、FUSE trap 實測、Cowork day-by-day、完整 commit
list、每個 hook 名單等——該類內容屬於：

  - docs/internal/v2.8.0-planning.md §12 Session Ledger / Live Tracker
  - docs/internal/v2.8.0-planning-archive.md RCA sections
  - commit messages / PR discussion

Phase .e E-5 會做最終 condensation + 切正式 `## [v2.8.0]` heading；但若每筆
bundle entry 都 ~30 行敘事，E-5 會變成重寫而非潤飾。請自律。

Compare：v2.7.0 最終條目約 55 行（Scale / Token / Test / Benchmark / ADR /
Breaking / Upgrade 七塊清楚區分），那是目標形狀。
-->

### Added

- **Phase .a commit-msg enforcement bundle（v2.8.0, Issue #53）**
  - **`pr_preflight.py --check-commit-msg` body/footer validation**：加 `validate_commit_msg_body()` helper，每個 post-header 非註解行 > 100 chars → ERROR；缺 blank-line-after-header → WARN。本地 commit-msg hook（PR #44 C2）本來只驗 header，CI commitlint 多驗 `footer-max-line-length ≤ 100`，PR #51 / PR #54 踩到「local 過 CI 擋」走 force-push-with-lease 修的情境至此消除
  - **`make commit-bypass-hh ARGS="-F _msg.txt" [EXTRA_SKIP=...]`**：codified narrow bypass — 只跳 `head-blob-hygiene`（FUSE Trap #57 的唯一合法 bypass case），commit-msg hook + 其他 pre-commit hook 仍跑。替代 sledgehammer `git commit --no-verify`
  - **testing-playbook §v2.8.0 LL #3 更新**：regulation layer → enforcement layer；新規則「FUSE Trap #57 繞道一律 `make commit-bypass-hh`」
  - **`tests/dx/test_preflight_msg_validator.py` 20 → 29 tests**：9 條新 body/footer 驗證（long line ERROR / 恰 100 chars 邊界 / 缺 blank-line-after-header WARN / comment 行不計 / 自訂 max_length / empty msg / CLI 端 rejection / CLI 端 warnings-only 仍 pass）
  - Dogfood：本 PR 的所有 commit 走 `make commit-bypass-hh`，commit-msg validator 自己驗自己。closes Issue #53

- **Phase .a Scanner correctness + test harness bundle（v2.8.0, A-10 fix + A-8b + A-8d + LL ext）**
  - **A-10 product fix — WatchLoop hierarchical scan awareness**（`components/threshold-exporter/app/config.go` WatchLoop block, [Issue #52](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/52)）：root cause — WatchLoop 在 hierarchical mode 下仍用 flat-only `scanDirFileHashes`（`os.ReadDir(dir)` + `IsDir()` skip），看不到 `conf.d/<domain>/<region>/tenant.yaml` 等 nested tenant file 的變動，所以 file 改動**永遠不觸發 reload**；測試靠 `os.Chtimes(now-5s)` 詭技偶然湊效率才 pass。改為在 hierarchical mode 走 `scanDirHierarchical`（recursive）+ per-file hash diff。`TestWatchLoop_DebouncedReload_DetectsFileChange` 從 Chtimes 版本改為直接寫檔觸發，dev container `-race -count=30` → **30/30 PASS**（修前 1/30 flake）
  - **A-8b `TestScanDirHierarchical_K8sSymlinkLayout`**（planning §12.2）：K8s ConfigMap mount pattern invariant lock — file-symlinks ARE followed (`os.ReadFile` resolves)、dir-symlinks NOT recursed（`filepath.WalkDir` lstat semantics），防止未來 Go stdlib 升級悄悄 regress
  - **A-8d `TestScanDirHierarchical_MixedValidInvalid` + 新 metric `da_config_parse_failure_total{file_basename}`**：poison-pill chaos — malformed YAML 不污染 sibling 正常 file 的發現 / 掃描；broken file 仍進 hash 表（change detection 可感知 recovery）；**新 Counter** 提供「tenant 檔持續損壞」的 ops observability signal（Gemini R3 #3 原提案，per-file error-skip 邏輯本就存在，這批純加 metric exposure + behavior lock）
  - **Testing-playbook §v2.8.0 LL #3 extension** — 本地 commit-msg hook（PR #44 C2）**只驗 header**，不驗 body/footer；CI commitlint 多驗 `footer-max-line-length` 等 body 規則。PR #51 self-review commit 本地過、CI 擋（long pytest path 被當 footer），force-push 修。暫時 mitigation + 長期 enforcement 併入 Issue #53
  - A-8c 已於 PR #51 merge；A-8 family 至此 b/c/d 三件齊。僅 A-8 Golden Parity `hypothesis` 擴充（基礎已在 codebase）還可後續做

- **Phase .a Dev Container enablement bundle（v2.8.0, PR #51 接手: Trap #62 + A-12(v) + A-8c + testing LL）**
  - **Trap #62** `windows-mcp-playbook.md` — dev container mount scope drift workaround（cp-test-revert workflow for editing claude worktree files that need to run in container）
  - **A-12(v) `scripts/session-guards/git_check_lock.sh` hardening** — self-PID + name filter（Trap #58 long-term fix）+ `.git/HEAD` NUL-byte corruption auto-repair（Trap #59 long-term fix）+ dedicated `--check-head` subcommand + 14 pytest cases
  - **A-8c `TestConfigManager_DeletedTenantCleanup`**（`config_hierarchy_integration_test.go`）— behavior-lock test for the delete path's atomic-swap; verifies all 4 per-tenant maps + `inheritanceGraph.TenantDefaults` clear the deleted tenant in one swap, no collateral damage to siblings, no goroutine leak. `-race -count=30` clean in Dev Container
  - **testing-playbook.md §v2.8.0 LL** — codify 3 patterns 從 PR #49/#50/#52 踩到的: subprocess CLI test 不計 coverage / 本地輸出截斷要二次驗證 / `--no-verify` 僅跳 FUSE Trap #57 不跳 commit-msg
  - **Version consistency drift fix**（承接 PR #51 中繼 commit）：`design-system-guide.md` front-matter 回 v2.7.0、`windows-mcp-playbook.md` Trap #58 body `v2.8.0-planning` 包 backtick 避開 `bump_docs.py` regex
  - A-10 product race 留 [Issue #52](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/52)（另 PR 處理 `config_debounce.go` 的 atomic-swap empty window）；`--no-verify` 長期 enforcement 留 [Issue #53](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/53)
  - 詳見 `v2.8.0-planning-archive.md §S#21`

- **Phase .a SSOT drift cleanup bundle（v2.8.0, A-2 + docs/design-system-guide 殘留 refs + §12.4 traps codify）**：延續 PR #49 的 bundle 做法，把 Phase .a 軌道一剩下的純文件收尾一次處理。三項同屬「code/canonical state 已往前走，但 authoritative docs 沒跟上」的 SSOT drift 類型，彼此零耦合但 theme 一致：
  - **A-2 — `docs/interactive/changelog.html` 補 v2.1-v2.6 時間線（REG-003 resolved）**：原檔只有 v2.0.0 + v1.10-v1.13 五張卡片，缺 v2.1 ~ v2.6 共 6 個 minor release。手工新增 6 張 `version-entry` card（對應 `CHANGELOG.md` L377-895 各版 release notes 的 highlight 摘要 + date + hl-tag + stats-bar），v2.6.0 標記 `badge-current CURRENT`，v2.0.0 移除過時的 CURRENT badge。**REG-003** 登記於 v2.7.0 CHANGELOG L172（「REG-003 changelog.html 修復延至 v2.8.0」）至此 resolved；`known-regressions.md` 本身已於 Session #16 per radical-delete policy phantom-deleted（registry 與 main 代碼一致即可不寫 retro-narrative），故 REG-003 resolution 不再回寫 registry row，僅記於本條。
  - **`docs/internal/design-system-guide.md` 殘留 stale token refs 更新**：PR #34 Token Audit 完成後 guide 未同步——(i) §3.1 整段 `primary/secondary` 家族描述改寫為 `accent/hero-*/tile-*` canonical namespace（對應 PR #34 → PR#1c 的 token-split），附 token 歷史導覽段；(ii) §3.3 Icon 色表 6 row 全部更新（validation/cli/rules/wizard/dashboard/chart 的 light+dark 值與 `design-tokens.css` SSOT 對齊，如 icon-cli `#2563eb` → `#f59e0b` 是語意改色不是值 drift）+ 附註 `*-bg` variant 配套使用規則；(iii) §3.4 `phase-*` → `journey-*` token 整段 rename（token 名 + 值 + dark mode 欄同步）；(iv) §3.5 mode-silent/mode-maintenance 兩 row 色值對齊；(v) 3 處內文 `--da-color-primary` 引用全改 `--da-color-accent`（§Category table / focus-visible CSS / legacy alias example）；(vi) L26 次要文字 `#64748b` → `#475569`（TECH-DEBT-007 post-fix canonical 值）；(vii) front-matter `version: v2.7.0 → v2.8.0`（內容已反映 v2.8.0 canonical token 狀態）。
  - **`docs/internal/windows-mcp-playbook.md` 新增 trap #58-#61**（codify `v2.8.0-planning.md §12.4` 中仍 open 的環境 trap）：#58 `make git-preflight` 把自身 bash 當活 git 程序誤判（self-PID filter missing；手動繞道 + 長期解 A-12 子項 v）；#59 `.git/HEAD` 被 NUL byte 填到 57 bytes 使 `git rev-parse HEAD` fatal（FUSE write-cache loss 的變體，附 `printf 'ref: refs/heads/<br>\n' > .git/HEAD` 直接 rewrite）；#60 `generate_doc_map.py` 長 I/O regen 在 FUSE 被 fsync 中斷 → HEAD corruption + 全檔假 "new file"（`make recover-index` 救急 + 長期 `--safe` atomic rename 提案）；#61 PowerShell `Out-File -Encoding utf8` / `Set-Content -Encoding utf8` **強制加 BOM** 使 commit message 首字被污染、commitlint 連環 fail（與 pitfall #32 JSON BOM 區別，這條針對 commit-msg；正解 `[IO.File]::WriteAllText($p, $m, [Text.UTF8Encoding]::new($false))` + filter-branch batch de-BOM SOP）。
  - **`docs/internal/{testing,benchmark,windows-mcp,github-release}-playbook.md` `verified-at-version: v2.7.0 → v2.8.0`**：dogfood PR #49 A-6 新工具 `bump_playbook_versions.py --to v2.8.0` 4 檔一次 bump 成功（驗證工具正確性 + 對齊本批 trap 新增後的複查狀態）。

- **Phase .a 軌道一 DX polish bundle（v2.8.0, A-6 + A-9 + A-11）**：Phase .a 軌道一三個散落的小額收尾合併成單一 PR。三項彼此獨立、都純 Python + markdown，無 Go / Playwright runtime 依賴；合併成 bundle 是因為 scope 接近（都是「把既有手動流程或設計契約 codify 為工具 / 章節」），拆成三個 PR 只會稀釋 review 訊號。
  - **A-6 `scripts/tools/dx/bump_playbook_versions.py`**：tag 切版時 bump 4 份 operational playbook（`testing-playbook.md` / `benchmark-playbook.md` / `windows-mcp-playbook.md` / `github-release-playbook.md`）的 `verified-at-version:` front-matter 欄位。刻意**不**合併進 `bump_docs.py` — 後者是**元件版號**（platform/exporter/tools/tenant-api）的 SSOT，playbook 的 `verified-at-version` 是**人工複查戳記**，語意不同。CLI 支援 `--to vX.Y.Z` / `--check`（CI 偵測落後）/ `--dry-run`（印 diff 不寫）。Byte-faithful：用 `read_bytes` + `write_bytes` 避免 `read_text` 預設的 universal-newline translation 吃掉 CRLF（test harness 實測出這個 bug）。ASCII-only 輸出（pitfall #45 精神延伸，`->` 而非 `→`）。`dev-rules.md` 的 `verified-at-version` 刻意**不**列入 scope，維持與 `check_playbook_freshness.py::PLAYBOOK_PATHS` 同步。**19 tests** (`tests/dx/test_bump_playbook_versions.py`) 覆蓋 UPDATED / OK / MISSING 三狀態 + `--check` / `--dry-run` 模式 + LF/CRLF 行尾保留 + idempotent + 其他 front-matter 欄位不動 + 版號格式驗證。
  - **A-9 `scripts/tools/lint/check_path_metadata_consistency.py`**：實作 `docs/schemas/tenant-config.schema.json::definitions.metadata.$comment` 的設計契約（ADR-017）— 當 conf.d/ 目錄階層（`domain/region/env/`）與 tenant `_metadata.{domain,region,environment}` 不一致時發出**警告但不阻擋**（always exit 0），schema 已允許 override，此工具只負責 surface drift。保守啟發式：只對第一層目錄映射 `domain`；只對允許清單 `{prod, production, staging, stage, dev, development, test, qa}` 內的 segment 映射 `environment`，不夠把握的 case 完全不警告（避免 fixture / 非標準命名的 false positive）。`_*.yaml` 檔案跳過（defaults/policies/profiles 不是 tenant 檔）。註冊進 `scripts/tools/validate_all.py::TOOLS` 並新增 `.pre-commit-config.yaml` manual-stage hook `path-metadata-consistency-check`（scope `^components/threshold-exporter/config/conf\.d/.*\.yaml$`）。CI 模式輸出單行 `<file>:0: warning: ...` 格式便利 GH Actions annotations / `grep`。**16 tests** (`tests/lint/test_check_path_metadata_consistency.py`) 覆蓋 path inference / environment mismatch / domain mismatch / 大小寫不敏感 / `_*.yaml` 跳過 / malformed YAML 不 crash / CI 模式格式 / missing config dir soft fail。現有 repo 乾淨（0 mismatch）。
  - **A-11 `docs/internal/github-release-playbook.md §PR CI 診斷流程`**：新章節，專治 Cowork VM proxy 封 `api.github.com` + Desktop Commander shell 找不到 `gh` 時，從 Windows 側走 `curl.exe` + REST 排 PR check 失敗的標準路徑。核心內容為四段 URL ladder（`/pulls/{n}` → `/commits/{sha}/check-runs` → `/actions/runs/{id}/jobs` → `/check-runs/{id}/annotations`），特別點出 `/actions/jobs/{id}/logs` 常回 403，改打 annotations endpoint 拿 human-readable 錯誤訊息。環境層陷阱（PowerShell BOM / `Invoke-RestMethod` timeout / `gh pr checks` JSON 沒 `conclusion` 欄位 / stacked PR DIRTY state 靜默跳過 CI）**不複製** 回本章節，改以 table 交叉引用 `windows-mcp-playbook.md` pitfall #28 / #31 / #32 / #50 / #56；doc-as-code 原則避免雙處 SSOT drift。附 PowerShell 最小可用診斷 snippet（`curl.exe -H Authorization: Bearer` + `ConvertFrom-Json` + `Where-Object`）。Quick Action Index 新增一列指向本章。

- **Pitfall #45 byte-level enforcement（v2.8.0 Phase .d, PR #45, branch `feat/v280-bat-ascii-purity`）**：把 pitfall #45（Desktop Commander `start_process` 執行 `.bat` 時編碼損壞）從「人工紀律 + playbook 備註」升級為 CI-gated 雙層防線。Dogfood PR #44 的 session resilience 工具鏈時，重新解 root-cause 發現真正的機制是 cmd.exe batch parser 對繼承的 OEM codepage（cp950 / cp437，**不是** cp65001）做 byte-level 讀取，任何 ≥0x80 byte 都可能落在 parser 的 shell metachar 範圍（0x80–0xBF 含 cp1252 標點 continuation byte），**後續幾行**才出現 `@echo off` / `setlocal` / `goto` 「不存在」的假象。`cmd /c` 不救（子 cmd 仍繼承父 codepage），`chcp 65001` 不救（preamble 已用錯 codepage 讀完）。PowerShell 呼 `.bat` 不撞這條路徑是因為 PS runtime 先把 command line decode 成 UTF-16 再交給 cmd。
  - **`tests/dx/test_bat_label_integrity.py` 從 7 → 16 條**：新增 `ALL_OPS_BAT_FILES` 覆蓋所有 `scripts/ops/*.bat`（原本 label integrity 只測 `win_git_escape.bat` / `win_gh.bat`，`dx-run.bat` 漏網）；3 條新 parametrized assertion — `test_bat_files_are_ascii_pure`（每個 byte < 0x80，違規時 print L:col + byte + UTF-8 decoded preview）/ `test_bat_files_are_crlf`（沒有 bare LF）/ `test_bat_files_have_no_utf8_bom`（檔頭不得 `EF BB BF`）。helper `_find_non_ascii(bytes) -> list[tuple[int, int, int, str]]` 回報前 10 筆違規供 pytest 輸出。
  - **`scripts/tools/lint/check_bat_ascii_purity.py` pre-commit L1 hook**（`bat-ascii-purity-check`）：scope 限定 `scripts/ops/*.bat`（其他 `.bat` 如 dev-container bind-mount script 不走 Desktop Commander start_process 路徑，不受 pitfall #45 管），pre-commit 透過 `files: ^scripts/ops/.*\.bat$` 把關。fail 輸出 L:col:byte + UTF-8 decoded preview（前 5 筆）+ 三條修法（替換 CJK / em-dash → `--` / 重存 CRLF / 去 BOM）+ byte-level 根因說明 + playbook pitfall #45 連結。argparse 接受 `paths` 位置參數（pre-commit 傳 staged files），empty 時 fallback 掃全目錄。
  - **ASCII-ify 3 個 `scripts/ops/*.bat` 倖存者**：`dx-run.bat`（`#核心原則` → `"Core Principle" section`）、`win_gh.bat`（em-dash + `§MCP Shell Pitfalls` / `§修復層 C` 4 處）、`win_git_escape.bat`（`§MCP Shell Pitfalls` / `§FUSE Phantom Lock 防治` + em-dash 2 處）。全部保留原 CRLF（驗證：`0d 0a` 出現 23 / 187 / 406 次，無 bare LF）。
  - **`docs/internal/windows-mcp-playbook.md` 兩處更新**：
    - Pitfall #45 row 擴充為 byte-level 根因（OEM codepage 繼承、byte-oriented parser、0x80–0xBF metachar 碰撞、parser 狀態機破壞、為何 `cmd /c` / `chcp 65001` 不救、為何 PowerShell 能過）+ 三條鐵律 + CI gate 引用。
    - §MCP Shell Pitfalls 表第 3 列標注 **CI-gated ✅** + 列出具體 hook / pytest 名稱；章節末段說明「encoding / CRLF / BOM 現已由 pre-commit + pytest 雙層攔截，8.3 short path 與 em-dash 引號仍人工紀律」。
  - **Dogfood chain 完整**：本 PR 在 FUSE phantom lock + stuck stale index 狀態下執行，走 Windows 側 `_phantom_unlock.ps1` → `_branch_create.ps1` → `_cleanup.ps1` plumbing escape hatch 配合 Desktop Commander `start_process`，RED（3 新 test 紅）→ surgical edit → GREEN（16 條全綠 + 合成的 bad .bat 被 hook 拒），完整驗證 PR #44 的逃生門工具鏈。

- **Session resilience + token-economy bundle（v2.8.0 Phase .c, PR #44, branch `feat/v280-session-resilience-bundle`, 8 commits: C1–C8）**：解決 Cowork FUSE mount 下反覆踩到的兩類 showstopper——(a) `.git/index.lock` / `.git/HEAD.lock` 幻影鎖讓所有 `git add` / `commit` / `update-ref` 直接 fail、(b) `.git/index` 被寫壞後 `git status` 以下全部不可用。同步把 commit-msg 驗證從 CI-only 搬到本地、把 pre-push marker gate 做成 PR-state 感知，整組落成「code-first 逃生門」：
  - **C1 `.commitlintrc.yaml` 擴展 enum**：`type-enum` 加 `chore` / `revert`，`scope-enum` 加 `config` / `resilience` 對應 PR #44 本身的類別。既有 Conventional Commits 家族不變。
  - **C2 `scripts/hooks/commit-msg` + `scripts/tools/dx/pr_preflight.py --check-commit-msg` / `--check-pr-title`**：把 commitlint 檢查本地化。`commit-msg` hook 由 session-init 自動安裝進 `.git/hooks/`（見 C6）；`pr_preflight` 新增兩個離線子命令：
    - `--check-commit-msg <file>`：讀 commit msg file → 解析 header → 對 `.commitlintrc.yaml` 的 `type-enum` / `scope-enum` / 長度上限驗證。fail 時列明違規項 + 修正建議。
    - `--check-pr-title <string>`：同樣的驗證邏輯，但輸入是 PR title。CI 端用來擋 PR title drift（跟 commit header 不同步的經典坑）。
    - `_read_commitlint_enum()` 不依賴 PyYAML，block-style flow 手解，對應 repo 現行 YAML 格式。
    - **新測試 `tests/dx/test_preflight_msg_validator.py`** 覆蓋合法/違法 type/scope/長度/空白字元/CRLF 結尾 etc.
  - **C3 `scripts/ops/fuse_plumbing_commit.py` + `make fuse-commit` / `make fuse-locks`**：幻影鎖場景下的 commit 逃生門。當 `.git/index.lock` 以 EPERM 狀態存在（`ls` 看得到、`rm` 失敗、`git` 拒絕 create own lock）時，走 git plumbing：`hash-object -w <file>` → 建 `GIT_INDEX_FILE=/tmp/plumb_idx_...` 的 temp index → `update-index --add --cacheinfo` → `write-tree` → `commit-tree` → 直接 write `.git/refs/heads/<branch>`。完全跳過 `.git/index` + `.git/index.lock` 的 handshake。三種 mode：
    - `--auto --msg msg.txt file1 file2` — 偵測幻影鎖 → 有則 plumbing、無則 normal path（hooks 有跑）
    - `--force-plumbing` — 永遠走 plumbing（skip hooks；quality gate 另外由 `make pr-preflight` 把關）
    - `--show-locks` — 列出偵測到的 phantom lock paths（診斷用）
    - `--amend`、exit codes 0/1/2 語意、保留 exec bit、best-effort `.git/index` 同步
    - **新測試 `tests/dx/test_fuse_plumbing_commit.py`** 覆蓋 detect / plumbing path / normal path / amend / ref 寫失敗回報 / exec bit 保留
  - **C4 `scripts/ops/recover_index.sh` + `make recover-index`**：`.git/index` 被寫壞（`index file corrupt` / `index uses ???? extension, which we do not understand` / `index file smaller than expected` / `bad index file signature` / `bad index file sha1 signature`）時的重建路徑。從 HEAD 走 `GIT_INDEX_FILE=$TMP_IDX git read-tree HEAD` 建 temp index，cp 到 `$INDEX.recover.$$` 同路徑 staging → `mv` 覆蓋 `.git/index`（rename(2) 同 FS atomic）。`--check` 模式只診斷（exit 0=clean / 2=corrupt）、預設模式診斷+修復。
    - **新測試 `tests/dx/test_recover_index.py`** 覆蓋 clean / 各類 corruption signature / `--check` 模式 / rebuild success / rebuild fail 路徑
  - **C5 `scripts/ops/win_git_escape.bat` `:done` / `:done_err` label fix + cmd-redirect pattern 文件化 + 三項 review polish**：
    - **Critical bug fix**：`win_git_escape.bat` 所有 `:do_*` handler 都 `goto :done` 或 `goto :done_err`，但這兩個 label **整個檔案都沒定義**（`:usage` 之後直接 EOF，最後一行甚至 truncate 成無換行的 `echo   `）。cmd.exe 對「goto 不存在的 label」採靜默 errorlevel=1，所以**每次成功命令都回 rc=1**，caller 永遠看到 `FAILED`。補回兩個 label（`popd` + `endlocal` + `exit /b 0/1`）、補齊 truncate 的 `:usage`、保正確 CRLF + EOF 換行。
    - **MCP PowerShell cmd-redirect pattern 文件化**：兩支 `.bat` header 加上經過 dogfood 驗證的呼叫範例。三件套：`CreateNoWindow=$true`（斷開 MCP console handle 繼承，**非這個 MCP 還是會 hang**）、`cmd.exe /s /c "..."`（`/s` 讓 cmd 乾淨地剝掉外層引號，**不是 `/c """"..."""` 那套**——實測後者會在某些 PS 引號路徑上變成 exit=0 / 0 bytes 的假通過）、`WaitForExit(ms)`（給 MCP 一個 process handle 等待，而不是讓它持有開著的 pipe）。
    - **S1 `session-init.py` `_install_commit_msg_hook` install/update 指示修正**：舊邏輯 `return "installed" if not dst.exists() else "updated"` 跑在 `dst.write_bytes()` **之後**，`dst` 永遠 exists，所以永遠回 "updated"，telemetry 的「初次安裝」事件被整個遮蔽。改為 `write_bytes` 前先 capture `existed_before = dst.exists()`。
    - **S6 `recover_index.sh` 注釋錯誤 + non-atomic write 修正**：舊注釋說 `cp (not mv) ... for atomic write behavior` — **裸 cp onto .git/index 不是 atomic**（讀者可能看到寫到一半的檔案）。改走 cp 到同 FS 的 sibling `$INDEX.recover.$$` → mv（rename(2) atomic），注釋同步更正。
    - **S7 `require_preflight_pass.sh` 注釋錯誤修正**：舊注釋說 `gh pr view with --head filter`——但指令其實是 `gh pr view <branch>`（沒用 `--head`，branch 自動對 head 分支）。改注釋，行為不變。
    - **新測試 `tests/dx/test_bat_label_integrity.py`** 7 條 parametrized assertion：每個 `goto :X` 必須有對應 `:X` label（擋 C5 bug class）、`:done` / `:done_err` 都必須存在（exit-handling contract）、header 必須含 `Process.Start` + `WaitForExit` + `CreateNoWindow` + `/s /c`（MCP caller pattern 可發現性）。
  - **C6 `scripts/session-guards/session-init.py` auto-heal git hooks**：PreToolUse hook 每次起手式時：
    - `_heal_pre_commit_shebang()` — 偵測 `.git/hooks/pre-commit` 的 shebang 指向不存在的 interpreter（典型 Windows `pre-commit install` 寫 `#!C:\Python*\python.exe` 路徑到 FUSE Linux 側不可用）→ 自動改為 `#!/usr/bin/env python3`。
    - `_install_commit_msg_hook()` — 把 `scripts/hooks/commit-msg`（C2）copy 進 `.git/hooks/commit-msg`、chmod 0o755、內容相同時 no-op、status 送進 telemetry。
    - Telemetry 新增 `hook_status: {pre_commit_shebang, commit_msg}` 欄位，和既有 session-init telemetry 合併寫 JSON Lines。所有 heal 失敗**絕不 block** session 起手式（只進 telemetry）。
  - **C7 `scripts/ops/require_preflight_pass.sh` pre-push marker 條件性啟動**：舊版任何 push 都要 `.git/.preflight-ok.<HEAD-sha>` marker，WIP iteration 階段（PR 還沒開）每次 push-to-save 都被擋、`make pr-preflight` 要跑 3-5 分鐘，是長期摩擦源。改為 state-aware：
    - `GIT_PREFLIGHT_STRICT=1` → 永遠要 marker（舊行為保留成 opt-in）
    - `gh` 不可用 → 要 marker（安全 fallback）
    - `gh pr view <branch> --json state --jq '.state'` 回 `OPEN` → 要 marker（PR 已開，CI 可見性 + reviewer noise 成本已實化）
    - `gh` 可用但無 OPEN PR → 允許 push（WIP 階段，作者自付成本）
    - **新測試 `tests/dx/test_preflight_pass_gate.py`** 15 條 parametrized 覆蓋 STRICT / 各 PR state / gh 可用與否 / multi-branch push / orthogonal bypass + main protection。特別做了 `_make_gh_missing_path(tmp_path)` helper：symlink bash/git/basename/sh/cat 到 clean dir，可靠地模擬「`gh` 不在 PATH」的 fallback 路徑。
    - **`tests/dx/test_preflight_marker.py` 既有 `blocks_*` 案例補 `env_extra={"GIT_PREFLIGHT_STRICT": "1"}`**：pin 到舊「永遠要 marker」契約，不受測試機 `gh` 可用性影響。
  - **C8 `pr_preflight.py` / `check_pr_scope_drift.py` 對非 UTF-8 git stderr 容錯**：兩個 orchestrator 的 `run()` helper 原本是 `subprocess.run(..., text=True)`（預設 UTF-8 decode）。Windows 側 git 的本地化 progress 輸出可能含 cp1252 smart-quote（0x93 / 0x94 / 0x96）等非 UTF-8 位元組，**一顆就整條 preflight 崩潰**（`UnicodeDecodeError: can't decode byte 0x93 in position 18`），連帶破壞 pre-push marker 寫入。改傳 `encoding="utf-8", errors="replace"`，stderr 本就只用於人看 / grep signature，replacement char 無害。C8 的修正 dogfood 實證：跑 `make pr-preflight` 不再被 git fetch 的 localized progress line 咬死。

- **`scripts/hooks/commit-msg`** — Conventional Commits header 本地化檢查器（installed 進 `.git/hooks/` by session-init C6）。defensive 處理：repo root 靠 `git rev-parse --show-toplevel` 解析不依賴 cwd、找不到 `pr_preflight.py` 不 block、python interpreter 多候選 PATH resolve（`python3` / `python` / 絕對路徑 fallback）。
- **`make` targets**：`fuse-commit MSG=msg.txt FILES="a b"` / `fuse-locks` / `recover-index`（對應 C3 / C3 / C4）。

### Changed

- **`CLAUDE.md` Makefile Top 7 擴充說明**：`make win-commit` 行補充 `+ fuse-commit / recover-index` 指向 PR #44 的 FUSE 逃生門工具鏈。
- **`docs/internal/windows-mcp-playbook.md`** 新增 §FUSE Phantom Lock 防治 + §修復層 C 補 CreateNoWindow/`/s /c` 的實測 pattern（見下方 Fixed）。

- **session-init telemetry + `--stats` CLI（v2.8.0 Phase .b, PR feat/v280-session-init-telemetry）**：PR #42 事後稽核發現 — PreToolUse hook 已上線，但缺「hook 真的有跑嗎」的觀測路徑；只能靠使用者手動 `--status` 看單一 session marker，跨 session 趨勢（幾次 init / 幾次 noop / vscode_toggle 失敗率 / avg duration）完全不可見。本次把 telemetry 內建進 hook 本身：
  - **每次 hook 呼叫 append 一筆 JSON Lines**（event=`init`/`noop`/`force`；`--status` / `--stats` 是 query，刻意不寫 log 避免自我污染）。欄位：`ts` / `session_id` / `marker_digest` / `event` / `duration_ms` / `vscode_toggle`（`ok`/`partial`/`skipped`）/ `vscode_msg` / `marker_path` / `repo_root` / `pid` / `argv`
  - **Log path cross-platform 解析（4 層優先序）**：`VIBE_SESSION_LOG` env override → Windows `%LOCALAPPDATA%\vibe\session-init.log` → POSIX `$XDG_CACHE_HOME/vibe/session-init.log` → home fallback（`~/.cache/vibe/` 或 `~/AppData/Local/vibe/`）。邏輯抽成 pure `_resolve_log_path(os_name, env, home)` 可直接 unit-test，不需 monkey-patch `os.name`（後者會撞到 pathlib `WindowsPath` 無法在 Linux 實例化的 INTERNALERROR）
  - **`VIBE_SESSION_LOG=/dev/null` / `NUL` 可完全停用**（CI / 使用者 opt-out）；`_is_disabled_log_path` 提早 return、連 `mkdir` 都不跑
  - **Log 寫入失敗絕不 block**：所有 OSError 收攏、僅 stderr 印 warning、exit 0 維持不變。遵循 PreToolUse hook 的既有 never-block 原則
  - **UTF-8 safety**：`json.dumps(ensure_ascii=False)` — CJK session id / vscode_git_toggle 中文訊息原樣落盤（不 escape 成 `\uXXXX`），`jq` / 肉眼 grep 都直接可讀。dogfood 實測 vscode_git_toggle 的「✅ VS Code Git 已關閉」訊息 round-trip 乾淨
  - **`--stats` subcommand**：印 log path / size / total events / `init=N noop=N force=N` / sessions tracked / `vscode_toggle: ok=N partial=N skipped=N` / avg init duration / last N events 摘要。支援 `--limit N`（預設 10）/ `--json`（輸出原始 JSON Lines 供 `jq` pipe）/ `--session <SID>`（過濾單一 session）。Malformed log lines 自動 skip（例如寫到一半被 SIGKILL 的歪斜 line），不讓統計掛點
  - **21 新測試**（tests/dx/test_session_init.py：13 → 34）：
    - `TestTelemetryLog` × 11：env override / XDG / LOCALAPPDATA / home fallback / override-wins-on-nt-and-posix（pure function 測試，不碰 `os.name`）/ init/noop/force 事件 / partial toggle / `--status` 不寫 log / 寫入失敗 never block / `/dev/null` 停用 / CJK round-trip
    - `TestStatsCLI` × 7：empty log / summarize / `--json` mode / `--session` filter / `--limit N` / 歪斜 line skip / `--stats` 不自污染
  - **End-to-end dogfood** 已跑過 4 次 hook 呼叫（1× force + 2× noop + 1× new session init）+ `--stats --session` / `--stats --json` pipe 驗證全綠
  - **CLAUDE.md / vibe-workflow skill 同步更新**：把 `--stats` 加進手動觸發指令集、標注 log 位置與停用方式

- **Windows MCP 側 ad-hoc script 防治（v2.8.0 Phase .a, PR #41）**：延續 PR #39/#40 的 code-driven 精神，把「不要寫 throw-away `_commit.ps1` / `_pr.bat`」從文字規範升級為 L1 pre-commit hook：
  - **`scripts/tools/lint/check_ad_hoc_git_scripts.py`**（pre-commit 硬失敗，whitelist 模式）：掃 repo 內所有 `*.bat` / `*.ps1` / `*.cmd`，不在 `scripts/ops/` / `scripts/tools/` / `tools/` allowlist 中即 fail。用 whitelist 而非 blacklist regex 的理由：PR #40 session 寫了 `_p40_commit.ps1` / `_p40_pr.bat` / `_p40_checks.bat` / `_p40_failog.bat` / `_p40_diag.bat` 五隻 script，黑名單追不上每個新動詞（check / failog / diag），whitelist 強制所有新 wrapper 走 PR review。
  - **`scripts/ops/win_gh.bat`**（v2.8.0 新增）：GitHub CLI 的 MCP-friendly wrapper。Desktop Commander PowerShell 下 `"C:\Program Files\GitHub CLI\gh.exe"` 的引號會被多層 escape 破壞；`win_gh.bat` 改用 8.3 short path `C:\PROGRA~1\GITHUB~1\gh.exe`、強制 `PATHEXT` + `PATH` 含 `Git\cmd`、全 ASCII 註解、CRLF line endings。子命令：`pr-checks [PR#]` / `pr-view [PR#]` / `pr-create <flags>` / `run-view <RUN_ID>` / `run-log <RUN_ID>` / `raw <args>`（逃生門）。取代 session 每次自己寫 `_pr_checks.bat` 的循環。
  - **`docs/internal/windows-mcp-playbook.md §修復層 C` 重寫**：逃生門工具表新增 `win_gh.bat`；opening 改為「⛔⛔⛔ 鐵則」並 chronicle PR #39 / #40 的 1 + 5 = 6 支 ad-hoc script。
  - **`docs/internal/windows-mcp-playbook.md` 新增 §MCP Shell Pitfalls 節**：編寫 `.bat` / `.ps1` wrapper 必讀的 4 雷清單（Short path / CRLF / ASCII-only / `PATHEXT`+`PATH` 雙設）+ 起手式模板 + 3-step 自測 one-liner。
  - **新增 LL #54 + #55 + #56**：#54 chronicle PR #39 / #40 ad-hoc script proliferation；#55 記錄 `win_gh.bat` 初次實作踩到的 short-path / CRLF / PATH 三件套；**#56** 記錄 PR #41 dogfood 本身觸發的兩個二次踩坑——(a) `win_gh.bat` / `win_git_escape.bat` 實作時忘了 `set PATHEXT`，撞到使用者 profile 的 `PATHEXT=.CPL` 直接 break gh 內部 git 呼叫；(b) PR #41 base 堆在 PR #40 還未 squash-merge 的分支上，main squash 後 PR #41 進入 `mergeStateStatus: DIRTY`，GH 靜默跳過 `on: pull_request` CI（零 workflow 觸發）。對應修正：兩個 wrapper 都加 `set "PATHEXT=.COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"`、`feat/p41-codify-windows-commit` rebase 到 origin/main 去除重複 commits。
  - **`.gitignore` 政策翻轉**：移除 `_*.bat` / `_*.ps1` / `_*.txt` / `_*.md` scratch-hiding patterns（保留 `_*.json` / `_*.out` / `_*.err` / `_ci_logs/` 等真正 unreviewable artifact）。理由：「藏起來」讓 cleanup 步驟不可見，下個 session 看不到前一個 session 的垃圾，每次重寫。改走 **adopt-or-delete** 路線——scratch 要不升格進 `scripts/ops/`、要不 commit 前刪乾淨。
  - **`CLAUDE.md` Top 6 坑 → Top 7 坑**：新增 §6「⛔ 絕對不要寫 `_foo.bat` / `_p*_commit.ps1`」指向 `win_gh.bat` / `win_git_escape.bat`。

- **`dev-rules.md §P2` 轉 code-driven enforcement（v2.8.0 Phase .a）**：PR #39/#40 踩坑後的結構性改進——純文字規範（「`gh pr create` 前記得掃 drift」）對 agent 記性依賴過高，改寫為兩個 hook：
  - **`scripts/tools/lint/check_devrules_size.py`**（pre-commit 硬上限）：`docs/internal/dev-rules.md` 超過 **500 行**即 fail。用意是把 dev-rules.md 的累積量做為「code-driven 遷移壓力反向指標」——文字規範越肥代表越多條目本來就該當 hook 寫掉。新增規則時作者被迫三選一：prune / promote（升 L1/L2 hook）/ archive。放寬 `MAX_LINES` 屬禁忌，需在 PR body 明述理由。
  - **`scripts/tools/lint/check_pr_scope_drift.py`**（pr-preflight 硬失敗）：偵測兩項——tool-map drift（`generate_tool_map.py --check` 失敗，典型肇因：新增 `scripts/tools/**/*.py` 但沒 regen）+ working-tree dirty（unstaged / uncommitted staged 存在，典型肇因：session 邊改 playbook / CLAUDE.md 忘記 git add）。
  - **`scripts/tools/dx/pr_preflight.py` 新增 Scope drift phase**：`make pr-preflight` 從 6 項 → **7 項**檢查（branch / behind-main / conflict / local hooks / **scope-drift** / CI / mergeable）。PR merge 前必過。
  - **`dev-rules.md §P2` 從文字敘述改為 hook pointer**：規則本體即 code，避免「文字規範 → 記性 → 執行」三段 rot。新增 drift 項目時改 code，不改本節。

### Changed

- **`.pre-commit-config.yaml` 新增 `devrules-size-check` hook**：緊鄰 `tool-map-check`，僅在 `docs/internal/dev-rules.md` 變動時觸發。
- **`Makefile` `pr-preflight` target 描述更新**：反映 7 項檢查範圍（含 scope-drift）。
- **`dev-rules.md` 大幅瘦身（v2.8.0 Phase .a）**：520 → 487 行，為新 500-line cap 留 buffer。壓縮 §S3 / §S5 的反例+正例 block（資訊保留、用註解合併）。未刪任何規則條文。
- **CLAUDE.md tool count `114 → 117`**：同步 `docs/internal/tool-map.md` regenerated 計數（ops 46 / dx 29 / lint 41，累積 +3：PR #40 的 `check_devrules_size.py` + `check_pr_scope_drift.py`，PR #41 的 `check_ad_hoc_git_scripts.py`）。`make pr-preflight` 描述同步更新為 7 項。

### Fixed

- **A-5a `make pr-preflight` / `pre-tag` Chart.yaml path bug（Phase .a A-5a）**：Helm chart 從 `components/threshold-exporter/` 遷至 `helm/threshold-exporter/`（parallels `helm/tenant-api/`）時遺漏 3 處 stale reference，導致 `make version-check` 印 `grep: components/threshold-exporter/Chart.yaml: No such file or directory` warning、`make chart-package` / `chart-push` target 失效：
  - `Makefile:475` — `CHART_DIR := components/threshold-exporter` → `helm/threshold-exporter`
  - `scripts/tools/dx/bump_docs.py:66` — `CHART_YAML = REPO_ROOT / "components" / "threshold-exporter" / "Chart.yaml"` → `"helm" / "threshold-exporter"`（line 68 `TENANT_API_CHART_YAML` 早已用 `helm/` 為正確範式）
  - `docs/internal/github-release-playbook.md:397` — release step 15 驗證指令 grep path 同步修正
  - 驗證：`make version-check` 輸出不再含該 warning；Helm chart 發佈主路徑恢復可用

### Changed

- **Doc governance — Planning Artifact Policy 升 SSOT + Session Ledger 退場（Phase .a Session #18）**：v2.8.0 期間反覆觸發 context-compact 的根因分析催生兩項規範化動作。原 `v2.8.0-planning.md §12.6` 的「Planning Artifact Policy」（L1/L2/L3 文件分類 / 決策樹 / retention rule）跨版長期原則升至 `dev-rules.md §A 產出物治理`（`docs/internal/dev-rules.md`）作 SSOT；新增 §A6「v2.9.0+ planning doc 不再保留 §12.1 Session Ledger」（compact-pressure 主因為 append-only Session 表 row 動輒 2-4 KB）。
  - **新規範**：`dev-rules.md §A1-A7`（taxonomy / 4 條為何不落 repo / pattern 清冊 / 決策樹 / retention rule / Session Ledger 退場 / dissent pointer）
  - **新工具**：`scripts/tools/lint/validate_planning_session_row.py`（manual-stage：偵測 §12.1 Session row 超過 char limit，預設 2000）
  - **新 Makefile target**：`make check-planning-bloat`（直接呼叫上述 lint）+ `make session-cleanup` 末尾自動跑（best-effort，不阻擋 cleanup）
  - **既有 planning.md 瘦身**（Session #18 同步執行）：`v2.8.0-planning.md` 從 ~987 lines → 720 lines；§12.3.1 Q1-Q6 詳細分析 / §12.4 resolved-trap RCA（#3 #9 #10 #11 #12）/ §12.6 政策 dissent / §12.7 PR#1 完整 mapping table 全搬至 `v2.8.0-planning-archive.md`（gitignored，maintainer-local）；§12.5 Active TODO 從 ~15 mixed `[x]/[ ]` 收斂為 5 純 active 項
  - **CLAUDE.md / 其他 SSOT 不變**：本次遷移為 internal 文件治理，不影響 user-facing API / schema / CLI

- **A-5b scan_component_health `status: archived` opt-in 下架路徑（Phase .a A-5b）**：`scripts/tools/dx/scan_component_health.py` 擴展 Tier policy 以支援 registry 手動標記下架，非破壞性（Q2 warning-only 政策延伸）：
  - **Registry schema 擴展**：`docs/assets/tool-registry.yaml` 工具項可新增 `status: archived` + `archived_reason: "..."`（本版未標任何工具 archived，schema + 邏輯擴展為主）
  - **scan 行為**：archived 工具產出 `tier: "Archived"` / `status: "ARCHIVED"`，保留 LOC / i18n 作 visibility metric，**從所有 aggregates 排除**（`tier_distribution` / `token_group_distribution` / `playwright_coverage` / `i18n_coverage_distribution` / `tools_with_hardcoded_hex` / `tools_with_hardcoded_px` / `tier1_token_group_a/c`）—— aggregates 分母統一改用 `active_results`（`status == "OK"`）
  - **Summary 新增 4 欄位**：`total_active_tools`（未 archived 計數）、`archived_count`、`archived_tools: [keys]`、`archive_candidates: [{key, reason}]`（自動建議清單供 PR review）
  - **自動建議 criteria（6 條 AND）**：`tier == "Tier 3 (deprecation_candidate)"` AND `loc < 50` AND `tier_breakdown.recency == -1`（>180 天未動）AND `tier_breakdown.writer == 0` AND `not playwright_spec` AND `first_commit > 365 天`。過於保守（防 false positive）—— 最終下架決策仍需維護者寫入 registry
  - **新測試**：`tests/dx/test_scan_component_health.py`（12 cases，`_is_archive_candidate` × 8 threshold 測試 + `scan()` × 4 integration，tmp_path + monkeypatch 完全隔離 git/jsx 依賴）
  - **dev-rules.md 新增 §T 工具生命週期**：四態轉換表（active / deprecation_candidate / archive_candidate / archived）+ 判定來源 + scan_component_health 行為 + opt-in rationale + 排除後仍保留 LOC/i18n 的原因（避免 archived 工具在 registry 中完全消失）

- **TECH-DEBT-007 resolved — `--da-color-hero-muted` contrast fail via token-split**（v2.8.0 Phase .a PR#1c）：修復 multi-tenant-comparison / dependency-graph 在 light bg 下 axe-core `color-contrast` 40-node 違規。根因並非 token 色值單純「太淺」，而是**單一 semantic token 被迫服務兩種亮度相差 > 40% 的背景**（hero dark `#0f172a` + tile light `hsl(x,60%,90%)` / SVG white）——任何單值都無法同時滿足 WCAG AA 4.5:1。
  - `docs/assets/design-tokens.css`：保留 `--da-color-hero-muted: #94a3b8`（hero dark bg 7.2:1 AA pass）；新增 `--da-color-tile-muted: #6b7280`（white 4.83:1 AA pass），light / dark mode 皆同值（consumer 背景不翻色）
  - `docs/interactive/tools/multi-tenant-comparison.jsx` L194 `defaultBadgeStyle`：`hero-muted` → `tile-muted`（HeatmapRow cell badge 處於 `hsl(hue,60%,90%)` 永遠亮底）
  - `docs/interactive/tools/dependency-graph.jsx` L215：SVG `<text fill>` `hero-muted` → `tile-muted`（parent `bg-white` SVG 容器）
  - L133 `MetricCard subStyle` **刻意排除在 PR#1c scope 外**：card bg 隨 `[data-theme="dark"]` 翻色（light `#f8fafc` ↔ dark `#334155`），需 theme-aware override 另案處理 → 登錄 TECH-DEBT-016 追蹤
  - 新增 `dev-rules.md §S5 單一 semantic token 不可 serve 亮度相差 > 40% 的兩種背景`：固化 token-split 規則 + 命名慣例（`--da-color-<surface>-<intent>`）+ 雙主題翻色 caveat
  - `known-regressions.md`：TECH-DEBT-007 狀態 open → **resolved**（附 fixed_in 引述本 PR）；TECH-DEBT-016 新登錄
  - 背景分析：plan.md §12.4 Trap #10（shared-token-across-opposing-backgrounds 反模式）、§12.5 PR#1c spec

- **Blast Radius PR comment length guard**（v2.7.0 defensive patch）：`scripts/tools/ops/blast_radius.py` `generate_pr_comment` 加三層守門，避免 GitHub 65,536 char 硬上限造成 CI 靜默失敗（422 Unprocessable Entity，bot 會「成功」但 comment 不存在）：
  - 當 Tier A+B affected tenant > 50 時，切 **summary-only mode**（只列 tenant IDs，不展 per-field diff；完整 diff 走 `blast-radius-report` workflow artifact）
  - Summary-only mode 內部再 cap 200 條，多的收斂為「+N more」
  - 60,000 char safety limit：即使 fell-through（例如單一 tenant 有上千欄位變動），也會 auto-fallback 到 summary-only 或最後手段硬截斷
  - 新增 `--artifact-hint` CLI flag，`.github/workflows/blast-radius.yml` 對應 pass workflow run URL，讓 reviewer 看 summary-only comment 時能一鍵跳去 artifact
  - 9 tests 於 `tests/ops/test_blast_radius.py::TestPRCommentLengthGuard`：1000-tenant、超長 field diff、Tier C count-only、artifact hint rendering 等情境均驗證 output 長度 < 65,536
  - 發現來源：Gemini R2 cross-review；實測 1000-tenant Tier A 場景原輸出 ~260 KB，超限 4 倍



## [v2.7.0] — 千租戶配置架構 + 元件健壯化 (2026-04-19)

v2.7.0 把租戶配置的資料結構升級為可支撐千租戶規模（`conf.d/` 階層 + `_defaults.yaml` 繼承引擎 + dual-hash 熱重載），把 v2.6.0 的 Design Token 定義推進到全面採用，並把測試與 CI 從「能跑」升級為「可規模化」。

### Scale Foundation I — 千租戶配置架構（ADR-017 / ADR-018）

- **`conf.d/<domain>/<region>/<env>/` 階層目錄**：任一層可放 `_defaults.yaml`，`L0 defaults -> L1 domain -> L2 region -> L3 tenant` 四層 deep merge，array replace / null-as-delete 語義明確
- **Dual-hash 熱重載**：`source_hash`（原始檔 SHA-256）+ `merged_hash`（canonical JSON SHA-256）並行追蹤，merged_hash 變才 reload；300ms debounce 吸收 K8s ConfigMap symlink rotation 的連續寫入
- **Mixed-mode**：舊扁平 `tenants/*.yaml` 與新 `conf.d/` 可共存，無強制一次遷移
- **`GET /api/v1/tenants/{id}/effective`**：回傳 merged config + 繼承鏈 + dual hashes，方便 debug 實際生效設定
- **新 CLI**：`da-tools describe-tenant`（含 `--what-if <file>` 模擬 `_defaults.yaml` 變動 -> diff merged_hash）+ `da-tools migrate-conf-d`（扁平 -> 階層自動 `git mv`，預設 `--dry-run`）
- **Schema 新增**：`tenant-config.schema.json` 加入 `definitions/defaultsConfig` + `_metadata.$comment`

### 元件健壯化

- **Design Token 全面遷移**：9 個 Tier 1 JSX 工具完成 Tailwind -> arbitrary value token 改寫（`wizard` / `deployment-wizard` / `alert-timeline` / `dependency-graph` / `config-lint` / `rbac` / `cicd-setup-wizard` / `tenant-manager` / `multi-tenant-comparison`）；剩餘 7 個 px-only 工具延 v2.8.0
- **`[data-theme]` 單軌 dark mode**（ADR-016）：移除 Tailwind `dark:` 雙軌橋接，解決 v2.6.0 誤用陷阱
- **Component Health Snapshot**（ADR-013）：`scan_component_health.py` 五維評分（LOC / Audience / Phase / Writer / Recency）-> Tier 1 = 11 / Tier 2 = 25 / Tier 3 = 3；新增 `token_density` 量化 token 採用進度
- **Colorblind 合規**（ADR-012）：`threshold-heatmap` 結構化 severity（不只靠顏色）
- **TECH-DEBT 類別獨立 budget**（ADR-014）：從 REG budget 分出，不佔 REG P2/P3 配額
- **新 lint**：`check_aria_references.py` / `axe_lite_static.py` / `check_design_token_usage.py`

### 測試與基礎設施

- **`tests/` 子目錄分層**：`dx/` / `ops/` / `lint/` / `shared/`，匹配 `scripts/tools/` 的分層
- **1000-tenant synthetic fixture**：`generate_synthetic_tenants.py` 產可重現的千租戶資料，供 B-1 Scale Gate 量測
- **Blast Radius CI bot**：PR 變更自動計算影響的 tenants / rules / thresholds，comment 到 PR
- **Pre-commit**：31 auto + 13 manual-stage；`make pre-tag` 整合 `version-check` + `lint-docs` + `playbook-freshness-ll`

### Benchmark（1000 tenants, Intel Core 7 240H, Go 1.26.1, `-benchtime=3s -count=3`）

| 指標 | 時間 | 語義 |
|:---|---:|:---|
| `FullDirLoad_1000` | 112 ms | Cold load（scan + YAML parse + merge + hash） |
| `IncrementalLoad_1000_NoChange` | 2.45 ms | Dual-hash reload noop（45x 快於 cold） |
| `IncrementalLoad + MtimeGuard` | 1.30 ms | 加 mtime 短路（86x 快於 cold） |
| `MergePartialConfigs_1000` | 653 us | 階層 merge 本身 |

SLO：cold load 112 ms / 1000 tenants；reload 熱路徑 1.30 ms 相對於預設 15 s scan_interval 僅 0.0087%，幾乎零 overhead。完整報告見 [`benchmarks.md §12`](docs/benchmarks.md#12-incremental-hot-reload-b-1-scale-gate)。

### ADR 新增（ADR-012~018，7 條）

colorblind 結構化 severity / component health + token_density / TECH-DEBT 獨立 budget / token 遷移策略 / 單軌 dark mode / `conf.d/` 階層 / `_defaults.yaml` 繼承引擎 + dual-hash 熱重載。

### Breaking changes

無。`conf.d/` 與繼承引擎為**新增能力**；舊扁平 `tenants/*.yaml` 完全向後相容，Schema 只新增不改動。

### Upgrade notes

- 既有使用者：不需變更
- 想採用 `conf.d/` 分層：見 `docs/scenarios/multi-domain-conf-layout.md` + `incremental-migration-playbook.md`，或 `da-tools migrate-conf-d --dry-run`
- 熱重載：dual-hash 預設啟用，debounce window 300ms 可用 `--scan-debounce` 調整

---

## [v2.6.0] — Operator 遷移路徑 × PR Write-back × 設計系統統一 (2026-04-07)

v2.6.0 的核心是「讓 enterprise 客戶能信賴地在 Operator 環境下運營」：建立完整的 ConfigMap → Operator 遷移工具鏈與對稱文件（ADR-008 addendum），引入 PR-based 非同步寫入支援 GitHub 與 GitLab 雙平台（ADR-011），統一設計系統消除三套平行 CSS 的技術債，並新增 4 個互動工具強化價值傳達。

### K8s Operator 完整遷移路徑

v2.3.0 引入的 Operator 指南是單一文件；v2.6.0 將其擴展為與 ConfigMap 路徑完全對稱的文件體系與工具鏈。

- **ADR-008 addendum**：正式記錄架構邊界宣言——threshold-exporter 不 watch 任何 CRD，CRD → conf.d/ 轉換由外部控制器或 CI 負責。含 Mermaid 邊界圖 + 三問判斷標準（ZH + EN 雙語）
- **`operator-generate` 大幅增強**：AlertmanagerConfig 6 種 receiver 模板（Slack, PagerDuty, Email, Teams, OpsGenie, Webhook），每種自動產出 `secretKeyRef` 引用 K8s Secret（零明文 credential）。新增 `--receiver-template`、`--secret-name`、`--secret-key` 參數
- **三態抑制規則 CRD 化**：Silent / Maintenance mode 自動包含在每個 AlertmanagerConfig 產出（4 條 inhibit rules）
- **Helm `rules.mode` toggle**：threshold-exporter chart 新增 `configmap | operator` 切換 + ServiceMonitor 條件模板，operator section 含 ruleLabels、serviceMonitor、receiverTemplate、secretRef
- **`da-tools migrate-to-operator`**（新增 CLI）：讀取現有 ConfigMap rules → 產出等效 CRD + 6 階段遷移清單（Discovery → Generate → Shadow → Compare → Switch → Cleanup）+ rollback 程序。`validate_tenant_name()` RFC 1123 驗證確保 CRD apply 不失敗
- **Operator Setup Wizard**（新增 JSX）：互動式偵測環境 → 選 CRD 類型 → 產出命令，每步驟含 contextual help + 常見陷阱提示
- **Kustomization.yaml 自動產生**：`operator-generate --kustomize` 產出標準格式，含 commonLabels + sorted resources + namespace
- **`drift_detect.py` Operator 模式**：`--mode operator` 透過 kubectl 取得 PrometheusRule CRD 的 spec.groups SHA-256，與本地 YAML 比對。kubectl timeout 30s + 三種錯誤處理
- **Decision Matrix**：提升到 Getting Started 層級，決策樹 + 10 維度比較表（ZH + EN）
- **文件對稱化**：`prometheus-operator-integration.md` 拆分為 4 組子文件（Prometheus / Alertmanager / GitOps / Shadow Monitoring）各含 ZH + EN 版本 + 2 hub 導航頁 = 10 篇新文件

### PR-based Write-back + 非同步 API

v2.5.0 的 tenant-api 只支援 direct write（API → YAML → git commit）。v2.6.0 新增 PR 模式與非同步批量操作，讓高安全環境能透過 code review 流程管理配置變更。

- **ADR-011**（新增 ADR，ZH + EN 雙語）：定調 PR lifecycle state model（pending / merged / conflicted）、GitHub PAT 權限與 Secret 管理策略、多 PR 合併衝突處理、eventual consistency 語義
- **PR-based write-back**：`_write_mode: direct | pr` 配置切換（`-write-mode` flag + `TA_WRITE_MODE` env）。UI 操作 → 建立 PR → reviewer 核准 → 合併。PR-mode API response 回傳 `pr_url` + `status: "pending_review"`
- **Batch PR 合併**：群組批量操作合併為單一 PR（非 N 個），減少 reviewer 負擔
- **Async batch operations**：`?async=true` query param 啟用非同步模式，回傳 `task_id` + `status: "pending"`。goroutine pool 執行，GET `/tasks/{id}` polling 查詢進度
- **Orphaned task 容錯**：Pod 重啟後 in-memory task state 遺失，GET `/tasks/{id}` 回傳 404 附帶 `pod_may_have_restarted` hint
- **SSE 即時通知**：`GET /api/v1/events` 端點，gitops.Writer 寫入成功後自動推播 `config_change` 事件。採用 Server-Sent Events 實作，零外部依賴
- **tenant-manager.jsx**：Pending PRs 提示 banner（頂部顯示待審核 PR 數量與連結，30s 輪詢）

### Platform Abstraction Layer + GitLab 支援

為使 PR write-back 成為平台無關的能力，抽取 platform interface 並新增 GitLab MR 支援。

- **`internal/platform/platform.go`**（新增）：`PRInfo` struct、`Client` interface（5 methods: CreateBranch / CreatePR / ListOpenPRs / ValidateToken / DeleteBranch）、`Tracker` interface（6 methods）。handler 只依賴 interface，provider 可替換
- **`internal/gitlab/`**（新增套件）：GitLab REST API v4 client，`PRIVATE-TOKEN` header 認證，`url.PathEscape` 支援含 `/` 的 `group/subgroup/project` 路徑。全部 5 個 `platform.Client` 方法 + 6 個 `platform.Tracker` 方法
- **Write mode 路由**：`--write-mode direct | pr | pr-github | pr-gitlab` 四種模式，`pr` 為 `pr-github` alias（向後相容）
- **On-Premise 支援**：GitHub Enterprise Server（`TA_GITHUB_API_URL`）+ 自託管 GitLab（`TA_GITLAB_API_URL`）。`SetBaseURL()` 已納入 `platform.Client` interface
- **Compile-time interface assertions**：`var _ platform.Client = (*Client)(nil)` + `var _ platform.Tracker = (*Tracker)(nil)` 確保型別安全
- **錯誤訊息衛生化**：`doRequest` 在 HTTP 4xx/5xx 時 log 完整 response body（debugging），回傳 caller 的 error 只含 status code（不洩漏 API body 給前端）。GitHub + GitLab 兩端一致
- **GitLab state 正規化**：`normalizeState()` 將 GitLab `opened` 映射為 `open`（與 GitHub 一致）
- **ListOpenPRs pagination**：per_page=100, 10 pages safety limit

### 設計系統統一

v2.5.0 暴露了三套平行 CSS 系統（CSS variables / Tailwind / inline styles）是所有無障礙問題的根源。v2.6.0 建立 design token SSOT 並全面遷移。

- **`docs/assets/design-tokens.css`**（新增）：統一 CSS variable 定義（11 個類別：color, spacing, typography, shadow, radius, transition 等），按 §1-§11 組織，命名規範 `--da-{category}-{element}-{modifier}`
- **Dark mode 三態切換**：`[data-theme="dark"]` attribute 取代 `@media (prefers-color-scheme: dark)`。Portal 加入 Light / Dark / System 三態切換按鈕，狀態存 localStorage（fallback: in-memory + cookie）
- **tenant-manager.jsx 遷移**：消除 454 行 hardcoded inline styles，全面切換至 CSS variables + Tailwind classes
- **focus-visible 全局化**：CSS 層統一實作，不再依賴各 JSX 檔案自行加入
- **index.html 統一**：legacy aliases（`var(--bg)`, `var(--muted)`）全面遷移至 `var(--da-*)` tokens
- **`docs/internal/design-system-guide.md`**（新增）：design token 命名規範、使用方式、`[data-theme]` 切換機制、Light/Dark/System 三態邏輯

### 價值傳達與互動工具

讓潛在使用者與現有客戶能快速量化平台的採用價值。

- **ROI Calculator 增強**：新增 Quick Estimate 模式（單一輸入即出結果）+ 完整三維計算（Rule Maintenance + Alert Storm + Time-to-Market）
- **Migration ROI Calculator**（新增 JSX）：輸入 PromQL 行數 / rules / tenants → coverage estimation + migration effort + break-even analysis
- **Cost Estimator**（新增 JSX, 827 lines）：tenants × packs × scrape interval × retention × HA replicas × deployment mode → Resource Summary + Monthly Cost + ConfigMap vs Operator 比較 + Quick Recommendation
- **Notification Template Editor**（大幅改版, 897 lines）：從 Previewer 升級為 Editor——可編輯 title/body 模板 + template variable autocomplete + validation（unmatched braces, char limits）+ live preview + export YAML/JSON + template gallery（Detailed/Compact/Bilingual presets）
- **architecture-and-design.md** 每個子主題加入 business impact 欄位（ZH + EN，O(M) vs O(N×M) 複雜度對比、Onboard 2hr→5min 等量化指標）
- **release-notes-generator.jsx** 新增 `generateAutoSummary()` 函式，CHANGELOG 角色分流自動摘要（per-role "What's new for you"）

### 測試與品質

- **Playwright axe-core 整合**：`@axe-core/playwright` 自動偵測 WCAG 違規，整合到既有 5 個 smoke tests + 新增 Operator Wizard 12 tests
- **Property-based testing**（新增 22 tests）：Hypothesis 覆蓋 tenant name RFC 1123 validation、SHA-256 hashing、drift detection symmetry、YAML round-trip、kustomization builder。`@settings(max_examples=100)` 確保覆蓋
- **Go `-race` 全通過**：Phase .e 發現並修復 async/taskmanager.go + ws/hub_test.go data race。`Get()` 改為回傳 deep copy snapshot 防止併發讀寫
- **大型 Python 工具重構**：`generate_alertmanager_routes.py`（1,474→1,645 lines，21 helpers extracted，>100 行函式 4→0）+ `init_project.py`（1,404→1,438 lines，6 helpers extracted）
- **aria-live regions**：tenant-manager.jsx 新增 4 個 region（sidebar, PRs banner, batch, tenant grid）+ threshold-heatmap.jsx 新增 3 個 region
- **Batch response summary**：tenant_batch.go + group_batch.go 回傳 `summary` 欄位（"N succeeded, M failed"）
- **version-consistency hook 擴展**：覆蓋 e2e/package.json、JSX 工具版號
- **tool-registry.yaml 對齊**：補齊 3 個缺失條目（rbac-setup-wizard, release-notes-generator, threshold-heatmap）

### 數字

| 項目 | v2.5.0 | v2.6.0 | 變化 |
|------|--------|--------|------|
| JSX 互動工具 | 38 | 42 | +4 |
| ADRs | 10 | 11（+ ADR-011）+ ADR-008 addendum | +1 |
| Operator 子文件 | 1 | 10（4 ZH + 4 EN + 2 hub） | +9 |
| Go test packages（`-race` clean） | — | 11 packages, 0 race | NEW |
| Property-based tests (Hypothesis) | 0 | 22 | NEW |
| Helm chart features | — | `rules.mode` toggle + ServiceMonitor | NEW |
| Write-back 模式 | 1（direct） | 4（direct / pr / pr-github / pr-gitlab） | +3 |
| Platform providers | 0 | 2（GitHub + GitLab） | NEW |
| Python 工具 | 91 | 95 | +4 |
| Pre-commit hooks | 19 auto + 9 manual | 19 auto + 10 manual | +1 manual |
| 環境變數（tenant-api） | ~10 | ~18 | +8（Write-back + GitLab） |

### 🐛 Bug Fixes

- `migrate_to_operator.py`：`discover_tenant_configs()` 靜默過濾無效 tenant 名稱 → 改為回報至 `analysis["issues"]` 清單
- `tracker.go`：`RegisterPR()` 同 tenant 可能重複 append → 改為 replace-or-append 邏輯
- `migration-roi-calculator.jsx`：2 個 label 未翻譯
- index.html：light-mode `.journey-phase-badge` + `.card-icon` 殘留 hardcoded hex color → 全部改用 design tokens
- README.md / README.en.md：badge 版號 v2.5.0 → v2.6.0
- troubleshooting.en.md：缺少 Prometheus Operator 章節 → 新增完整診斷+修正步驟+Rollback 程序
- troubleshooting.md：Operator 章節僅有診斷 → 補充三種修正步驟 + Rollback 程序

---

## [v2.5.0] — Multi-Tenant Grouping × Saved Views × E2E Testing (2026-04-06)

v2.5.0 在 v2.4.0 建立的 Tenant API 基礎上，實現租戶分群管理（ADR-010）、Saved Views、Playwright E2E 測試基礎，並新增 4 個互動工具。

### Multi-Tenant Grouping（ADR-010）

- 新增 `conf.d/_groups.yaml` 儲存結構：靜態 `members[]` 成員清單，Git 版本化，可 code review
- Group CRUD API：`GET/PUT/DELETE /api/v1/groups/{id}` + `POST /api/v1/groups/{id}/batch` 批量操作
- Permission-filtered listing：ListGroups 只回傳使用者有權限存取至少一個成員的 group
- 批量操作逐 tenant 驗證寫入權限，部分失敗不影響已成功項目

### Saved Views API

- 新增 `conf.d/_views.yaml`：持久化篩選條件（environment + domain + 自訂 filter 組合）
- CRUD 端點：`GET/PUT/DELETE /api/v1/views/{id}`，支援使用者自建常用視圖
- 與 Portal tenant-manager 整合：一鍵切換預設篩選

### Tenant Metadata 擴展

- 新增可選欄位：`environment`、`region`、`domain`、`db_type`、`tags[]`、`groups[]`
- 全部向後相容——未設定 metadata 的 tenant 不受影響
- Metadata 僅 API/UI 層使用，不影響 Prometheus metric cardinality

### RBAC 增強

- `_rbac.yaml` 新增 `environments[]` 和 `domains[]` 可選過濾欄位
- 支援「特定 group 只能管理 production 環境」等細粒度控制

### 新增互動工具（34 → 38 JSX tools）

- **Deployment Profile Wizard** (`deployment-wizard.jsx`)：互動式 Helm values 產生器
- **RBAC Setup Wizard** (`rbac-setup-wizard.jsx`)：互動式 `_rbac.yaml` 產生
- **Release Notes Generator** (`release-notes-generator.jsx`)：從 CHANGELOG 自動產生角色導向更新摘要
- **Threshold Heatmap** (`threshold-heatmap.jsx`)：跨 tenant 閾值分佈熱力圖 + 離群偵測 + CSV 匯出

### Playwright E2E 測試基礎

- 5 個 critical path spec（38 個 test case）：portal-home、tenant-manager、group-management、auth-flow、batch-operations
- Mock API 隔離（無外部依賴）、GitHub Actions CI 整合
- `tests/e2e/playwright.config.ts` + `.github/workflows/playwright.yml`

### CI/CD 改進

- tenant-api Go 測試納入 CI pipeline（2,115 行測試程式碼）
- Release 流程強化：`make pre-tag` 閘門、`bump_docs.py` 新增 tenant-api 版號線

### 數字

| 項目 | v2.4.0 | v2.5.0 | 變化 |
|------|--------|--------|------|
| JSX 互動工具 | 34 | 38 | +4 |
| ADRs | 9 | 10 | +1（ADR-010） |
| Playwright E2E specs | 0 | 5（38 test cases） | NEW |
| API 端點 | ~10 | ~16 | +6（groups + views） |

---

## [v2.4.0] — 防守深化 × 體質精簡 × 租戶管理 API (2026-04-05)

v2.4.0 的核心是「從能用到好管」：將 v2.3.0 release 暴露的手動痛點全面自動化（Phase A），對膨脹的核心檔案進行結構性重構（Phase B/B.5），引入 Tenant Management API 作為管理平面（Phase C），並重整 Playbook 體系（Phase D）。

### Phase A — 防守工具補強

將 v2.3.0 release 過程中手動發現的 6 類問題轉化為 pre-commit hook，auto hooks 從 13 個增至 19 個。

- **`check_build_completeness.py`**：`build.sh` ↔ `COMMAND_MAP` 雙向同步檢查，防止 Docker image 中工具遺漏
- **`check_bilingual_structure.py`**：ZH/EN 文件 heading hierarchy 骨架比對 + README 雙語導航對稱性
- **`check_jsx_i18n.py`**：`TOOL_META` ↔ `CUSTOM_FLOW_MAP` key set 一致性、`window.__t` 雙參數驗證
- **`check_makefile_targets.py`**：每個 `dx/generate_*.py` 和 `dx/sync_*.py` 工具被至少一個 Makefile target 引用
- **`check_metric_dictionary.py`**：`metric-dictionary.yaml` 與 Rule Pack YAML 交叉驗證，偵測 stale/undocumented entries
- **`check_cli_coverage.py` hook 化**：從測試升級為 pre-commit auto hook，cheat-sheet ↔ cli-reference ↔ COMMAND_MAP 三向一致
- **`_lint_helpers.py`**：抽取 `parse_command_map()`、`parse_build_sh_tools()`、`BUILD_EXEMPT` 等共用邏輯，消除 ~80 行重複

### Phase B — Go config.go 分拆 + 程式碼體質改善

- **config.go 拆分**（2,093 行 → 4 檔案）：`config_types.go`（268 行，型別定義）+ `config_parse.go`（277 行，YAML 解析）+ `config_resolve.go`（750 行，ResolveAt + 驗證）+ `config.go`（823 行，ConfigManager + 公開 API）
- 拆分為純結構移動，public API 語意不變，benchmark 差異 -0.3% ~ -5.0%（±5% 以內）
- **config_test.go table-driven 重構**：4,236 → 3,929 行（-7.2%），38 個重複 test function 收斂為 8 個 table-driven test，test function 總數 145 → 115
- Go 全部 145 測試通過，Python 3,657 passed / 44 skipped / 0 failed

### Phase B.5 — 文件與測試瘦身

Phase B 做到了「結構整理」，B.5 補做「內容精簡」。

- **合併 `context-diagram.md` → `architecture-and-design.md`**：~70% 重疊內容消除，淨刪 ~1,165 行，docs/ 檔案數 115 → 113
- **`incremental-migration-playbook.md` 瘦身**：1,165 行 → 575 行（-50.6%），冗長 JSON 範例改為摘要，手動 kubectl 序列改為 `da-tools` 命令
- **三態說明集中化**：`tenant-lifecycle.md` 的 60 行重複三態解釋改為 hyperlink + 3 行速查
- **版號全域修正**：44 處過時版號更新 + 文件計數修正
- 文件總計：docs/ -2,362 行（-6.4%），-2 個檔案

### Phase C — Tenant Management API（ADR-009）

新增 `components/tenant-api/` Go 元件，為 da-portal 加入 Backend API。

**架構決策（ADR-009）**
- API 語言選 Go：與 threshold-exporter 共用 `pkg/config` 解析邏輯，避免 Go↔Python 雙端維護
- 認證用 oauth2-proxy sidecar：API server 零 auth 程式碼，讀 `X-Forwarded-Email` / `X-Forwarded-Groups` header
- 寫回用 commit-on-write：UI 操作 → API → 修改 YAML → git commit（操作者名義），保留完整 audit trail
- RBAC 用 `_rbac.yaml` + `atomic.Value` 熱更新：lock-free 讀取，與 threshold-exporter reload 模式一致
- 不引入資料庫——Git repo 就是 database

**`pkg/config/` 抽取**
- 將 threshold-exporter 的型別與解析邏輯抽入 `components/threshold-exporter/app/pkg/config/`（`types.go` + `parse.go` + `resolve.go`）
- tenant-api 透過 `go.mod replace` directive 直接 import 共用型別

**API 端點**
- `GET /api/v1/tenants` — 租戶列表（支援 group/env 篩選）
- `GET/PUT /api/v1/tenants/{id}` — 單一租戶 CRUD
- `POST /api/v1/tenants/{id}/validate` — 乾跑驗證（不寫入）
- `POST /api/v1/tenants/batch` — 批量操作（`sync.Mutex` 同步，response 預留 `task_id`）
- `GET /api/v1/tenants/{id}/diff` — 預覽變更差異
- Health check / readiness probe / Prometheus metrics

**Portal 降級安全**：API 不可用時，tenant-manager.jsx 自動降級為 platform-data.json 唯讀模式。

**交付物**：Go binary + Docker image（distroless base）+ Helm chart + K8s manifests + 五線版號新增 `tenant-api/v*`

### Phase D — Playbook 重整 + 文件治理

- Playbook 結構化：testing-playbook 五段分層、benchmark-playbook 加入決策樹、windows-mcp-playbook 32 個 pitfall 分類索引
- `bump_docs.py` 自動計數功能：掃描並更新散落各處的工具數量、Rule Pack 數量等
- doc-map.md 自動生成預設包含 ADR

### 數字

| 項目 | v2.3.0 | v2.4.0 | 變化 |
|------|--------|--------|------|
| Pre-commit hooks | 13 auto + 7 manual | 19 auto + 9 manual | +6 auto, +2 manual |
| Go config.go | 2,093 行 × 1 檔 | 4 檔（268 + 277 + 750 + 823） | 結構拆分 |
| config_test.go | 4,236 行 / 145 函式 | 3,929 行 / 115 函式 | -7.2% / table-driven |
| docs/ 行數 | 37,059 | 34,697 | -2,362（-6.4%） |
| Components | 3 | 4（+ tenant-api） | +1 |
| ADRs | 8 | 9（+ ADR-009） | +1 |
| JSX 互動工具 | 29 | 34 | +5 |
| 版號線 | 4 | 5（+ tenant-api/v*） | +1 |
| Python 工具 | 84 | 91 | +7 |

---

## [v2.3.0] — Operator-Native × Management UI × Platform Maturity (2026-04-04)

v2.3.0 聚焦四大主題：Operator-Native 整合、Multi-Instance Management UI、Portal & Doc 成熟度、品質閘門升級。

### Phase .a — Portal & DX Foundation

**Self-Service Portal 模組化**
- `self-service-portal.jsx`（1,376 行）→ 5 個模組：`portal-shared.jsx`（共用常數/函式/元件）+ `YamlValidatorTab.jsx` + `AlertPreviewTab.jsx` + `RoutingTraceTab.jsx` + coordinator
- 新增 `dependencies` frontmatter 機制：jsx-loader.html 支援 YAML frontmatter 中宣告依賴，依序載入 → `loadDependency()` / `loadDependencies()` / `transformImports()`
- `window.__portalShared` 模式：共用模組透過全域變數註冊，tab 模組解構取用

**Template Gallery 外部化**
- 24 個模板 → `docs/assets/template-data.json`（雙語 `{zh, en}` 物件格式 + `category` 欄位）
- `template-gallery.jsx` 改為 `useEffect` fetch 載入，新增 loading/error 狀態
- 檔案大小：806 → 293 行（-64%）

**Portal Hub 五層重組**
- 29 個工具卡片從 2 區（Interactive / Advanced）→ 5 層級：Start Here、Day-to-Day、Explore & Learn、Simulate & Analyze、Platform Operations
- 新增 Quick Access 面板（5 個常用工具快捷連結）
- 每層級附色彩標籤（Onboarding / Core Workflow / Reference / What-If / Engineer）
- Role filter 同時作用於 Quick Access chips
- Tour 步驟更新、Footer 版號同步

**文件模板系統**
- 新增 `docs/internal/doc-template.md`：定義文件標準結構（frontmatter + 必要 section + Related Resources）
- 新增 `scripts/tools/lint/check_doc_template.py`：frontmatter 完整性 + Related Resources 存在性 + 版號一致性

**`_lib_python.py` 模組拆分**
- `_lib_python.py` → 4 個子模組：`_lib_constants.py`（守護值/常數）+ `_lib_io.py`（檔案 I/O）+ `_lib_validation.py`（驗證邏輯）+ `_lib_prometheus.py`（HTTP/Prometheus 查詢）
- 原檔保留為 re-export facade（向後相容，53 行）

**SAST Rule 7**
- 新增 `TestStderrRouting`：AST 掃描 `print("ERROR..."` / `print("Error..."` 確保附帶 `file=sys.stderr`
- 支援 literal string 和 f-string 兩種格式偵測

---

### Phase .b — Operator-Native + Federation

**ADR-008: Operator-Native Integration Path**
- 雙路整合架構決策：既有 ConfigMap 路徑保留，新增 Operator-Native 模式作為 BYO 方案
- 工具鏈適配而非平台重寫原則——threshold-exporter Go 核心語意不變
- 新增 `detectConfigSource()` 函式：逐級檢測 operator env var → git-sync `.git-revision` 文件 → configmap（預設）

**Prometheus Operator 整合指南**
- 新增 `docs/prometheus-operator-integration.md`（雙語 zh + en）：架構圖、CRD 對應、3 個部署場景（all-in-one / mixed / operator-only）
- BYO 文件清理：移除 Prometheus Operator appendices，改為重定向至新指南
- ServiceMonitor / PrometheusRule / AlertmanagerConfig CRD 映射表

**da-tools Operator 工具**
- **`da-tools operator-generate`** — 從 Rule Packs + Tenant 配置產生 PrometheusRule / AlertmanagerConfig / ServiceMonitor CRD YAML
  - 支援 `--namespace` / `--labels` / `--annotations` 自訂，`--output-format yaml | json`
  - 整合於 da-tools entrypoint + build.sh 打包
- **`da-tools operator-check`** — CRD 驗證工具：PrometheusRule 語法 + AlertmanagerConfig 路由合法性 + ServiceMonitor label 一致性
  - 支援 `--kubeconfig` / `--context` 直連 K8s 驗證，亦支援離線 YAML 驗證
  - Registered in CI lint pre-commit hooks

**Config Info Metric（四層感知）**
- 新增 `threshold_exporter_config_info{config_source, git_commit}` info metric
- 三種模式 + 自動偵測：
  - `configmap`（預設）：從 ConfigMap mount path 讀取 config version
  - `git-sync`：讀取 `.git-revision` 共享 volume 文件，提供 git commit SHA
  - `operator`：讀取 env var `CONFIG_SOURCE=operator` + `GIT_COMMIT=<sha>`
- `detectConfigSource()` 呼叫於 reload 時，確保 metric 實時反映部署形態

**Federation Scenario B（邊緣-中央分裂）**
- **`da-tools rule-pack-split`** — Rule Pack 聯邦分裂工具：
  - Part 1（正規化層）：邊緣側 metric value 驗證、單位轉換、異常值濾除 → 產生 Prometheus RecordingRules
  - Parts 2+3（閾值 + 警報層）：中央側聚合、cross-edge 關聯、全域告警決策 → 產生 Alerting Rules
  - 支援 `--operator` CRD 輸出 + `--gitops` 模式（目錄結構）
  - 關鍵特性：無狀態 split（idempotent）、邊緣 auto-healing（快照回滾）
- **`federation-integration.md` §8** — Scenario B 完整文件：三階段部署（邊緣佈建 → 中央策略 → 端對端驗證）、MTTR 優化、成本模型

**Go 單元測試（+12 tests，覆蓋率 87% → 94%）**
- WatchLoop 整合測試：無檔案變動 / 新增檔案 / 更新現有檔案
- `resolveConfigPath()` 三情案例：configmap flag / git-sync flag / 未設定（預設 configmap）
- `detectConfigSource()` 四情案例：configmap（預設）/ git-sync / operator / precedence（operator > git-sync > configmap）
- Config Info metric 收集器三情案例：各模式 value 驗證 + label 正確性
- Fail-Safe Reload E2E：config 不可讀時 fallback 邏輯

---

### Phase .c — Management UI + Intelligence

**Tenant Manager Data Foundation**
- 新增 `scripts/tools/dx/generate_tenant_metadata.py`：從 `conf.d/` 目錄結構推斷租戶 metadata
  - Rule Pack 推斷：根據 YAML 中 metric prefix 比對 Rule Pack 定義
  - 運營模式推斷：`_silent_mode` / `_state_maintenance` 標誌偵測
  - 路由通道推斷：`_routing` 配置解析
- 擴展 `scripts/tools/dx/generate_platform_data.py`：產出的 `platform-data.json` 新增 `tenant_groups` + `tenant_metadata` 結構
- Tenant metadata 版本化：支援 `--output-dir` 自訂輸出路徑，方便 GitOps 集成

**Tenant Manager UI 元件**
- 新增 `docs/interactive/tools/tenant-manager.jsx`（~650 行）：
  - 響應式卡片牆佈局，環境/層級徽章（dev/staging/prod + app/infra/platform）
  - 運營模式指示器：Normal / Silent / Maintenance 視覺標記 + expires 倒數
  - 批量操作：批次維護/靜默模式 YAML 產生器，支援日期範圍選擇
  - 篩選+搜尋：按環境/層級/模式多維度過濾，模糊搜尋租戶名
- 加入 `tool-registry.yaml` + Portal Hub Tier 1 (Day-to-Day 層級)

**閾值推薦 × Portal 智慧**
- 新增 `docs/assets/recommendation-data.json`：15 個核心指標的 P50/P95/P99 預計算資料
  - 資料來源：歷史基線 + 業界最佳實踐
  - 格式：`{metric_name: {p50, p95, p99, source, last_updated}}`
- 擴展 `docs/interactive/tools/AlertPreviewTab.jsx`：
  - Progress bar 上疊加 recommended value marker 視覺指示
  - Confidence badge（high/medium/low）顯示推薦可信度
  - 新增 "Apply Recommended Values" 按鈕，一鍵生成更新 YAML

**OPA/Rego 策略整合**
- 新增 `scripts/tools/ops/policy_opa_bridge.py`（~450 行）：tenant YAML → OPA input JSON 轉換 + 雙模式評估
  - 轉換函式：YAML 欄位 → OPA JSON 輸入格式映射（支援 nested policies）
  - 評估模式：REST API 模式（連接遠端 OPA 伺服器）+ 本地 opa binary 模式
  - 違規輸出格式轉換：OPA violations → da-tools 標準格式（location + description）
- `scripts/policies/examples/` 新增三個 Rego 範例策略：
  - `routing-compliance.rego`：路由規則命名 / receiver type / group_wait 範圍 validation
  - `threshold-bounds.rego`：閾值範圍檢查 / 關鍵指標預留冗餘
  - `naming-convention.rego`：租戶/告警 ID 命名規範 + Prefix 合法性
- 登記為 `da-tools opa-evaluate` 子命令 + CI lint 整合

**Portal i18n Lint 工具**
- 新增 `scripts/tools/lint/check_portal_i18n.py`（~250 行）：掃描 JSX 檔案尋找硬編碼字串
  - AST 解析：偵測 string literal 未用 `window.__t()` 包裝的情況
  - 支援 `--fix-mode`：自動生成修復建議（帶位置資訊）
  - 排除清單：URL / 特殊字元序列 / i18n 函式呼叫內部字串
- 加入 pre-commit manual-stage hooks 為 `check-portal-i18n`

---

### Phase .d — Quality Gate + CI Maturity

**GitHub Actions CI Matrix**
- 新增 `.github/workflows/ci.yml`：Python 3.10/3.13 × Go 1.22/1.26 矩陣（4 × 2 = 8 組合）
- 4 個主 jobs：lint（文件+工具格式）、python-tests（pytest + coverage）、go-tests（threshold-exporter）、lint-docs（SAST + doc 品質）
- pip/Go module 緩存策略、coverage artifacts 產生、失敗時自動 debug log 產出

**Coverage Gate 強制**
- `pyproject.toml` 新增 `fail_under = 85`，CI 強制 `--cov-fail-under=85` 執行
- README.md 新增 CI badge 與 coverage badge（green ≥85%、yellow 80–85%、red <80%）
- Python 工具預期整體覆蓋率 ≥85%

**Python 型別系統加強**
- `_lib_constants.py`、`_lib_io.py`、`_lib_validation.py`、`_lib_prometheus.py` 加入完整型別提示
- 新增 `mypy.ini`：strict mode for all `_lib_*` modules、relaxed mode for test files
- CI lint job 新增 `mypy scripts/tools/_lib_*.py --config-file=scripts/tools/mypy.ini` 步驟

**Integration + Snapshot 測試**
- `tests/test_tool_exit_codes.py`（parametrized）：全部 84+ 工具的 `--help` + invalid args exit code 合約測試
- `tests/test_pipeline_integration.py`：scaffold → validate → routes 完整 pipeline 端對端測試
- `tests/test_snapshot.py`：help output stability snapshot tests，支援 `--snapshot-update` CI 模式

**Pre-commit Hook 驗證確認**
- 確認 13 個 auto-run hooks + 7 個 manual-stage hooks 全部運作，Phase .a–.c 新增項目完全涵蓋
- `make pre-commit-audit` 新增 make 目標印出 hook 清單與觸發規則

---

## [v2.2.0] — 採用管線 + UX 升級 + 運維工具 (2026-03-17)

v2.2.0 聚焦三大主題：降低採用門檻的 Adoption Pipeline、Portal 互動體驗全面升級、配置運維新工具。新增 2 個 CLI 工具、3 個互動工具、Portal 三大 Tab 重構、24 個 Template Gallery 模板、5-tenant 展演腳本與 Hands-on Lab。

### 採用管線（Phase A — Adoption Pipeline）

- **`da-tools init`** — 專案骨架一鍵產生：CI/CD pipeline（GitHub Actions / GitLab CI）、`conf.d/` 目錄（含 `_defaults.yaml` + tenant YAML）、Kustomize overlays、`.pre-commit-config.da.yaml`，支援 `--non-interactive` 自動模式
- **GitOps CI/CD 整合指南** (`docs/scenarios/gitops-ci-integration.md`) — 三階段管線（Validate → Generate → Apply）、ArgoCD / Flux 整合、PR Comment Bot 工作流
- **Kustomize Overlays** — `configMapGenerator` 模式產生 threshold-config ConfigMap

### UX 升級（Phase B — Portal & Templates）

**Self-Service Portal 重構（3 Tab）**
- **Tab 1 (YAML Validation)**: Rule Pack 多選 → metric autocomplete → 動態 sample YAML 產生 → 即時驗證（含 pack-aware metric key 交叉檢查）
- **Tab 2 (Alert Preview)**: Pack-grouped 滑桿、視覺化閾值條、disabled/no-threshold 狀態顯示、severity dedup 說明
- **Tab 3 (Routing Trace)**: Metric+severity 輸入 → Alert origin → Inhibit check → 四層合併 → Domain Policy check → 通知派送 → NOC 副本

**Template Gallery 擴充（6 → 24 模板）**
- 7 場景模板：ecommerce、iot-pipeline、saas-backend、analytics、enterprise-db、event-driven、search-platform
- 13 Quick Start 模板：每個可選 Rule Pack 各一
- 4 特殊模板：maintenance、routing-profile、finance-compliance、minimal
- View mode 切換（All / Scenarios / Quick Start）+ Pack filter chips + Coverage summary

**新增互動工具**
- **CI/CD Setup Wizard** (`cicd-setup-wizard.jsx`) — 5 步精靈產生 `da-tools init` 命令：CI Platform → Deploy Mode → Rule Packs → Tenants → Review & Generate（第 27 個 JSX 工具）
- **Notification Template Previewer** (`notification-previewer.jsx`) — 6 種 receiver 通知預覽（Slack / Email / PagerDuty / Webhook / Teams / Rocket.Chat）+ Dual-Perspective annotation 展示 + Severity Dedup 說明（第 28 個）
- **Platform Health Dashboard** (`platform-health.jsx`) — 平台健康儀表板：元件狀態、租戶概覽、Rule Pack 使用分佈、Reload 事件時間線（第 29 個）

**展演與教學**
- **Demo Showcase** (`scripts/demo-showcase.sh`) — 5-tenant 完整展演腳本（prod-mariadb / prod-redis / prod-kafka / staging-pg / prod-oracle），7 步驟自動執行，支援 `--quick` 模式
- **Hands-on Lab** (`docs/scenarios/hands-on-lab.md`) — 30–45 分鐘 Docker-based 實戰教程，8 個練習覆蓋 init → validate → routes → routing trace → blast radius → three-state → domain policy

### 運維工具（Phase C — Operations）

- **`da-tools config-history`** — 配置快照與歷史追蹤：`snapshot` / `log` / `show` / `diff` 子命令，`.da-history/` 存儲，SHA-256 變更偵測，git-independent 輕量級版本控制

### 漸進式遷移 Playbook

- **`docs/scenarios/incremental-migration-playbook.md`** — 四階段雙軌並行遷移法（Strangler Fig Pattern）：Phase 0 Audit（`onboard` + `blind-spot`）→ Phase 1 Pilot（單一 domain 影子部署）→ Phase 2 Dual-Run（`shadow-verify` 品質比對）→ Phase 3 Cutover（逐 domain 切換）→ Phase 4 Cleanup。每步有 CLI 指令、預期輸出、回退方式
- **`architecture-and-design.md` §2.13** — 新增效能架構說明：Pre-computed Recording Rule vs Runtime Aggregation 的 PromQL 對比，解釋為什麼 tenant 增加不會導致 Prometheus CPU/Memory 暴增

### GitOps Native Mode

- **`da-tools init --config-source git`** — 產生 git-sync sidecar Kustomize overlay，threshold-exporter 直接從 Git 倉庫讀取配置，省去 ConfigMap 中間層。支援 SSH / HTTPS 認證、自訂分支與路徑。git-sync sidecar 寫入 emptyDir shared volume，threshold-exporter 的既有 Directory Scanner + SHA-256 hot-reload 機制無縫復用
- **`da-tools gitops-check`** — GitOps Native Mode 就緒度驗證工具，三個子命令：`repo`（Git 倉庫可達性 + 分支驗證）、`local`（本地 conf.d/ 結構驗證）、`sidecar`（K8s git-sync 部署狀態檢查），支援 `--json` 和 `--ci` 模式
- **Container Image Security Hardening** — 三層防護：base pin + build-time upgrade + attack surface reduction
  - threshold-exporter：`alpine` → `distroless/static-debian12:nonroot`（零 CVE，無 shell/apk/openssl）
  - da-tools：`python:3.13-alpine` → `python:3.13.3-alpine3.22` multi-stage build（修復 CVE-2025-48174, CVE-2025-15467）
  - da-portal：`nginx:1.28-alpine` → `nginx:1.28.0-alpine3.22` + `apk del libavif gd libxml2`（移除未使用 library，消除掃描器 false positive）

### 數字

| 項目 | v2.1.0 | v2.2.0 | 變化 |
|------|--------|--------|------|
| Python 工具 | 73 | 77 | +4 |
| da-tools CLI 命令 | 27 | 36 | +9 |
| JSX 互動工具 | 26 (+1 wizard) | 29 | +3 |
| Template Gallery 模板 | 6 | 24 | +18 |
| 場景文件 | 6 | 9 | +3 |
| Makefile targets | — | +1 (`demo-showcase`) | NEW |

---

## [v2.1.0] — 運維自助 + 告警智能化 + 性能優化 + 跨域路由 (2026-03-16)

v2.1.0 自 v2.0.0 起的全量升級。涵蓋 Go Exporter 增量熱載入、告警關聯分析、跨域路由架構 (ADR-006/007)、生態整合 (Backstage Plugin)、5 個新 CLI 工具、3 個互動工具、測試 +75%（1,759 → 3,070）、文件治理與正確性全面校正。

### Go Exporter 核心

**Incremental Hot-Reload (§5.6)**
- per-file SHA-256 index + parsed config cache，WatchLoop 增量重載路徑
- `ConfigManager` 新增 `fileHashes` / `fileConfigs` / `fileMtimes` 欄位
- `scanDirFileHashes()` — mtime guard + 輕量 hash check（mtime 未變直接跳過 I/O）
- `IncrementalLoad()` — 比對 per-file hash → 只重新解析 changed/added files → `mergePartialConfigs()`
- `fullDirLoad()` — 完整載入並初始化 cache（首次載入或 fallback）
- `applyBoundaryRules()` — 提取為獨立函式供共用
- **效能優化**：logConfigStats 取代 Resolve()、mtime guard、incremental merge（tenant 檔變動直接 patch）、byte cache（scan 快取復用，免除重複 I/O）
- 15 個 Go tests + 5 個 benchmarks（含 NoChange / OneFileChanged / ScanHashes / MergePartials）

**程式碼品質**
- 4 處 error print 修正為 `stderr` 輸出
- `parsePromDuration` / `isDisabled` / `clampDuration` 新增單元測試
- Go test 增加 config_test.go（801 行）+ config_bench_test.go（268 行）+ main_test.go（97 行）

### 跨域路由架構（ADR-006 + ADR-007）

**ADR-006: Tenant Mapping Topologies (1:1, N:1, 1:N)**
- 資料面映射方案：Prometheus Recording Rules 實現 1:N 映射（exporter 零修改）
- `generate_tenant_mapping_rules.py` — 讀取 `_instance_mapping.yaml`，產出 Recording Rules（36 tests）
- `scaffold_tenant.py` 新增 `--topology=1:N`、`--mapping-instance`、`--mapping-filter` 參數（9 tests）
- 範例設定檔 `_instance_mapping.yaml`

**ADR-007: Cross-Domain Routing Profiles**
- 四層合併管線：`_routing_defaults` → `routing_profiles[ref]` → tenant `_routing` → `_routing_enforced`
- `generate_alertmanager_routes.py` 擴展：profile 解析 + `check_domain_policies()` 驗證（21 tests）
- `scaffold_tenant.py` 新增 `--routing-profile` 參數
- 重構 `_parse_config_files()` → `_parse_platform_config()` + `_parse_tenant_overrides()` 子函式
- 範例設定檔 `_routing_profiles.yaml`、`_domain_policy.yaml`

**ADR-007 工具生態**
- `explain_route.py` — 路由合併管線除錯器：四層展開、`--show-profile-expansion`、`--json`、da-tools CLI 整合（25 tests）
- `check_routing_profiles.py` — CI lint 工具：未知 profile ref、孤立 profile、格式錯誤 constraints、`--strict` 模式（28 tests + pre-commit hook）

### 新增 CLI 工具

- **`da-tools test-notification`** — 6 種 receiver 連通性測試（webhook/slack/email/teams/pagerduty/rocketchat），Dry-run / CI gate / per-tenant 批次。57 tests，97% 覆蓋率
- **`da-tools threshold-recommend`** — 基於歷史 P50/P95/P99 的閾值推薦引擎，純 Python 統計，信心等級分級。54 tests，96% 覆蓋率
- **`da-tools alert-correlate`** — 告警關聯分析：時間窗口聚類 + 關聯分數 + 根因推斷，支援線上/離線模式。95% 覆蓋率
- **`da-tools drift-detect`** — 跨叢集配置漂移偵測：SHA-256 manifest 比對，pairwise 多目錄分析 + 修復建議。99% 覆蓋率
- **`da-tools explain-route`** — 路由合併管線除錯器（ADR-007），25 tests

### 生態整合

- **Backstage Plugin**：`components/backstage-plugin/` TypeScript/React plugin
  - `DynamicAlertingPage` + `DynamicAlertingEntityContent`
  - `PrometheusClient` API 層：via Backstage proxy 查詢 threshold / silent_mode / ALERTS
  - Entity 整合：`dynamic-alerting.io/tenant` annotation → 自動對應租戶

### 互動工具

- **Multi-Tenant Comparison** (`multi-tenant-comparison.jsx`)：Heatmap 色彩矩陣 + Outlier detection + Divergence Ranking（第 25 個 JSX 工具）
- **Alert Noise Analyzer** (`alert-noise-analyzer.jsx`)：MTTR 計算、震盪偵測、去重空間分析、Top noisy alerts（第 26 個）
- **ROI Calculator** (`roi-calculator.jsx`)：Rule 維護 / Alert Storm / Time-to-Market 三模型成本分析（第 27 個）

### DX Tooling

- **`check_frontmatter_versions.py`** — frontmatter version 全域掃描 + `--fix` 自動修復（29 tests）
- **`coverage_gap_analysis.py`** — per-file 覆蓋率排行報表（22 tests）
- **`check_bilingual_content.py`** — 雙語內容 CJK 比例 lint
- **`check_doc_links.py`** — 跨語言對應檔案驗證
- **`validate_all.py`** 增強：`--notify`（桌面通知）、`--diff-report`（CI 失敗自動 diff）
- **`generate_rule_pack_stats.py --format summary`** — Badge 風格單行輸出
- **Snapshot tests v2** — alert_correlate、drift_detect、bilingual_content 快照測試

### 安全加固

- SAST 規則擴充：6 rules 自動掃描（encoding + shell + chmod + yaml.safe_load + credentials + dangerous functions），189 patterns
- NetworkPolicy 精細化、container security context 強化
- 憑證掃描 + `.env` 防護 + `os.chmod 0o600` 補齊
- **CVE 緩解**：CVE-2025-15467 (openssl CVSS 9.8 pre-auth RCE) + CVE-2025-48174 (libavif buffer overflow)
  - 所有 Dockerfile 加入 `apk --no-cache upgrade` 拉取安全修補
  - `da-tools` base image pin 從 `python:3.13-alpine` → `python:3.13.2-alpine3.21`
- **CI Image Scanning**：release workflow 三個 image 均加入 Trivy 掃描（CRITICAL + HIGH 阻斷）

### 品質閘門

- Pre-commit hooks：12 → **13** 個 auto-run（新增 `routing-profiles-check`）
- `build.sh` 修補：新增遺漏的 `alert_correlate`、`notification_tester`、`threshold_recommend` 打包

### 測試覆蓋率

Python 測試總數從 v2.0.0 的 1,759 提升至 **3,070**（+75%）。v2.1.0 新增工具均達 95%+ 覆蓋率，5 個既有工具從 41–74% 提升至 63–99%。Coverage gate 維持 `fail_under=64`，實際整體覆蓋率高於此基線。

### 數字

| 項目 | v2.0.0 | v2.1.0 | 變化 |
|------|--------|--------|------|
| Python 工具 | 62 | 73 | +11 |
| da-tools CLI 命令 | 23 | 27 | +4 |
| JSX 互動工具 | 24 | 26 (+1 wizard) | +3 |
| ADRs | 5 | 7（006/007 Accepted） | +2 |
| Python 測試 | 1,759 | 3,070 | +1,311 |
| Pre-commit hooks | 12 + 5 manual | 13 + 5 manual | +1 |
| Go tests (new files) | — | +3 files (1,166 lines) | NEW |

### Benchmark — Incremental Hot-Reload（Go, `-count=3` median）

| Benchmark | ns/op | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| IncrementalLoad_NoChange_10 | 165,700 | 34,272 | 176 |
| IncrementalLoad_NoChange_1000 | 1,528,000 | 2,027,264 | 13,085 |
| IncrementalLoad_OneFileChanged_10 | 220,600 | 73,280 | 241 |
| IncrementalLoad_OneFileChanged_1000 | 6,908,000 | 6,652,880 | 22,211 |
| ScanDirFileHashes_1000 | 1,206,000 | 1,985,200 | 13,012 |

### 文件治理與正確性

**Root README (zh/en) 增強**
- 開頭改為問題導向定位（規則膨脹 + 變更瓶頸），新增「適用場景」聲明與版本 badge
- 「關鍵設計決策」表新增 ADR 連結欄 + Sentinel 三態控制、四層路由合併兩行
- Quick Start 下方新增「生產部署」指引，文件導覽新增 Day-2 Operations 路徑

**ADR 生命週期更新**
- ADR-006/007：`📋 Proposed` → `✅ Accepted (v2.1.0)`，checklist 改為實作摘要 + 後續方向
- ADR-004：「現況與後續方向」替代舊 Roadmap 段落
- ADR-001/003：新增 v2.1.0 living-doc 狀態行
- ADR-002/004：新增「相關決策」交叉引用（ADR-005/006）

**architecture-and-design.md**
- 新增 §2.12 Routing Profiles 與 Domain Policies（四層合併管線 Mermaid 圖）
- 「本文涵蓋內容」補上三態模式、Dedup、路由系統
- 拆分文件導覽表移除過時 §N 前綴
- ADR-006 工具引用、Rule Pack 數量修正、雙語 annotation 章節翻譯

**benchmarks.md 重構**
- §8（Alertmanager Idle-State）合併至 §10（Under-Load）作為 baseline 比較表
- §13（pytest-benchmark）去重：移除與 §7 重複的 route generation 行
- 傳統方案估算加註推算基礎（per-rule ~0.3ms / ~60KB）
- 自引用修正、相關資源連結格式修正

**docs/README.md (zh/en) 去重**
- 移除與 root README 重複的「工具速查」22 行表 → 精簡為摘要 + 連結
- 移除重複的「快速命令」和「版本與維護」段落

**Component README 修正**
- threshold-exporter：斷裂 §11.1 引用 → 指向 `gitops-deployment.md`
- da-tools：版號表 `v2.0.0` → `v2.1.0`，移除過時措辭
- da-portal：`24 JSX tools` → `26`，image tag `v2.0.0` → `v2.1.0`
- backstage-plugin：移除不存在的 `(§5.13)` 引用

**交叉引用修正**
- ADR-006 (zh/en)：`§2.6` → `§2.3`（Tenant-Namespace 映射模式）
- ADR README (zh/en)：ADR-006/007 badge 更新為 ✅ Accepted

### 🐛 Bug Fixes

- 修復 `entrypoint.py` help text 遺漏 `validate-config` 命令
- 4 處 Python error output 修正為 stderr
- `da-tools build.sh` TOOL_FILES 補齊遺漏工具

---

## [v2.0.0] — Alert Intelligence + Full-Stack DX Overhaul (2026-03-15)

v2.0.0 正式版。自 v1.11.0 起的全量升級：76 個 commits、346 個檔案變更（+73,057 / -12,023）。涵蓋 Go Exporter 增強、Rule Pack 擴展、告警智能化、互動工具生態、文件全面重構、測試工程化、專案結構正規化。

> **版號說明**：v1.12.0 / v1.13.0 / v2.0.0-preview 系列皆為開發中版本（無 Git tag / GitHub Release），統一於 `v2.6.0` 正式釋出。

### 🔧 Go Exporter 增強

**Tenant Profiles（四層繼承）**
- Go schema 新增 `Profiles map[string]map[string]ScheduledValue` 欄位
- `applyProfiles()` fill-in pattern：Load 階段展開 profile 至 tenant overrides（僅填入未設定的 key）
- `_profiles.yaml` boundary enforcement：LoadDir 限制 profiles 只能從該檔載入
- `ValidateTenantKeys()` 擴展：`_profile` 引用不存在的 profile → WARN
- 繼承順序：Global Defaults → Profile → Tenant Override（tenant 永遠勝出）
- 13 個新 Go 測試案例

**Dual-Perspective Annotation**
- `platform_summary` annotation：Alert 同時攜帶 Platform 視角（NOC）和 Tenant 視角 summary
- 與 `_routing_enforced` 整合：NOC 收到 `platform_summary`，tenant 收到原始 `summary`

### 📦 Rule Pack 擴展（13 → 15）

- **JVM Rule Pack** (`rule-pack-jvm.yaml`)：GC pause rate、heap memory usage、thread pool — 7 alert rules（含 composite `JVMPerformanceDegraded`）
- **Nginx Rule Pack** (`rule-pack-nginx.yaml`)：active connections、request rate、connection backlog — 6 alert rules
- Projected Volume 13 → 15 ConfigMap sources，scaffold_tenant / metric-dictionary 同步更新

### 🚀 告警智能化（3 個新工具 + 1 個 Self-Service Portal）

**Alert Quality Scoring (`da-tools alert-quality`)**
- 4 項品質指標：Noise（震盪偵測）、Stale（閒置 14 天）、Resolution Latency（flapping 警告）、Suppression Ratio
- 三級評分（GOOD/WARN/BAD）+ per-tenant 加權分數（0–100）
- 輸出：text / `--json` / `--markdown`，CI gate：`--ci --min-score 60`
- 57 個測試，89.8% 覆蓋率

**Policy-as-Code (`da-tools evaluate-policy`)**
- 宣告式 DSL：10 種運算子（required / forbidden / gte / lte / matches / one_of ...）
- `when` 條件式、萬用字元目標（`*_cpu`）、dot-path 嵌套（`_routing.receiver.type`）
- Duration 比較、tenant 排除、error/warning 雙嚴重度
- CI gate：`--ci` 有 error 違規 exit 1
- 106 個測試，94.0% 覆蓋率

**Cardinality Forecasting (`da-tools cardinality-forecast`)**
- 純 Python 線性回歸（無 numpy）：趨勢分類（growing/stable/declining）+ 風險等級（critical/warning/safe）
- 觸頂天數預測 + 預計日期，可設基數上限（`--limit`）和預警天數（`--warn-days`）
- CI gate：`--ci` 有 critical 風險 exit 1
- 61 個測試，93.5% 覆蓋率

**Tenant Self-Service Portal (`self-service-portal.jsx`)**
- 三分頁 SPA：YAML 驗證（schema + routing guardrails）、告警預覽（滑桿模擬）、路由視覺化（樹狀圖）
- 瀏覽器端執行，零後端依賴，雙語支援（zh/en）

**Self-Hosted Portal (`da-portal` Docker image)**
- `ghcr.io/vencil/da-portal` — nginx:alpine 靜態 image，打包 24 JSX tools + Hub + Guided Flows + vendor JS
- 企業內網 / air-gapped 部署：`docker run -p 8080:80`，免 build step
- Volume mount 客製化：`platform-data.json`、`flows.json`、`nginx.conf`（含 Prometheus reverse proxy placeholder 解決 CORS）
- CI/CD：`portal/v*` tag 觸發 `release.yaml` 自動 build + push GHCR

### 🛠️ DX 自動化工具（+8 個新工具）

**Operations**
- **`shadow_verify.py`**：Shadow Monitoring 三階段驗證（preflight / runtime / convergence）
- **`byo_check.py`**：BYO Prometheus & Alertmanager 整合驗證（取代手動 curl + jq）
- **`grafana_import.py`**：Grafana Dashboard ConfigMap 匯入（sidecar 掛載 + verify + dry-run）
- **`federation_check.py`**：多叢集 Federation 整合驗證（edge / central / e2e 三模式）

**Scalable Configuration Governance**
- **`assemble_config_dir.py`**：Sharded GitOps 組裝工具 — 多來源 conf.d/ 合併、SHA-256 衝突偵測、assembly manifest
- **`da_assembler.py`**：ThresholdConfig CRD → YAML 輕量 controller（Watch / One-shot / 離線渲染 / Dry-run）
- **ThresholdConfig CRD**（`dynamicalerting.io/v1alpha1`）：namespace-scoped + RBAC + printer columns

**DX 工具迭代**
- `validate_all.py`：`--profile` + `--watch`（CSV timing trend）、`--smart`（git diff → affected-check 自動跳過）
- `bump_docs.py`：`--what-if`（全 238 rules 審計）
- `generate_cheat_sheet.py` / `generate_rule_pack_stats.py`：`--lang zh/en/all` 雙語
- `check_doc_freshness.py`：false-positive 修正 + `--fix`
- `check_translation.py`：cross-dir + lang fix
- `check_includes_sync.py`：`--fix`（自動建立缺失 .en.md stub）

### 🎯 互動工具生態（0 → 24 JSX tools）

**工具矩陣**：23 個位於 `docs/interactive/tools/` + 1 個 `docs/getting-started/wizard.jsx`
- Config：Playground、Lint、Diff、Schema Explorer、Template Gallery
- Rule Pack：Selector、Matrix、Detail、PromQL Tester
- 運維：Alert Simulator/Timeline、Health Dashboard、Capacity Planner、Threshold Calculator
- 學習：Architecture Quiz、Glossary、Dependency Graph、Runbook Viewer、Onboarding Checklist
- 展示：Platform Demo、Migration Simulator、CLI Playground、Self-Service Portal

**基礎設施**
- **tool-registry.yaml**（單一真相源）→ `sync_tool_registry.py`（`make sync-tools`）自動同步 Hub 卡片 + TOOL_META + JSX frontmatter
- **platform-data.json**（共用資料源）：從 Rule Pack YAML 萃取（15 packs, 139R + 99A），JSX 工具 fetch 共用
- **jsx-loader.html**：瀏覽器端 JSX transpiler + `TOOL_META`（related footer）+ `__PLATFORM_DATA` 預載 + Guided Flow 模式
- **tool-consistency-check**（pre-commit）：Registry ↔ Hub ↔ TOOL_META ↔ JSX ↔ MD 一致性驗證

**Guided Flows**
- `flows.json` 多步引導流程（onboarding / tenant-setup / alert-deep-dive），`?flow=onboarding` 啟動
- Cross-step data（`__FLOW_STATE` + sessionStorage）、progress persistence、completion tracking
- Conditional steps + checkpoint validation（`__checkFlowGate()` Next 按鈕閘門）
- Custom flow builder：`?flow=custom&tools=...` Hub 互動式 builder，24 工具全覆蓋
- Flow analytics：進度條、完成率、drop-off 步驟偵測

### 🌐 Bilingual Annotations (i18n)

- **Rule Pack 雙語 annotation**：`summary_zh` / `description_zh` / `platform_summary_zh` — 三個 Pilot Pack（MariaDB, PostgreSQL, Kubernetes）
- **Alertmanager template fallback**：Go `or` function 優先中文、自動 fallback 英文（所有 receiver 類型）
- **CLI i18n**：`detect_cli_lang()` 偵測 `DA_LANG`/`LANG` → argparse help 雙語切換（23 個 CLI 命令）
- **check_bilingual_annotations.py**：Rule Pack 雙語覆蓋率驗證（pre-commit manual stage）

### 📄 文件全面重構

**結構重組**
- architecture-and-design.md 拆分為 6 個專題文件（benchmarks / governance-security / troubleshooting / migration-engine / federation-integration / byo-prometheus-integration）
- 3 個角色入門指南（for-platform-engineers / for-domain-experts / for-tenants）zh/en
- 全面雙語化：33 → 46 對 `.en.md` 文件
- MkDocs Material 站點：CJK 搜尋、tags、i18n 切換、abbreviation tooltips
- Glossary（30+ 術語）+ 5 ADRs + JSON Schema（VS Code 自動補全）

**內容修訂**
- 根 README (zh/en) 重寫：角色導向痛點敘事（Platform / Tenant / Domain / Enterprise）
- architecture-and-design.en.md：補 §2.3 Tenant-Namespace Mapping、修 §3.1（15 packs + `prometheus-rules-*` 命名）、補 Bilingual Annotations
- Benchmarks 重寫：5 輪實測數據統一採集（idle + under-load + routing + alertmanager + reload）
- 6 份文件精簡（avg -23%）：移除過時內容、手動 curl 改為 da-tools CLI 引用
- Scenario CLI 修正：`tenant-lifecycle.md` (zh/en) 修正 4 個不存在的 CLI flags
- Tool-map 重生成：62 個工具完整覆蓋（之前僅 18 個）

**文件 CI 工具鏈（13 tools）**
- `validate_mermaid.py` / `check_doc_links.py` / `check_doc_freshness.py` / `doc_coverage.py`
- `add_frontmatter.py` / `doc_impact.py` / `check_translation.py` / `check_includes_sync.py`
- `sync_glossary_abbr.py` / `sync_schema.py` / `generate_cheat_sheet.py` / `inject_related_docs.py`
- `validate_all.py`：統一驗證入口

### 🔒 Security Audit & Hardening

- **程式碼安全**：ReDoS 防護（regex 長度限制）、URL 注入白名單、SSRF scheme 白名單（http/https only）、Prototype pollution 過濾（`__proto__`/`constructor`）、YAML 100KB 上限、`os.chmod` 補齊
- **文件安全加固**：HTTP→HTTPS 範例、webhook 驗證升為 error、`--web.enable-lifecycle` 安全註解、Grafana 密碼警告、新增「生產環境安全加固」章節

### 🏗️ 專案結構正規化

- **scripts/tools/ 三層子目錄化**：62 個工具分入 `ops/`（30）、`dx/`（18）、`lint/`（13）+ root（1 + 1 lib）
  * Docker flat layout 相容（dual sys.path + build.sh 自動 strip）
- **JSX 工具搬遷**：22 個工具 `docs/` → `docs/interactive/tools/`，registry/flows/loader/hub 路徑同步
- **測試歸位**：`test_assemble_config_dir.py`、`test_da_assembler.py`、`test_flows_e2e.py` 統一搬入 `tests/`
- **generate_tool_map.py 重寫**：自動掃描 ops/dx/lint/root 子目錄

### 🧪 測試工程化（14 輪系統化重構）

| 項目 | v1.11.0 | v2.0.0 | 變化 |
|------|---------|--------|------|
| 測試檔案 | 5 | 40 | +35 |
| 測試數量 | ~790 | 1,759 | +969 |
| Go 測試 | 97 | 110 | +13 |
| Coverage gate | 無 | 64%（`setup.cfg`） | NEW |
| Test markers | 無 | 5（slow/integration/benchmark/regression/snapshot） | NEW |
| Factories | 無 | 12（`factories.py` + `PipelineBuilder`） | NEW |

**關鍵里程碑**：
- Wave 5-6：pytest 遷移、SAST 掃描器（189 rules）、整合測試
- Wave 7-8：property-based tests（Hypothesis）、snapshot tests（18 JSON）、coverage gate
- Wave 9-10：factories 拆分、domain policy、deepdiff structured diff
- Wave 11-12：unittest→pytest batch migration、metric_dictionary fixture
- Wave 13：conftest re-export cleanup、duplicate removal、factory docstrings
- Wave 14-16：parametrize、scaffold snapshots、benchmark baseline、validate_all coverage
- Wave 17：coverage attack — baseline_discovery（31→55%）、backtest_threshold（32→70%）、batch_diagnose（49→71%）
- Wave 18：parametrize sweep — 合併重複測試方法

### 🛡️ 品質閘門

- **Pre-commit hooks**：0 → 12 個 auto-run + 5 個 manual-stage（schema / translation / flow E2E / jsx-babel / i18n coverage）
- **新增 hooks**：`tool-map-check`、`doc-map-check`、`rule-pack-stats-check`、`glossary-check`、`changelog-lint`、`version-consistency`、`includes-sync`、`platform-data-check`、`repo-name-check`、`tool-consistency-check`、`structure-check`、`doc-links-check`
- **Docker CI 修正**：build.sh 自動 strip sys.path hack + 觸發路徑 `**/*.py` + 3 個遺漏工具打包修正
- **Conventional Commits** + `generate_changelog.py` 自動化

### 📦 Dependency Upgrades

- **Prometheus**: v2.53.0 → v3.10.0（PromQL 相容性已驗證，15 個 Rule Pack 無影響）
- **Alertmanager**: v0.27.0 → v0.31.1
- **configmap-reload**: v0.14.0 → v0.15.0
- **Grafana**: 11.1.0 → 12.4.1
- **kube-state-metrics**: v2.10.0 → v2.18.0
- **Go**: 1.22 → 1.26.1（go.mod + Dockerfile + CI）
- **Frontend CDN**: React 18.2.0 → 18.3.1、Babel 7.23.9 → 7.26.4、Lucide 0.383.0 → 0.436.0

### 📊 Numbers

| 項目 | v1.11.0 | v2.0.0 | 變化 |
|------|---------|--------|------|
| Rule Packs | 13 | 15 | +2 |
| Python 工具 | ~20 | 62 | +42 |
| da-tools CLI 命令 | 20 | 23 | +3 |
| JSX 互動工具 | 0 | 24 | +24 |
| 文件（docs/ .md） | ~20 | 68 | +48 |
| 雙語文件對 | 0 | 46 | +46 |
| Python 測試 | ~790 | 1,759 | +969 |
| 測試檔案 | 5 | 40 | +35 |
| Pre-commit hooks | 0 | 12 + 5 manual | +17 |
| Docker images | 2 | 3 | +1 (da-portal) |

### 📈 Benchmark（v2.0.0，15 Rule Packs，Kind 叢集）

**Idle-State（2 tenant，237 rules，43 rule groups）：**

| 指標 | v1.11.0 (13 packs) | v2.0.0 (15 packs) | 變化 |
|------|-------|-------|------|
| Total Rules | 141 | 237 | +96 |
| Rule Groups | 27 | 43 | +16 |
| Eval Time / Cycle | 20.3ms | 23.2ms | +2.9ms |
| p50 per-group | 1.23ms | 0.39ms | 改善 |
| p99 per-group | 6.89ms | 4.89ms | 改善 |
| Prometheus CPU | 0.014 cores | 0.004 cores | — |
| Prometheus Memory | 142.7MB | 112.6MB | — |
| Exporter Heap (×2 HA) | 2.4MB | 2.2MB | — |
| Active Series | ~6,037 | 6,239 | +202 |

**Go Micro-Benchmark（Intel Core 7 240H，`-count=5` median）：**

| Benchmark | ns/op (median) | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| Resolve_10Tenants_Scalar | 12,209 | 26,488 | 61 |
| Resolve_100Tenants_Scalar | 100,400 | 202,777 | 520 |
| Resolve_1000Tenants_Scalar | 1,951,206 | 3,848,574 | 5,039 |
| ResolveAt_10Tenants_Mixed | 34,048 | 40,052 | 271 |
| ResolveAt_100Tenants_Mixed | 405,797 | 462,636 | 2,622 |
| ResolveAt_1000Tenants_Mixed | 5,337,575 | 5,258,548 | 26,056 |
| ResolveAt_NightWindow_1000 | 5,404,213 | 5,223,925 | 25,056 |
| ResolveSilentModes_1000 | 86,700 | 186,086 | 10 |

**Route Generation Scaling（Python `generate_alertmanager_routes.py`）：**

| Tenants | Wall Time | Routes | Inhibit Rules |
|---------|-----------|--------|---------------|
| 2 | 181ms | 3 | 2 |
| 10 | 196ms | 8 | 10 |
| 50 | 248ms | 41 | 50 |
| 100 | 327ms | 80 | 100 |

---

> **歷史版本 (v0.1.0–v1.11.0)：** 詳見 [`CHANGELOG-archive.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CHANGELOG-archive.md)
