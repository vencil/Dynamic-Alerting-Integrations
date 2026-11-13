---
section: Changed
topic: confd-family
issues: [3535]
created: 2026-11-13T07:27:32+00:00
---
- **conf.d 檔一律以 `0644` 寫出（da-guard、tenant-api；[#3535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3535)）**：conf.d 屬 repo 生成物，會 commit、由另一個 uid 的讀者讀（例如 try-local 的 exporter 與 tenant-api 分屬不同容器）。`da-guard write` 的新檔、取代、改寫與複製都寫成 `0644`，不再沿用原檔權限；搬移仍是同一個檔，權限不動。值只有一個來源：`pkg/config.ConfDFileMode`，tenant-api 所有 conf.d 寫入點共用，並與 Python `_lib_atomic_write` 的預設值由測試釘在一起。⚠️ 行為變更：之前 da-guard 的新檔是 `0600`，取代與改寫沿用原檔權限。結束碼不變。
