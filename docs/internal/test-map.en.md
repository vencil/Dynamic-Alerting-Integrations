# Test Architecture Map

> Quick reference for test infrastructure, conventions, and file layout — for AI Agents and developers.
>
> **Related docs:** [Testing Playbook](testing-playbook.md) (troubleshooting) · [Benchmark Playbook](benchmark-playbook.md) (methodology, pitfalls) · [Advanced Scenarios & Test Coverage](../scenarios/advanced-scenarios.md) (E2E + feature matrix) · [Benchmarks](../benchmarks.md) (performance data)

## Test Infrastructure

| File | Responsibility |
|------|---------------|
| `tests/conftest.py` | sys.path setup + pytest fixtures (session + function scope) |
| `tests/factories.py` | All factory helpers + PipelineBuilder + mock_http_response (with docstrings) |
| `setup.cfg` | pytest markers + coverage config (fail_under=80) |

## Factory List

| Factory | Purpose | Location |
|---------|---------|----------|
| `write_yaml()` | Write YAML to temp directory | factories.py |
| `make_receiver()` | Generate receiver dict (5 types) | factories.py |
| `make_routing_config()` | Generate routing config | factories.py |
| `make_tenant_yaml()` | Generate tenant YAML string | factories.py |
| `make_defaults_yaml()` | Generate _defaults.yaml string | factories.py |
| `make_am_receiver()` | Generate native AM format receiver | factories.py |
| `make_am_config()` | Generate complete AM config dict | factories.py |
| `make_override()` | Generate per-rule routing override | factories.py |
| `make_enforced_routing()` | Generate enforced routing config | factories.py |
| `mock_http_response()` | Mock HTTP response (urlopen mock) | factories.py |
| `populate_routing_dir()` | Pre-populate multi-tenant routing YAML | factories.py |
| `PipelineBuilder` | Chain-build scaffold → routes pipeline | factories.py |

## Test Markers

| Marker | Purpose | Run Command |
|--------|---------|-------------|
| `slow` | Slow tests (benchmark, property-based) | `pytest -m "not slow"` to skip |
| `integration` | Cross-module integration tests | `pytest -m integration` |
| `benchmark` | Performance baseline tests | `pytest -m benchmark` |
| `regression` | Known bug regression tests | `pytest -m regression` |
| `snapshot` | Output format stability snapshots | `pytest -m snapshot` |

## Test File Reference

