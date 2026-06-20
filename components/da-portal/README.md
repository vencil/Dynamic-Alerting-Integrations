# da-portal (v2.9.0)

<!-- 標題版號 = 最後 released portal tag（目前 v2.8.0）。Release wrap 切六線 tag 時，
     本標題 + 下方 helm --version 跟著批次同步 bump。 -->

> 💡 **只想把元件跑起來看看？** → **[QUICKSTART.md](QUICKSTART.md)**（`docker run` 一行、≤ 2 分鐘看到一整套互動工具）。本篇 README 是給**部署 / 維運 portal 的人**看的進階配置與 packaging **參考**。

把 Dynamic Alerting 的 **44 個瀏覽器互動工具**（Hub + Wizard + Tenant Manager + Self-Service Portal）封進一顆 nginx-alpine image，供內網 / air-gapped 環境離線使用。工具在 CI 端用 **esbuild 預先 build 成 ESM bundle**，image 只負責靜態服務——不在 runtime 做任何 build。

**Companion 文件：** [helm chart](../../helm/da-portal/) · [Interactive Tools Hub](../../docs/interactive-tools.md) · [tool-registry.yaml（工具 SSOT）](../../docs/assets/tool-registry.yaml) · [architecture-and-design](../../docs/architecture-and-design.md)

---

## 這個 portal 服務誰？

da-portal 把告警/維運能力封成瀏覽器工具，給三種角色。**各角色的上手步驟都有專屬指南**，本 README 不重複（只負責部署 / packaging 參考）：

| 角色 | 在 portal 做什麼 | 上手指南 |
|------|------------------|----------|
| **Tenant（租戶）** | 自助調閾值、存 / 套用 Saved View、預覽自己會收到的告警 | [for-tenants](../../docs/getting-started/for-tenants.md) |
| **Platform Engineer / SRE** | 為租戶除錯、規劃容量、驗證四層路由、lint 設定 | [for-platform-engineers](../../docs/getting-started/for-platform-engineers.md) |
| **Domain Expert** | 把監控知識落成 Rule Pack、在 CI 守 cardinality 預算 | [for-domain-experts](../../docs/getting-started/for-domain-experts.md) |

> **誰會讀這份 README？** 是**部署 / 自架 portal** 的人（碰 Dockerfile、Helm、nginx.conf、`docker run`）。Tenant 等終端使用者用的是「已部署好的 portal UI」，請走上方角色指南。

---

## 1. What & Why

- **Source（輸入）** — `tools/portal/src/interactive/tools/*.jsx`（44 個工具；`tools/portal/manifest.json` 為 build entry SSOT、`docs/assets/tool-registry.yaml` 為工具 metadata SSOT）+ `tools/portal/src/getting-started/wizard.jsx`
- **Build** — `make portal-build` 跑 [esbuild](../../tools/portal/build.mjs)，把每個工具 bundle 成 ESM 檔輸出到 `docs/assets/dist/<tool>.js`，**committed 進 repo**（CI 有 drift gate 確保 source 與 dist 同步）。測試走 `make test-portal`（Vitest）
- **Image（輸出）** — 一顆 nginx-alpine，COPY 進 Hub（`docs/interactive/`）+ 預先 build 的 `docs/assets/dist/` + 共享 assets + vendor；瀏覽器直接載入 ESM bundle，**runtime 不做 build / transpile**
- **為什麼是「預先 build + 靜態服務」** — 1) 內網 / air-gapped 場景不能在 runtime 跑 npm；2) build 在 CI 一次完成、結果 committed，部署端零依賴；3) image 純靜態 → 不需 secret、不需 runtime config
- **vendor probe（離線優先）** — `make vendor-download` 把 React 18.3 / ReactDOM / Tailwind / Lucide 抓進 `docs/assets/vendor/`；瀏覽器啟動先 probe local，找不到才 fallback CDN（每個工具 bundle 透過頁面提供的這些 global 執行）
- **不做的事** — 不在 runtime build；不持久化（無 state，資料來自 mount 的 JSON 或 tenant-api proxy）；不直連 Prometheus（CORS-free 查詢走 tenant-api 或自訂 nginx proxy）

