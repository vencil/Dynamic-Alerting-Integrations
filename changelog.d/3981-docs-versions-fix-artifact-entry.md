---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-24T23:13:30+00:00
---
- **`validate_docs_versions.py --fix` 改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：所有修補算完才開始寫，寫入改經 `write_repo_artifact`（原子取代）。以 `>>` 把 stdout 附加到某份要修的文件時，原本照修該檔、報告再接在後面（結束碼 `0`），現在是結束碼 `2` 加一行點名該檔，不修任何檔（開頭的 `Source of truth:` 那段仍會先寫進該檔；以 `>` 重導時 shell 先清空它，它不再是修補目標，行為不變）。目前使用者不可寫的檔原本是 `unexpected internal error`，現在是結束碼 `2` 加一行；但在它之前的檔可能已寫出，唯讀檔不在寫前預檢的範圍（#4250）。屬於別的使用者、但目前使用者可寫的檔，原本內容寫入後在 `chmod` 失敗（`unexpected internal error`），現在就地寫入、權限不變，結束碼 `0` 加兩行 WARN。修過的檔原本一律改成 `0644`，現在可執行檔保留 `0755`。「🔧 Fixed」訊息（文字不變）改在全部寫完之後印出。
