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
