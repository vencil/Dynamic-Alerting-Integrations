# da-portal (v2.9.0)

<!-- 標題版號 = 最後 released portal tag（目前 v2.8.0）。Release wrap 切各線 tag 時，
     本標題 + 下方 helm --version 跟著批次同步 bump。 -->

> 💡 **只想把元件跑起來看看？** → **[QUICKSTART.md](QUICKSTART.md)**（`docker run` 一行、≤ 2 分鐘看到一整套互動工具）。本篇 README 是給**部署 / 維運 portal 的人**看的進階配置與 packaging **參考**。

把 Dynamic Alerting 的 **瀏覽器互動工具**（Hub + Wizard + Tenant Manager + Self-Service Portal）封進一顆 nginx-alpine image，供內網 / air-gapped 環境離線使用。工具在 CI 端用 **esbuild 預先 build 成 ESM bundle**，image 只負責靜態服務——不在 runtime 做任何 build。

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

- **Source（輸入）** — `tools/portal/src/interactive/tools/*.jsx`（`tools/portal/manifest.json` 為 build entry SSOT、`docs/assets/tool-registry.yaml` 為工具 metadata SSOT）+ `tools/portal/src/getting-started/wizard.jsx`
- **Build** — `make portal-build` 跑 [esbuild](../../tools/portal/build.mjs)，把每個工具 bundle 成 ESM 檔輸出到 `docs/assets/dist/<tool>.js`。**每個消費端自己 build**：image 的 `portal-build` stage、Pages 部署、Smoke／visual baseline 都從 `tools/portal/` 現場 build；dist 是 build 產物，不在 git 裡（[#3738](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3738)）。測試走 `make test-portal`（Vitest）
- **Image（輸出）** — 一顆 nginx-alpine，COPY 進 Hub（`docs/interactive/`）+ `portal-build` stage 建出的 bundle（落在 `/assets/dist/`）+ 共享 assets + vendor；瀏覽器直接載入 ESM bundle，**runtime 不做 build / transpile**
- **為什麼是「預先 build + 靜態服務」** — 1) 內網 / air-gapped 場景不能在 runtime 跑 npm；2) build 在 image build／部署流程裡完成，跑起來的 image 與部署端零 Node 依賴；3) image 純靜態 → 不需 secret、不需 runtime config
- **vendor probe（離線優先）** — `make vendor-download` 把 React 18.3 / ReactDOM / Tailwind / Lucide 抓進 `docs/assets/vendor/`；瀏覽器啟動先 probe local，找不到才 fallback CDN（每個工具 bundle 透過頁面提供的這些 global 執行）
- **不做的事** — 不在 runtime build；不持久化（無 state，資料來自 mount 的 JSON）；不轉送任何請求（不 proxy 到 tenant-api、Prometheus 或其他元件，見下方「需要登入的 portal」）

> **Hub UX 細節**（工具的分類、journey-phase、related-tool 圖、search）見 [docs/interactive-tools.md](../../docs/interactive-tools.md)。本 README 只負責 operator quick-reference。

---

## 2. What's New in v2.8.0

精簡重點（完整版本歷程見 [CHANGELOG.md](../../CHANGELOG.md)）：

- **Tenant Manager API-First** — Tenant Manager 預設打 `GET /api/v1/tenants/search`（live data，分頁避過千租戶 DOM freeze），無後端時 fallback 靜態 `platform-data.json` + demo-mode 提示
- **Tenant Manager → Wizard 深連結** — `TenantCard` 可一鍵帶著 tenant id 跳到 Alert Builder / Routing Trace，少掉反覆 copy-paste
- **oauth2-proxy CVE 收斂** — 升到無已知 CRITICAL/HIGH 的版本

> **v2.8.0 之後（未 bump 版號）** — 工具的核心純邏輯（calculator / validator / parser / 路由演算法）已抽成可單元測試的模組並補上測試（behavior-preserving）；對部署 / 使用零影響。

---

## 3. Quick Start

### Docker（單機 / air-gapped）

```bash
# 1. 抓 vendor 進 docs/assets/vendor/（air-gapped 必跑；CI build 自動跑）
make vendor-download

# 2. Build image（從 repo root，因為 COPY 來源是 docs/ 與 tools/；
#    工具 bundle 由 Dockerfile 的 portal-build stage 從 tools/portal/ 現場 build）
make portal-image                                  # 預設 tag: ghcr.io/vencil/da-portal:v2.9.0
# 或：docker build -t ghcr.io/vencil/da-portal:v2.9.0 -f components/da-portal/Dockerfile .

# 3. Run
docker run -p 8080:80 ghcr.io/vencil/da-portal:v2.9.0
# 開瀏覽器：http://localhost:8080  →  Hub
```

