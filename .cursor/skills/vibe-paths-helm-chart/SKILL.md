---
name: vibe-paths-helm-chart
description: "Vibe 路徑觸發指引：動到 helm/** 之前先讀的章節與一句約束。"
paths:
  - "helm/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-helm-chart

先讀：
- `docs/internal/windows-mcp-playbook.md` §Helm Upgrade 防衝突
- `docs/internal/iac-lint-baseline.md` §Layer 2 — Helm template

約束：Helm SAST L2 只在 CI 硬擋，本地要手動跑 `pre-commit run --hook-stage manual --all-files iac-helm-sast-check`；values 裡的 secret-shape 是 L3，同一份 baseline 列管。
