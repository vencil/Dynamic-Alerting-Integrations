# Playbook 審核報告與實作計畫 (2026-04-12)

> **目的**：回應 poyu 提出的 7 項疑慮，對專案 Playbook 體系進行全面審核，產出具體可執行的改善計畫。
> **範圍**：5 份 Playbook + 3 份 Map + CLAUDE.md + pre-commit + C:\Users\vencs 散落腳本

---

## 一、Playbook 合理性評估與業界對標（疑慮 1）

### 1.1 做對了什麼（值得保留的模式）

**分層 Playbook 架構**：按領域切分（infra / QA / perf / release）是業界 runbook 標準做法，與 Google SRE Handbook 的 "domain-specific playbook" 思路一致。這不是歪路。

**Lesson Learned 版本化追蹤**：每條 LL 標記版本 + 日期 + 🛡️ 自動化狀態，這超越多數開源專案的做法。業界常見的是無結構的 "lessons learned" wiki，退化速度極快。專案的 `verified-at-version` + `playbook-freshness` 是正確方向。

**漸進式修復層級**（FUSE phantom lock Level 1-6）：這是 incident response 的 escalation pattern，SRE 領域標準做法。從預防 → 偵測 → 輕量修復 → 重量級修復 → 核選項，結構清晰。

**Machine-generated Maps**（doc-map / tool-map / test-map）：用腳本產生索引、pre-commit 防 drift，這是 doc-as-code 的正確實踐。

### 1.2 需要留意的偏離

**偏離 1：Playbook 同時承擔 "操作手冊" 和 "經驗日誌" 兩個角色**

業界標準做法是分開：
- **Runbook**：純操作步驟，無敘事，可機械執行（適合 AI agent）
- **Postmortem / LL Log**：敘事性質，記錄因果脈絡（適合人類回顧）

目前 testing-playbook 混合了兩者 — 既有 "K8s 排錯步驟" 也有 "v2.1.0 Backstage 整合踩坑故事"。這導致 AI agent 在搜尋操作步驟時要穿越大量敘事內容。

**建議**：不需要重構檔案結構（代價太高），但應在每份 Playbook 頂部加一個 **Quick Action Index**，用錨點連結直接跳到可執行步驟，跳過敘事。

**偏離 2：跨 Playbook 重複內容**

| 重複內容 | 出現位置 | 處置 |
|---------|---------|------|
| docker exec stdout 為空 | windows-mcp ×4 處 + testing-playbook | 保留 windows-mcp §核心原則 1 份，其餘改為錨點連結 |
| conf.d/ wrapped format | testing-playbook + test-map | 只在 testing-playbook 保留完整說明 |
| PAT 權限矩陣 | github-release + windows-mcp #25 | 只在 github-release 保留，windows-mcp 改連結 |
| PowerShell JSON 編碼 | windows-mcp + github-release | 統一為一種推薦做法（ConvertTo-Json + UTF8），刪除 single-line 方法 |

**偏離 3：缺少 "何時不需要讀 Playbook" 的指引**

CLAUDE.md 的任務分流表只說 "必讀"，沒有明確的 skip 條件。AI agent 傾向過度讀取，浪費 token。

**建議**：在分流表加 "跳過條件" 欄，例如 "純文件修改 → 不需讀 Playbook，pre-commit hooks 自動把關"。

### 1.3 結論

Playbook 架構**方向正確**，不需要流程再造。需要的是：瘦身（去重複）、加索引（Quick Action Index）、加跳過條件。

---

## 二、知識效益篩選 — 避免為寫而寫（疑慮 2）

### 2.1 篩選標準

每條 LL 必須通過以下至少一項測試，否則應歸檔或刪除：

| 測試 | 說明 |
|------|------|
| **Recurrence Test** | 這個問題是否在不同 session 重複出現過？只出現一次且已有 🛡️ 自動化的 → 歸檔 |
| **Actionability Test** | 讀完這條 LL，AI agent 能否直接改變行為？純敘事無操作指引的 → 改寫或歸檔 |
| **Freshness Test** | 跨越兩個 minor 版本未被引用 → 強制三選一（已有此規則，確認執行） |

### 2.2 具體瘦身建議

