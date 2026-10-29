---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-29T01:30:00+00:00
---
- **`check_image_pin_capability` 加上 render 路徑（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §12）**：每個 pin 了 da-tools 的 chart 以預設 values 與每個 `values*.yaml` 跑 `helm template`（帶與 `check_iac_helm` 共用的 `RENDER_BASE_SETS`），輸出經 `da-crdecode` 讀，與既有的「去掉 template action 再讀」路徑找到的 workload 取聯集：以 `include` 注入的 da-tools 容器現在也會被檢查。manifest 與 values 檔改經 `da-crdecode`。⚠️ values 檔與 manifest 裡的重複鍵不再報錯，照 kubectl 與 Helm 取後者（實測）；去 action 的 template 仍以嚴格讀法擋重複鍵。沒有 helm、渲染失敗或沒有 `da-crdecode` 時結束碼 2。
