---
section: Changed
topic: test-infra
issues: [1349, 1454, 1746]
created: 2026-09-26T17:00:00+00:00
---
- **測試基礎設施：coverage 可見度、平行化與選擇性執行（internal）**：以 subprocess 呼叫的工具現在對 coverage 可見（裸 `coverage report/xml` 前須先 `coverage combine`）；`make test`／`coverage` 的 xdist worker 數改走 `PYTEST_WORKERS`（預設 `auto`），`make dc-test` 也改為平行。新增 diff-scoped 測試選擇器 `make verify-diff`（映射不到即 fail-closed 全跑）。`--dry-run` 零寫入閘門擴及 `dx/` 與 `lint/`，`da-tools init` 產出的 kustomize 樹改由真的 `kustomize build` 驗收；`testing-playbook.md` 補上 mutation harness 作法。見 [#1746](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1746)。
