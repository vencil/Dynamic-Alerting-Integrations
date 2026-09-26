---
section: Fixed
topic: portal
issues: [1245, 1351, 1989]
created: 2026-09-26T17:00:00+00:00
---
- **精靈產出與自架 da-portal 映像可直接使用（portal、ops；[#1245](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1245)、[#1351](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1351)、[#1989](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1989)）**：部署精靈改為每個 chart 各產一份只含該 chart 會讀的 key 的 values，並更新先前全數過期的 image ref（其中 `oauth2-proxy` 缺 registry 前綴、會 ImagePullBackOff）；CI/CD 精靈的 workflow 預覽改為逐字等於 `da-tools init` 的產物；`platform-demo` 顯示的改為真實可執行的 da-tools 指令。自架 da-portal 映像補上漏帶的三份資產（Alert Preview、Config Template Gallery 的資料與 design token CSS），Self-Service Portal 載入即 crash 的問題修正。⚠️ would-fire 預覽在「當前值等於閾值」時改判為不觸發，與 Prometheus 的嚴格 `>` 一致；config-diff 改用共用的安全 YAML parser。
