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
{{- $url := toString .Values.upstream.url -}}
{{- $u := urlParse $url -}}
{{- $scheme := lower ($u.scheme | default "") -}}
{{- if not (has $scheme (list "http" "https")) -}}
{{- fail (printf "federation-proxy: upstream.url %q must be http:// or https:// (#3807)" $url) -}}
{{- end -}}
{{- /* DNS is case-insensitive, namespace names are not: lower-case first. */ -}}
{{- $host := lower ($u.hostname | default "") -}}
{{- if contains ":" $host -}}
{{- fail (printf "federation-proxy: upstream.url %q names an IP address; set networkPolicy.upstreamNamespace, or networkPolicy.restrictEgress=false and allow that egress yourself (#3807)" $url) -}}
{{- end -}}
{{- $np := .Values.networkPolicy | default dict -}}
{{- /* atoi, not int: int reads a leading 0 as octal (":010" -> 8). */ -}}
{{- $port := ternary 443 80 (eq $scheme "https") -}}
{{- with regexFind ":[0-9]+$" ($u.host | default "") -}}
{{- $port = atoi (trimPrefix ":" .) -}}
{{- end -}}
{{- /* hasKey, not truthiness: 0 must reach the range check, not read as unset. */ -}}
{{- if and (hasKey $np "upstreamPort") (ne (toString $np.upstreamPort) "") (ne (toString $np.upstreamPort) "<nil>") -}}
{{- $port = atoi (toString $np.upstreamPort) -}}
{{- end -}}
{{- if or (lt $port 1) (gt $port 65535) -}}
{{- fail (printf "federation-proxy: upstream port %d (from upstream.url %q or networkPolicy.upstreamPort) is not 1-65535 (#3807)" $port $url) -}}
{{- end -}}
{{- $ns := "" -}}
{{- $parts := splitList "." $host -}}
{{- if $np.upstreamNamespace -}}
{{- $ns = toString $np.upstreamNamespace -}}
{{- else if and $host (eq (len $parts) 1) -}}
{{- $ns = .Release.Namespace -}}
{{- else if and (ge (len $parts) 3) (eq (index $parts 2) "svc") (or (eq (len $parts) 3) (eq (join "." (slice $parts 3)) "cluster.local")) -}}
{{- $ns = index $parts 1 -}}
{{- else -}}
{{- fail (printf "federation-proxy: cannot tell which namespace upstream.url %q is in, and the NetworkPolicy allows egress only to it. Use the Service form `<svc>.<namespace>.svc` (or `.svc.cluster.local`), or set networkPolicy.upstreamNamespace (a custom cluster domain needs it too); for an upstream outside the cluster set networkPolicy.restrictEgress=false and allow that egress yourself (#3807)" $url) -}}
{{- end -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$" $ns) -}}
{{- fail (printf "federation-proxy: %q is not a namespace name (lower-case DNS label); a rule for it would select nothing and drop the upstream traffic silently (#3807)" $ns) -}}
{{- end -}}
{{- dict "namespace" $ns "port" $port | toJson -}}
{{- end }}
