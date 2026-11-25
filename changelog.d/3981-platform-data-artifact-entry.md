---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-25T04:10:02+00:00
---
- **`generate_platform_data.py`（`make platform-data`）改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`platform-data.json` 與 portal 的離線 fallback 都判斷過才開始寫，寫入改經 `write_repo_artifact`：原子取代，hard link 改為就地寫入並在 stderr 多一行 WARN；權限照 checkout（原本一律改成 `0644`，現在可執行檔保留 `0755`）。以 `>>` 把 stdout 接到任一檔時，原本是結束碼 `0`、該檔被報告弄壞，現在是 `2`，兩檔都不寫。以 `>`（`--release-charts`）接到任一檔時也是 `2`、兩檔都不寫，但 shell 在程式啟動前已把該檔截空，原內容仍會遺失。任一檔是 FIFO 時（會讀到它的模式）原本卡住，現在是 `2`；是目錄、父目錄是檔、非 UTF-8 時，原本是 `unexpected internal error`（寫入時常已先寫出 `platform-data.json`），現在是 `2` 加一行，兩檔都不寫；權限被拒時也是 `2` 加一行，但 fallback 被拒時 `platform-data.json` 已寫出（#4250）。`--check --release-charts` 遇到不是 JSON 物件的 `platform-data.json` 原本是 `unexpected internal error`，現在是結束碼 `1`（過時）；不帶 `--release-charts` 時它是 `charts` 的來源，維持結束碼 `2`。
