---
title: "專案 Context 圖：角色、工具與產品互動關係"
tags: [architecture, context-diagram]
audience: [all]
version: v2.0.0-preview.3
lang: zh
---
# 專案 Context 圖：角色、工具與產品互動關係

> **Language / 語言：** | **中文（當前）**

> **v2.0.0-preview** | 適用對象：所有參與者（Platform Engineers、Domain Experts、Tenant Teams）

## 簡介

本文件透過 Context 圖（C4 模型）呈現多租戶動態警報平台的**角色分工、工具使用流程、與產品基礎設施的互動關係**。

**核心概念：**
- **三層角色分工**：Platform Engineer（平台層）、Domain Expert（專業領域）、Tenant Team（租戶層）
- **21 個支援工具**：涵蓋 Onboarding、Daily Ops、Migration、Governance 四大工作流
- **五類基礎設施**：Config、Compute、Evaluation、Routing、Notification

此圖幫助新加入者快速理解：
1. 我在這個系統中的角色是什麼？
2. 我應該使用哪些工具？
3. 我的工作成果如何影響下游系統？

---

## 1. 整體 Context 圖

```mermaid
graph TB
    subgraph Roles["🏢 三層角色分工"]
        PE["Platform Engineer<br/>平台工程師"]
        DX["Domain Expert<br/>領域專家 (DBA/SRE)"]
        TT["Tenant Team<br/>租戶團隊 (SRE/DBA)"]
    end

    subgraph Config["📋 配置層"]
        Defaults["_defaults.yaml<br/>平台級預設"]
        Profiles["_profiles.yaml<br/>四層繼承鏈"]
        RulePacks["Rule Packs<br/>(15個YAML)"]
        TenantYAML["tenant YAML<br/>(conf.d/)"]
    end

    subgraph Tools["🛠 工具生態（21個）"]
        subgraph OnboardTools["Onboarding"]
            ScaffoldTool["scaffold_tenant.py"]
            OnboardAnalyze["onboard_platform.py"]
        end

        subgraph OpsTools["Daily Ops"]
            DiagnoseTool["diagnose.py"]
            BatchDiag["batch_diagnose.py"]
            CheckAlert["check_alert.py"]
            PatchConfig["patch_config.py"]
        end

        subgraph MigTools["Migration"]
            MigrateRule["migrate_rule.py"]
            ValidateMig["validate_migration.py"]
            CutoverTool["cutover_tenant.py"]
        end

        subgraph GovTools["Governance"]
            ValidateConfig["validate_config.py"]
            ConfigDiff["config_diff.py"]
            LintRules["lint_custom_rules.py"]
            DeprecateRule["deprecate_rule.py"]
            OffboardTool["offboard_tenant.py"]
        end

        subgraph AdvTools["Advanced"]
            BlindSpot["blind_spot_discovery.py"]
            BaselineDisc["baseline_discovery.py"]
            BacktestThresh["backtest_threshold.py"]
            MaintScheduler["maintenance_scheduler.py"]
            BumpDocs["bump_docs.py"]
            GenerateRoutes["generate_alertmanager_routes.py"]
        end
    end

    subgraph Infra["🏗 基礎設施產品"]
        subgraph ConfigInfra["Config (ConfigMap)"]
            ThreshConfig["threshold-config"]
        end

        subgraph ComputeInfra["Compute"]
            Exporter["threshold-exporter ×2<br/>(HA, 8080)"]
        end

        subgraph EvalInfra["Evaluation"]
            Prom["Prometheus<br/>+ 15 Rule Packs"]
        end

        subgraph RoutingInfra["Routing & Alert"]
            AlertMgr["Alertmanager<br/>(Dynamic routing)"]
        end

        subgraph NotifInfra["Notification"]
            Channels["Slack | PagerDuty<br/>Email | Teams<br/>RocketChat | Webhook"]
        end
    end

    subgraph GitOps["📂 GitOps層"]
        GitRepo["Git Repository<br/>(conf.d/ + rule-packs/)"]
    end

    %% Role -> Config Management
    PE -->|管理| Defaults
    PE -->|管理| Profiles
    PE -->|維護| RulePacks
    DX -->|貢獻| RulePacks
    DX -->|維護| TenantYAML
    TT -->|管理| TenantYAML

    %% Config -> GitOps
    Defaults -->|PR| GitRepo
    Profiles -->|PR| GitRepo
    RulePacks -->|PR| GitRepo
    TenantYAML -->|PR| GitRepo

    %% Role -> Tools (Examples)
    PE -->|使用| ValidateConfig
    PE -->|使用| ConfigDiff
    PE -->|使用| GenerateRoutes
    DX -->|使用| LintRules
    DX -->|使用| MigrateRule
    TT -->|使用| ScaffoldTool
    TT -->|使用| DiagnoseTool
    TT -->|使用| CheckAlert

    %% Tools -> Config
    ScaffoldTool -->|生成| TenantYAML
    OnboardAnalyze -->|分析| Defaults
    MigrateRule -->|轉換| TenantYAML
    GenerateRoutes -->|消費| TenantYAML

    %% Tools -> Infra
    ValidateConfig -->|驗證| ThreshConfig
    PatchConfig -->|更新| ThreshConfig
    DiagnoseTool -->|查詢| Prom
    CheckAlert -->|查詢| AlertMgr
    ConfigDiff -->|比對| GitRepo

    %% GitOps -> Infra
    GitRepo -->|GitOps Sync<br/>(ArgoCD/Flux)| ThreshConfig

    %% Infrastructure Flow
    ThreshConfig -->|SHA-256<br/>hot-reload| Exporter
    Exporter -->|Metrics :8080| Prom
    Prom -->|Alert Rules<br/>Evaluation| AlertMgr
    AlertMgr -->|Routes & Groups| Channels

    %% Advanced tools
    BlindSpot -->|掃描| Prom
    BaselineDisc -->|觀測| Prom
    BacktestThresh -->|回測| Prom
    MaintScheduler -->|建立| AlertMgr
    BumpDocs -->|版號一致| GitRepo

    %% Styling
    classDef roleStyle fill:#e3f2fd,stroke:#1976d2,stroke-width:2px,color:#000
    classDef configStyle fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px,color:#000
    classDef toolStyle fill:#e8f5e9,stroke:#388e3c,stroke-width:2px,color:#000
    classDef infraStyle fill:#fff3e0,stroke:#e65100,stroke-width:2px,color:#000
    classDef gitStyle fill:#f0f0f0,stroke:#616161,stroke-width:2px,color:#000

    class PE,DX,TT roleStyle
    class Defaults,Profiles,RulePacks,TenantYAML configStyle
    class ScaffoldTool,OnboardAnalyze,DiagnoseTool,BatchDiag,CheckAlert,PatchConfig,MigrateRule,ValidateMig,CutoverTool,ValidateConfig,ConfigDiff,LintRules,DeprecateRule,OffboardTool,BlindSpot,BaselineDisc,BacktestThresh,MaintScheduler,BumpDocs,GenerateRoutes toolStyle
    class ThreshConfig,Exporter,Prom,AlertMgr,Channels infraStyle
    class GitRepo gitStyle
```

