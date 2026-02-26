# 測試注意事項 — 排錯手冊 (Testing Playbook)

> 測試前置準備與已知問題修復指引。
> **相關文件：** [Windows-MCP Playbook](windows-mcp-playbook.md) (docker exec 模式、Helm 防衝突、Prometheus 查詢)

## 測試前置準備

1. **Dev Container**: `docker ps` → `vibe-dev-container` 運行中。
2. **Kind 叢集**: `docker exec vibe-dev-container kubectl get nodes` 正常。
3. **PyYAML**: `docker exec vibe-dev-container python3 -c "import yaml"` (失敗則 `pip3 install pyyaml`)。
4. **清理殘留**: `docker exec vibe-dev-container pkill -f port-forward`。

## K8s 環境問題

| # | 問題 | 修復 |
|---|------|------|
| 1 | Helm upgrade 不清理舊 ConfigMap key | `kubectl delete cm threshold-config -n monitoring` → `helm upgrade` |
| 2 | Helm field-manager conflict | 見 [Windows-MCP Playbook → Helm 防衝突流程](windows-mcp-playbook.md#helm-upgrade-防衝突流程) |
| 3 | ConfigMap volume 更新延遲 30-90s | hot-reload 驗證需等 45+ 秒 |
| 4 | Metrics label 順序 (`component,metric,severity,tenant`) | grep 用 `metric=.*tenant=`，不要反過來 |
| 5 | 場景測試殘留值 | 測試前用 `patch_config.py` 恢復預設 |
| 6 | Projected volume ConfigMap 未生效 | 確認所有 6 個 `configmap-rules-*.yaml` 已 apply；projected volume 要求每個 ConfigMap 都存在 |

## Projected Volume 架構 (v0.5+)

6 個獨立 ConfigMap 透過 `projected` volume 投射至 `/etc/prometheus/rules/`：

```
configmap-rules-{mariadb,kubernetes,redis,mongodb,elasticsearch,platform}.yaml
```

每個 DB Rule Pack 含 `*-recording.yml` + `*-alert.yml`；Platform 含 `platform-alert.yml`。

**常見問題:**
- 少 apply 一個 ConfigMap → Prometheus Pod 啟動失敗（projected volume 要求所有 source 都存在）。
- 修改單個 Rule Pack 只需 apply 對應的 ConfigMap，不影響其他。
- YAML 驗證: `python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>`。

## HA 相關測試 (v0.5+)

### Recording Rules — max vs sum

threshold-exporter 跑多 replica 時，每個 Pod 匯出相同的 `user_threshold`。聚合必須用 `max by(tenant)`，否則閾值翻倍。

```bash
# 驗證: 所有 threshold recording rules 使用 max (不應有 sum)
docker exec vibe-dev-container bash -c "\
  grep -n 'user_threshold' k8s/03-monitoring/configmap-rules-*.yaml \
  > /workspaces/vibe-k8s-lab/output.txt 2>&1"
# 期望: 全部行含 'max by'，0 行含 'sum by'
```

**注意**: 真實 DB 指標 (如 `rate(mysql_...)`) 仍用 `sum by(tenant)`，這是正確的 — 只有 `user_threshold` 需改 `max`。

### HA 部署驗證清單

| 項目 | 驗證指令 | 期望 |
|------|----------|------|
| 2 Pods Running | `kubectl get pods -n monitoring -l app=threshold-exporter` | 2/2 Ready |
| PDB 存在 | `kubectl get pdb -n monitoring` | `minAvailable: 1` |
| AntiAffinity | `kubectl get deploy ... -o jsonpath='{.spec.template.spec.affinity}'` | 含 `podAntiAffinity` |
| RollingUpdate | `kubectl get deploy ... -o jsonpath='{.spec.strategy}'` | `maxUnavailable: 0` |
| Platform alerts | `kubectl exec -n monitoring deploy/prometheus -- ls /etc/prometheus/rules/` | 含 `platform-alert.yml` |

### kubectl scale 注意

`helm upgrade` 後 replicas 可能被覆蓋。若只有 1 個 Pod：
1. 檢查 `values.yaml` 的 `replicaCount` 是否為 2
2. `kubectl scale deploy threshold-exporter -n monitoring --replicas=2` 補救
3. 根因通常是 server-side apply 與 Helm 的 field ownership 衝突

## Performance Benchmark 測試

使用一次性 port-forward 查詢 Prometheus 內建指標（詳見 [Windows-MCP Playbook → Prometheus API 查詢模式](windows-mcp-playbook.md#prometheus-api-查詢模式)）：

**關鍵指標基線 (v0.5.0, 2 tenants, Kind 單節點)：**

| 指標 | 值 | 說明 |
|------|----|------|
| 規則總數 | 85 | 33 recording + 35 alert + 17 threshold-norm |
| 規則群組數 | 18 | 6 pack × 3 groups (部分合併) |
| 每週期總評估時間 | ~20.8ms | `sum(prometheus_rule_group_last_duration_seconds)` |
| p50 per-group | 0.59ms | `prometheus_rule_group_duration_seconds{quantile="0.5"}` |
| p99 per-group | 5.05ms | `prometheus_rule_group_duration_seconds{quantile="0.99"}` |
| 空向量 pack 成本 | <1.75ms | ES/Redis/MongoDB 無 exporter 時 |

**用途：** 更新 README benchmark 表格或驗證架構變更未造成性能退化。

## Shell 測試腳本陷阱

### grep 正則特殊字元 (PromQL)

```bash
# ❌ ( 和 * 是 regex
grep "sum by.tenant..*(rate" file.yaml
# ✅ 用 -F 做 literal match
grep -F "max by(tenant)" file.yaml
```

### 測試斷言必須隨程式碼同步更新

修改程式輸出格式後，舊斷言會失敗。
**原則**: 修改輸出格式時，搜尋所有 `assert_.*contains` 確認是否需要更新。

### 完美 vs 複雜分類規則

`migrate_rule.py` 的分類取決於表達式結構:
- **Perfect**: `metric > threshold` (無函式無運算)
- **Complex**: 含 `rate()`, `/`, 運算符 → 觸發多行 ASCII 警告方塊
- **Unparseable**: 含 `absent()`, `predict_linear()` 等語義不可轉換函式

新增測試案例前，先跑 `--dry-run` 確認實際分類。

## SAST 相關測試

| 項目 | 驗證指令 |
|------|----------|
| Go G112 (ReadHeaderTimeout) | `grep ReadHeaderTimeout components/threshold-exporter/app/main.go` |
| Python CWE-276 (file permissions) | `grep -n "os.chmod" scripts/tools/migrate_rule.py scripts/tools/scaffold_tenant.py` |
| Go vet | `docker exec -w .../app vibe-dev-container go vet ./...` |

新增 Python 工具寫檔時，**每個 `open(..., "w")` 後必須接 `os.chmod(path, 0o600)`**。
