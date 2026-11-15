---
section: Changed
topic: confd-family
issues: [3901]
created: 2026-11-15T16:30:00+08:00
---
- **⚠️ Breaking：`lint` 選到的檔讀不了或消失一律結束碼 2；`lint` 的掃描目標、`onboard --rule-files`、`silencer-drift-check --rule-source` 改為宣告（tools；[#3901](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3901)）**：這三個「檔案或目錄」輸入各判定一次，拒收是結束碼 2、一行點名路徑（旗標有名字時連同旗標）。`lint`：掃描目標或它底下選到的檔在判定後讀不了或消失，以前是該檔的一筆 ERROR（不帶 `--ci` 結束碼 0、帶了是 1），現在結束碼 2；不是 UTF-8 或不是合法 YAML 仍是 ERROR。`silencer-drift-check --rule-source ''` 以前被當成目前目錄（結束碼 0）、檔案加尾斜線以前照讀，現在都拒收；直接給的懸空連結不論 `--exclude` 一律拒收（`lint`、`onboard` 以前可被排除掉）；沒有 YAML 的目錄訊息改為 `--rule-source <path>: selects nothing (…)`。`onboard`、`silencer-drift-check`：選到的檔在讀取時消失，結束碼 2、點名該檔、不輸出報告（以前輸出少了該檔的報告）。選不到的項目（懸空連結、列不出的目錄）的訊息改成每個項目一行 `<旗標> <路徑>: cannot be chosen to read: <原因>`。
