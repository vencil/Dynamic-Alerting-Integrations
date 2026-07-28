---
title: "ADR-028: Federation 撤銷儲存 tamper-evidence — off-cluster 對帳為主控"
tags: [adr, tenant-api, federation, security, audit]
audience: [platform-engineers, security, sre]
version: v2.9.0
lang: zh
id: ADR-028
tracking_kind: adr
status: accepted
domain: tenant-api
created_at: 2026-07-04
updated_at: 2026-07-04
---
# ADR-028: Federation 撤銷儲存 tamper-evidence — off-cluster 對帳為主控

## 狀態

✅ **Accepted**（2026-07-04）。owner ratify（設計 PR [#995](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/995) merged）+ MVP 實作落地（producer PR [#997](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/997)；reconciler + 告警本 PR）。Refs [#924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/924)（自 [#903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/903) RFC 拆出），設計經兩輪外部 adversarial review（Gemini：架構收斂 + 實作邊角護欄）+ CodeRabbit。實作階段三處 impl-time refinement 見 §Action Items。

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

```json
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
- **dual-write gap（accepted risk，外審 Gemini 補）**：producer 在 ConfigMap commit **之後**才發事件，若 tenant-api pod 在這奈秒間隙硬死（OOMKill／node crash），撤銷已生效但事件**丟失** → 該 token 失去 tamper-evidence 錨點，未來針對它的 un-revoke 抓不到。徹底封需 **Outbox pattern**（先寫 CM、另一 process 派發事件），但對 4h-TTL 撤銷是荒謬的過度工程 → **接受此風險**（需 crash 剛好落在間隙 ∧ 攻擊者剛好精準針對該 token，雙巧合）。**不改邏輯**、記錄於此。
- **large-payload / OOM 邊界（外審 Gemini 補）**：對帳每輪把 24h 窗撤銷事件整包讀入記憶體（`urlopen().read()` → `json.loads`）。有寫入權的攻擊者狂灌撤銷（或極大合法量）→ payload 撐破 reconciler Pod 記憶體上限（`resources.limits.memory`）→ OOMKill。**安全**（非新破口）：fail-closed → 反覆 OOM 使 `last_reconcile_ts` 停滯 → `FederationRevocationReconcileStale`(critical) 觸發（被攻擊致瞎＝立即告警，非靜默 all-clear）。streaming／逐行 decode 或調高記憶體只為**降噪** → **defer-with-trigger**（見下表）。
- **schema-drift 靜默盲點（本 PR 對抗式 review 補）**：帶 `event:"federation_token_revoked"` 標記卻缺欄位／時間壞掉的 row 被 `parse_events` 丟棄。若 tenant-api 事件 schema 漂移使**全部** row 不可解析 → 對帳出 0 事件、`last_reconcile_ts` 照刷（看似健康）→ Stale **不**觸發、真 un-revoke 漏報。**緩解**：暴露 `federation_revocation_events_dropped` gauge 令漂移**可見**（非零＝有標記卻解不出＝schema drift）；專屬解析錯誤告警 → **defer-with-trigger**（實務觀察到持續 > 0 再加，避免 alert-count churn）。
- **⛔ 證據通道從未接上（本 ADR 的前提錯誤，#1234 事後補記）**：本 ADR 通篇假設撤銷事件「重用既有 Vector→VictoriaLogs 管線」即可抵達偵測端（見上方〈選項與取捨〉的「成本近乎零（重用既有管線）」）。**該前提當時不成立**：`helm/vector` 的 `source.extraLabelSelector` 只 tail `federation-gateway`，**tenant-api 根本不在採集範圍內**，所以 D1 的查詢從上線第一天就**永遠回零筆**。這不是實作 bug，是設計時未驗證的假設——**低成本的「重用既有管線」正是它沒被驗證的原因**：改用新管線會被迫寫設定、寫設定會撞到 selector；宣稱重用則什麼都不必碰，於是也什麼都沒檢查。
  - **為何沒有任何告警抓到**：`zero rows` 同時是**健康狀態的合法觀測**（「這段時間沒有撤銷」），所以對帳每輪都「成功」、`last_reconcile_ts` 照刷、`TamperSuspected` 因無事件可比對而恆為 0、`ReconcileStale` 保持綠。三條告警**全綠**，控制**完全無效**。
  - ⚠️ **今日的實際狀態是「大聲壞掉」而非靜默全綠**（勿以「三條告警都綠」描述現況）：reconciler 釘的 `da-tools:v2.9.0` image **不含** `_federation_revocation_reconciler.py`（#1240 image-pin EXEMPTIONS 列管，修法＝下一個 `tools/v*` release），且在 #1234 §D 落地前 VictoriaLogs 的 NetworkPolicy 會丟掉 reconciler 的查詢——兩者都讓 `ReconcileStale` 觸發。**靜默偽綠是那兩項修好之後才會出現的狀態**，而那正是 canary 要防的東西。
  - **封法只能在來源端**：任何純偵測端的推論都封不住「零筆」（撤銷集合法為空時證明不了任何事，且部署後 ≤4h ramp 會誤報）。故 tenant-api 每 ~5m 發一筆 content-free `federation_revocation_channel_heartbeat`，**走與真實證據完全相同的 transform 與 sink**，`federation_revocation_channel_up` 斷言它到得了，缺席即 `FederationRevocationEvidenceChannelDown`（critical）。canary 走**不同**路徑就什麼都證不到，這是它的核心約束。
  - **教訓（可轉移）**：「重用既有管線」在 ADR 裡要當成**待驗證的可達性主張**，不是零成本前提；而任何「空集合＝健康」的偵測面，都需要一個**已知訊號的 canary**才有 liveness。
- **⛔ 執行端與偵測端的解析契約（#1235 / TRK-349，2026-07 補）**：本 ADR 的偵測論證預設「gateway 看到的撤銷集合 ＝ reconciler 看到的撤銷集合」，但 `revoked.txt` 有**兩個獨立實作**的讀取端（gateway Lua / reconciler Python），兩者對「一行的邊界」與「什麼算空白」語意不同 → **同一份檔案可解析出不同的 token 集合**，前提無任何機制保證。修法是把契約**顯式化並三邊一致**：合法行必須逐位元組符合 `^ftk_[0-9a-f]+$`（只容忍行尾單一 CR，讓 CRLF 檔在兩端一致），任何正規化都被移除——**正規化本身就是分岔來源**。契約值由 chart（`revokedSet.tokenIdPattern`）注入 Lua，不在腳本內手抄；寫入端（`revoke()`）先驗 charset 讓非法 id 進不了 store。
  - **⛔ 兩端刻意不對稱，勿「統一」**：gateway 遇到違反契約的行 → **作廢整輪 reload、沿用前一份 set**（那份是竄改前載入的，仍含真正的 token_id ＝ 攻擊被擋）；reconciler **不得**比照，它必須讓該 id **不出現在 live set**——那個「缺席」正是 `TamperSuspected` 的觸發條件。把兩端和諧化會**把偵測本身悄悄刪掉**，而所有元件仍顯示健康。程式碼兩處各有註解釘住這件事。
  - **⛔ load 時偵測到不合法 entry 只計數、絕不剔除**：`revokedText()` 每次寫入都從 store 文件**逐字重生** `revoked.txt`，所以「載入時順手清掉髒 entry」會在下一次任意寫入把該 token 徹底移出撤銷集——**由平台親手完成攻擊者要做的 un-revoke**。這是「FIX 移除附帶防護」家族的實例，測試已釘住 round-trip 存活。
  - **⛔「逐位元組」是關於 BYTES 的主張，所以讀取路徑也在契約內**：pattern 只管「這串位元組是不是 id」，管不到「哪串位元組才算一行」。兩端原本的讀法各自都會在解析器看到任何東西之前改寫它看到的位元組，於是 reload 可以**成功**而解析出與偵測端不同的集合——分歧在 pattern 檢查的**下面一層**。故兩端的讀取都改為**長度基底**、整份讀入自行切行，Python 端明確關掉解碼層的行終止符轉換。**驗契約時務必從 production 入口驗**（讀檔那一段），字串層測試對這一層結構性失明。
  - **驗證面的誠實邊界（#1235 收尾修正）**：三處 pattern 字面值相等由 `tests/ops/test_revoked_set_contract.py` 機械比對，但**字面相等 ≠ 語意相等**——它只擋得住「手抄的 pattern 漂移」。**其餘三件事各有各的護欄，不可互相代替**：(a) **pattern 方言漂移**（同一個字面值在三個引擎意思不同）→ federation-e2e **S11** 正向等價案；(b) **讀取語意漂移**（誰算一行）→ federation-e2e **S10** 負向案，⚠️ **S11 對此毫無阻力**（舊讀法與新讀法都能通過 S11）；(c) **位元組空間覆蓋** → `tests/ops/test_revoked_set_contract.py` 的**機械窮舉一致性矩陣**（每個位元組 × 每個相對位置，跑 production 入口，與一份直接寫在 bytes 上的獨立 oracle 比對，並自帶「這個矩陣真的抓得到正規化讀取器」的 meta-guard）。**本 ADR 現在靠的是矩陣＋行為場景，而不是三個字面值彼此相等。**
  - **新增可觀測性：兩個 gauge ＋ 兩條 critical 告警（推翻本 PR 早先「刻意不加 metric」的決定）**：早先的理由是「被拒行意味該 id 不在 live set，若它確實被撤銷過 `TamperSuspected` 就會觸發」。⛔ **該推理只涵蓋壞行上的那一個 id**。gateway 的處置是作廢**整輪** reload、沿用前一份 set，所以**壞行出現之後發出的每一筆撤銷都沒有被載入、沒有生效**，而 audit log 與檔案對這些新撤銷的認知**完全一致**，`tamper_suspected` 因此維持 0；再者，**首次載入就撞到壞行的 worker 沒有前值可留、什麼都不擋**。`revoked.txt` **不見**一直是大聲的（偵測端讀到空集合、全部缺席），**被拒**卻是無聲的——本 ADR 的論證從未計入這個不對稱。故新增 `federation_revocation_live_set_rejected_lines`（本行程讀到的檔案，看得到成因）與 `federation_gateway_revoked_set_reload_rejected`（從 gateway 自己的 log stream 讀回、以 `log_type:"gateway_operational"` 限定串流，是執行面自述），各配一條 critical 告警（`> 0`, `for:10m` ＝一個 reconcile 週期加等量餘裕；條件持久，窗長無成本）。**兩個而非一個**：前者讀的是自己那份 kubelet 投影、未必等於 gateway 當下載到的；後者不受投影／時間差影響，但對「沒產生那句 warn 的成因」是啞的。IR 見 [runbook](../internal/federation-revocation-reconciler-runbook.md)。
  - **仍未涵蓋（誠實邊界）**：(a) **收不到流量的 worker 永遠不會 reload**——reload 掛在請求路徑的時間閘上，一個閒置 worker 既不會產生拒載 warn、也不會更新它手上的集合，兩個 gauge 對它都是沉默的；(b) **兩端讀的是同一份 ConfigMap 的不同 kubelet 投影、時間點也不同**，所以「我這邊的檔案看起來乾淨」不等於「gateway 載進去了」——這正是**兩個** gauge 而非一個的原因，但它縮小差距、不消除差距；(c) gauge 是**計數**，永不回報行內容（ADR-028 D3），所以定位壞行仍是 IR 的人工步驟。
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
| reconciler streaming／逐行 parse 或調高記憶體 | 大 payload 令 reconciler 實務中反覆 OOM→Stale（噪音），或正常撤銷量逼近 24h 窗記憶體容量 |
| reconciler 事件解析錯誤（schema drift）專屬告警 | `federation_revocation_events_dropped` 在實務中觀察到持續 > 0（tenant-api 事件 schema 變更） |

## Consequences

- **變容易**：un-revoke 從「無聲」變「分鐘內可偵測」；撤銷有了獨立於寫者的稽核跡；與 #926 合體覆蓋「誰寫×寫什麼」。
- **變難 / 新增運維面**：多一支 reconciler Deployment（要顧它的 liveness）；VictoriaLogs 需保留期 ≥ token TTL + IR 窗（4h + ~72h « 預設 30d，確認即可）。
- **要回訪**：defer 的密碼學層 / fail-closed 由上表 trigger 帶回，不主動預建。

## Action Items（MVP 實作）

> **實作落地 + impl-time refinements**：MVP 分兩段交付——**producer（[#997](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/997)）** + **reconciler + 告警（本 PR）**。三處偏離原設計，皆有依據：
> - **reconciler 改長駐 Deployment + `/metrics`（非 CronJob）**：平台無 Pushgateway／textfile-collector／vmalert（親驗），短命 CronJob 無法被 scrape；exporter + `up` liveness 才 Prometheus-native。實作為 da-tools image 內的 `_federation_revocation_reconciler.py`（`_`-prefix、`BUILD_EXEMPT`＝baked 但非 dispatched CLI），Deployment 直接 invoke——仍 reuse da-tools、不新增 release 線。
> - **live 集改「mount `revoked.txt`」直讀（非 API + RBAC）**：kubelet projection＝真·直讀，**免 RBAC**、比 API-read 更難被 compromise 的 tenant-api 欺騙（G3 更純）。
> - **fail-open counter 由 reconciler 從 VictoriaLogs 查發（非 mtail）**：gateway 讀取失敗 warn 去 Envoy stderr，mtail 只 tail audit access-log 檔、看不到。

1. [x] producer（[#997](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/997)）：`revoke()` 在**新增撤銷**時發 `federation_token_revoked`（opaque `token_id` + `expires_at`、**無租戶**、mutate() commit 後才發、idempotent 不重發、per-instance logger seam）。⚠️ **約束**：tenant-api 須跑 **log level ≤ Info**（事件 Info 級，Warn+ 會靜默過濾掉、令 tamper-evidence 失效）。
2. [x] reconciler（見上 refinements）：pure `reconcile()` + **fail-closed**（查詢／讀取失敗增 error counter、**不刷 `last_reconcile_ts`**、絕不誤報 all-clear）+ clock-skew margin + settled 窗 + `events_dropped` gauge（有標記卻解不出的 row 計數＝schema-drift 可見性，本 PR 對抗式 review 補）；`helm/federation-reconciler`（Deployment + SA〔免 RBAC／token〕 + NetworkPolicy〔egress VictoriaLogs〕 + CM-mount + scrape）。
3. [x] 3 告警（`FederationRevocationTamperSuspected` crit／`…ReconcileStale` crit〔staleness `time()-ts>30m` **or** `absent()`〕／`FederationGatewayRevocationLoadFailure` warn）+ promtool 契約測試（7 案）。
4. [x] fail-open 偵測：reconciler 暴露 `federation_gateway_revocation_load_errors` **gauge**（近 ~10m 窗、非 24h），告警 `> 0 for: 2m`。
5. [ ] （D2 輔助）in-CM revoked-set digest——**defer**（明標非主控，小 follow-up）。
6. [x] runbook：[`federation-revocation-reconciler-runbook.md`](../internal/federation-revocation-reconciler-runbook.md)。
7. [x] 相鄰 fail-open issue 已開：[#996](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/996)（fail-closed 降級，defer-with-trigger）。
8. [x] **證據通道接通 + liveness canary（[#1234](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1234) / TRK-348）**——修 §D3 那條前提錯誤。兩半：(a) **通道**：`helm/vector` source selector 改集合式涵蓋 tenant-api，並在 `demux` **之前**插 `origin_split` 分流到專屬 `federation_evidence` transform（PII row-allowlist ＋ field keep-list 重建），rows 只進 0:0 平台 store、**結構上**到不了任何租戶分區；VictoriaLogs netpol 補 `federation-reconciler`。(b) **canary**：tenant-api 每 5m 發 `federation_revocation_channel_heartbeat`（無租戶識別碼、走同一 transform/sink），reconciler 新增 `federation_revocation_channel_up` / `federation_revocation_heartbeats_seen` 兩個 gauge（心跳查詢在**既有 fail-closed try 內**——VictoriaLogs 短暫不可用須保留前值，不得偽報通道死亡），第 4 條告警 `FederationRevocationEvidenceChannelDown`（critical，`== 0 for 15m` ≈ 3 個心跳週期，**不加** `or absent()`——該成因已由 `ReconcileStale` 的 `absent()` 涵蓋）。⛔ **刻意不做**：`vector_component_discarded_events_total{component_id="federation_evidence"}` 的 rate-based tripwire——該通道設計上就近乎 100% 丟棄，任何 floor 量到的是 tenant-api 請求量而非 allowlist 失配（詳述與 dynamic-threshold 也不可行的理由見 platform-log-aggregation-runbook §8.7）；canary 才是對的偵測器。
