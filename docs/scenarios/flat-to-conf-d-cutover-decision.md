---
title: "Flat → conf.d/ Cutover Decision Guide"
date: 2026-04-28
audience: platform-ops, sre
verified-at-version: v2.8.0
---

# 從扁平 `tenants/` 遷移到階層 `conf.d/` — 決策指南

> 配套文件：
> - **遷移逐步操作手冊** → [`incremental-migration-playbook.md`](incremental-migration-playbook.md)
> - **退版程序** → `incremental-migration-playbook.md` §Emergency Rollback Procedures（[文件](incremental-migration-playbook.md)）
> - **遷移工具** → `scripts/tools/dx/migrate_conf_d.py` (`--dry-run` / `--apply`)
> - **架構設計理由** → [`docs/adr/017-conf-d-directory-hierarchy-mixed-mode.md`](../adr/017-conf-d-directory-hierarchy-mixed-mode.md)

---

## 1. 你需要決定的事

**你是否該從扁平 `tenants/` layout 遷移到階層 `conf.d/<domain>/<region>/<env>/` layout？什麼時候遷？**

平台**支援兩種 layout 並存**（mixed mode），但這只是**遷移過程的暫態**，不該是長期穩定態。本文件幫你回答：

1. 我**現在**該不該遷？（決策矩陣，§2）
2. 遷移會經歷的中間狀態**有什麼坑**？（已知 gap，§3）
3. mixed mode 跑起來**慢多少**？（§4 量化資料）
4. 客戶 cutover 期間，**支援契約**是什麼？（§5）

---

## 2. 決策矩陣：要不要遷？

| 你的情況 | 建議 | 理由 |
|---|---|---|
| **< 50 tenant，無組織分群需求** | ⏸️ **不遷** | hierarchical 帶來的 cascading defaults / blast-radius scope 收益 < 你維護兩種 layout 認知成本 |
| **50-200 tenant，單一 BU/team 管理** | 🟡 **可遷可不遷** | tipping point — 看你是否預期下個季度跨 region 擴。若是，先遷 |
| **200+ tenant，多 BU/region/env 分群** | 🟢 **遷** | cascading defaults 省的 YAML 行數 + blast-radius 的 scope 訊號是 hard wins |
| **客戶端有 GitOps PR 流程**（C-10 batch-pr 規劃中）| 🟢 **遷** | hierarchy-aware chunking 預設按 domain 切 PR，扁平 layout 只能單一 mega-PR |
| **預期下個 quarter 引入跨 region/env defaults**（threshold 因 region 不同）| 🟢 **遷** | flat 沒地方放 region 級 `_defaults.yaml`；遷 cost 只增不減，越早越好 |
| **客戶端 `_defaults.yaml` 一年內幾乎不動**（純 flat 寫死 thresholds）| ⏸️ **不遷** | hierarchical 的核心收益是 cascading 改動 — 用不到就沒收益，只剩維護成本 |

### 2.1 不遷的副作用

留在扁平 layout 平台**完全支援**（v2.7.0 起雙模式並存無 EOL 計劃），但你會錯失：

- **Cascading defaults**：threshold 改一處 → 整個 region/env 受影響的 tenants 全部更新；扁平要逐 tenant 改
- **Blast-radius scope 訊號**：alerts dashboard 上 `da_config_blast_radius_tenants_affected{scope=domain}` 在扁平 mode 永遠是 `tenant`，看不出影響範圍
- **GitOps PR 自然分塊**：扁平大型變更只能單一 PR，hierarchy-aware chunking 自動按 domain 切多個小 PR
- **遷移時的 hierarchy-aware rollback**（B-4）：incremental migration playbook 的反序退版表預設 hierarchy；扁平只能逐 PR revert

### 2.2 遷的代價

| 一次性成本 | 持續性成本 |
|---|---|
| Cutover 期間 mixed mode 掃描變慢（§4 量化）| `_metadata.{domain,region,environment}` 必須在每個 tenant YAML 維護 |
| 多人協作時 conflict surface 變大（多目錄 `_defaults.yaml`）| GitOps merge 衝突可能跨多目錄，需要 `da-tools batch-pr` 工具協助 (Phase .c C-10) |
| 一次性 staging rehearsal（B-4 hard gate）| Operator 要會看 `defaultsPathLevel` 推算受影響 tenants |

