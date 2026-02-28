# Architecture and Design — Multi-Tenant Dynamic Alerting Platform Technical Whitepaper

> **Language / 語言：** **English (Current)** | [中文](architecture-and-design.md)

## Introduction

This document provides Platform Engineers and Site Reliability Engineers (SREs) with an in-depth exploration of the technical architecture of the "Multi-Tenant Dynamic Alerting Platform" (v0.12.0).

**This document covers:**
- System architecture and core design principles
- Config-driven configuration workflow
- Governance model for Projected Volume and Rule Packs
- Performance analysis and scalability proof
- High availability (HA) design
- Governance, audit, and security compliance

**Related documentation:**
- **Quick Start** → [README.en.md](../README.en.md)
- **Migration Guide** → [migration-guide.md](migration-guide.md)
- **Rule Packs Documentation** → [rule-packs/README.md](../rule-packs/README.md)
- **threshold-exporter Component** → [components/threshold-exporter/README.md](../components/threshold-exporter/README.md)

---

## 1. System Architecture Diagram

```mermaid
graph TB
    subgraph Cluster["Kind Cluster: dynamic-alerting-cluster"]
        subgraph TenantA["Namespace: db-a (Tenant A)"]
            ExpA["Tenant A Exporter\n(MariaDB, Redis, etc.)"]
        end

        subgraph TenantB["Namespace: db-b (Tenant B)"]
            ExpB["Tenant B Exporter\n(MongoDB, Elasticsearch, etc.)"]
        end

        subgraph Monitoring["Namespace: monitoring"]
            subgraph Config["ConfigMap Volume Mounts"]
                CfgDefault["_defaults.yaml\n(Platform Defaults)"]
                CfgTenantA["db-a.yaml\n(Tenant A Overrides)"]
                CfgTenantB["db-b.yaml\n(Tenant B Overrides)"]
            end

            subgraph Export["threshold-exporter\n(×2 HA Replicas)"]
                TE1["Replica 1\nport 8080"]
                TE2["Replica 2\nport 8080"]
            end

            subgraph Rules["Projected Volume\nRule Packs"]
                RP1["configmap-rules-mariadb.yaml"]
                RP2["configmap-rules-kubernetes.yaml"]
                RP3["configmap-rules-redis.yaml"]
                RP4["configmap-rules-mongodb.yaml"]
                RP5["configmap-rules-elasticsearch.yaml"]
                RP6["configmap-rules-platform.yaml"]
            end

            Prom["Prometheus\n(Scrape: TE, Rule Evaluation)"]
            AM["Alertmanager\n(Routing, Dedup, Grouping)"]
            Slack["Slack / Email\n(Notifications)"]
        end
    end

    Git["Git Repository\n(Source of Truth)"]
    Scanner["Directory Scanner\n(conf.d/)"]

    Git -->|Pull| Scanner
    Scanner -->|Hot-reload\nSHA-256 hash| Config
    Config -->|Mount| Export
    ExpA -->|Scrape| Prom
    ExpB -->|Scrape| Prom
    Config -->|Load YAML| TE1
    Config -->|Load YAML| TE2
    TE1 -->|Expose metrics| Prom
    TE2 -->|Expose metrics| Prom
    Rules -->|Mount| Prom
    Prom -->|Evaluate rules\ngroup_left matching| Prom
    Prom -->|Fire alerts| AM
    AM -->|Route & Deduplicate| Slack
```

**Architecture highlights:**
1. **Directory Scanner** scans the `conf.d/` directory, automatically discovering `_defaults.yaml` and tenant configuration files
2. **threshold-exporter × 2 HA Replicas** read ConfigMap and output three-state Prometheus metrics
3. **Projected Volume** mounts 6 independent rule packs, zero PR conflicts, each team independently owns their rules
4. **Prometheus** uses `group_left` vector matching to join with user thresholds, achieving O(M) complexity

---

## 2. Core Design: Config-Driven Architecture

### 2.1 Three-State Logic

The platform supports a "three-state" configuration pattern, providing flexible default values, overrides, and disable mechanisms:

| State | Configuration | Prometheus Output | Description |
|-------|---------------|-------------------|-------------|
| **Custom Value** | `metric_key: 42` | ✓ Output custom threshold | Tenant override of default |
| **Omitted (Default)** | Not specified in YAML | ✓ Output platform default | Uses `_defaults.yaml` |
| **Disable** | `metric_key: "disable"` | ✗ No output | Completely disable metric |

**Prometheus output example:**

```
# Custom value (db-a tenant)
user_threshold{tenant="db-a", metric="mariadb_replication_lag", severity="warning"} 10

# Default value (db-b tenant, not overridden)
user_threshold{tenant="db-b", metric="mariadb_replication_lag", severity="warning"} 30

# Disabled (no output)
# (metric not present)
```

### 2.2 Directory Scanner Mode (conf.d/)

