---
title: DX Tooling Backlog
lang: zh
---

# DX Tooling Backlog

> 已完成的平台功能與 DX 工具改善追蹤。
> 與產品 Roadmap（`architecture-and-design.md` §5）分開管理：§5 只放 forward-looking 方向。

## 狀態說明

- **候選** — 已識別但尚未排入
- **進行中** — 當前 iteration 正在實作
- **完成** — 已合併，標註完成 Round

---

## 候選

### validate_all.py `--diff-report`

Check fail 時自動產出 unified diff（expected vs actual），不用手動跑 generator 再 diff。

### check_doc_freshness.py Helm chart 版號檢查

目前只檢查 Docker image 版號，擴展到 `helm install/upgrade` 命令中的 `--version` flag 比對。

### generate_rule_pack_stats.py `--format summary`

產出單行摘要格式（如 `15 packs, 139 rec, 99 alert`），可嵌入 CLAUDE.md / README 作為 badge-like 引用。

### validate_all.py `--notify`

完成後發送桌面通知（配合 long-running `--watch` 模式），使用 OS native notification。

### check_doc_links.py 跨語言連結驗證

驗證 zh doc 引用的 en doc 確實存在（反之亦然），目前只做同語言內部連結。

### ~~check_doc_links.py 加入 pre-commit~~ ✔ R11 完成

已加入 `.pre-commit-config.yaml`，攔截壞掉的 Related Resources 連結。

### CLI 命令覆蓋率 lint（cheat-sheet ↔ entrypoint.py COMMAND_MAP）

擴展 `check_doc_freshness.py` 或新建 lint：解析 `entrypoint.py` 的 COMMAND_MAP 作為權威來源，驗證 `cheat-sheet.md` / `cheat-sheet.en.md` / `cli-reference.md` / `cli-reference.en.md` 的命令清單完整性，攔截新增命令但忘記更新文件的情況。

### 雙語內容一致性 lint（check_bilingual_content.py）

現有 `check_translation.py` 只驗證結構（標題/程式碼/表格數量），不檢查內容。新增 lint 偵測 `.en.md` 中出現大段未翻譯中文文字（如表格描述欄整段中文），使用字元範圍偵測（CJK Unified Ideographs）。

### frontmatter 版號全域掃描

擴展 `validate_docs_versions.py`：掃描所有 `docs/**/*.md` 的 frontmatter `version:` 欄位，與 CLAUDE.md 宣告的版號比對，攔截版號 drift。目前只檢查特定檔案的版號字串。

### Interactive Tools 發想（GitHub Pages）

以下為 first-visit UX 改善過程中產生但尚未排入的互動工具構想：

**AK. Cost Estimator**
從 Capacity Planner 延伸，輸入雲端供應商（AWS/GCP/Azure）和 instance type，估算每月 Prometheus + 儲存成本。

**AL. Alert Noise Analyzer**
貼入一段時間的 alert 記錄（JSON/CSV），分析 MTTA/MTTR、top noisy alerts、dedup 有效率、建議閾值調整。

**AM. Multi-Tenant Comparison**
輸入多個 tenant YAML，橫向比較所有 tenant 的閾值設定差異、共同點和異常值（outlier detection）。

**AP. Release Notes Generator**
從兩個版本號的 CHANGELOG 段落自動產生面向不同受眾（Platform/Tenant/DBA）的精簡 release notes。

---

## 候選 — 測試覆蓋率與品質

> Wave 15-17（覆蓋率 57% → 64.4%，1419 → 1506 tests）後識別的改善項目。
> 目標：全模組 ≥ 70% 覆蓋率。

### 覆蓋率攻略 — 剩餘低覆蓋模組

目前 < 70% 的模組（依優先順序）：

| 模組 | 現況覆蓋率 | 主要缺口 | 難度 |
|------|-----------|---------|------|
| `bump_docs.py` | 34.9% | CLI file-rewrite 邏輯，大量 IO mock 需求 | 高 |
| `validate_all.py` | 41.4% | main() sequential/parallel 模式 + --fix/--watch | 中 |
| `baseline_discovery.py` | 55.4% | main() 觀測迴圈（time.sleep + CSV 寫入） | 中 |
| `batch_diagnose.py` | 71.1% | main() ThreadPoolExecutor CLI | 低 |
| `blind_spot_discovery.py` | 74.3% | import 初始化 + CLI entry | 低 |

### ~~parametrize 大掃除~~ ✔ W18 完成

