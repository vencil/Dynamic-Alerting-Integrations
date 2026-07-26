# Federation 撤銷 reconciler — 運維 / IR runbook

> ADR-028 D1（#924）的偵測端。搭配 [ADR-028](../adr/028-federation-revocation-tamper-evidence.md) 讀。

## 這是什麼

一支長駐 **Deployment**（`helm/federation-reconciler`，跑 da-tools image 內的 `_federation_revocation_reconciler.py`），週期性把**撤銷事件日誌**（VictoriaLogs，`event:"federation_token_revoked"`）跟 **live 撤銷集**（`tenant-federation-store` ConfigMap 的 `revoked.txt`，以唯讀 volume mount）對帳，偵測 **un-revoke**（有寫入權的攻擊者把未過期的撤銷偷偷刪掉），並用 `/metrics` 暴露給 Prometheus。

- **為何 Deployment 非 CronJob**：平台無 Pushgateway/textfile/vmalert，短命 CronJob 無法被 scrape；exporter + `up` liveness 才是 Prometheus-native。
- **為何 mount 讀而非 API**：kubelet projection＝真·source-of-truth 直讀、不經可能被 compromise 的 tenant-api、且免 RBAC（ADR-028 G3）。
- **fail-closed**：VictoriaLogs 查詢或 `revoked.txt` 讀失敗 → 增 error counter、**不刷 `last_reconcile_ts`**（讓 staleness 告警觸發），**絕不誤報 all-clear**。

## 指標（`/metrics`，port 9099）

| metric | 型別 | 意義 |
|---|---|---|
| `federation_revocation_tamper_suspected` | gauge | 目前疑似 un-revoke 的 token 數 |
| `federation_revocation_last_reconcile_timestamp_seconds` | gauge | 最後一次**成功**對帳的 unix 時間（fail-closed 不刷新）|
| `federation_revocation_reconcile_errors_total` | counter | 失敗的對帳次數 |
| `federation_revocation_events_checked` | gauge | 上輪對帳的事件數 |
| `federation_revocation_events_dropped` | gauge | 上輪有 `federation_token_revoked` 標記卻解析失敗的 row 數（**非零＝tenant-api 事件 schema drift**，對帳覆蓋被侵蝕）|
| `federation_gateway_revocation_load_errors` | gauge | 近窗（~10m）gateway 撤銷清單讀取失敗數（fail-open 訊號）|
| `federation_revocation_channel_up` | gauge | **證據通道 liveness**（ADR-028 D1 / #1234）：窗內（30m）看到 tenant-api 心跳 canary＝1，否則 0。⛔ 查詢失敗**不會**把它寫 0（fail-closed，保留前值）——「問不到」不等於「通道死了」。fresh deploy 首次成功對帳前為 0（告警 `for:15m` 即寬限窗）|
| `federation_revocation_heartbeats_seen` | gauge | 窗內心跳筆數（除錯 / chaos 驗證用；健康時約 5-6＝5m 週期落進 30m 窗）。**告警訊號是 `channel_up`，不是這個**；這個用來看「衰減中」vs「已斷」|

## 告警與 IR

### `FederationRevocationTamperSuspected`（critical）
**意義**：log 說某 token 已撤銷且未過期，但它不在 live 撤銷集 → 疑似 un-revoke。
**IR**：
1. 查 VictoriaLogs 拿 opaque token_id：`{job="kube-audit"}` 之外用 `event:"federation_token_revoked"` 過濾近 24h（reconciler pod log 也會印 `TAMPER SUSPECTED: ...`）。
2. **租戶去識別化**：log 只有 token_id（ADR-028 D3）；IR 時從 store 的 records 以 token_id 反查租戶，**別**把租戶識別碼寫回工單。
3. diff live store vs git 歷史；若確為惡意刪除，從 git 還原該撤銷（break-glass 見 governance-security.md）。
4. 併查 #926 audit（是否有非平台身份寫 ConfigMap）——但本告警的威脅是**帶合法 SA 身份**的寫入，#926 可能看不到。

### `FederationRevocationReconcileStale`（critical）
**意義**：reconciler 逾 30min 未成功對帳，或指標消失（pod down / 從未 scrape）→ **偵測本身瞎了**。
**IR**：`kubectl -n monitoring get deploy federation-reconciler` + 看 pod log 有無 fail-closed 訊息（`reconcile pass failed (fail-closed...)`）+ 確認 VictoriaLogs 可達。fresh deploy 時 `last_reconcile_ts` 初始為 0（stale-by-default），首次成功對帳前有 `for:10m` 寬限。

### `FederationRevocationEvidenceChannelDown`（critical）

