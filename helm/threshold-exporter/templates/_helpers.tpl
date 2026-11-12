{{- /*
  The ConfigMap the exporter reads its conf.d from (#1425): the operator's
  `thresholdConfig.existingConfigMap`, else the chart's own
  `threshold-config`. ONE definition for every reader of that tree — the
  exporter's Deployment and the config-validation CronJob (#3353), which
  must judge exactly what the exporter reads. A non-string value
  (`--set thresholdConfig.existingConfigMap=true`) would mount a ConfigMap
  literally named "true"; it is refused instead.
*/ -}}
{{- define "threshold-exporter.configMapName" -}}
{{- $existing := .Values.thresholdConfig.existingConfigMap -}}
{{- if not (or (kindIs "invalid" $existing) (kindIs "string" $existing)) -}}
{{- fail (printf "thresholdConfig.existingConfigMap must be a ConfigMap NAME (string), got %s" (kindOf $existing)) -}}
{{- end -}}
{{- $existing | default "threshold-config" -}}
{{- end -}}

{{- /*
  The config-validation CronJob's memory (#3353 owner ruling): NOT a second
  number to keep in step with the tree, but the exporter's own
  `resources.limits.memory` × `configValidation.memoryFactor`. The operator
  who raises the exporter for more tenants raises this with it. Measured on
  generated trees (synthetic-v2): validate-config peaked at 2.5× the
  exporter's load RSS at 500 tenants and 3.2× at 5000; the default factor
  is 4. Too small still fails LOUD: the Job is OOM-killed, fails, and
  CronJobLastRunFailed fires.
  The quantity must be an integer with a Kubernetes binary or decimal suffix
  (Ki…Ti, k/M/G/T — the decimal kilo is a lowercase k) or plain bytes;
  anything else fails the render rather than guessing.
*/ -}}
{{- define "threshold-exporter.validationMemory" -}}
{{- $limits := (.Values.resources | default dict).limits | default dict -}}
{{- $q := toString ($limits.memory | default "") -}}
{{- if not $q -}}
{{- fail "configValidation.enabled needs resources.limits.memory: the CronJob's memory is the exporter's limit × configValidation.memoryFactor (#3353)" -}}
{{- end -}}
{{- if not (regexMatch "^[0-9]+(Ki|Mi|Gi|Ti|k|M|G|T)?$" $q) -}}
{{- fail (printf "resources.limits.memory %q: configValidation derives its memory from it and reads only an integer with an optional Ki/Mi/Gi/Ti/k/M/G/T suffix (#3353)" $q) -}}
{{- end -}}
{{- /* memoryFactor: a positive integer — values.schema.json's to enforce. */ -}}
{{- $n := regexFind "^[0-9]+" $q | atoi -}}
{{- $unit := regexReplaceAll "^[0-9]+" $q "" -}}
{{- printf "%d%s" (mul $n (int64 .Values.configValidation.memoryFactor)) $unit -}}
{{- end -}}
