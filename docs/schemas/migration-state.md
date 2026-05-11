---
title: "Migration State Schema (.da/migration-state.json)"
tags: [schema, migration, multi-system, automation]
audience: [platform-engineers, sre, automation]
version: v2.7.0
lang: zh
---

# Migration State Schema (`.da/migration-state.json`)

> **SSOT 演進**：本檔案是 schema 的手寫 SSOT（v1）。**未來會由 da-tools 程式碼 auto-generate 覆蓋本文件**——`components/da-tools/app/migration_state.py` Pydantic model + CI hook（v2.9 epic）。在程式碼 SSOT ship 之前，本檔 manually maintained，CI cross-check 不可用。

---

## 用途

`da-tools onboard --analyze` 的 Phase 0 discovery 輸出。**Dual output**:

| 形式 | 路徑 | 給誰 |
|---|---|---|
| JSON | `.da/migration-state.json`（commit 進客戶 GitOps repo）| 機器讀（後續 phase 自動化、cutover candidate selector、CI gate）|
| Markdown summary | stdout / `migration-summary.md` | 人類讀（PR description、ops review、stakeholder broadcast）|

兩個輸出**從同一個 internal state 派生**——保證一致。

---

## 為什麼 dual output（automation closure 觀點）

如果只產 Markdown，後續 Phase 3 的 cutover candidate selector / 自動清理腳本就**沒法讀**孤兒規則清單，automation 閉環斷掉。JSON 給機器、Markdown 給人類，是 SRE 工具鏈正確姿勢。

---