**意義**：reconciler 在**可達的** VictoriaLogs 裡看不到 tenant-api 心跳 canary → 證據通道（tenant-api log → Vector `origin_split` → `federation_evidence` transform → VictoriaLogs）**斷了**，un-revoke 偵測正在對著**空 feed** 報 all-clear。

⛔ **這是「偵測本身瞎了」，不是 log 管線的小毛病**。少了這條告警，空通道與「這段時間沒發生撤銷」**觀測上完全相同**：`TamperSuspected` 永遠不會觸發（沒有事件可比對）、`ReconcileStale` 也保持綠（對帳每輪都「成功」、`last_reconcile_ts` 照刷）。這正是 ADR-028 §D3 原本假設「走既有 Vector 管線」時留下的破口（#1234）。

**IR**（依序，每步都能單獨結案）：

1. **producer 還在發嗎**：`kubectl -n tenant-api logs deploy/tenant-api | grep federation_revocation_channel_heartbeat`。沒有 → 檢查 (a) log level 是否 ≤ Info（Warn+ 會靜默濾掉，與撤銷事件同一約束）；(b) federation 是否啟用（canary 與 orphan detector 同綁 `--federation-key`，關掉 federation 就不發——這是刻意的：沒有 producer 時不該報「通道健康」）；(c) `--federation-heartbeat-interval` 是否被調到接近/超過 reconciler 的 30m 查詢窗。
2. **Vector 還選得到這個 pod 嗎**：`helm/vector` 的 `source.extraLabelSelector` 必須涵蓋 tenant-api 的 `app.kubernetes.io/name`，且 `evidenceChannel.events` 必須仍列 tenant-api **實際輸出**的事件名。⚠️ **producer 端改名（slog 重構 / 欄位改叫 `event_type` / 事件名改字）的長相與本告警一模一樣**——先比對實際輸出再改 values。
3. **store 裡到底有沒有**：對 VictoriaLogs 下 `log_type:federation_evidence`。空集合＝確認通道斷（回到步驟 1-2）；**有資料卻仍告警**＝問題在 reconciler 的查詢面（`--heartbeat-lookback` / `--settle` 窗、`log_type` 值漂移），不在通道。
4. **看 `federation_revocation_heartbeats_seen` 的形狀**（dashboard「Heartbeat canary」panel）：從 6 緩降＝偶發丟列或 producer 週期被拉長；**斷崖式歸零**＝改名 / selector / pod 不在了。
5. **併發 `ReconcileStale`？** 那多半是 VictoriaLogs 不可達，而**不是**通道斷——查詢失敗走 fail-closed，`channel_up` 保留前值，所以本告警若同時響，是「前一輪就已經 0」而非本輪失敗造成。先修可達性再回頭看本條。

⚠️ **本告警不會因 VictoriaLogs 短暫不可用而誤報**：心跳查詢在 `reconcile_once` 既有的 fail-closed try 區塊內，失敗即 early-return、不碰 `channel_up`。

