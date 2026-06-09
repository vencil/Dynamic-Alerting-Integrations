---
title: "互動式工具"
tags: [interactive, tools, react]
audience: [all]
version: v2.9.0
lang: zh
---

# 互動式工具

> **Language / 語言：** **中文 (Current)** | [English](./interactive-tools.en.md)

> **受眾**：全角色——下表「給誰」欄標出每個工具的主要使用者。

本平台提供**五個**互動式工具，幫不同角色快速上手。**想直接試** → [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/)，或本機 `make portal-run`（見[使用方式](#使用方式)）。

## 工具一覽

| 工具 | 給誰 | 做什麼 | 何時用 |
|------|------|--------|--------|
| **入門精靈 (Wizard)** | 全角色新手 | 依角色（Platform Engineer / Domain Expert / Tenant）引導到對應入門文件，並顯示各角色關鍵操作步驟 | 第一次接觸平台、角色導向入門 |
| **Tenant YAML Playground** | 租戶 / Domain Expert | 即時驗證 Tenant YAML（key 名稱、三態值、排程格式）並即時預覽產出的 Prometheus metrics | 撰寫或調試 Tenant YAML 時快速驗證 |
| **Rule Pack 選擇器** | 平台 / 租戶（導入期） | 依技術棧（MySQL / PostgreSQL / Redis / JVM / Nginx 等）推薦適用 Rule Packs，顯示每個 Pack 的 alert 數與涵蓋指標 | 初次導入時選擇啟用哪些 Rule Packs |
| **CLI 指令建構器** | DevOps / 平台 | 選 da-tools 子命令 → 填參數 → 自動產生完整 `docker run` 指令，一鍵複製 | 不熟悉 Docker 指令格式時 |
| **ROI Calculator** | 決策者 | 輸入組織規模（租戶數、Rule Pack 數、On-call 人數）與現有運維成本，即時算三項效益：Rule 維護 O(N×M)→O(M) 降幅、告警風暴壓制率、Onboard 自動化加速（可匯入 `alert_quality.py --json` 實際數據修正預估）| 平台評估階段，向決策者展示量化 TCO 節省 |

> **原始碼**：五個元件在 `tools/portal/src/`（入門精靈在 `getting-started/`，其餘在 `interactive/tools/`），均為獨立 React functional component，無需額外 state management library。

## 使用方式

這些 `.jsx` 元件可在以下環境直接執行：

1. **GitHub Pages（公開環境推薦）** — 開啟 repo Settings → Pages → Source 選 `main` / `/docs`，即可透過 `docs/interactive/index.html` 入口頁直接在瀏覽器試用。元件透過 `docs/assets/jsx-loader.html` 在瀏覽器端以 Babel standalone 即時轉譯，無需 build step
2. **da-portal Docker Image（企業內網 / air-gapped 推薦）** — `docker run -p 8080:80 ghcr.io/vencil/da-portal` 即可在內網架設完整的互動工具 Portal。支援 volume mount 客製化 `platform-data.json` 和 `flows.json`，nginx reverse proxy 可解決 Prometheus CORS 問題。詳見 [components/da-portal/](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)
3. **Claude Artifacts** — 將 `.jsx` 內容貼入對話，Claude 會即時渲染
4. **React 開發環境** — `npx create-react-app` 後將元件引入使用
5. **CodeSandbox / StackBlitz** — 線上即時預覽

### 本機預覽

```bash
# 方式 A：Python http.server（快速驗證）
cd docs && python3 -m http.server 8888
# 開啟 http://localhost:8888/interactive/

# 方式 B：da-portal Docker（與部署環境一致）
make portal-image && make portal-run
# 開啟 http://localhost:8080
```
