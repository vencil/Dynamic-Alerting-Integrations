---
title: "治理、稽核與安全合規"
tags: [governance, security, audit]
audience: [platform-engineer, security]
version: v2.9.0
lang: zh
---
# 治理、稽核與安全合規

> **Language / 語言：** | **中文（當前）** | [English](./governance-security.en.md)

> 相關文件：[Architecture](architecture-and-design.md) · [GitOps Deployment](integration/gitops-deployment.md) · [Custom Rule Governance](custom-rule-governance.md)

---

## 治理與稽核 (Governance & Audit)

### 自然稽核跡 (Natural Audit Trail)

每個租戶 YAML ⟷ Git 歷史記錄：

```bash
$ git log --follow conf.d/db-a.yaml
commit 5f3e8a2 (HEAD)
Author: alice@db-a-team.com
Date:   2026-02-26

    Increase MariaDB replication_lag threshold from 10s to 15s

    Reason: High load during 6-9pm peak hours
    Ticket: INCIDENT-1234

commit 1a2c5b9
Author: bob@db-a-team.com
Date:   2026-02-20

    Add monitoring for new Redis cluster
    Metric: redis_memory_usage_percent
    Default: 75% warning, 90% critical
```

> **git 稽核 vs 執行期 ConfigMap 稽核**：上面的 git 歷史是**設定來源**的天然稽核跡；但彙整渲染後的跨租戶 ConfigMap 是**執行期**產物，其「叢集內竄改」偵測需要 off-cluster kube-apiserver audit（tamper-evident-from-inside）。operator RBAC 收斂基線 + audit policy 見 [跨租戶 ConfigMap 硬化](cross-tenant-configmap-hardening.md)。

### 權責分離 (Separation of Duties)

| 角色 | 職責範圍 | 可修改 | 無法修改 |
|------|---------|--------|---------|
| **Platform Team** | 全域預設、Rule Pack 維護、enforced routing | `_defaults.yaml`、`_routing_enforced`、`_routing_defaults`、Rule Pack YAML | 租戶覆蓋 |
| **Domain Expert** | 特定 DB 類型的 Rule Pack、metric dictionary | `rule-packs/rule-pack-<db>.yaml`、`metric-dictionary.yaml` | 平台預設、其他 DB |
| **Tenant Team** | 自身閾值、路由、運營模式 | `<tenant>.yaml` 中的閾值三態、`_routing`(含 overrides)、`_silent_mode`、`_state_maintenance`、`_severity_dedup` | 預設值、state_filters、其他 tenant |

Git RBAC（搭配 `.github/CODEOWNERS`）：
```bash
# CODEOWNERS — PR 自動指派 reviewer
conf.d/_defaults.yaml                @platform-team
conf.d/db-a.yaml                     @db-a-team
rule-packs/rule-pack-mariadb.yaml    @dba-team
```

詳見 [GitOps 部署指南](integration/gitops-deployment.md) 的 tenant 自助設定範圍。

### API RBAC (v2.5.0+)

tenant-api 透過 `conf.d/_rbac.yaml` 控制 API 層級的讀寫權限。RBAC Manager 使用 `atomic.Value` 熱更新，檔案變更後無需重啟。

安全預設行為：若 `_rbac.yaml` 不存在或為空，系統進入 **open-read mode**（所有已認證使用者可讀，無人可寫）。

### RBAC 救援 SOP（Break-Glass Procedure）

當管理員不慎修改 `_rbac.yaml` 導致所有人（包括自己）喪失 API 寫入權限時，依照以下步驟恢復：

**情境 A：有 Git 寫入權限（推薦）**

```bash
# 1. 直接在 Git repo 中編輯 _rbac.yaml，恢復 admin 群組
git clone <repo-url> && cd <repo>
vi conf.d/_rbac.yaml   # 加回 admin 群組的 write/admin 權限

# 2. 提交並推送
git add conf.d/_rbac.yaml
git commit -m "fix: restore admin RBAC permissions (break-glass)"
git push

# 3. tenant-api 會透過 SHA-256 hot-reload 自動載入新配置（無需重啟）
```

**情境 B：無 Git 權限但有 K8s 存取**

```bash
# 直接編輯 ConfigMap（僅限緊急情況，事後須回寫 Git）
kubectl edit configmap tenant-config -n <namespace>
# 在 _rbac.yaml 段落中恢復 admin 群組
# 儲存後 tenant-api sidecar 自動 reload
```

**情境 C：完全刪除 `_rbac.yaml`**