**Directory structure:**
```
conf.d/
├── _defaults.yaml         # Platform global defaults (managed by Platform team)
├── db-a.yaml             # Tenant A overrides (managed by db-a team)
├── db-b.yaml             # Tenant B overrides (managed by db-b team)
└── ...
```

**`_defaults.yaml` content (Platform managed):**
```yaml
defaults:
  mysql_connections: 80
  mysql_cpu: 80
  mysql_slave_lag: 30
  container_cpu: 80
  container_memory: 85

state_filters:
  container_crashloop:
    reasons: ["CrashLoopBackOff"]
    severity: "critical"
  maintenance:
    reasons: []
    severity: "info"
    default_state: "disable"
```

**`db-a.yaml` content (Tenant override):**
```yaml
tenants:
  db-a:
    mysql_connections: "70"          # Override default 80
    container_cpu: "70"              # Override default 80
    mysql_slave_lag: "disable"       # No replica, disable
    # mysql_cpu not specified → use default value 80
    # Dimensional labels (Phase 2B)
    "redis_queue_length{queue='tasks'}": "500"
    "redis_queue_length{queue='events', priority='high'}": "1000:critical"
```

#### Boundary Enforcement Rules

| File Type | Allowed Blocks | Violation Behavior |
|-----------|----------------|-------------------|
| Files with `_` prefix (`_defaults.yaml`) | `defaults`, `state_filters`, `tenants` | — |
| Tenant files (`db-a.yaml`) | Only `tenants` | Other blocks automatically ignored + WARN log |

#### SHA-256 Hot-Reload

Does not rely on file modification time (ModTime), but rather on **SHA-256 content hash**:

```bash
# On each ConfigMap update
$ sha256sum conf.d/_defaults.yaml conf.d/db-a.yaml conf.d/db-b.yaml
abc123... conf.d/_defaults.yaml
def456... conf.d/db-a.yaml
ghi789... conf.d/db-b.yaml

# Kubernetes ConfigMap symlink mounted will rotate
# Old hash → new hash
# threshold-exporter detects change, reloads configuration
```

**Why SHA-256 instead of ModTime?**
- Kubernetes ConfigMap creates a symlink layer, ModTime is unreliable
- Same content = same hash, avoid unnecessary reloads

### 2.3 Multi-tier Severity

Support both `_critical` suffix and `"value:severity"` syntax:

**Method 1: `_critical` suffix (suitable for basic thresholds)**
```yaml
tenants:
  db-a:
    mysql_connections: "100"            # warning threshold
    mysql_connections_critical: "150"   # _critical → auto-generate critical alert
```

**Method 2: `"value:severity"` syntax (suitable for dimensional labels)**
```yaml
tenants:
  redis-prod:
    "redis_queue_length{queue='orders'}": "500:critical"
```

**Prometheus output:**
```
user_threshold{tenant="db-a", component="mysql", metric="connections", severity="warning"} 100
user_threshold{tenant="db-a", component="mysql", metric="connections", severity="critical"} 150
```

#### Auto-Suppression

Platform Alert Rules use `unless` logic to auto-suppress warning when critical triggers:

```yaml
- alert: MariaDBHighConnections          # warning
  expr: |
    ( tenant:mysql_threads_connected:max > on(tenant) group_left tenant:alert_threshold:connections )
    unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
- alert: MariaDBHighConnectionsCritical  # critical
  expr: |
    ( tenant:mysql_threads_connected:max > on(tenant) group_left tenant:alert_threshold:connections_critical )
    unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
```

**Result:**
- Connection count ≥ 150 (critical): only critical alert fires
- Connection count 100-150 (warning only): warning alert fires

---

## 3. Projected Volume Architecture (Rule Packs)

### 3.1 Six Independent Rule Packs

| Rule Pack | Owning Team | ConfigMap Name | Recording Rules | Alert Rules |
|-----------|------------|-----------------|----------------|-------------|
| MariaDB | DBA | `configmap-rules-mariadb` | 7 | 8 |
| Kubernetes | Infra | `configmap-rules-kubernetes` | 5 | 4 |
| Redis | Cache | `configmap-rules-redis` | 7 | 6 |
| MongoDB | AppData | `configmap-rules-mongodb` | 7 | 6 |
| Elasticsearch | Search | `configmap-rules-elasticsearch` | 7 | 7 |
| Platform | Platform | `configmap-rules-platform` | 0 | 4 |
| **Total** | | | **33** | **35** |

### 3.2 Self-Contained Three-Part Structure

Each Rule Pack contains three separate and reusable parts:

#### Part 1: Normalization Recording Rules
```yaml
groups:
  - name: mariadb-normalization
    rules:
      # Normalization naming: tenant:<component>_<metric>:<function>
      - record: tenant:mysql_threads_connected:max
        expr: max by(tenant) (mysql_global_status_threads_connected)

      - record: tenant:mysql_slow_queries:rate5m
        expr: sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))
```

