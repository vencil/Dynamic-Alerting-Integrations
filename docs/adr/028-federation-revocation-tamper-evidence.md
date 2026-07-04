---
title: "ADR-028: Federation 撤銷儲存 tamper-evidence — off-cluster 對帳為主控"
tags: [adr, tenant-api, federation, security, audit]
audience: [platform-engineers, security, sre]
version: v2.9.0
lang: zh
id: ADR-028
tracking_kind: adr
status: proposed
domain: tenant-api
created_at: 2026-07-04
updated_at: 2026-07-04
---
# ADR-028: Federation 撤銷儲存 tamper-evidence — off-cluster 對帳為主控

## 狀態

🟡 **Proposed**（2026-07-04）。owner 核可後昇格 Accepted。Refs [#924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/924)（自 [#903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/903) RFC 拆出），設計經兩輪外部 adversarial review（Gemini：架構收斂 + 實作邊角護欄）。

> 依語言政策，ADR 自 ADR-019 起不另製 `.en.md`。

## TL;DR

- **問題**：federation 撤銷集（`tenant-federation-store` ConfigMap 的 `revoked.txt` / `store.json`）**依設計 runtime-mutable**——tenant-api 的 SA 執行期寫它。偷了該 SA token、或 tenant-api pod RCE 的攻擊者，能刪掉一個 `token_id`（**un-revoke**、把已撤銷 token 復活），且寫入帶**合法身份** → 身分控制（RBAC / VAP / [#926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/926) 的 out-of-band 告警）全看不到。
- **重定框**：這是 **Certificate Transparency 類問題**（偵測一個**有合法權限者**濫用權限），業界正典是 **append-only + 獨立觀察者**，**不是**原地放 checksum——同一個可寫位置內的 keyless hash，攻擊者刪一項後可重算整條（[#569](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/569) chargeback runbook §7.3 自承「非 compliance-grade WORM」）。且本平台已用 **4h token TTL** 把窗口壓短（短憑證 > 複雜撤銷，對齊 SPIFFE/CT 業界走向）——**本 ADR 是替這個 4h 窗加一層偵測，不是造 revocation 帝國。**
- **三條決策**：
  1. **主控 = off-cluster 對帳（detective）**：`revoke()` 發一則結構化撤銷事件，走**既有** Vector→VictoriaLogs（append-only、在 ConfigMap 之外）；一支**離線 verifier** 直讀 ConfigMap 真相源對帳——「log 曾撤銷、且未到期、卻不在 live set」＝ un-revoke → `critical` 告警。
  2. **輔助 = in-CM digest（沿用 #569）**：只抓意外損毀 / 懶得重算的手改，**明講非主控**。
  3. **honest boundary**：tamper-**evident** 非 tamper-**proof**；錨定的是 tenant-api 範圍威脅，被更大的（VictoriaLogs / 全叢集）compromise 打穿 → 密碼學層列 defer-with-trigger。
- **一項顯式風險接受**：gateway `revoked_check.lua` 讀撤銷清單失敗時 **fail-open**（4h TTL 上限）。這是**刻意的**可用性權衡（同檔對「跨租戶外洩」是 fail-closed 為證）→ 本 ADR 記為**具名 Risk Acceptance + 便宜偵測**，真正的 fail-closed 降級另立 issue（defer-with-trigger）。

## 背景：威脅面與現況

撤銷儲存的形狀（[ADR-020](020-tenant-federation.md) Posture B、`configmap_store.go`）：
- `store.json` = 真相（records + revoked set），只由 tenant-api 讀寫；`revoked.txt` = 衍生投影（每次寫重生），gateway 以 projected volume 唯讀掛載、每 30s reload。
- 撤銷 = `revoke()` 把 `token_id` append 進 revoked set；`pruneDoc()` 每次寫會**剪掉已過期**的 revoked 項（為守 ConfigMap ~1MiB 上限）。

**威脅（#924）**：直接編輯 store 把一個**未過期**的 `token_id` 從 revoked set 拿掉 = un-revoke，讓一個本該失效的 federation JWT 在其 TTL 剩餘時間內復活，**無任何 failed-auth 訊號**。

**為什麼身分控制擋不住**：真實 compromise 路徑（偷 SA token / pod RCE）產生的寫入帶**合法 tenant-api SA 身份**，通過任何「誰可以寫」的檢查。這正是專案反覆踩的 *single-writer ≠ trusted-writer*。

**為什麼 pruning 讓它更隱蔽**：撤銷集本來就會因過期而縮小，un-revoke 混在正常 prune 裡難以區分——所以「偵測 set 縮小」不能只看大小，要看**內容**（哪個特定 token 該在卻不在）。

**與 [#926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/926) 的關係（互補、非重疊）**：#926 Part B 的 out-of-band 告警抓「**非平台身份**」寫 ConfigMap、且只到 `Metadata` level（不知改了哪個 key）。#924 的威脅寫入帶**合法身份**（#926 剛好漏）＋需要**內容級**判斷（哪個 token 被 un-revoke）。兩者咬合：#926 管「誰在寫」，#924 管「合法者寫了什麼」。

## 決策

### D1（主控）off-cluster 對帳

**寫端**：`revoke()`（及任何改動 revoked set 的路徑）發一則結構化事件到 stdout，經既有 Vector→VictoriaLogs 落 append-only 日誌：

```
{ "event": "federation_token_revoked", "token_id": "<opaque>", "expires_at": "<rfc3339>", "ts": "<rfc3339>" }
```

**驗端（load-bearing）**：一支週期 reconciler（CronJob）：
1. **直讀 ConfigMap 真相源**（唯讀 RBAC，`get` 該一個 resourceName）——**不經 tenant-api API**：若 tenant-api 已被 compromise，它可回傳偽造的 API response 騙過 verifier；直接讀 API server 上的 ConfigMap 收窄攻擊面（外審採納）。
2. 查 VictoriaLogs 取「窗口內所有 `federation_token_revoked` 事件」。
3. **對帳**：凡事件中 `now < expires_at` 的 `token_id`，斷言它仍在 live revoked set。缺一個 → un-revoke → 發 gauge + 告警 `FederationRevocationTamperSuspected`（`critical`）。
4. **verifier 自我 liveness**：每輪發 heartbeat；逾 N 輪未對帳 → `FederationRevocationReconcileStale`（防偵測機制被靜默拔除，對齊本專案 codify-trigger 紀律）。

**Trade-off（明寫）**：detective（偵測非阻止，un-revoke 到偵測有分鐘級 lag，« 4h TTL）＋ in-cluster 錨點（非真 off-cluster WORM）＋ keyless（靠 sink 的 append-only 特性 + 攻擊者搆不到 sink，非密碼學不可偽造）。攻擊者能從 ConfigMap 刪掉，但**收不回已 ship 出去的 log line**。

### D2（輔助）in-CM digest

沿用 #569 形狀：store 每次寫時，附一個 revoked set 的 digest（如另一個 ConfigMap key）。**只**抓意外損毀 / 懶得同步 digest 的手改。**明講非主控**：有寫入權的蓄意攻擊者可同時更新 digest（#569 runbook §7.3 已承認），故它不進威脅模型的主線，僅為便宜的 defense-in-depth。

### D3 honest boundary + PII 最小化

- **honest boundary**：tamper-**evident** 非 proof。錨點強度取決於「攻擊者的 ConfigMap 寫入權**不**延伸到改 VictoriaLogs」——對 tenant-api 範圍威脅成立（偷 SA token 不等於能改 log sink）；全叢集 / VictoriaLogs compromise 則打穿 → 見 defer-with-trigger 的密碼學層。
- **PII 最小化（去識別化，外審採納）**：對帳只用 **opaque `token_id`**（非 PII）；**不**把 `<tenant>` 之類客戶識別碼寫進事件——否則 audit sink（VictoriaLogs/SIEM）反而成為客戶機敏資料的外洩庫。IR 時要知道是哪個租戶，從 store 的 records 以 `token_id` 反查即可（映射本就在 store，不必進 log）。

## 選項與取捨

| 選項 | 複雜度 | 防的威脅 | 判定 |
|---|---|---|---|
| **A. off-cluster 對帳（本 ADR）** | 低（重用既有管線） | 有寫入權的蓄意 un-revoke（tenant-api 範圍） | ✅ **採用** |
| B. 同 ConfigMap 內 hash-chain / `.sha256` | 低 | 只防意外 / 懶得重算者 | ❌ 對本威脅是 theater（可重算），降為 D2 輔助 |
| C. Merkle / 透明度 log（Rekor / Trillian） | 高（整套 log 服務） | 密碼學不可否認 | ⏸️ defer（見下） |
| D. keyed / forward-secure MAC（RFC 5848 類） | 中高（金鑰管理） | 連 log sink 被改也防 | ⏸️ defer（見下） |

**核心取捨**：A 用「把紀錄外置到攻擊者搆不到的 append-only sink」買到對本威脅的偵測，成本近乎零（重用 Vector→VictoriaLogs + mtail/rules）；B 看似對（issue 原文建議沿用 #569）但對「有寫入權的攻擊者」無效，只能當輔助；C/D 是「真 tamper-proof」但成本與本威脅（4h 窗）不成比例。

## 與既有不變式的關係

- **[ADR-023](023-write-plane-single-writer-invariant.md) 單一寫者**：撤銷寫入序列化在單副本上 → 事件與 append **天然有序**，無並發 append 複雜度。verifier 是**唯讀**、無寫平面、不觸碰單寫者不變式。
- **[ADR-020](020-tenant-federation.md) store posture**：ConfigMap 為唯一狀態、`revoked.txt` 每次寫重生——D2 的 digest 附加為另一個 key，不改真相源結構；append-only 完整紀錄放 **off-store**（VictoriaLogs），避開 ConfigMap ~1MiB 上限與 prune 衝突（append-only 與 self-prune 在同一 CM 內互斥）。

## 相鄰破口：gateway fail-open 的顯式風險接受

`revoked_check.lua`（[ADR-020](020-tenant-federation.md) Layer 2）讀 `revoked.txt` 失敗時 **fail-open**：檔案 missing → 空集（全放行）；mid-read raise → 保留上次 set；一律不 500。

**攻擊者視角（外審 escalate）**：當 #924/#926 封住「改 store」與「偽造撤銷」，攻擊者的下一步是 **DoS 防禦本身**——弄壞 gateway 對 `revoked.txt` 的讀取（刪/卸載 projected-volume key、打滿相關資源、讓 Lua 逾時），fail-open 就放行**所有**本該撤銷的 token，最長 4h。

**判定：顯式 Risk Acceptance + 便宜偵測，不趕工 fail-closed**：
- 這是**刻意權衡**：撤銷檢查套用**全部** federation 流量，天真 fail-closed = 任何 ConfigMap sync 抖動 / 新 pod 首載前 / volume remount 都造成**整個 federation 斷線**（自我 DoS）。同一支 Lua 對「跨租戶外洩」（VictoriaLogs mode 的 account_id）是 fail-**closed** ——證明 fail-open 是針對「撤銷 staleness ≤ 4h 可接受」的**局部、經思考**的選擇，非疏漏。
- 本 ADR 把它從 code inline 註解**升為具名 Risk Acceptance**，並加**便宜偵測**：gateway 讀撤銷清單失敗 / 檔案 missing 時發 metric → 告警（fail-open 被觸發＝可見事件，零可用性風險，吻合本 ADR 的 detective thesis）。
- 真正的 fail-closed 降級（區分 missing vs empty、定義降級範圍、測抖動邊界）是它自己的 mini-design → **另立 issue，defer-with-trigger**。

## Defer-with-trigger（Future Work）

| 項目 | Reopen trigger |
|---|---|
| Merkle / 透明度 log（Rekor/Trillian，選項 C） | 客戶 RFP 要密碼學不可否認證明；或 token TTL 被調高到遠超 4h（撤銷窗變長） |
| keyed / forward-secure MAC（選項 D） | 威脅模型擴到「攻擊者也能改 VictoriaLogs」（＝全叢集 compromise 進 scope） |
| 真·off-cluster WORM / SIEM（[#566](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/566) X-2 / 把 #926 audit ship 外部） | PCI/SOC 要求叢集外不可變留存 |
| gateway fail-closed 降級模式 | 獨立 issue（本 ADR 相鄰破口節）；trigger = 非 HA 單實例 / 高合規客戶要求撤銷讀取失敗即阻斷 |

## Consequences

- **變容易**：un-revoke 從「無聲」變「分鐘內可偵測」；撤銷有了獨立於寫者的稽核跡；與 #926 合體覆蓋「誰寫×寫什麼」。
- **變難 / 新增運維面**：多一支 reconciler CronJob（要顧它的 liveness）；VictoriaLogs 需保留期 ≥ token TTL + IR 窗（4h + ~72h « 預設 30d，確認即可）。
- **要回訪**：defer 的密碼學層 / fail-closed 由上表 trigger 帶回，不主動預建。

## Action Items（MVP 實作，ADR accepted 後）

1. [ ] `configmap_store.go`：`revoke()`（及 revoked-set mutation 路徑）發結構化 `federation_token_revoked` 事件（`token_id` + `expires_at` + `ts`，**不含租戶識別碼**）。
2. [ ] **reconciler = Python 腳本掛 CronJob**（沿用 [#569](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/569) chargeback-aggregator 形狀：ConfigMap 掛載腳本 + pinned image + `concurrencyPolicy: Forbid`，reuse-over-build、**不新增 Go binary / release 線**）。**直讀 ConfigMap 的 `revoked.txt` key**（純 token_id 行、免 `store.json` schema、無 Go↔Python 雙寫漂移）× 查 VictoriaLogs 對帳 → gauge。唯讀 RBAC（`get` 單一 resourceName）、**絕不經 tenant-api API**。**邊角護欄（Gemini round-2）**：查詢窗只對「已穩定落盤」的 log（`[now-24h, now-1m]`，避 ingestion lag 誤判）；`now < expires_at` 加 **~2m clock-skew tolerance**（近到期即消失視為正常 prune、不誤報 critical，跨 API-server／VictoriaLogs／node 時鐘差）；`concurrencyPolicy: Forbid`（慢查詢跨排程週期不重疊跑、免重複告警）。
3. [ ] 告警 `FederationRevocationTamperSuspected`（critical）+ `FederationRevocationReconcileStale`（verifier liveness）；promtool 行為契約測試。
4. [ ] （便宜偵測）gateway 撤銷清單讀取失敗 → **counter** `federation_gateway_revocation_load_errors_total{reason="file_missing|parse_error"}` + 告警 `rate(...) > 0` **`for: 2m`**（濾掉 pod 啟動 / volume 重建瞬間的 I/O 抖動；持續才代表掛載真壞或遭 DoS，fail-open 觸發可見）。
5. [ ] （D2 輔助）in-CM revoked-set digest key，明確標註「非主控」。
6. [ ] runbook：對帳/鑑識程序（查 VictoriaLogs × store 反查租戶）＋ fail-open Risk Acceptance 條目。
7. [ ] 另立 issue：gateway fail-closed 降級模式（defer-with-trigger）。
