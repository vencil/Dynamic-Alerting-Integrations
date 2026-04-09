---
title: Lessons Learned Archive
audience: [platform-engineers, sres, contributor]
---

# Lessons Learned Archive

> **Purpose**: 歸檔 Playbook 中已不再適用或已固化為正式 SOP 的 lesson-learned 條目。保留歷史脈絡，避免主 Playbook 膨脹。

## 歸檔原則

依 `CLAUDE.md` §Playbook 維護原則 #2 知識退火 — lesson-learned 跨越兩個 minor 版本時強制三選一：

1. **固化為正式規範** — 加入 dev-rules.md 或 Playbook 主文
2. **標記 🛡️ 已自動化** — workaround 已由 pre-commit / CI hook 取代
3. **歸檔至本文** — 原陷阱已不可能再現（工具鏈升級、API 變更、環境遷移）

## 已歸檔條目

### GitHub Release Playbook

#### `v*` tag 觸發 exporter build

**狀態**: 🗄️ 已歸檔（v* 不再觸發 CI）

**歷史脈絡**: v2.4.x 時期，`v*` tag 會同時觸發 exporter CI build，導致 Multi-repo 場景下誤觸發 exporter pipeline。自 v2.5.0 起 `release.yaml` 已拆分 tag pattern（`v*` → platform、`exporter/v*` → exporter），此陷阱不再可能再現。

#### `mkdocs gh-deploy` 連續失敗

**狀態**: 🗄️ 已歸檔（workaround 已轉移至 Windows-MCP #29-30）

**歷史脈絡**: 早期 MkDocs gh-deploy 在 Windows 環境下因 PowerShell cp950 編碼導致連續失敗。Workaround 已固化為 Windows-MCP Playbook #29-30 的 PowerShell UTF-8 環境變數設定。

### Windows-MCP Playbook

#### GitHub Release `already_exists` 422

**狀態**: 🗄️ 已歸檔（PATCH 繞道已固化為 Re-tag SOP）

**歷史脈絡**: REST API `POST /releases` 在 tag 已存在時回傳 422 `already_exists`。Workaround：先 `GET /releases/tags/<tag>` 取 id，再 `PATCH /releases/<id>`。此 workaround 已固化為 github-release-playbook.md Re-tag 完整 SOP，不再需要作為 lesson-learned 追蹤。

## 維護原則

- **不刪除**：歸檔是保留，不是刪除。原條目仍可被搜尋與追溯。
- **雙向連結**：主 Playbook 的歸檔條目保留 `🗄️ 已歸檔` 標記與本文連結。
- **新增歸檔**：每個 minor release 後檢視 Playbook，依三選一原則決定去留。
