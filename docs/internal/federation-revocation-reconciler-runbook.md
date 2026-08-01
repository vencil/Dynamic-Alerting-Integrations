# Federation 撤銷 reconciler — 運維 / IR runbook

> ADR-028 D1（#924）的偵測端。搭配 [ADR-028](../adr/028-federation-revocation-tamper-evidence.md) 讀。

## 這是什麼

一支長駐 **Deployment**（`helm/federation-reconciler`，跑 da-tools image 內的 `_federation_revocation_reconciler.py`），週期性把**撤銷事件日誌**（VictoriaLogs，`log_type:"federation_evidence" AND event:"federation_token_revoked"`——#1237 起限定串流，與 heartbeat canary 同類別）跟 **live 撤銷集**（`tenant-federation-store` ConfigMap 的 `revoked.txt`，以唯讀 volume mount）對帳，偵測 **un-revoke**（有寫入權的攻擊者把未過期的撤銷偷偷刪掉），並用 `/metrics` 暴露給 Prometheus。

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
| `federation_revocation_live_set_rejected_lines` | gauge | **執行面凍結——成因側**（#1235 / TRK-349）：掛載的 `revoked.txt` 中違反 token-id 行契約的行數，以**本行程讀到的檔案**為準。非零＝gateway 正在**拒載整份**撤銷集。只計數，**永不**回報行內容（ADR-028 D3）|
| `federation_gateway_revoked_set_reload_rejected` | gauge | **執行面凍結——gateway 自述**（#1235 / TRK-349）：近窗（~10m）從 **gateway 自己的 log stream** 讀回的拒載 warn 數（查詢以 `log_type:"gateway_operational"` 限定串流）。與上一列**觀測不同平面**（兩端讀的是同一份 ConfigMap 的不同 kubelet 投影、時間點也不同），故**兩者皆需要**、任一可單獨非零 |
| `federation_gateway_revoked_set_missing` | gauge | **執行面空集合**（#1236 / TRK-350）：近窗（~10m）gateway 自報「撤銷清單檔案不存在」的 warn 數。⛔ **與上面兩列不同族**：那兩列是「凍結在舊集合」（stale but enforcing），這一列是 gateway **手上什麼都沒有**——每個已撤銷 token 都被放行至 TTL。這是 ADR-028 §相鄰破口 點名的攻擊形態（刪／卸載 projected key），也是 namespace 錯配的長相（#1313）|

## 告警與 IR

### `FederationRevocationTamperSuspected`（critical）
**意義**：log 說某 token 已撤銷且未過期，但它不在 live 撤銷集 → 疑似 un-revoke。
**IR**：
1. 查 VictoriaLogs 拿 opaque token_id：用 `log_type:"federation_evidence" AND event:"federation_token_revoked"` 過濾近 24h（與 reconciler 同一支查詢；#1237 起加 `log_type` 限定，避免比對到非權威來源灌入的同名事件）（reconciler pod log 也會印 `TAMPER SUSPECTED: ...`）。
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

⛔ **本告警只吃「讀不到」那一句 warn**（`federation: revoked-set reload failed`）。gateway 另有**兩句**語意不同的 warn，各有自己的 gauge 與告警，見下（`revoked-set rejected` 與 `revoked-set missing`）。三句在 reconciler 端以「兩兩互不為子字串」的機械斷言釘住，所以不會互相吃到對方的 row。

### `FederationGatewayRevokedSetMissing`（critical；#1236 / TRK-350）

**意義**：gateway 自報撤銷清單檔案**不存在**。⛔ 這是三條 gateway 告警中**唯一「執行面為空集合」**的：另外兩條都保留了前一份撤銷集（stale but enforcing），這一條代表 gateway 手上什麼都沒有——**每個已撤銷 token 都被放行至 TTL（≤4h）**。因此是 critical 而非 warning。

**長相**：gateway 容器 log 出現 `federation: revoked-set missing: /etc/revoked/revoked.txt is absent; enforcing an EMPTY set — every revoked token is honoured until its TTL`。
手查：`log_type:"gateway_operational" AND app:"envoy" AND "federation: revoked-set missing"`（來源限定的誠實邊界同 §fail-open 那條）。

