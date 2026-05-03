# da-portal (v2.7.0)

<!-- 標題版號 = 最後 released tag；v2.8.0 in-flight feature 在內文以 **v2.8.0** inline 標記。
     Phase .e release wrap 切五線 tag 時，本標題 + 下方 helm --version 跟著批次同步 bump 為 v2.8.0。 -->

> **核心 component** — 把 Dynamic Alerting 的 43 個互動工具（Hub + Wizard + Tenant Manager + Self-Service Portal）封進一顆 ~12 MB nginx-alpine image，**zero build step**（瀏覽器端 Babel standalone 轉譯 JSX），給內網 / air-gapped 環境離線使用。
>
> **Companion 文件：** [helm chart](../../helm/da-portal/) · [Interactive Tools Hub 文件](../../docs/interactive-tools.md) · [tool-registry.yaml SOT](../../docs/assets/tool-registry.yaml) · [architecture-and-design](../../docs/architecture-and-design.md)

---

## 1. What & Why

- **Input** — `docs/interactive/tools/*.jsx`（43 個工具，由 `tool-registry.yaml` 為 SSOT 列管）+ `docs/getting-started/wizard.jsx` + `docs/assets/{platform-data.json, flows.json, design-tokens.css, tool-registry.yaml, vendor/}`，build 階段 `COPY` 進 image
- **Output** — 一顆 nginx-alpine image，跑起來給瀏覽器讀靜態檔案；JSX 由 `jsx-loader.html` 用 Babel standalone 在瀏覽器端轉譯
- **Why static + zero build** — 1) 內網 / air-gapped 場景不能跑 npm CI；2) 工具作者改 JSX 推 PR 即可，無 webpack / vite watch；3) image 純靜態 → 不需 secret，不需 runtime config
- **Why bundled vendor** — `make vendor-download` 把 React 18.3 / Tailwind / Babel 7.26 / Lucide 抓進 `docs/assets/vendor/`，build 時一起塞進 image；瀏覽器啟動先 probe local，找不到才 fallback CDN
- **不做的事** — 不執行 build pipeline；不持久化（無 state，所有資料來自 mount 進來的 JSON / Tenant API proxy）；不直連 Prometheus（CORS-free 查詢走 Tenant API 或自訂 nginx proxy）

> **Hub UX 細節** — 43 工具的分類、journey-phase、related-tool 圖、search 行為見 [docs/interactive-tools.md](../../docs/interactive-tools.md)。本 README 只負責 operator quick-reference。

---

## 2. What's New in v2.8.0

