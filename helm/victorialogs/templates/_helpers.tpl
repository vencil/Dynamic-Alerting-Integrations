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
name its Secret), only those with a Secret in observe. Rendered as a YAML
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
{{- $seen := dict -}}
{{- $out := list -}}
{{- $missing := list -}}
{{- range .Values.auth.consumers -}}
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
{{- if and (eq $mode "enforce") (not .existingSecret) -}}
{{- $missing = append $missing (toString .name) -}}
{{- end -}}
{{- if .existingSecret -}}
{{- $out = append $out . -}}
{{- end -}}
{{- end -}}
{{- /* All at once: an install that names no Secrets should learn every
consumer it has to provision from one render, not one per attempt. */ -}}
{{- if $missing -}}
{{- fail (printf "auth.mode=enforce (the default) needs auth.consumers[].existingSecret for every consumer — missing: %s. Create one Secret per consumer in this namespace with key `password` (hex, no trailing newline) and name it there, or remove consumers you do not run; to roll credentials out first, set auth.mode=observe. See docs/internal/platform-log-aggregation-runbook.md §1.2 (#1295)" (join ", " $missing)) -}}
{{- end -}}
{{- toYaml $out -}}
{{- end }}
