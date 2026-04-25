#!/usr/bin/env python3
"""Synthetic tenant fixture generator — produce N-tenant conf.d/ for benchmark & integration testing.

Usage:
    python3 scripts/tools/dx/generate_tenant_fixture.py --count 100 --output /tmp/fixture-100
    python3 scripts/tools/dx/generate_tenant_fixture.py --count 1000 --layout hierarchical
    python3 scripts/tools/dx/generate_tenant_fixture.py --count 2000 --layout flat --with-defaults
    python3 scripts/tools/dx/generate_tenant_fixture.py --count 500 --layout hierarchical --with-defaults

Supported counts: any positive integer; common benchmarks are 100, 500, 1000, 2000.

Layout modes:
    flat         — All tenant YAML files in a single conf.d/ directory (legacy pattern)
    hierarchical — Grouped by domain/region/env subdirectories with _defaults.yaml at each level

The --with-defaults flag injects _defaults.yaml files with platform-wide threshold defaults.
In hierarchical mode, _defaults.yaml is placed at domain and env levels.
In flat mode, a single _defaults.yaml is placed at the conf.d/ root.
"""
import argparse
import hashlib
import math
import os
import random
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DOMAINS = ["finance", "logistics", "healthcare", "retail", "media", "infra", "analytics", "iot"]
REGIONS = ["us-east", "us-west", "eu-central", "eu-west", "ap-northeast", "ap-southeast"]
ENVIRONMENTS = ["prod", "staging", "dev"]
DB_TYPES = ["postgresql", "mariadb", "redis", "mongodb"]
TIERS = ["gold", "silver", "bronze"]
OWNERS = [
    "platform-team", "dba-team", "sre-team", "app-team-alpha",
    "app-team-bravo", "data-engineering", "ml-platform", "security-ops",
]
RECEIVER_TYPES = ["webhook", "slack", "email", "teams", "pagerduty"]

# Metric templates for threshold generation
METRIC_TEMPLATES = {
    "postgresql": [
        "pg_stat_activity_count",
        "pg_replication_lag_seconds",
        "pg_database_size_bytes",
        "pg_locks_count",
        "pg_stat_bgwriter_buffers_alloc",
    ],
    "mariadb": [
        "mysql_global_status_threads_connected",
        "mysql_global_status_slow_queries",
        "mysql_global_status_aborted_connects",
        "mysql_slave_status_seconds_behind_master",
    ],
    "redis": [
        "redis_memory_used_bytes",
        "redis_connected_clients",
        "redis_rejected_connections_total",
        "redis_keyspace_hits_total",
    ],
    "mongodb": [
        "mongodb_connections_current",
        "mongodb_opcounters_query",
        "mongodb_mem_resident",
        "mongodb_repl_oplog_window_seconds",
    ],
}

# Routing profiles
ROUTING_PROFILES = ["default", "high-priority", "noc-escalation", "silent-hours", "business-hours-only"]


def _seed_rng(seed: int) -> random.Random:
    """Create a seeded RNG for reproducible fixtures."""
    return random.Random(seed)


def _gen_threshold_value(rng: random.Random) -> str:
    """Generate a random threshold value in various formats."""
    fmt = rng.choice(["scalar", "scalar", "scalar", "severity", "disable"])
    if fmt == "scalar":
        return str(rng.randint(50, 100000))
    elif fmt == "severity":
        val = rng.randint(100, 50000)
        sev = rng.choice(["warning", "critical"])
        return f"{val}:{sev}"
    else:
        return "disable"


def _gen_scheduled_threshold(rng: random.Random) -> dict:
    """Generate a scheduled threshold with time-window overrides."""
    return {
        "default": _gen_threshold_value(rng),
        "overrides": [
            {
                "window": f"{rng.randint(0, 12):02d}:00-{rng.randint(13, 23):02d}:00",
                "value": _gen_threshold_value(rng),
            }
        ],
    }


