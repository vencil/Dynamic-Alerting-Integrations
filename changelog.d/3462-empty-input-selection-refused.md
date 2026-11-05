---
section: Changed
topic: da-tools
issues: [3462, 3548]
created: 2026-11-05T14:05:03+00:00
---
- **⚠️ Breaking：輸入目錄存在卻一個檔都沒選到時改為呼叫端錯誤，不再當成「沒有東西」照常產出（ops 工具；[#3462](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3462)、[#3548](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3548)）**：operator-generate、operator-check、runtime-audit 的 `--rule-packs-dir` 沒有任何 rule pack，以及 migrate-to-operator 的 `--source-dir` 頂層沒有 config 檔時，結束碼由 0 改為 2，什麼都不寫；`lint --ci` 一個檔都沒選到時也由 0 改為 2。operator-generate 與 runtime-audit 的預設 `rule-packs` 不存在時，比照明確給出的值判定；operator-check 則不比對數量，並在該列註明。state-reconcile 的預設 `.da/state` 那裡有東西卻不是目錄（一般檔、懸空 symlink、上層是一般檔或懸空 symlink）時，由 0（覆寫 manifest）改為 2。這些工具中有 `--json` 的，拒絕時 stdout 皆為空（`lint` 沒有 `--json`）。
