---
section: Changed
topic: da-tools
issues: [2313]
created: 2026-09-28T12:36:00+00:00
---
- **da-tools 映像內的 Python 工具改為與 repo 原始碼逐字相同（[#2313](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2313)）**：`build.sh` 不再於建置時用 `sed` 刪掉工具裡 repo 佈局用的 parent-dir `sys.path.insert`。那一步只剝得到其中一種寫法、純屬美觀，卻讓映像裡跑的程式和測試驗過的程式不同。映像攤平後該項目指向 `/opt`（只有 `venv/` 與 `da-tools/`），無害；真正的風險——從該目錄遮蔽出貨模組——由 `tests/ops/test_image_flat_layout.py` 依 import 來源判定守住。工具行為不變。
