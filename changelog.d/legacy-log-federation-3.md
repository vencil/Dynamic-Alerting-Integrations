---
section: Fixed
topic: log-federation
issues: [1288, 1293, 1294, 1295, 1302]
created: 2026-09-26T17:00:00+00:00
---
- **租戶日誌投影的來源判定與版本一致性修正（`helm/vector`、`helm/victorialogs`）**：Vector 的 origin spoof guard 比錯 `pod_owner` 的欄位形狀，讓所有合法 gateway audit 列都被判成 `suspicious_audit`，租戶日誌分區因此靜默清空，現已修正。平台用來判定來源與時間的欄位（`pod_owner`、`timestamp` 等）改在合併不可信 payload 之前讀取，被稽核方不能再自行偽造來源或把事件推出偵測視窗。CI、dev container 與 chart 的 Vector 版本統一為實際部署的 `0.57.0`，並新增 `VIBE_REQUIRE_VECTOR`／`VIBE_REQUIRE_HELM`，讓 CI 缺少 binary 時失敗而非靜默跳過 VRL 行為測試。`victorialogs` NetworkPolicy 的說明更正：同 namespace 能建 pod 的人可自貼 label 繞過，這不是邊界（行為不變）（[#1293](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1293)、[#1294](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1294)、[#1295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1295)）。
