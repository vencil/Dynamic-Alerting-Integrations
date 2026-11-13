---
section: Changed
topic: dx
issues: [3849]
created: 2026-11-13T15:00:00+00:00
---
- **Renovate 改到 git-only chart 的映像時，同一支 PR 一併升 chart 版號（[#3849](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3849)）**：`renovate.json` 為 7 個沒有發布線推送的 chart 各加一條 `bumpVersions` 規則（patch），原本這類 Renovate PR 會被 chart-version-bump（#1269）擋下、要人手補版號。`tests/ops/test_renovate_config.py` 綁住規則清單與 git-only chart 集合相等、每條只升自己 `Chart.yaml` 的 `version` 鍵；推送的 chart（threshold-exporter、da-portal、tenant-api）版號仍由 release 收尾決定，不給規則。
