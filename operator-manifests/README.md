# operator-manifests/

> **此目錄為 `operator_generate.py` 自動產出，勿手動編輯。**

每個 `da-rule-pack-*.yaml` 對應 `rule-packs/rule-pack-*.yaml` 的 Operator CRD 轉換結果。

> **三副本 hard gate（ADR-024 PR3-pre）**：本目錄是 rule pack 的第三副本，與 `rule-packs/`（source）、`k8s/03-monitoring/configmap-rules-*.yaml`（ConfigMap 副本）須語意一致。改 rule pack 後忘了重生本目錄會被 `check_rulepack_sync.py --ci`（CI job「Lint Rule Packs」）與 pre-commit `rulepack-3copy-drift` hook 擋下。

## 工作流

```bash
# --components rules：只重生 14 個 PrometheusRule（本目錄的內容），
# 不產出 ServiceMonitor / AlertmanagerConfig（那些不在三副本 hard gate 範圍）。
python scripts/tools/ops/operator_generate.py --components rules --output-dir operator-manifests/
kubectl apply -f operator-manifests/
```

詳見 [migration-guide.md](../docs/migration-guide.md) 及 [cli-reference.md](../docs/cli-reference.md)。
