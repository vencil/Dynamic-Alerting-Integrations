---
name: vibe-paths-k8s-manifests
description: "Vibe 路徑觸發指引：動到 k8s/**、operator-manifests/** 之前先讀的章節與一句約束。"
paths:
  - "k8s/**"
  - "operator-manifests/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-k8s-manifests

先讀：
- `docs/internal/iac-lint-baseline.md` §Layer 4 — k8s raw manifest

約束：raw manifest 的 SAST L4 是 manual hook：`pre-commit run --hook-stage manual --all-files k8s-manifests-sast-check`；`k8s/03-monitoring/configmap-rules-*.yaml` 是 rule-packs 的產物，改來源再 `make rulepack-configmaps`。改精靈會帶的 image（`WIZARD_THIRD_PARTY`／`WIZARD_FIRST_PARTY`）的 tag，同 helm-chart 那三件事（platform-data → `images.js` 鏡像 → dist）；其他 image 不牽動 portal。