> image 的 `portal-build` stage 自己 build bundle，所以 build image 前不必先 `make portal-build`；本機 `docs/assets/dist/` 不會進 build context（`.dockerignore`）。dist 不進 git，改過工具 source 只 commit source。只是要跑 published image，直接 `docker run` 即可，無需以上 build 步驟。

### Helm（K8s，純靜態）

```bash
helm install da-portal \
  oci://ghcr.io/vencil/charts/da-portal --version 2.9.0 \
  -n monitoring --create-namespace
```

da-portal chart 只有純靜態部署：一個 nginx 容器，沒有登入、沒有 Ingress、不轉送任何請求。

### 需要登入的 portal（Tenant Manager 等打 API 的工具）

用 tenant-api chart：同一顆 image 以 portal 容器跑在 tenant-api 的 pod 裡，前面是 tenant-api 的 oauth2-proxy，`/api/` 送 tenant-api、其餘送 portal（[ADR-027 §5](../../docs/adr/027-tenant-api-caller-identity.md)）。整個部署只有一個 oauth2-proxy、一個 Ingress。

詳 [helm/da-portal/README.md](../../helm/da-portal/README.md)。

---

## 4. Architecture

### 兩條交付路徑

| 路徑 | 服務內容 | 用途 |
|------|----------|------|
| **公開文件站**（mkdocs / GitHub Pages） | serve 整個 `docs/` 樹（Hub + `docs/assets/dist/` + assets） | 線上試玩 / 文件 |
| **da-portal Docker image**（本元件） | nginx 把 Hub + dist + assets 封進 image | 自架 / air-gapped / Helm 部署 |

兩者載入的都是從 `tools/portal/` 現場 build 出來的 ESM bundle（URL 都是 `/assets/dist/`）：Pages 部署在 `mkdocs build` 前先 build，image 由 `portal-build` stage build。差別只在誰來做靜態服務。

### 瀏覽器端載入流程

```
┌─────────────────────────────────────────────────────────────────────┐
│ Browser                                                             │
│  ├─ /interactive/index.html     ── Hub（工具卡片 + search）          │
│  ├─ /assets/jsx-loader.html     ── 載入工具 + vendor probe + theme   │
│  └─ /assets/dist/<tool>.js      ── 預先 build 的 ESM bundle（esbuild）│
└─────────────────────────────────────────────────────────────────────┘
              │ static asset GET
              ▼
┌──────────────────────────┐
│ nginx (this image)       │
│  - serve docs/...        │
│  - CSP / HSTS headers    │
│  - SPA fallback          │
│  - /healthz K8s probe    │
│  - tenants/simulate: 501 │
└──────────────────────────┘
```

登入後的 portal（tenant-api chart）：瀏覽器 → oauth2-proxy → `/api/` 經 pod 內 Unix socket 到 tenant-api、其餘到同 pod 的 portal 容器；portal 容器只讀 chart 自帶的 nginx 設定，同樣不轉送。

關鍵點：

