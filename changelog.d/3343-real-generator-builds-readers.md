---
section: Fixed
topic: ci
issues: [3343, 2977]
created: 2026-11-01T00:23:28+00:00
---
- **nightly race 的兩個紅燈：tenant-api 的 real-generator 測試自建全部 reader，parsetree 的位元組邊界測試在 `-short` 下縮小（[#3343](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3343)、[#2977](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2977)）**：`TestRealGenerator_*` 自 10-29 起在 nightly 每輪必紅，原因是產生器要讀出貨規則的 da-crdecode 與 da-read 只在 `ci.yml` 建，nightly 沒有，產生器 exit 2 被報成 `run_failed`。測試改為比照 da-guard，三支 binary 都是「env 有設用 env，否則經 `scripts/ops/go_build.sh` 自建」，清單由 Python 測試釘住 `_lib_consumer_read.READER_BINARY_ENVS`；`ci.yml` 的專用建置步驟移除。`TestPlainStringByteBoundary` 在 `-race` 下單次約 158 秒，nightly `-count=10` 讓 `pkg/parsetree` 六夜撞上 30 分鐘逾時；`-short` 時只跑一種 payload（約 44 秒），PR CI 照跑三種。
