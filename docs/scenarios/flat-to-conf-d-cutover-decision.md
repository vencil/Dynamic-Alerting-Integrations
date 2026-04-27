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
2. 但 `Load()` 把該 error 打成 `WARN: hierarchical scan during Load failed: ...` 然後**繼續執行**（`components/threshold-exporter/app/config.go` L194）
3. 扁平 mode `loadDir()` 在此前已成功，靜默 last-wins-merge 把 duplicate 收掉
4. 結果：`Load()` 回傳 `nil` error，tenant 從**其中一個檔案**留下（map iteration order 決定，不可預期）

**目前 mitigation**：

- 任何 cutover 期間 grep `journalctl -u threshold-exporter | grep "WARN: hierarchical scan during Load failed"`
- `da-tools tenant-verify <id> --conf-d conf.d/` 能印出 tenant 來源檔案路徑——若兩個檔案都列出，duplicate 確實存在
- `migrate_conf_d.py --dry-run` 在規劃階段就能偵測同 tenant 出現在兩處

**長期 fix（已開 [issue #127](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/127) 追蹤，排在 v2.8.x 強化 PR）**：把 hierarchical scan 的 duplicate error **propagate 為 hard error**，讓 `Load()` fail-fast。`config_mixed_mode_test.go::TestMixedMode_DuplicateAcrossModes_DetectedButNotPropagated` 鎖死目前 WARN-only 行為——強化 PR land 時需把 test 改寫為「Load 必須回 error」，這是該 PR 必須觸碰的訊號。

❌ **`_metadata.{domain,region,environment}` 沒有從路徑自動推斷**

扁平 tenant 在根目錄沒有 path-derived metadata。產生效果：

- alerts 標籤上 `domain` / `region` / `env` 是空的 → dashboard 群組可能漏掉這些 tenants
- 唯一推斷路徑是 `migrate_conf_d.py`（讀 tenant YAML 內已寫的 `_metadata` block 推斷目標目錄）

**目前 mitigation**：mixed mode 期間，扁平 tenants 的 `_metadata` block 要由 customer 顯式維護（不是新規範——這是 v2.7.0 起的契約，只是 cutover 期間特別容易疏忽）。

---

## 4. Mixed-mode 效能特徵

### 4.1 預期 degradation — 量測待定

planning §B-5 設「mixed mode 與同 tenant 數的 pure hierarchical 比較，degradation **≥ 10%** 即觸發 follow-up 改善 PR」。

**目前 dev container 量測 inconclusive**——n=3 single-shot 的數字過度受 fixture-create 成本（once.Do 1000 yaml 寫入）污染，且 mixed fixture 的 cascading defaults 數量（9 個 `_defaults.yaml` = 1 root + 8 L1）遠少於 pure-hier 1000T 的 201 個（L0+L1+L2+L3 完整 cascading），post-warmup 比較反而看到 mixed 在某些 op 上更快。

**Authoritative numbers gated on nightly bench-record**——已開 [issue #128](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/128) 追蹤把 4 個 mixed-mode benchmarks 加進 nightly workflow，累積 28+ data points 後 `analyze_bench_history.py` 才能下定論。在那之前，**本節暫不發表具體數字**——避免單次量測 artifact 變成 customer 引用的「事實」。

### 4.2 等量測 land 後預期會看到的 trade-offs（hypothesis）

待 §4.1 nightly data 抵達後驗證／推翻：

1. **Hypothesis A：mixed mode 的 ScanDir 較慢** — `scanDirHierarchical` walk root 遇到混合 entry（檔案 + 子目錄）比純 nested 多一些 branch；單 op cost 增量小（毫秒級），但每 reload tick 都會打到
2. **Hypothesis B：mixed mode 的 FullDirLoad/DiffAndReload 反而較快** — mixed fixture 的 cascading defaults 通常未滿四層（L0+L1，少 L2/L3），整體 parse 成本低於 fully-cascaded pure-hier。換句話說：「**mixed mode 性能特徵高度依賴 cascading defaults 的密度**，不是 layout 本身慢**」
3. **Hypothesis C：相對 ratio 取決於 fixture shape** — 若客戶的 mixed mode 在 cutover 中 **同時** 引入更多層 cascading defaults，degradation 可能浮現；若 cutover 維持 minimal defaults 但 reorganize 檔案 tree，可能反而更快

實際結果見 [issue #128](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/128) acceptance criteria 結束後 update 本節。

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
