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

victorialogs mode (ADR-021): the gateway is the authorization plane for
tenant log queries. The cross-tenant isolation boundary is the
`tenant-federation-logs` audience — a metrics-pull token (aud
`tenant-federation`) MUST NOT be accepted here, or capability model B
collapses (one capability leak becomes two). jwt_authn enforces the
audience natively (a wrong-aud token 401s before the Lua even runs), so
this guard's job is to make a MIS-CONFIGURED deploy fail at template time
rather than silently accept metrics tokens against the log store: in
victorialogs mode, jwt.audience MUST be `tenant-federation-logs`.
*/}}
{{- define "federation-gateway.validateMode" -}}
{{- if not (has .Values.mode (list "prom-label-proxy" "vm-cluster" "victorialogs")) }}
{{- fail (printf "federation-gateway: mode must be \"prom-label-proxy\", \"vm-cluster\" or \"victorialogs\", got %q" .Values.mode) }}
{{- end }}
{{- if eq .Values.mode "victorialogs" }}
{{- if ne .Values.jwt.audience "tenant-federation-logs" }}
{{- fail (printf "federation-gateway: victorialogs mode requires jwt.audience=\"tenant-federation-logs\" (a metrics-pull token must not query the log store), got %q" .Values.jwt.audience) }}
{{- end }}
{{- end }}
{{- end }}
