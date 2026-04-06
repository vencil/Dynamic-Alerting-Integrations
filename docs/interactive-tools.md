---
title: "互動式工具"
tags: [interactive, tools, react]
audience: [all]
version: v2.5.0
lang: zh
---

# 互動式工具

本平台提供四個互動式 React 元件，可在支援 React 的環境中執行（如 Claude Artifacts、CodeSandbox、或自建頁面）。

## 入門精靈 (Wizard)

**檔案：** `docs/getting-started/wizard.jsx`

根據使用者角色（Platform Engineer / Domain Expert / Tenant）引導至對應的入門文件，並動態顯示各角色的關鍵操作步驟。

**適用場景：** 新使用者第一次接觸平台時的角色導向入門。

## Tenant YAML Playground

**檔案：** `docs/interactive/tools/playground.jsx`

互動式 Tenant YAML 編輯器，支援即時語法驗證（key 名稱、三態值、排程格式）並即時預覽產出的 Prometheus metrics。

**適用場景：** 撰寫或調試 Tenant YAML 配置時快速驗證。

## Rule Pack 選擇器

**檔案：** `docs/interactive/tools/rule-pack-selector.jsx`

根據技術棧（MySQL / PostgreSQL / Redis / JVM / Nginx 等）推薦適用的 Rule Packs，顯示每個 Pack 的 alert 數量與涵蓋指標。

**適用場景：** 初次導入時選擇需要啟用哪些 Rule Packs。

## CLI 指令建構器

**檔案：** `docs/interactive/tools/cli-playground.jsx`

選擇 da-tools 子命令 → 填入參數 → 自動產生完整 `docker run` 指令，一鍵複製。

**適用場景：** 不熟悉 Docker 指令格式時快速產生正確的執行命令。

## ROI Calculator

**檔案：** `docs/interactive/tools/roi-calculator.jsx`

採用效益試算器 — 輸入組織規模（租戶數、Rule Pack 數、On-call 人員數）和現有運維成本（配置變更耗時、告警風暴頻率、手動 Onboard 時間），即時計算三項效益：Rule 維護時間 O(N×M) → O(M) 降幅、告警風暴自動壓制率、Onboard 自動化加速。支援匯入 `alert_quality.py --json` 實際數據修正預估。

**適用場景：** 平台評估階段，向決策者展示量化的 TCO 節省。

---

## 使用方式

這些 `.jsx` 檔案可在以下環境直接執行：

1. **GitHub Pages（公開環境推薦）** — 開啟 repo Settings → Pages → Source 選 `main` / `/docs`，即可透過 `docs/interactive/index.html` 入口頁直接在瀏覽器試用。元件透過 `docs/assets/jsx-loader.html` 在瀏覽器端以 Babel standalone 即時轉譯，無需 build step
2. **da-portal Docker Image（企業內網 / air-gapped 推薦）** — `docker run -p 8080:80 ghcr.io/vencil/da-portal` 即可在內網架設完整的互動工具 Portal。支援 volume mount 客製化 `platform-data.json` 和 `flows.json`，nginx reverse proxy 可解決 Prometheus CORS 問題。詳見 [components/da-portal/](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)
3. **Claude Artifacts** — 將 `.jsx` 內容貼入對話，Claude 會即時渲染
4. **React 開發環境** — `npx create-react-app` 後將元件引入使用
5. **CodeSandbox / StackBlitz** — 線上即時預覽

每個元件均為獨立的 React functional component，無需額外 state management library。

### 本機預覽

```bash
# 方式 A：Python http.server（快速驗證）
cd docs && python3 -m http.server 8888
# 開啟 http://localhost:8888/interactive/

# 方式 B：da-portal Docker（與部署環境一致）
make portal-image && make portal-run
# 開啟 http://localhost:8080
```
