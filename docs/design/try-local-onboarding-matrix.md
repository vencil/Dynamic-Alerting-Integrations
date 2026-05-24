---
title: "Try-Local Onboarding Matrix — 四產品 Showcase 漏斗與啟動模式邊界"
tags: [design, onboarding, developer-experience, try-local, showcase]
audience: [platform-engineer, domain-expert, contributor]
version: v2.9.0
lang: zh
status: accepted
created_at: 2026-05-25
updated_at: 2026-05-25
---
# Try-Local Onboarding Matrix — 四產品 Showcase 漏斗與啟動模式邊界

> 本文為 epic [#449](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/449)（sub-issue [#464](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/464)）的 ADR-style 設計決策紀錄，是 try-local onboarding 的策略 SSOT。後續實作子題（component QUICKSTART / 多架構 image / compose stack / README hub）以本文為準。
>
> 依語言政策（ZH-primary SSOT，見 [ssot-language-evaluation](../internal/ssot-language-evaluation.md)）本 rationale 文件為 ZH-only；對外漏斗頂內容（root README、QUICKSTART header）另採雙語，見下方「雙語策略」。

## 狀態

**Accepted**（2026-05-25）。設計經 diverge → converge → 兩輪外部 adversarial review（Claude adversarial agent + Gemini critique，均經 verify-don't-claim 過濾）後定案。ADR 初稿再經第三輪外審（Gemini，APPROVED + 3 執行期 blocking patch）→ 合併為 D3 指令防呆 + D4 啟動時序 + D7 執行期實作前提。

## 背景

v2.8.0 後，平台的 4 個 component 都已是可獨立交付的產品：

- **threshold-exporter** — config-driven 多租戶閾值發射器（SHA-256 熱重載、目錄掃描）
- **tenant-api** — file-based 租戶自助管理 API（GitOps 回寫，無 DB 依賴）
- **da-portal** — 38 個互動工具的 self-service portal
- **da-tools** — 70+ 子命令的維運 CLI 工具箱

但 onboarding 主路徑**只有「裝進 k8s」一條**，把上述 component 當產品的宣傳價值埋掉了。owner 的北極星原話：

> 「目前這一套設計有點浪費宣傳效果，沒有把現在也是產品的其他 component 帶出來介紹、試用。」

v2.8.0 closure session（2026-05-12）owner 本機嘗試建立完整 stack 跑 soak-readiness，**在多層失敗**（Dockerfile build break + kind apply 缺 manifest path + mariadb 啟動缺 root password 注入）。同樣的 onboarding pain 會壓在 prospective customer / contributor 身上。

本 epic 對齊 v2.9.0 strategic theme（[#426](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/426)「reactive > predictive」）——try-local 是 reactive feedback 的接收口。

### 成功標準（SLI）

1. **Showcase SLI**：訪客 **~1 分鐘內**看懂這裡有 4 個各自能試的產品。
2. **Try SLI**：cold machine（剛 clone、無 prior cache）從 `git clone` 到第一個預期輸出（`/metrics` payload / `curl /api/v1/me` 回 200 / 瀏覽器看到 UI / da-guard 攔截紅字）**≤ 10 分鐘**。Toolchain 安裝時間不計，但文件須明列 prerequisite + 安裝連結。

> 關鍵認知：成功標準不是「整合 stack 跑得起來」，而是「**每個 component 的產品宣傳潛力都不被浪費**」。據此，per-component 可見度（component QUICKSTART #466 + README hub #467）的權重 **高於** 整合 compose 機制（#465）。

## 決策

### D1. 摩擦階梯（Friction Ladder）— 4 個啟動模式

依摩擦力由低到高排列，宣傳命中率最高的放在最低摩擦層：

| Mode | 定位 | 指令 | 摩擦 | 適合 |
|---|---|---|---|---|
| **Mode 0 — 核心雙星（hero）** | 10 秒看到產品價值 | `docker compose up da-portal tenant-api` | 最低（2 service，無 toolchain） | 所有「想看」的人 |
| Mode 1 — 單 binary | 試單一 component | `go build && ./threshold-exporter …` / `docker run …` | 中（需該 component 的 toolchain） | 想深入單一產品 |
| Mode 2 — 全 compose | 看整合 + 真實告警紅燈 | `docker compose up` | 中高（6+1 service） | 想看整套運作 |
| Mode 3 — kind local cluster | production-shape dry-run | 既有 Helm / k8s manifest | 最高 | ops 驗收 |

**Mode 0「核心雙星」是 hero**（推翻早期「portal 單機一行指令」的構想）：da-portal 的 38 個工具中，~34 個讀 build-time 靜態 `platform-data.json` 可獨立運作，但旗艦 **Tenant Manager / Saved Views / Simulate** 需要 tenant-api 後端（portal 對 `/api/v1/*` 的 13 個 fetch）。單跑 portal 會讓旗艦顯示 error state → 反傷第一印象。`docker compose up da-portal tenant-api` 兩個指令、約 10 秒，旗艦全活，直接 demo「租戶自助管理 + GitOps 回寫」的真實產品價值。

**進程式揭露**：Mode 0 核心雙星 → `docker compose up`（全）加上監控網格 + 真實告警紅燈 → Mode 3 production-shape。

### D2. Persona × Mode 矩陣

| Persona | 想看 | 想試 | 想驗 | 建議 mode |
|---|---|---|---|---|
| **Platform Engineer** | 4 產品 overview | exporter binary / 全 compose | k8s production readiness | **Mode 0 → Mode 2 → Mode 3** |
| **Domain Expert** | Rule Pack 結構、Profile Builder | Profile Builder 10min | 客戶 rule corpus pilot | **Mode 0 → Mode 2**（多半無 Go toolchain，走 hosted UI） |
| **Tenant User** | 自己 tenant config | yaml + simulate 10min | PR write-back flow | **Mode 0 → Mode 2**（hosted UI） |
| **Contributor（評估要不要貢獻）** | 4 產品能耐 | da-tools 攔截 demo / 任一 binary | build + test | **Mode 0 → Mode 1** |

所有 persona 的「想看」一律先導向 **Mode 0**——它是零摩擦的共同前門。

### D3. 四產品 At-a-Glance 定位（宣傳實體 payload）

README hub（#467）的 root README「Try it locally」展廳直接鋪開此矩陣。每個 component 必含「這是什麼 / 為什麼 / 給誰」+ 一行有感試用：

| 產品 | 核心價值（What & Why） | Day-0 一行快刷 | Day-1 深度整合 |
|---|---|---|---|
| **da-portal** | 運維自助入口，把複雜 PromQL 與告警配置封進 38 個互動工具，瀏覽器打開就玩 | `docker compose up da-portal tenant-api` | 納入完整監控網格、與真實告警紅燈聯動 |
| **threshold-exporter** | 把告警閾值代碼化（Config-as-Code），SHA-256 熱重載、目錄掃描多租戶——改 YAML 即生效，不重啟 | `go build && ./threshold-exporter --config-dir ./config/conf.d` | Helm 部署、對齊動態告警體系 |
| **tenant-api** | 輕量 file-based 租戶配置收容所，天生支援 GitOps 自動化 PR 工作流，零 DB | `docker run --rm -p 8080:8080 -v $(pwd)/seed:/conf.d ghcr.io/vencil/tenant-api:v2.8.0` | 配合 da-tools 簽發 federation key（v2.9.0） |
| **da-tools** | 運維瑞士軍刀，在 CI 階段攔截高基數指標與惡意配置，守護監控穩定 | `docker run --rm -v $(pwd)/examples:/work ghcr.io/vencil/da-tools:v2.8.0 guard /work/dangerous-conf.d`（親眼見證攔截） | 整合進 GitHub Actions / GitLab CI 作 Required Merge Guard |

> **指令防呆**（外審 round 2 Patch 2）：Day-0 指令一律採 **docker-native volume-mount 形式**（無 toolchain 假設、無相對路徑陷阱、即用即棄 `--rm`）。da-tools 的 image tag 是 **`:v2.8.0`**（`tools/v` git tag prefix 在發佈時被 strip，**非** `:tools-v2.8.0`）。完整可複製版見各 component QUICKSTART（#466）。tenant-api 的寫回 demo 需 git-init 的 seed repo，見 D7。

### D4. 告警觸發模型（為什麼 try-local 這樣 seed）

理解此模型才能正確設計 seed，故記錄為 ADR 級 reference：

- threshold-exporter 的 `/metrics` emit 的是 `user_threshold` gauge ——**使用者設定的閾值本身**，不是實測值。
- 真實告警靠 Prometheus rule 比對「實測值 > `user_threshold`」才 fire。純 exporter + Prometheus + Alertmanager **沒有實測值來源**，低閾值不會自己 fire。
- operational rule pack 另有**自含 sentinel 告警**（`TenantSilentWarning` / `TenantSeverityDedupEnabled` 等），純 config-driven 即 fire，但它們 `severity: none`（inhibit source、非 user-facing 紅燈）。

**seed-with-signal = 兩層**（決策 D5）：

1. **主紅燈（產品價值）**：`pushgateway` 持一筆合成實測值（如 `mysql_global_status_threads_connected{tenant=…}=200`）> seed 的低 `user_threshold`（如 50）→ DB rule pack 的 critical **真的 fire** → 紅燈在 **Prometheus `:9090/alerts` + Alertmanager `:9093`**（**不是** portal——portal 不讀 live 告警）。機制複用已驗的 `tests/e2e-bench` 模式，try-local 用一個 **one-shot seed container** push 一次即退出（單筆靜態 push，非持續產流量）。
2. **Secondary（設計概念展示）**：seed 另一個 tenant 帶 silent_mode active + severity dedup 預設開 → sentinel 在 `/metrics` + inhibit 行為可見，demo v2.8.0 的 **Sentinel Alert / Severity Dedup** 設計概念。

**啟動時序**（外審 round 2 Patch 3，機制修正）：pushgateway **持久保留** push 進來的 metric 直到被 scrape，且 Prometheus 對 firing alert 會週期 **re-send**——故 one-shot seed 早於 Prometheus/Alertmanager 就緒**不會**丟失告警。正確的 determinism 來源是：(a) one-shot seed `depends_on` **pushgateway**（`condition: service_healthy`，pushgateway `/-/healthy`）；(b) **smoke test 以 timeout poll `/api/v1/alerts`** 直到 critical firing（**非**固定 sleep 後單次斷言）；(c) 監控服務 ordering：prometheus ← exporter + pushgateway（started）、alertmanager ← prometheus（healthy）。詳 D7。

### D5. Auth 接線（B1 + 四層防線）

try-local 要讓 portal 在 compose 活，需解兩件事：**upstream DNS** + **身分 header**（compose 無 oauth2-proxy）。da-portal 的 `nginx.conf` 已內建 `/api/v1/` reverse proxy，但 upstream 寫死 K8s service DNS。

**決策 B1**（採「跑我們出貨的同一份 image」以建立信任）：

- **upstream**：docker-compose **network alias** 讓 `tenant-api.monitoring.svc.cluster.local` 解析到 compose 的 tenant-api 容器——published image 原封跑，**零檔案掛載**（避開 Windows/WSL2 單檔掛載地雷）。
- **auth**：tenant-api 新增 `--dev-bypass-auth` / `TA_DEV_BYPASS_AUTH` flag（預設 off），on 時 middleware 自注 dev 身分（user + group），搭配 seed 的 `_rbac.yaml`（demo group → 兩個 sample tenant）讓 `/api/v1/me` 回正確 tenant 列表。

**把 auth-bypass 埋進 production binary 的資安風險，以四層防線馴服**（對齊專案既有「四層防線」文化）：

1. **預設 off**——不主動啟用。
2. **僅 loopback bind**——`--dev-bypass-auth` on 時若 bind address 非 loopback → 拒啟。
3. **Runtime poison pill**——startup 時若 bypass=true 且偵測到 `KUBERNETES_SERVICE_HOST` env 或 `/var/run/secrets/kubernetes.io` 存在（即身處 k8s 叢集內）→ 立即 `panic("FATAL: Dev auth bypass is strictly forbidden inside Kubernetes clusters.")`。防手動惡意佈署 / 極端誤配（SAST 防不到的）。
4. **SAST 護欄**——[#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) kube-linter 新增 rule：`TA_DEV_BYPASS_AUTH` 出現在任何 helm/k8s manifest → BLOCK（required status check，server-side 不可繞）。

> 第 1-2 層防君子與正常 CI；第 3-4 層防小人與意外。blast radius 死鎖在 local。dev-bypass 須在 try-local README 明標「dev fixture，非 production auth」。

### D6. 雙語策略（Cβ — 漏斗頂雙語）

- **強制雙語**：root README「Try it locally」展廳（`README.en.md` 已存在，bilingual structure check 對既有配對強制 h2/h3 parity）+ 4 個 QUICKSTART 的「what / why / 一行試」header 段。
- **ZH-primary（不另製 .en.md）**：本 ADR rationale + QUICKSTART 深層 troubleshooting。

理由：EN 投在「讓海外 dev 決定要不要點進去試」的漏斗槓桿最高處，深層 rationale 留 ZH-primary，維護有界且不違 語言政策 lock（ZH 為主 + EN 為輔本就政策允許；強制全遷移才違）。promo 對象以現有中文客群為主、漏斗頂兼顧國際 drive-by。

> **Re-eval trigger**：若日後確認轉國際定位（或觸發 語言政策 lock 任一條件：≥3 非 ZH 母語 contributor PR/issue、客戶 RFP 要求 EN、maintainer 主動 pivot），則升級為全雙語並當作 語言政策 lock re-eval 的 maintainer-pivot trigger。

### D7. 執行期實作前提（外審 round 2 合併，Track C2-C9 must-honor）

外審 round 2 抓到三個會讓 SLI 第一秒破功的執行期盲點，列為 compose stack（#465）的硬性實作前提：

1. **GitOps 寫回需 git-init 的 seed repo（Patch 1，BLOCKING）**：tenant-api 的 gitops writer 即使在 `direct` 模式也會對 `gitDir` 跑 `git add` + `git commit`（`writer.go`），無 `.git` 則 `git rev-parse HEAD` 失敗 → portal 按 Save 觸發 500 或寫進 container ephemeral layer（重啟蒸發）。故 `try-local/seed/` **必須預建一個 `git init` 過、含初始 conf.d + 一個 commit 的 config repo**，compose 的 tenant-api 以 **volume mount** 掛入，並設 `TA_WRITE_MODE=direct`（commit-on-write，**不需 remote**）。如此 Save 後一個**真實 git commit** 落在 host 掛載目錄——Day-0 最強震撼。`pr` / `pr-github` / `pr-gitlab` 模式會 `git push origin`，需真 remote，屬 production-only，不在 try-local demo 範圍。
2. **Day-0 指令 docker-native（Patch 2）**：見 D3 防呆註——volume-mount 形式 + 正確 image tag `:v2.8.0`。
3. **告警 pipeline 啟動 determinism（Patch 3）**：見 D4「啟動時序」——seed `depends_on` pushgateway healthy；smoke test 以 timeout poll `/api/v1/alerts`；監控服務正確 ordering。

## 為什麼不用其他方案

- **為什麼沒 Mode 4 = k3s？** Mode 3 已用 kind 覆蓋 production-shape dry-run；k3s 不增加 onboarding 價值、徒增第二套 k8s 心智負擔。
- **為什麼移除 mariadb？** tenant-api 是 file-based（conf.d + git），啟動不需 DB（`wireFederation` 在 federation-key 為空時碰 k8s 前即 return）；平台也不直連被監控的 DB（exporter 純 config-driven）。原 issue 的 mariadb 依賴是事實誤解；移除後 stack 啟動更快、少一組失敗模式。
- **為什麼 seed 不純 sentinel？** 自含 sentinel 全 `severity: none`（plumbing），且 portal 不讀 live 告警 → 純 sentinel 既看不到、又非「產品抓到問題」的有感 demo。
- **為什麼 seed 不純 pushgateway？** 只 pushgateway 會錯過 v2.8.0 Sentinel Alert / Severity Dedup 設計概念的展示。故**兩者都做**：pushgateway 當主紅燈、sentinel 當概念展示。
- **為什麼 auth 不用 B2（衍生 image）/ B3（runtime mount）？** B3 的 runtime 單檔掛載在 WSL2 易觸發 `invalid mount config` / 檔案鎖定；B2 衍生 image 雖 prod 零風險，但「特製 image」削弱「跑我們出貨的同一份 image」的信任宣傳。B1 + 四層防線在信任宣傳與資安之間取最佳平衡。
- **為什麼 portal 單機不當 hero？** 旗艦 Tenant Manager 需 backend，單跑顯示 error state，反傷第一印象。

## 實作計畫

| Track | Sub-issue | 交付 | 依賴 |
|---|---|---|---|
| A | #464 | 本 ADR | — |
| C1 | #463 | multi-arch image（amd64+arm64）+ da-tools build 重構 | — |
| B | #466 | 4 × QUICKSTART.md（含產品定位 header、有感 fixture，禁 `--help`） | — |
| C2-C9 | #465 | try-local/ compose（6+1：exporter+pushgateway+prometheus+alertmanager+tenant-api+da-portal + one-shot seed）+ 兩層 seed + git-init seed repo（D7-1）+ network-alias + depends_on/smoke-poll determinism（D7-3）+ nightly | amd64 image（已有 v2.8.0）+ B anchor 名 |
| D | #467 | README hub + 動態 badge + Next-Step CTA + Doc-as-Code sync | B + C2-C9 anchor |

**PR 序列**：PR1(A) / PR2(C1) / PR3(B) 可並行 → PR4(C2-C9) → PR5(D)。各 track 各 branch → PR → owner merge（dev-rules #12）。C2 不被 C1 硬卡（amd64 image 已存在，arm64 解鎖 Mac M-series；C1 須在公開 badge / 宣傳前落地）。

## 後果（Consequences）

### 正面

- 4 個 component 各有 standalone「試我」入口，宣傳潛力不被浪費。
- Mode 0 兩指令 10 秒看到 GitOps 真實價值。
- 跑 published image（B1）= 最強信任宣傳。
- seed 兩層同時 demo 產品價值（threshold-breach）與設計概念（sentinel / dedup）。

### 負面 / 取捨（Explicit Trade-offs）

- **B1 對 production tenant-api binary 引入 auth-bypass flag** ——以四層防線（含 runtime poison pill + SAST BLOCK）馴服，但仍是須持續審視的 surface。
- **紅燈在 Prometheus/AM UI、不在 portal** ——portal 賣點改為 live Tenant Manager / Saved Views / Simulate；「整合告警 dashboard 進 portal」是另一個（更大）的 scope。
- **try-local stack 維護負擔** ——以 nightly smoke + 連續 3 fail 自動開 issue（`try-local-broken` label，不擋 release）+ 動態 badge（綁 smoke 結果，避免靜態綠 badge 對壞掉的 stack 說謊）緩解。
- **Cβ 半雙語檔的 parity 維護**略尷尬（heading 同步靠 bilingual check 機械強制）。

### Defer-with-trigger

- **pushgateway 之外的「真實 metric 持續穿閾值」demo** → trigger：客戶/貢獻者明確要求看連續告警行為，或客戶 onboarding（post-epic dogfooding）浮現此 gap。
- **try-local README 全雙語 / per-component 深層雙語** → trigger：語言政策 lock re-eval 條件。
- **Mode 3 kind 的一鍵自動化（`make try-kind`）** → trigger：compose Mode 2 不足以提供 production-shape 回饋時。
- **federation 進 try-local** → trigger：單機可模擬 federation 多叢集拓樸變得可行（目前本質 multi-cluster）。
- **da-tools helm chart** → trigger：da-tools 出現長駐 server mode（目前 CLI 一次性，無 chart）。

## Out of Scope（邊界）

- ❌ 任何新 component（mock traffic generator / sidecar / operator）——pushgateway 走 **one-shot 靜態 push**，非持續產流量。
- ❌ Production-deployment 重新設計（k8s path 不動）。
- ❌ Auth/RBAC 完整 demo——用 dev fixture（`--dev-bypass-auth` + `_rbac.yaml`），**不證明** production RBAC。
- ❌ **Windows 原生（非 WSL2）**——不支援 bind mount path 翻譯；Windows 用戶需 WSL2 + Docker Desktop WSL2 backend。
- ❌ **arm32 / RISC-V** multi-arch——只做 amd64 + arm64。
- ❌ **mariadb / 任何 DB 依賴**——tenant-api file-based、平台不直連被監控 DB。
- ❌ **federation 多叢集拓樸**（ADR-020 / ADR-021）——本質 multi-cluster，不適單機 compose；tenant-api federation endpoints 在 try-local 維持 default-off（`--federation-key` 空）。
- ❌ **Anonymous telemetry / usage ping**——與 reactive theme 衝突 + 隱私複雜度。

## 關聯

- Epic [#449](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/449)；Track sub-issues [#463](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/463) / [#464](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/464) / [#465](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/465) / [#466](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/466) / [#467](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/467)
- v2.9.0 closure theme [#426](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/426)（reactive > predictive）
- 配套 [#447](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/447) Dockerfile build context（compose stack 前置，已 closed）+ [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) container/k8s IaC SAST（dev-bypass SAST 護欄 + try-local stack 過 lint）
- 語言政策 [ssot-language-evaluation](../internal/ssot-language-evaluation.md)（語言政策 lock）

## 相關資源

- [getting-started](../getting-started/)（3 persona guide，README hub #467 加 try-local first step）
- [rule-packs](rule-packs.md)（operational rule pack sentinel 來源）
- 既有 compose 接線參考：`tests/e2e-bench/docker-compose.yml`（exporter→prometheus→alertmanager + pushgateway 模式）