def _gen_dimensional_key(rng: random.Random, metric: str) -> str:
    """Generate a dimensional threshold key like metric{label='value'}."""
    labels = ["db", "queue", "table", "schema", "cluster"]
    label = rng.choice(labels)
    if rng.random() < 0.3:
        return f'{metric}{{{label}=~"temp.*"}}'
    else:
        val = rng.choice(["orders", "users", "payments", "inventory", "sessions", "logs"])
        return f'{metric}{{{label}="{val}"}}'


def _gen_receiver(rng: random.Random, tenant_id: str) -> dict:
    """Generate a routing receiver configuration."""
    rtype = rng.choice(RECEIVER_TYPES)
    base = {"type": rtype}
    if rtype == "webhook":
        base["url"] = f"https://hooks.example.com/{tenant_id}/alerts"
        base["send_resolved"] = rng.choice([True, False])
    elif rtype == "slack":
        base["api_url"] = "https://hooks.slack.com/services/T00/B00/xxxx"
        base["channel"] = f"#alerts-{tenant_id}"
        base["send_resolved"] = True
    elif rtype == "email":
        base["to"] = [f"alerts-{tenant_id}@example.com"]
        base["smarthost"] = "smtp.example.com:587"
        base["from"] = "alerting@example.com"
    elif rtype == "teams":
        base["webhook_url"] = f"https://outlook.office.com/webhook/{tenant_id}"
        base["send_resolved"] = True
    elif rtype == "pagerduty":
        base["routing_key"] = hashlib.md5(tenant_id.encode()).hexdigest()[:24]
        base["severity"] = "critical"
    return base


def _gen_routing(rng: random.Random, tenant_id: str) -> dict:
    """Generate routing configuration for a tenant."""
    routing: dict[str, Any] = {
        "receiver": _gen_receiver(rng, tenant_id),
        "group_by": rng.sample(["alertname", "severity", "db_type", "namespace"], k=rng.randint(1, 3)),
        "group_wait": f"{rng.choice([30, 60, 120])}s",
        "group_interval": f"{rng.choice([1, 3, 5])}m",
        "repeat_interval": f"{rng.choice([1, 4, 12, 24])}h",
    }
    # 20% chance of override
    if rng.random() < 0.2:
        routing["overrides"] = [
            {
                "alertname": rng.choice(["HighMemoryUsage", "ReplicationLag", "SlowQueries"]),
                "receiver": _gen_receiver(rng, tenant_id),
            }
        ]
    return routing


def _gen_tenant_config(rng: random.Random, tenant_id: str, db_type: str) -> dict:
    """Generate a single tenant's configuration."""
    config: dict[str, Any] = {}

    # _metadata
    config["_metadata"] = {
        "owner": rng.choice(OWNERS),
        "tier": rng.choice(TIERS),
        "environment": rng.choice(ENVIRONMENTS),
        "region": rng.choice(REGIONS),
        "domain": rng.choice(DOMAINS),
        "db_type": db_type,
        "tags": rng.sample(["critical-path", "batch-job", "real-time", "legacy", "migration"], k=rng.randint(1, 3)),
    }

    # _namespaces
    config["_namespaces"] = [f"ns-{tenant_id}"]
    if rng.random() < 0.15:
        config["_namespaces"].append(f"ns-{tenant_id}-secondary")

    # _routing_profile (30% chance)
    if rng.random() < 0.3:
        config["_routing_profile"] = rng.choice(ROUTING_PROFILES)

    # _routing (70% chance — some inherit from profile)
    if rng.random() < 0.7:
        config["_routing"] = _gen_routing(rng, tenant_id)

    # Thresholds — pick 2-5 metrics from the db_type's pool
    metrics = METRIC_TEMPLATES.get(db_type, METRIC_TEMPLATES["postgresql"])
    chosen_metrics = rng.sample(metrics, k=min(rng.randint(2, 5), len(metrics)))
    for m in chosen_metrics:
        # 80% scalar, 15% scheduled, 5% dimensional
        r = rng.random()
        if r < 0.80:
            config[m] = _gen_threshold_value(rng)
        elif r < 0.95:
            config[m] = _gen_scheduled_threshold(rng)
        else:
            config[_gen_dimensional_key(rng, m)] = _gen_threshold_value(rng)

    # Operational modes (sparse)
    if rng.random() < 0.05:
        config["_silent_mode"] = rng.choice(["warning", "critical", "all"])
    if rng.random() < 0.03:
        config["_state_maintenance"] = {
            "target": "enable",
            "expires": "2026-12-31T00:00:00Z",
            "reason": "Scheduled maintenance window",
        }
    if rng.random() < 0.1:
        config["_severity_dedup"] = rng.choice(["auto", "manual", "disable"])

    return config


