# threshold-bounds.rego — Threshold reasonable range checks
# Validates that threshold values fall within sensible bounds.
#
# Violations:
#   - percentage metrics exceeding 100% (error)
#   - negative connection thresholds (error)
#   - critical metrics set to "disable" (warning)

package dynamic_alerting.policy

import future.keywords.in

# Percentage metrics should be 0-100
violations[v] {
    tenant := input.tenants[name]
    key := [k | some k; tenant[k]; not startswith(k, "_")]
    metric := key[_]
    val := to_number(tenant[metric])
    endswith(metric, "_percent")
    val > 100
    v := {
        "msg": sprintf("tenant '%s' metric '%s' value %v exceeds 100%%", [name, metric, val]),
        "severity": "error",
        "tenant": name,
        "field": metric
    }
}

# Connection thresholds shouldn't be negative
violations[v] {
    tenant := input.tenants[name]
    key := [k | some k; tenant[k]; not startswith(k, "_")]
    metric := key[_]
    val := to_number(tenant[metric])
    contains(metric, "connections")
    val < 0
    v := {
        "msg": sprintf("tenant '%s' metric '%s' has negative threshold %v", [name, metric, val]),
        "severity": "error",
        "tenant": name,
        "field": metric
    }
}

# Warn if threshold is set to "disable" for critical metrics
violations[v] {
    tenant := input.tenants[name]
    critical_metrics := {
        "mysql_connections",
        "pg_connections",
        "redis_memory"
    }
    metric := critical_metrics[_]
    tenant[metric] == "disable"
    v := {
        "msg": sprintf("tenant '%s' has disabled critical metric '%s'", [name, metric]),
        "severity": "warning",
        "tenant": name,
        "field": metric
    }
}
