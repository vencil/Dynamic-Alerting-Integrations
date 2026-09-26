---
section: Changed
topic: ci-pipeline
issues: [1158, 2102]
created: 2026-09-26T17:00:00+00:00
---
- **CI 依路徑分流、分片與依賴 pin 收斂（ci）**：`ci.yml` 以 path filter 讓 docs-only／portal-only PR 跳過 Go 與 Python 測試，`Go Tests (1.26)`／`Python Tests (3.13)`／`Portal Tests` 改由恆跑的聚合 gate 回報（`Portal Tests` 因此可設為 required check），gate 的判定邏輯另以真值表測試釘住。Python 測試與 coverage 各切成三個平行分片（[#2102](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2102)），coverage 拆成 advisory 的 `Python Coverage (3.13)` job、不擋 merge。CI 與 devcontainer 的 pip 依賴 pin 到 `requirements/ci-constraints.txt` 單一 SSOT、`go install` 改釘版本，並新增未 pin 安裝的 pre-commit 閘門（[#1158](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1158)）；平台 ConfigMap 納入 pint 掃描。
