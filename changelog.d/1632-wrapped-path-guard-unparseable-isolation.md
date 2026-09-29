---
section: Changed
topic: lint-guards
issues: [1632]
created: 2026-09-28T22:52:15+08:00
---
- **折行路徑守衛遇到不能 parse 的 `.py` 不再整輪中止（lint；[#1632](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1632)）**：`tests/ops/test_wrapped_path_references.py` 的隱式串接掃描原本碰到第一支語法錯誤的 tracked `.py` 就拋出 `SyntaxError`，排在它後面的檔案都沒掃到，真正被拆開的路徑引用因此不會出現在輸出裡。現在每支檔案各自處理：不能 parse 的檔改列在獨立的 `UNPARSEABLE` 段落，訊息寫的是「這個檔不能 parse」，其他檔照常掃完，最後一起回報。這一類仍然判紅，因為 repo 裡沒有其他 Python 語法閘門。刻意寫壞的 fixture 請在測試執行時產生（例如放在 `tmp_path`），不要進版控。
