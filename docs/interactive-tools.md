---
title: "互動式工具"
tags: [interactive, tools, react]
audience: [all]
version: v1.13.0
lang: zh
---

# 互動式工具

本平台提供四個互動式 React 元件，可在支援 React 的環境中執行（如 Claude Artifacts、CodeSandbox、或自建頁面）。

## 入門精靈 (Wizard)

**檔案：** `docs/getting-started/wizard.jsx`

根據使用者角色（Platform Engineer / Domain Expert / Tenant）引導至對應的入門文件，並動態顯示各角色的關鍵操作步驟。

**適用場景：** 新使用者第一次接觸平台時的角色導向入門。

## Tenant YAML Playground

**檔案：** `docs/playground.jsx`

互動式 Tenant YAML 編輯器，支援即時語法驗證（key 名稱、三態值、排程格式）並即時預覽產出的 Prometheus metrics。

**適用場景：** 撰寫或調試 Tenant YAML 配置時快速驗證。

## Rule Pack 選擇器

**檔案：** `docs/rule-pack-selector.jsx`

根據技術棧（MySQL / PostgreSQL / Redis / JVM / Nginx 等）推薦適用的 Rule Packs，顯示每個 Pack 的 alert 數量與涵蓋指標。

**適用場景：** 初次導入時選擇需要啟用哪些 Rule Packs。

## CLI 指令建構器

**檔案：** `docs/cli-playground.jsx`

選擇 da-tools 子命令 → 填入參數 → 自動產生完整 `docker run` 指令，一鍵複製。

**適用場景：** 不熟悉 Docker 指令格式時快速產生正確的執行命令。

---

## 使用方式

這些 `.jsx` 檔案可在以下環境直接執行：

1. **Claude Artifacts** — 將 `.jsx` 內容貼入對話，Claude 會即時渲染
2. **React 開發環境** — `npx create-react-app` 後將元件引入使用
3. **CodeSandbox / StackBlitz** — 線上即時預覽

每個元件均為獨立的 React functional component，無需額外 state management library。
