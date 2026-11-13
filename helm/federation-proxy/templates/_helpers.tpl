{{/*
Expand the name of the chart.
*/}}
{{- define "federation-proxy.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "federation-proxy.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "federation-proxy.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: dynamic-alerting
{{- end }}

{{/*
Selector labels
*/}}
{{- define "federation-proxy.selectorLabels" -}}
app.kubernetes.io/name: {{ include "federation-proxy.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
federation-proxy (#3807). The upstream's namespace and port for the egress
rule, as {"namespace": ..., "port": ...}, read from upstream.url:
`http(s)://<svc>[:port]` is this release's namespace; `<svc>.<ns>.svc` or
`<svc>.<ns>.svc.cluster.local` is <ns>. The port is the URL's, else the
scheme's (http 80, https 443). `networkPolicy.upstreamNamespace` overrides the
namespace. Any other host (an external FQDN, an IP, a bare `<svc>.<ns>`, a
custom cluster domain) fails: the rule must not be a guess.
*/}}
{{- define "federation-proxy.upstreamTarget" -}}
{{- $u := urlParse (toString .Values.upstream.url) -}}
{{- $scheme := $u.scheme | default "" -}}
{{- if not (has $scheme (list "http" "https")) -}}
{{- fail (printf "federation-proxy: upstream.url %q must be http:// or https:// (#3807)" (toString .Values.upstream.url)) -}}
{{- end -}}
{{- $hostport := splitList ":" ($u.host | default "") -}}
{{- $host := index $hostport 0 -}}
{{- $port := ternary "443" "80" (eq $scheme "https") -}}
{{- if gt (len $hostport) 1 -}}
{{- $port = index $hostport 1 -}}
{{- end -}}
{{- $np := .Values.networkPolicy | default dict -}}
{{- $ns := "" -}}
{{- $parts := splitList "." $host -}}
{{- if $np.upstreamNamespace -}}
{{- $ns = $np.upstreamNamespace -}}
{{- else if and $host (eq (len $parts) 1) -}}
{{- $ns = .Release.Namespace -}}
{{- else if and (ge (len $parts) 3) (eq (index $parts 2) "svc") (or (eq (len $parts) 3) (eq (join "." (slice $parts 3)) "cluster.local")) -}}
{{- $ns = index $parts 1 -}}
{{- else -}}
{{- fail (printf "federation-proxy: cannot tell which namespace upstream.url %q is in, and the NetworkPolicy allows egress only to it. Use the Service form `<svc>.<namespace>.svc` (or `.svc.cluster.local`), or set networkPolicy.upstreamNamespace (a custom cluster domain needs it too); for an upstream outside the cluster set networkPolicy.restrictEgress=false and allow that egress yourself (#3807)" (toString .Values.upstream.url)) -}}
{{- end -}}
{{- dict "namespace" $ns "port" (int $port) | toJson -}}
{{- end }}
