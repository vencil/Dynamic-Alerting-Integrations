---
title: "租戶 Offboarding Runbook — Federation 清理"
tags: [runbook, federation, offboarding, security, operations]
audience: [platform-engineers, sre]
version: v2.9.1
lang: zh
status: active
domain: tenant-api
created_at: 2026-05-19
updated_at: 2026-05-19
---

# 租戶 Offboarding Runbook — Federation 清理

> 移除一個租戶時，**federation 的兩樣東西不會隨 `conf.d/<tenant>.yaml` 一起消失**，必須手動清掉，否則留下殭屍憑證與孤兒設定檔。本 runbook 是 ADR-020 §Token model 的 offboarding 收尾程序（issue [#521](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/521)）。

## 背景：為什麼 offboarding 不會自動清乾淨

租戶 offboarding 在本平台是一個 **git 操作** —— 移除該租戶的 `conf.d/<tenant>.yaml`。但 federation 有兩樣東西**不在** `conf.d/<tenant>.yaml` 裡，所以不會被一併帶走：

| 殘留物 | 存放處 | 不清會怎樣 |
|---|---|---|
| **federation token records** | `tenant-federation-store` ConfigMap（runtime state，ADR-020 Posture B）| token 在密碼學上仍合法、gateway 仍會驗過 —— 殭屍憑證，直到 4h TTL 到期 |
| **federation subset 檔** | `conf.d/_federation/<tenant>.yaml`（per-tenant 指標子集，ADR-020 IV-2e）| 孤兒設定檔永久殘留；若日後 tenant id 被重用，會變成意外的既存狀態 |

風險等級 **低**：殭屍 token 對已刪租戶注入 `{tenant="X"}` 只會回空集（無 live 資料外洩），且受 gateway per-token / per-tenant 限流約束 —— 屬 offboarding-completeness / 合規問題。但「低」不等於「可略過」：稽核（SOC 2 / ISO 27001）會檢查 offboarding 流程是否完整。

## 前提

- 你要 offboard 的租戶 id（以下記為 `<tenant>`）。
- tenant-api 的存取權：撤銷 token 走已認證的 `DELETE /api/v1/federation/tokens/{id}`（操作者經 oauth2-proxy 的互動式登入即可 —— 不需要任何帶外憑證）。
- conf.d repo 的寫入權（offboarding 的 git PR）。

## 步驟

把以下四步**放進同一個 offboarding PR / 同一次操作**，offboarding 才算完整。

### 1. 撤銷該租戶的 federation token

列出該租戶當前的 token：

```sh
curl -s "$TENANT_API/api/v1/federation/tokens?tenant_id=<tenant>"
```

對回傳的每一個 `token_id` 撤銷：

```sh
curl -X DELETE "$TENANT_API/api/v1/federation/tokens/<token_id>"
```

`DELETE` 把 token id 寫進 `tenant-federation-store` ConfigMap 的 revoked set —— gateway 會在最終一致的延遲後拒絕它（見下方 §最終一致性）。

> 若該租戶**沒有**任何有效 token（`tokens` 回空陣列），本步驟跳過 —— 但仍要做步驟 2。

### 2. 移除 federation subset 檔

```sh
git rm conf.d/_federation/<tenant>.yaml   # 若該檔存在
```

並非每個租戶都有 subset 檔（只有曾經設定過 federation 指標子集的租戶才有）。檔案不存在就跳過。

> **平台 whitelist（`_federation_policy.yaml`）不要動** —— 那是平台層級的、不隨單一租戶 offboarding 改變。只刪 per-tenant 的 `_federation/<tenant>.yaml`。

### 3. 移除租戶設定檔（offboarding 本身）

```sh
git rm conf.d/<tenant>.yaml
```

步驟 2、3 在同一個 commit、走同一次 PR review。

### 4. 驗證

- `GET /api/v1/federation/tokens?tenant_id=<tenant>` 回空陣列。
- `conf.d/_federation/<tenant>.yaml` 與 `conf.d/<tenant>.yaml` 都已不在 repo。
- 等過最終一致性窗口後（見下），舊 token 打 gateway 應回 `403`。

## Federation 撤銷的最終一致性（合規用語）

撤銷**不是即時生效**。`DELETE` 把 token id 寫進 ConfigMap 的 revoked set 後，gateway 端的生效路徑是：

1. kubelet 把 ConfigMap 的更新同步進 gateway pod 的 projected volume —— 最長約 1 分鐘。
2. gateway 的 Lua filter 以時間閘（預設 30s）重讀 `revoked.txt`。

合計**最長約 1–2 分鐘**舊 token 仍可能通過。這是**最終一致（eventual consistency）**，是分散式系統的標準代價、被接受的設計取捨（ADR-020 放棄 server-side revocation list 的對價是 gateway 限流 + 4h TTL），**非漏洞**。稽核時可直接引用本段。對外的 UI / API 回應措辭**不可暗示撤銷即時生效**。

## 殘留偵測（passive detector 安全網）

tenant-api 內建一個**被動偵測器**：週期性掃描，若發現 federation token 或 `_federation/<tenant>.yaml` subset 檔的母租戶已不在 conf.d，就：

- 噴一條 `slog` **WARN** log（列出孤兒 token id / 孤兒 subset 檔）。
- 更新 `/metrics` 的 `tenant_api_federation_orphaned_tokens` / `tenant_api_federation_orphaned_subset_files` gauge。

偵測器**只觀測、不自動撤銷、不自動刪檔** —— 它是「人忘了跑本 runbook」的安全網，不是替代品。看到該 metric > 0 或該 WARN log，就回到本 runbook 把對應租戶補清乾淨。

> 為什麼不做「自動撤銷的 reconciler」：自動依「租戶不在 conf.d」推論去撤銷，在 conf.d 暫態異常（GitOps sync 中、設定壞檔）時會誤殺活租戶的憑證。低風險問題不值得用一個有誤殺風險的常駐自動化去解 —— 偵測（warn）給了安全網卻零誤殺風險。詳 [#521](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/521) 的決策討論。

## 已退租租戶持續打 gateway —— 告警與殘留流量

退租後，**租戶端的 Grafana data source / CronJob 不知道自己被退租**，會繼續每 ~30s 帶舊 token 打 gateway。兩個階段：

| 階段 | 時間 | gateway 回應 | 計入 `tenant_federation_requests_total`？ |
|---|---|---|---|
| 撤銷後、token 未到期 | 撤銷後 ≤ 4h（至 token 原 TTL 到期）| `403` —— 撤銷 token 仍是合法 JWT，claim 已注入 | **計入** `{status="auth_failed"}` |
| token 到期後 | > 4h | `401` —— `jwt_authn` 在 claim 注入前就擋下 | **不計入** —— access log 無 `tenant_id`（ADR-020 §Audit log「Metric 邊界」）|

### `FederationRejectionRateAnomaly` 不會對已退租租戶誤報

`FederationRejectionRateAnomaly`（`k8s/03-monitoring/configmap-rules-platform.yaml`）自 [#550](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/550) 起在規則尾端 join `and on (tenant) tenant_metadata_info` —— 只評估**仍在 conf.d** 的租戶。完成步驟 3（`git rm conf.d/<tenant>.yaml`）後，threshold-exporter 重載、該租戶的 `tenant_metadata_info` 序列消失，告警的 join 隨之把它排除：殭屍 token 在上表階段一造成的 100% `auth_failed` **不會**再讓平台 ops 被一個已不存在的租戶 call 醒。

> 若你**仍**看到此告警對某已退租租戶觸發 —— 代表步驟 3 沒做完（`conf.d/<tenant>.yaml` 還在 repo），threshold-exporter 仍在發該租戶的 `tenant_metadata_info`。回到 §步驟 補完。

### 殘留流量本身（選用清理）

即使告警已不誤報，已退租租戶那條注定失敗的輪詢仍是**無謂的 gateway 負載**（階段一還會在 audit log 留 403 噪音）。真正的修法在對方手上 —— **通知已退租客戶關掉他們的 Grafana data source / CronJob**。

對方不配合、殘留流量造成困擾時，可在 ingress / WAF 層（或 gateway per-IP）block 對方來源 IP。這是**選用**清理、非必要步驟：gateway 的 per-IP 限流本就壓得住洪流，階段二的 `401` 也很便宜（`jwt_authn` 直接擋、不進 audit log、不進 metric）。

## 反面 —— 不要這樣做

- **不要**用 `git push --force` 或繞過 PR review 來「快速」offboard —— offboarding 是低頻操作，沒有快的需求，走正常 PR。
- **不要**直接 `kubectl edit` 那個 `tenant-federation-store` ConfigMap 去手動刪 record —— 它有 schema（`revoked.txt` 格式），手改容易寫壞；一律走 `DELETE` API。
- **不要**刪平台 whitelist `_federation_policy.yaml` 裡的東西 —— 那不屬單一租戶 offboarding。
