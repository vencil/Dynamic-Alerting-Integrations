---
section: Security
topic: portal
issues: [3521]
created: 2026-11-01T23:35:29+00:00
---
- **portal 的建置與測試工具升 major，清掉 dev 工具鏈的 critical／high 漏洞（[#3521](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3521)）**：vitest 2.1.9 → 5.0.5（vite 7 改為明列的 dev 依賴，`@vitejs/plugin-react` → 5.2.0），esbuild 0.24.2 → 0.28.2。tinypool 的 RCE gadget、vitest UI server 讀檔、vite path traversal、esbuild dev server 等問題一併消失，`npm audit` 由 11 筆降為 5 筆 moderate；剩下的全部來自 Stryker 鎖定的 minimatch 與 qs，要等上游修。只影響開發與 CI，不影響出貨內容；`docs/assets/dist` 會重新產生，chunk 檔名跟著換，內容是 esbuild 的等價改寫。
