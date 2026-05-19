---
title: "tenant-api 套件結構檢視 — ADR-020 epic 後"
tags: [tenant-api, refactor, architecture, decision-record]
audience: [platform-engineers, sre]
version: v2.9.0
lang: zh
status: active
domain: tenant-api
created_at: 2026-05-20
updated_at: 2026-05-20
---

# tenant-api 套件結構檢視 —— ADR-020 epic 後

> ADR-020 federation epic（[#380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380)）為 tenant-api 加上 federation 子域；#510 PR-A review 時 maintainer flag 了一張 [#537](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/537)，要求 epic 落地後重新檢視套件結構。本文是該檢視的決策紀錄。
>
> **結論：現況可接受，不做結構性 refactor。** 細節與重新檢視觸發條件見下。

## 結論摘要

| 評估項目 | 結論 | 重新檢視觸發 |
|---|---|---|
| federation handlers → `internal/handler/federation/` 子套件 | **不做** | handler 套件 LoC 接近 ~7000，或 `Deps` 介面需重大改造時順手處理 |
| `internal/federation/` 拆 token / policy / admission | **不做** | 套件 >3000 LoC，或域間出現明確 cohesion 退化 |
| GitOps writer 寫鎖競爭（Symptom 1） | **延** | PR-mode 部署 + 自助寫入併發實際觸發 `WriteTimeout` / 503 / 504 |
| GitOps writer git history 膨脹（Symptom 2） | **延** | init container 啟動延遲變顯著（>30s 等級） |

---

## 1. 背景

#510 PR-A 把約 1.5k LoC 加進 tenant-api（2-tier policy + admission validator），maintainer 觀察到 `internal/handler/` 已偏扁平，想在 #380 epic 收尾後做一次整理。但 #537 明文「to be assessed, not prescriptive」—— 結論未必是動手。

**時機**：#537 規定 epic 的 tenant-api 工作落地後再做（避免改動中靶）。檢視當下，相關工作均已收尾：
- #510（policy + admission） → PR #536 + #538 merged
- #521（offboarding orphan detector） → PR #549 merged
- #517（JWKS endpoint） → 評估確認被靜態金鑰分發機制 supersede，closed without code

federation footprint 已穩定，符合 #537 的時機要求。

## 2. 現況盤點（2026-05-20）

### `internal/handler/`

- 25 檔（不含 `*_test.go`），4562 LoC。
- 最大檔：`middleware.go` 454 / `federation_policy.go` 428 / `tenant_search.go` 398 / `group.go` 325。
- federation 兩檔（`federation.go` 228 + `federation_policy.go` 428）合計 656 LoC = **handler 套件的 14%**。
- 全部 31 個 handler 方法以 `(d *Deps)` receiver 形式存在 —— Deps 集中容器是 PR-4 刻意設計（見 `deps.go` 開頭註解），把每個 handler 從 1–8 個位置參數收斂到單一依賴注入點。

### `internal/federation/`

- 7 檔，1587 LoC。內含：
  - `federation.go` (340) — `Manager`：token 簽發 / 列出 / 撤銷
  - `store.go` (177) — `RecordStore` interface + in-memory impl
  - `configmap_store.go` (314) — ConfigMap-backed `RecordStore` (ADR-020 Posture B)
  - `mint_limiter.go` (63) — 簽發頻率限制
  - `policy.go` (222) — 2-tier policy（platform whitelist + per-tenant subset）
  - `admission.go` (278) — admission validator（Prometheus Series API）
  - `orphan.go` (193) — offboarding orphan detector

### `internal/gitops/writer.go`

- 502 LoC（主檔）。
- 寫入透過全域 `w.mu` 序列化（ADR-009 設計）。direct mode 鎖期幾十 ms（本地 `git` 子行程）；PR mode 鎖期 ~1–3 s（含遠端 `git push`）。

---

## 3. 評估與決策

### 3.1 federation handlers → `internal/handler/federation/`：**不做**

`internal/handler/` 全部 31 個 handler 是 `*Deps` 的方法。Go 語言要求 method receiver 與其 type 同套件 —— 把 federation handler 搬去子套件必須二選一：

**(a)** 把這些 handler 從 `(d *Deps) CreateFederationToken(...)` 改成自由函式 `federation.CreateFederationToken(d *handler.Deps, ...)`。call style 與其餘 26+ handler 不一致；路由註冊與測試 fixture 都得改。

**(b)** 把 `Deps` 本身搬到獨立 `internal/handler/deps/`，然後把全部 25 個 handler 檔改成依賴 `*deps.Deps`、改成自由函式 —— 動到 25 檔、整批 receiver 改寫，風險高。

兩條都跟「federation 只佔 14% 的局部結構整理」**不成比例**。`Deps` 集中容器是刻意設計，局部破壞它的價值低於成本。

**現況可接受**：4562 LoC / 25 檔在 Go 套件規模上不算大（許多正式 Go 專案套件超過 30 檔）；`tenant_*.go` / `group*.go` / `federation*.go` 的檔名前綴已提供視覺分組與 IDE 跳轉路徑。

### 3.2 `internal/federation/` 拆 token / policy / admission：**不做**

技術上可拆。自然會切成：
- `internal/federation/token/`：`federation.go` + `store.go` + `configmap_store.go` + `mint_limiter.go`（~894 LoC）
- `internal/federation/policy/`：`policy.go` + `admission.go`（500 LoC，policy 在 PUT 時呼叫 admission，原本就耦合）
- `internal/federation/orphan/`：`orphan.go`（193 LoC，依賴 store）

依賴圖無循環。但**不拆**的理由：

- 7 檔 / 1587 LoC 在 Go 套件規模上不大。
- 全部 7 檔都屬同一個域內的內聚概念（token 生命週期、政策、admission、orphan 偵測），flat 比子套件更直覺、import path 也更短（`federation.Manager` 比 `token.Manager` 更可讀）。
- 拆會多 2 個 import 邊界 → exported / internal 邊界決定、test fixture 在跨套件重複，friction 換取的邊際收益低。

### 3.3 其他 cohesion 改進

走過 handler 與 federation 套件全部檔案，**沒有發現明確需要動的整理**。檔案大小分布健康（中位數約 200 LoC，最大 454），命名前綴清楚，沒有「應該在 X 卻在 Y」的明顯 misfile。

---

## 4. Addendum —— GitOps writer 高頻 self-service 寫入

[#537 addendum](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/537) 觀察的兩個症狀源於**同一個 ADR-009 假設**：`gitops.Writer` 為**低頻 admin 編輯**（每天個位數）設計；ADR-020 IV-2e 引入**租戶自助寫入**（`PUT /tenants/{id}/federation`）在大租戶基底 × 自動化下可能差數量級。兩個症狀要視為同根問題，不可拆開處理。

### Symptom 1 —— 全域寫鎖競爭：**延**

`Writer` 用全域 `w.mu` 序列化每次寫入 —— 這是**正確**的：git index / lock 是單一共享資源，per-tenant lock 只是把競爭挪去 git 端。direct mode 鎖期是幾十 ms 本地子行程，併發塞流很短、`WriteTimeout=30s` 內吞得下。

真正暴露是 **PR mode**（ADR-011）的 `WritePR` 在鎖內含 `git push` 遠端操作（~1–3 s）。PR mode + 自助寫入併發爆量 → 排隊超時 → 503 / 504。

**優先級：低 / 條件性。** 只在 PR mode 部署 + 真實併發寫入負載下會咬；direct mode 不受影響。
**未來方向**：async write queue，或把遠端操作從鎖內移出（push 後 fire-and-forget）。
**重新檢視觸發**：PR mode production 出現可重現的 `WriteTimeout`。

### Symptom 2 —— git history 膨脹：**延**

每個 subset 寫一個 commit。高頻自助（200 租戶 × 每天 Terraform sync ≈ 200 commits/天）會穩定膨脹 `.git`。tenant-api Helm chart 的 `git-clone` init container 每次新 pod 都做 full clone，大歷史拉長啟動 / Deployment rollout。

可評估的緩解：`git clone --depth 1` shallow clone。**評估 caveat**：writer 的衝突偵測呼叫 `git rev-parse HEAD~1`（`commitParent()`），depth-1 clone 在 pod 第一次自己 commit 前**沒有 `HEAD~1`** —— 引入前必須驗證 `commitParent()` degrade gracefully（今天看起來會 graceful，但未實測）。另一條路：週期性 history compaction。

**優先級：低。** 慢燒型啟動延遲，非穩態 failure；git 對大歷史處理還算可以。
**重新檢視觸發**：init container 啟動延遲變顯著（例如 >30s），或 `.git` 大小撞 PVC quota。

---

## 5. 整體結論

ADR-020 federation epic 為 tenant-api 加了實質份量（`internal/federation/` 全新 1587 LoC、handler 端 +656 LoC），但**結構上沒有惡化到需要動刀**。`Deps` 方法模式給 handler 套件天花板，7 檔的 federation 套件還在「flat 健康」區。

GitOps writer 兩個 addendum 症狀都標 low / 條件性，在實際 production 訊號出現前不該推進 —— 「為 PR-mode 高併發 self-service 重寫 writer」是 speculative work，違反 dev-rules 對「未證實需求別預先建」的紀律。

未來重新檢視沿用本文 §3 / §4 的觸發條件，不另立新 ticket；觀察到觸發時在當下脈絡的工作 thread 提出即可。

## 關聯

- [Issue #537](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/537) — 本檢視的來源 ticket
- [Issue #380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380) — ADR-020 federation epic
- [ADR-009 — Tenant Manager CRUD API](../adr/009-tenant-manager-crud-api.md) — `gitops.Writer` 設計（commit-on-write）
- [ADR-011 — PR-based write-back](../adr/011-pr-based-write-back.md) — PR / MR mode 設計
- [ADR-020 — Tenant Federation](../adr/020-tenant-federation.md) — federation epic 設計總文