**testing-playbook.md**（499 行，最臃腫）：
- v2.1.0 Backstage Integration LL（2 條）→ Backstage 已穩定，且有 🛡️ → **歸檔至 archive/**
- v2.1.0 DX Enhancement LL（--diff-report, --format）→ 純功能記錄，不是陷阱 → **歸檔**
- v2.1.0 Lint Tool Patterns（reverse validation）→ 已 🛡️ 固化為 lint rule → **歸檔**
- 預計可瘦身 **~80 行**（16%）

**windows-mcp-playbook.md**（615 行，第二臃腫）：
- docker exec stdout 重複 4 次 → 保留 1 次 + 3 處改錨點 → 減少 **~30 行**
- Windows Clone symlink 初次設定 → 已穩定，移到 getting-started 類文件 → 減少 **~40 行**
- 預計可瘦身 **~70 行**（11%）

**效益**：Playbook 總行數從 ~1900 行降至 ~1750 行，更重要的是降低 AI agent 搜尋噪音。

---

## 三、AI 導覽與決策樹（疑慮 3）

### 3.1 現有決策樹評估

CLAUDE.md 的 **任務分流表** 是目前最關鍵的 AI 導覽機制。結構合理（任務類型 → 必讀 → 選讀 → 為什麼），但有兩個缺口：

**缺口 A：分流表只導向 "讀什麼"，沒導向 "做什麼"**

AI agent 讀完 Playbook 後，仍需自行判斷操作步驟。對於高頻操作（git commit / push / tag），應有可直接執行的決策樹。

**缺口 B：沒有 "卡住了怎麼辦" 的升級路徑**

AI agent 遇到 FUSE 問題時，Playbook 列了 Level 1-6，但沒有明確的 "嘗試 N 秒後升級" 門檻。

### 3.2 建議新增：Git 操作決策樹（放在 windows-mcp-playbook）

```
Git 操作入口
├── 1. make git-preflight（每次 git 操作前）
│   ├── 成功 → 正常操作（dev container 內）
│   └── 失敗（lock 清不掉）
│       ├── 2. make fuse-reset（等 10 秒）
│       │   ├── 成功 → 正常操作
│       │   └── 失敗
│       │       ├── 3. Windows 逃生門（見 §修復層 C）
│       │       │   └── 用 win-git-escape.bat 在 Windows 端完成操作
│       │       └── 4. 如果連 Windows 端也失敗 → 回報使用者，不要無限重試
```

### 3.3 保留合理自行發想的空間

不是所有操作都需要決策樹。以下場景應留給 AI 自行判斷：
- 程式碼邏輯修改（architecture decision 不適合機械化）
- 測試策略選擇（哪些 marker、多少 coverage）
- LL 撰寫（判斷是否值得記錄）

**原則**：操作型流程（git / release / benchmark）→ 給決策樹；思考型任務（設計 / 除錯 / 撰寫）→ 給原則 + 範例，不給步驟。

---

## 四、Git 三層架構與逃生門設計（疑慮 4）

### 4.1 確認設計思路

poyu 的思路完全正確且應被明確寫入 Playbook：

> **主路徑**：Dev Container 層做所有事（code / test / commit / push）
> **逃生門**：Windows 環境是備援，只在 FUSE 卡死時使用，目標是不讓整個 session 卡死

這個思路目前散落在 windows-mcp-playbook §修復層 C，但不夠顯眼。建議：

1. 在 CLAUDE.md 的 "最常踩的 5 個坑" 區塊加入明確聲明
2. 在 windows-mcp-playbook 頂部加一行設計原則

### 4.2 三層環境職責矩陣（新增至 Playbook）

| 操作 | 主路徑：Dev Container | 備援：Cowork VM | 逃生門：Windows Native |
|------|---------------------|----------------|---------------------|
| Code editing | ✅ | ✅ Read+Edit | ❌ |
| Go test | ✅ | ❌ | ❌ |
| Python test | ✅ | ✅ | ❌ |
| git add/commit | ✅ | ⚠️ FUSE 風險 | ✅ 逃生門 |
| git push | ✅ | ⚠️ FUSE 風險 | ✅ 逃生門 |
| git tag | ✅ | ⚠️ FUSE 風險 | ✅ 逃生門 |
| gh pr create | ✅ | ❌ gh 不在 VM | ✅ 逃生門 |
| pre-commit | ✅ | ✅ | ❌ 環境不完整 |
| Helm / K8s | ✅ | ❌ | ❌ |

---

## 五、Windows 逃生門工具收編（疑慮 5）

### 5.1 C:\Users\vencs 盤點結果

在 C:\Users\vencs 發現 **~400 個檔案**，由多個 Claude session 各自創建。分類如下：

| 類別 | 數量 | 可收編 | 應刪除 |
|------|------|-------|-------|
| Git/GitHub .bat 腳本 | ~96 | 5-6 個通用樣板 | 其餘全部 |
| 命令輸出 .txt | ~101 | 0 | 全部 |
| CI 診斷工具 (_ci/) | ~175 | blob_cmp.py 概念 | 其餘全部 |
| PR body .md | ~8 | 0 | 全部 |
| PowerShell 工具 | ~5 | Fix-ClaudeDesktop.ps1, gitconfig-fuse-tuning | 其餘 |
| .json 設定 | ~3 | 0 | 全部 |

### 5.2 🚨 安全警報

**`C:\Users\vencs\git-push-docs.bat` 內含明文 GitHub PAT token。**

→ **立即行動**：撤銷該 PAT（GitHub Settings → Developer settings → Fine-grained tokens）

### 5.3 收編計畫

**收編進 `scripts/ops/` 的工具（3 個）：**

1. **`win_git_escape.bat`**（新建）— 整合散落的 git_branch/commit/push/tag .bat 為單一 Windows 逃生門腳本
   - 子命令：`status | add | commit | push | tag | pr-create | pr-list`
   - 不含任何 credential（使用 `gh auth` 或 `~/.git-credentials`）
   - 輸出重導至 `%TEMP%\vibe-git-*.txt`，不污染 user 目錄
   - 自動設定 `PYTHONUTF8=1` + 正確的 `PATH`

2. **`win_git_escape.ps1`**（新建）— PowerShell 版本，提供 `gh` CLI 包裝
   - 功能：`pr-create | pr-view | ci-status | release-create`
   - UTF-8 處理（`[Text.UTF8Encoding]::new($false)` 無 BOM）
   - 不含 credential

3. **`gitconfig-fuse-tuning.sample`**（已存在概念，正式化）
   - 內容：`fsmonitor=false`, `trustctime=false`, `untrackedCache=false`, `filesRefLockTimeout=1500`
   - 加入 Makefile target：`make install-fuse-gitconfig`

**不收編，改用 open source 替代：**

- `Fix-ClaudeDesktop.ps1`（455KB 的 Claude Desktop 重置腳本）→ 這是 Cowork 產品問題，不屬於專案工具範圍。記錄在 Playbook 作為 "如果需要" 的提示即可，不收編進 repo。

### 5.4 C:\Users\vencs 清理計畫

```
保留：
  .gitconfig（使用者自己的 git 設定）
  .local/bin/（工具二進位檔）
  gitconfig-fuse-tuning（收編後可刪）
  標準 Windows 使用者目錄（Desktop, Documents, Downloads 等）

刪除（確認後執行）：
  *.bat（~96 個 session 殘留腳本）
  *.txt（~101 個命令輸出）
  *.md（PR body 等暫存文件，非 Desktop/Documents 下的）
  *.json（PR metadata 暫存）
  _ci/ 整個目錄（~175 個診斷腳本）
  *.ps1（root 下的 session 腳本，.local/bin/ 保留）
```

### 5.5 防止未來重複造輪子

**根本原因**：每個 session 不知道逃生門工具已存在，因為工具散落在 C:\Users\vencs 而不在 repo 內。

**解法**：
1. 工具收編進 `scripts/ops/`（見 5.3）→ 在 repo 內，CLAUDE.md 可引用
2. 在 CLAUDE.md 任務分流表加一行：

| 任務類型 | 必讀 | 工具 |
|---------|------|------|
| FUSE 卡死需 Windows 逃生門 | windows-mcp-playbook §修復層 C | `scripts/ops/win_git_escape.bat` |

3. 在 windows-mcp-playbook §修復層 C 直接嵌入使用範例，而不是讓 agent 自己寫腳本

---

## 六、sed -i 違規的強制管制方案（疑慮 6）

### 6.1 現狀分析

目前的防線：
- **文字規範**：dev-rules.md #11（AI agent 經常忽略）
- **症狀修復**：`file-hygiene` pre-commit hook 修 NUL byte + EOF（事後補救，不防根因）
- **drift 偵測**：`head-blob-hygiene` hook 掃 HEAD tree（commit 後才發現）

問題：這些都是 **事後補救**，不是 **事前阻止**。AI agent 用 `sed -i` 改了檔案、產生了破壞，file-hygiene hook 雖然能修，但修的過程本身就可能再觸發問題（改了再改，staging area 混亂）。

### 6.2 提案：三層防禦（從 "文字" 升級到 "機制"）

**第一層：Bash wrapper 攔截（事前阻止，最關鍵）**

在 Cowork VM 的 shell 環境中，加一個 bash function 覆蓋 `sed`，偵測到 `-i` flag + 掛載路徑時直接報錯。

**放置位置**：必須放在 `/etc/profile.d/vibe-sed-guard.sh`（非 `.bashrc`）。原因：Cowork Bash 工具執行 `sh -c 'command'` 是非互動式 shell，不會 source `.bashrc`，但會 source `/etc/profile.d/*.sh`。Dev Container 內則放在 entrypoint 或 `/etc/bash.bashrc`。

```bash
# /etc/profile.d/vibe-sed-guard.sh（Cowork VM）
# 或 Dev Container 的 /etc/bash.bashrc
sed() {
  # 偵測 -i 或 -i'' 或 -i.bak 搭配掛載路徑
  local has_inplace=false
  local has_mount_path=false
  for arg in "$@"; do
    case "$arg" in
      -i|-i''|-i*) has_inplace=true ;;
      /sessions/*/mnt/*|./mnt/*) has_mount_path=true ;;
    esac
  done
  if $has_inplace && $has_mount_path; then
    echo "ERROR: sed -i on mounted path is prohibited (dev-rules #11)." >&2
    echo "Use: Read+Edit tools, or pipe: git show HEAD:file | sed '...' > file" >&2
    return 1
  fi
  command sed "$@"
}
```

**效果**：AI agent 呼叫 `sed -i /sessions/.../mnt/...` 時直接失敗，錯誤訊息包含正確做法。不需要 agent 讀文件、不需要 agent 記住規則。

**第二層：pre-commit hook 加強（事中偵測）**

現有 `file-hygiene` hook 已能修復症狀。加一個偵測 hook，專門掃 staged diff 中是否有 `sed -i` 的痕跡（NUL byte 出現在原本沒有 NUL 的檔案）：

```yaml
# .pre-commit-config.yaml 新增
- id: sed-inplace-guard
  name: "Guard: detect sed -i damage on mounted files"
  entry: python3 scripts/tools/lint/detect_sed_damage.py
  language: system
  types: [text]
```

**第三層：CLAUDE.md 強化提示（現有，微調）**

在 CLAUDE.md "最常踩的 5 個坑" 中，將 #1 的描述從被動描述改為主動指令：

```diff
- 1. **#11 檔案衛生** — 禁止對掛載路徑用 `sed -i`
+ 1. **#11 檔案衛生** — ⛔ 永遠不要用 Bash 工具執行 `sed -i`。改用 Read+Edit 工具。
+    違反時 shell 會直接報錯阻止，不需要嘗試。
```

### 6.3 效益對比

| 防禦層 | 類型 | AI agent 感知 | 可繞過性 |
|-------|------|-------------|---------|
| 文字規範（現有） | 被動 | 可能忽略 | 100%（直接用 bash） |
| file-hygiene hook（現有） | 事後修復 | commit 時才知道 | N/A（修症狀不防根因） |
| **bash wrapper（新增）** | **事前阻止** | **立即報錯 + 正確指引** | **低（需刻意繞過）** |
| sed-damage hook（新增） | 事中偵測 | commit 時報錯 | 低 |

**關鍵洞察**：對 AI agent 來說，"執行時直接失敗並給出正確做法" 比 "文件裡寫了規則" 有效 100 倍。這就是 harness 思維。

---

## 七、實作計畫（疑慮 7）

### Phase 1：安全 + 清理（本次 session 可完成）

| # | 任務 | 產出 | 優先級 |
|---|------|------|-------|
| 1.1 | 撤銷 exposed PAT | 安全修復 | 🔴 P0 |
| 1.2 | 清理 C:\Users\vencs 散落檔案 | 乾淨的 user 目錄 | 🟡 P1 |
| 1.3 | 建立 `scripts/ops/win_git_escape.bat` | Windows 逃生門工具 | 🟡 P1 |
| 1.4 | 建立 `scripts/ops/win_git_escape.ps1` | PowerShell 逃生門工具 | 🟡 P1 |

### Phase 2：Harness 強化（本次 session 可完成）

| # | 任務 | 產出 | 優先級 |
|---|------|------|-------|
| 2.1 | 建立 sed -i bash wrapper | `/etc/profile.d/vibe-sed-guard.sh` | 🔴 P0 |
| 2.2 | 建立 `detect_sed_damage.py` pre-commit hook | 新 hook 加入 `.pre-commit-config.yaml` | 🟡 P1 |
| 2.3 | 建立 `gitconfig-fuse-tuning.sample` + Makefile target | 正式化 FUSE git 調優 | 🟢 P2 |

### Phase 3：Playbook 瘦身 + 導覽優化（可分批）

| # | 任務 | 產出 | 優先級 |
|---|------|------|-------|
| 3.1 | windows-mcp-playbook 去重複（docker exec ×4 → ×1）| 減少 ~30 行 | 🟡 P1 |
| 3.2 | testing-playbook 歸檔低效益 LL | 減少 ~80 行 | 🟢 P2 |
| 3.3 | 統一 PowerShell JSON 為單一推薦做法 | 去除混淆 | 🟢 P2 |
| 3.4 | 各 Playbook 頂部加 Quick Action Index | 加速 AI 導覽 | 🟡 P1 |
| 3.5 | CLAUDE.md 分流表加 "跳過條件" + "逃生門工具" 列 | 減少不必要讀取 | 🟡 P1 |

### Phase 4：Git 決策樹 + 三層職責矩陣

| # | 任務 | 產出 | 優先級 |
|---|------|------|-------|
| 4.1 | 在 windows-mcp-playbook 加 Git 操作決策樹 | Mermaid 流程圖 | 🟡 P1 |
| 4.2 | 在 windows-mcp-playbook 頂部加三層環境職責矩陣 | 快速參考表 | 🟡 P1 |
| 4.3 | CLAUDE.md 加入 "主路徑 / 逃生門" 設計原則聲明 | 確保思路持久化 | 🟡 P1 |

### Phase 5：驗證

| # | 任務 | 方法 |
|---|------|------|
| 5.1 | sed -i wrapper 功能測試 | 在 Cowork VM 中實測 `sed -i` 被攔截 |
| 5.2 | Windows 逃生門工具功能測試 | 在 Windows 端實測 git status/commit/push |
| 5.3 | pre-commit hook 測試 | `pre-commit run --all-files` 通過 |
| 5.4 | Playbook 交叉引用完整性 | 確認所有錨點連結有效 |
| 5.5 | CLAUDE.md 分流表一致性 | 確認新工具/新 Playbook 段落已反映 |

---

## 附錄 A：本報告的判斷依據

### 業界對標參考

| 專案做法 | 業界對標 | 評估 |
|---------|---------|------|
| 分層 Playbook | Google SRE Handbook Ch.14-15 | ✅ 一致 |
| LL 版本化追蹤 + 🛡️ | Netflix Incident Review / Etsy Morgue | ✅ 超越多數 |
| Machine-generated Maps | Backstage TechDocs / Spotify Golden Path | ✅ 一致 |
| Playbook 混合操作+敘事 | PagerDuty Runbook Best Practice 建議分開 | ⚠️ 偏離 |
| FUSE Level 1-6 | Incident escalation pattern (SRE) | ✅ 正確 |
| shell wrapper 阻止危險操作 | HashiCorp Sentinel / OPA policy-as-code | ✅ 同理念 |

### 不採用的替代方案

| 方案 | 不採用原因 |
|------|-----------|
| 完全重構 Playbook 為 Runbook + LL 兩套系統 | ROI 不足：5 份 Playbook 約 1900 行，重構成本高，Quick Action Index 能解決 80% 問題 |
| 用 git hook 禁止 `sed` binary 整體 | 過度限制：`sed`（不帶 `-i`）在 pipe 中是安全的 |
| 把逃生門工具放 npm package 發佈 | 過度工程：只有這個專案需要，`scripts/ops/` 足矣 |
| 用 .gitattributes 的 filter driver 攔截 sed | 太隱晦，AI agent 看不到錯誤訊息，不符合 harness 原則 |