```bash
# 刪除 _rbac.yaml 讓系統回到 open-read mode
# 所有已認證使用者可讀取，但無人可寫入
# 這是安全的「停損」操作——先恢復可見性，再重建權限
git rm conf.d/_rbac.yaml && git commit -m "emergency: remove RBAC to restore read access" && git push
```

**預防措施**：建議在 CI 中加入 `_rbac.yaml` 的 pre-merge 檢查——確認至少一個群組擁有 admin 權限，防止意外提交空權限配置。

### 配置驗證與合規

v1.7.0 起，`validate_config.py` 提供一站式配置驗證，涵蓋：

1. **YAML 格式驗證** — 語法正確性
2. **Schema 驗證** — Go `ValidateTenantKeys()` + Python `validate_tenant_keys()` 偵測未知/typo key（JSON Schema 定義見 [docs/schemas/](schemas/README.md)）
3. **路由驗證** — `generate_alertmanager_routes.py --validate` 檢查 receiver 結構 + domain allowlist
4. **Custom Rule Lint** — `lint_custom_rules.py` deny-list 合規檢查
5. **版號一致性** — `bump_docs.py --check` 確認三條版號線同步

```bash
# 一站式驗證（CI 可直接消費 JSON 輸出）
da-tools validate-config --config-dir conf.d/ --json
```

---

## 安全合規 (Security Compliance)

### SAST 自動化測試（7 條規則）

`tests/test_sast.py` 對 `scripts/tools/` 全部 Python 檔案進行 AST 層掃描，每次 commit 自動執行（426+ tests）。

| # | 規則 | 偵測方式 | 嚴重度 |
|---|------|---------|--------|
| 1 | `open()` 必須帶 `encoding="utf-8"` | AST 掃描 open() call，排除二進位模式 | High |
| 2 | `subprocess` 禁止 `shell=True` | AST 掃描 subprocess.run/call/Popen keywords | Critical |
| 3 | 寫入檔案需搭配 `os.chmod(0o600)` | 同函式內 write-open + chmod 配對（advisory） | Medium |
| 4 | 禁止 `yaml.load()`，強制 `yaml.safe_load()` | AST 掃描 yaml.load 缺少 SafeLoader | Critical |
| 5 | 禁止硬編碼機密（password/token/secret/api_key） | Regex 掃描，排除環境變數引用和 placeholder | High |
| 6 | 禁止危險函式（eval/exec/pickle.load/os.system） | AST 掃描內建函式 + 模組函式 | Critical |
| 7 | 禁止不安全的檔案操作（無異常處理的 pathlib 操作） | AST 掃描 Path.mkdir/unlink/rename 缺少 try-except | Medium |

### Go 元件安全

| 檢查 | 說明 |
|------|------|
| ReadHeaderTimeout (G112) | 防 Slowloris 攻擊，`http.Server` 必須設置（目前 3s） |
| 完整 Timeout 套件 | ReadTimeout 5s, WriteTimeout 10s, IdleTimeout 30s, MaxHeaderBytes 8192 |
| G113 | Uncontrolled memory consumption |
| G114 | 禁止使用 `http.Request.RequestURI`（不安全，用 URL.Path） |

### Python 型別系統規範

所有 `_lib_*.py` 子模組須加入完整型別提示（PEP 484），CI 透過 `mypy --strict` 驗證。新增工具應在 shared library 層補充型別，新工具檔若涉及檔案 I/O / HTTP 請求應標註返回型別。

### Python SSRF 保護

`_lib_python.py` 中的 `_validate_url_scheme()` 對所有 HTTP 請求做 URL scheme 白名單驗證（僅允許 http/https），搭配 timeout 限制。

### 機密管理 (Secret Management)

| 元件 | 機制 |
|------|------|
| MariaDB | K8s Secret (`mariadb-credentials`) + `.my.cnf` 掛載（`defaultMode: 0400`） |
| Grafana | K8s Secret (`grafana-credentials`) + `secretKeyRef` 引用 |
| Makefile `shell` target | `--defaults-file=/etc/mysql/credentials/.my.cnf`（不在指令中暴露密碼） |
| Helm values | 密碼預設為空字串，必須在安裝時提供：`--set mariadb.rootPassword=$(openssl rand -base64 24)` |

### Container 安全加固

所有容器遵循最小權限原則：