- **最終 image 不含 build 工具** — bundle 在 `portal-build` stage（node）建好，最終 nginx stage 只 `COPY --from=portal-build` 成品 + `apk del nginx-module-image-filter nginx-module-xslt`（移掉靜態 server 用不到的模組，cascade 拔 28 個 dep；image 從 72 → 44 packages）+ libavif 殘留檢查（CVE-2025-48174 mitigation）
- **工具是 esbuild ESM bundle** — source 在 `tools/portal/src/`，`make portal-build` 產 `docs/assets/dist/`；瀏覽器直接 import，**不在瀏覽器端 transpile**
- **vendor probe** — `jsx-loader.html` 啟動跑同源 sync XHR 試 `vendor/react.production.min.js`；HTTP 200 走 local，否則退 CDN（React 18.3.1 / ReactDOM / Tailwind / Lucide 0.436.0）
- **不轉送** — `nginx.conf` 沒有任何 `proxy_pass`、不讀身分 header（ADR-027 §5；`tests/shared/test_portal_image_nginx_conf.py` 守）
- **simulate 不代理**（[#2125](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2125)）— `POST /api/v1/tenants/simulate` 由 threshold-exporter 提供、不在 tenant-api，本 image 與 Helm chart 都**不**代理它：`location = /api/v1/tenants/simulate` 固定回 `501` + `{"code":"SIMULATE_NOT_PROVIDED",...}`，simulate-preview 工具據此顯示「此部署不提供模擬預覽」

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
| `tool-registry.yaml` | 工具的 metadata SSOT（title / audience / journey_phase / related） | hand-edited |
| `design-tokens.css` | 主題 CSS 變數（色 / 間距 / 字 / 明暗主題） | hand-edited，CI 跑 `check_undefined_tokens.py` |

### 改 nginx.conf

```bash
docker run -p 8080:80 \
  -v ./custom-nginx.conf:/etc/nginx/conf.d/default.conf \
  ghcr.io/vencil/da-portal:v2.9.0
```

範本見 [`nginx.conf`](nginx.conf)。⚠️ 自行加 `proxy_pass` 把請求轉給 tenant-api 不會帶身分：tenant-api 只在自己 pod 內的 Unix socket 上接受身分（ADR-027 §1）。

---

## 6. Image Details

| Property | Value |
|----------|-------|
| Base image | nginx-alpine（版本以 [`Dockerfile`](Dockerfile) 的 `FROM` 為準） |
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
1. **ESM imports** — 工具與其 sibling 模組用標準 `import` / `export`；`make portal-build` 由 esbuild bundle 成 dist。dist 不進 git，只 commit source；CI 的 Portal Tests 會從 source build 並跑 bundle size budget 與 Vitest
2. **Design tokens not hardcoded colors** — 用 `var(--da-color-accent)` 而非 `bg-blue-500`；CI lint（`check_undefined_tokens.py`）會抓未定義 token
3. **重邏輯抽成可測模組** — calculator / validator / parser 等純邏輯抽到 tool-local `<tool>/<engine>.js` + 補 Vitest，別讓單檔長成 monolith

完整守則見 [`docs/internal/dev-rules.md`](../../docs/internal/dev-rules.md)。

---

## 9. Troubleshooting

| 症狀 | 可能原因 | 解法 |
|------|---------|------|
| Hub 載入後工具白屏 / 404 | image 沒含 `/assets/dist/`（舊 / 自建 image，或 `portal-build` stage 被改掉） | 確認 image 的 `/usr/share/nginx/html/assets/dist/` 有 bundle；用 repo 的 Dockerfile 重 build |
| 404 on `/healthz` | 舊 image（< v2.3.0） | 重 build：`make portal-image` |
| Tenant Manager 顯示的是示範租戶（`prod-mariadb-01` 等） | `/api/v1/tenants/search` 非 2xx（含 401／403）或連不上，**且** `platform-data.json` 缺或是空的（有內容時改顯示那份靜態資料） | 純靜態部署（本 image 單跑、da-portal chart）沒有後端，demo mode 為 expected；登入後的 portal（tenant-api chart）則確認 tenant-api 跑著且 RBAC 有 read 權限 |
| 工具顯示舊資料 | 掛載的 `platform-data.json` 過舊 | `make platform-data` 重產 + 重 mount |
| air-gapped 環境 vendor probe 失敗 | `make vendor-download` 沒跑 / `docs/assets/vendor/` 沒進 image | 確認 `docs/assets/vendor/react.production.min.js` 存在；重 build |
| `var(--da-*)` 顯示為 fallback 色 | 用了未定義的 token | 跑 `python3 scripts/tools/lint/check_undefined_tokens.py`，補 `design-tokens.css` |

---

## 10. Related Documentation

- [Interactive Tools Hub 使用指南](../../docs/interactive-tools.md)
- [Tool Registry SSOT](../../docs/assets/tool-registry.yaml) — 工具 metadata
- [Design Tokens](../../docs/assets/design-tokens.css) — 主題系統 CSS 變數
- [Helm Chart README](../../helm/da-portal/README.md) — 純靜態部署
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

> **回報問題** — Issue tracker：<https://github.com/vencil/Dynamic-Alerting-Integrations/issues>。若是工具載入 / vendor probe / API 呼叫問題，請附 (1) 瀏覽器 console log，(2) `curl -I http://<portal>/assets/dist/<tool>.js` 與 `/assets/vendor/react.production.min.js` 結果，(3) 登入後的 portal：`kubectl -n tenant-api logs deploy/tenant-api -c oauth2-proxy --tail 50`。
