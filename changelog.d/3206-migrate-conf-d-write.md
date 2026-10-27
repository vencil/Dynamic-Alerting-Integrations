---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-27T13:40:11+00:00
---
- **`migrate_conf_d.py` 搬檔改由 `da-guard write` 搬（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10）**：`--apply` 不再跑 `mkdir -p`／`git mv`，整個搬移計畫一次交給 `da-guard write`：逐位元組搬（權限、擁有者不變），整棵樹判一次，一個被拒就一個都不搬，拒絕逐檔點名、結束碼 2。conf.d 在 git 工作樹裡時，搬完把每個來源的 index 項目原樣移到目標，`git status` 顯示改名、沒 stage 的修改照舊不 stage；有來源 git 沒追蹤時，搬之前就結束碼 2；不在 git 工作樹裡就只搬檔。⚠️ 可見變化：`--dry-run` 改問同一批的 `--dry-run`，會被拒的計畫在預覽就結束碼 2；預覽改印「來源 -> 目標」，不再印 `git mv`。頂層是 symlink 的租戶檔現在被拒（原本 `git mv` 會搬 symlink 本身）。`--output-plan` 的 JSON 不變。
