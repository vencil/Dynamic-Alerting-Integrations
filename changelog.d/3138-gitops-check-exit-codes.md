---
section: Fixed
topic: docs
issues: [3138]
created: 2026-10-20T12:49:36+00:00
---
- **CLI Reference 的 `gitops-check` 補上結束碼表（[#3138](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3138)）**：中英兩頁寫明 0／1／2 各在什麼情況出現：只有 `sidecar` 會給 warning，帶 `--ci` 時 warning 是 0、不帶是 1；`local --dir` 指到不存在的目錄是 1 而不是 2；沒給子命令、參數錯誤、`local` 沒有可用的 da-guard 都是 2。這張表現在也由 CLI 契約檢查比對程式實際的結束碼。