掃描所有測試檔，找出 3+ 重複 pattern 的 test method，合併為 `@pytest.mark.parametrize`。
實際影響：5 檔案、38 個 test method → 8 parametrized，減少 74 行重複碼。

### Snapshot test 擴展

為 `generate_report()` / `generate_markdown()` / `print_text_report()` 輸出加入 JSON snapshot 穩定性測試，防止格式意外變更。

### test docstring 覆蓋率 lint

新增 pre-commit hook 或 lint script，自動檢查所有 test method 是否有繁體中文 docstring。
確保測試意圖一目了然。

### conftest fixture 精簡

`routing_dir` fixture 僅 3 個檔案使用，評估是否值得內聯化。
`config_dir` fixture 維持現狀（137+ 使用處，轉 `tmp_path` 成本過高）。

### Coverage 差距分析自動化

產生 per-file coverage 排行報表，整合到 `make coverage` 或 CI 產出，方便持續追蹤 70% 達標進度。

---

## 完成紀錄

### 平台功能

| Round | 項目 | 摘要 |
|-------|------|------|
| R10 | Sharded GitOps — `assemble_config_dir.py` | SHA-256 衝突偵測、assembly manifest、多來源 conf.d/ 合併 |
| R10 | Assembler Controller — `da_assembler.py` | ThresholdConfig CRD → YAML 輕量 controller，watch / one-shot / offline render |
| R10 | ThresholdConfig CRD + RBAC | `k8s/crd/thresholdconfig-crd.yaml`、`assembler-rbac.yaml`、example CR |
| R10 | GitHub Pages Interactive Tools | `docs/interactive/index.html` + `jsx-loader.html` 改寫，瀏覽器端 JSX 載入 |
| R11 | Alert Quality Scoring (§5.3) | `alert_quality.py` — 4 指標品質評分、三級評分、CI gate、57 tests (89.8%) |
| R11 | Policy-as-Code Path A (§5.4) | `policy_engine.py` — 10 運算子 DSL、when 條件、validate_config 整合、106 tests (94.0%) |
| R11 | Cardinality Forecasting (§5.8) | `cardinality_forecasting.py` — 線性回歸趨勢預測、三級風險分類、61 tests (93.5%) |
| R11 | Tenant Self-Service Portal (§5.7) | `self-service-portal.jsx` — 三分頁 SPA（YAML 驗證 + 告警預覽 + 路由視覺化），第 24 個 JSX 工具 |

### DX 工具

| Round | 項目 | 摘要 |
|-------|------|------|
| R7 | generate_doc_map.py `--include-adr` | ADR 納入 doc-map，H1 title 萃取 |
| R7 | validate_docs_versions.py doc-file-count | 文件數自動驗證 + auto-fix |
| R7 | bump_docs.py `--what-if` | 232 rules 完整審計 |
| R8 | generate_cheat_sheet.py bilingual | `--lang zh/en/all` 雙語速查表 |
| R8 | check_doc_freshness.py false-positive fix | code-block-only 匹配 + stopword |
| R8 | check_translation.py cross-dir + lang fix | full-path pairing + empty-lang guard |
| R8 | validate_all.py `--profile` + `--watch` | CSV timing trend + file-watch polling |
| R9 | check_doc_freshness.py `--fix` | `.doc-freshness-ignore` 支援 |
| R9 | generate_rule_pack_stats.py `--lang` | `--lang zh/en/all` 雙語統計表 |
| R9 | check_includes_sync.py `--fix` | 自動建立缺失 .en.md stub |
| R9 | validate_all.py `--smart` | git diff → affected-check 自動跳過 |

### 測試重構

| Wave | 項目 | 摘要 |
|------|------|------|
| W11 | parametrize 瘦身 | test-map 建立、gitignore hygiene |
| W12 | unittest→pytest 批次遷移 | 4 檔案遷移 + metric_dictionary fixture + doc-map 瘦身規則 |
| W13 | conftest re-export 清理 | 重複測試移除、factory docstrings |
| W14 | test-map sync + parametrize | scaffold snapshots、benchmark baseline |
| W15 | docstring 覆蓋率 + 2 檔案遷移 | analyze_gaps 53→83%、assemble_config_dir +11 CLI tests |
| W16 | unittest 全面掃蕩 | 15 檔案轉換、validate_all 14→41%、fail_under 59→61 |
| W17 | 覆蓋率攻略×3 | baseline_discovery 31→55%、backtest_threshold 32→70%、batch_diagnose 49→71%、fail_under 61→64 |
| W18 | parametrize 大掃除 | 5 檔案 38 methods → 8 parametrized，-74 行重複碼，1519 tests 全過 |