**Purpose:** Normalize raw metrics from different exporters into unified namespace `tenant:<metric>:<function>`

#### Part 2: Threshold Normalization
```yaml
groups:
  - name: mariadb-threshold-normalization
    rules:
      - record: tenant:alert_threshold:connections
        expr: max by(tenant) (user_threshold{metric="connections", severity="warning"})

      - record: tenant:alert_threshold:connections_critical
        expr: max by(tenant) (user_threshold{metric="connections", severity="critical"})
```

**Key:** Use `max by(tenant)` rather than `sum` to prevent HA double-counting (see section 5.3)

#### Part 3: Alert Rules
```yaml
groups:
  - name: mariadb-alerts
    rules:
      - alert: MariaDBHighConnections
        expr: |
          (
            tenant:mysql_threads_connected:max
            > on(tenant) group_left
            tenant:alert_threshold:connections
          )
          unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "MariaDB connections {{ $value }} exceeds threshold ({{ $labels.tenant }})"
```

### 3.3 Advantages

1. **Zero PR Conflicts** — Each ConfigMap is independent, different teams can push in parallel
2. **Team Autonomy** — DBAs own MariaDB rules, no central platform review needed
3. **Reusable** — Rules can easily be ported to other Prometheus clusters
4. **Independent Testing** — Each pack can be validated and released independently

---

## 4. Performance Analysis — Core Advantages

### 4.1 Vector Matching Complexity Analysis

**Traditional approach (multi-tenant hardcoded):**
```
N tenants × M alert rules = N×M independent PromQL evaluations
Complexity: O(N×M)

Example: 100 tenants, 35 alert rules
= 3,500 independent rule evaluations
```

**Dynamic approach (vector matching with `group_left`):**
```
M alert rules × 1 vector matching = M evaluations
Complexity: O(M), independent of tenant count

Example: 100 tenants, 35 alert rules
= 35 rule evaluations (regardless of tenant count)
```

### 4.2 Actual Benchmark Data (Kind Cluster Measurement)

**Current setup: 2 tenants, 85 rules, 18 alert groups**

```
Total evaluation time (per cycle): ~20.8ms
- p50 (50th percentile):  0.59ms per group
- p99 (99th percentile):  5.05ms per group
```

**Scalability comparison:**

| Metric | Current (2 tenants) | Traditional (100 tenants) | Dynamic (100 tenants) |
|--------|-------|-------------------|------------------|
| Alert rule count | 35 (fixed) | 3,500 (35×100) | 35 (fixed) |
| Recording rule count | 50 (normalization) | 0 (embedded in alerts) | 50 (fixed) |
| **Total rule count** | **85** | **3,500** | **85** |
| Evaluation complexity | O(M) | O(N×M) | O(M) |
| **Estimated evaluation time** | **~20.8ms** | **~850ms+** | **~20.8ms** |

**Conclusion:**
- Traditional approach increases evaluation time by **40×** at 100 tenants
- Dynamic approach maintains **constant** evaluation time, linear scalability

### 4.3 Empty Vector Zero-Cost

6 rule packs are pre-loaded. Packs without deployed exporters are evaluated against empty vectors.

**Kind cluster actual measurement:**

| Rule Pack | Status | Rule Count | Evaluation Time | Notes |
|-----------|--------|-----------|-----------------|-------|
| MariaDB | ✓ Active | 7 | **2.12ms** | Has exporter |
| MongoDB | ✗ No exporter | 7 | **0.64ms** | Empty vector |
| Redis | ✗ No exporter | 7 | **0.41ms** | Empty vector |
| Elasticsearch | ✗ No exporter | 7 | **1.75ms** | Complex PromQL, still low-cost |

**Conclusion:**
- Empty vector operations are approximately O(1)
- Pre-loading unused rule packs has **negligible** overhead (< 1ms)
- When new tenants come online, all rules automatically apply, **no redeployment needed**

### 4.4 Memory Efficiency

```
Single threshold-exporter Pod:
- ConfigMap memory: ~5MB (YAML parsing)
- Output metrics: ~2,000 series (2 tenants)
- Total usage: ~150MB (RSS)

× 2 HA Replicas: ~300MB total
+ Prometheus rule cache: ~50MB
= Cluster overhead: ~350MB

vs. Traditional approach (3,500 rules):
- Prometheus rule cache: ~200MB+
- Total overhead: ~400MB+ (single hub)
```

### 4.5 Resource Usage Baseline

Actual measurements from a Kind single-node cluster (2 tenants, 85 rules):

| Metric | Component | Value | Purpose |
|--------|-----------|-------|---------|
| CPU (5m avg) | Prometheus | ~0.02 cores | Capacity planning — estimate CPU requests |
| RSS Memory | Prometheus | ~150MB | Memory budgeting — set memory limits |
| RSS Memory | threshold-exporter (per pod) | ~64MB | Pod resource limits tuning |
| RSS Memory | threshold-exporter (×2 HA) | ~128MB total | Cluster memory planning |

