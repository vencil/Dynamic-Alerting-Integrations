---
section: Changed
topic: tools
issues: [3996]
created: 2026-11-16T16:28:34+00:00
---
- **⚠️ BREAKING：validate-config 對同一件事只報一次；產生器的 `FAIL:` 摘要只計數（tools；[#3996](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3996)）**：共用鍵判決有四種事實與 da-guard 主閘門相同（子目錄 `_defaults.yaml` 的保留鍵、子目錄送不出去的鍵與退役鍵、根 `defaults:` 底下的 `_` 鍵），以前在 `routes` 列的明細、摘要與 `guard_errors` 列各報一次。現在 key-verdict 的每個判決與 finding 帶 `gate_kind`：Go 在讀主閘門那個集合時填入主閘門的 finding kind，不靠比對名稱；validate-config 的 `routes` 列略過這些，由 `guard_errors` 帶修法句報一次。`generate-routes --validate` 的 `FAIL: N error(s) found` 不再重列已印過的行，只印沒在前面出現的（例如 inhibit tripwire）；「nothing to render」那條也只計數。
