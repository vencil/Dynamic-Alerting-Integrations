# Project Structure Audit — 2026-04-12

> 1,501 files / 177 directories（排除 .git / node_modules / .venv）

---

## 一、結構評估：可改善的痛點

### 1. `scripts/` 路徑層級不一致

目前有兩套 scripts 根：

```
scripts/ops/          ← 2 files（git_check_lock.sh, vscode_git_toggle.py）
scripts/tools/ops/    ← 47 files（核心運維工具）
scripts/tools/dx/     ← 23 files
scripts/tools/lint/   ← 37 files
```

`scripts/ops/` 和 `scripts/tools/ops/` 名稱重疊，容易混淆。CLAUDE.md 提到 `scripts/session-guards/` 路徑（main PR #21 已規劃搬遷但尚未完成）。

**建議**：把 `scripts/ops/` 的 2 個 session guard 腳本搬到 `scripts/session-guards/`（PR #21 已部分做）。最終結構：

```
scripts/
├── session-guards/    ← vscode_git_toggle.py, git_check_lock.sh
└── tools/
    ├── ops/           ← 47 核心運維工具
    ├── dx/            ← 23 DX 工具
    └── lint/          ← 37 lint 工具
```

### 2. `components/threshold-exporter/` 混合 app + Helm chart（審查後：維持現狀）

threshold-exporter 的 Go 原始碼和 Helm chart 放在同一個目錄，其他 Helm charts 都在 `helm/`。

**審查結論**：這是**刻意的架構選擇**。release.yaml 直接從 `components/threshold-exporter/` 打包 chart（line 90），CODEOWNERS 也指向此路徑。exporter 的 config/ + chart templates 有緊密耦合，co-location 是合理的。搬遷會牽動 CI + CODEOWNERS + Makefile。

**建議**：維持現狀，但在 `components/threshold-exporter/README.md` 加一句說明 chart co-location 的設計理由，避免新人疑惑。

### 3. `operator-output/` 是生成物（審查後：保留但加護欄）

14 個 `da-rule-pack-*.yaml`，與 `rule-packs/rule-pack-*.yaml` 一一對應。這是 `operator_generate.py --output-dir operator-output/` 的產出。

**審查結論**：`migration-guide.md` 和 `cli-reference.md` 的標準工作流是 `operator_generate.py → kubectl apply -f operator-output/`，屬於 **GitOps 工作流的一部分**（使用者跑工具、commit 產出、apply 到 cluster）。不適合 `.gitignore`。

**建議**：加一個 `operator-output/README.md` 標明「此目錄為 `operator_generate.py` 自動產出，勿手動編輯」+ 在 pre-commit 加一個 drift check 確保 `operator-output/` 與 `rule-packs/` 同步。

### 4. `docs/interactive/tools/` 有 portal-shared.js 和 portal-shared.jsx 兩個版本

- `portal-shared.js`（602 行）— IIFE 模式，`window.__portalShared` 全域掛載
- `portal-shared.jsx`（590 行）— React 模組化重寫

兩個檔案功能重疊但語法不同。tool-registry.yaml 裡沒有登記 portal-shared。

**審查結論**：`portal-shared.jsx` 被 `self-service-portal.jsx` 的 `dependencies:` 引用、`doc-map.md` 登記、`testing-playbook.md` 提及。`portal-shared.js`（IIFE 版）**零引用**——是 v2.3.0 模組化重寫前的遺留。

**建議**：刪除 `portal-shared.js`，保留 `portal-shared.jsx`。

### 5. 根目錄 ignore 檔案偏多（7 個）——低優先

```
.gitignore / .claudeignore / .docorphan-ignore / .doclinkignore
.doc-freshness-ignore / .changelog-lint-ignore / .validation-profile.csv
```

**審查結論**：每個 ignore 檔案對應一個獨立的 lint 工具，合併需要改寫所有工具的讀取邏輯，ROI 不高。這是成熟 doc pipeline 的正常代價。

**建議**：降為 P3。短期只需確保 `doc-map.md` 的 Change Impact Matrix 有列出這 7 個檔案的用途，新人能查到即可。

### 6. `docs/internal/` 混合永久文件和臨時文件

`_resume-*` 和 `v2.5.0-v2.6.0-planning.md` 是暫態性質（已 .gitignore），但與 playbook、dev-rules 等永久文件放一起。

**建議**：在 `.gitignore` 已覆蓋的前提下可接受。若覺得 internal/ 太擁擠，可考慮 `docs/internal/scratch/` 子目錄放暫態檔，但 ROI 不高。

### 7. 96 個 Python 測試全平鋪在 `tests/` 根

`tests/` 下有 96 個 `test_*.py`，全部在同一層。對應的 source 分散在 `scripts/tools/ops/`、`dx/`、`lint/`。

**建議**：短期可維持（pytest 配置簡單），但超過 100 個時考慮按 source 結構分子目錄：

```
tests/
├── ops/
├── dx/
├── lint/
├── e2e/        ← 已存在
└── snapshots/  ← 已存在
```

### 8. `wizard.jsx` 位置不一致

