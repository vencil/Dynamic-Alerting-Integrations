---
title: "ADR-028: 偵測聯邦 token 撤銷被偷偷撤回 — 以叢集外的日誌對帳為主"
tags: [adr, tenant-api, federation, security, audit]
audience: [platform-engineers, security, sre]
version: v2.9.0
lang: zh
id: ADR-028
tracking_kind: adr
status: accepted
domain: tenant-api
created_at: 2026-07-04
updated_at: 2026-11-21
---
# ADR-028: 偵測聯邦 token 撤銷被偷偷撤回 — 以叢集外的日誌對帳為主

## 狀態

✅ **Accepted**（2026-07-04）。

- 2026-11-07 修訂：日誌庫前方的 vmauth 預設強制授權，只有 Vector 能寫入日誌庫；偽造證據的門檻因此移到 Vector 的輸入面（見「後果與已知限制」）。

> 依語言政策，ADR 自 ADR-019 起不另製 `.en.md`。

## 決策摘要

聯邦 token（federation token，租戶用來跨叢集查詢的短效 JWT，預設效期 4 小時）被撤銷後，撤銷紀錄存在 ConfigMap 裡，而 tenant-api 依設計在執行期改寫這份 ConfigMap。本 ADR 決定：

1. **主要防線是偵測，不是阻止。** 每次撤銷都另外寫一筆結構化日誌，送進 ConfigMap 以外、只能追加的日誌庫（VictoriaLogs）。一支常駐的對帳程式（reconciler）定期比對「日誌說撤銷過、還沒過期的 token」與「ConfigMap 裡現存的撤銷集」，缺一個就發 critical 告警。
2. **不在 ConfigMap 內放校驗碼當主要防線。** 有寫入權的人改了內容可以順手重算校驗碼。
3. **gateway 讀不到撤銷清單時放行（fail-open），是明說的風險接受**，配上偵測告警；讀不到就擋（fail-closed）的降級另案設計。

這是「讓竄改看得見」（tamper-evident），不是「讓竄改做不到」（tamper-proof）。

## 背景

### 撤銷資料怎麼存

聯邦撤銷存放在 `tenant-federation-store` ConfigMap（見 [ADR-020](020-tenant-federation.md)）：

- `store.json` 是真相來源（已簽發的 token 紀錄與撤銷集），只有 tenant-api 讀寫。
- `revoked.txt` 是從 `store.json` 衍生的清單，一行一個 `token_id`。tenant-api 每次寫入都從撤銷集逐字重新產生它，只寫入還沒過期的項目。
- federation-gateway 以唯讀 volume 掛載 `revoked.txt`，每 30 秒（`revokedSet.reloadIntervalSeconds`）重讀一次；token 在清單裡就回 403。
- tenant-api 每次寫入時會刪掉已過期的撤銷項，避免 ConfigMap 超過約 1 MiB 的上限。過期的 token 本來就會被 JWT 的到期檢查擋下，刪掉安全。

### 威脅：用合法身分撤回撤銷

偷到 tenant-api 的 ServiceAccount token，或在 tenant-api pod 裡取得執行權的攻擊者，可以從撤銷集裡刪掉一個還沒過期的 `token_id`。這等於撤回撤銷（下文稱 un-revoke）：一個本該失效的 token 在剩餘效期內恢復可用，而且不會產生任何認證失敗的訊號。

身分類的控制擋不住這件事：RBAC、admission policy、「非平台身分寫入 ConfigMap」的告警，問的都是「誰在寫」，而這筆寫入的身分正是合法的 tenant-api。要抓它，得判斷「寫了什麼」，也就是哪個特定 token 該在卻不在。只看撤銷集變小也不夠，因為撤銷集本來就會因過期清理而縮小。

這類問題的業界做法是「只能追加的紀錄 + 獨立的觀察者」，例如憑證透明度（Certificate Transparency）：把紀錄放到攻擊者改不了的地方，由別人來比對。在同一個可寫位置放不帶金鑰的雜湊沒有用，攻擊者刪掉一項後可以重算。

## 決策

### 偵測主體：撤銷日誌與現存撤銷集對帳

**寫入端（tenant-api）**：撤銷寫進 ConfigMap 成功之後，tenant-api 以 Info 等級寫一筆結構化日誌：

```json
{ "event": "federation_token_revoked", "token_id": "ftk_…", "expires_at": "<RFC3339>" }
```

