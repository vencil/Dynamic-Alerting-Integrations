---
section: Fixed
topic: confd-family
issues: [2480]
created: 2026-09-30T12:12:03+00:00
---
- **conf.d 大小寫對等測試不再因主機上的 kubectl 而隨機轉紅（tests；[#2480](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2480)）**：`test_confd_case_parity_across_tools.py` 執行各工具時，改在 PATH 最前面放一支立即回報「沒有 cluster」的 kubectl stub，在 POSIX 主機上不再繼承主機的 kubectl（Windows 上 stub 不會生效，目前也沒有 CI 在 Windows 跑這支測試）。先前 `operator_check` 呼叫 kubectl 時，在有 kubectl、沒有 cluster 的 CI runner 上，三次執行可能有的立即失敗、有的逾時，rc 不一致而讓對等比對轉紅；本機沒有 kubectl 時則一律 skip，同一道閘門的答案取決於機器。新增回歸測試確認工具看到的是 stub 而不是主機的 kubectl。