---

## 2. Onboarding 工作流詳圖

```mermaid
graph LR
    subgraph Input["輸入"]
        Existing["既有配置<br/>(Prometheus rules)"]
    end

    subgraph Process["Onboarding 工具組"]
        Analyze["onboard_platform.py<br/>反向分析"]
        Scaffold["scaffold_tenant.py<br/>互動式產生"]
    end

    subgraph Output["輸出"]
        Hints["onboard-hints.json<br/>分析結果"]
        Config["tenant YAML<br/>初始配置"]
    end

    subgraph Validation["驗證"]
        Validate["validate_config.py<br/>一站式檢查"]
    end

    subgraph Deployment["部署"]
        GitPush["Git PR<br/>conf.d/"]
        GitOps["GitOps Sync<br/>threshold-config"]
    end

    Existing -->|掃描| Analyze
    Analyze -->|輸出| Hints
    Hints -->|導入| Scaffold
    Scaffold -->|生成| Config
    Config -->|驗證| Validate
    Validate -->|通過| GitPush
    GitPush -->|Merge| GitOps

    classDef processStyle fill:#c8e6c9,stroke:#388e3c,color:#000
    classDef outputStyle fill:#bbdefb,stroke:#1976d2,color:#000
    classDef validationStyle fill:#ffe0b2,stroke:#e65100,color:#000

    class Analyze,Scaffold processStyle
    class Hints,Config outputStyle
    class Validate validationStyle
```

