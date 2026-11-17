---
section: Fixed
topic: portal
issues: [3964]
created: 2026-11-15T14:33:49+00:00
---
- **Config Lint 照 exporter 的讀法讀閾值，讀不懂的值另報「exporter 不讀」（portal；[#3964](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3964)）**：閾值規則先前用 `Number()` 讀頁面寬鬆解析器的值：未加引號的 `0x64` 讀成 100 並報「非常高」、`Infinity` 也照數字判，但 exporter 拒收這些值；反過來，exporter 照 96 讀的 `"96"`、`'96'`、`96   # 註解`、`96:critical` 都不報。現在 `threshold-too-high`／`threshold-too-low`／`critical-lower-than-warning` 改經 `parseTenantYaml` 與 `readThresholdRow` 讀值，和 Alert Preview、YAML Validator、Routing Trace 同一個讀法；新規則 `threshold-not-read` 點名 exporter 不讀的值；YAML 本身讀不了時改報一筆「閾值沒有檢查」。`_defaults` 的閾值與以 `<<` 合併進來的值讀不到原文、不再判斷，改各報一筆「沒有檢查」（info）；整份貼上的 conf.d 檔（`tenants:` 包一層）也不判斷。