**IR**：

1. **先分辨錯配 vs 攻擊**——這兩者長得一模一樣，但處置完全不同。查 store ConfigMap 是否存在於 **gateway 自己的 namespace**：ConfigMap 是 namespace-scoped，projected volume 只解析得到掛載 pod 自身 namespace 的那一個。tenant-api 的 `federation.store.namespace` 必須指向 gateway 與 reconciler 所在的 namespace（三方相等，見 [#1313](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1313)）。**新裝或剛改過拓撲就響 ⇒ 幾乎必然是錯配。**
2. **ConfigMap 在、但 `revoked.txt` key 不見了** ⇒ 這是帶外寫入。合法路徑（tenant-api `revoke()`／任何 store 寫入）每次都會重生該 key，不可能刪掉它。併查 [#926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/926) audit 看誰寫過 `tenant-federation-store`，並視為 incident 處理——ADR-028 §相鄰破口 正是把「刪／卸載 projected key」列為攻擊者在寫入路徑被封死後的下一步。
3. **確認曝險窗**：從第一筆這句 warn 到修復之間，**所有**已撤銷但未過期的 token 都可用。撤銷集復原後，gateway 於下一個 `reloadIntervalSeconds`（預設 30s）內重新載入。必要時縮短 token TTL 或輪替簽章金鑰以強制失效。
4. ⚠️ **不要**用 `FederationRevocationTamperSuspected` 是否同時響來判斷嚴重度：那條比對的是 audit log 與**檔案**，而檔案不見時它讀到空集，只有在窗內仍有未過期撤銷事件時才會響。近 4h 無撤銷 ⇒ 它保持綠，但本告警描述的曝險依然成立。
手查 VictoriaLogs（與 reconciler 同一支查詢）：`log_type:"gateway_operational" AND app:"envoy" AND "federation: revoked-set reload failed"`（#1237 起限定串流類別＋Envoy 容器，排除同 pod 的 mtail／logrotate sidecar 噪音；⚠️ 此限定只排除**合法** sidecar，非來源身分驗證——見 ADR-028 D3 §偵測查詢的來源限定的誠實邊界）。

### `FederationRevocationLiveSetRejected` ／ `FederationGatewayRevokedSetReloadRejected`（皆 critical；#1235 / TRK-349）

**長相**：gateway 容器 log 出現 `federation: revoked-set rejected: line does not match the token-id contract; keeping the previously loaded set`；reconciler pod log 出現 `revoked.txt: N line(s) rejected by the token-id contract`。

**兩條告警分別是什麼**：

- **`FederationRevocationLiveSetRejected`**（`federation_revocation_live_set_rejected_lines > 0`, `for:10m`, critical）——**成因側**。本 reconciler 從掛載的 `revoked.txt` 讀到違反行契約的行。它看得到成因，但讀的是**自己那份 kubelet 投影**，未必等於 gateway 當下載到的那份。
- **`FederationGatewayRevokedSetReloadRejected`**（`federation_gateway_revoked_set_reload_rejected > 0`, `for:10m`, critical）——**執行面自述**。直接從 gateway 自己的 log stream 讀回牠拒載了幾次。它不受上述投影／時間差影響，但對「沒產生那句 warn 的成因」是啞的。手查：`log_type:"gateway_operational" AND app:"envoy" AND "federation: revoked-set rejected"`（#1237 起限定，同上一段的誠實邊界）。
- ⛔ **兩者刻意不是冗餘**：各自在對方看不見的地方看得見，**任一可單獨非零**。看到其中一條就把另一條一起調出來看。
- **`for:10m` 的來由**：reconcile 週期 300s，10m ＝一個完整週期再加等量餘裕，足以吸收單次異常（例如讀到正在被 kubelet 換版的投影）。此條件是**持久性**的（只有帶外寫入造得出來、只有人為改得掉），窗開長不花任何成本——與 `FederationGatewayRevocationLoadFailure` 的 `for:2m`（去抖真正瞬態的 I/O）不同。

⛔ **不要與 `FederationGatewayRevocationLoadFailure` 混為一談——兩者姿態相反**：

| | `revoked-set reload failed` | `revoked-set rejected` |
|---|---|---|
| 發生了什麼 | 清單**讀不到** | 清單讀到了、但**被拒絕** |
| 目前的放行姿態 | **fail-open**：撤銷 token 正在被放行 | **沒有**放行任何東西：沿用前一份（竄改前）的撤銷集，仍在擋 |
| 對應訊號 | `federation_gateway_revocation_load_errors` ↑、`FederationGatewayRevocationLoadFailure`（warning, `for:2m`）| `federation_revocation_live_set_rejected_lines` ／ `federation_gateway_revoked_set_reload_rejected` ↑、上面兩條 critical 告警（`for:10m`）|
| 首要動作 | 修 mount / ConfigMap 可達性 | 查**誰寫壞了 `revoked.txt`** |

⛔ **這兩條告警在講「執行面凍結」，不是「有東西被放行」。判讀時務必分開三種姿態**：

1. **FREEZE（穩態，最常見）**：gateway 沿用前一份 set，該 set 裡的 token 仍被 403。但**壞行出現之後才發出的每一筆撤銷都沒有載入、沒有生效**，而且會一直如此直到壞行被清掉。
   ⛔ **`FederationRevocationTamperSuspected` 對這些新撤銷不會響**——它比對的是 audit log **vs 檔案**，而檔案裡**確實有**那些新撤銷；壞掉的是 gateway **載到的**東西。`revoked.txt` **不見**一直是大聲的（偵測端讀到空集合、全部 token 都「缺席」），**被拒**卻曾經是無聲的——這兩條告警就是在補這個不對稱。
2. **COLD START（更糟，優先排除）**：pod **剛起來、第一次載入**就撞到壞行 → **沒有前值可留**，該 worker 的撤銷集為空 → 與 missing-file 同姿態、**fail OPEN**，該 worker 對任何撤銷 token 都放行。
   **怎麼查**：`kubectl -n <ns> get pod -l app=federation-gateway -o wide` 取各 pod 的 `startTime`，比對 gateway log 中**首見**該 warn 的時間戳；只要有任一 pod 的啟動時間落在首見 warn **之後**（或兩者相距在一個 `reloadIntervalSeconds` 內），就當作冷啟動處理：**先讓 `revoked.txt` 回到合法內容再重啟該 pod**，不要先重啟——重啟到同一份壞檔只會再冷啟動一次。滾動更新／節點驅逐/擴容都會製造新 pod，所以只要壞行還在，冷啟動風險就一直存在。
3. **不是 fail-open（就 FREEZE 而言）**：沿用中的那份 set 沒有多放行任何東西。把這兩條讀成 `LoadFailure` 會讓 IR 走錯方向。

**為什麼是「整份作廢」而非「跳過那一行」**：`revoked.txt` 由 gateway（執行端）與本 reconciler（偵測端）兩個獨立實作讀取，兩端必須接受完全相同的行；跳過單行會讓兩端集合再度分岔。作廢整輪＝沿用竄改前的 set＝**攻擊被擋**（ADR-028 §D3 解析契約）。

**IR**：

1. **確認執行面沒有破口**：這行 warn 出現時 gateway 仍在用前一份 set，撤銷仍生效。**冷啟動例外**：若 pod 是**剛起來、第一次載入**就撞到這行，就沒有前值可留 → 該 worker 的撤銷集為空（與 missing-file 同姿態，fail-open）。查 pod 啟動時間 vs 首見這行 warn 的時間；若吻合，優先重啟／回滾 `revoked.txt` 內容而不是慢慢查（先回滾內容、再重啟，見上面 COLD START）。
2. **查偵測面有沒有同時響**：reconciler pod log 會印 `revoked.txt: N line(s) rejected by the token-id contract`。若該行本來承載的是一個**真的被撤銷過**的 token_id，`FederationRevocationTamperSuspected` 會跟著觸發——那才是 IR 的主線，走上面 TamperSuspected 的步驟。⚠️ 反過來**不成立**：`TamperSuspected` 保持綠**不代表**沒問題，壞行之後的新撤銷不會讓它響（見上面 FREEZE）。
3. **回溯寫入者**：合法路徑（tenant-api `revoke()`）已在寫入端驗 charset，不可能產生這種行。所以來源是 (a) 直接對 ConfigMap 的手改／自動化，(b) 早於該防線寫入的殘留 entry，或 (c) 有寫入權的攻擊者。併查 #926 audit 看誰寫過 `tenant-federation-store`。
4. **清理**：tenant-api **不會**在載入時把不合法 entry 剔除——那等於替攻擊者完成 un-revoke（`revokedText()` 每次寫入都從 store 逐字重生）。要移除必須是人為、確認過該 token 已過期或本就不該在集合裡之後，直接改 `store.json`。

⛔ **絕不要自動移除那筆 entry**（含任何「順手清一下」的腳本／自動化）：`revoked.txt` 每次寫入都從 store 文件逐字重生，移除它＝**由平台親手完成攻擊者要做的 un-revoke**。壞 entry 留著才是安全的一邊——它會持續被寫出、持續對偵測端可見、持續被 gateway 的整份檢查擋下。

**為什麼是兩個 gauge 而不是一個**：見上面「兩條告警分別是什麼」。早先版本刻意**不加** metric，理由是「被拒的行意味該 id 不在 live set，若它真的被撤銷過 `TamperSuspected` 就會響」——那個推理只涵蓋**壞行上的那個 id**，對**其他** id 一句話都沒說；而 gateway 的處置是作廢**整輪**，於是壞行之後的所有撤銷都未執行，同時 audit log 與檔案對它們的認知完全一致、`tamper_suspected` 維持 0。**不要拿當初用來否決它的那個論證去移除這兩個 gauge。**

## 誠實邊界（ADR-028）

tamper-**evident** 非 proof。錨定 tenant-api 範圍威脅（偷 SA / RCE），被更大的（VictoriaLogs / 全叢集）compromise 打穿。覆蓋只到「已成功發事件的撤銷」；**部署後 ≤4h ramp**（事件發射上線前的既有撤銷無 log、其 un-revoke 要到自然過期才不再是盲點）。**tenant-api 須跑 log level ≤ Info**——撤銷事件是 Info 級，跑 Warn+ 會靜默過濾掉、令 tamper-evidence 失效。**dual-write gap（accepted risk）**：事件在 ConfigMap commit 後才發，pod 若在該奈秒間隙硬死（OOM/node crash）則撤銷生效但事件丟失、該 token 失錨——Outbox pattern 可封但對 4h-TTL 過度工程，接受此雙巧合風險。**large-payload / OOM**：每輪把 24h 窗事件整包讀入記憶體，攻擊者狂灌撤銷（或極大量）可撐破 `resources.limits.memory` → OOMKill；因 fail-closed，反覆 OOM 令 `ReconcileStale` 觸發（被攻擊致瞎＝告警，非靜默），streaming／調記憶體為 defer-with-trigger。**schema-drift 盲點**：若 tenant-api 事件格式漂移使全部 row 解析失敗，對帳出 0 事件但 `last_reconcile_ts` 照刷（看似健康），真 un-revoke 會漏報——用 `federation_revocation_events_dropped` gauge 讓漂移可見（非零即查），專屬告警為 defer-with-trigger。**空通道盲點（#1234，已封）**：ADR-028 §D3 原本假設撤銷事件「走既有 Vector 管線」進 VictoriaLogs，該前提**當時不成立**（source selector 只 tail federation-gateway），於是查詢**永遠回零筆**、對帳「成功」、`last_reconcile_ts` 照刷——整個控制是**看起來接好的 no-op**。⛔ 這個盲點的關鍵性質是「零筆」同時是**健康狀態的合法觀測**，所以任何純偵測端的推論都封不住它；封法只能是**在來源端注入已知訊號**——tenant-api 每 5m 發 canary、`channel_up` 斷言它到得了，缺席即 `EvidenceChannelDown`。**殘餘邊界**：canary 只證明**通道**活著，不證明**撤銷事件**也走同一條路——兩者共用同一個 transform 與 sink（`vector test` 的 `projection_tests.yaml` 機械釘住同路徑），但若 `evidenceChannel.events` 只留 canary 而移除 `federation_token_revoked`，通道會顯示健康而撤銷證據全被丟棄。改那份 allowlist 是 security-reviewed 動作，正因如此。

## 上線前 chaos 驗證（推薦，外審 Gemini 補）

promtool 是理論契約；推正式前在 `vibe-k8s-lab` 實驗叢集實地驗一輪，確認真實 scrape interval 下的收斂時間與有無拍頻（beat frequency）：

1. **檔案 missing（`GatewayRevokedSetMissing`，#1236）**：手動刪 / 卸載 gateway 的 `revoked.txt` projected key → 觀察 **`federation_gateway_revoked_set_missing`** 在近窗上升、告警於 `for:5m` 後觸發 critical、復原後回落。⚠️ **這一步在 #1236 之前是一份跑不通的驗收程序**：原文要求觀察 `federation_gateway_revocation_load_errors`，但刪 key 走的是 Lua 的 missing 分支——它在 pcall 內正常 return，兩個既有 warn 都不發射，所以那個 gauge 恆 0、告警永不觸發。對應本注入的是這條新 gauge。
1b. **fail-open（`GatewayRevocationLoadFailure`）**：這條要用**讀取中途失敗**（而非檔案不存在）才觸發，例如在 gateway 讀取期間抽換 projected volume 的 inode。若只是要驗告警鏈路本身，直接對該 gauge 灌值比構造真實 I/O 失敗實際得多。
2. **fail-closed（`ReconcileStale`）**：暫停 VictoriaLogs Service（或 NetworkPolicy 擋 egress）→ 觀察 `reconcile_errors_total` 上升、`last_reconcile_ts` 停滯、`ReconcileStale` 於 `for:10m` 後觸發（絕不誤報 all-clear）。
3. **schema-drift（`events_dropped`）**：注入缺欄位的 `federation_token_revoked` 測試事件 → 觀察 `events_dropped` 上升而非靜默。
4. **拍頻**：確認 reconcile interval（300s）與 Prometheus scrape interval 不會在 `for:` 邊界產生 flap；必要時調 `for:` 或 interval。
5. **證據通道斷裂（`EvidenceChannelDown`，#1234）**——本 runbook 唯一驗「偵測面有沒有輸入」的場景，**務必實跑**（其餘四場景驗的是「有輸入時判斷對不對」）。三種等價注入，任一即可，建議依序遞增可信度：
   - **停 producer**：`kubectl -n tenant-api scale deploy/tenant-api --replicas=0` → 觀察 `federation_revocation_heartbeats_seen` 在一個 30m 窗內衰減至 0、`federation_revocation_channel_up` 落 0、告警於 `for:15m` 後觸發；復原後心跳應在**一個 reconcile 週期內**（≤300s）把 `channel_up` 拉回 1（producer 啟動即刻發一次，不等第一個 tick）。
   - **拔掉 Vector source**：把 `source.extraLabelSelector` 改回只選 federation-gateway → 驗**同一個** metric 反應。這條同時驗證了 §D 的 selector 是真的載重路徑，而非只是 values 上的裝飾。
   - **模擬 producer 改名**（最貼近真實失效）：把 `evidenceChannel.events` 的心跳項改成一個 tenant-api 不會發的名字 → 驗「allowlist 與 producer 失配」這個**最可能的真實成因**確實被抓到，而 `vector_component_discarded_events_total{component_id="federation_evidence"}` 這條 counter 對它**沒有可讀反應**（設計上就近乎 100% 丟棄；見 [platform-log-aggregation-runbook §8.7](platform-log-aggregation-runbook.md)）。
   ⛔ **必須一併驗反向（fail-closed 不誤報）**：擋掉 reconciler → VictoriaLogs 的 egress（NetworkPolicy 或停 Service）→ 應觸發 `ReconcileStale`、而 `channel_up` **維持前值不落 0**、`EvidenceChannelDown` **不**因此新觸發。少了這一步就無法區分「通道斷」與「查不到」，而兩者的 IR 路徑完全不同。
