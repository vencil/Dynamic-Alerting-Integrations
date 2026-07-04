{{- define "federation-reconciler.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "federation-reconciler.labels" -}}
app.kubernetes.io/name: {{ include "federation-reconciler.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: federation-revocation-reconciler
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "federation-reconciler.selectorLabels" -}}
app.kubernetes.io/name: {{ include "federation-reconciler.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Fully-qualified image ref with an optional digest pin. */}}
{{- define "federation-reconciler.image" -}}
{{- $img := printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- with .Values.image.digest }}{{- $img = printf "%s@%s" $img . -}}{{- end -}}
{{- $img -}}
{{- end -}}
