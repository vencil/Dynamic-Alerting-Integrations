# 測試注意事項 — 排錯手冊 (Testing Playbook)

> 從 `CLAUDE.md` 抽取。包含測試前置準備清單與已知問題修復指引。

## 測試前置準備

1. **確認 Dev Container 運行**: `docker ps` 應看到 `vibe-dev-container`。
2. **確認 Kind 叢集**: `docker exec vibe-dev-container kind get clusters` → `dynamic-alerting-cluster`。
3. **確認 kubeconfig**: `docker exec vibe-dev-container kubectl get nodes`。失敗則重新匯出: `kind export kubeconfig --name dynamic-alerting-cluster --kubeconfig /root/.kube/config`。
4. **確認 PyYAML**: `docker exec vibe-dev-container python3 -c "import yaml"` 失敗則安裝: `pip3 install pyyaml`。
5. **清理殘留 port-forward**: `docker exec vibe-dev-container pkill -f port-forward`。

## 已知問題與修復

### 1. Helm upgrade 不清理舊 ConfigMap key
從單檔升級到目錄模式後，`config.yaml` key 殘留。
- **現象**: Exporter WARN log `state_filters found in config.yaml`。
- **修復**: `kubectl delete cm threshold-config -n monitoring` → `make component-deploy`。

### 2. ConfigMap Volume Propagation 延遲
K8s ConfigMap volume mount 更新延遲 30-90 秒。
- **現象**: `patch_config.py` 更新 ConfigMap 後，exporter 的 `/metrics` 不立即反映。
- **修復**: 整合測試中 hot-reload 驗證需等 45+ 秒 (`integration-2c.sh` 已調整)。

### 3. Shell 腳本中 JSON 嵌入
`_lib.sh` 的 `get_cm_value()` 不可用 `'''${var}'''` 內嵌 JSON（多行 YAML 值會破壞 Python string）。
- **已修復**: 改用 `kubectl ... | python3 -c "json.load(sys.stdin)"`。

### 4. Metrics label 順序
`user_threshold` 的 label 順序為 `component, metric, severity, tenant`。grep 時注意不要假設 `tenant` 在 `metric` 前面。
- **正確**: `grep 'metric="connections".*tenant="db-a"'`
- **錯誤**: `grep 'tenant="db-a".*metric="connections"'`

### 5. Scenario 測試殘留值
場景測試中斷後，ConfigMap 中 tenant 值可能停留在測試值 (如 mysql_connections=5)。
- **修復**: 測試前用 `patch_config.py` 恢復預設值，或檢查 `get_cm_value` 確認當前值。

## Phase 3 新增 — Shell 測試腳本陷阱

### 6. grep 正則特殊字元
在 `assert_file_contains` 等 grep 斷言中，PromQL 表達式含有大量正則特殊字元。

| 字元 | grep 意義 | 修復方式 |
|------|----------|---------|
| `*` | 重複前一字元 0+ 次 | 用 `.` (任意字元) 取代，或用 `grep -F` |
| `(` `)` | 群組 | 用 `.` 取代，或 `\(` 跳脫 |
| `|` | alternation | 用 `grep -E` 或只搜尋其中一個選項 |

```bash
# 錯誤: sum by.tenant..*(rate 中的 * 和 ( 是 regex 特殊字元
grep "sum by.tenant..*(rate" file.yaml

# 正確: 用 . 作為萬用字元
grep "sum by.tenant. .rate." file.yaml

# 或用 -F 做 literal match (但不能再用 . 萬用)
grep -F "sum by(tenant)" file.yaml
```

### 7. YAML safe_dump 轉義多行字串
Python `yaml.safe_dump()` 會將多行 PromQL 表達式轉為帶 `\n` 的單行字串 (加雙引號)。

- **現象**: 原始 YAML `unless on(tenant)\n(user_state_filter...)` → dump 後變成 `"...\\nunless on(tenant)..."`
- **影響**: `grep "unless.*maintenance"` 失敗，因為 `unless` 和 `maintenance` 不在同一行。
- **修復**: 只 grep 關鍵詞 `grep "maintenance"` 而非跨行模式。

```bash
# 錯誤: unless 和 maintenance 被 safe_dump 放在不同位置
grep "unless.*maintenance" output.yaml

# 正確: 只搜尋關鍵詞
grep "maintenance" output.yaml
```

### 8. 中文 + emoji + alternation 混合 grep
含中文與 emoji 的 alternation pattern 在 bash grep 中常出問題。

```bash
# 錯誤: 複雜 alternation 容易因 encoding 失敗
grep "AI 猜測\|單點\|叢集" file.txt

# 正確: 簡化為單一關鍵詞
grep "單點" file.txt

# 或用 grep -E 並逐一驗證
grep -E "(單點|叢集)" file.txt
```

### 9. 測試計數斷言需精確分析
`migrate_rule.py` 的「完美 vs 複雜」分類取決於表達式結構，不是主觀判斷:
- **完美 (Perfect)**: 簡單 `metric > threshold`，無函式無運算 → 直接轉三態
- **複雜 (Complex)**: 含 `rate()`、`/`、`absent()` 等需要 heuristic 或 LLM

當新增測試案例時，先在腳本中跑一次 dry-run 確認實際分類，再寫斷言數字。
