---
section: Added
topic: da-tools
issues: [1516]
created: 2026-10-10T17:30:00+08:00
---
- **ADR-037：config-diff 改為比較渲染結果（[#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516)）**：平台層設定（`_defaults.yaml` 裡 `defaults:` 以外的鍵、routing profile、根目錄平台檔的 `tenants`）的變更，目前的 PR 報告都說不出影響了誰。ADR-037 決定 config-diff 改成在 base 與 PR 兩側各跑一次 `da-guard served-values` 與路由產生器，比較兩邊的渲染結果，並具名列出比不到的設定。這是 breaking change：報告與 JSON 結構都會改，且一律需要 da-guard。實作另行進行。
