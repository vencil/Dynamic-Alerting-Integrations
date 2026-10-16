---
section: Fixed
topic: alertmanager-routing
issues: [2295]
created: 2026-09-28T23:45:00+00:00
---
- **route generator 因重複 key 整份拒讀的檔，da-guard 不再放行（alertmanager-routing；[#2295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2295)、[#2905](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2905)）**：產生器讀 conf.d 時，檔內任一 mapping 出現重複 key 就拒讀整份；yaml.v3 不把 alias key 與其 anchor 並列、同一 mapping 兩個 `<<` 算重複，da-guard 以前照 yaml.v3 讀法放行（有的位置甚至讓 domain policy 靜默失效）。現在 `parse-tree` 把這類檔判為讀不了（`duplicate_key`），da-guard 列為未判定證據 `generator_unreadable` 並回 exit 4；exporter 自己會丟掉的檔照舊 exit 3。