只有新增的撤銷才寫；同一個 token 重複撤銷不再寫。寫入前先檢查 `token_id` 符合 `^ftk_[0-9a-f]+$`，不符合的直接拒絕，不會進入撤銷集。

**偵測端（reconciler）**：`helm/federation-reconciler` 部署一支常駐 Deployment，每 300 秒跑一輪：

1. 讀撤銷集：直接以唯讀 volume 掛載同一份 `revoked.txt`。不經 tenant-api 的 API（被入侵的 tenant-api 可以回假資料），也不需要任何 RBAC。
2. 查日誌：向 VictoriaLogs 查過去 24 小時內的撤銷事件。太新的事件先不比對：事件要比 `max(settleSeconds, projectionGraceSeconds)`（預設 60 與 180 秒）更舊才進入比對，讓日誌寫入延遲和 reconciler 自己那份 volume 的更新延遲都過去，避免把剛撤銷的 token 誤判成被撤回。
3. 比對：日誌裡每個離到期還超過 120 秒（時鐘誤差的緩衝）的 `token_id`，都必須在現存撤銷集裡。缺幾個，`federation_revocation_tamper_suspected` 就是幾。
4. 自身存活：只有整輪成功才更新 `federation_revocation_last_reconcile_timestamp_seconds`。任何查詢或讀檔失敗，這一輪不更新任何結果，也不回報「沒問題」；連續失敗會讓 `FederationRevocationReconcileStale` 觸發。

reconciler 的 pod 不掛 ServiceAccount token、沒有任何 Role，網路只能出到 DNS 與 VictoriaLogs。它刻意比一個被入侵的 tenant-api 權限更小。

攻擊者能從 ConfigMap 刪掉撤銷項，但收不回已經送出去的日誌。

### 證據通道與它的存活訊號

撤銷事件要經 Vector 採集才進得了 VictoriaLogs，所以 Vector 的採集範圍必須包含 tenant-api（`source.extraLabelSelector` 涵蓋 `tenant-api` 與 `federation-gateway`）。Vector 把 tenant-api 的這類事件分到專用的 `federation_evidence` 處理段：只放行列名的事件、只保留列名的欄位，標上 `log_type:"federation_evidence"`，且只寫入平台自己的分區，租戶查不到。

只看撤銷事件無法判斷通道是否通：「查到零筆」可能是這段時間沒人撤銷，也可能是通道斷了。所以 tenant-api 每 5 分鐘再送一筆不含任何內容的心跳 `federation_revocation_channel_heartbeat`，走與撤銷事件完全相同的處理段與目的地。reconciler 查最近 30 分鐘的心跳，查到就把 `federation_revocation_channel_up` 設為 1，否則為 0；持續 15 分鐘為 0 就觸發 `FederationRevocationEvidenceChannelDown`。心跳必須走同一條路，走別條路就證明不了撤銷事件到得了。

心跳查詢與其他查詢在同一個失敗即放棄的區塊裡：VictoriaLogs 暫時連不上時，`channel_up` 保留前一輪的值，不會誤報通道斷線（那種情況已由 `ReconcileStale` 回報）。

這三個數字互相牽動，不要只調一個：心跳間隔 5 分鐘、查詢窗 30 分鐘、告警等待 15 分鐘。查詢窗遠大於間隔，漏一筆心跳才不會被讀成斷線。

### `revoked.txt` 的逐位元組契約

`revoked.txt` 有兩個獨立實作的讀取端：gateway 的 Lua 與 reconciler 的 Python。兩者必須從同一份檔案讀出同一組 token，否則偵測端看到的撤銷集不是執行端在用的那份。契約如下，兩端與 tenant-api 一致：

- 只有 LF 分行；每行可以帶一個行尾 CR（讓 CRLF 寫成的檔案讀出相同結果），除此之外不做任何正規化。
- 非空行必須整行符合 `^ftk_[0-9a-f]+$`。這個 pattern 由 gateway chart 的 `revokedSet.tokenIdPattern` 注入 Lua，不在腳本裡手抄。
- 兩端都把整份檔案讀進來自己切行（以長度為準），不用逐行讀取的 API；Python 端以 `newline=""` 關掉解碼層的換行轉換。逐行 API 會在解析器之前改寫位元組，讓 pattern 檢查通過、讀出的集合卻不同。

兩端遇到違反契約的行，**刻意採取不同處置，不要統一**：

