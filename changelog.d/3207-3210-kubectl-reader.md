---
section: Changed
topic: da-tools
issues: [3207, 3210]
created: 2026-10-24T07:00:00+00:00
---
- **`operator-check` 與 `gitops-check sidecar` 改用同一支叢集讀取：kubectl 沒有回答不再當成「沒部署」（[#3207](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3207)、[#3210](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3210)）**：新增 `_lib_kubectl` 的 `kubectl_list`／`kubectl_get`（`kubectl get … -o json`；以名稱讀的 `kubectl_get` 只需 `get` 權限），兩支工具的每一次叢集讀取都經過它；kubectl 結束碼非 0（叢集連不上、權限不足、資源類型不存在）、不在 PATH、逾時，或輸出不是物件清單，一律是讀取失敗（`kubectl_error`），空清單才代表「有回答、沒有」。實測 kubectl 1.37.1 對真的 kube-apiserver：權限不足時結束碼 1、stdout 仍印出空清單，所以先看結束碼、不單看輸出。`operator-check` 的 CRD 有沒有裝改為以名稱 `get`（不存在是有回答的結論）；`gitops-check sidecar` 以名稱讀 Secret 與 Deployment，不再跑現行 kubectl 已拿掉的 `kubectl version --short`。文件的「檢查項目」改成與實作一致，拿掉不存在的「自動探索」namespace。⚠️ 行為變更：`operator-check` 叢集連不上或權限不足 `0`（`--ci` `1`）→ `2`，AlertmanagerConfig 讀取失敗 `0` → `2`；`gitops-check sidecar` 齊全 `1` → `0`、缺件由 warn 改 fail（`--ci` `0` → `1`）、叢集連不上／權限不足／沒有 kubectl `1`（`--ci` `0`）→ `2`。
