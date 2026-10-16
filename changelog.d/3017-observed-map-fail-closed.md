---
section: Changed
topic: threshold-governance
issues: [3017]
created: 2026-10-16T01:49:30+00:00
---
- **observed-map 缺檔或不完整不再靜默通過（[#3017](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3017)）**：`metric_observed_map.yaml` 缺檔、讀不了或沒有 `keys` 時，`threshold-recommend` 與 `threshold-govern` 改以結束碼 `2` 退出並點名檔案；先前缺檔或沒有 key 時會把每個 key 靜默略過、結束碼 `0`（YAML 壞掉時是 traceback）。`--generate-observed-map` 可重建缺檔或沒有 key 的 map，但不覆寫 YAML 壞掉的 map。drift-guard（`check_threshold_observed_map.py --ci`）遇到缺 map 回 `2`，alert 有比較、map 卻沒收錄的 key（coverage gap）由 WARN 改為失敗。⚠️ 行為變更：依賴「缺 map 時 rc 0」的流程會改為失敗。