| 讀取端 | 遇到違反契約的行 | 為什麼 |
|---|---|---|
| gateway（執行端） | 整輪重讀作廢，沿用前一份撤銷集 | 前一份是竄改前載入的，仍含真正的 `token_id`，攻擊因此無效 |
| reconciler（偵測端） | 只丟掉那一行，讓它的 `token_id` 不出現在現存撤銷集 | 這個「不在」正是 `TamperSuspected` 的觸發條件；比照 gateway 會讓偵測消失，而各元件仍顯示健康 |

tenant-api 載入時遇到不合契約的撤銷項只計數、不剔除：`revoked.txt` 每次寫入都從撤銷集逐字重生，剔除等於在下一次寫入時替攻擊者完成 un-revoke。

gateway 作廢整輪重讀之後，之後新增的撤銷也都不會生效，而日誌與檔案對這些撤銷的認知一致，`tamper_suspected` 維持 0。所以另外有兩個指標與兩條 critical 告警：

- `federation_revocation_live_set_rejected_lines`：reconciler 讀自己那份 `revoked.txt` 時被拒的行數。看得到成因，但讀的是自己那份 volume，未必等於 gateway 當下載入的。
- `federation_gateway_revoked_set_reload_rejected`：從 gateway 自己的日誌數「重讀被拒」的警告。反映執行端實況，但看不到成因。

指標只回報行數，不回報行內容；定位哪一行壞掉是事件處理時的人工步驟。

三方的 pattern 字面值由 `tests/ops/test_revoked_set_contract.py` 比對；字面值相同不代表三個 regex 引擎語意相同，所以同一支測試另外窮舉每個位元組在每個相對位置的讀法，從實際讀檔的入口跑，與一份直接定義在位元組上的獨立判準比對。

### 偵測查詢只認平台標記的串流

三類日誌查詢都以平台寫入的分類欄位限定來源，不只比對內容：

- 撤銷事件與心跳：`log_type:"federation_evidence"`。兩者必須同一個串流，否則可能出現「事件查詢讀不到、心跳仍正常」的假健康。
- gateway 的三種重讀警告：`log_type:"gateway_operational"` 加 `app:"envoy"`。`app` 只用來排除同一個 pod 裡其他 sidecar 的雜訊。

`log_type` 由 Vector 依來源 pod 的 label 與 namespace 決定：撤銷證據只收 `tenant-api` namespace（`evidenceChannel.podNamespace`），gateway 警告只收 `monitoring` namespace（`evidenceChannel.gatewayNamespace`）。namespace 由控制面指派，pod 無法自選；pod label 與容器名則可以自己宣告。Vector 讀這些來源欄位時，用的是合併 producer 自己 JSON 之前的值，所以日誌內容蓋不掉它們。

這是提高偽造門檻，不是驗證來源身分：能在上述 namespace 建 pod 的人，仍可以讓自己的輸出被歸進這兩個串流。`monitoring` namespace 還跑著 Prometheus、Grafana、Alertmanager 等元件，對能在那裡建 pod 的人，這道綁定不增加成本。真正的來源身分驗證需要 producer 端簽章或 admission control。

### 鎖存告警與取樣盲區

reconciler 每輪從頭重算指標，非零只維持一輪。若告警寫成「`> 0` 持續 5 分鐘」，單輪偵測撐不到門檻，永遠不觸發。所以會因單次事件而非零的三條告警改成鎖存：`max_over_time(<指標>[1h]) > 0`，問的是「過去一小時內是否偵測到過」，而不是「此刻是否仍成立」。

| 告警 | 條件 | 等待 | 等級 |
|---|---|---|---|
| `FederationRevocationTamperSuspected` | `max_over_time(federation_revocation_tamper_suspected[1h]) > 0` | 5m | critical |
| `FederationGatewayRevokedSetMissing` | `max_over_time(federation_gateway_revoked_set_missing[1h]) > 0` | 5m | critical |
| `FederationGatewayRevokedSetReloadRejected` | `max_over_time(federation_gateway_revoked_set_reload_rejected[1h]) > 0` | 10m | critical |
| `FederationRevocationLiveSetRejected` | `federation_revocation_live_set_rejected_lines > 0` | 10m | critical |
| `FederationRevocationReconcileStale` | 上次成功對帳超過 1800 秒；或 Deployment 有副本卻沒有這個指標 | 10m | critical |
| `FederationRevocationEvidenceChannelDown` | `federation_revocation_channel_up == 0` | 15m | critical |
| `FederationGatewayRevocationLoadFailure` | `federation_gateway_revocation_load_errors > 0` | 2m | warning |

