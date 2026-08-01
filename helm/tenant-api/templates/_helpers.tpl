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
Namespace the federation token-store ConfigMap lives in (#1313).

Empty `federation.store.namespace` = this release's namespace, which is the
historical behaviour and stays the default so existing installs are untouched.

⛔ SINGLE SOURCE. configmap-federation-store.yaml, the RBAC pair and the
--federation-namespace flag all resolve the namespace THROUGH THIS TEMPLATE and
must never re-derive it. Three independent copies is exactly how the store and
its consumers drifted apart in the first place: a ConfigMap is namespace-scoped
and both the gateway and the reconciler consume it as a VOLUME, so they can only
ever resolve one in their OWN namespace — mounting is not an API read that can
cross namespaces.
*/}}
{{- define "tenant-api.federationStoreNamespace" -}}
{{- .Values.federation.store.namespace | default .Release.Namespace -}}
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
