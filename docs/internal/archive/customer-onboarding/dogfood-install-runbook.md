---
title: "prod-install Dogfood Runbook — cold-walk #1 (Track B Phase 1, static/CLI)"
tags: [internal, onboarding, dogfood, install, runbook]
audience: [maintainers]
version: v2.8.1
lang: zh
status: living
related-issue: "#141 (Track B — prod-install dogfood)"
---

# prod-install Dogfood Runbook — cold-walk #1 (Track B Phase 1)

> **這是什麼**：依 issue #141 Track B，maintainer 以「新平台工程師視角」**只照文件**冷走 production
> 安裝路徑，記錄摩擦點與 independence。本次走 **Option A — CLI + 靜態 Helm 驗證、無 live cluster**
> （決策見下）；live install / hot-reload 為 deferred residue。內部、maintainer-local。

## Run metadata

| 項目 | 值 |
|---|---|
| Run # | 1（Track B Phase 1）|
| 日期 | 2026-05-31 |
| 範圍決策 | **Option A**：本機無 `helm`/`k3d`/`kind`、dev container 未跑 → 不架 cluster。dogfood da-tools 安裝 + config 驗證命令（Docker）+ 靜態驗證 helm install 命令/charts。Windows-host k3d 會注入環境雜訊（最差保真度），故 live 留待 Linux 殘量處理 |
| Host | Windows 11 · Git Bash · `docker` + `kubectl` 在、`helm`/`k3d`/`kind` 缺、`cosign` 缺 |
| 進入點文件 | `docs/migration-toolkit-installation.md`（da-tools CLI 安裝）+ `docs/getting-started/for-platform-engineers.md`（K8s/Helm 部署 + 驗證工具）|

## 走過的步驟與結果

| 來源 | 步驟（文件原話） | 結果 |
|---|---|---|
| install §Path A | `docker pull` + `da-tools --help` / `guard --help` / `guard defaults-impact --config-dir /conf.d --required-fields cpu,memory` | ✅ 命令正確（exit 1 只是 seed conf.d 無 cpu/memory required 欄位）|
| install §Path B | 版號範例 `TAG=tools/v2.7.0` + `da-guard --version → v2.7.0` | ❌ **TB-F1**（stale，最新 `tools/v2.8.0`）|
| install §Path A 註 | line 68「cosign 簽章為後續迭代項目」 | ❌ **TB-F2**（與同檔 §Signature Verification + 實際 release 的 `.sig/.cert` 矛盾）|
| install §Signature Verification | `verify_release.sh --tag tools/v2.8.0 --artefact da-parser-linux-amd64.tar.gz` | ✅ 命令/asset 正確；cosign 缺時**優雅報錯**（`missing required tool 'cosign' — install per <url>` exit 2）|
| platform §驗證工具 | `da-tools validate-config --config-dir /conf.d` | ✅ 5 checks PASS |
| platform §驗證工具 | `da-tools evaluate-policy --config-dir /conf.d` | ✅ 優雅（seed 無 `_policies`）|
| platform §驗證工具 | `da-tools alert-quality` / `cardinality-forecast` | ✅ 子命令存在、`--prometheus` flag 正確（需 live Prom 才能實跑 → deferred）|
| platform §30 秒部署 | `helm install threshold-exporter ./helm/threshold-exporter/ -n monitoring --create-namespace` | ✅ 靜態驗證：chart path 存在、`replicaCount: 2`（對應「×2 HA」）、無 `--set`、命令正確 |
| platform §掛載 Rule Pack | 「Prometheus **StatefulSet**」+ `prometheus-statefulset.yaml` + `kubectl edit statefulset prometheus` | ❌ **TB-F4**（實際是 **Deployment** / `deployment-prometheus.yaml`；`kubectl edit statefulset` 會直接失敗）|

## Friction log

### TB-F1 — 二進位安裝範例版號 stale（real bug + bump_docs 覆蓋缺口）🔴
- **現象**：`migration-toolkit-installation.md{,.en}` Path B 範例 `TAG=tools/v2.7.0`（行 115/134/168）+ `da-guard --version → v2.7.0`（行 128），但最新 tools release 是 **`tools/v2.8.0`**（2026-05-12）。複製貼上者裝到舊版。
- **根因**：`bump_docs.py:165-166` 的 sync rule 只配**粗體** `**`tools/vX.Y.Z`**` 形式 → 抓不到 code-block 的 `TAG=` / `--version` 範例。
- **修法（已套用）**：4 處範例 → `v2.8.0`（行 13 已 v2.8.0、行 14 `≤ tools/v2.7.0` 為**刻意歷史敘述**保留）。
- **bump_docs rule 刻意不加寬**：blanket 加寬會誤配行 14 的歷史 `≤ tools/v2.7.0`（同 CHANGELOG released-section 陷阱），風險高於價值 → 留作 manual。

### TB-F2 — line 68 cosign「後續迭代」自相矛盾（stale）🔴
- 同檔 §Signature Verification（行 209）已寫「tools/v2.8.0 起每個 artefact 附 cosign keyless 簽章」，且 release `tools/v2.8.0` 實際有 `.sig`/`.cert`/`.cyclonedx`（67 assets）。
- **修法（已套用）**：行 68 改為「cosign keyless 簽章自 tools/v2.8.0 起隨每個 artefact 發佈（驗證見 §Signature Verification）」。

