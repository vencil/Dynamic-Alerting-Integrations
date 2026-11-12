---
section: Changed
topic: exporter
issues: [3638]
created: 2026-11-12T00:10:48+00:00
---
- **⚠️ Breaking：`_silent_mode`、`_state_<filter>` 與 `_metadata` 依寫下的結構判斷形式，純量不再被當成映射（exporter、da-guard、tenant-api；[#3638](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3638)）**：讀取端以前用字串比對猜形式，所以純量 `_silent_mode: "target: critical"` 會像映射一樣靜音。現在只有寫成映射的值才照結構讀；純量照它寫下的樣子讀。`_silent_mode`：純量或頂層沒有 `target` 鍵的映射／序列是不認得的值（WARN、不靜音、寫入關卡拒收），也不再與同內容的映射算作「沒改」。`_state_<filter>`：純量 `"target: enable"` 不會打開維護（fail-closed）；文字不是 YAML 的純量照純量讀，filter 未宣告時不再另報 WARN（共用判決以 `state_filter_undeclared` 報出）。`_metadata`：寫成字串（#2859）不再照讀，每個欄位為空，由每次 commit 的 WARN 與 `da_config_metadata_string_form` 點名；疊層時整個取代下層；tenant-api 把這些租戶當成標籤未知，有範圍限制的讀者看不到、只有平台管理者能寫，profile 或根平台檔寫成字串時，繼承它的租戶都受影響。其他保留鍵稍後逐一改為依結構讀。