> **Hub UX 細節**（44 工具的分類、journey-phase、related-tool 圖、search）見 [docs/interactive-tools.md](../../docs/interactive-tools.md)。本 README 只負責 operator quick-reference。

---

## 2. What's New in v2.8.0

精簡重點（完整版本歷程見 [CHANGELOG.md](../../CHANGELOG.md)）：

- **Tenant Manager API-First** — Tenant Manager 預設打 `GET /api/v1/tenants/search`（live data，分頁避過千租戶 DOM freeze），無後端時 fallback 靜態 `platform-data.json` + demo-mode 提示
- **Tenant Manager → Wizard 深連結** — `TenantCard` 可一鍵帶著 tenant id 跳到 Alert Builder / Routing Trace，少掉反覆 copy-paste
- **oauth2-proxy CVE 收斂** — Helm 三個 profile 同步升到無已知 CRITICAL/HIGH 的版本

> **v2.8.0 之後（未 bump 版號）** — 工具的核心純邏輯（calculator / validator / parser / 路由演算法）已抽成可單元測試的模組並補上測試（behavior-preserving）；對部署 / 使用零影響。

---

## 3. Quick Start

### Docker（單機 / air-gapped）

```bash
# 1. 抓 vendor 進 docs/assets/vendor/（air-gapped 必跑；CI build 自動跑）
make vendor-download

# 2. Build 工具 bundle（esbuild → docs/assets/dist/）
make portal-build

# 3. Build image（從 repo root，因為 COPY 來源是 docs/ 與 tools/）
make portal-image                                  # 預設 tag: ghcr.io/vencil/da-portal:v2.9.0
# 或：docker build -t ghcr.io/vencil/da-portal:v2.9.0 -f components/da-portal/Dockerfile .

# 4. Run
docker run -p 8080:80 ghcr.io/vencil/da-portal:v2.9.0
# 開瀏覽器：http://localhost:8080  →  Hub
```

> `make portal-build` 在改過任何工具 source 後必跑（CI 有 drift gate）。只是要跑 published image，直接 `docker run` 即可，無需以上 build 步驟。

### Helm（K8s，含 oauth2-proxy 與 tenant-api integration）

```bash
helm install da-portal \
  oci://ghcr.io/vencil/charts/da-portal --version 2.9.0 \
  -n monitoring --create-namespace \
  -f values-override.yaml
```

| Profile | Values file | 適用場景 |
|---------|-------------|----------|
| Default | `values.yaml` | 純 portal + tenant-api proxy（無 IdP） |
| Tier-1 | `values-tier1.yaml` | portal + tenant-api + GitOps writeback |
| Tier-2 | `values-tier2.yaml` | Tier-1 + oauth2-proxy + Ingress + NetworkPolicy |

詳 [helm/da-portal/README.md](../../helm/da-portal/README.md)。

---

## 4. Architecture

### 兩條交付路徑

| 路徑 | 服務內容 | 用途 |
|------|----------|------|
| **公開文件站**（mkdocs / GitHub Pages） | serve 整個 `docs/` 樹（Hub + `docs/assets/dist/` + assets） | 線上試玩 / 文件 |
| **da-portal Docker image**（本元件） | nginx 把 Hub + dist + assets 封進 image | 自架 / air-gapped / Helm 部署 |

兩者都載入同一份**預先 build 的 ESM bundle**（`docs/assets/dist/`），差別只在誰來做靜態服務。

### 瀏覽器端載入流程

