---
section: Security
topic: tenant-api
issues: [2928]
created: 2026-10-13T14:21:00+00:00
---
- **AccountID 高水位：帳本被蓋回舊版時不再重發 id（tenant-api；[#2928](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2928)）**：配發過的最大 AccountID 另記在 federation token ConfigMap 的 annotation `tenant-api.vibe/account-id-high-water`，不跟 `tenant-api-state` 一起被還原。每次配發先預留高水位（連同預留當時帳本的 `next_account_id` 一起記下）、再寫帳本，配發前比對：帳本計數器低於上次預留的起點（舊帳本在運行中被蓋回）即拒絕配發、啟動時拒絕啟動，指示跑 `tenant-api state restore`；介於起點與高水位之間（上次預留沒寫進帳本，含整批 Backfill 失敗）就跳過整段；高水位落後（升級後第一次啟動）則補齊。`state restore` 合併後的計數器會高於高水位。chart 的 namespace 搬遷守衛也把這個 annotation 當資料，搬遷步驟要連同它一起搬。升級後第一次啟動以當下帳本為基準，若當時帳本已倒退，這個機制救不回。
