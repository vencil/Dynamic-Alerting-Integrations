---
section: Changed
topic: exporter
issues: [2153]
created: 2026-09-28T02:21:00+00:00
---
- **一份 tenant 檔宣告多個 tenant 時，載入與 reload 不再每個 tenant 各解析整份檔一次（exporter；[#2153](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2153)）**：階層式 conf.d 計算 `merged_hash` 時，原本對檔內每個 tenant 都重新讀取、解析整份檔，一份檔宣告的 tenant 越多，冷載入與 reload 的耗時就成平方成長；只改一個值的 reload 也要付同樣的代價。現在一次冷載入、一次 reload 內，每份檔只讀取與解析一次，檔內所有 tenant 共用那份結果（每個 tenant 各自取得自己區塊的複本，互不影響），解析結果不跨 reload 保留。`da-guard` 的範圍檢查同樣受益。`merged_hash`、合併結果、錯誤訊息、log 與 `da_config_parse_failure_total` 計數都與先前相同。
