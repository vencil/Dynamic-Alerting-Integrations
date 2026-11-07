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

{{/*
Store credentials (#1295): the username and the Secret-backed password the
aggregate script sends as HTTP basic auth. Nothing without a Secret.
Shared by the CronJob and the manual Job so the two cannot drift.
*/}}
{{- define "chargeback.victorialogsAuthEnv" -}}
{{- with .Values.victorialogs.auth.existingSecret }}
- name: VICTORIALOGS_USERNAME
  value: {{ required "victorialogs.auth.username is required when victorialogs.auth.existingSecret is set" $.Values.victorialogs.auth.username | quote }}
- name: VICTORIALOGS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ . | quote }}
      key: password
{{- end }}
{{- end }}