---

## 3. Mixed-mode 已知行為與 gap

### 3.1 受支援的行為（locked by tests in `config_mixed_mode_test.go`）

✅ **Root `_defaults.yaml` 適用兩邊**：扁平與階層 tenant 都繼承根目錄 defaults

✅ **Mid-level defaults scope 正確**：`<root>/finance/_defaults.yaml` 改動只影響 finance/ 下的 nested tenants；扁平 tenants 不會出現在 `blast_radius{scope=domain}` 計數

✅ **Hot migration 安全**：`os.Rename` 把扁平 tenant 移到 nested + 加 mid-level `_defaults.yaml`，下次 `diffAndReload` 自動更新該 tenant 的 defaults chain，無 duplicate error

✅ **Sticky `hierarchicalMode`**：一旦 `_defaults.yaml` 被偵測到，模式不會回退到扁平掃描——避免「不小心刪掉中間層 defaults 就丟失 nested tenants」的 footgun

### 3.2 已知 gap（v2.8.0 待強化）

❌ **跨 mode duplicate tenant ID 是 WARN 不是 hard error**

如果同一個 tenant ID 同時出現在 `<root>/<id>.yaml`（扁平）和 `<root>/<dir>/<id>.yaml`（階層），manager 行為：

1. `populateHierarchyState` 內部的 `scanDirHierarchical` **正確偵測** duplicate 並回 error，包含**兩個檔案路徑**的 message
2. 但 `Load()` 把該 error 打成 `WARN: hierarchical scan during Load failed: ...` 然後**繼續執行**（[config.go L194](../../components/threshold-exporter/app/config.go))
3. 扁平 mode `loadDir()` 在此前已成功，靜默 last-wins-merge 把 duplicate 收掉
4. 結果：`Load()` 回傳 `nil` error，tenant 從**其中一個檔案**留下（map iteration order 決定，不可預期）

**目前 mitigation**：

- 任何 cutover 期間 grep `journalctl -u threshold-exporter | grep "WARN: hierarchical scan during Load failed"`
- `da-tools tenant-verify <id> --conf-d conf.d/` 能印出 tenant 來源檔案路徑——若兩個檔案都列出，duplicate 確實存在
- `migrate_conf_d.py --dry-run` 在規劃階段就能偵測同 tenant 出現在兩處

**長期 fix（排在 v2.8.x 強化 PR）**：把 hierarchical scan 的 duplicate error **propagate 為 hard error**，讓 `Load()` fail-fast。`config_mixed_mode_test.go::TestMixedMode_DuplicateAcrossModes_DetectedButNotPropagated` 鎖死目前 WARN-only 行為——強化 PR land 時需把 test 改寫為「Load 必須回 error」，這是該 PR 必須觸碰的訊號。

❌ **`_metadata.{domain,region,environment}` 沒有從路徑自動推斷**

扁平 tenant 在根目錄沒有 path-derived metadata。產生效果：

- alerts 標籤上 `domain` / `region` / `env` 是空的 → dashboard 群組可能漏掉這些 tenants
- 唯一推斷路徑是 `migrate_conf_d.py`（讀 tenant YAML 內已寫的 `_metadata` block 推斷目標目錄）

**目前 mitigation**：mixed mode 期間，扁平 tenants 的 `_metadata` block 要由 customer 顯式維護（不是新規範——這是 v2.7.0 起的契約，只是 cutover 期間特別容易疏忽）。

---

## 4. Mixed-mode 效能特徵

### 4.1 預期 degradation

planning §B-5 設「mixed mode 與同 tenant 數的 pure hierarchical 比較，degradation **≥ 10%** 即觸發 follow-up 改善 PR」。

當前量測（v2.8.0 dev container, n=1, **single-shot, NOT statistically valid**——只用於檢驗方向，不是承諾數字）：

