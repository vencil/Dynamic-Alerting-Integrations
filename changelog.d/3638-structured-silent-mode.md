---
section: Changed
topic: exporter
issues: [3638]
created: 2026-11-12T00:10:48+00:00
---
- **⚠️ Breaking：`_silent_mode` 與 `_state_<filter>` 依寫下的結構判斷形式，純量不再被當成映射（exporter、da-guard、tenant-api；[#3638](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3638)）**：以前讀取端以字串比對 `target:` 來猜形式，所以純量 `_silent_mode: "target: critical"` 會像映射 `{target: critical}` 一樣靜音。現在只有寫成映射、且帶 `target` 鍵的值才走結構化讀法；純量照它寫下的樣子讀，成為不認得的值（WARN、不靜音），寫入關卡也照此判定；頂層沒有 `target` 鍵的映射或序列（例如 `{reason: "target: x"}`、`[{target: all}]`）同樣是不認得的值，以前會讀成關閉或解析錯誤。兩者都不再與同內容的映射算作「沒改」。要時間盒靜音請寫成映射（`_silent_mode: {target: critical, expires: …}`）。`_state_<filter>`（含 `_state_maintenance`）同理：純量 `"target: enable"` 不會打開維護，而是不認得的值（fail-closed；平台宣告了該 filter 時印 WARN，未宣告時由共用判決報出）；文字不是 YAML 的純量（`"expires: ["`）也照純量讀，平台未宣告該 filter 時不再由到期讀取端另報一則 WARN（該鍵由共用判決以 `state_filter_undeclared` 報出）。其他保留鍵的讀法不變，後續逐一改為依結構讀。
