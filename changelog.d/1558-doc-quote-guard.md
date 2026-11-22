---
section: Added
topic: lint
issues: [1558]
created: 2026-11-22T11:51:29+00:00
---
- **文件裡宣告為節錄的區塊必須逐行對得上來源檔（lint；[#1558](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1558)）**：新 hook `doc-quote-check`（`scripts/tools/lint/check_doc_quotes.py`，CI Lint job 每個 PR 都跑）檢查 ```` ```<lang> {.quote src="<repo 路徑>" anchor="…"} ```` 區塊：每段都要等於來源檔的連續整行（只正規化共同縮排與行尾空白），中間省略要以整行 `# ...` 標出位置，`anchor` 必須恰好出現一次，`foo.md` 與 `foo.en.md` 的 `.quote` 數與 `src` 必須相同。opt-in：沒宣告的區塊不看。`.quote` 加入 fence 屬性的封閉詞彙，不能與 `.confd` 等消費者屬性並用，列在 md-yaml-fence-check 的豁免報表。寫法見 `docs/internal/doc-template.md` §3.2。
