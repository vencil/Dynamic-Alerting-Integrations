"""Custom Alerts vectorized recipe compiler (ADR-024 Capability B, #741 S1+S2).

Platform-authored parameterized recipes → ONE vectorized `group_left` rule per
shape (rule count = shape count, NOT per-tenant fan-out — preserves the rule-pack
O(M) invariant, benchmarks.md §2). Tenants fill recipe params only; they never
write PromQL (the declarative-only bedrock).

Modules:
  shape   — recipe_id slug (Go↔Python cross-language contract) + shape grouping
            + strict metric/label validation + safe selector assembly
  recipes — the 6 core recipe PromQL emitters (threshold/rate/ratio/absence/p99/forecast)
  loader  — conf.d tree walk + _custom_alerts inheritance + per-tenant cap count
"""