def _gen_defaults_yaml(rng: random.Random, db_types: list[str] | None = None) -> dict:
    """Generate a _defaults.yaml content."""
    defaults: dict[str, Any] = {"defaults": {}}
    all_db_types = db_types or DB_TYPES
    for dbt in all_db_types:
        metrics = METRIC_TEMPLATES.get(dbt, [])
        for m in metrics[:3]:  # top 3 metrics per db type as defaults
            defaults["defaults"][m] = _gen_threshold_value(rng)
    return defaults


def generate_flat(count: int, output_dir: Path, with_defaults: bool, seed: int) -> None:
    """Generate a flat conf.d/ layout."""
    rng = _seed_rng(seed)
    output_dir.mkdir(parents=True, exist_ok=True)

    if with_defaults:
        _write_yaml(output_dir / "_defaults.yaml", _gen_defaults_yaml(rng))

    for i in range(count):
        db_type = DB_TYPES[i % len(DB_TYPES)]
        domain = DOMAINS[i % len(DOMAINS)]
        tid = f"{domain}-{db_type}-{i:04d}"
        tenant_config = {"tenants": {tid: _gen_tenant_config(rng, tid, db_type)}}
        _write_yaml(output_dir / f"{tid}.yaml", tenant_config)

    print(f"✅ Generated {count} tenant files (flat) in {output_dir}")


