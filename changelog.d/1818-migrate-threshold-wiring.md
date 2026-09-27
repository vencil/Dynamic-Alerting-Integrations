---
section: Fixed
topic: da-tools
issues: [1818]
created: 2026-09-27T16:51:23+00:00
---
- **migrate 產出的告警規則終於會響（da-tools；[#1818](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1818)）**：先前產物有三處讓規則在 runtime 永遠不響，而且沒有任何錯誤訊息。一、recording rule 把來源指標也加上 `custom_` 前綴，讀的是沒人產生的 series。二、閾值 selector 寫 `metric="custom_<key>"`，exporter 發射的卻是 `component="custom", metric="<key>"`。三、產出的 key 沒在 `_defaults.yaml` 宣告，exporter 不發射。現在前綴只加在 key 與 record 名稱上；selector 依 exporter 的拆法產生；另產出 `defaults-snippet.yaml`，合併進 `_defaults.yaml` 後 warning 層對所有租戶生效。有 warning 配對的 critical 層不能放 defaults，要各租戶自己寫 `<key>_critical`；只有 critical 的舊規則改讀 base 列，照樣對所有租戶生效。同一個指標被多條規則設不同門檻時，改為取第一條並在輸出與報告點名衝突，不再悄悄留下最後一條。`prefix-mapping.yaml` 的 `_critical` 項也改為記原本的指標名。v2.9.0 映像的 migrate 還沒有這些修正。
