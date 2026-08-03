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
{{- include "federation-gateway.validateRevokedSet" . }}
{{- include "federation-gateway.validatePreflight" . }}
{{- end }}

{{/*
Fail-loud guard for the store-mount preflight (#1316).

Both ways of disarming it silently are closed here, and both were REPRODUCED
before this guard existed rather than assumed:

  * empty sentinelKey — the check is `[ -e "$STORE_DIR/$SENTINEL_KEY" ]`, and
    with an empty key that tests the mount DIRECTORY, which always exists. The
    preflight would then exit 0 on every pod, in every mode, including on a
    completely unresolved mount. Nothing anywhere would say the check had
    stopped working.
  * a mode outside the enum — the script treats "not enforce" as warn, so a
    typo like `enforced` renders happily, reports success, and silently
    downgrades an operator who asked to fail closed into failing OPEN. That is
    the one direction this feature must never fail in by accident.
*/}}
{{- define "federation-gateway.validatePreflight" -}}
{{- if not (.Values.revokedSet.sentinelKey | default "") }}
{{- fail "federation-gateway: revokedSet.sentinelKey must not be empty — the preflight would test the mount directory itself, which always exists, so it would pass on every pod including a completely unresolved mount (#1316)" }}
{{- end }}
{{- if kindIs "bool" .Values.preflight.mode }}
{{- fail "federation-gateway: preflight.mode arrived as a BOOLEAN. YAML 1.1 — which Helm parses values files with — reads an unquoted `off` (and `on`/`yes`/`no`) as a boolean, so `mode: off` in a values FILE is not the string \"off\". Quote it: `mode: \"off\"`. (`--set preflight.mode=off` on the command line is unaffected.)" }}
{{- end }}
{{- if not (has .Values.preflight.mode (list "off" "warn" "enforce")) }}
{{- fail (printf "federation-gateway: preflight.mode must be \"off\", \"warn\" or \"enforce\", got %q — anything else is treated as warn, silently downgrading a fail-closed intent into fail-open (#1316)" .Values.preflight.mode) }}
{{- end }}
{{- end }}

{{/*
Fail-loud guard for the revoked-set line contract (#1235 / TRK-349).

revokedSet.tokenIdPattern is templated into revoked_check.lua and decides
which revoked.txt lines the ENFORCEMENT plane accepts. Two ways to make it
useless silently, both closed here:

  * empty — Lua's string.match against "" succeeds for every input, so an
    empty value would accept any line at all while still looking configured.
  * unanchored — a pattern without ^...$ matches a SUBSTRING, so a line
    could carry arbitrary bytes around a valid-looking id. Unambiguous whole
    lines are the entire point: the detection plane (the ADR-028 reconciler)
    reads the same file with its own parser, and the two ends can only agree
    if each line means exactly one thing.

A deploy that gets this wrong must fail at template time, not degrade into
a gateway that accepts a revoked set the reconciler reads differently.
*/}}
{{- define "federation-gateway.validateRevokedSet" -}}
{{- $p := .Values.revokedSet.tokenIdPattern | default "" }}
{{- if not $p }}
{{- fail "federation-gateway: revokedSet.tokenIdPattern must not be empty — an empty Lua pattern accepts every revoked.txt line (#1235)" }}
{{- end }}
{{- if not (and (hasPrefix "^" $p) (hasSuffix "$" $p)) }}
{{- fail (printf "federation-gateway: revokedSet.tokenIdPattern must be anchored with ^...$ so a line matches as a whole, got %q (#1235)" $p) }}
{{- end }}
{{- /* The value has to mean the SAME THING in three dialects — this Lua
       pattern, tenant-api's Go regexp, and the reconciler's Python re. The
       characters below are meaningful in some of them and literal in the
       others, so a value carrying one can never be equivalent across all
       three, however identical the three copies look. Both directions are
       rejected, because both have a natural-looking trap:
         {  }  \  |   regex-only. Pinning the id length with braces is the
                      obvious "tightening"; Lua reads it literally, matches
                      NOTHING, and the gateway then refuses every reload —
                      staying on the set it already had, with a freshly
                      started worker enforcing none at all.
         %            Lua-only (its escape character). `%x` is the natural
                      Lua-native tightening; it works perfectly in the
                      gateway while Go and Python read it literally and
                      reject every real id — the revoke path then errors
                      and the reconciler suspects everything. Loud rather
                      than silent, but still a contract that cannot hold.
       The literal-equality test in tests/ops/test_revoked_set_contract.py
       cannot catch either: it compares the three copies to each other, and
       they ARE equal — they just do not mean the same thing. That is the
       whole of #1235, one level up. */}}
{{- range $bad := list "{" "}" "\\" "|" "%" }}
{{- if contains $bad $p }}
{{- fail (printf "federation-gateway: revokedSet.tokenIdPattern contains %q, which does not mean the same thing in all three dialects the contract has to hold in (this Lua pattern, tenant-api's Go regexp, the reconciler's Python re). Braces/backslash/alternation are regex-only and Lua takes them literally; %% is Lua-only and Go and Python take it literally. Either way the three sides stop agreeing while their literals still look identical, which is exactly the drift #1235 exists to close. If the contract genuinely needs to change it must change on all three sides together and stay expressible in all three. Got %q" $bad $p) }}
{{- end }}
{{- end }}
{{- end }}