## Schema v1（outline）

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-05-10T14:00:00Z",
  "generated_by": "da-tools onboard --analyze v2.8.0",

  "discovery": {
    "tier_a_static": {
      "rule_files_scanned": 42,
      "rules_total": 380,
      "syntax_errors": [],
      "orphan_rules": [
        {"name": "MyAlert", "file": "rules.yaml", "reason": "no matching receiver"}
      ],
      "rules_with_no_route": [],
      "receivers_unused": [],
      "tenant_id_violations": []
    },
    "tier_b_live_snapshot": {
      "available": true,
      "captured_at": "2026-05-10T13:55:00Z",
      "currently_firing_count": 7,
      "active_routes_hit": 12,
      "alerts_by_severity": {"critical": 2, "warning": 5}
    },
    "tier_c_historical": {
      "available": false,
      "reason": "Customer has no Thanos / VM-long-retention / ELK"
    }
  },

  "current_state": {
    "phase": "0_discovery",
    "plan_choice": null,
    "started_at": "2026-05-10T14:00:00Z",
    "completed_phases": []
  },

  "scope": {
    "clusters": [
      {"name": "staging-us-east", "stage": "0_discovery"},
      {"name": "prod-us-east", "stage": "0_discovery"},
      {"name": "prod-us-west", "stage": "0_discovery"}
    ],
    "tenants_total": 145,
    "rule_packs_targeted": ["mysql", "postgres", "redis"],
    "metric_split_planned": true
  },

  "gate_log": []
}
```

---

## 欄位語意（outline）

### Top-level
- **schema_version** — semver `"<major>.<minor>"`. v0.1 outline = `"1.0"`. Major bump = breaking field changes.
- **generated_at** — ISO 8601 UTC.
- **generated_by** — tool + version stamp.

### `discovery.tier_a_static`
- **rule_files_scanned** / **rules_total** — counts; sanity for client.
- **syntax_errors** — list of `{file, line, message}`. Hard gate: must be 0.
- **orphan_rules** — rule with no matching AM receiver (will silent-drop alerts).
- **rules_with_no_route** — opposite: rule fires, no route configured.
- **receivers_unused** — receiver defined but no rule routes to it.
- **tenant_id_violations** — hardcoded tenant id (dev-rule #2 violation).

### `discovery.tier_b_live_snapshot`
- **available** — false if Prom unreachable / no permission. Don't block.
- **currently_firing_count** — sanity for "what's actively noisy".
- **active_routes_hit** — distinct AM routes that resolved at snapshot time.

### `discovery.tier_c_historical`
- **available** — typically false (most customers).
- 若 available 則 `lookback_days` + 各 alert 的 fire/resolve histogram.

### `current_state`
- **phase** — enum `0_discovery | 1_preflight | 2_shadow | 3_cutover_canary | 3_cutover_full | 4_decommission`.
- **plan_choice** — `A | B | null`. Null until Phase 1 starts.
- **completed_phases** — append-only list（後續 Phase 自動 advance）.

### `scope`
- **clusters** — list with per-cluster X-Y matrix position（`stage` 對應該 cluster 的當前 phase；正交於整體 `current_state.phase`）.
- **rule_packs_targeted** — which Rule Packs (Mysql / Postgres / Redis...) the customer plans to import.
- **metric_split_planned** — boolean. True triggers `_defaults.yaml` adoption in Phase 4.

### `gate_log`
- 列已通過的 Gate：`{gate_id, passed_at, criteria_met}`. 後續 phase 機械讀此 log 決定能否 advance.

---

## 演進路線

### v1 (本 outline)
- 手寫 schema definition
- da-tools 寫 Python dict → JSON dump
- CI 不檢查 drift（風險 acceptable，schema 早期）

### v2（v2.9 backlog）
- Pydantic model 在 `components/da-tools/app/migration_state.py`
- `da-tools onboard --analyze` import model + dump
- CI hook：model schema → JSON Schema → 對比本 .md → drift fails
- **本 .md 變成 generated artifact**（標 `<!-- AUTO-GENERATED, DO NOT EDIT -->`）

---

## 關聯

- **使用者**：[multi-system-migration-playbook §3](../scenarios/multi-system-migration-playbook.md#3-phase-0-discovery-inventory) Phase 0
- **schema 系列**：[docs/schemas/](README.md)（其他 schema 文件）
- **Future SSOT**：v2.9 backlog（待開 issue）

---

## Storage Layout（推薦：per-cluster file split）

> **避免 X-Y matrix 場景下的 git merge conflict**——多 cluster 並行推進不同 phase 時，所有 automation 寫入同一檔會造成 GitOps repo 永無止境的衝突。

### 推薦：per-cluster split

```
.da/
├── state/
│   ├── staging-us-east.json
│   ├── prod-us-east.json
│   └── prod-us-west.json
└── manifest.json           # 列舉所有 state 檔
```

**好處**：
- staging 推 Phase 4 時、prod 推 Phase 2 不互相 commit conflict
- 每個 cluster ops team 改自己的檔、PR 不交叉
- automation 寫 `--cluster-state .da/state/$CLUSTER.json` 即可單檔讀寫

### Manifest 檔（discovery 用）

```json
{
  "schema_version": "1.0",
  "states": [
    {"cluster": "staging-us-east", "path": ".da/state/staging-us-east.json"},
    {"cluster": "prod-us-east", "path": ".da/state/prod-us-east.json"},
    {"cluster": "prod-us-west", "path": ".da/state/prod-us-west.json"}
  ]
}
```

### 例外：單檔模式

只在以下情境才合理：
- **單 cluster 部署**（最常見的 small-scale customer）— 直接寫 `.da/migration-state.json`
- **強 CI auto-rebase + retry 機制**已就位 — automation 對 push conflict 有明確 retry path

若採單檔，**CI 必須具備**：

1. write 前 `git pull --rebase`（pick up parallel writes）
2. push 失敗 retry（exponential backoff，max 5 retries）
3. retry 仍失敗 → 暫存 state diff、人工 reconcile

否則建議直接走 per-cluster split。

### Tools 慣例

`da-tools onboard --analyze` 預設 `--output .da/state/<cluster-name>.json`（從 `--cluster-name` flag 推），不再 default 到 single file。客戶單 cluster 不指定也仍 work（fallback to `.da/state/default.json`）。

