---
section: Fixed
topic: lint-guards
issues: [2645]
created: 2026-10-03T14:36:18+08:00
---
- **禁 BOM 擴到文件／資料檔，freshness lint 樣板讀不懂 front-matter 改報錯（lint / dx；[#2645](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2645)）**：`tests/shared/test_sast.py` 的 BOM 規則原本只掃 `.py`，現在也掃 `.md` / `.yaml` / `.yml` / `.json`（`.ps1` / `.bat` / `.cmd` 刻意不掃），並以字面量下限防掃描面被截斷——帶 BOM 的 front-matter 會讓以 `---` 開頭判斷的掃描器整檔跳過而不出聲。`scaffold_lint --kind freshness` 產出的 lint 遇到不合法的 front-matter YAML 不再靜默回空，而是點名該檔並 exit 2，與其他 kind 讀不了檔時的行為一致。
