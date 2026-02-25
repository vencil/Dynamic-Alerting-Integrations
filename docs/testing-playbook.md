# 測試注意事項 — 排錯手冊 (Testing Playbook)

> 測試前置準備與已知問題修復指引。

## 測試前置準備

1. **Dev Container**: `docker ps` → `vibe-dev-container` 運行中。
2. **Kind 叢集**: `docker exec vibe-dev-container kubectl get nodes` 正常。
3. **PyYAML**: `docker exec vibe-dev-container python3 -c "import yaml"` (失敗則 `pip3 install pyyaml`)。
4. **清理殘留**: `docker exec vibe-dev-container pkill -f port-forward`。

## K8s 環境問題

| # | 問題 | 修復 |
|---|------|------|
| 1 | Helm upgrade 不清理舊 ConfigMap key | `kubectl delete cm threshold-config -n monitoring` → `make component-deploy` |
| 2 | ConfigMap volume 更新延遲 30-90s | hot-reload 驗證需等 45+ 秒 |
| 3 | Shell `get_cm_value()` JSON 嵌入破壞 | 已改用 `kubectl ... \| python3 -c "json.load(sys.stdin)"` |
| 4 | Metrics label 順序 (`component,metric,severity,tenant`) | grep 用 `metric=.*tenant=`，不要反過來 |
| 5 | 場景測試殘留值 | 測試前用 `patch_config.py` 恢復預設 |
| 6 | Projected volume ConfigMap 未生效 | 確認所有 5 個 `configmap-rules-*.yaml` 已 `kubectl apply`；projected volume 要求每個 ConfigMap 都存在 |

## Projected Volume 架構注意事項 (v0.4.1+)

Recording Rules / Alert Rules 已從 `configmap-prometheus.yaml` 拆離至 5 個獨立 ConfigMap：

```
configmap-rules-{mariadb,kubernetes,redis,mongodb,elasticsearch}.yaml
```

每個 ConfigMap 含 `*-recording.yml` + `*-alert.yml` 兩個 key，透過 `projected` volume 投射至 `/etc/prometheus/rules/`。

**常見問題:**
- 少 apply 一個 ConfigMap → Prometheus Pod 啟動失敗（projected volume 要求所有 source 都存在）。
- 修改單個 Rule Pack 只需 apply 對應的 ConfigMap，不影響其他。
- YAML 驗證: `python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>` 確保每個 ConfigMap 合法。

## Shell 測試腳本陷阱

### grep 正則特殊字元 (PromQL)
PromQL 含 `*`, `(`, `)`, `|` 等 regex 特殊字元。

```bash
# 錯誤: ( 和 * 是 regex
grep "sum by.tenant..*(rate" file.yaml
# 正確: 用 . 作萬用字元
grep "sum by.tenant. .rate." file.yaml
# 或 -F 做 literal match
grep -F "sum by(tenant)" file.yaml
```

### 測試斷言必須隨程式碼同步更新
修改程式輸出格式後，舊斷言會失敗。例如:
- 報告從 `rule-pack-redis.yaml` 改為 `已預載` → grep `"rule-pack-redis"` 失敗
- Recording rule 註解從單行改為多行 ASCII 警告方塊 → grep 需改搜 `AI 智能猜測注意` 而非舊格式

**原則**: 修改輸出格式時，搜尋所有 `assert_.*contains` 確認是否需要更新。

### 完美 vs 複雜分類規則
`migrate_rule.py` 的分類取決於表達式結構:
- **Perfect**: `metric > threshold` (無函式無運算)
- **Complex**: 含 `rate()`, `/`, 運算符 → 觸發多行 ASCII 警告方塊
- **Unparseable**: 含 `absent()`, `predict_linear()` 等語義不可轉換函式

新增測試案例前，先跑 `--dry-run` 確認實際分類。

## SAST 相關測試 (v0.4.1+)

| 項目 | 驗證指令 |
|------|----------|
| Go G112 (ReadHeaderTimeout) | `grep ReadHeaderTimeout components/threshold-exporter/app/main.go` |
| Python CWE-276 (file permissions) | `grep -n "os.chmod" scripts/tools/migrate_rule.py scripts/tools/scaffold_tenant.py` |
| Go vet | `docker exec -w .../app vibe-dev-container go vet ./...` |

新增 Python 工具寫檔時，**每個 `open(..., "w")` 後必須接 `os.chmod(path, 0o600)`**。