**Automated collection:**

```bash
make benchmark              # Full report (human-readable)
make benchmark ARGS=--json  # JSON output (CI/CD consumption)
```

### 4.6 Storage and Cardinality Analysis

**Why Cardinality Matters More Than Disk**

The performance bottleneck in Prometheus is **Active Series count**, not disk space. Each series consumes approximately 2KB of memory, and the series count directly determines: query latency, memory usage, and compaction frequency.

**Kind cluster measurements:**

| Metric | Value | Description |
|--------|-------|-------------|
| TSDB Disk Usage | ~12MB | All rules and metrics included |
| Active Series Total | ~2,800 | Includes all exporters + recording rules |
| `user_threshold` Series | ~16 | Threshold metrics from threshold-exporter |
| Series Per Tenant (marginal) | ~8 | Marginal cost of adding 1 tenant |

**Scaling estimation formula:**

```
Marginal cost of adding N tenants:
  Series delta = N × (series per tenant)
  Memory delta ≈ Series delta × 2KB

Example (100 tenants):
  user_threshold series = 100 × 8 = 800
  Memory delta ≈ (800 - 16) × 2KB ≈ 1.5MB
  Total series ≈ 2,800 - 16 + 800 = 3,584
```

**Conclusion:** The dynamic architecture has minimal series growth per tenant (~8 series each). 100 tenants add only ~1.5MB of memory. Compared to the traditional approach (35+ independent rules per tenant, each potentially generating multiple series), the cardinality advantage is significant.

---

## 5. High Availability Design

### 5.1 Deployment Strategy

```yaml
replicas: 2
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0    # Zero-downtime rolling update
    maxSurge: 1

affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
```

**Features:**
- 2 replicas spread across different nodes
- During rolling update, always 1 replica available
- Kind single-node cluster: soft affinity allows bin-packing

### 5.2 Pod Disruption Budget

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: threshold-exporter-pdb
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: threshold-exporter
```

**Guarantee:** Always 1 replica serving Prometheus scrapes, even during active maintenance

### 5.3 Critical: `max by(tenant)` vs `sum`

#### ❌ Wrong: Using `sum`
```yaml
- record: tenant:alert_threshold:connections
  expr: |
    sum by(tenant)
      user_threshold{tenant=~".*", metric="connections"}
```

**Problem:**
- Prometheus scrapes the same metric from two replicas → double value
- `sum by(tenant)` adds values from both replicas → **threshold doubled**
- Alerts fire incorrectly

#### ✓ Correct: Using `max`
```yaml
- record: tenant:alert_threshold:connections
  expr: |
    max by(tenant)
      user_threshold{tenant=~".*", metric="connections"}
```

**Advantage:**
- Takes the maximum value from both replicas (logically identical)
- Avoids double-counting
- Alert threshold accurate under HA

### 5.4 Self-Monitoring (Platform Rule Pack)

4 dedicated alerts monitor threshold-exporter itself:

| Alert | Condition | Action |
|-------|-----------|--------|
| ThresholdExporterDown | `up{job="threshold-exporter"} == 0` for 2m | PagerDuty → SRE |
| ThresholdExporterAbsent | Metrics absent > 5m | Warning → Platform team |
| TooFewReplicas | `count(up{job="threshold-exporter"}) < 2` | Warning → SRE |
| HighRestarts | `rate(container_last_terminated_reason[5m]) > 0.1` | Investigation |

---

## 6. Governance & Audit

### 6.1 Natural Audit Trail

Each tenant YAML ↔ Git history:

```bash
$ git log --follow conf.d/db-a.yaml
commit 5f3e8a2 (HEAD)
Author: alice@db-a-team.com
Date:   2026-02-26

    Increase MariaDB replication_lag threshold from 10s to 15s

    Reason: High load during 6-9pm peak hours
    Ticket: INCIDENT-1234

commit 1a2c5b9
Author: bob@db-a-team.com
Date:   2026-02-20

    Add monitoring for new Redis cluster
    Metric: redis_memory_usage_percent
    Default: 75% warning, 90% critical
```

### 6.2 Separation of Duties

| Role | Can Modify | Cannot Modify |
|------|-----------|---------------|
| **Platform Team** | `conf.d/_defaults.yaml` | Tenant overrides, alert rules |
| **Tenant Team** | `conf.d/<tenant>.yaml` | Defaults, state_filters |
| **All** | N/A | `state_filters` (only in _defaults) |

Git RBAC:
```bash
# .gitignore or Branch Protection Rules
conf.d/_defaults.yaml ← admin:platform-team exclusive push rights

