---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-29T00:53:00+00:00
---
- **讀 Kubernetes manifest、Helm values／`Chart.yaml`／渲染結果的工具改經 `da-crdecode`（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §12）**：`check_configmap_mount_completeness`、`check_cross_ns_url_consistency`、`check_k8s_manifests`、`check_ksm_version_allowlist`、`check_log_egress_policy`、`check_md_yaml_drift --check crd`、`check_scrape_reachability`、`check_single_writer_invariant`、`check_threshold_reachability`、`check_threshold_unit_sanity`、`check_image_refs_resolve`、`configmap_assemble`、`generate_byo_rulepack_table`、`generate_crd_schemas`、`generate_platform_data`、`measure_judgement_budget`。值是 kubectl／Helm 的讀法，本 repo 上的結果與先前相同。⚠️ 沒有 `da-crdecode` 時結束碼 2（pre-commit、CI、Makefile 已接上建置；`make configmap-assemble` 要帶 `DA_CRDECODE_BINARY`）；`check_image_refs_resolve` 讀不了的檔不再 `::warning::` 略過而是結束碼 2；`Chart.yaml` 的 `appVersion` 在 Helm 讀法下不是字串時 `generate_platform_data` 拒收。