### `FederationGatewayRevocationLoadFailure`（warning）
**意義**：gateway 讀不到 `revoked.txt`、**fail-open**（撤銷 token 被放行至 ≤4h TTL）——ADR-028 具名 Risk Acceptance 被觸發、變可見。
**IR**：查 gateway 的 `revoked.txt` projected-volume mount + `tenant-federation-store` ConfigMap；**持續**失敗（非 pod 啟動 / remount 瞬態）可能是 mount 遭竄改（DoS 防禦本身，見 [#996](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/996)）。真正的 fail-closed 降級是 #996 的 defer-with-trigger。

## 誠實邊界（ADR-028）

tamper-**evident** 非 proof。錨定 tenant-api 範圍威脅（偷 SA / RCE），被更大的（VictoriaLogs / 全叢集）compromise 打穿。覆蓋只到「已成功發事件的撤銷」；**部署後 ≤4h ramp**（事件發射上線前的既有撤銷無 log、其 un-revoke 要到自然過期才不再是盲點）。**tenant-api 須跑 log level ≤ Info**——撤銷事件是 Info 級，跑 Warn+ 會靜默過濾掉、令 tamper-evidence 失效。**dual-write gap（accepted risk）**：事件在 ConfigMap commit 後才發，pod 若在該奈秒間隙硬死（OOM/node crash）則撤銷生效但事件丟失、該 token 失錨——Outbox pattern 可封但對 4h-TTL 過度工程，接受此雙巧合風險。**large-payload / OOM**：每輪把 24h 窗事件整包讀入記憶體，攻擊者狂灌撤銷（或極大量）可撐破 `resources.limits.memory` → OOMKill；因 fail-closed，反覆 OOM 令 `ReconcileStale` 觸發（被攻擊致瞎＝告警，非靜默），streaming／調記憶體為 defer-with-trigger。**schema-drift 盲點**：若 tenant-api 事件格式漂移使全部 row 解析失敗，對帳出 0 事件但 `last_reconcile_ts` 照刷（看似健康），真 un-revoke 會漏報——用 `federation_revocation_events_dropped` gauge 讓漂移可見（非零即查），專屬告警為 defer-with-trigger。**空通道盲點（#1234，已封）**：ADR-028 §D3 原本假設撤銷事件「走既有 Vector 管線」進 VictoriaLogs，該前提**當時不成立**（source selector 只 tail federation-gateway），於是查詢**永遠回零筆**、對帳「成功」、`last_reconcile_ts` 照刷——整個控制是**看起來接好的 no-op**。⛔ 這個盲點的關鍵性質是「零筆」同時是**健康狀態的合法觀測**，所以任何純偵測端的推論都封不住它；封法只能是**在來源端注入已知訊號**——tenant-api 每 5m 發 canary、`channel_up` 斷言它到得了，缺席即 `EvidenceChannelDown`。**殘餘邊界**：canary 只證明**通道**活著，不證明**撤銷事件**也走同一條路——兩者共用同一個 transform 與 sink（`vector test` 的 `projection_tests.yaml` 機械釘住同路徑），但若 `evidenceChannel.events` 只留 canary 而移除 `federation_token_revoked`，通道會顯示健康而撤銷證據全被丟棄。改那份 allowlist 是 security-reviewed 動作，正因如此。

## 上線前 chaos 驗證（推薦，外審 Gemini 補）

promtool 是理論契約；推正式前在 `vibe-k8s-lab` 實驗叢集實地驗一輪，確認真實 scrape interval 下的收斂時間與有無拍頻（beat frequency）：

1. **fail-open（`GatewayRevocationLoadFailure`）**：手動刪 / 卸載 gateway 的 `revoked.txt` projected key → 觀察 `federation_gateway_revocation_load_errors` 在近窗上升、告警於 `for:2m` 後觸發、復原後回落。
2. **fail-closed（`ReconcileStale`）**：暫停 VictoriaLogs Service（或 NetworkPolicy 擋 egress）→ 觀察 `reconcile_errors_total` 上升、`last_reconcile_ts` 停滯、`ReconcileStale` 於 `for:10m` 後觸發（絕不誤報 all-clear）。
3. **schema-drift（`events_dropped`）**：注入缺欄位的 `federation_token_revoked` 測試事件 → 觀察 `events_dropped` 上升而非靜默。
4. **拍頻**：確認 reconcile interval（300s）與 Prometheus scrape interval 不會在 `for:` 邊界產生 flap；必要時調 `for:` 或 interval。
5. **證據通道斷裂（`EvidenceChannelDown`，#1234）**——本 runbook 唯一驗「偵測面有沒有輸入」的場景，**務必實跑**（其餘四場景驗的是「有輸入時判斷對不對」）。三種等價注入，任一即可，建議依序遞增可信度：
   - **停 producer**：`kubectl -n tenant-api scale deploy/tenant-api --replicas=0` → 觀察 `federation_revocation_heartbeats_seen` 在一個 30m 窗內衰減至 0、`federation_revocation_channel_up` 落 0、告警於 `for:15m` 後觸發；復原後心跳應在**一個 reconcile 週期內**（≤300s）把 `channel_up` 拉回 1（producer 啟動即刻發一次，不等第一個 tick）。
   - **拔掉 Vector source**：把 `source.extraLabelSelector` 改回只選 federation-gateway → 驗**同一個** metric 反應。這條同時驗證了 §D 的 selector 是真的載重路徑，而非只是 values 上的裝飾。
   - **模擬 producer 改名**（最貼近真實失效）：把 `evidenceChannel.events` 的心跳項改成一個 tenant-api 不會發的名字 → 驗「allowlist 與 producer 失配」這個**最可能的真實成因**確實被抓到，而 `vector_component_discarded_events_total{component_id="federation_evidence"}` 這條 counter 對它**沒有可讀反應**（設計上就近乎 100% 丟棄；見 [platform-log-aggregation-runbook §8.7](platform-log-aggregation-runbook.md)）。
   ⛔ **必須一併驗反向（fail-closed 不誤報）**：擋掉 reconciler → VictoriaLogs 的 egress（NetworkPolicy 或停 Service）→ 應觸發 `ReconcileStale`、而 `channel_up` **維持前值不落 0**、`EvidenceChannelDown` **不**因此新觸發。少了這一步就無法區分「通道斷」與「查不到」，而兩者的 IR 路徑完全不同。
