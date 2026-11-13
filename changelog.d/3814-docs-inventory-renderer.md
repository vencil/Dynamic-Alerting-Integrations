---
section: Changed
topic: lint
issues: [3814]
created: 2026-11-13T12:30:00+00:00
---
- **文件 yaml 區塊清冊改用站台渲染器找出程式碼區塊（lint；[#3814](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3814)）**：`scripts/tools/_lib_doc_yaml_inventory.py` 用 mkdocs.yml 的 `markdown_extensions` 渲染每份 `docs/**/*.md`，網站渲染成程式碼區塊、語言經 pygments 對到 YAML lexer 的就是要判的區塊，屬性也取自渲染器的解讀；手寫的 fence／blockquote 樣式比對全部刪除，Go 測試的第二份抽取器一併刪除，只讀清冊的 JSON。⚠️ 破壞性：blockquote 裡的 yaml 區塊、`~~~yaml`、大寫、```` ``` {.yaml} ```` 不再報錯，而是照常被判（清單項目或 admonition 內縮排 4 格以上的 blockquote 先前沒有任何檢查讀到）；`yaml+jinja` 等其他 lexer 不算 yaml；網站不渲染成程式碼區塊的寫法不再報錯，因為它不是區塊。`pymdownx.snippets` 刻意不展開。對不回原始檔行號的區塊一律報錯。hook 與 CI 改裝 markdown、pymdown-extensions、mkdocs（版本取自 `requirements/ci-constraints.txt`）。
