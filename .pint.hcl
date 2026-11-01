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
  #   - rule-packs/rule-pack-*.yaml   : every rule-pack SOURCE — since #1595 D1a
  #     that includes the platform self-monitoring pack
  #     (rule-packs/rule-pack-platform.yaml), so the ADR-025 guardian (`Watchdog`
  #     + `AlertmanagerWebhookNotificationsFailing`) and every other platform
  #     alert sit under the gate in their source form.
  #   - tests/rulepacks/*.rules.yaml  : the bare-rules promtool targets that are
  #     not rule packs (runtime canary, liveness eq1-vs-bare, the ADR-030
  #     reference-validation candidates). The `*_test.yaml` promtool specs in that
  #     dir are NOT rule files and are skipped (not matched here).
  #
  # NO k8s/03-monitoring/configmap-rules-*.yaml. The criterion is mechanical: all
  # of them carry a `DO NOT EDIT` banner because generate_rulepack_configmaps.py
  # generates them FROM rule-packs/rule-pack-*.yaml, already linted above — the
  # platform ConfigMap too since #1595 (it used to be the one hand-maintained
  # copy, read here through `parser.relaxed`; that special case is gone with it).
  # Linting a copy reports every finding TWICE (measured before: 689 entries vs
  # 393, the same alerts/for on rule-packs/rule-pack-mariadb.yaml and
  # k8s/03-monitoring/configmap-rules-mariadb.yaml), half of them on a file
  # developers must not edit. Copy-sync is guarded by check_rulepack_sync.py.
  # The PrometheusRule copies are not committed (#3427); recipes/ + conf.d are
  # not rule files.
  #
  # NB: pint AUTO-ANCHORS every regexp here (it parses `X` as `^X$`), so no explicit
  # `^...$` is needed.
  include = [
    "rule-packs/rule-pack-.*\\.yaml",
    "tests/rulepacks/.*\\.rules\\.yaml",
  ]
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