| # | 能力 | 影響 |
|---|------|------|
| 1 | **Tenant Manager API-First Mode + Saved Views frontend** — Tenant Manager UI 先打 `GET /api/v1/tenants/search`（live data，page_size 預設 50 / 上限 500，避過 500+ 租戶 DOM freeze），fallback 靜態 `platform-data.json`；新增 `SavedViewsPanel.jsx` 把 v2.5.0 已有的 Tenant API saved-views backend wire 起來（CRUD + demo-mode 404 graceful hide）。詳 [#148](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/148) / [#149](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/149) / [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) | 千租戶可用；首次把 v2.5.0 backend 真正暴露給操作員 |
| 2 | **Tenant Manager × Wizard deep links** — `TenantCard` footer 多兩個 anchor：🛠️ Alert (`?component=alert-builder&tenant_id=<name>`) + 🧭 Route (`?component=routing-trace&tenant_id=<name>`)；對應 `alert-builder.jsx` / `routing-trace.jsx` 讀 URL param seed `labels.tenant`。詳 [#94](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/94) | 把 Tenant Manager 從「viewer」變「launcher」，少 N 次 copy-paste tenant id |
| 3 | **`tenant-manager.jsx` 兩階段拆分（1691 → 817 LOC, -52%）** — Phase 1 抽出 `fixtures/` `styles.js` `utils/yaml-generators.js` 與 `GroupSidebar`；Phase 2 抽出 `useTenantData` `useModalFocusTrap` `ApiNotificationToast` `OverflowBanner`。確立 **window-global 自我註冊** pattern（`window.__X = X;` at file tail），因 jsx-loader 用 indirect eval 不會 leak `const`/`let` 到 global。詳 [#153](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/153) | 為其他超過 1000 LOC 的工具（operator-setup-wizard 1252 LOC 等）建立可複製的 decomposition pattern |
| 4 | **JSX 工具腳手架 `scaffold_jsx_dep.py` + line-count guard** — `scaffold_jsx_dep.py` 一行命令自動 wire 5 個 touch-point（front-matter `dependencies:` / 符號宣告 / `window.__X` 註冊 / orchestrator import / loader registry）；新 lint `check_jsx_line_count.py` soft cap 1500 / hard cap 2500 LOC，防止下個 monolith 偷偷長出來。詳 [#160](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/160) / [#152](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/152) | 把 PR-2d / PR-153 學到的 pattern codify 成工具 + lint，下個拆分不用憑記憶 |
| 5 | **Design-token 收斂 + drift 檢測** — `design-tokens.css` 補齊 4 個 token（`--da-color-link-on-dark` / `--da-color-accent-border-soft` / `--da-color-semantic-other` / `--da-color-hero-gradient`）；新 lint `check_undefined_tokens.py` 掃 JSX/JS/CSS 內所有 `var(--da-*)` 是否 defined，Tier B wizard.jsx hardcoded 色值收斂；同時抓出 `OverflowBanner.jsx:30` 用錯 token 的舊 bug。詳 [#142](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/142) / [#85](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/85) / [#86](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/86) | dark-mode / 主題切換不再有 hardcoded 色值漏洞 |
| 6 | **oauth2-proxy v7.7.1 / v7.15.1 → v7.15.2（CVE 收斂）** — `helm/da-portal/values{,-tier1,-tier2}.yaml` 三檔同步 bump；清掉 CVE-2026-34457（CRITICAL，health-check User-Agent matching 繞 `auth_request` mode）/ CVE-2026-40575（CRITICAL，`X-Forwarded-Uri` header spoofing）/ GHSA-pxq7-h93f-9jrg（HIGH，fragment confusion in `skip_auth_routes`）。Trivy 0.70.0：v7.15.2 在 Debian 13.4 + go binary 為 0 HIGH / 0 CRITICAL。詳 [#92](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/92) of umbrella [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100) | Tier-1 / Tier-2 兩種 deployment profile 都 benefit |

> **升級路徑** — 大多 v2.8.0 變更為 additive（新 lint / 新 hook / 新 sub-component）；唯一 breaking 風險是 **Tenant Manager 預設 hit `/api/v1/tenants/search`**：若 deploy 沒裝 Tenant API（單純丟 image + static JSON），會 fallback `platform-data.json` + 出現 demo-mode toast（非 fatal）。

---

## 3. Quick Start

### Docker（單機 / air-gapped）

```bash
# 1. 抓 vendor 進 docs/assets/vendor/（air-gapped 必跑；CI build 自動跑）
make vendor-download

# 2. Build image（從 repo root，因為 COPY 來源是 docs/）
make portal-image                                  # 預設 tag: ghcr.io/vencil/da-portal:latest
# 或：docker build -t ghcr.io/vencil/da-portal:v2.8.0 -f components/da-portal/Dockerfile .

# 3. Run
docker run -p 8080:80 ghcr.io/vencil/da-portal:v2.8.0
# 開瀏覽器：http://localhost:8080  →  Hub
```

### Helm（K8s，含 oauth2-proxy 與 Tenant API integration）

```bash
helm install da-portal \
  oci://ghcr.io/vencil/charts/da-portal --version 2.7.0 \
  -n monitoring --create-namespace \
  -f values-override.yaml
```

Helm chart 三個 profile：

| Profile | Values file | 適用場景 |
|---------|-------------|----------|
| Default | `values.yaml` | 純 portal + Tenant API proxy（無 IdP） |
| Tier-1 | `values-tier1.yaml` | Git-Native：portal + Tenant API + GitOps writeback |
| Tier-2 | `values-tier2.yaml` | Full Stack：Tier-1 + oauth2-proxy + Ingress + NetworkPolicy |

詳 [helm/da-portal/README.md](../../helm/da-portal/README.md)。

---

## 4. Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│ Browser                                                            │
│  ├─ /interactive/index.html       ── Hub（43 工具卡片 + search）  │
│  ├─ /interactive/tools/<x>.jsx    ── Babel standalone 端轉譯      │
│  └─ /assets/jsx-loader.html       ── React + vendor probe + theme │
└────────────────────────────────────────────────────────────────────┘
              │ static asset GET                  │ /api/v1/* (CORS-free)
              ▼                                   ▼
┌──────────────────────────┐         ┌────────────────────────────┐
│ nginx (this image)       │         │ tenant-api / oauth2-proxy  │
│  - serve docs/...        │ proxy → │  - GET /tenants/search     │
│  - CSP / HSTS headers    │         │  - PUT /tenants/{id}       │
│  - SPA fallback          │         │  - POST /tenants/simulate  │
│  - /healthz K8s probe    │         │  - X-Forwarded-{Email,User,│
└──────────────────────────┘         │    Groups} 注入            │
                                     └────────────────────────────┘
```

關鍵點：

- **Image 不含 build step** — `Dockerfile` 只 `COPY` + `apk del nginx-module-image-filter nginx-module-xslt`（移掉靜態 server 用不到的模組，cascade 拔 28 個 dep；image 從 72 → 44 packages，~23 MB 省）+ libavif 殘留檢查（CVE-2025-48174 mitigation）
- **Vendor probe** — `jsx-loader.html` 啟動跑同源 sync XHR 試 `vendor/react.production.min.js`；HTTP 200 走 local，否則退 CDN（Tailwind / React 18.3.1 / Babel 7.26.4 / Lucide 0.436.0）
- **Tool 之間共享狀態** — JSX 用 indirect eval 載入，`const`/`let` 不 leak global → 用 `window.__X = X;` 自我註冊 pattern（v2.8.0 由 `scaffold_jsx_dep.py` codify）
- **Tenant API proxy** — `nginx.conf` 的 `/api/v1/` 預設 upstream 為 `tenant-api.monitoring.svc.cluster.local:8080`，自動轉 oauth2-proxy 注入的 `X-Forwarded-Email` / `X-Forwarded-User` / `X-Forwarded-Groups`

---

## 5. Customization（不重 build，只 mount）

### 改 Tool 資料 / 引導流程

```bash
docker run -p 8080:80 \
  -v ./my-platform-data.json:/usr/share/nginx/html/assets/platform-data.json \
  -v ./my-flows.json:/usr/share/nginx/html/assets/flows.json \
  -v ./my-tool-registry.yaml:/usr/share/nginx/html/assets/tool-registry.yaml \
  ghcr.io/vencil/da-portal:v2.8.0
```

| Mount 檔 | 用途 | 來源 |
|----------|------|------|
| `platform-data.json` | Rule Pack catalog（DB / 中介軟體 / runtime defaults） | `make platform-data` 產 |
| `flows.json` | Guided Flows（onboarding 等順序步驟） | hand-edited |
| `tool-registry.yaml` | 43 工具的 SSOT（title / audience / journey_phase / related） | hand-edited，CI 跑 `check_jsx_loader_compat.py` 比對 |
| `design-tokens.css` | 100+ CSS 變數（色 / 間距 / 字 / 主題） | hand-edited，CI 跑 `check_undefined_tokens.py` |

### 改 nginx.conf（換 Tenant API upstream / 加 Prometheus proxy）

```bash
docker run -p 8080:80 \
  -v ./custom-nginx.conf:/etc/nginx/conf.d/default.conf \
  ghcr.io/vencil/da-portal:v2.8.0
```

預設 `nginx.conf` 把 `/api/v1/` proxy 給 `tenant-api.monitoring.svc.cluster.local:8080`。要改 upstream（指向你環境的 Tenant API）或加 Prometheus reverse proxy 給 alert preview 直查 PromQL，editable 範本見 [`nginx.conf`](nginx.conf)。

---

## 6. Image Details

| Property | Value |
|----------|-------|
| Base image | `nginx:1.28-alpine3.23` |
| Image size | ~12 MB（含 vendor），~9 MB（CDN-only） |
| Health check | `GET /healthz`（`HEALTHCHECK --interval=30s --timeout=3s`） |
| Listen port | 80 |
| User | `nginx`（non-root；K8s `securityContext.runAsNonRoot: true` 友好） |
| Build step | 無（純 `COPY`；瀏覽器端 Babel transpile） |
| Vendor mode | local（`docs/assets/vendor/` 存在）→ CDN fallback |

### Security headers（`nginx.conf` 內 baked）

| Header | Value |
|--------|-------|
| Content-Security-Policy | `default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; ...` |
| Strict-Transport-Security | `max-age=31536000; includeSubDomains` |
| X-Frame-Options | `DENY` |
| X-Content-Type-Options | `nosniff` |
| Referrer-Policy | `strict-origin-when-cross-origin` |
| Permissions-Policy | `camera=(), microphone=(), geolocation=()` |

> CDN fallback 需要 `script-src` 含 `cdnjs.cloudflare.com`；air-gapped 部署可移掉這條，只留 `'self'`。

---

## 7. Environment Variables

| Variable | Default | 說明 |
|----------|---------|------|
| `NGINX_PORT` | `80` | container 內 listen port |
| `NGINX_WORKER_PROCESSES` | `auto` | nginx worker 數 |

> Portal **不靠 env 設定行為**；所有可變項（Tenant API upstream、tool 列表、theme tokens）走 volume mount。

---

## 8. Develop（加 / 改一個工具）

| 步驟 | 命令 |
|------|------|
| 1. Scaffold 新工具的 dep 檔 | `python3 scripts/tools/dx/scaffold_jsx_dep.py --tool foo --kind hook --name useFoo` |
| 2. 編寫工具 JSX | `docs/interactive/tools/foo.jsx` |
| 3. 註冊到 SSOT | 編 `docs/assets/tool-registry.yaml` 加 `- key: foo` block |
| 4. 重新產 platform-data | `make platform-data` |
| 5. Lint（auto-stage） | `pre-commit run --all-files` |
| 6. 本機 smoke | `make portal-image && docker run -p 8080:80 ghcr.io/vencil/da-portal:latest` |
| 7. E2E（Playwright） | `make e2e-portal`（spec 在 `tests/e2e/portal-*.spec.ts`） |

**JSX 寫作三守則：**
1. **檔尾自我註冊** — 任何 dep 檔（hook / sub-component / util）結尾必有 `window.__X = X;`，orchestrator 端 `const X = window.__X;`（jsx-loader 用 indirect eval，不會 leak `const` 到 global）
2. **front-matter `dependencies:`** — orchestrator 列出所有 dep 路徑，loader 依序 fetch + eval
3. **Design tokens not hardcoded colors** — `var(--da-color-accent)` 而非 `bg-blue-500`；CI lint 會抓

完整守則見 [`docs/internal/dev-rules.md`](../../docs/internal/dev-rules.md)。

---

## 9. Troubleshooting

| 症狀 | 可能原因 | 解法 |
|------|---------|------|
| Hub 載入後白屏 | 某 JSX parse 失敗 + 沒 ErrorBoundary | 開 DevTools console；單檔修；v2.8.0+ 規劃加 ErrorBoundary（見 [PR roadmap](#)） |
| 404 on `/healthz` | 舊 image（< v2.3.0） | 重 build：`make portal-image` |
| CORS error on Tenant API | nginx proxy upstream 錯 | 改 `nginx.conf` 的 `proxy_pass`，或在 K8s 確認 `tenant-api` Service 存在於 `monitoring` namespace |
| Tenant Manager 顯示 demo-mode toast | `/api/v1/tenants/search` 回 404 / 5xx | 確認 Tenant API 跑著且 RBAC 有 read 權限；也可能是純靜態部署（無 backend），demo mode 為 expected |
| Tools 顯示舊資料 | 掛載的 `platform-data.json` 過舊 | `make platform-data` 重產 + 重 mount |
| Air-gapped 環境 vendor probe 失敗 | `make vendor-download` 沒跑 / `docs/assets/vendor/` 沒進 image | 確認 `docs/assets/vendor/react.production.min.js` 存在；重 build |
| `var(--da-*)` 顯示為 fallback 色 | 用了 undefined token | 跑 `python3 scripts/tools/lint/check_undefined_tokens.py`，補 `design-tokens.css` |

---

## 10. Related Documentation

- [Interactive Tools Hub 使用指南](../../docs/interactive-tools.md)
- [Tool Registry SOT](../../docs/assets/tool-registry.yaml) — 43 工具元資料
- [Design Tokens](../../docs/assets/design-tokens.css) — 主題系統 100+ CSS 變數
- [Helm Chart README](../../helm/da-portal/README.md) — 三個 profile 細節
- [架構深度](../../docs/architecture-and-design.md) — 9 個核心設計概念
- [Dev Rules](../../docs/internal/dev-rules.md) — JSX 寫作守則 + 12 條開發規範

---

## 11. 版號策略

| Tag 格式 | 產出 |
|----------|------|
| `portal/v*` | `ghcr.io/vencil/da-portal` Docker image + Helm chart |
| `exporter/v*` | threshold-exporter + Helm chart |
| `tools/v*` | da-tools CLI image |
| `tenant-api/v*` | tenant-api REST API |
| `v*` | Platform tag (GitHub Release，不觸發 build) |

---

> **回報問題** — Issue tracker：https://github.com/vencil/Dynamic-Alerting-Integrations/issues。若是 JSX 載入 / vendor probe / Tenant API proxy 問題，請附 (1) 瀏覽器 console log，(2) `curl -I http://<portal>/assets/vendor/react.production.min.js` 結果，(3) `kubectl logs -l app=da-portal -c oauth2-proxy --tail 50`（如果是 Tier-2 deploy）。
