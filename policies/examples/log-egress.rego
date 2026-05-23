# log-egress.rego — Log-aggregation egress + env-override policy (#566 T4-1/T4-2)
#
# Mirrors the three rules enforced by the LIVE gate
# scripts/tools/lint/check_log_egress_policy.py:
#   1. Vector sink endpoints must target an allowlisted host.
#   2. Vector-reserved env vars (VECTOR_*) may only be set via downward-API
#      fieldRef — never a literal value or other valueFrom (override =
#      pipeline hijack).
#   3. Sensitive-named env (*TOKEN* / *KEY* / *SECRET* / ...) must use
#      valueFrom, never a literal `value:` (literal = hardcoded secret /
#      attacker-substitutable credential).
#
# ── Status: illustrative + migration seam, NOT the live CI gate ─────────
# The repo's live enforcement is the Python lint above (consistent with
# the other check_*.py gates; runs with zero extra binary). This rego is
# the policy-as-code artifact in the project's policy DSL and the seam for
# a future OPA Gatekeeper runtime control (#566 future-proofing): the core
# rules below are written decoupled from the input shape, so adopting
# Gatekeeper later only needs a thin wrapper mapping AdmissionReview's
# `request.object` onto the same helpers — no rule rewrite.
#
# Evaluate (when the `opa` binary is available):
#   helm template helm/vector -n monitoring \
#     | yq -o=json eval-all '[.]' -                 # multi-doc → JSON array
#     | opa eval -I -d policies/examples/log-egress.rego \
#         'data.dynamic_alerting.egress.violations'
#
# Input contract: a JSON array of rendered Kubernetes manifests
# (`input` is the array). For the Vector ConfigMap, the embedded
# `data["vector.yaml"]` is itself a YAML string — a host wrapper should
# parse it and attach the structured sinks (see the Python lint's
# _iter_sink_endpoints for the canonical extraction) before eval, OR
# evaluate sink rules against the parsed config separately. The env rules
# below operate directly on the rendered pod-spec env arrays.

package dynamic_alerting.egress

import future.keywords.in

# ── Configurable data (override via `opa eval -d data.json`) ────────────
default allowed_host_globs := ["*.svc", "*.svc.cluster.local", "localhost", "127.0.0.1"]
reserved_env_globs := ["VECTOR_*"]
sensitive_env_globs := ["*TOKEN*", "*KEY*", "*SECRET*", "*PASSWORD*", "*CREDENTIAL*"]

# ── Reusable helpers (the Gatekeeper-migration seam) ────────────────────

# host_allowed(host) — true if host matches any allowlist glob.
host_allowed(host) if {
    some g in allowed_host_globs
    glob.match(g, [], host)
}

# is_reserved_env(name) / is_sensitive_env(name) — name-class predicates.
is_reserved_env(name) if {
    some g in reserved_env_globs
    glob.match(g, [], name)
}

is_sensitive_env(name) if {
    some g in sensitive_env_globs
    glob.match(upper(g), [], upper(name))
}

# env_via_field_ref(env) — true if the entry sources from downward-API
# fieldRef (the only legitimate form for a reserved var).
env_via_field_ref(env) if {
    env.valueFrom.fieldRef
}

# env_is_literal(env) — true if a literal `value:` is set.
env_is_literal(env) if {
    env.value
}

# ── Rule 1: sink egress allowlist ───────────────────────────────────────
# Expects each manifest object that carries a parsed Vector config to
# expose `_parsed_vector_config.sinks` (host wrapper attaches it). Kept
# defensive: only fires when the structured field is present.
violations[v] {
    some obj in input
    sinks := obj._parsed_vector_config.sinks
    some sink_name, sink in sinks
    some url in sink.endpoints
    host := split(trim_prefix(split(url, "://")[count(split(url, "://")) - 1], "//"), "/")[0]
    bare := split(host, ":")[0]
    not host_allowed(bare)
    v := {
        "msg": sprintf("sink %q egress to non-allowlisted host %q", [sink_name, bare]),
        "severity": "error",
        "rule": "sink-egress-allowlist",
    }
}

# ── Rule 2: reserved env override ───────────────────────────────────────
violations[v] {
    some obj in input
    env := pod_envs(obj)[_]
    is_reserved_env(env.name)
    not env_via_field_ref(env)
    v := {
        "msg": sprintf("env %q overrides a Vector-reserved var via non-fieldRef source", [env.name]),
        "severity": "error",
        "rule": "reserved-env-override",
    }
}

# ── Rule 3: sensitive env literal value ─────────────────────────────────
violations[v] {
    some obj in input
    env := pod_envs(obj)[_]
    is_sensitive_env(env.name)
    env_is_literal(env)
    v := {
        "msg": sprintf("sensitive env %q set via literal value; use valueFrom.secretKeyRef", [env.name]),
        "severity": "error",
        "rule": "sensitive-env-literal",
    }
}

# pod_envs(obj) — flatten env across the pod template of any workload kind.
pod_envs(obj) := envs if {
    obj.kind == "CronJob"
    envs := [e |
        c := obj.spec.jobTemplate.spec.template.spec.containers[_]
        e := c.env[_]
    ]
}

pod_envs(obj) := envs if {
    obj.kind in {"Deployment", "DaemonSet", "StatefulSet", "Job"}
    envs := [e |
        c := obj.spec.template.spec.containers[_]
        e := c.env[_]
    ]
}
