---
section: Fixed
topic: dx
issues: [2014]
created: 2026-09-27T04:57:58+00:00
---
- **`reword_chain.py` 拒絕沒有列到分支 tip 的 mapping（dx；[#2014](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2014)）**：mapping 的最後一項不是目標分支目前的 tip 時，改為 exit 2 並說明會被丟掉幾顆 commit，不寫任何 ref，`--dry-run` 亦同。先前會照樣改寫、回 0，最後一項之後的 commit 從分支上消失。`--branch ''`（只印新 chain、不更新 ref）與尚不存在的分支不受影響。`windows-mcp-playbook.md` 補上「列到 tip、其餘填 `-`」的用法。
