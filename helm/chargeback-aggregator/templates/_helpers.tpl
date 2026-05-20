{{/*
Expand the name of the chart.
*/}}
{{- define "chargeback.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "chargeback.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "chargeback.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: dynamic-alerting
app.kubernetes.io/component: chargeback
{{- end }}

{{/*
Selector labels
*/}}
{{- define "chargeback.selectorLabels" -}}
app.kubernetes.io/name: {{ include "chargeback.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
