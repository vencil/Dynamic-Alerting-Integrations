---
section: Changed
topic: exporter
issues: [3703]
created: 2026-11-14T12:10:00+00:00
---
- **⚠️ BREAKING：parse-tree 讀不了檔時的 `message` 不再帶檔裡寫的字（exporter、da-guard、tenant-api、route generator；[#3703](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3703)）**：`problem.message` 改成只由固定句子與行號組成，不再引用鍵名、tag、anchor 名、值，也不轉述 yaml.v3 的錯誤句（先前會原樣出現在 exporter 的 WARN、`generate-routes` 的 stderr 與 `--findings-json`）。例：`the key "x" is written twice …` → `a key is written twice in one mapping (first at line N)`；`the tag !x is not read on a scalar …` → `a scalar's tag is not read (…)`；`yaml: line 2: did not find expected …` → `line 2: yaml.v3 cannot parse the text into a document`。`kind` 與 JSON 形狀不變；訊息長度不再隨檔案內容變長，原本 512 位元組的截斷一併移除。比對這些字串的腳本要更新。
