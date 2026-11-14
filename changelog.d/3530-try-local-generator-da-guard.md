---
section: Fixed
topic: dx
issues: [3530]
created: 2026-11-05T23:52:17+00:00
---
- **try-local：產生器容器帶上判定要跑的 da-guard、da-crdecode、da-read，`/v1/status` 回 `ready: true`（[#3530](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3530)）**：`try-local/generator/Dockerfile` 少了 da-guard，產生器服務對每筆寫入回 503 `da_guard_missing`、`ready` 永遠是 false，nightly smoke 因此連續失敗。映像新增與 recipe-preview 相同的 da-guard 建置階段（從 `components/threshold-exporter/app` 建、旗標走 `scripts/ops/go_build.sh`），build context 改為 repo root；da-crdecode 同一階段建置，da-read 從 `components/da-read` 建置，映像不再帶 amtool；compose、`component-docker-build`／`nightly-image-scan` 的矩陣、IaC lint 的 context 對照與 `make docker-build-all` 同步調整。
