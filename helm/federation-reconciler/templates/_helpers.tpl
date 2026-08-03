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

{{/*
Fail-loud guard for the store-mount preflight (#1316). Mirrors
helm/federation-gateway's `validatePreflight` — see that chart's copy for the
two reproduced silent-disarm paths this closes (an empty sentinelKey makes the
check test the mount DIRECTORY, which always exists; a mode outside the enum is
treated as warn, silently downgrading a fail-closed intent into fail-open).
*/}}
{{- define "federation-reconciler.validatePreflight" -}}
{{- if not (.Values.store.sentinelKey | default "") }}
{{- fail "federation-reconciler: store.sentinelKey must not be empty — the preflight would test the mount directory itself, which always exists, so it would pass on every pod including a completely unresolved mount (#1316)" }}
{{- end }}
{{- if kindIs "bool" .Values.preflight.mode }}
{{- fail "federation-reconciler: preflight.mode arrived as a BOOLEAN. YAML 1.1 — which Helm parses values files with — reads an unquoted `off` (and `on`/`yes`/`no`) as a boolean, so `mode: off` in a values FILE is not the string \"off\". Quote it: `mode: \"off\"`. (`--set preflight.mode=off` on the command line is unaffected.)" }}
{{- end }}
{{- if not (has .Values.preflight.mode (list "off" "warn" "enforce")) }}
{{- fail (printf "federation-reconciler: preflight.mode must be \"off\", \"warn\" or \"enforce\", got %q — anything else is treated as warn, silently downgrading a fail-closed intent into fail-open (#1316)" .Values.preflight.mode) }}
{{- end }}
{{- end }}

{{/* Fully-qualified image ref with an optional digest pin. */}}
{{- define "federation-reconciler.image" -}}
{{- $img := printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- with .Values.image.digest }}{{- $img = printf "%s@%s" $img . -}}{{- end -}}
{{- $img -}}
{{- end -}}
