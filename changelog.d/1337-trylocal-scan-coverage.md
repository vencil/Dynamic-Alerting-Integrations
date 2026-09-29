---
section: Security
topic: security-supply-chain
issues: [1337]
created: 2026-09-29T14:30:00+00:00
---
- **try-local 示範環境拉的第三方映像全部納入夜掃（[#1337](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1337)）**：`prom/pushgateway` 與 `curlimages/curl` 只有 try-local 用到，先前不在任何 CVE 掃描裡，現在加進夜掃第三方 matrix（tag＋digest）。新的守衛以「映像名＋tag」比對 try-local 與 matrix：try-local 拉的第三方映像只要有一顆沒被掃到就轉紅；try-local 仍只寫 tag。⚠️ Renovate 升 matrix 時不會一起改 try-local，那支 PR 會被這道守衛擋下，要手動把 `try-local/docker-compose.yaml` 改到同一個 tag。