| 容器 | runAsNonRoot | readOnlyRootFilesystem | drop ALL caps | allowPrivilegeEscalation |
|------|:-----------:|:---------------------:|:-------------:|:------------------------:|
| threshold-exporter | ✓ | ✓ | ✓ | ✓ |
| Prometheus | ✓ | ✓ | ✓ | ✓ |
| Alertmanager | ✓ | ✓ | ✓ | ✓ |
| config-reloader | ✓ | ✓ | ✓ | ✓ |
| Grafana | ✓ | ✓ | ✓ | ✓ |
| MariaDB | — | — | ✓ | ✓ |
| mysqld-exporter | — | ✓ | ✓ | ✓ |
| kube-state-metrics | ✓ | ✓ | ✓ | ✓ |

所有 Pod 設定 `seccompProfile: RuntimeDefault`。Docker image 全部 pin 到具體 patch 版本。

### Container Image Security (v2.2.0 updated)

**三層防護策略：**

1. **Base image pin** — 所有 Dockerfile pin 到包含安全修補的特定 Alpine 版本，避免 floating tag 導致 CI cache 凍結在舊版
2. **Build-time upgrade** — `apk --no-cache upgrade` 在建置時拉取最新 point-release 修補
3. **Attack surface reduction** — da-portal 移除不需要的 library（libavif, gd, libxml2 等），threshold-exporter 使用 distroless（零 package manager）

| Image | Base | Pin 策略 | CVE 防護 |
|-------|------|---------|---------|
| threshold-exporter | `distroless/static-debian12:nonroot` | digest pin | 零 CVE：無 shell/apk/libc/openssl，Go 內建 crypto |
| da-tools | `python:3.13.3-alpine3.22` | patch+alpine pin | Alpine 3.22 修復 libavif + openssl；`apk upgrade` 補漏 |
| da-portal | `nginx:1.28.2-alpine3.23` | patch+alpine pin | Alpine 3.23 + `apk del` 移除 libavif/gd/libxml2 未使用 library |

**CI 掃描：** 每個 image push 後自動執行 Trivy 掃描（CRITICAL + HIGH），有已修復的高危漏洞時阻斷 release。見 `.github/workflows/release.yaml`。

**企業 Registry 建議：** 定期 rebuild（建議每月或 CVE 公告後 48h 內）。設定 Trivy/Grype 排程掃描已上架 image。

**CVE 追蹤紀錄：**

- **CVE-2025-15467 (openssl, CVSS 9.8)**：CMS AuthEnvelopedData stack buffer overflow → pre-auth RCE。影響 OpenSSL 3.0–3.6。修復：Alpine 3.22 含修補版 `libssl3`。threshold-exporter 不受影響（distroless + Go 內建 crypto）。
- **CVE-2025-48174 (libavif, CVSS 4.5–9.1)**：`makeRoom()` integer overflow → buffer overflow。影響 libavif < 1.3.0。修復：Alpine 3.22 ships libavif >= 1.3.0。da-portal 額外執行 `apk del libavif` 徹底移除（static file server 不需要圖片處理 library）。threshold-exporter 不受影響（distroless 無 libavif）。
- **CVE-2025-48175 (libavif, CVSS 4.5–9.1)**：`rgbRowBytes` 等乘法 integer overflow。與 CVE-2025-48174 同批修復（libavif >= 1.3.0）。
- **CVE-2026-1642 (nginx, CVSS 5.9)**：SSL upstream injection — MITM 可在 TLS handshake 前注入明文回應。影響 nginx < 1.28.2。修復：da-portal pin `nginx:1.28.0`（1.28 stable 已修復）。

### NetworkPolicy（Ingress + Egress）

Default deny-all（Ingress + Egress）+ 逐元件白名單：

| 元件 | Ingress 來源 | Egress 目的 |
|------|-------------|------------|
| Prometheus | monitoring namespace (9090) | tenant ns 9104/8080, Alertmanager 9093, kube-state-metrics, DNS, K8s API 6443 |
| Alertmanager | Prometheus (9093) | DNS, webhook HTTPS 443（封鎖 cloud metadata 169.254.169.254） |
| Grafana | monitoring namespace (3000) | Prometheus 9090, DNS |
| threshold-exporter | Prometheus (8080) | DNS only |
| kube-state-metrics | Prometheus (8080/8081) | K8s API 6443, DNS |
| tenant-api | 4180（oauth2-proxy，全叢集）；**8080 僅 Prometheus pod（`app=prometheus`）+ threshold-govern CronJob（`component=threshold-govern`）** — pod 級白名單 | Egress opt-in（`networkPolicy.egress.enabled`，預設 off）；啟用後 DNS + Prometheus 9090 + `extraEgress`（K8s API / git forge） |
| da-portal | 4180 + listenPort，僅 `allowedNamespaces`（monitoring + `ingress-nginx`〔values 預設；換成你的 ingress controller ns〕；**不含租戶 ns**） | — |

