# pint — Prometheus rule linter config. https://cloudflare.github.io/pint/
#
# ADR-025 deferred item (rule static-analysis). The high-ROI win is pint's
# `alerts/template` check, which mechanically catches the "aggregation strips a
# label the alert template uses → silent-forever alert" class that has burned
# this repo 5× (today guarded only by hand-comments: rule-pack-kubernetes.yaml,
# rate.yaml). The other default checks false-positive on this repo's intentional
# idioms, so they're disabled here (see the match-all rule block below).
#
# Exemptions live in THIS file (the central, audited registry) rather than as
# scattered inline `# pint disable` comments — and editing .pint.hcl never
# perturbs the rule-pack ↔ configmap ↔ operator-manifest semantic sync.

parser {
  # The Prometheus rule documents pint can parse:
  #   - rule-packs/rule-pack-*.yaml   : canonical component rule-pack SOURCE
  #   - tests/rulepacks/*.rules.yaml  : the bare-rules EXTRACTS of the platform
  #     self-monitoring pack. This puts the ADR-025 guardian (`Watchdog` +
  #     `AlertmanagerWebhookNotificationsFailing`) and the SSE-reconnect sentinel
  #     UNDER the gate — the "guardian must not run unguarded" point. The
  #     `*_test.yaml` promtool specs in that dir are NOT rule files and are skipped
  #     (not matched here).
  #   - k8s/03-monitoring/configmap-rules-platform*.y(a)ml : the DEPLOYED platform
  #     self-monitoring pack, read through `relaxed` below.
  #
  # The extracts USED to be the only pint-reachable form of the platform pack ("it
  # is ConfigMap-wrapped → unparseable"). That is no longer true: pint has parsed
  # rules embedded in a ConfigMap `data:` block since v0.19.0 when the file is
  # listed in `parser.relaxed`, and this repo pins 0.86.0. Measured on 0.86.0:
  # 26 of the pack's 42 alerts already had extracts, the other 16 were invisible to
  # pint, and 6 of those 16 carry real `alerts/template` label-flow risk (an
  # aggregation that strips labels + a `{{ $labels.X }}` template) —
  # ConfigBlastRadiusHighImpact, FederationAuditPipelineSilent,
  # FederationGatewayBackendErrors, FederationRejectionRateAnomaly,
  # ThresholdExporterTooFewReplicas, VectorBufferEventsDropped.
  #
  # PREFIX + BOTH extensions, deliberately NOT the exact filename: the pytest-side
  # platform-pack scanners key off `_PLATFORM_CM_PREFIX = "configmap-rules-platform"`
  # and `_RULES_FILE_EXTS = (".yaml", ".yml")` (
  # tests/ops/test_generate_routes_orchestration.py), and their floors are worded as a SUM across every platform
  # rules ConfigMap so that SPLITTING the pack keeps satisfying them. An exact
  # `configmap-rules-platform\.yaml` would make a future
  # configmap-rules-platform-federation.yaml invisible to pint while pytest kept
  # scanning it — the two sides would silently disagree about what "the platform
  # pack" is. Verified with a scratch split file: 394 entries with the prefix form,
  # 393 with the exact-filename form (and 394 again after renaming it to `.yml`).
  #
  # ONLY the platform prefix is included, NOT the other 16 configmap-rules-*.yaml.
  # The criterion is mechanical, not taste: those 16 all carry a `DO NOT EDIT`
  # banner because generate_rulepack_configmaps.py generates them FROM
  # rule-packs/rule-pack-*.yaml, which is already linted above;
  # configmap-rules-platform.yaml is the ONLY hand-maintained one (`grep -L
  # "DO NOT EDIT" k8s/03-monitoring/configmap-rules-*.yaml` returns exactly it).
  # Including all 17 measured 689 entries vs 393 (+75%) and reported every finding
  # TWICE — e.g. the same alerts/for lands on both rule-packs/rule-pack-mariadb.yaml
  # and k8s/03-monitoring/configmap-rules-mariadb.yaml — so half the gate's output
  # would point developers at a generated file they must not edit. Copy-sync is
  # guarded by check_rulepack_sync.py instead. operator-manifests/ stays out for the
  # same reason; recipes/ + conf.d are not rule files.
  #
  # NB: pint AUTO-ANCHORS every regexp here (it parses `X` as `^X$`), so no explicit
  # `^...$` is needed — pint 0.86.0 `-l DEBUG` echoes the list back as
  # `^k8s/03-monitoring/configmap-rules-platform.*\.ya?ml$`.
  include = [
    "rule-packs/rule-pack-.*\\.yaml",
    "tests/rulepacks/.*\\.rules\\.yaml",
    "k8s/03-monitoring/configmap-rules-platform.*\\.ya?ml",
  ]
  # `relaxed` is a per-path regexp LIST, not a global boolean — only paths matched
  # here are parsed as "may contain rules anywhere in the YAML" instead of "must be
  # a bare `groups:` document". It is load-bearing, not cosmetic: adding the
  # ConfigMap to `include` WITHOUT listing it here yields entries=352 and a **Fatal**
  # parse problem (CI red), because pint then insists the file is a rule document.
  # Scoping it to the platform ConfigMap keeps the strict parse everywhere else;
  # measured impact on the pre-existing 351 entries is ZERO (pint's problem output is
  # byte-identical before/after — only the entries count and the log header move).
  relaxed = ["k8s/03-monitoring/configmap-rules-platform.*\\.ya?ml"]
}

ci {
  # `pint ci` would diff against this; CI actually runs `pint lint` (full, scoped
  # by the clean baseline below) so the gate is deterministic and order-free.
  baseBranch = "main"
}

# Disable the checks that only false-positive on this repo's established idioms:
#   alerts/comparison — flags absent()-based *ExporterAbsent / sentinel alerts as
#                       "always firing" (they ARE, by design).
#   promql/impossible — flags the intentional `... or vector(0)` empty-vector guard
#                       as dead code.
#   rule/dependency   — flags the intentional split between recording-rule and
#                       alerting-rule groups.
# (The online checks promql/series + promql/cost are skipped via `--offline` in CI.)
rule {
  disable = ["alerts/comparison", "promql/impossible", "rule/dependency"]
}

# Exempt the platform-scoped *Absent / Inert SENTINELS from alerts/template:
# their expr intentionally aggregates `tenant` away (the alert is platform-scoped
# and renders empty → dropped), but the repo's required-labels policy
# (lint_custom_rules.py) MANDATES a `tenant` label on every rule — so here the
# "template uses a label the query won't have" is BY DESIGN, not a bug. A genuine
# new rule that strips a label it actually needs is NOT name-matched → still caught.
#
# NB: pint AUTO-ANCHORS every `match.name` regexp (it parses `X` as `^X$`, per
# https://cloudflare.github.io/pint/configuration.html), so `.+ExporterAbsent`
# already means "the name ENDS with ExporterAbsent" — a name merely CONTAINING the
# substring (e.g. `FooExporterAbsentButBuggy`) is NOT exempted (verified: it's
# caught). No explicit `^...$` needed.
rule {
  match {
    name = "(.+ExporterAbsent|VersionAwareThresholdInert)"
  }
  disable = ["alerts/template"]
}