鎖存的代價：條件消失後，告警仍會再燒最多一小時。對一條 critical 安全訊號，這是刻意的，寧可讓人確認過再消失。

鎖存之後，「合法撤銷剛寫入、reconciler 自己那份 volume 還沒更新」這種一輪的假警報會直接發出，所以偵測端查日誌時才多等 `projectionGraceSeconds`，只比對夠舊的事件。不能改成「連續兩輪缺席才算」：攻擊窗短於一輪，不可能被取樣兩次。也不能改成調大共用的 `settleSeconds`：它同時是 gateway 警告查詢窗（過去 600 秒）的近端，調大會縮小那個窗。

**鎖存只保證「被取樣到的偵測會送到人手上」，不保證偵測會發生。** gateway 的重讀發生在請求路徑上（Envoy 的 Lua filter 沒有計時器），攻擊者自己的請求就能觸發重讀並在同一個請求被放行，所以攻擊時長約為 kubelet 更新 volume 的延遲加上最多 30 秒。這比 reconciler 的 300 秒一輪短，攻擊可以完整落在兩次取樣之間。週期性對帳的目標是讓狀態收斂，本來就允許漏看中間狀態；要補這個洞得讓偵測由事件驅動，追蹤於 [#1322](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1322)。那不是單純加一個 watch：reconciler 現在沒有 token、沒有 RBAC、連不到 API server，而 RBAC 的 `resourceNames` 限制不了 `list`／`watch`，加 watch 等於給它整個 namespace 的 ConfigMap 讀取權。

### 事件不帶租戶識別碼

撤銷事件與心跳只帶不透明的 `token_id` 與到期時間，不帶租戶或任何客戶識別碼；指標也只回報計數。否則稽核日誌庫本身會變成客戶識別資料的外洩點。事件處理時要知道是哪個租戶，以 `token_id` 回 `store.json` 的紀錄查。

### 範例

reconciler 的比對邏輯是純函式，可以直接餵輸入。設現在是 `2026-11-21T10:00:00Z`，日誌裡有三筆撤銷事件，現存撤銷集只有 `ftk_a1`：

```text
日誌事件：
  ftk_a1  expires_at 2026-11-21T12:00:00Z
  ftk_b2  expires_at 2026-11-21T13:30:00Z
  ftk_c3  expires_at 2026-11-21T10:01:00Z
revoked.txt：
  ftk_a1
```

輸出：

```text
checked 2 suspected ['ftk_b2'] tamper_suspected 1
```

`ftk_b2` 還有 3.5 小時才到期卻不在撤銷集，判為疑似撤回。`ftk_c3` 離到期不到 120 秒，視為正常的過期清理，不比對。

讀取契約的效果：`revoked.txt` 內容為 `ftk_a1\nFTK_B2\nftk_c3\r\n` 時，reconciler 讀出 `{ftk_a1, ftk_c3}`，被拒 1 行（大寫不合 pattern；行尾的 CR 被容忍）。同一份檔案，gateway 會整輪作廢、沿用前一份撤銷集。

## 相鄰破口：gateway fail-open 的風險接受

gateway 讀 `revoked.txt` 失敗時不回 500，依失敗方式有三種結果，各發一句不同的警告：

| 情況 | gateway 的撤銷集 | 指標 → 告警 |
|---|---|---|
| 讀取中途出錯 | 沿用前一份，仍在擋 | `federation_gateway_revocation_load_errors` → `FederationGatewayRevocationLoadFailure`（warning） |
| 內容違反契約 | 沿用前一份，仍在擋 | `federation_gateway_revoked_set_reload_rejected` → `FederationGatewayRevokedSetReloadRejected`（critical） |
| 檔案不存在 | **變成空集合，所有已撤銷的 token 都放行到到期** | `federation_gateway_revoked_set_missing` → `FederationGatewayRevokedSetMissing`（critical） |

三句警告的文字互不為子字串，reconciler 才能分開計數。把其中兩種讀成同一個訊號，事件處理會對「現在有沒有在放行」得出相反結論。首次載入就失敗的 worker 沒有前一份可沿用，什麼都不擋。

攻擊者封不住儲存、也偽造不了撤銷時，下一步會是弄壞防線本身：刪掉或卸載 volume 裡的 `revoked.txt`，讓 gateway 放行所有已撤銷的 token，最長到 token 效期結束（4 小時）。

**判定：接受這個風險，配偵測，不趕做 fail-closed。** 撤銷檢查套用在全部聯邦流量上，天真的 fail-closed 會讓任何 ConfigMap 同步抖動、新 pod 首次載入前、volume 重新掛載都變成整個聯邦斷線。同一支 Lua 對另一件事是 fail-closed 的：VictoriaLogs 模式下 token 缺少合法的租戶帳號 claim 就拒絕，因為那裡的失敗後果是跨租戶外洩。可見這裡的 fail-open 是針對「撤銷最多晚 4 小時生效可以接受」的局部選擇。

真正的 fail-closed 降級（區分「不存在」與「空」、界定降級範圍、測抖動邊界）是另一份設計，追蹤於 [#996](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/996)。

## 後果與已知限制

- **變容易**：un-revoke 從完全無聲變成通常幾分鐘內可偵測；撤銷有了獨立於寫入者的稽核紀錄。
- **新增的運維面**：多一支要顧存活的 reconciler；VictoriaLogs 的保留期要大於 token 效期加事件處理所需的時間（4 小時 + 約 72 小時，預設保留 30 天足夠）。
- **tenant-api 的日誌等級必須是 Info 或更詳細。** 撤銷事件是 Info 等級，`TA_LOG_LEVEL` 設成 warn 或 error 會把它濾掉，偵測就失效。
- **偵測錨點的強度，取決於攻擊者的 ConfigMap 寫入權不延伸到日誌庫。** 對 tenant-api 範圍的入侵成立；整個叢集或 VictoriaLogs 本身被入侵時就打穿。
- **日誌庫的寫入權只給 Vector。** VictoriaLogs 前方的 vmauth 依「消費端 × 路徑」授權：只有 Vector 能寫入（`/insert`），reconciler、gateway、Grafana、chargeback 只能查詢（`/select`），沒帶帳密的請求只能存取健康檢查與指標（victorialogs chart 預設 `auth.mode: enforce`，見 [`platform-log-aggregation-runbook.md`](../internal/platform-log-aggregation-runbook.md) §1.2）。剩下的偽造路徑都經過 Vector：在受採集的 namespace 建 pod，或寫入節點上 Vector 讀的容器日誌檔。
- **寫入與日誌之間的縫隙。** 撤銷寫進 ConfigMap 之後才寫日誌；若 pod 剛好在兩者之間被強制終止，這筆撤銷生效但沒有日誌，之後針對它的 un-revoke 抓不到。要封住得用 outbox 模式（先寫一份待送清單，另一個程序負責送出），對 4 小時效期的撤銷是過度設計。接受此風險：需要「剛好在縫隙崩潰」且「攻擊者剛好針對那個 token」兩件事同時發生。
- **大量撤銷事件可能讓 reconciler 記憶體不足。** 每輪把 24 小時窗的事件整包讀進記憶體。大量撤銷會讓 pod 因記憶體不足被終止，結果是 `ReconcileStale` 觸發，被打瞎會立即告警，不會靜默。
- **事件格式漂移會靜默縮小覆蓋。** 帶撤銷事件標記但缺欄位或時間無法解析的列會被丟掉，數量記在 `federation_revocation_events_dropped`。全部列都解析不了時，對帳結果是零事件、看似健康；目前沒有對應的告警，只有這個指標。
- **閒置的 gateway worker 不會重讀。** 重讀掛在請求上，收不到流量的 worker 既不更新撤銷集，也不產生任何警告。
- **兩端讀的是同一份 ConfigMap 的不同 volume、時間點也不同。** 所以「reconciler 這邊乾淨」不等於「gateway 載入了」；兩個拒絕指標縮小這個落差，但不消除它。
- **目前 chart 釘的映像跑不了 reconciler。** `helm/federation-reconciler` 釘 `da-tools:v2.9.0`，而那個映像不含 `_federation_revocation_reconciler.py`；部署後 `ReconcileStale` 會觸發，直到下一個 `tools/v*` 版本更新釘版。`check_image_pin_capability.py` 列管這個例外，釘版一變就要求移除。

## 考慮過的替代方案

| 做法 | 防得住有寫入權的蓄意撤回嗎 | 代價 | 判定 |
|---|---|---|---|
| **叢集外日誌對帳（本 ADR）** | 能偵測（tenant-api 範圍內） | 一支 reconciler、一條證據通道 | 採用 |
| 在同一份 ConfigMap 放雜湊鏈或 `.sha256` | 不能：有寫入權就能重算 | 低 | 只能抓意外損毀或忘了同步的手改，未實作；需要時當輔助，不當主要防線 |
| Merkle 樹或透明度日誌服務（例如 Rekor、Trillian） | 能，且密碼學上不可否認 | 高：整套日誌服務 | 延後 |
| 帶金鑰、具前向安全的訊息驗證碼（MAC；金鑰會定期演進，舊金鑰洩漏也偽造不了新紀錄，例如 RFC 5848 的做法） | 能，連日誌庫被改也防得住 | 中高：金鑰管理 | 延後 |
| reconciler 改用 CronJob | — | 平台沒有 Pushgateway 這類收短命程序指標的元件，CronJob 的結果無法被 Prometheus 抓取 | 不採用，改為常駐 Deployment 並暴露 `/metrics` |
| reconciler 經 Kubernetes API 讀撤銷集 | — | 需要 ServiceAccount token、RBAC 與通往 API server 的網路 | 不採用，改為掛載 `revoked.txt`，reconciler 因此不需要任何 Kubernetes 權限 |
| reconciler 經 tenant-api 的 API 讀撤銷集 | — | 被入侵的 tenant-api 可以回假資料騙過偵測 | 不採用 |

## 待觸發才做的項目

| 項目 | 觸發條件 |
|---|---|
| Merkle 樹或透明度日誌 | 客戶要求密碼學上不可否認的證明；或 token 效期調到遠超 4 小時 |
| 帶金鑰的訊息驗證碼 | 威脅模型擴大到「攻擊者也能改 VictoriaLogs」 |
| 叢集外不可變留存（寫入後不可改的儲存，或外部的安全事件分析平台） | PCI、SOC 等稽核要求叢集外不可變留存 |
| gateway fail-closed 降級（[#996](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/996)） | 沒有多副本備援的單實例部署，或高合規客戶要求讀不到撤銷清單就阻斷 |
| 事件驅動的偵測（[#1322](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1322)） | 出現一次真實的 un-revoke；token 效期大幅下修；平台開始收集 API server 稽核日誌；或相關 namespace 引入 admission control |
| reconciler 逐行解析或調高記憶體 | reconciler 實務上反覆因記憶體不足重啟，或正常撤銷量逼近 24 小時窗的記憶體上限 |
| 事件格式漂移的專屬告警 | `federation_revocation_events_dropped` 在實務上持續大於 0 |
| ConfigMap 內的撤銷集校驗碼 | 出現意外損毀或手改未同步的實際案例 |

## 與其他決策的關係

- [ADR-023](023-write-plane-single-writer-invariant.md)（單一寫入者）：撤銷寫入在單一副本上序列化，事件天然有序。reconciler 只讀，不碰寫入面。
- [ADR-020](020-tenant-federation.md)（聯邦儲存）：ConfigMap 仍是唯一狀態；只能追加的完整紀錄放在儲存之外，避開 ConfigMap 大小上限與過期清理的衝突。

## 實作位置

| 部分 | 位置 |
|---|---|
| 撤銷寫入、事件、`token_id` 檢查、載入時計數 | `components/tenant-api/internal/federation/token/configmap_store.go`、`manager.go` |
| 心跳 | `components/tenant-api/internal/federation/token/heartbeat.go` |
| gateway 重讀與三種警告 | `helm/federation-gateway/files/revoked_check.lua` |
| reconciler | `scripts/tools/ops/_federation_revocation_reconciler.py`、`helm/federation-reconciler/` |
| 證據通道 | `helm/vector/values.yaml`（`evidenceChannel`）、`helm/vector/templates/configmap.yaml` |
| 告警 | `rule-packs/rule-pack-platform.yaml` |
| 讀取契約測試 | `tests/ops/test_revoked_set_contract.py` |
| 事件處理手冊 | [`federation-revocation-reconciler-runbook.md`](../internal/federation-revocation-reconciler-runbook.md) |

## 相關

- [#924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/924)：本決策的原始議題
- [ADR-020](020-tenant-federation.md)、[ADR-023](023-write-plane-single-writer-invariant.md)
