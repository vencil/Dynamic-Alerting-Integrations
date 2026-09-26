---
section: Fixed
topic: portal
issues: [944, 1200, 1215, 1226, 1321]
created: 2026-09-26T17:00:00+00:00
---
- **Portal 對租戶顯示的規則、計數與建議值改為對齊出貨內容（portal；[#1226](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1226)、[#1321](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1321)）**：多支工具的手抄資料已過期或本來就錯：「Rule Pack 詳情」列了數十筆名稱對不上出貨規則的告警與 recording rule（多為名稱漂移，少數為未出貨的能力，已移除）、Rule Pack 計數與告警總數停在舊版快照（離線 fallback 之外，入門精靈的告警總數與部分說明文字線上也錯）、「套用推薦值」會把 `pg_connections` 設成數學上永不觸發的值、Migration Simulator 對映出不存在的 threshold key，MariaDB `mysql_cpu` 仍被當成 CPU% 展示。現已全數校正；離線 Rule Pack 目錄與計數改由 `make platform-data` 產生或衍生，並加上 drift gate。宣告型 key 不再被 validator 誤報為「不在預設清單」，「產生範例」會以註解列出它們（不代填數值）。新增 lint 檢查 portal 規則宣稱與閾值單位／值域。
