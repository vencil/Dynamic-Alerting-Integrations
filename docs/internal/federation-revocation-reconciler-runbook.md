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

### `FederationGatewayRevocationLoadFailure`（warning）
**意義**：gateway 讀不到 `revoked.txt`、**fail-open**（撤銷 token 被放行至 ≤4h TTL）——ADR-028 具名 Risk Acceptance 被觸發、變可見。
**IR**：查 gateway 的 `revoked.txt` projected-volume mount + `tenant-federation-store` ConfigMap；**持續**失敗（非 pod 啟動 / remount 瞬態）可能是 mount 遭竄改（DoS 防禦本身，見 [#996](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/996)）。真正的 fail-closed 降級是 #996 的 defer-with-trigger。

## 誠實邊界（ADR-028）

tamper-**evident** 非 proof。錨定 tenant-api 範圍威脅（偷 SA / RCE），被更大的（VictoriaLogs / 全叢集）compromise 打穿。覆蓋只到「已成功發事件的撤銷」；**部署後 ≤4h ramp**（事件發射上線前的既有撤銷無 log、其 un-revoke 要到自然過期才不再是盲點）。**tenant-api 須跑 log level ≤ Info**——撤銷事件是 Info 級，跑 Warn+ 會靜默過濾掉、令 tamper-evidence 失效。**dual-write gap（accepted risk）**：事件在 ConfigMap commit 後才發，pod 若在該奈秒間隙硬死（OOM/node crash）則撤銷生效但事件丟失、該 token 失錨——Outbox pattern 可封但對 4h-TTL 過度工程，接受此雙巧合風險。**large-payload / OOM**：每輪把 24h 窗事件整包讀入記憶體，攻擊者狂灌撤銷（或極大量）可撐破 `resources.limits.memory` → OOMKill；因 fail-closed，反覆 OOM 令 `ReconcileStale` 觸發（被攻擊致瞎＝告警，非靜默），streaming／調記憶體為 defer-with-trigger。**schema-drift 盲點**：若 tenant-api 事件格式漂移使全部 row 解析失敗，對帳出 0 事件但 `last_reconcile_ts` 照刷（看似健康），真 un-revoke 會漏報——用 `federation_revocation_events_dropped` gauge 讓漂移可見（非零即查），專屬告警為 defer-with-trigger。

## 上線前 chaos 驗證（推薦，外審 Gemini 補）

promtool 是理論契約；推正式前在 `vibe-k8s-lab` 實驗叢集實地驗一輪，確認真實 scrape interval 下的收斂時間與有無拍頻（beat frequency）：

1. **fail-open（`GatewayRevocationLoadFailure`）**：手動刪 / 卸載 gateway 的 `revoked.txt` projected key → 觀察 `federation_gateway_revocation_load_errors` 在近窗上升、告警於 `for:2m` 後觸發、復原後回落。
2. **fail-closed（`ReconcileStale`）**：暫停 VictoriaLogs Service（或 NetworkPolicy 擋 egress）→ 觀察 `reconcile_errors_total` 上升、`last_reconcile_ts` 停滯、`ReconcileStale` 於 `for:10m` 後觸發（絕不誤報 all-clear）。
3. **schema-drift（`events_dropped`）**：注入缺欄位的 `federation_token_revoked` 測試事件 → 觀察 `events_dropped` 上升而非靜默。
4. **拍頻**：確認 reconcile interval（300s）與 Prometheus scrape interval 不會在 `for:` 邊界產生 flap；必要時調 `for:` 或 interval。
