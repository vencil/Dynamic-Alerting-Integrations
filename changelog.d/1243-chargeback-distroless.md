---
section: Security
topic: security-supply-chain
issues: [1243]
created: 2026-09-29T15:00:00+00:00
---
- **chargeback-aggregator 改用 distroless Python 映像（helm；chart 0.2.3；[#1243](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1243)）**：映像由 `python:3.14-slim` 改為 `gcr.io/distroless/python3-debian13:nonroot`。原本的兩筆 HIGH 都出在 pip 自帶的 setuptools 與 msgpack；聚合腳本只用標準庫、執行時不安裝任何套件，改用不含 pip 的映像後就沒有這兩筆。⚠️ Python 由 3.14 變為 3.13，映像內也沒有 shell，排錯時 `kubectl exec … sh` 無法使用。
