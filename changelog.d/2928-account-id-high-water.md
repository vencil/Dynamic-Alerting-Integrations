---
section: Security
topic: tenant-api
issues: [2928]
created: 2026-10-13T14:21:00+00:00
---
- **AccountID 高水位：帳本被蓋回舊版時不再重發 id（tenant-api；[#2928](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2928)）**：配發過的最大 AccountID 另記在 federation token ConfigMap 的 annotation `tenant-api.vibe/account-id-high-water`，不跟 `tenant-api-state` 一起被還原。每次配發先寫高水位、再寫帳本，配發前比對：帳本計數器落後高水位超過一筆（舊帳本在運行中被蓋回）即拒絕配發、啟動時拒絕啟動，指示跑 `tenant-api state restore`；剛好落後一筆（高水位寫成、帳本寫入失敗）就跳過那個號；高水位落後（升級後第一次啟動）則補齊。`state restore` 合併後的計數器會高於高水位。chart 的 namespace 搬遷守衛也把這個 annotation 當資料，搬遷步驟要連同它一起搬。升級後第一次啟動以當下帳本為基準，若當時帳本已倒退，這個機制救不回。
