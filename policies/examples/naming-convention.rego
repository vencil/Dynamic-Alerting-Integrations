# naming-convention.rego — Tenant naming convention policy
# Validates that tenant names follow expected patterns.
#
# Violations:
#   - tenant names not matching lowercase/hyphen pattern (warning)
#   - tenant names missing environment prefix (warning)

package dynamic_alerting.policy

import future.keywords.in

# Tenant names should be lowercase with hyphens
violations[v] {
    tenant := input.tenants[name]
    not regex.match(`^[a-z0-9][a-z0-9-]*[a-z0-9]$`, name)
    v := {
        "msg": sprintf("tenant name '%s' doesn't follow naming convention (lowercase, hyphens only)", [name]),
        "severity": "warning",
        "tenant": name,
        "field": "tenant_name"
    }
}

# Tenant names should include an environment prefix
violations[v] {
    tenant := input.tenants[name]
    prefixes := {
        "prod-",
        "staging-",
        "dev-",
        "test-",
        "qa-"
    }
    not any_prefix(name, prefixes)
    v := {
        "msg": sprintf("tenant name '%s' lacks environment prefix (prod-/staging-/dev-/test-/qa-)", [name]),
        "severity": "warning",
        "tenant": name,
        "field": "tenant_name"
    }
}

any_prefix(s, prefixes) {
    prefix := prefixes[_]
    startswith(s, prefix)
}