def generate_hierarchical(count: int, output_dir: Path, with_defaults: bool, seed: int) -> None:
    """Generate a hierarchical conf.d/ layout: domain/region/env/tenant.yaml."""
    rng = _seed_rng(seed)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Distribute tenants across domain/region/env
    slots: list[tuple[str, str, str]] = []
    for d in DOMAINS:
        for r in REGIONS:
            for e in ENVIRONMENTS:
                slots.append((d, r, e))

    tenants_per_slot = max(1, count // len(slots))
    remainder = count - tenants_per_slot * len(slots)

    idx = 0
    domain_db_types: dict[str, list[str]] = {}
    for slot_i, (domain, region, env) in enumerate(slots):
        if idx >= count:
            break
        n = tenants_per_slot + (1 if slot_i < remainder else 0)
        slot_dir = output_dir / domain / region / env
        slot_dir.mkdir(parents=True, exist_ok=True)

        for j in range(n):
            if idx >= count:
                break
            db_type = DB_TYPES[idx % len(DB_TYPES)]
            tid = f"{domain}-{db_type}-{idx:04d}"
            tenant_config = {"tenants": {tid: _gen_tenant_config(rng, tid, db_type)}}
            # Override metadata to match directory
            tenant_config["tenants"][tid]["_metadata"]["domain"] = domain
            tenant_config["tenants"][tid]["_metadata"]["region"] = region
            tenant_config["tenants"][tid]["_metadata"]["environment"] = env
            _write_yaml(slot_dir / f"{tid}.yaml", tenant_config)
            domain_db_types.setdefault(domain, []).append(db_type)
            idx += 1

    # Inject _defaults.yaml at domain and env levels
    if with_defaults:
        _write_yaml(output_dir / "_defaults.yaml", _gen_defaults_yaml(rng))
        for domain in DOMAINS:
            domain_dir = output_dir / domain
            if domain_dir.exists():
                db_types_in_domain = list(set(domain_db_types.get(domain, DB_TYPES[:2])))
                _write_yaml(domain_dir / "_defaults.yaml", _gen_defaults_yaml(rng, db_types_in_domain))

    print(f"✅ Generated {idx} tenant files (hierarchical) in {output_dir}")
    print(f"   Structure: {len(DOMAINS)} domains × {len(REGIONS)} regions × {len(ENVIRONMENTS)} envs")


def _zipfian_sizes(count: int, alpha: float, max_size: int, rng: random.Random) -> list[int]:
    """Sample `count` integers from a discrete Zipf-like distribution.

    Returns a list of "tenant size multipliers" in the range [1, max_size]
    drawn so that low values dominate (most tenants are small) and high
    values are rare (few tenants are large). `alpha` controls skew; higher
    alpha = sharper drop-off. Synthetic-v2 uses alpha=1.5 by design choice
    (per phase-b-e2e-harness §6.2): close to real ops distribution where
    a small fraction of tenants accumulate disproportionate threshold
    overrides while the long tail is near-default.

    Implementation note: uses the inverse-CDF method on a normalized
    discrete distribution rather than `random.zipfian` (3.12+) so this
    keeps working on older Python in CI. We cap by max_size to bound
    test fixture YAML size — true Zipf has unbounded support.
    """
    weights = [1.0 / ((i + 1) ** alpha) for i in range(max_size)]
    total = sum(weights)
    cdf = []
    running = 0.0
    for w in weights:
        running += w / total
        cdf.append(running)
    out = []
    for _ in range(count):
        u = rng.random()
        # bisect-style: find smallest index with cdf >= u
        for size_idx, threshold in enumerate(cdf):
            if u <= threshold:
                out.append(size_idx + 1)
                break
        else:
            out.append(max_size)
    return out


def _power_law_depths(count: int, alpha: float, max_depth: int, rng: random.Random) -> list[int]:
    """Sample `count` integers in [0, max_depth] from a power-law tail.

    Returns _metadata block depth multipliers — most tenants get 0
    (flat metadata), a long tail get nested overlays. Distinct from
    Zipf because here the mass is concentrated at 0; only rare tenants
    deserve depth >= 1. `alpha` higher = more flat tenants. Synthetic-v2
    uses alpha=2.0 by default (long-tail per phase-b-e2e-harness §6.2).
    """
    out = []
    for _ in range(count):
        # Sample depth from power-law: P(d) ∝ 1 / (d+1)^alpha for d in [0, max_depth].
        # Inverse-CDF closed form is messy; just sample-reject from CDF table.
        weights = [1.0 / ((d + 1) ** alpha) for d in range(max_depth + 1)]
        total = sum(weights)
        u = rng.random()
        running = 0.0
        for d, w in enumerate(weights):
            running += w / total
            if u <= running:
                out.append(d)
                break
        else:
            out.append(max_depth)
    return out


def generate_synthetic_v2(count: int, output_dir: Path, with_defaults: bool, seed: int) -> None:
    """Generate a synthetic-v2 hierarchical fixture with skewed distributions.

    Layered on top of `generate_hierarchical` (same domain/region/env tree)
    but with two realistic-ops skews per phase-b-e2e-harness design §6.2:

      1. Zipfian tenant size — most tenants override 1-2 thresholds; a
         small fraction (~5-10%) override 5-10. Implementation: sample
         a "size multiplier" in [1, 6] via Zipf alpha=1.5 and replicate
         the threshold dict that many times (up to the available metric
         template count) to vary YAML body size.
      2. Power-law _metadata overlay depth — most tenants have flat
         metadata (depth=0); a long tail (~5%) get depth 1-3 nested
         overlay blocks (e.g. nested escalation policies, inherited
         silence schedules). Implementation: sample depth in [0, 3]
         via power-law alpha=2.0.

    Why these specific distributions: they push the inheritance graph
    + merged_hash compute through a non-uniform input that better
    surfaces tail-latency in the e2e harness measurement. Uniform
    fixtures (synthetic-v1, PR #59) hide variance because every tenant
    looks alike.
    """
    rng = _seed_rng(seed)
    output_dir.mkdir(parents=True, exist_ok=True)

    slots: list[tuple[str, str, str]] = []
    for d in DOMAINS:
        for r in REGIONS:
            for e in ENVIRONMENTS:
                slots.append((d, r, e))

    tenants_per_slot = max(1, count // len(slots))
    remainder = count - tenants_per_slot * len(slots)

    # Sample skew distributions up-front for the full tenant set.
    sizes = _zipfian_sizes(count, alpha=1.5, max_size=6, rng=rng)
    depths = _power_law_depths(count, alpha=2.0, max_depth=3, rng=rng)

    idx = 0
    domain_db_types: dict[str, list[str]] = {}
    for slot_i, (domain, region, env) in enumerate(slots):
        if idx >= count:
            break
        n = tenants_per_slot + (1 if slot_i < remainder else 0)
        slot_dir = output_dir / domain / region / env
        slot_dir.mkdir(parents=True, exist_ok=True)

        for j in range(n):
            if idx >= count:
                break
            db_type = DB_TYPES[idx % len(DB_TYPES)]
            tid = f"{domain}-{db_type}-{idx:04d}"
            tenant_config = {"tenants": {tid: _gen_tenant_config(rng, tid, db_type)}}
            tenant_config["tenants"][tid]["_metadata"]["domain"] = domain
            tenant_config["tenants"][tid]["_metadata"]["region"] = region
            tenant_config["tenants"][tid]["_metadata"]["environment"] = env

            # Zipfian: replicate threshold dict to grow YAML body.
            # _gen_tenant_config produces a base set of threshold keys; we
            # add `_extra_threshold_NN` keys to reach the desired multiplier.
            size_mult = sizes[idx]
            for extra_i in range(size_mult - 1):
                extra_key = f"_extra_threshold_{extra_i:02d}"
                tenant_config["tenants"][tid][extra_key] = _gen_threshold_value(rng)

            # Power-law: deepen _metadata with nested overlay blocks.
            depth = depths[idx]
            if depth > 0:
                meta = tenant_config["tenants"][tid]["_metadata"]
                cursor = meta
                for d_i in range(depth):
                    nested_key = f"_overlay_l{d_i}"
                    cursor[nested_key] = {
                        "schedule": rng.choice(["business-hours", "off-hours", "24x7"]),
                        "escalation_tier": rng.choice(TIERS),
                    }
                    cursor = cursor[nested_key]

            _write_yaml(slot_dir / f"{tid}.yaml", tenant_config)
            domain_db_types.setdefault(domain, []).append(db_type)
            idx += 1

    if with_defaults:
        _write_yaml(output_dir / "_defaults.yaml", _gen_defaults_yaml(rng))
        for domain in DOMAINS:
            domain_dir = output_dir / domain
            if domain_dir.exists():
                db_types_in_domain = list(set(domain_db_types.get(domain, DB_TYPES[:2])))
                _write_yaml(domain_dir / "_defaults.yaml", _gen_defaults_yaml(rng, db_types_in_domain))

    print(f"✅ Generated {idx} tenant files (synthetic-v2) in {output_dir}")
    print(f"   Skew: Zipf alpha=1.5 (sizes 1-6), power-law alpha=2.0 (overlay depths 0-3)")
    print(f"   Distribution: sizes p50={sorted(sizes)[len(sizes)//2]} p99={sorted(sizes)[int(len(sizes)*0.99)] if len(sizes)>1 else sizes[0]}; depths p50={sorted(depths)[len(depths)//2]} p99={sorted(depths)[int(len(depths)*0.99)] if len(depths)>1 else depths[0]}")


def _write_yaml(path: Path, data: dict) -> None:
    """Write a dict as YAML. Avoids importing yaml to keep deps minimal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        _dump_yaml(f, data, indent=0)


def _dump_yaml(f, obj, indent: int = 0) -> None:  # noqa: C901 — simple recursive writer
    """Minimal YAML serializer (avoids PyYAML dependency for fixture generation)."""
    prefix = "  " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                f.write(f"{prefix}{k}:\n")
                _dump_yaml(f, v, indent + 1)
            elif isinstance(v, bool):
                f.write(f"{prefix}{k}: {'true' if v else 'false'}\n")
            elif isinstance(v, (int, float)):
                f.write(f"{prefix}{k}: {v}\n")
            else:
                # String — quote if contains special chars
                sv = str(v)
                if any(c in sv for c in ":{}\n#[]") or sv.startswith('"'):
                    f.write(f'{prefix}{k}: "{sv}"\n')
                else:
                    f.write(f"{prefix}{k}: {sv}\n")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                first = True
                for k, v in item.items():
                    if first:
                        f.write(f"{prefix}- {k}:")
                        first = False
                    else:
                        f.write(f"{prefix}  {k}:")
                    if isinstance(v, (dict, list)):
                        f.write("\n")
                        _dump_yaml(f, v, indent + 2)
                    elif isinstance(v, bool):
                        f.write(f" {'true' if v else 'false'}\n")
                    elif isinstance(v, (int, float)):
                        f.write(f" {v}\n")
                    else:
                        sv = str(v)
                        if any(c in sv for c in ":{}\n#[]"):
                            f.write(f' "{sv}"\n')
                        else:
                            f.write(f" {sv}\n")
            else:
                sv = str(item)
                if any(c in sv for c in ":{}\n#[]"):
                    f.write(f'{prefix}- "{sv}"\n')
                else:
                    f.write(f"{prefix}- {sv}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic tenant fixtures for benchmark & integration testing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--count", "-n", type=int, required=True,
        help="Number of tenants to generate (common: 100, 500, 1000, 2000)",
    )
    parser.add_argument(
        "--layout", "-l", choices=["flat", "hierarchical", "synthetic-v2"], default="flat",
        help="Directory layout mode (default: flat). 'synthetic-v2' is hierarchical layout with Zipf+power-law skews per phase-b-e2e-harness §6.2 (B-1 Phase 2 calibration baseline).",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output directory (default: tests/fixtures/synthetic-<count>-<layout>/conf.d)",
    )
    parser.add_argument(
        "--with-defaults", action="store_true",
        help="Inject _defaults.yaml files with platform-wide threshold defaults",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for reproducible output (default: 42)",
    )
    args = parser.parse_args()

    if args.output:
        output_dir = Path(args.output)
    else:
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        output_dir = repo_root / "tests" / "fixtures" / f"synthetic-{args.count}-{args.layout}" / "conf.d"

    if output_dir.exists() and any(output_dir.iterdir()):
        print(f"⚠️  Output directory {output_dir} already exists and is not empty.")
        print(f"   Use a different --output or remove it first.")
        sys.exit(1)

    if args.layout == "flat":
        generate_flat(args.count, output_dir, args.with_defaults, args.seed)
    elif args.layout == "synthetic-v2":
        generate_synthetic_v2(args.count, output_dir, args.with_defaults, args.seed)
    else:
        generate_hierarchical(args.count, output_dir, args.with_defaults, args.seed)

    # Summary stats
    file_count = sum(1 for _ in output_dir.rglob("*.yaml"))
    total_size = sum(f.stat().st_size for f in output_dir.rglob("*.yaml"))
    print(f"   Files: {file_count} YAML")
    print(f"   Size:  {total_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