```
┌─────────────────────────────────────────────────────────────────────┐
│ Browser                                                             │
│  ├─ /interactive/index.html     ── Hub（44 工具卡片 + search）       │
│  ├─ /assets/jsx-loader.html     ── 載入工具 + vendor probe + theme   │
│  └─ /assets/dist/<tool>.js      ── 預先 build 的 ESM bundle（esbuild）│
└─────────────────────────────────────────────────────────────────────┘
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

- **Image 不含 build step** — `Dockerfile` 只 `COPY` 預先 build 的成品 + `apk del nginx-module-image-filter nginx-module-xslt`（移掉靜態 server 用不到的模組，cascade 拔 28 個 dep；image 從 72 → 44 packages）+ libavif 殘留檢查（CVE-2025-48174 mitigation）
- **工具是 esbuild ESM bundle** — source 在 `tools/portal/src/`，`make portal-build` 產 `docs/assets/dist/`；瀏覽器直接 import，**不在瀏覽器端 transpile**
- **vendor probe** — `jsx-loader.html` 啟動跑同源 sync XHR 試 `vendor/react.production.min.js`；HTTP 200 走 local，否則退 CDN（React 18.3.1 / ReactDOM / Tailwind / Lucide 0.436.0）
- **tenant-api proxy** — `nginx.conf` 的 `/api/v1/` 預設 upstream 為 `tenant-api.monitoring.svc.cluster.local:8080`，自動轉 oauth2-proxy 注入的 `X-Forwarded-Email` / `X-Forwarded-User` / `X-Forwarded-Groups`

---

## 5. Customization（不重 build，只 mount）

### 改工具資料 / 引導流程

```bash
docker run -p 8080:80 \
  -v ./my-platform-data.json:/usr/share/nginx/html/assets/platform-data.json \
  -v ./my-flows.json:/usr/share/nginx/html/assets/flows.json \
  -v ./my-tool-registry.yaml:/usr/share/nginx/html/assets/tool-registry.yaml \
  ghcr.io/vencil/da-portal:v2.9.0
```

| Mount 檔 | 用途 | 來源 |
|----------|------|------|
| `platform-data.json` | Rule Pack catalog（DB / 中介軟體 / runtime defaults） | `make platform-data` 產 |
| `flows.json` | Guided Flows（onboarding 等順序步驟） | hand-edited |
| `tool-registry.yaml` | 44 工具的 metadata SSOT（title / audience / journey_phase / related） | hand-edited |
| `design-tokens.css` | 主題 CSS 變數（色 / 間距 / 字 / 明暗主題） | hand-edited，CI 跑 `check_undefined_tokens.py` |

### 改 nginx.conf（換 tenant-api upstream / 加 Prometheus proxy）

```bash
docker run -p 8080:80 \
  -v ./custom-nginx.conf:/etc/nginx/conf.d/default.conf \
  ghcr.io/vencil/da-portal:v2.9.0
