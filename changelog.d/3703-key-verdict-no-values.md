---
section: Changed
topic: exporter
issues: [3703]
created: 2026-11-13T16:20:00+00:00
---
- **⚠️ BREAKING：共用判決的理由句不再帶設定值（exporter、da-guard、route generator；[#3703](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3703)）**：`value_unread` 的 `reason` 改成只由固定的字組成，不含作者寫的值、欄位名與解析器錯誤（先前 `_profile`、`_silent_mode`、`_metadata`、`_state_*`、`_severity_dedup` 的值會原樣出現在 `generate-routes --validate` 的 stderr 與 `--findings-json`）。句子換了：`references unknown profile "x"` → `references a profile that is not defined under the root profiles:`；`unknown field(s) a, b (…)` → `2 unknown field(s) (…)`；`unknown silent mode "x" (…)` → `unknown silent mode (…)`；`expires "x" is not RFC3339 (…): <解析錯誤>` → `expires is not RFC3339 (…)`。tenant-api 寫入關卡的訊息仍點名值，括號內的理由同步換句；`_profile` 那句改為 `_profile "x" references a profile …`。產生器自己的訊息另見同 issue 的片段。比對這些字串的腳本要更新。
