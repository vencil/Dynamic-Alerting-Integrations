---
title: "場景：多域名階層式配置 — conf.d/ 目錄結構重構（v2.7.0）"
tags: [scenario, configuration, conf.d, hierarchy, multi-domain]
audience: [platform-engineer, operator, devops]
version: v2.7.0
lang: zh
---
# 場景：多域名階層式配置 — conf.d/ 目錄結構重構（v2.7.0）

> **Language / 語言：** **中文 (Current)** | [English](./multi-domain-conf-layout.en.md)

> **v2.7.0** | 相關文件：[ADR-017（架構決策）](../adr/017-conf-d-directory-hierarchy-mixed-mode.md)、[ADR-018（繼承機制）](../adr/018-defaults-yaml-inheritance-dual-hash.md)

## 背景與問題

### 為什麼需要階層式結構？

在平台成長過程中，tenants 數量從數十增至數百。原有的**平面結構**（所有 tenant 配置檔直接放在 `conf.d/`）面臨三個核心問題：

| 問題 | 影響 | 優先級 |
|------|------|--------|
| **檔案爆炸** | 300+ tenant YAML 檔全混在一起，難以定位單一 tenant | ⭐⭐⭐ |
| **跨域名配置重複** | Finance、Infra、Ops 三個業務域各有重複的預設值、告警閾值、receiver 設定 | ⭐⭐⭐ |
| **區域合規政策** | 歐盟 GDPR 要求 EU 資料在 eu-west，美國 SOC2 要求 US 資料在 us-east，現有結構無法表達 | ⭐⭐⭐ |
| **存取控制邊界** | Infra 團隊不應該看到 Finance tenant 配置；Finance DevOps 不該改到 Ops 的預設值 | ⭐⭐ |

### 平面結構的極限

```yaml
# 舊：conf.d/ 平面結構
conf.d/
├── tenant-finance-a.yaml          # Finance，US-East，Prod
├── tenant-finance-b.yaml          # Finance，US-East，Staging
├── tenant-finance-c.yaml          # Finance，EU-West，Prod
├── tenant-infra-d.yaml            # Infra，US-East，Prod
├── tenant-ops-e.yaml              # Ops，Global，Prod
├── ... (300+ 個檔案混在一起)
```

**痛點**：

1. 找到 Finance 的所有 tenant 需要 `grep` 檔案名
2. Finance、Infra、Ops 各自維護一份預設值副本 → 無法同步
3. 無法清楚表達「EU-West 預設值」或「Staging 預設值」的概念
4. RBAC 無法按域名+區域分配存取權

## 解決方案：階層式配置設計

### 目錄結構

```yaml
conf.d/
├── _defaults.yaml                        # 全局預設值（所有 tenant 繼承）
│
├── finance/
│   ├── _defaults.yaml                    # Finance 域預設值
│   │   # 覆蓋全局預設，新增 Finance 特定的告警閾值、receiver、RBAC 策略
│   │
│   ├── us-east/
│   │   ├── _defaults.yaml                # Finance US-East 區域預設（例如 timezone, webhook 域名）
│   │   ├── prod/
│   │   │   ├── tenant-a.yaml             # Finance, US-East, Prod
│   │   │   └── tenant-b.yaml
│   │   └── staging/
│   │       └── tenant-c.yaml             # Finance, US-East, Staging
│   │
│   └── eu-west/
│       ├── _defaults.yaml                # Finance EU-West 區域預設（GDPR 政策、簽名金鑰）
│       └── prod/
│           └── tenant-d.yaml             # Finance, EU-West, Prod
│
├── infra/
│   ├── _defaults.yaml                    # Infra 域預設值
│   └── prod/
│       └── tenant-e.yaml                 # Infra, Prod (無區域分隔)
│
└── ops/
    └── tenant-f.yaml                     # Ops tenant (純平面，無域名結構)
```

### 目錄語義

| 層級 | 含義 | 例 | 責任方 |
|------|------|-----|--------|
| `conf.d/_defaults.yaml` | 全局預設（signature algo, global receiver, 基礎 routing rule） | 所有 tenant 繼承 | Platform Admin |
| `conf.d/<domain>/_defaults.yaml` | 業務域預設（Finance/Infra/Ops 特定的告警閾值、owner） | Finance 所有 tenant 繼承 | Domain Lead |
| `conf.d/<domain>/<region>/_defaults.yaml` | 區域預設（timezone, compliance 政策, 地區 webhook） | Finance US-East tenant 繼承 | Regional Ops |
| `conf.d/<domain>/<region>/<env>/tenant-*.yaml` | 單一 tenant 配置（僅此 tenant 特有覆蓋） | 無（全部繼承） | Tenant Owner |

## 繼承與覆蓋 (Inheritance & Merge)

