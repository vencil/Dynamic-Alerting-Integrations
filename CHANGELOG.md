# Changelog

## [Unreleased]

### Changed - Week 1 Refactoring (2025-02-16)

#### Project Rename
- Renamed project from `vibe-k8s-lab` to `dynamic-alerting-integrations`
- Updated cluster name from `vibe-cluster` to `dynamic-alerting-cluster`
- Updated all documentation and configuration files

#### Directory Structure
- Added `components/` for sub-component manifests
  - `threshold-exporter/` - Dynamic threshold exporter (to be implemented)
  - `config-api/` - Configuration API (placeholder)
  - `alert-router/` - Alert router (placeholder)
  - `kube-state-metrics/` - K8s state metrics
- Added `environments/` for environment-specific configs
  - `local/` - Local development (dev images, memory storage)
  - `ci/` - CI/CD (registry images, Redis, HA)
- Added `tests/` for integration tests
- Added `.claude/skills/` for AI Agent skills

#### Features
- **Component Management System**
  - `make component-build` - Build component image and load to Kind
  - `make component-deploy` - Deploy component with environment config
  - `make component-test` - Run integration tests
  - `make component-logs` - View component logs
  - `make component-list` - List available components

- **inspect-tenant Skill**
  - Comprehensive health check for tenants
  - Checks Pod status, DB health, Exporter status, Metrics availability
  - JSON output for programmatic processing
  - Usage: `make inspect-tenant TENANT=db-a`

- **Prometheus Enhancements**
  - Added Recording Rules (Normalization Layer)
    - `tenant:mysql_cpu_usage:rate5m`
    - `tenant:mysql_connection_usage:ratio`
    - `tenant:mysql_uptime:hours`
  - Added Dynamic Threshold rules (with defaults)
    - `tenant:alert_threshold:cpu`
    - `tenant:alert_threshold:connections`
  - Updated Alert Rules to use `group_left` join
  - Added `tenant` label to all scrape configs
  - Added scrape configs for kube-state-metrics and threshold-exporter

- **kube-state-metrics Integration**
  - Deployment script: `scripts/deploy-kube-state-metrics.sh`
  - Provides K8s native metrics for Scenario C (State Matching)
  - Metrics: pod phase, container status, deployment replicas, node conditions

#### Documentation
- Added `docs/architecture-review.md` - Comprehensive evaluation and recommendations
- Added `docs/deployment-guide.md` - Deployment instructions and troubleshooting
- Updated `CLAUDE.md` with Week 1 changes and next steps
- Added `CHANGELOG.md` (this file)

#### Infrastructure
- Prepared environment configs for local and CI/CD
- Created placeholder README for threshold-exporter component
- Added deployment scripts for kube-state-metrics

### To Be Implemented

#### Week 2-3: threshold-exporter
- HTTP API for threshold configuration
- Prometheus metrics endpoint
- Memory-based storage (local) / Redis (production)
- Integration with Prometheus recording rules

#### Week 4: Scenario A Verification
- Integration test scripts
- End-to-end dynamic threshold testing
- Alert state verification

## [0.1.0] - 2025-02-15

### Added
- Initial project setup with Kind cluster
- MariaDB instances (db-a, db-b) with mysqld_exporter sidecar
- Prometheus, Grafana, Alertmanager monitoring stack
- Basic alert rules (MariaDBDown, MariaDBHighConnections, etc.)
- Dev Container configuration
- Makefile with common operations
- Verification scripts
- Alert testing scripts
- Documentation (README, CLAUDE.md)

### Features
- Two-tenant MariaDB setup
- Sidecar pattern for metrics export
- Static Prometheus scrape configuration
- Basic alert rules with static thresholds
- Cross-platform shell scripts
- Helm chart for MariaDB deployment