---

## 3. Daily Ops 工作流詳圖

```mermaid
graph TD
    subgraph Config["配置管理"]
        TenantYAML["tenant YAML<br/>(閾值、路由、三態)"]
    end

    subgraph Ops["日常運營工具"]
        Diagnose["diagnose.py<br/>單租戶健康檢查"]
        BatchDiag["batch_diagnose.py<br/>多租戶並行報告"]
        CheckAlert["check_alert.py<br/>Alert 狀態查詢"]
        PatchConfig["patch_config.py<br/>局部更新 + diff"]
    end

    subgraph Query["查詢對象"]
        Prom["Prometheus<br/>(metrics/targets)"]
        AlertMgr["Alertmanager<br/>(active alerts)"]
        ConfigMap["threshold-config<br/>(current state)"]
    end

    subgraph Output["診斷輸出"]
        Report["健康報告<br/>結構化 JSON"]
        Status["Alert 狀態<br/>分組統計"]
        Preview["ConfigMap diff<br/>預覽"]
    end

    TenantYAML -->|修改| PatchConfig
    PatchConfig -->|更新| ConfigMap
    ConfigMap -->|查詢| Prom

    Diagnose -->|查詢| Prom
    Diagnose -->|查詢| AlertMgr
    Diagnose -->|輸出| Report

    BatchDiag -->|並行執行| Diagnose

    CheckAlert -->|查詢| AlertMgr
    CheckAlert -->|輸出| Status

    PatchConfig -->|預覽| Preview

    classDef opsToolStyle fill:#a5d6a7,stroke:#388e3c,color:#000
    classDef queryStyle fill:#ffcc80,stroke:#f57c00,color:#000

    class Diagnose,BatchDiag,CheckAlert,PatchConfig opsToolStyle
    class Prom,AlertMgr,ConfigMap queryStyle
```

---

## 4. Migration 工作流詳圖

```mermaid
graph LR
    subgraph Legacy["舊系統"]
        OldRules["傳統 PromQL<br/>Alert Rules"]
    end

    subgraph Analysis["AST 分析"]
        MigrateTool["migrate_rule.py<br/>(Triage + Prefix)"]
    end

    subgraph Conversion["轉換輸出"]
        NewConfig["新 Rule Pack<br/>YAML"]
        CustomRules["Custom Rules<br/>(無覆蓋的規則)"]
    end

    subgraph Validation["驗證與回測"]
        ValidateMig["validate_migration.py<br/>(Shadow Monitoring)"]
        BacktestThresh["backtest_threshold.py<br/>(Prometheus 7d replay)"]
    end

    subgraph Cutover["切換"]
        CutoverTool["cutover_tenant.py<br/>(一鍵自動化)"]
    end

    OldRules -->|輸入| MigrateTool
    MigrateTool -->|輸出| NewConfig
    MigrateTool -->|輸出| CustomRules
    NewConfig -->|驗證| ValidateMig
    CustomRules -->|回測| BacktestThresh
    ValidateMig -->|確認| CutoverTool
    BacktestThresh -->|確認| CutoverTool

    classDef migToolStyle fill:#ce93d8,stroke:#7b1fa2,color:#000
    classDef validStyle fill:#ffcc80,stroke:#f57c00,color:#000

    class MigrateTool migToolStyle
    class ValidateMig,BacktestThresh validStyle
```

---

## 5. Governance 工作流詳圖

