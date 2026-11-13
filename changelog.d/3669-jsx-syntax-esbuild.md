---
section: Changed
topic: dx
issues: [3669]
created: 2026-11-13T07:25:16+00:00
---
- **portal JSX 語法檢查改用 build 本身的 esbuild，行數檢查改成純 Python（lint；[#3669](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3669)）**：⚠️ `lint_jsx_babel.py`／`jsx-babel-check` 移除，改為 `lint_jsx_syntax.py`（`jsx-syntax-check`）與 `check_jsx_linecount.py`（`jsx-linecount-check`，`--strict-linecount` 改名 `--strict`）。語法檢查以 portal lockfile 釘版的 esbuild、經 `tools/portal/esbuild-shared.mjs`（build 共用）解析 `tools/portal/src` 與 `entries` 每個 `.jsx`／`.js`，不比 build 嚴也不比它鬆；需 `make portal-deps`，缺 node、esbuild 不在 portal 底下或版本與 lockfile 不同時結束碼 `2`。抓得到 Babel 漏掉的重複 `export default`。行數檢查不再因缺 node 回 `2`。`style={{}}` 靜態檢查與 `--strict-static` 移除：它防的瀏覽器端 Babel 已不存在。
