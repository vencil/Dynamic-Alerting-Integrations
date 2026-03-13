---
title: "治理、稽核與安全合規"
tags: [governance, security, audit]
audience: [platform-engineer, security]
version: v1.13.0
lang: zh
---
# 治理、稽核與安全合規

> **Language / 語言：** [English](governance-security.en.md) | **中文（當前）**

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

## 安全合規 (Security Compliance — SAST)

### Go 元件安全

#### ReadHeaderTimeout (Gosec G112 — Slowloris)
```go
// ✓ 正確
server := &http.Server{
    Addr:              ":8080",
    Handler:           mux,
    ReadHeaderTimeout: 10 * time.Second,  // 必須設置
}

// ✗ 違反
server := &http.Server{
    Addr:    ":8080",
    Handler: mux,
    // 無 ReadHeaderTimeout → Slowloris 攻擊風險
}
```

**為什麼：** 防止客戶端傳送緩慢的 HTTP 標頭，耗盡伺服器資源

#### 其他檢查
- **G113** — Potential uncontrolled memory consumption
- **G114** — Use of `http.Request.RequestURI` (不安全，用 URL.Path)

### Python 元件安全

#### 檔案權限 (CWE-276)
```python
# ✓ 正確
with open(path, 'w') as f:
    f.write(config_content)
os.chmod(path, 0o600)  # rw-------

# ✗ 違反
# 預設檔案權限 0o644 (rw-r--r--) → 其他使用者可讀
```

#### 無 Shell 注入 (Command Injection)
```python
# ✓ 正確
result = subprocess.run(['kubectl', 'patch', 'configmap', ...], check=True)

# ✗ 違反
result = os.system(f"kubectl patch configmap {name}")  # shell=True 風險
```

### SSRF 保護

所有本地 API 呼叫註記為 `# nosec B602`：

```python
# nosec B602 — localhost-only, no SSRF risk
response = requests.get('http://localhost:8080/health')
```

---

> 本文件從 [`architecture-and-design.md`](architecture-and-design.md) 獨立拆分。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Governance, Audit & Security Compliance"](./governance-security.en.md) | ⭐⭐⭐ |
| ["多租戶客製化規則治理規範 (Custom Rule Governance Model)"](./custom-rule-governance.md) | ⭐⭐ |