```mermaid
graph TD
    subgraph Input["輸入（待審批）"]
        RulePR["Rule Pack PR<br/>(新規則)"]
        ConfigPR["Config PR<br/>(閾值變更)"]
    end

    subgraph CheckTools["檢查工具"]
        ValidateAll["validate_config.py<br/>→ YAML/schema/routes"]
        ConfigDiffTool["config_diff.py<br/>→ Blast Radius"]
        LintTool["lint_custom_rules.py<br/>→ Custom Rule 治理"]
    end

    subgraph Review["Review 與決策"]
        CODEOWNERS["CODEOWNERS<br/>Role-Based"]
        Decision["通過/拒絕<br/>merge"]
    end

    subgraph Lifecycle["生命週期"]
        Deprecate["deprecate_rule.py<br/>標記下架"]
        Offboard["offboard_tenant.py<br/>清理下架租戶"]
    end

    RulePR -->|CI Check| ValidateAll
    ConfigPR -->|Blast Radius| ConfigDiffTool
    ConfigPR -->|Custom Rule Check| LintTool

    ValidateAll -->|結果| CODEOWNERS
    ConfigDiffTool -->|結果| CODEOWNERS
    LintTool -->|結果| CODEOWNERS

    CODEOWNERS -->|審批| Decision

    Decision -->|規則下架| Deprecate
    Decision -->|租戶下架| Offboard

    classDef checkStyle fill:#90caf9,stroke:#1976d2,color:#000
    classDef reviewStyle fill:#f8bbd0,stroke:#c2185b,color:#000

    class ValidateAll,ConfigDiffTool,LintTool checkStyle
    class CODEOWNERS,Decision reviewStyle
```

---

## 6. 角色與工具對應表

| 角色 | 主責 | 核心工具 | 偶用工具 |
|------|------|---------|---------|
| **Platform Engineer** | 平台級配置、Rule Pack 維護、基礎設施 | `validate_config.py`<br/>`generate_alertmanager_routes.py`<br/>`config_diff.py` | `bump_docs.py`<br/>`maintenance_scheduler.py` |
| **Domain Expert (DBA/SRE)** | 特定 Rule Pack、metric dictionary、governance | `lint_custom_rules.py`<br/>`migrate_rule.py`<br/>`deprecate_rule.py` | `validate_config.py`<br/>`backtest_threshold.py` |
| **Tenant Team (SRE/DBA)** | 租戶配置、閾值、路由、三態、metadata | `scaffold_tenant.py`<br/>`diagnose.py`<br/>`check_alert.py` | `validate_migration.py`<br/>`offboard_tenant.py`<br/>`patch_config.py` |

---

## 7. 工具按工作流分類表

| 工作流 | 階段 | 工具 | 輸入 | 輸出 | 用時 |
|--------|------|------|------|------|------|
| **Onboarding** | Analysis | `onboard_platform.py` | Prometheus rules | `onboard-hints.json` | 1–2 min |
| | Generation | `scaffold_tenant.py` | `--from-onboard` / 互動 | `tenant.yaml` | 2–5 min |
| | Validation | `validate_config.py` | `tenant.yaml` | 驗證報告 | 10–30 sec |
| **Daily Ops** | Health Check | `diagnose.py` | Tenant ID | 結構化報告 | 5–10 sec |
| | Batch Report | `batch_diagnose.py` | Namespace | 多租戶 CSV | 30–60 sec |
| | Alert Query | `check_alert.py` | Filter (alertname/labels) | JSON 結果 | 2–5 sec |
| | Config Update | `patch_config.py` | ConfigMap name, key, value | 更新 + diff preview | 5 sec |
| **Migration** | Rule Conversion | `migrate_rule.py` | 舊 PromQL | 新 YAML + custom rules | 10–30 sec |
| | Shadow Validation | `validate_migration.py` | 舊 rule + 新 rule | Diff 報告 + convergence | 2–5 min |
| | Threshold Backtest | `backtest_threshold.py` | Metric + threshold + days | 歷史命中統計 | 30–120 sec |
| | Cutover | `cutover_tenant.py` | Tenant config | 全自動切換（§7.1） | 5–10 min |
| **Governance** | Config Validation | `validate_config.py` | YAML 檔 | Multi-check 報告 | 10–30 sec |
| | Blast Radius | `config_diff.py` | Old dir + new dir | 差異 + impact report | 5–10 sec |
| | Rule Linting | `lint_custom_rules.py` | Custom rule YAML | 合規報告 | 5 sec |
| | Rule Deprecation | `deprecate_rule.py` | Rule name + end date | Migration 提示 + silence config | 1–2 sec |
| | Tenant Offboarding | `offboard_tenant.py` | Tenant ID + reason | 清理 + 審計日誌 | 30–60 sec |
| **Advanced** | Blind Spot Scan | `blind_spot_discovery.py` | Cluster targets | Unmonitored 清單 | 10–30 sec |
| | Baseline Discovery | `baseline_discovery.py` | Metric pattern + period | 閾值建議表 | 1–3 min |
| | Version Management | `bump_docs.py` | Platform/Exporter/Tools 版號 | 更新 CHANGELOG + docs | 5 sec |
| | AM Route Generation | `generate_alertmanager_routes.py` | Tenant YAML | Alertmanager fragment | 1–2 sec |
| | Maintenance Scheduling | `maintenance_scheduler.py` | Cron + duration | AlertManager silence CronJob | 10 sec |