`docs/getting-started/wizard.jsx` 是唯一不在 `docs/interactive/tools/` 的 JSX 工具，但 tool-registry 有登記（`file: getting-started/wizard.jsx`）。其他 37 個工具都在 `docs/interactive/tools/`。

**建議**：搬到 `docs/interactive/tools/wizard.jsx` 並更新 registry，保持一致。或在 registry 加 comment 說明此工具刻意放在 getting-started 旁邊。

### 9. Interactive tools 子元件未登記但無問題

`AlertPreviewTab.jsx`、`RoutingTraceTab.jsx`、`YamlValidatorTab.jsx` 不在 tool-registry 中，但它們是 `self-service-portal.jsx` 的 `dependencies:`，不是獨立工具。`portal-shared.jsx` 同理。這是正確的——registry 只登記獨立入口點。無需改動。

### 10. `roi-calculator.jsx` vs `migration-roi-calculator.jsx` 不是重複

兩者 diff 1115 行（各約 680 行），是不同受眾的獨立工具：前者面向 platform-engineer 做採用效益試算，後者面向 SRE 做遷移成本估算。registry 分別登記。保留兩者。

---

## 二、可能不再需要的檔案（評估去留）

### 🔴 建議刪除

| 檔案 | 理由 | 備註 |
|------|------|------|
| `_dex_check.log` | 根目錄 debug log，未 tracked | 清理即可 |
| `_dock_cmd.log` | 同上 | 清理即可 |
| `_docker_ps.log` | 同上 | 清理即可 |
| `_dockerd.log` | 同上 | 清理即可 |
| `_winpush.log` | 同上 | 清理即可 |
| `portal-shared.js` | IIFE 舊版，零引用（.jsx 版被 self-service-portal + doc-map + playbook 引用） | 直接刪除 |
| `tests/.benchmarks/` | 空目錄，無內容 | 若不影響 pytest-benchmark 可刪 |

### 🟡 建議評估

| 檔案/目錄 | 理由 | 建議 |
|-----------|------|------|
| `operator-output/`（14 files） | 自動生成物，但屬 GitOps 工作流 | ~~.gitignore~~ → 加 README 標注 + drift check |
| `docs/internal/ssot-language-evaluation.md`（23K, status: draft） | 語言策略評估文件，如已做完決策可歸檔 | 確認決策是否已 finalized，是 → archive/ |
| `docs/internal/design-system-guide.md`（19K） | Portal 設計系統指南，如 da-portal 未活躍開發可凍結 | 確認是否仍被參考 |
| `.build/threshold-exporter-2.6.0.tgz` | Helm chart 打包產出，已 .gitignore | 確認不需清理 |
| `docs/internal/v2.5.0-v2.6.0-planning.md`（87K） | 版本規劃文件，已 .gitignore | 大檔案但不影響 repo，可定期清理 |
| `CHANGELOG-archive.md`（67K） | v1.x 歷史記錄 | 保留但考慮壓縮或移到 wiki |

### 🟢 確認保留

| 項目 | 原因 |
|------|------|
| `docs/` 的 `.en.md` 雙語對 | 有意為之的 i18n 策略，bilingual-structure-check 依賴 |
| `tests/snapshots/`（27 files, 36K） | Snapshot testing 活躍使用 |
| `policies/examples/`（3 rego files） | OPA policy 範例，體積小 |
| `k8s/crd/examples/` | CRD 範例，有文檔參考價值 |
| `environments/`（2 files） | CI/local 環境配置，精簡但必要 |
| `docs/interactive/tools/`（42 JSX files） | Portal 互動工具 SOT，tool-registry.yaml 管理 |

---

## 三、結構改善優先序

| 優先 | 改動 | 影響範圍 | 預估工作量 | 狀態 |
|------|------|---------|-----------|------|
| P0 | 清理根目錄 5 個 `_*.log`（`.gitignore` 已有 `*.log`） | 無 | 5 min | ✅ done |
| P0 | 刪除 `portal-shared.js`（IIFE 舊版，零引用確認） | interactive tools | 5 min | ✅ done |
| P1 | `scripts/ops/` → `scripts/session-guards/` 完成搬遷 + 更新 CLAUDE.md / Makefile / playbook 引用 | CLAUDE.md, Makefile, playbook | 1 hr | ✅ done |
| P1 | `operator-output/` 加 README 標注 | pre-commit | 30 min | ✅ done（README 已加；drift check hook 留 P3） |
| P1 | `wizard.jsx` 位置加 comment 說明 | tool-registry | 15 min | ✅ done（registry 加註 co-location 理由） |
| P2 | `components/threshold-exporter/README.md` 加 Helm chart 交叉引用 | 文件 | 15 min | ✅ done（PR#21 已搬 chart 到 `helm/`） |
| P2 | `docs/internal/ssot-language-evaluation.md` status: draft → decided | 文件 | 視決策而定 | ✅ done |
| P3 | tests/ 子目錄分層（>100 files 時觸發） | pytest config, CI | 2-3 hr | 🔜 deferred |
| P3 | doc-lint ignore 在 doc-map 集中說明用途 | 文件 | 30 min | 🔜 deferred |
| P3 | CHANGELOG-archive.md 壓縮 / 移 wiki | repo size | 30 min | 🔜 deferred |
