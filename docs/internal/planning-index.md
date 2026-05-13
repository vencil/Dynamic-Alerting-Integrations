---
title: "Planning Index — Discovery-based Backlog View"
tags: [internal, dx, planning, derived-view]
audience: [contributors, ai-agents]
version: v2.8.1
lang: zh
---

# Planning Index — Discovery-based Backlog View

> **本文件為 derived view，由 `scripts/dx/generate_planning_index.py` 自動產生。**
>
> 修改 status / pr_ref 等請改 source file（各 backlog `.md` 的 frontmatter / 嵌入式 yaml block、`flaky-tests.yaml`、code 內 `// TECH-DEBT(id=...)` 註解）。改完跑 `make planning-index` 重新渲染；pre-commit drift gate `planning-index-check` 會擋 stale 表。
>
> Source 與 namespace 政策見 [ADR-020](../adr/020-planning-ssot.md)；legacy ID → TRK 對映見 [`planning-id-mapping.md`](planning-id-mapping.md)。
>
> 本檔由 `scripts/dx/generate_planning_index.py` 寫入；不要手動編輯哨點之間的內容。

## 索引

<!-- PLANNING_INDEX_START -->
### proposed (1)

| ID | Kind | Title | Domain | PR | Source |
|----|------|-------|--------|------|--------|
| `ADR-021` | adr | ADR-021: Tenant Federation — Label-Injection Proxy over Self-Built Endpoint | tenant-api | — | [docs/adr/021-tenant-federation.md](../adr/021-tenant-federation.md) |


<!-- PLANNING_INDEX_END -->

## 資料來源

| Source | 偵測方式 | 預期 entry 形態 |
|--------|----------|----------------|
| `docs/**/*.md` top-of-file frontmatter | `---` 區塊含 `tracking_kind:` 欄 | 整個檔案視為一個 planning item（典型用法：每個 ADR、每份獨立 spec） |
| `docs/**/*.md` 嵌入式 YAML | H2/H3 heading 後緊接 \`\`\`yaml ... \`\`\` 區塊含 `tracking_kind:` | 一份檔案多個 entry（典型用法：`dx-tooling-backlog.md` / `frontend-quality-backlog.md` 等批次清單）|
| `flaky-tests.yaml` 頂層 list | 每個 dict 含 `tracking_kind:` | flaky test 升級為正式追蹤項目時用 |
| Code-comment 註解 | `// TECH-DEBT(id=TRK-042, status=in-progress, tracking_kind=tech-debt)` 或 `# TECH-DEBT(...)`，逗號分隔 key=value | 直接埋在程式碼內的 inline tech-debt |

## 為什麼是 derived view（不是 SSOT）

SSOT 永遠在 source（各 backlog / yaml / code），index 是 grep + render 的快照。修改某 entry 的 `status:` 必須改 source；index 只是 review-friendly 的快查表。pre-commit hook 把這個 invariant 機械化擋住：source 改了沒重新 render 就 fail。

## 關聯

- [ADR-020 §Layer 2 — Discovery-based Index Generator](../adr/020-planning-ssot.md#三層設計) — 本工具的 design rationale
- [`planning-id-mapping.md`](planning-id-mapping.md) — legacy ID → TRK 對映表
- [`dev-rules.md` §P1](dev-rules.md) — commit trailer 規範
- [issue #379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) — Planning SSOT system implementation epic（本工具為 chunk 2a）
