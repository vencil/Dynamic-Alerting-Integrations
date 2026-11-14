---
section: Changed
topic: tools
issues: [3539]
created: 2026-11-14T07:10:00+00:00
---
- **kubectl 沒有自己的結束碼時一律是結束碼 2，並說明可能已寫入（[#3539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3539)）**：⚠️ kubectl 被 signal 中止時，cutover-tenant 與 `make configmap-assemble` 先前是結束碼 1，現在是 2，訊息為「was killed by signal N」（不再說「did not run」；其他外部引擎被 signal 中止時的訊息同樣改成這一句）。grafana-import `--dashboard-dir` 匯入到一半、某個 dashboard 的 `kubectl create`／`apply`／`label` 起不來／逾時／被中止時，先前已套用的各列照印（`--json` 亦同）、其後的 dashboard 不再嘗試；單一 `--dashboard` 時這個失敗也改成報表裡的一列。drift-detect `--mode operator`：沒有 `items` 的 `{"kind":"List"}` 先前當成空叢集（結束碼 0），現在是 2；`--kubeconfig`／`--context` 改放在 `get` 之前；kubectl 非零結束時印出它的 stderr（超過 4000 字元時印最後 4000 字元並註明）。上述入口的 kubectl 輸出改寫到暫存檔再讀，因此需要可寫的暫存目錄，沒有時結束碼 2（「cannot capture the output of kubectl」）。
