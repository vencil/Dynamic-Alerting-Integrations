---
section: Changed
topic: dx
issues: [1018]
created: 2026-11-06T01:20:00+00:00
---
- **本地 kind 叢集的 Kubernetes 版本改由單一檔案釘住（dev 環境；[#1018](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1018)）**：新增 `scripts/ops/kind-node-image.env`（`kindest/node` 以 digest 釘版），`scripts/setup.sh` 與 `test/rulepack-e2e/run.sh` 建叢集時改以 `--image` 帶入。原本不帶 image，叢集版本跟著 kind binary 的預設值走，升級 kind 時會悄悄改變。`test_toolchain_pin_parity.py` 守住：shell／Makefile／workflow／Python 裡建 kind 叢集的地方（含 `helm/kind-action`）都要帶這個 image，devcontainer 的 kubectl 與它相差不超過一個 minor。
