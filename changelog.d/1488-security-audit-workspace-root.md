---
section: Fixed
topic: dx
issues: [1488]
created: 2026-11-07T10:49:26+08:00
---
- **vibe-security-audit 的 workflow 腳本要求 `workspaceRoot`，跨元件路徑一律對快照解析（dx；[#1488](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1488)）**：攻擊類別以 `components/...`、`scripts/...` 點名的檔案先前沒有解析基準，agent 可能對自己的 cwd（主樹）解析，而稽核輸出看不出讀錯了樹。現在 `args.workspaceRoot` 必填、須為絕對路徑且涵蓋 `target`，Recon／Hunt／Validate 的 prompt 都帶同一句解析規則；缺漏、相對路徑、檔案系統根、target 不在其下皆拒跑。SKILL.md 的呼叫範例同步。
