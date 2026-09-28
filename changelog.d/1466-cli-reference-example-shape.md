---
section: Fixed
topic: docs
issues: [1466]
created: 2026-09-28T07:00:57+00:00
---
- **CLI Reference 英文版範例改回 `da-tools <cmd>` 簡寫，範例路徑一律相對（[#1466](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1466)）**：英文頁原本把約 50 個範例寫成各自帶掛載的完整 `docker run`，與中文頁及 Docker 使用模式不一致，其中一例還帶了 docker 沒有的 `--kubeconfig` 旗標；現在與中文頁同形，前綴只剩 Docker 使用模式一份。兩頁用到 `/tmp`、`/data/...` 的範例改成相對路徑（容器只看得到目前目錄），並註明 da-tools 映像不含 `opa`。Migration Guide 的簡寫提示改指向 Docker 使用模式。
