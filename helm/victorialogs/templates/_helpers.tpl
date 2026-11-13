{{/*
Expand the name of the chart.
*/}}
{{- define "victorialogs.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "victorialogs.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "victorialogs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: dynamic-alerting
app.kubernetes.io/component: log-store
{{- end }}

{{/*
Selector labels
*/}}
{{- define "victorialogs.selectorLabels" -}}
app.kubernetes.io/name: {{ include "victorialogs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
vmauth (#1295). Env var holding consumer $name's password, e.g.
federation-gateway -> VMAUTH_PASSWORD_FEDERATION_GATEWAY. vmauth resolves the
%{...} placeholder in auth.yml from it, so no password is ever rendered into a
ConfigMap.
*/}}
{{- define "victorialogs.consumerPasswordEnv" -}}
VMAUTH_PASSWORD_{{ . | upper | replace "-" "_" }}
{{- end }}

{{/*
Consumers that get a vmauth user: every consumer in enforce (and each must
have a Secret), only those with a Secret in observe. Each Secret is named in
`auth.existingSecrets` (a map keyed by consumer name, #3847) and copied onto
the consumer as `existingSecret`, so callers read it there. Rendered as a YAML
list so callers can `fromYamlArray` it.
*/}}
{{- define "victorialogs.authConsumers" -}}
{{- $mode := .Values.auth.mode -}}
{{- if not (has $mode (list "observe" "enforce")) -}}
{{- fail (printf "auth.mode must be observe or enforce (got %q); see values.yaml auth (#1295)" $mode) -}}
{{- end -}}
{{- if and (eq $mode "enforce") (not .Values.auth.consumers) -}}
{{- fail "auth.mode=enforce with no auth.consumers would lock every consumer out: list them (#1295)" -}}
{{- end -}}
{{- $secrets := .Values.auth.existingSecrets | default dict -}}
{{- if not (kindIs "map" $secrets) -}}
{{- fail (printf "auth.existingSecrets must be a map of <consumer name>: <Secret name> (got %v) (#3847)" $secrets) -}}
{{- end -}}
{{- $seen := dict -}}
{{- $out := list -}}
{{- $missing := list -}}
{{- range .Values.auth.consumers -}}
{{- /* Values from a chart before 0.7.0 name each Secret on the consumer;
those rows would otherwise be ignored without a word. */ -}}
{{- if hasKey . "existingSecret" -}}
{{- fail (printf "auth.consumers[%s].existingSecret was removed in chart 0.7.0: name the Secret in `auth.existingSecrets.%s` instead, and stop restating auth.consumers unless you change it. With `helm upgrade --reuse-values`, use `--reset-then-reuse-values` (Helm >= 3.14) so the new defaults apply (#3847)" (toString .name) (toString .name)) -}}
{{- end -}}
{{- if not (regexMatch "^[a-z0-9]([a-z0-9-]*[a-z0-9])?$" (toString .name)) -}}
{{- fail (printf "auth.consumers: name %q must be lowercase letters, digits and dashes" (toString .name)) -}}
{{- end -}}
{{- if hasKey $seen .name -}}
{{- fail (printf "auth.consumers: %q is listed twice" .name) -}}
{{- end -}}
{{- $_ := set $seen .name true -}}
{{- if not .paths -}}
{{- fail (printf "auth.consumers[%s].paths is empty: a consumer with no paths can reach nothing" .name) -}}
{{- end -}}
{{- $secret := index $secrets (toString .name) | default "" -}}
{{- if not (kindIs "string" $secret) -}}
{{- fail (printf "auth.existingSecrets.%s must be a Secret name (got %v) (#3847)" .name $secret) -}}
{{- end -}}
{{- if and (eq $mode "enforce") (not $secret) -}}
{{- $missing = append $missing (toString .name) -}}
{{- end -}}
{{- if $secret -}}
{{- $out = append $out (merge (dict "existingSecret" $secret) .) -}}
{{- end -}}
{{- end -}}
{{- /* A key that names no consumer is a typo or a removed consumer: either
way the Secret it names is never used, and the consumer it was meant for is
not configured. */ -}}
{{- $unknown := list -}}
{{- range $name, $_ := $secrets -}}
{{- if not (hasKey $seen $name) -}}
{{- $unknown = append $unknown $name -}}
{{- end -}}
{{- end -}}
{{- if $unknown -}}
{{- fail (printf "auth.existingSecrets names %s, which auth.consumers does not list (known: %s). Fix the name, or remove the key (#3847)" (join ", " (sortAlpha $unknown)) (join ", " (sortAlpha (keys $seen)))) -}}
{{- end -}}
{{- /* All at once: an install that names no Secrets should learn every
consumer it has to provision from one render, not one per attempt. */ -}}
{{- if $missing -}}
{{- fail (printf "auth.mode=enforce (the default) needs a Secret for every consumer in auth.existingSecrets (<consumer>: <Secret name>) — missing: %s. Create one Secret per consumer in this namespace with key `password` (hex, no trailing newline) and name it there, or remove consumers you do not run; to roll credentials out first, set auth.mode=observe. See docs/internal/platform-log-aggregation-runbook.md §1.2 (#1295)" (join ", " $missing)) -}}
{{- end -}}
{{- /* A named Secret that is not there is an OUTAGE on upgrade, not a broken
consumer: vmauth reads every password through secretKeyRef, the strategy is
Recreate, so the running pod is deleted first and the new one sits in
CreateContainerConfigError with the Service empty. Checked only when this
release's Deployment already exists: that is the case with a pod to lose,
and it proves `lookup` is talking to a cluster (it returns empty under
`helm template` and client-side dry-run, so offline and GitOps renders are
unaffected). Applies in observe too — a Secret named there is mounted the
same way. The judgement lives in victorialogs.secretProblems so a test can
feed it lookup results without a cluster. */ -}}
{{- if and (include "victorialogs.checkSecretsOnUpgrade" .) (lookup "apps/v1" "Deployment" .Release.Namespace (include "victorialogs.name" .)) -}}
{{- $found := dict -}}
{{- range $out -}}
{{- $_ := set $found (toString .existingSecret) (lookup "v1" "Secret" $.Release.Namespace (toString .existingSecret)) -}}
{{- end -}}
{{- $broken := include "victorialogs.secretProblems" (dict "consumers" $out "secrets" $found) -}}
{{- if $broken -}}
{{- fail (printf "auth.consumers name Secrets this namespace (%s) cannot serve — %s. The Deployment uses Recreate, so upgrading now would delete the running store pod and leave the new one unable to start. Create the Secret(s) with key `password` first, then re-run. See docs/internal/platform-log-aggregation-runbook.md §1.2 (#1295)" $.Release.Namespace $broken) -}}
{{- end -}}
{{- end -}}
{{- toYaml $out -}}
{{- end }}

{{/*
victorialogs (#1295). Given {consumers: [...], secrets: {name: lookup result}},
one "; "-joined line per consumer whose Secret is missing (lookup returned
empty) or has no non-empty `password`; empty when all are usable.
*/}}
{{- define "victorialogs.secretProblems" -}}
{{- $broken := list -}}
{{- range .consumers -}}
{{- $secret := index $.secrets (toString .existingSecret) | default dict -}}
{{- if not $secret -}}
{{- $broken = append $broken (printf "%s: Secret %q not found" .name .existingSecret) -}}
{{- else if not (index ($secret.data | default dict) "password") -}}
{{- $broken = append $broken (printf "%s: Secret %q has no non-empty key `password`" .name .existingSecret) -}}
{{- end -}}
{{- end -}}
{{- join "; " $broken -}}
{{- end }}

{{/*
victorialogs (#1295). "true" unless auth.checkSecretsOnUpgrade is explicitly
false. An UNSET value counts as on: `helm upgrade --reuse-values` from a chart
that predates the key carries the old release's values, where it is absent —
and that 0.4.x → 0.5.0 upgrade is exactly the one that names new Secrets
(measured on k3s: with a plain truthiness test the check silently skipped).
*/}}
{{- define "victorialogs.checkSecretsOnUpgrade" -}}
{{- if ne (toString .Values.auth.checkSecretsOnUpgrade) "false" -}}true{{- end -}}
{{- end }}

{{/*
victorialogs (#1295 PR2). One NetworkPolicy `from` peer for a network identity
{namespace, podLabels}, given {who: <label for errors>, from: <map>, ns:
<release namespace>}. `namespace` alone admits every pod of that namespace;
`podLabels` alone matches pods of THIS namespace (a podSelector with no
namespaceSelector beside it); both = one peer, i.e. those labels in that
namespace. Unknown keys fail, so a typo (`podSelector:`) cannot silently drop
the restriction. A bare `namespace` equal to this release's namespace fails: it
would admit every pod beside the store.
*/}}
{{- define "victorialogs.netpolPeer" -}}
{{- $from := .from -}}
{{- if not $from -}}
{{- fail (printf "%s has no `from`. Since chart 0.6.0 each consumer carries the network identity the store's NetworkPolicy admits it by (it replaced networkPolicy.allowedPodSelectors / allowedNamespaces): a values file that restates auth.consumers must restate `from` too — copy it from the chart's values.yaml (#1295)" .who) -}}
{{- end -}}
{{- if not (kindIs "map" $from) -}}
{{- fail (printf "%s: `from` must be a map with `namespace` and/or `podLabels` (got %v); see values.yaml auth.consumers (#1295)" .who $from) -}}
{{- end -}}
{{- range $k, $_ := $from -}}
{{- if not (has $k (list "namespace" "podLabels")) -}}
{{- fail (printf "%s: unknown key %q in `from` — only `namespace` and `podLabels` (#1295)" $.who $k) -}}
{{- end -}}
{{- end -}}
{{- if and (hasKey $from "namespace") (not (kindIs "string" $from.namespace)) -}}
{{- fail (printf "%s: `from.namespace` must be a string (got %v) — quote it: a bare `no`/`on`/`123` is not read as a name (#1295)" .who $from.namespace) -}}
{{- end -}}
{{- $ns := $from.namespace | default "" -}}
{{- $labels := $from.podLabels | default dict -}}
{{- if not (kindIs "map" $labels) -}}
{{- fail (printf "%s: `from.podLabels` must be a map of label: value (#1295)" .who) -}}
{{- end -}}
{{- if and (not $ns) (not $labels) -}}
{{- fail (printf "%s: `from` needs `namespace`, `podLabels`, or both — an empty one would admit nothing or everything (#1295)" .who) -}}
{{- end -}}
{{- if and (eq $ns .ns) (not $labels) -}}
{{- fail (printf "%s: `from.namespace: %s` without `podLabels` admits every pod in the store's own namespace — add podLabels (#1295)" .who $ns) -}}
{{- end -}}
{{- $peer := dict -}}
{{- if $ns -}}
{{- $_ := set $peer "namespaceSelector" (dict "matchLabels" (dict "kubernetes.io/metadata.name" $ns)) -}}
{{- end -}}
{{- if $labels -}}
{{- $_ := set $peer "podSelector" (dict "matchLabels" $labels) -}}
{{- end -}}
{{- toYaml (list $peer) -}}
{{- end }}
