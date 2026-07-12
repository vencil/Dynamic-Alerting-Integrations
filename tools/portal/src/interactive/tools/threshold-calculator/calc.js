---
title: "Threshold Calculator — percentile → threshold suggestion engine"
purpose: |
  Pure data + functions extracted from threshold-calculator.jsx (portal ROI
  wave 6) so the percentile-based threshold heuristic and the YAML emitter can
  be exercised without React. Pre-extraction these were inline at the top of the
  .jsx with 0% unit coverage — the core value of the tool (the suggestion math)
  was untested.

  Behavior is a VERBATIM move, not a re-derivation: the orchestrator now imports
  these back and keeps only React state + render. Any change to the numbers here
  is a behavior change and must be intentional.

  Public API:
    METRIC_PROFILES   per-metric { label, unit, desc, typical:{min,max,p50,p90,p95,p99}, inverted? }
    PERCENTILES       ['p50','p90','p95','p99'] — slider / target render order
    suggestThreshold(profile, percentile, customValues) -> { warning, critical }
      base = customValues[percentile] (if defined) else profile.typical[percentile]
      normal metric  : warning = round(base * 1.15), critical = round(base * 1.4)
      inverted metric: warning = max(0, round(base * 0.85)), critical = max(0, round(base * 0.7))
    generateYAML(selections) -> string
      selections: array of { metric, warning, critical }; emits
      `<metric>: "<warning>"` + `<metric>_critical: "<critical>"` under tenants.my-app

  Closure deps: none. Pure functions; receive profile / config as args.
---

const METRIC_PROFILES = {
  mysql_connections: {
    label: 'MySQL Connections',
    unit: 'connections',
    desc: 'Current number of active database connections',
    typical: { min: 10, max: 500, p50: 50, p90: 120, p95: 180, p99: 250 },
  },
  mysql_cpu: {
    label: 'MySQL Threads Running',
    unit: 'threads',
    desc: 'Running-threads saturation (threads_running, NOT host CPU%); Nichter tiers: busy 10–30, high 30–50, overloaded 50–100 (#944)',
    typical: { min: 2, max: 100, p50: 25, p90: 60, p95: 75, p99: 90 },
  },
  pg_connections: {
    label: 'PostgreSQL Connections',
    unit: 'connections',
    desc: 'Active PostgreSQL connections',
    typical: { min: 5, max: 300, p50: 30, p90: 80, p95: 120, p99: 200 },
  },
  pg_cache_hit_ratio: {
    label: 'PG Cache Hit Ratio',
    unit: '%',
    desc: 'Buffer cache hit percentage (higher is better, threshold is minimum)',
    typical: { min: 70, max: 100, p50: 95, p90: 98, p95: 99, p99: 99.5 },
    inverted: true,
  },
  redis_memory: {
    label: 'Redis Memory',
    unit: '%',
    desc: 'Memory usage as percentage of maxmemory',
    typical: { min: 10, max: 100, p50: 40, p90: 70, p95: 80, p99: 92 },
  },
  redis_evictions: {
    label: 'Redis Evictions',
    unit: 'evictions/s',
    desc: 'Key eviction rate per second',
    typical: { min: 0, max: 5000, p50: 50, p90: 500, p95: 1000, p99: 3000 },
  },
  kafka_lag: {
    label: 'Kafka Consumer Lag',
    unit: 'messages',
    desc: 'Max consumer group lag in messages',
    typical: { min: 0, max: 1000000, p50: 1000, p90: 50000, p95: 100000, p99: 500000 },
  },
};

const PERCENTILES = ['p50', 'p90', 'p95', 'p99'];

function suggestThreshold(profile, percentile, customValues) {
  const cv = customValues || {};
  const base = cv[percentile] !== undefined ? cv[percentile] : profile.typical[percentile];
  // Warning = selected percentile + 15% headroom, Critical = +40% headroom
  const warning = profile.inverted ? Math.max(0, Math.round(base * 0.85)) : Math.round(base * 1.15);
  const critical = profile.inverted ? Math.max(0, Math.round(base * 0.7)) : Math.round(base * 1.4);
  return { warning, critical };
}

function generateYAML(selections) {
  const lines = ['tenants:', '  my-app:'];
  selections.forEach(s => {
    lines.push(`    ${s.metric}: "${s.warning}"`);
    lines.push(`    ${s.metric}_critical: "${s.critical}"`);
  });
  return lines.join('\n');
}

export { METRIC_PROFILES, PERCENTILES, suggestThreshold, generateYAML };