### TB-F4 — Prometheus 被誤標 StatefulSet（real bug，命令會失敗）🔴
- `for-platform-engineers.md{,.en}` 行 69/70/110/113（EN 69/70/108/111）稱「Prometheus StatefulSet」+ `prometheus-statefulset.yaml` + `kubectl edit statefulset prometheus`。
- **實際**：`k8s/03-monitoring/deployment-prometheus.yaml`（`kind: Deployment`，projected volume 在行 122）；`k8s/` 全域**無任何 StatefulSet**。`kubectl edit statefulset prometheus` 會回 NotFound。
- **修法（已套用）**：StatefulSet → Deployment、`prometheus-statefulset.yaml` → `deployment-prometheus.yaml`、`kubectl edit statefulset` → `kubectl edit deployment`（ZH+EN 共 8 處）。

### TB-F3 — validate_config 用 repo-local python 路徑（minor observation，非 bug）
- platform §一站式驗證用 `python3 scripts/tools/ops/validate_config.py`（需 clone repo），而非 install doc 剛教的可攜 `da-tools validate-config`。兩者皆可跑（路徑存在）。受眾是部署平台者、多半有 repo，故**不修**；僅記錄可考慮補一行可攜替代。

## helm 安裝決策：**不裝**
documented `helm install` 已靜態驗證（path 對、`replicaCount: 2`、無 `--set`），charts 另由 #448 Container SAST（kube-linter）把關。`helm template` 的邊際價值（抓 render error，charts 已 CI-lint）低於「裝 helm + 維護」成本 → **跳過**，live install / `helm template` 歸入 deferred residue。

## Independence baseline
命令層（可靜態/CLI 驗證者）：大多數**照文件即可獨立完成**；3 個 doc bug（TB-F1 裝到舊版 / TB-F2 矛盾困惑 / TB-F4 命令直接失敗）會卡住或誤導，**均已修**。
- runtime 層（pod 起得來 / hot-reload 真觸發 / RBAC 真擋）= **未測**，屬 deferred residue（需 live Linux cluster）。

## Deferred residue（Track B Phase 2，需環境）
- live `helm install` + `kubectl get pod` 起來、projected-volume rule-pack 真掛、hot-reload 真觸發（需 cluster）。
- cosign 完整驗章（需裝 cosign）；`alert-quality` / `cardinality-forecast` 實跑（需 live Prometheus）。
- 最faithful 環境 = Linux（非 Windows k3d）。

## 對抗式複查 — 全 repo 同類掃描（被「review 做的充足嗎」逼出）

初版只修了「dogfood 撞到」的兩檔；對抗式 grep 整個 `docs/` 找同類 bug，**抓到漏網的**：

| 位置 | 類別 | 判定 |
|---|---|---|
| `migration-guide.md{,.en}:29` | TB-F1（寫死 `tools/v2.7.0` 下載 URL，同檔 Path A 卻 v2.8.0）| ✅ **補修 → v2.8.0** |
| `scenarios/incremental-migration-playbook.md{,.en}:155` | TB-F1（`--set image.tag=v2.7.0`，最新 exporter = `exporter/v2.8.0`、chart appVersion 2.8.0、無 upgrade-from 敘事）| ✅ **補修 → v2.8.0** |
| `adr/005-projected-volume-for-rule-packs.md{,.en}` | TB-F4（YAML 範例註「Prometheus StatefulSet」）| ✅ **補修 → Deployment**（ADR `status: accepted` 且 `updated_at 2026-05-13` = living，非凍結）|
| `integration/troubleshooting-checklist.md{,.en}:721` | TB-F4? `kubectl rollout restart statefulset prometheus -n <prom-ns>` | ⏸️ **刻意不改**——`<prom-ns>` placeholder = **客戶自有** Prometheus（operator-managed 多為 StatefulSet），非我方 `deployment-prometheus.yaml` |
| `scenarios/multi-system-migration-playbook.md{,.en}:602` | TB-F4? `kubectl scale statefulset prometheus-k8s` | ⏸️ **刻意不改**——`prometheus-k8s` 是 prometheus-operator 命名，指客戶 operator Prometheus，**正確** |
| `design/roadmap-future.md{,.en}` | TB-F2? cosign | ⏸️ 確認**一致**——已寫「Layer 1 已交付 cosign keyless」，非 future |

**修錯邊檢查（TB-F4）**：`deployment-prometheus.yaml` = `kind: Deployment`；`k8s/`+`helm/`+`design/` 無任何「Prometheus 應為 StatefulSet」設計指令 → 文件改 Deployment 是對的、非「文件對 code 錯」。

## 已套用的 doc 修正（本 cycle，含對抗式複查擴充）
- **TB-F1**（stale tools 版號，全 6 檔）：`migration-toolkit-installation.md{,.en}`（4 處）+ `migration-guide.md{,.en}`（下載 URL）+ `scenarios/incremental-migration-playbook.md{,.en}`（`--set image.tag`）→ `v2.8.0`。
- **TB-F2**（cosign 措辭）：`migration-toolkit-installation.md{,.en}` line 68。
- **TB-F4**（Prometheus StatefulSet→Deployment）：`for-platform-engineers.md{,.en}`（8 處）+ `adr/005-projected-volume-for-rule-packs.md{,.en}`（範例註）。
- **刻意保留**：troubleshooting-checklist / multi-system-migration（客戶側 Prometheus，非我方 manifest）；migration-toolkit `≤ tools/v2.7.0`（歷史敘述）；bump_docs rule 不加寬（避歷史敘述誤配）。
