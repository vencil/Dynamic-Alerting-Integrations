---
section: Fixed
topic: da-tools
issues: [1374]
created: 2026-09-28T20:50:00+08:00
---
- **Windows 上直接執行的工具不再把子行程輸出解成 cp950（da-tools；[#1374](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1374)）**：`scripts/` 底下呼叫 git、kubectl、docker、gh、helm、node 等的地方一律以 UTF-8 解碼。以前在 zh-TW Windows 上遇到中文輸出時，呼叫端拿到的是空值而不是錯誤——例如 `gitops-check repo` 的 `config_path_verified` 在路徑存在時仍回 `false`，並把原因誤報成「伺服器可能不支援 git archive」。`coverage_gap_analysis` 另替 Python 子行程設 `PYTHONIOENCODING=utf-8`，讓兩端編碼一致。Linux 容器內的行為不變。