| Benchmark | Pure-hier 1000T 基線 | Mixed 500flat+500hier | 比例 |
|---|---|---|---|
| `ScanDirHierarchical` | ~51ms (v2.8.0 PR #59) | ~107ms | ~2.0× ⚠️ |
| `FullDirLoad` | ~237ms (v2.8.0 PR #59) | ~437ms | ~1.85× ⚠️ |
| `DiffAndReload_NoChange` | ~189ms (v2.8.0 PR #59) | ~583ms | ~3.0× ⚠️ |

⚠️ **觸發 §B-5 follow-up threshold**——數字暫定，待 nightly `bench-record.yaml` 累積 28 點後 `analyze_bench_history.py` 跑統計才為定論。但若數字趨近這個範圍，**mixed mode 確實是「儘快走完 cutover」的場景，不適合長駐**。

### 4.2 為什麼慢

initial guess（待 perf profile 確認）：

1. **L0 root 同時 host 扁平 tenant 檔 + 子目錄項**：`scanDirHierarchical` walk root 時遇到混合 entry，一些 cache locality 假設失效
2. **`fullDirLoad` 在 diffAndReload 尾段**：archive S#27 finding 1 已知問題——這個 cost 在混合 mode 多了個面向（root 多了 500 個檔要 parse）
3. **per-tenant defaults chain 長度不一**：扁平 tenant chain=1 (root only)，nested chain=2 (root + L1 domain)；compute path 多了 branch

### 4.3 Cutover 期間性能監控建議

```bash
# Watch reload duration p99 — 若 mixed mode 期間飆 > 1s 持續 5 min，
# 可能就是 mixed-mode degradation 在 production 變現
sum(rate(da_config_reload_duration_seconds_sum[5m])) /
  sum(rate(da_config_reload_duration_seconds_count[5m])) > 1

# Watch reload trigger rate — git-sync cadence 變化時這個會抖動
sum(rate(da_config_reload_trigger_total[5m])) by (reason)
```

---

## 5. Cutover 過程支援契約

### 5.1 客戶可預期

| 階段 | 平台保證 | 客戶責任 |
|---|---|---|
| Cutover 前（pre-flight）| `migrate_conf_d.py --dry-run` 印出**所有**待移動檔案 + 缺 `_metadata` 警告 | 跑 dry-run + 補完 metadata |
| Cutover 期間（mixed mode 暫態）| 兩種 layout 並存 OK，root defaults cascade、blast-radius scope 正確 | 監看 §4.3 PromQL；duplicate ID grep WARN |
| Cutover 完成（pure hierarchical）| 性能回到 baseline，所有 cascading + blast-radius features 全可用 | 跑 staging rehearsal 退版測試（B-4 hard gate）|

### 5.2 升級 / 退版安全

- **升級**：`migrate_conf_d.py --apply` 走 `git mv`，每個檔案獨立 commit；任何時刻 `git revert` 部分 commit 即可暫停
- **完全退版**：`incremental-migration-playbook.md` §Emergency Rollback Procedures（[文件](incremental-migration-playbook.md)） §「退版順序」表格 + `da-tools tenant-verify --all --json > pre-base.json` 拍快照 + 反序 revert + 驗證 checklist

### 5.3 Customer escalation triggers

下列任一情況直接升 vencil-on-call：

1. `WARN: hierarchical scan during Load failed: duplicate tenant ID` 連續出現 > 1 hour（duplicate 沒被清掉）
2. `da_config_reload_duration_seconds` p99 持續 > 5s（顯著超出 mixed-mode 預期 degradation）
3. `da_config_parse_failure_total` 任何 `_*` 前綴 file_basename 的 increment（_defaults.yaml 解析失敗會 silently drop 整個 block，cycle-6 RCA 已 codify 為 ERROR-level + metric，但客戶可能沒設 alert）
4. Cutover 期間 alert 觸發行為變化（例：原本 fire 的 alert 突然不 fire）— 大機率是 mixed mode 期間 `domain`/`region` label 漏寫

---

## 6. 相關文件

- ADR-017：[`conf.d/` directory hierarchy + mixed mode 決策](../adr/017-conf-d-directory-hierarchy-mixed-mode.md)
- ADR-018：[Defaults YAML inheritance + dual-hash hot-reload](../adr/018-defaults-yaml-inheritance-dual-hash.md)
- 遷移工具：`scripts/tools/dx/migrate_conf_d.py`
- 遷移操作手冊：[`incremental-migration-playbook.md`](incremental-migration-playbook.md)
- B-1 Phase 1 baseline 量測：[`benchmark-playbook.md` §v2.8.0 1000-Tenant Hierarchical Baseline](../internal/benchmark-playbook.md)（internal）