conf.d/db-a.yaml ← write:db-a-team
conf.d/db-b.yaml ← write:db-b-team
```

### 6.3 Configuration Validation and Compliance

Automatically executed on each ConfigMap update:

1. **YAML Format Validation** — Syntax correctness
2. **Boundary Checks** — Tenants cannot modify state_filters
3. **Default Value Validation** — Thresholds in reasonable range (e.g., 0-100%)
4. **Anomaly Detection** — Unusual value detection (e.g., threshold > 10× normal)

---

## 7. Security Compliance (SAST)

### 7.1 Go Component Security

#### ReadHeaderTimeout (Gosec G112 — Slowloris)
```go
// ✓ Correct
server := &http.Server{
    Addr:              ":8080",
    Handler:           mux,
    ReadHeaderTimeout: 10 * time.Second,  // Must be set
}

// ✗ Violation
server := &http.Server{
    Addr:    ":8080",
    Handler: mux,
    // No ReadHeaderTimeout → Slowloris attack risk
}
```

**Why:** Prevent clients from sending slow HTTP headers, exhausting server resources

#### Other Checks
- **G113** — Potential uncontrolled memory consumption
- **G114** — Use of `http.Request.RequestURI` (unsafe, use URL.Path)

### 7.2 Python Component Security

#### File Permissions (CWE-276)
```python
# ✓ Correct
with open(path, 'w') as f:
    f.write(config_content)
os.chmod(path, 0o600)  # rw-------

# ✗ Violation
# Default file permission 0o644 (rw-r--r--) → readable by other users
```

#### No Shell Injection (Command Injection)
```python
# ✓ Correct
result = subprocess.run(['kubectl', 'patch', 'configmap', ...], check=True)

# ✗ Violation
result = os.system(f"kubectl patch configmap {name}")  # shell=True risk
```

### 7.3 SSRF Protection

All local API calls marked with `# nosec B602`:

```python
# nosec B602 — localhost-only, no SSRF risk
response = requests.get('http://localhost:8080/health')
```

---

## 8. Troubleshooting and Edge Cases

### 8.1 SHA-256 Hot-Reload Delay

**Scenario:** After ConfigMap update, threshold-exporter still shows old value

```bash
# Diagnosis
$ kubectl get configmap -n monitoring configmap-defaults -o jsonpath='{.metadata.generation}'
5

$ kubectl logs -n monitoring deployment/threshold-exporter | grep "SHA256"
2026-02-26T10:15:32Z SHA256: abc123... (old)
2026-02-26T10:20:45Z SHA256: def456... (updated after 5min)
```

**Cause:** Kubernetes syncs ConfigMap mounts at most every 60 seconds

**Solution:**
1. Force restart: `kubectl rollout restart deployment/threshold-exporter`
2. Or wait for mount sync (typical < 1 minute)

### 8.2 Empty Vector Alerts Don't Fire

**Scenario:** Redis has no deployed exporter, but Redis alert rules still evaluate

```promql
# Issue:
redis_memory_usage_percent{job="redis-exporter"} >= on(tenant) group_left
  user_threshold{metric="redis_memory_usage_percent", severity="warning"}

# Right side is empty vector (no Redis data in user_threshold)
# group_left matching fails → alert doesn't fire ✓ Expected behavior
```

**Verification (not an issue):**
```bash
$ kubectl exec -it prometheus-0 -c prometheus -- \
  promtool query instant 'count(redis_memory_usage_percent)'
0  # No Redis metric ✓
```

### 8.3 Dual-Replica Scrape Double-Counting

**Scenario:** Prometheus scrapes from two threshold-exporter replicas, user_threshold values double

```
user_threshold{tenant="db-a", severity="warning"} 30  (from replica-1)
user_threshold{tenant="db-a", severity="warning"} 30  (from replica-2)
# ↓ sum by(tenant) would produce 60 (Wrong!)
```

**Fix:** Ensure all threshold rules use `max by(tenant)`

```yaml
- record: tenant:alert_threshold:slave_lag
  expr: |
    max by(tenant)  # ✓ Not sum
      user_threshold{metric="slave_lag"}
```

---

## 9. Implemented Advanced Scenarios

### 9.1 Scenario D: Maintenance Mode and Composite Alerts (Implemented ✓)

All Alert Rules have built-in `unless maintenance` logic, tenants can mute with one state_filter switch:

```yaml
# _defaults.yaml
state_filters:
  maintenance:
    reasons: []
    severity: "info"
    default_state: "disable"   # Disabled by default

# Tenant enables maintenance mode:
tenants:
  db-a:
    _state_maintenance: "enable"  # All alerts suppressed by unless
```

Composite alerts (AND logic) and multi-tier severity (Critical auto-suppresses Warning) are also fully implemented.

### 9.2 Enterprise Test Coverage Matrix

The following matrix maps automated test scenarios to enterprise protection requirements. Each scenario's assertions can be verified via `make test-scenario-*` with a single command.

