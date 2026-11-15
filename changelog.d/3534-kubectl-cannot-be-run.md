---
section: Fixed
topic: tools
issues: [3534]
created: 2026-11-03T15:32:05+00:00
---
- **PATH 上的 kubectl 無法啟動時改報具名錯誤（`_lib_kubectl`；[#3534](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3534)）**：kubectl 找得到但起不來（沒有執行權限、`Exec format error`、`#!` 指向的直譯器不存在）時，經 `_lib_kubectl` 讀叢集的路徑（diagnose、batch-diagnose、operator-check、grafana-import `--verify`）以前是 traceback 加 `unexpected internal error`（直譯器不存在時則誤報「not on PATH」），現在 stderr 印「cannot run kubectl: …」並指出是哪個檔案。結束碼不變（2）。
