---
title: "治理、稽核與安全合規"
tags: [governance, security, audit]
audience: [platform-engineer, security]
version: v2.4.0
lang: zh
---
# 治理、稽核與安全合規

> **Language / 語言：** | **中文（當前）**

> 相關文件：[Architecture](architecture-and-design.md) · [GitOps Deployment](gitops-deployment.md) · [Custom Rule Governance](custom-rule-governance.md)

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

詳見 [GitOps 部署指南](gitops-deployment.md) 的 tenant 自助設定範圍。

### 配置驗證與合規

v1.7.0 起，`validate_config.py` 提供一站式配置驗證，涵蓋：

1. **YAML 格式驗證** — 語法正確性
2. **Schema 驗證** — Go `ValidateTenantKeys()` + Python `validate_tenant_keys()` 偵測未知/typo key
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

### Container Image Security（v2.2.0 updated）

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

### Portal 安全標頭

`nginx.conf` 設定：X-Frame-Options (SAMEORIGIN), X-Content-Type-Options (nosniff), Referrer-Policy, Content-Security-Policy（限制 script/style/connect 來源）, Strict-Transport-Security (HSTS)。

---

> 本文件從 [`architecture-and-design.md`](architecture-and-design.md) 獨立拆分。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [Custom Rule Governance](./custom-rule-governance.md) | 規則治理模型 |
| [GitOps Deployment](./gitops-deployment.md) | 部署安全、RBAC |
| [Testing Playbook](./internal/testing-playbook.md) | SAST 測試執行 |
