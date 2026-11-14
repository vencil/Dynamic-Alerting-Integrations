---
section: Changed
topic: exporter
issues: [3703]
created: 2026-11-14T14:00:00+00:00
---
- **⚠️ BREAKING：`key_checks` 改成結構化記錄，產生器不再原樣印出鍵與 label 值（da-guard、route generator；[#3703](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3703)）**：served-values 與 key-verdict 的 `key_checks` 由 `{<租戶>: {version_labels, renames}}` 句子改為 `{<租戶>: [{kind, key, head, replacement_head, reason}]}`；`kind` 是封閉集合（4 種 `version_label_*`、3 種 `renamed*`），`reason` 是每個 kind 一句固定說明，只有 `key`、`head` 帶作者寫的字。key-verdict 文件升為 `da-guard.key-verdict/v4`（v3 視為舊版 da-guard）；served-values 仍為 v1，舊形狀視為舊版 da-guard。`generate-routes --validate` 與 validate-config 的那一行改成 `WARN: tenant=t: key check <kind>: metric <指標>, key <key:xxxxxx> — <說明>`（改名類為 NOTICE、多帶 `replacement metric`），finding 多帶 `field`。exporter 的 log 與 tenant-api 的訊息不變。比對舊句子的腳本要更新。