---

## 8. 配置與基礎設施互動

```mermaid
graph LR
    subgraph ConfigLayer["📋 配置層"]
        Defaults["_defaults.yaml"]
        Profiles["_profiles.yaml<br/>(四層鏈)"]
        RulePacks["rule-pack-*.yaml<br/>(15個)"]
        TenantYAML["<tenant>.yaml<br/>(conf.d/)"]
    end

    subgraph GitOpsLayer["📂 GitOps層"]
        GitRepo["Git Repository<br/>conf.d/ + rule-packs/"]
    end

    subgraph PlatformLayer["🏗 Platform層"]
        CM["ConfigMap<br/>threshold-config"]
    end

    subgraph ComputeLayer["💻 Compute層"]
        Exp1["threshold-exporter #1<br/>(port 8080)"]
        Exp2["threshold-exporter #2<br/>(port 8080)"]
    end

    subgraph EvalLayer["📊 Evaluation層"]
        PV["Projected Volume<br/>(15 Rule Packs)"]
        Prom["Prometheus<br/>(3h + SHA-256)"]
    end

    subgraph RoutingLayer["🔀 Routing層"]
        RuleGen["generate_alertmanager_routes.py"]
        AM["Alertmanager<br/>(Dynamic routing)"]
    end

    subgraph NotifLayer["📢 Notification層"]
        Slack["Slack"]
        PD["PagerDuty"]
        Email["Email"]
        Teams["Teams"]
        RChat["RocketChat"]
        Webhook["Webhook"]
    end

    Defaults -->|1. Merge in| GitRepo
    Profiles -->|2. Merge in| GitRepo
    RulePacks -->|3. Mount via| PV
    TenantYAML -->|4. Merge in| GitRepo

    GitRepo -->|GitOps Sync<br/>(ArgoCD/Flux)| CM

    CM -->|SHA-256 hot-reload| Exp1
    CM -->|SHA-256 hot-reload| Exp2

    Exp1 -->|/metrics<br/>(metrics + flags)| Prom
    Exp2 -->|/metrics<br/>(metrics + flags)| Prom

    PV -->|Mounted Rules| Prom

    Prom -->|Alert Evaluation| RuleGen

    TenantYAML -->|Route/Receiver| RuleGen

    RuleGen -->|Fragment| AM

    AM -->|Alert Routes| Slack
    AM -->|Alert Routes| PD
    AM -->|Alert Routes| Email
    AM -->|Alert Routes| Teams
    AM -->|Alert Routes| RChat
    AM -->|Alert Routes| Webhook

    classDef cfgLayerStyle fill:#f3e5f5,stroke:#7b1fa2,color:#000
    classDef gitLayerStyle fill:#f0f0f0,stroke:#616161,color:#000
    classDef platformLayerStyle fill:#fce4ec,stroke:#c2185b,color:#000
    classDef computeLayerStyle fill:#e3f2fd,stroke:#1976d2,color:#000
    classDef evalLayerStyle fill:#e8f5e9,stroke:#388e3c,color:#000
    classDef routingLayerStyle fill:#fff3e0,stroke:#e65100,color:#000
    classDef notifLayerStyle fill:#f1f8e9,stroke:#558b2f,color:#000

    class Defaults,Profiles,RulePacks,TenantYAML cfgLayerStyle
    class GitRepo gitLayerStyle
    class CM platformLayerStyle
    class Exp1,Exp2 computeLayerStyle
    class PV,Prom evalLayerStyle
    class RuleGen,AM routingLayerStyle
    class Slack,PD,Email,Teams,RChat,Webhook notifLayerStyle
```

