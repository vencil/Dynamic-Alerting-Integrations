---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-26T05:15:19+00:00
---
- **`workflow_path_filter assert --out` 改經 `write_repo_artifact`（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：每個 leg 的 `--out` 會被 `merge` 整份載入，所以改為原子取代。`--out` 的目錄不存在時，原本是 `FileNotFoundError` 的 traceback 加 `unexpected internal error`（rc 2），現在會先建立目錄；路徑不能用時是一行並點名 `--out`（rc 2）。`--out -` 原本會在 cwd 寫出名為 `-` 的檔，現在 rc 2。寫出的內容不變。
