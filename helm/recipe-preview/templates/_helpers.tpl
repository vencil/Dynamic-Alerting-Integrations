{{/*
Expand the name of the chart.
*/}}
{{- define "recipe-preview.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "recipe-preview.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "recipe-preview.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: dynamic-alerting
{{- end }}

{{/*
Selector labels
*/}}
{{- define "recipe-preview.selectorLabels" -}}
app.kubernetes.io/name: {{ include "recipe-preview.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
