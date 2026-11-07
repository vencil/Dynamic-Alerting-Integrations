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
{{- with include "chargeback.vlAuthSecret" . }}
- name: VICTORIALOGS_USERNAME
  value: {{ include "chargeback.vlAuthUser" $ | quote }}
- name: VICTORIALOGS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ . | quote }}
      key: password
{{- end }}
{{- end }}

{{/*
Store credentials (#1295), read null-safe. A release upgraded with
--reuse-values from a chart older than this one carries no
victorialogs.auth at all (Helm then does not merge the new defaults in),
and an operator following the README may set only existingSecret. So: a
missing `auth` is an empty map, and a missing username is the chart name —
which is this consumer's name in helm/victorialogs auth.consumers
(tests/shared/test_victorialogs_consumers.py renders both ways).
*/}}
{{- define "chargeback.vlAuthSecret" -}}
{{- $auth := .Values.victorialogs.auth | default dict -}}
{{- $auth.existingSecret | default "" -}}
{{- end }}
{{- define "chargeback.vlAuthUser" -}}
{{- $auth := .Values.victorialogs.auth | default dict -}}
{{- $auth.username | default .Chart.Name -}}
{{- end }}
