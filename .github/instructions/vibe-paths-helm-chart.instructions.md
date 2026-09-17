---
applyTo: "helm/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-helm-chart

先讀：
- `docs/internal/windows-mcp-playbook.md` §Helm Upgrade 防衝突
- `docs/internal/iac-lint-baseline.md` §Layer 2 — Helm template

約束：Helm SAST L2 只在 CI 硬擋，本地要手動跑 `pre-commit run --hook-stage manual --all-files iac-helm-sast-check`；values 裡的 secret-shape 是 L3，同一份 baseline 列管。新增 value→env 接線後跑 `helm template` 並 grep 那個 env：`{{- with X }}` 區塊內 `.` 被重綁成 X——裡面用 `.foo` 去拿 root 的 key 會靜默輸出空字串、X 的路徑拼錯則整段靜默不輸出，兩種 `helm template` 都 rc=0（寫 `.Values.foo` 反而會報錯）；要 root 用 `$.Values.foo`。
