---
section: Changed
topic: docs
issues: [1541, 1640]
created: 2026-09-28T08:45:34+00:00
---
- **README 的「N 個 Python 工具」不再把 `_` 開頭的 helper 模組算進去（[#1541](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1541)）**：`scripts/tools/{ops,dx,lint}` 底下像 `_grar_*.py`、`_version_patterns.py` 這類共用 helper 原本被算成工具，現在改列在 tool-map 的「共用函式庫」區段，公告數字因此變小。計數也不再受平台影響（大寫副檔名、目錄名大小寫在 Linux 與 Windows 上算法一致）。工具計數只寫在帶 `scripts/tools/{ops,dx,lint}` 範圍字樣的句子裡（[#1640](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1640)）。