### 繼承鏈

每個 tenant 配置檔的 **有效配置** 是以下層級的**深度合併** (deep merge)：

```
全局預設 ← 域預設 ← 區域預設 ← 環境預設 ← Tenant 配置
```

例如 `finance/us-east/prod/tenant-a.yaml`：

```
有效配置 = merge(
  conf.d/_defaults.yaml,              # 層級 1
  conf.d/finance/_defaults.yaml,      # 層級 2
  conf.d/finance/us-east/_defaults.yaml,  # 層級 3
  conf.d/finance/us-east/prod/_defaults.yaml,  # (若存在) 層級 4
  conf.d/finance/us-east/prod/tenant-a.yaml   # 層級 5
)
```

### 深度合併語義

- **物件級** (dict)：遞迴合併，子鍵覆蓋父鍵
- **陣列級** (list)：子陣列替代父陣列（不是追加）
- **null 值**：表示「顯式 opt-out」——忽略上層值
  
例：

```yaml
# 層級 2：conf.d/finance/_defaults.yaml
tenants:
  "_defaults":
    alerts:
      threshold:
        MariaDBHighConnections: 90
        DiskUsageHigh: 85
    receivers:
      - name: finance-channel
        type: slack

# 層級 5：conf.d/finance/us-east/prod/tenant-a.yaml
tenants:
  tenant-a:
    alerts:
      threshold:
        MariaDBHighConnections: 95      # 覆蓋：從 90 提高到 95
        # DiskUsageHigh 未提，繼承 85
    receivers:
      - name: finance-channel           # 替代整個陣列（如有需要加新 receiver，須明列 finance-channel）
      - name: custom-webhook
        type: http
```

### Null 值 Opt-Out（進階）

若 tenant-a 想「停用 Finance 域的 finance-channel receiver」：

```yaml
# conf.d/finance/us-east/prod/tenant-a.yaml
tenants:
  tenant-a:
    receivers: null    # 顯式 opt-out：不繼承 Finance 域預設的 receivers
    # 或明列新 receivers
    receivers:
      - name: custom-webhook
        type: http
```

## 操作指南

### 情景 1：從平面遷移到階層式

**前置**：確認現有 `conf.d/*.yaml` 結構

#### 步驟 A：乾跑 (Dry Run)

```bash
da-tools migrate-conf-d --dry-run \
  --input-layout flat \
  --output-layout hierarchical \
  --domain-map finance:db,ops:ops,infra:infra
```

輸出範例：

```
[DRY RUN] Processing 250 tenants...

Would move:
  conf.d/db-a.yaml → conf.d/finance/us-east/prod/tenant-a.yaml
  conf.d/db-c.yaml → conf.d/finance/eu-west/prod/tenant-c.yaml
  conf.d/ops-e.yaml → conf.d/ops/tenant-e.yaml

Would extract domain defaults into:
  conf.d/finance/_defaults.yaml (common keys: alerts.threshold.MariaDBHighConnections, receivers)
  conf.d/infra/_defaults.yaml

No changes made. Rerun with --apply to proceed.
```

#### 步驟 B：應用 (Apply)

```bash
da-tools migrate-conf-d --apply \
  --input-layout flat \
  --output-layout hierarchical \
  --domain-map finance:db,ops:ops,infra:infra
```

工具會自動：

1. 掃描所有 tenant，按前綴提取域名
2. 按 tenant 內的 `region` / `environment` 標籤分組
3. 萃取共同鍵值到各層 `_defaults.yaml`
4. 移動 tenant 檔案到新目錄結構
5. 執行 `validate-conf-d` 確保遷移成功

#### 步驟 C：驗證

```bash
# 逐 tenant 檢查繼承鏈
da-tools describe-tenant --name tenant-a --show-sources

# 輸出
tenant-a (finance/us-east/prod/tenant-a.yaml)
═════════════════════════════════════════════
Configuration sources (order of merge):
  1. conf.d/_defaults.yaml (global)
  2. conf.d/finance/_defaults.yaml (domain: finance)
  3. conf.d/finance/us-east/_defaults.yaml (region: us-east)
  4. conf.d/finance/us-east/prod/_defaults.yaml (environment: prod)
  5. conf.d/finance/us-east/prod/tenant-a.yaml (tenant-specific)

Effective configuration:
  alerts.threshold.MariaDBHighConnections: 90 (from: domain)
  receivers[0].type: slack (from: global)
  timezone: America/New_York (from: region)
  ...
```

### 情景 2：加入新 Tenant（hierarchy-ready）