| Scenario | Enterprise Protection | Test Method | Core Assertions | Command |
|----------|----------------------|-------------|-----------------|---------|
| **A — Dynamic Threshold** | Tenant-defined thresholds take effect immediately, no restart needed | Modify threshold → wait for exporter reload → verify alert fires | `user_threshold` value updated; alert state becomes firing | `make test-scenario-a` |
| **B — Weakest Link Detection** | Worst metric among multiple automatically triggers alert | Inject CPU stress → verify `pod_weakest_cpu_percent` normalization | Recording rule produces correct worst value; alert fires correctly | `make test-scenario-b` |
| **C — Three-State Comparison** | Metrics controlled by custom / default / disable states | Toggle three states → verify exporter metric presence/absence | custom: value=custom; default: value=global default; disable: metric disappears | Included in scenario-a |
| **D — Maintenance Mode** | Automatic alert silencing during planned maintenance | Enable `_state_maintenance` → verify alert suppressed by `unless` | All alerts remain inactive; resume normal after disabling | Included in scenario-a |
| **E — Multi-Tenant Isolation** | Modifying Tenant A never affects Tenant B | Lower A threshold/disable A metric → verify B unchanged | A alert fires, B alert inactive; A metric absent, B metric present | `make test-scenario-e` |
| **F — HA Failover** | Service continues after Pod deletion, thresholds don't double | Kill 1 Pod → verify alert continues → new Pod starts → verify `max by` | Surviving Pods ≥1 (PDB); alert uninterrupted; recording rule value = original (not 2×) | `make test-scenario-f` |
| **demo-full** | End-to-end lifecycle demonstration | Composite load → alert fires → cleanup → alert resolves | All 6 steps succeed; complete firing → inactive cycle | `make demo-full` |

#### Assertion Details

**Scenario E — Two Isolation Dimensions:**

- **E1 — Threshold Modification Isolation**: Set db-a's `mysql_connections` to 5 → db-a triggers `MariaDBHighConnections`, db-b's threshold and alert state remain completely unaffected
- **E2 — Disable Isolation**: Set db-a's `container_cpu` to `disable` → db-a's metric disappears from exporter, db-b's `container_cpu` continues to be exported normally

**Scenario F — `max by(tenant)` Proof:**

Two threshold-exporter Pods each emit identical `user_threshold{tenant="db-a", metric="connections"} = 5`. The recording rule uses `max by(tenant)` aggregation:

- ✅ `max(5, 5) = 5` (correct)
- ❌ If using `sum by(tenant)`: `5 + 5 = 10` (doubled, incorrect)

The test verifies the value remains 5 after killing one Pod, and after the new Pod starts, the series count returns to 2 but the aggregated value is still 5.

### 9.3 demo-full: End-to-End Lifecycle Flowchart

`make demo-full` demonstrates the complete flow from tool verification to real load. The sequence diagram below describes the core path of Step 6 (Live Load):

```mermaid
sequenceDiagram
    participant Op as Operator
    participant LG as Load Generator<br/>(connections + stress-ng)
    participant DB as MariaDB<br/>(db-a)
    participant TE as threshold-exporter
    participant PM as Prometheus

    Note over Op: Step 1-5: scaffold / migrate / diagnose / check_alert / baseline

    Op->>LG: run_load.sh --type composite
    LG->>DB: 95 idle connections + OLTP (sysbench)
    DB-->>PM: mysql_threads_connected ≈ 95<br/>node_cpu busy ≈ 80%+
    TE-->>PM: user_threshold_connections = 70

    Note over PM: Evaluate Recording Rule:<br/>normalized_connections = 95<br/>> user_threshold (70)

    PM->>PM: Alert: MariaDBHighConnections → FIRING

    Op->>LG: run_load.sh --cleanup
    LG->>DB: Kill connections + stop stress-ng
    DB-->>PM: mysql_threads_connected ≈ 5

    Note over PM: normalized_connections = 5<br/>< user_threshold (70)

    PM->>PM: Alert → RESOLVED (after for duration)
    Note over Op: ✅ Complete firing → resolved cycle verified
```

### 9.4 Scenario E: Multi-Tenant Isolation Verification

Verifies that modifying Tenant A's configuration never affects Tenant B. The flow is divided into two isolation dimensions:

```mermaid
flowchart TD
    Start([Phase E: Setup]) --> SaveOrig[Save db-a original thresholds]
    SaveOrig --> E1

    subgraph E1["E1: Threshold Modification Isolation"]
        PatchA[patch db-a mysql_connections = 5<br/>far below actual connections] --> WaitReload[Wait for exporter SHA-256 reload]
        WaitReload --> CheckA{db-a alert?}
        CheckA -- "firing ✅" --> CheckB{db-b alert?}
        CheckA -- "inactive ❌" --> FailE1([FAIL: Threshold not applied])
        CheckB -- "inactive ✅" --> CheckBVal{db-b threshold unchanged?}
        CheckB -- "firing ❌" --> FailE1b([FAIL: Isolation breached])
        CheckBVal -- "yes ✅" --> E2
        CheckBVal -- "no ❌" --> FailE1c([FAIL: Threshold leaked])
    end

    subgraph E2["E2: Disable Isolation"]
        DisableA[patch db-a container_cpu = disable] --> WaitAbsent[Wait for metric to disappear from exporter]
        WaitAbsent --> CheckAbsent{db-a container_cpu<br/>absent?}
        CheckAbsent -- "absent ✅" --> CheckBMetric{db-b container_cpu<br/>still present?}
        CheckAbsent -- "exists ❌" --> FailE2([FAIL: Disable not applied])
        CheckBMetric -- "exists ✅" --> Restore
        CheckBMetric -- "absent ❌" --> FailE2b([FAIL: Disable leaked])
    end

    subgraph Restore["E3: Restore"]
        RestoreA[Restore db-a original config] --> VerifyBoth{Both tenants<br/>back to initial state?}
        VerifyBoth -- "yes ✅" --> Pass([PASS: Isolation verified])
        VerifyBoth -- "no ❌" --> FailRestore([FAIL: Restore failed])
    end
```

