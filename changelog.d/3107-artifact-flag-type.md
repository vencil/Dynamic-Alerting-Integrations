---
section: Changed
topic: da-tools
issues: [3107]
created: 2026-11-19T03:00:00+00:00
---
- **另外兩個 artifact 旗標在解析時拒收 `-` 與空值（[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)）**：`configmap_assemble.py --output`、`state-reconcile --manifest-path` 給 `-` 或空值時，argparse 點名旗標、結束碼 `2`，什麼都不做。以前 `state-reconcile` 要到解析之後、讀舊 manifest 之前的檢查才拒收（空值的訊息是 `cannot write .`）；`configmap_assemble` 要到組完 manifest 之後的寫入前檢查才拒收。`state-reconcile --manifest-path ./-` 以前被當成 `-` 拒收，現在寫出名為 `-` 的檔。