```bash
# 1. 建立目錄結構（若不存在）
mkdir -p conf.d/finance/ap-south/prod

# 2. 建立 tenant 配置
cat > conf.d/finance/ap-south/prod/tenant-new.yaml << 'EOF'
tenants:
  tenant-new:
    _routing:
      receiver:
        type: slack
        api_url: https://hooks.slack.com/...
        channel: "#new-alerts"
    # 其他 tenant-specific 設定
EOF

# 3. 驗證（自動應用繼承）
da-tools describe-tenant --name tenant-new --show-sources
```

系統自動尋找：

- `conf.d/finance/ap-south/prod/_defaults.yaml` (若不存在，跳過)
- `conf.d/finance/ap-south/_defaults.yaml` (若不存在，跳過)
- `conf.d/finance/_defaults.yaml`
- `conf.d/_defaults.yaml`

### 情景 3：更新區域預設（bulk）

例：所有 EU-West tenant 需要 GDPR 模式簽名

```bash
cat > conf.d/finance/eu-west/_defaults.yaml << 'EOF'
tenants:
  "_defaults":
    _signature:
      algorithm: sha256
      mode: gdpr-compatible  # 歐盟合規簽名
    _encryption:
      enabled: true
      key_rotation_days: 90
EOF

# 驗證：所有 eu-west 下的 tenant 已生效
da-tools validate-conf-d --report-inheritance --filter "region=eu-west"
```

### 情景 4：混合模式（平面 + 階層）

遷移可以**漸進式進行**，新 tenant 用階層式，舊 tenant 保持平面：

```bash
conf.d/
├── _defaults.yaml
├── finance/                        # ← 新域名結構
│   ├── _defaults.yaml
│   └── us-east/prod/tenant-a.yaml
├── tenant-legacy-b.yaml            # ← 舊平面（仍支援）
└── ops/
    ├── _defaults.yaml
    └── tenant-e.yaml
```

系統同時支援：

- 純平面檔名：`conf.d/tenant-*.yaml`
- 階層式路徑：`conf.d/<domain>/.../<env>/tenant-*.yaml`
- 域名目錄但平面檔：`conf.d/<domain>/tenant-*.yaml`

## 工具支援

### 核心工具

| 工具 | 用途 | 版本 |
|------|------|------|
| `migrate-conf-d` | 平面→階層遷移，乾跑/應用 | v2.7.0+ |
| `describe-tenant` | 顯示 tenant 有效配置 + 繼承鏈 | v2.7.0+ |
| `validate-conf-d` | 檢查配置正確性、重複、衝突 | v2.7.0+ |
| `list-tenants` | 列舉所有 tenant + 所屬域/區/環 | v2.7.0+ |

### 使用範例

```bash
# 1. 快速檢查某 tenant 的有效值
da-tools describe-tenant --name tenant-a --key alerts.threshold

# 2. 找到所有 Finance tenant
da-tools list-tenants --filter domain=finance

# 3. 驗證配置無誤
da-tools validate-conf-d --check-merge-conflicts

# 4. 生成 configuration report（用於審計）
da-tools describe-tenant --generate-report --format json --output audit.json
```

## 注意事項

### ✅ 支援的特性

- ✅ 任意深度的目錄巢狀（不限 3 層）
- ✅ Env 變數在 `_defaults.yaml` 中（如 `{{ env.REGION }}`）
- ✅ 版本控制跟蹤（`.git-blame` 顯示哪層檔案做的修改）
- ✅ 反向相容：舊平面檔案仍可用

### ⚠️ 限制與陷阱

1. **檔案名約定**：`_defaults.yaml` 是保留字，不能當作 tenant 名稱
2. **循環繼承**：系統檢測並防止（`validate-conf-d` 會報錯）
3. **陣列合併**：只支援替代，不支援追加。若需追加新 receiver，須完整列出舊的
4. **環境變數逃逸**：`_defaults.yaml` 中的 env 變數僅在該檔案有效，tenant 檔案內不可引用

### 🛡️ 已自動化的檢查

- Pre-commit hook：禁止 `_defaults.yaml` 含有 hardcoded tenant id
- 配置驗證：檢測重複 receiver、未定義的 rule group 參考
- Git hook：對 `conf.d/` 的修改自動執行 `validate-conf-d` + `describe-tenant` 檢查

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [ADR-017：階層式 conf.d 設計決策](../adr/017-conf-d-directory-hierarchy-mixed-mode.md) | ⭐⭐⭐ |
| [ADR-018：繼承機制與雙重雜湊](../adr/018-defaults-yaml-inheritance-dual-hash.md) | ⭐⭐⭐ |
| [`da-tools` CLI 參考](../cli-reference.md) | ⭐⭐ |
| ["場景：租戶完整生命週期管理"](tenant-lifecycle.md) | ⭐⭐ |
| ["場景：多叢集聯邦架構"](multi-cluster-federation.md) | ⭐ |