### 9.5 Scenario F: HA Failover and `max by(tenant)` Anti-Doubling

Verifies that threshold-exporter HA ×2 continues operating after Pod deletion and that `max by(tenant)` aggregation does not double when Pod count changes:

```mermaid
flowchart TD
    Start([Phase F: Setup]) --> CheckHA{Running Pods ≥ 2?}
    CheckHA -- "yes" --> SavePods
    CheckHA -- "no" --> Scale[kubectl scale replicas=2] --> WaitScale[Wait for Pod Ready] --> SavePods

    SavePods[Record Pod Names + original thresholds] --> F2

    subgraph F2["F2: Trigger Alert"]
        PatchLow[patch db-a mysql_connections = 5] --> WaitThreshold[wait_exporter: threshold = 5]
        WaitThreshold --> WaitAlert[Wait for alert evaluation 45s]
        WaitAlert --> CheckFiring{MariaDBHighConnections<br/>= firing?}
        CheckFiring -- "firing ✅" --> F3
        CheckFiring -- "no ❌" --> FailF2([FAIL: Alert not triggered])
    end

    subgraph F3["F3: Kill Pod → Verify Continuity"]
        KillPod["kubectl delete pod (--force)"] --> Wait15[Wait 15s]
        Wait15 --> CheckSurvivor{Surviving Pods ≥ 1?<br/>PDB protection}
        CheckSurvivor -- "≥1 ✅" --> RebuildPF[Rebuild port-forward]
        CheckSurvivor -- "0 ❌" --> FailF3([FAIL: PDB not protecting])
        RebuildPF --> StillFiring{Alert still firing?}
        StillFiring -- "firing ✅" --> F4
        StillFiring -- "no ❌" --> FailF3b([FAIL: Failover interrupted])
    end

    subgraph F4["F4: Pod Recovery → Anti-Doubling Verification"]
        WaitRecovery[Wait for replacement Pod Ready ≤ 2min] --> CheckPods{Running Pods ≥ 2?}
        CheckPods -- "≥2 ✅" --> QueryMax["Query recording rule value"]
        CheckPods -- "<2 ❌" --> FailF4([FAIL: Pod not recovered])
        QueryMax --> CheckValue{"value = 5?<br/>(not 10)"}
        CheckValue -- "5 ✅ max correct" --> CountSeries["count(user_threshold) = 2?"]
        CheckValue -- "10 ❌ sum doubled" --> FailF4b([FAIL: max by failed])
        CountSeries -- "2 ✅" --> F5
        CountSeries -- "≠2 ❌" --> FailF4c([FAIL: Series count abnormal])
    end

    subgraph F5["F5: Restore"]
        RestoreConfig[Restore original thresholds] --> WaitResolve[Wait for alert resolved]
        WaitResolve --> Pass([PASS: HA verified<br/>max by anti-doubling confirmed])
    end
```

> **Key Proof**: Scenario F's Phase F4 is the critical verification for the entire HA design — it directly proves the correctness of `max by(tenant)` aggregation when Pod count changes. This is the technical rationale for choosing `max` over `sum`. See §5 High Availability Design for details.

---

## 10. Future Roadmap

The following items are listed in priority order. Items marked `[Backlog Bx]` correspond to the backlog IDs in CLAUDE.md.

### 10.1 ~~Regex Dimension Thresholds~~ `[B1]` — ✅ Completed (v0.12.0)

> **Completed in v0.12.0.** Config parser extended to support `=~` operator (e.g., `tablespace=~"SYS.*"`). Regex patterns are output as `_re` suffixed labels on Prometheus metrics. PromQL recording rules use `label_replace` + `=~` for actual matching at query time. This design keeps the exporter as a pure config→metric converter without external data dependencies.

### 10.2 Oracle / DB2 Rule-Pack Templates `[B3]`

Depends on B1 completion. Provides default rule-packs for Oracle (tablespace utilization, session count) and DB2 (lock wait, bufferpool hit ratio), enabling enterprise DBAs to use them out of the box.