> ⛔ **8080 是 header-trust 面（GHSA-3g2h-rf85-5rrv）**：tenant-api 的 8080 埠刻意繞過 oauth2-proxy、盲信 `X-Forwarded-Groups` / `X-Forwarded-Email`。任何能連到 8080 的 pod 可主張任意身分（含 `platform-admins`）。因此 8080 的 NetworkPolicy 必須是 **pod 級**（只放行上表兩類 workload），且 **不可停用** — 兩個 chart 的 `networkPolicy.enabled=false` 已 codified 為 `helm template` 硬失敗（tenant-api 無條件；da-portal 於 `oauth2Proxy.enabled=true` 時）。tenant-api namespace 另掛 **default-deny-ingress**（`podSelector:{}`，僅 Ingress）補齊「非 tenant-api pod 進入該 ns 即全開」的缺口。da-portal 另有 render-time **open-proxy guard**：`oauth2Proxy.enabled=false` 時強制 `portal.tenantApiUrl` / `portal.recipePreviewUrl` 為空（否則 nginx 無 strip proxy 會把 client 原始 `X-Forwarded-Groups` 直送後端 = 未認證開放代理），此 guard 為 render-time、**不依賴 CNI**。

> ⚠️ **NetworkPolicy 需 CNI 支援才生效（避免安全劇場）**：上表所有 Ingress/Egress 白名單、8080 pod 級限制、default-deny-ingress **全部依賴叢集 CNI 實作 NetworkPolicy**。若 CNI 不支援（基礎版 Flannel、部分雲廠商預設簡易 CNI），K8s API server 仍會「接受」這些物件、`helm install` 也照樣成功，但流量**完全不受限** — 8080 header-trust 面對整個叢集敞開，形成 *security theater*（看似封鎖、實際全開）。**生產部署務必使用 NetworkPolicy-aware CNI（Calico / Cilium / Antrea）**，並以「實際送一個應被拒的封包」實測 enforcement，不可只看 `helm install` 成功。這也是為何根因層 **#5（L7：KSA OIDC + TokenReview 或內部身分簽章，與 CNI 無關）才是唯一真信任邊界**、而本節的 NetworkPolicy 縱深屬 **L4 stopgap** 的原因。

> ⚠️ **L4 涵蓋限制（即使 CNI 正確 enforce 仍有結構性繞過面）**：pod 級 NetworkPolicy 是止血、非信任邊界，以下兩類攻擊 L4 無法防，唯 **#5 的 L7 caller 身分驗證**能覆蓋：
> - **同 namespace label 偽造（lateral movement）**：`podSelector` 盲信 pod label。若 monitoring ns 內某 pod 取得 `patch` / `create pods` 的 RBAC（或某 operator 以使用者輸入為 label 生成物件），攻擊者可自貼 `app=prometheus` 騙過 8080 白名單，再注入 `X-Forwarded-Groups: platform-admins`。
> - **host-network 降維**：NetworkPolicy 只治理 pod netns。`hostNetwork: true` 的 pod（node-exporter / fluentd 等特權 DaemonSet）或被攻破的 node 本身，可從 host netns 直接向 tenant-api Pod IP:8080 發包，多數 CNI 對此 local routing 預設放行 → 繞過規則。

### Portal 安全標頭

`nginx.conf` 設定：X-Frame-Options (SAMEORIGIN), X-Content-Type-Options (nosniff), Referrer-Policy, Content-Security-Policy（限制 script/style/connect 來源）, Strict-Transport-Security (HSTS)。

---

> 本文件從 [`architecture-and-design.md`](architecture-and-design.md) 獨立拆分。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [Custom Rule Governance](./custom-rule-governance.md) | 規則治理模型 |
| [規則生命週期治理（§7 全 tier 視圖）](./custom-rule-governance.md) | 規則生→老→病→死的橫切治理 + 成熟度限制（遷入評估必讀） |
| [GitOps Deployment](integration/gitops-deployment.md) | 部署安全、RBAC |
| [Testing Playbook](./internal/testing-playbook.md) | SAST 測試執行 |
| [跨租戶 ConfigMap 硬化](./cross-tenant-configmap-hardening.md) | operator RBAC 收斂基線 + off-cluster 稽核（PCI Req 7/10 · SOC 2 CC6/CC7）|
