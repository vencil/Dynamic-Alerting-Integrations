# routing-compliance.rego — Domain routing constraint policy
# Ensures tenants have routing configured and use approved receiver types.
#
# Violations:
#   - tenant without _routing configuration (warning)
#   - tenant with unsupported receiver type (error)

package dynamic_alerting.policy

import future.keywords.in

violations[v] {
    tenant := input.tenants[name]
    not tenant._routing
    v := {
        "msg": sprintf("tenant '%s' has no routing configuration", [name]),
        "severity": "warning",
        "tenant": name,
        "field": "_routing"
    }
}

violations[v] {
    tenant := input.tenants[name]
    tenant._routing.receiver.type
    not tenant._routing.receiver.type in {
        "webhook",
        "slack",
        "email",
        "teams",
        "pagerduty",
        "rocketchat"
    }
    v := {
        "msg": sprintf("tenant '%s' uses unsupported receiver type '%s'", [name, tenant._routing.receiver.type]),
        "severity": "error",
        "tenant": name,
        "field": "_routing.receiver.type"
    }
}
