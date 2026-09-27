---
section: Fixed
topic: dx
issues: [2023]
created: 2026-09-27T05:51:07+00:00
---
- **`check_doc_freshness --check` 不再把量不到年齡的文件算成新鮮（lint；[#2023](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2023)）**：git 讀不到時間戳（不是 git repo、失敗或逾時）的文件，以及 shallow clone 裡最後一次修改落在 shallow 邊界 commit 上的文件，改判為 unknown。`--check` 有陳舊文件時 exit 1；沒有陳舊、但有 unknown 時 exit 2，並在 stderr 列出是哪些文件與原因。shallow 下逐檔判斷：最後修改在已取得歷史內的文件照常計算年齡。還沒有任何 commit 的新文件算新鮮。`validate_all --fix` 不再對 freshness 呼叫工具不接受的 `--fix`。
