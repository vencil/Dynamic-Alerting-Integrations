---
section: Fixed
topic: da-tools
issues: [1818]
created: 2026-09-28T01:20:00+00:00
---
- **migrate 產物的告警說明不再渲染成空字串，validate 的批次比對也對得上 rate 類規則（da-tools；[#1818](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1818)）**：遷移後的告警讀依租戶聚合的 recording rule，`$labels` 只剩 `tenant`，原 annotation 裡的 `{{ $labels.instance }}` 之類照抄會變成空字串。現在原式子以 `=` 釘成單一值的 label 直接代入該值；其他改讀 `$labels.tenant`，annotation 附上原 label 名，並在規則檔與報告點名。含雙引號的 annotation（例如 `printf "%.2f"`）與模板化的 label 值先前會讓 `platform-alert-rules.yaml` parse 失敗，現在正確跳脫。`prefix-mapping.yaml` 新增 `old_query`／`new_query`：新值是實際的 recording rule，舊值是原規則左半邊以同一種方式依租戶聚合（保留 `rate()`），`validate --mapping` 改用這兩欄，改用黃金標準的項沒有 recording rule，改為跳過；舊版 migrate 產的檔會警告並退回原本的比對。v2.9.0 映像還沒有這些修正。
