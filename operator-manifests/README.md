# operator-manifests/

> **此目錄為 `operator_generate.py` 自動產出，勿手動編輯。**

每個 `da-rule-pack-*.yaml` 對應 `rule-packs/rule-pack-*.yaml` 的 Operator CRD 轉換結果。

## 工作流

```bash
python scripts/tools/ops/operator_generate.py --output-dir operator-manifests/
kubectl apply -f operator-manifests/
```

詳見 [migration-guide.md](../docs/migration-guide.md) 及 [cli-reference.md](../docs/cli-reference.md)。
