{{/*
Expand the name of the chart.
*/}}
{{- define "tenant-api.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "tenant-api.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "tenant-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: dynamic-alerting
{{- end }}

{{/*
Selector labels
*/}}
{{- define "tenant-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tenant-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
conf-dir volume definition — switches between emptyDir, hostPath, and PVC.
*/}}
{{- define "tenant-api.confDirVolume" -}}
{{- if eq .Values.confDir.type "hostPath" }}
hostPath:
  path: {{ .Values.confDir.hostPath | quote }}
  type: DirectoryOrCreate
{{- else if eq .Values.confDir.type "pvc" }}
persistentVolumeClaim:
  claimName: {{ include "tenant-api.name" . }}-conf
{{- else }}
emptyDir: {}
{{- end }}
{{- end }}
