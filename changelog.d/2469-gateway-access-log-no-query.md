---
section: Security
topic: federation
issues: [2469]
created: 2026-09-30T15:10:00+00:00
---
- **federation-gateway 的 access log 不再記 query string（helm；chart 0.6.1；[#2469](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2469)）**：`path` 欄位由 `%REQ(:PATH)%` 改為 `%REQ_WITHOUT_QUERY(:PATH)%`，stdout 與稽核鏡像兩個 sink 同時生效。先前放在 URL 的 `?access_token=` 雖然不被接受（只放 URL 回 401，header 已帶時照常放行），仍會隨 path 寫進 log。PromQL／LogsQL selector 照舊記在 `query` 欄位；`time`、`start`、`end`、`step` 等其他 URL 參數不再出現在 gateway 的 log 裡。