---

## 9. 新手快速導航

**我是 Platform Engineer，我該：**
1. 讀 [architecture-and-design.md](architecture-and-design.md) 理解整體架構
2. 學習 `validate_config.py` 和 `generate_alertmanager_routes.py`
3. 運用 `config_diff.py` 做 PR review blast radius 分析
4. 定期執行 `bump_docs.py` 維護版號一致性

**我是 Domain Expert (DBA)，我該：**
1. 讀 [custom-rule-governance.md](custom-rule-governance.md) 掌握治理模型
2. 使用 `migrate_rule.py` 協助新規則遷移
3. 用 `lint_custom_rules.py` 檢查自訂規則合規性
4. 用 `backtest_threshold.py` 驗證新閾值的歷史準確度

**我是 Tenant Team (SRE/DBA)，我該：**
1. 讀 [getting-started/for-tenants.md](getting-started/for-tenants.md) 快速上手
2. 用 `scaffold_tenant.py` 生成初始配置
3. 用 `diagnose.py` 定期檢查健康狀態
4. 用 `check_alert.py` 查詢 Alert 狀態
5. 用 `patch_config.py` 做局部更新（無需全量重新部署）

---

## 10. 相關文件與主題

- **深度架構** → [architecture-and-design.md](architecture-and-design.md)
- **遷移指南** → [migration-guide.md](migration-guide.md) 和 [migration-engine.md](migration-engine.md)
- **Tenant 快速入門** → [getting-started/for-tenants.md](getting-started/for-tenants.md)
- **治理與安全** → [governance-security.md](governance-security.md) 和 [custom-rule-governance.md](custom-rule-governance.md)
- **GitOps 部署** → [gitops-deployment.md](gitops-deployment.md)
- **故障排查** → [troubleshooting.md](troubleshooting.md)
- **Playbooks**（AI Agent 專用）
  - [docs/internal/testing-playbook.md](internal/testing-playbook.md)
  - [docs/internal/windows-mcp-playbook.md](internal/windows-mcp-playbook.md)
  - [docs/internal/github-release-playbook.md](internal/github-release-playbook.md)

---

**最後更新**：v2.0.0-preview | **維護者**：Platform Team

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Project Context Diagram: Roles, Tools, and Product Interactions"] | ⭐⭐⭐ |
| [001-severity-dedup-via-inhibit](adr/001-severity-dedup-via-inhibit.md) | ⭐⭐ |
| [002-oci-registry-over-chartmuseum](adr/002-oci-registry-over-chartmuseum.md) | ⭐⭐ |
| [003-sentinel-alert-pattern](adr/003-sentinel-alert-pattern.md) | ⭐⭐ |
| [004-federation-scenario-a-first](adr/004-federation-scenario-a-first.md) | ⭐⭐ |
| [005-projected-volume-for-rule-packs](adr/005-projected-volume-for-rule-packs.md) | ⭐⭐ |
| [README](adr/README.md) | ⭐⭐ |
| ["架構與設計 — 動態多租戶警報平台技術白皮書"](./architecture-and-design.md) | ⭐⭐ |