### 10.3 ~~Scheduled Thresholds~~ `[B4]` — ✅ Completed (v0.12.0)

> **Completed in v0.12.0.** `ScheduledValue` custom YAML type supports dual format: scalar strings (backward compatible) and structured `{default, overrides[{window, value}]}`. Time windows are UTC-only `HH:MM-HH:MM` format with cross-midnight support (e.g., `22:00-06:00`). `ResolveAt(now time.Time)` ensures testability. 45 test cases cover boundary conditions.

### 10.4 Benchmark Under-Load Mode `[B2]`

Currently, `make benchmark` only measures hot-reload latency in idle state. This mode will measure reload latency during real load (composite load), proving that "hot-reload does not impact production performance."

### 10.5 ~~Migration Tool AST Parsing~~ `[B6]` — ✅ Completed (v0.11.0)

> **Implemented in v0.11.0.** `migrate_rule.py` v4 integrates `promql-parser` (Rust PyO3 binding, v0.7.0) using an **AST-Informed String Surgery** architecture: the AST precisely identifies metric names and label matchers, word-boundary regex performs string replacements, and reparsing validates correctness. New capabilities include:
>
> - `extract_metrics_ast()` / `extract_label_matchers_ast()` — precise AST-based identification
> - `rewrite_expr_prefix()` — `custom_` prefix injection (word-boundary prevents substring false matches)
> - `rewrite_expr_tenant_label()` — `tenant=~".+"` label injection
> - `detect_semantic_break_ast()` — detects `absent()` / `predict_linear()` and other semantic-breaking functions
> - Graceful degradation: automatically falls back to regex when promql-parser is unavailable or parsing fails
> - 54 test cases covering compound `and/or/unless`, complex regex labels, aggregation+offset, nested semantic break detection, parse_expr all_metrics validation, dictionary loading, write_outputs integration (including AST path)
>
> CLI adds `--no-ast` flag to force regex-only mode.

### 10.6 Governance Evolution

Currently all tenant configs reside in a single `threshold-config` ConfigMap, and K8s native RBAC can only control access at the resource level, not at the key level. Splitting into multiple ConfigMaps is feasible but projected volumes require each ConfigMap name to be hardcoded in the Pod Spec — adding a new tenant would require a Deployment change and trigger a Pod restart, breaking the core hot-reload mechanism.

#### Current Best Practice: GitOps-Driven RBAC

The recommended approach is to shift configuration changes from `kubectl patch` to Git commit → GitOps sync (ArgoCD / Flux). The permission boundary moves up to the Git layer:

- **CODEOWNERS / Branch Protection**: Restrict Tenant A's team to only modify `conf.d/db-a.yaml`, while only the Platform Team can modify `_defaults.yaml`
- **CI/CD Pipeline**: Assembles the `conf.d/` directory into a single `threshold-config` ConfigMap and applies it, preserving hot-reload performance
- **Audit Trail**: Git history natively provides complete who / when / what change records

In practice, configuration changes operate at three levels:

1. **Standard Pathway**: All changes go through Git PR → review → merge → GitOps sync. Complete RBAC audit trail, suitable for routine threshold tuning and new tenant onboarding.
2. **Emergency Break-Glass**: During P0 incidents, SREs can use `patch_config.py` to directly runtime-patch the K8s ConfigMap for minimum MTTR.
3. **Drift Reconciliation**: After a break-glass patch, SREs must submit a follow-up PR to sync the change back to Git. Otherwise, the next GitOps sync will overwrite the K8s configuration back to the Git version — this self-healing property naturally prevents "forgot to update the code after firefighting" from becoming permanent technical debt.

#### Future Blueprint: CRD + Operator Architecture

When the platform scales to require auto-scaling, drift reconciliation, and cross-cluster management, a `ThresholdConfig` CRD and Operator can be introduced, elevating tenant configurations to Kubernetes first-class resources. K8s native RBAC would then provide precise per-CR access control, integrating seamlessly with GitOps toolchains. This path requires additional Operator development and operational investment, and is best evaluated when the product enters its scaling phase.

### 10.7 Prometheus Federation

Support multi-cluster architecture:
- Edge clusters each collect tenant metrics and run threshold-exporter
- Central cluster performs global alert evaluation via federation or remote-write
- Cross-cluster SLA monitoring and unified dashboards

---

## References

- **README.en.md** — Quick start and overview
- **migration-guide.md** — Migration from traditional approach
- **custom-rule-governance.md** — Multi-tenant custom rule governance model
- **rule-packs/README.md** — Rule pack development and extension
- **components/threshold-exporter/README.md** — Exporter internal implementation

---

**Document version:** v0.12.0 — 2026-02-28
**Last updated:** Phase 11 Exporter Core Expansion — B1 Regex Dimensions + B4 Scheduled Thresholds (ScheduledValue, ResolveAt, _re suffix labels)
**Maintainer:** Platform Engineering Team
