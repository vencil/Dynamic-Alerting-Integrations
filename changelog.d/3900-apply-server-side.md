---
section: Fixed
topic: alertmanager-routing
issues: [3900]
created: 2026-11-15T10:33:54+00:00
---
- **⚠️ BREAKING：`generate-routes --apply` 改用 server-side apply，並拒絕接手其他工具管理的欄位（route generator；[#3900](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3900)）**：先前 `--apply` 把整份 Alertmanager 設定當成 `kubectl create configmap --from-literal` 的單一命令列參數，設定約 128 KiB（數百個租戶）就因作業系統的參數長度上限起不了 kubectl；接著的 client-side `kubectl apply` 又把整個物件存進 annotation，再大一點就撞 Kubernetes 的 annotation 上限。現在 ConfigMap 與 `--output-configmap` 共用同一個 builder，經 stdin 交給 `kubectl apply --server-side --field-manager=da-generate-routes`；`alertmanager.yml` 由 Argo CD、Flux、Helm 等其他工具管理時，結束碼 2、點名該工具、不套用。`--apply` 與 `--output-configmap` 在值超過 Kubernetes 的 ConfigMap 上限時都結束碼 1、不寫；`--output-configmap` 超過 annotation 上限時印 WARN，建議 `kubectl apply --server-side` 或 Argo CD 的 `ServerSideApply=true`。Operator 路徑教的 Secret 指令同步改用 `--server-side`。
