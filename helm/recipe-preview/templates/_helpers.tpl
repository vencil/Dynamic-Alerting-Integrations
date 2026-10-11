{{/*
Expand the name of the chart.
*/}}
{{- define "recipe-preview.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "recipe-preview.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "recipe-preview.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: dynamic-alerting
{{- end }}

{{/*
Selector labels
*/}}
{{- define "recipe-preview.selectorLabels" -}}
app.kubernetes.io/name: {{ include "recipe-preview.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
A numeric value rendered as a decimal integer string for a PREVIEW_* env var
(#2907; the same helper as tenant-api.intEnv, #2862). Helm decodes a whole
number from a values file as float64, and `quote` prints a float64 of 1e6 or
more in exponent form ("2.097152e+06"); app.py's int() rejects that and the
container exits at startup. Numbers go through int64 first; a fractional
number fails the render, as does one outside the int64 range (int64
would wrap it to a negative number). A string is passed through unchanged.
Call with (list "<values path for the error>" <value>).
*/}}
{{- define "recipe-preview.intEnv" -}}
{{- $path := index . 0 -}}
{{- $v := index . 1 -}}
{{- if or (kindIs "float64" $v) (kindIs "int" $v) (kindIs "int64" $v) -}}
{{- if ne (float64 $v) (floor (float64 $v)) -}}
{{- fail (printf "%s must be a whole number, got %v" $path $v) -}}
{{- end -}}
{{- if ne (float64 (int64 $v)) (float64 $v) -}}
{{- fail (printf "%s is out of range for an integer, got %v" $path $v) -}}
{{- end -}}
{{- int64 $v | toString | quote -}}
{{- else -}}
{{- $v | quote -}}
{{- end -}}
{{- end }}
