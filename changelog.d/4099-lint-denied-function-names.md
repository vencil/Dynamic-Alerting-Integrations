---
section: Changed
topic: tools
issues: [4099]
created: 2026-11-21T10:48:24+00:00
---
- **`lint` 的 `denied_functions` 只收規則叫得到的名字；內建政策改為直接讀 `.github/custom-rule-policy.yaml`（tools；[#4099](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4099)）**：⚠️ breaking。每個名字必須是 Prometheus 載入 rule file 的 parser 認得且已啟用的函式或聚合運算子（`da-read promql-names`），否則開始 lint 前結束碼 `2`、點名鍵與名字。先前打錯字的名字什麼也沒禁、也沒有提示。`holt_winters` 在 Prometheus 3 已不存在，從內建政策移除；政策檔還列著它、或列了未啟用的 experimental 函式（`double_exponential_smoothing` 等）時請刪掉該條。內建政策與 repo 的政策檔、文件範例原本是三份拷貝，現在是同一份檔（映像內附）。
