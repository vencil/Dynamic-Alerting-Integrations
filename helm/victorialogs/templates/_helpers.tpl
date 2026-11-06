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
{{- if eq $mode "enforce" -}}
{{- $_ := required (printf "auth.consumers[%s].existingSecret is required when auth.mode=enforce: a Secret in this namespace with key `password` (#1295)" .name) .existingSecret -}}
{{- end -}}
{{- if .existingSecret -}}
{{- $out = append $out . -}}
{{- end -}}
{{- end -}}
{{- toYaml $out -}}
{{- end }}
