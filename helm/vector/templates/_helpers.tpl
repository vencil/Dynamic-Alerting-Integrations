{{/*
Expand the name of the chart.
*/}}
{{- define "vector.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "vector.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "vector.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: dynamic-alerting
app.kubernetes.io/component: log-shipper
{{- end }}

{{/*
Selector labels
*/}}
{{- define "vector.selectorLabels" -}}
app.kubernetes.io/name: {{ include "vector.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Producer namespace binding (#1288) — the SINGLE definition of both the default
and the absent-vs-explicitly-empty rule. configmap.yaml renders the route
conditions from these and NOTES.txt prints them back to the operator; if each
computed its own, a future change to a default in one place would silently
desync what we ENFORCE from what we TELL the operator we enforce. That is the
hand-copied-contract drift this repo keeps paying for, so there is exactly one
copy and both callers `include` it.

⛔ `default` is deliberately not used: an operator who sets the value to "" to
disable the binding would get the default back, because Go templates treat ""
as falsy. `hasKey` keeps absent (take the default) and explicitly-empty
(disabled) distinguishable. Returns "" when the binding is disabled, which the
callers test with a plain `if`.
*/}}
{{- define "vector.evidenceNamespace" -}}
{{- $ec := default dict .Values.evidenceChannel -}}
{{- $ns := "tenant-api" -}}
{{- if hasKey $ec "podNamespace" }}{{- $ns = ($ec.podNamespace | toString) }}{{- end -}}
{{- $ns -}}
{{- end }}

{{- define "vector.gatewayNamespace" -}}
{{- $ec := default dict .Values.evidenceChannel -}}
{{- $ns := "monitoring" -}}
{{- if hasKey $ec "gatewayNamespace" }}{{- $ns = ($ec.gatewayNamespace | toString) }}{{- end -}}
{{- $ns -}}
{{- end }}