| Test File | Target | Tests | Notes |
|-----------|--------|-------|-------|
| `test_generate_alertmanager_routes.py` | routing / receiver / inhibit / enforced | 142 | Largest feature test |
| `test_scaffold_db.py` | RULE_PACKS catalogue / scaffold generation / YAML validation | 129 | parametrize-optimized |
| `test_scaffold_tenant.py` | scaffold_tenant.py core functions | 81 | +9 routing profile/topology tests |
| `test_lib_python.py` | _lib_python shared library | 82 | |
| `test_entrypoint.py` | da-tools CLI entrypoint | 24 | monkeypatch-based |
| `test_onboard_platform.py` | Full onboard pipeline | 71 | parametrize receiver types |
| `test_integration.py` | Cross-module routing + PipelineBuilder | 17 | integration marker |
| `test_snapshot.py` | 18 JSON snapshots | 18 | snapshot marker |
| `test_domain_policy.py` | webhook domain allowlist + fnmatch | 26 | |
| `test_error_consistency.py` | warning format consistency | 14 | |
| `test_mutation_guards.py` | function behavior precise values | 49 | |
| `test_regression.py` | known bug regressions | 9 | regression marker |
| `test_validate_config.py` | validate_config.py config validation | 25 | |
| `test_config_diff.py` | config_diff.py diff detection | 40 | |
| `test_bump_docs.py` | bump_docs.py version bumping | 11 | |
| `test_maintenance_scheduler.py` | maintenance_scheduler.py scheduling | 55 | |
| `test_performance.py` | performance curves (scaling / load) | 7 | slow marker |
| `test_benchmark.py` | performance baselines | 14 | benchmark + slow markers |
| `test_property.py` | Hypothesis property-based | 15 | slow marker |
| `test_analyze_gaps.py` | analyze_rule_pack_gaps.py gap analysis | 34 | |
| `test_assemble_config_dir.py` | assemble_config_dir.py assembly | 34 | |
| `test_validate_all.py` | validate_all.py validation entry | 58 | coverage 14→41% |
| `test_baseline_discovery.py` | baseline_discovery.py baseline observation | 38 | coverage 31→55% |
| `test_backtest_threshold.py` | backtest_threshold.py threshold backtesting | 39 | coverage 32→70% |
| `test_batch_diagnose.py` | batch_diagnose.py batch diagnostics | 25 | coverage 49→71% |
| `test_alert_quality.py` | alert_quality.py alert quality scoring | 57 | v2.0.0, 89.8% coverage |
| `test_policy_engine.py` | policy_engine.py Policy-as-Code engine | 106 | v2.0.0, 94.0% coverage |
| `test_cardinality_forecasting.py` | cardinality_forecasting.py forecasting | 61 | v2.0.0, 93.5% coverage |
| `test_sast.py` | Repo-wide SAST compliance (6 rules) | 426 | encoding + shell + chmod + yaml.safe_load + credentials + dangerous functions |
| `test_migrate_ast.py` | migrate_rule AST engine | 67 | |
| `test_migrate_v3.py` | migrate_rule v3 engine | 38 | |
| `test_blind_spot_discovery.py` | blind_spot_discovery.py blind spot scan | 39 | |
| `test_lint_custom_rules.py` | lint_custom_rules.py rule linting | 40 | |
| `test_offboard_deprecate.py` | offboard/deprecate lifecycle | 34 | |
| `test_cutover_tenant.py` | cutover_tenant.py auto-switch | 26 | |
| `test_patch_config.py` | patch_config.py partial update | 38 | coverage 54→99% |
| `test_diagnose_inheritance.py` | diagnose inheritance chain | 7 | |
| `test_da_assembler.py` | da_assembler assembly | 36 | coverage 48→70% |
| `test_lib_helpers.py` | _lib helper functions | 34 | |
| `test_alert_correlate.py` | alert_correlate.py alert correlation | 46 | v2.1.0 |
| `test_check_bilingual_content.py` | check_bilingual_content.py bilingual lint | 24 | v2.1.0 |
| `test_check_cli_coverage.py` | check_cli_coverage.py CLI coverage lint | 29 | v2.1.0 |
| `test_check_frontmatter_versions.py` | check_frontmatter_versions.py version lint | 29 | v2.1.0 |
| `test_coverage_gap_analysis.py` | coverage_gap_analysis.py coverage gap | 22 | v2.1.0 |
| `test_diagnose.py` | diagnose.py tenant health diagnostics | 38 | coverage 40→88% |
| `test_drift_detect.py` | drift_detect.py config drift detection | 40 | v2.1.0 |
| `test_flows_e2e.py` | flows.json E2E validation | 0 | manual-stage marker |
| `test_notification_tester.py` | notification_tester.py notification testing | 57 | v2.1.0 |
| `test_snapshot_v2.py` | v2 snapshot stability | 6 | snapshot marker |
| `test_threshold_recommend.py` | threshold_recommend.py recommendation | 54 | v2.1.0 |
| `test_validate_migration.py` | validate_migration.py migration validation | 49 | coverage 22→99% |
| `test_check_routing_profiles.py` | check_routing_profiles.py routing profile lint | 28 | v2.1.0 ADR-007 |
| `test_explain_route.py` | explain_route.py routing debugger | 25 | v2.1.0 ADR-007 |
| `test_generate_tenant_mapping_rules.py` | generate_tenant_mapping_rules.py tenant mapping | 36 | v2.1.0 ADR-006 |
| `test_e2e_routing_profile.py` | routing profile E2E pipeline | 12 | v2.1.0 ADR-007 integration |
| `test_parse_platform_config.py` | _parse_platform_config unit tests | 35 | v2.1.0 refactor verification |
| `test_check_doc_freshness.py` | check_doc_freshness.py doc freshness scanner | 32 | v2.1.0 |
| `test_check_structure.py` | check_structure.py directory structure enforcement | 18 | v2.1.0 |
| `test_lint_tool_consistency.py` | lint_tool_consistency.py tool registry consistency | 25 | v2.1.0 |
| `test_check_bilingual_annotations.py` | check_bilingual_annotations.py bilingual annotation validation | 19 | v2.1.0 |
| `test_check_includes_sync.py` | check_includes_sync.py zh/en include sync | 23 | v2.1.0 |
| `test_check_doc_links.py` | check_doc_links.py doc cross-reference checker | 32 | v2.1.0 |
| `test_discover_instance_mappings.py` | discover_instance_mappings.py 1:N mapping auto-discovery | 18 | v2.1.0 ADR-006 |
| `test_explain_route_trace.py` | explain_route.py --trace route tracing simulation | 12 | v2.1.0 ADR-007 |
| `test_byo_check.py` | byo_check.py BYO integration pre-check verification | 14 | v2.1.0 |
| `test_federation_check.py` | federation_check.py federation multi-cluster verification | 18 | v2.1.0 |
| `test_check_repo_name.py` | check_repo_name.py repository name consistency | 14 | v2.1.0 |
| `test_shadow_verify.py` | shadow_verify.py Shadow Monitoring 3-phase verification | 16 | v2.1.0 |
| `test_offboard_tenant.py` | offboard_tenant.py safe tenant offboarding tool | 22 | v2.1.0 |

## Import Conventions

- Factory helpers: **import directly** `from factories import make_receiver, ...`
- conftest.py only provides pytest fixtures (session/function scope), no re-exports
- Test files should NOT `from conftest import` factory functions

## Snapshot Workflow

Snapshots are stored in `tests/snapshots/*.json` and auto-created on first run.

- Update snapshots: `UPDATE_SNAPSHOTS=1 pytest -m snapshot`
- Structured diff: integrated deepdiff for diff display

## Benchmark Baselines

Run performance baseline tests with `pytest -m benchmark` (requires pytest-benchmark).

| Test | v2.0.0-preview.4 Baseline | Description |
|------|--------------------------|-------------|
| `test_10_tenants` | ~38 µs | 10-tenant routing generation |
| `test_50_tenants` | ~197 µs | 50-tenant routing generation |
| `test_100_tenants` | ~394 µs | 100-tenant routing generation |
| `test_100_tenants` (inhibit) | ~32 µs | 100-tenant inhibit rules |
| `test_10_tenants_from_disk` | ~5.4 ms | 10-tenant with YAML I/O |
| `test_parse_integer` | ~102 ns | parse_duration_seconds micro-bench |

Baselines measured on Cowork VM (min_rounds=20, warmup=on), used for trend detection rather than absolute values. Update this table on version upgrades. Full methodology in [Benchmark Playbook](benchmark-playbook.md).

## Common Commands

```bash
make test                           # Full test suite
make test ARGS="-m 'not slow'"     # Skip slow tests
make coverage                       # Coverage report
make coverage ARGS="--html"        # HTML coverage
pytest -m integration              # Integration tests only
pytest -m regression               # Regression tests only
```
