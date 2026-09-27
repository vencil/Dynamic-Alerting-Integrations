---
section: Fixed
topic: cli-docs-accuracy
issues: [1380]
created: 2026-09-27T02:38:42+00:00
---
- **動手實驗從頭到尾照抄都對得上工具（docs；[#1380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1380)）**：練習 2 的租戶片段補上 init 產出的 `tenants:` → `<租戶名稱>:` 外框（原本整份貼上會 `0 tenant(s)` 卻顯示 PASS），新增 `_routing_profiles.yaml` 與 `_domain_policy.yaml`，拿掉不會生效的 `_domain_policy: finance`；練習 1、3、4 的預期輸出改為實跑結果；練習 5 第 4 層照實寫成未設定；練習 7 依設計的行為矩陣改寫（維護模式讓告警不觸發、靜音模式只擋通知），改用 threshold-exporter 的旗標 metric 觀察；glossary 的 Maintenance Mode 與「三態運營」條目原本寫成「sentinel＋inhibit」，改成與設計一致的 PromQL `unless`；練習 8 的 domain policy 真的會生效。⚠️ 發版整理時：頁首的「版本」說明是為 v2.9.0 image 寫的，新版 da-tools 發布後要一併拿掉。