```

預設 `nginx.conf` 把 `/api/v1/` proxy 給 `tenant-api.monitoring.svc.cluster.local:8080`。要改 upstream，或加 Prometheus reverse proxy 給 alert preview 直查 PromQL，editable 範本見 [`nginx.conf`](nginx.conf)。

---

## 6. Image Details

| Property | Value |
|----------|-------|
| Base image | `nginx:1.28-alpine3.23` |
| Image size | ~15 MB（含 dist + vendor），~12 MB（CDN-only，不含 vendor） |
| Health check | `GET /healthz`（`HEALTHCHECK --interval=30s --timeout=3s`，probe 走 127.0.0.1） |
| Listen port | 80 |
| User | `nginx`（non-root；K8s `securityContext.runAsNonRoot: true` 友好） |
| Build step | 無（純 `COPY` 預先 build 的成品） |
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

> CDN fallback 需要 `script-src` 含 `cdnjs.cloudflare.com`；air-gapped 部署（vendor 已 baked）可移掉這條，只留 `'self'`。

---

## 7. Environment Variables

| Variable | Default | 說明 |
|----------|---------|------|
| `NGINX_PORT` | `80` | container 內 listen port |
| `NGINX_WORKER_PROCESSES` | `auto` | nginx worker 數 |

> Portal **不靠 env 設定行為**；所有可變項（tenant-api upstream、工具列表、theme tokens）走 volume mount。

---

## 8. Develop（加 / 改一個工具）

| 步驟 | 命令 / 動作 |
|------|-------------|
| 1. 寫工具 JSX（ESM imports） | `tools/portal/src/interactive/tools/foo.jsx` |
| 2. 加 build entry | `tools/portal/entries/foo.entry.jsx`，並把 `foo` 加進 `tools/portal/manifest.json` |
| 3. 註冊 metadata SSOT | 編 `docs/assets/tool-registry.yaml` 加 `foo` block（title / audience / related） |
| 4. Build + 測試 | `make portal-build`（esbuild → dist）+ `make test-portal`（Vitest） |
| 5. 重新產 platform-data（如有動到 Rule Pack） | `make platform-data` |
| 6. Lint（auto-stage） | `pre-commit run --all-files` |
| 7. E2E（Playwright） | spec 在 `tests/e2e/portal-*.spec.ts` |

**JSX 寫作守則：**
1. **ESM imports** — 工具與其 sibling 模組用標準 `import` / `export`；`make portal-build` 由 esbuild bundle 成 dist。改過 source 後必跑 `portal-build`（CI 有 source↔dist drift gate）
2. **Design tokens not hardcoded colors** — 用 `var(--da-color-accent)` 而非 `bg-blue-500`；CI lint（`check_undefined_tokens.py`）會抓未定義 token
3. **重邏輯抽成可測模組** — calculator / validator / parser 等純邏輯抽到 tool-local `<tool>/<engine>.js` + 補 Vitest，別讓單檔長成 monolith

完整守則見 [`docs/internal/dev-rules.md`](../../docs/internal/dev-rules.md)。

---

## 9. Troubleshooting

| 症狀 | 可能原因 | 解法 |
|------|---------|------|
| Hub 載入後工具白屏 / 404 | image 沒含 `docs/assets/dist/`（舊 / 自建 image） | 確認 `make portal-build` 跑過且 `docs/assets/dist/` 有進 image；重 build |
| 404 on `/healthz` | 舊 image（< v2.3.0） | 重 build：`make portal-image` |
| CORS error on tenant-api | nginx proxy upstream 錯 | 改 `nginx.conf` 的 `proxy_pass`，或在 K8s 確認 `tenant-api` Service 存在於 `monitoring` namespace |
| Tenant Manager 顯示 demo-mode 提示 | `/api/v1/tenants/search` 回 404 / 5xx | 確認 tenant-api 跑著且 RBAC 有 read 權限；也可能是純靜態部署（無後端），demo mode 為 expected |
| 工具顯示舊資料 | 掛載的 `platform-data.json` 過舊 | `make platform-data` 重產 + 重 mount |
| air-gapped 環境 vendor probe 失敗 | `make vendor-download` 沒跑 / `docs/assets/vendor/` 沒進 image | 確認 `docs/assets/vendor/react.production.min.js` 存在；重 build |
| `var(--da-*)` 顯示為 fallback 色 | 用了未定義的 token | 跑 `python3 scripts/tools/lint/check_undefined_tokens.py`，補 `design-tokens.css` |

---

## 10. Related Documentation

- [Interactive Tools Hub 使用指南](../../docs/interactive-tools.md)
- [Tool Registry SSOT](../../docs/assets/tool-registry.yaml) — 44 工具 metadata
- [Design Tokens](../../docs/assets/design-tokens.css) — 主題系統 CSS 變數
- [Helm Chart README](../../helm/da-portal/README.md) — 三個 profile 細節
- [架構深度](../../docs/architecture-and-design.md) — 9 個核心設計概念
- [Dev Rules](../../docs/internal/dev-rules.md) — JSX 寫作守則 + 開發規範

---

## 11. 版號策略

| Tag 格式 | 產出 |
|----------|------|
| `portal/v*` | `ghcr.io/vencil/da-portal` Docker image + Helm chart |
| `exporter/v*` | threshold-exporter + Helm chart |
| `tools/v*` | da-tools CLI image |
| `tenant-api/v*` | tenant-api REST API |
| `v*` | Platform tag（GitHub Release，不觸發 build） |

---

> **回報問題** — Issue tracker：<https://github.com/vencil/Dynamic-Alerting-Integrations/issues>。若是工具載入 / vendor probe / tenant-api proxy 問題，請附 (1) 瀏覽器 console log，(2) `curl -I http://<portal>/assets/dist/<tool>.js` 與 `/assets/vendor/react.production.min.js` 結果，(3) `kubectl logs -l app=da-portal -c oauth2-proxy --tail 50`（如果是 Tier-2 deploy）。
