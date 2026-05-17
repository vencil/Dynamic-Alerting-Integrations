{{/*
Expand the name of the chart.
*/}}
{{- define "federation-gateway.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "federation-gateway.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "federation-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: dynamic-alerting
{{- end }}

{{/*
Selector labels
*/}}
{{- define "federation-gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "federation-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Fail-loud guard for the proxy mode.
*/}}
{{- define "federation-gateway.validateMode" -}}
{{- if not (has .Values.mode (list "prom-label-proxy" "vm-cluster")) }}
{{- fail (printf "federation-gateway: mode must be \"prom-label-proxy\" or \"vm-cluster\", got %q" .Values.mode) }}
{{- end }}
{{- end }}
